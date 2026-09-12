# -*- coding: utf-8 -*-
"""
米国株版の「決算翌日反映」検知〜再取得。日本株側は TDnet（適時開示の外部フィード）を
30分おきにポーリングして「今日どの銘柄が決算を出したか」を検知するが、米国には
それに相当する無料・網羅的なリアルタイム開示フィードが無い。

代わりに、yfinance が銘柄ごとに保持している「次回決算予定日」（next_earn。
build_earnings() が calendar/earnings_dates から算出し、毎晩のバッチで
site/us/summaries/<TICKER>.json に既に書き出し済み）を使う。予定日を過ぎても
まだ実績（earn_disc_date）が予定日以降に進んでいなければ「発表済みだが
yfinance未反映」とみなして優先的に再取得する。

検知に新規のyfinance呼び出しが要らない（ローカルのsummary jsonを読むだけ）ため、
日本株版のように「検知」と「再取得確認」を別スクリプト・別cronに分ける必要がなく、
1本のスクリプトで完結する。

  python earnings_watch_us.py                # 検知＋期限到来分の再取得
  python earnings_watch_us.py --dry           # 検知件数を見るだけで再取得しない
  python earnings_watch_us.py --status        # 一覧表示のみ
  python earnings_watch_us.py --force         # 期限を無視して全pendingを再取得（テスト用）

想定運用：6時間おきに実行（GitHub Actions cron。earnings-watch-us.yml）。
反映確認は yfinance の実績決算日（analyze_us.generate_us の summary['earn_disc_date']）
が next_earn（検知時点の予定日）の前日以降になったかで判定。まだなら6時間おきに
最大3日弱（9回）リトライし、それでも反映されなければ giveup として翌日の
nightly-us.yml の毎晩フル再取得に委ねる（このスクリプトは追わない）。
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import analyze_us
import batch_us  # REP/SUM/PFD のパスとファイル書き出し規約を共有するため

HERE = os.path.dirname(__file__)
UNIV = os.path.join(HERE, "universe_us.json")
PENDING = os.path.join(HERE, "pending_earnings_us.json")

RETRY_HOURS = 6
MAX_ATTEMPTS = 9          # 6時間 x 9 = 54時間（約3日弱）
PRUNE_AFTER_DAYS = 14     # resolved/giveup を掃除するまでの保持日数


def load_json_or(path, default):
    if os.path.isfile(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return default


def load_universe():
    j = load_json_or(UNIV, {})
    items = j.get("tickers", j.get("codes", []))
    out = {}
    for it in items:
        if isinstance(it, dict):
            t = str(it.get("ticker") or it.get("code") or "").upper()
            if t:
                out[t] = it.get("name")
        else:
            out[str(it).upper()] = None
    return out


def load_pending():
    return load_json_or(PENDING, {})


def save_pending(p):
    json.dump(p, open(PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _date(s):
    try:
        return dt.date.fromisoformat(s) if s else None
    except Exception:
        return None


def scan_new(uni, pending, today, log=print):
    """summary jsonの next_earn を見て、予定日を過ぎたのにまだ反映されていない
    銘柄を pending に登録する。新規のyfinance呼び出しはしない。"""
    new_count = 0
    for ticker, name in uni.items():
        p = os.path.join(batch_us.SUM, f"{ticker}.json")
        s = load_json_or(p, None)
        if not s:
            continue
        next_earn = _date(s.get("next_earn"))
        if not next_earn or next_earn > today:
            continue  # まだ予定日前
        disc_date = _date(s.get("earn_disc_date"))
        if disc_date and disc_date >= next_earn - dt.timedelta(days=1):
            continue  # このバッチ自体で既に反映済み（追跡不要）

        existing = pending.get(ticker)
        if existing and existing.get("target_date") == next_earn.isoformat():
            continue  # 同じ決算サイクルを既に追跡中 or 処理済み

        pending[ticker] = {
            "ticker": ticker, "name": name or s.get("name"),
            "target_date": next_earn.isoformat(),
            "detected_at": dt.datetime.now().isoformat(timespec="seconds"),
            "status": "pending", "attempts": 0,
            "next_retry_at": dt.datetime.now().isoformat(timespec="seconds"),  # 即時に1回目
        }
        new_count += 1
        log(f"  + 検知 {ticker} {name or ''}  予定日{next_earn.isoformat()}を経過・未反映")
    return new_count


def is_due(entry, now, force):
    if entry.get("status") != "pending":
        return False
    if force:
        return True
    try:
        return dt.datetime.fromisoformat(entry["next_retry_at"]) <= now
    except Exception:
        return True


def try_resolve(ticker, entry, cfg):
    """1銘柄を再取得して反映を確認。→ (resolved: bool, error: str|None)"""
    res = analyze_us.generate_us(ticker, cfg=cfg)
    if not res["ok"]:
        return False, res["error"]
    s = res["summary"]
    disc_date = _date(s.get("earn_disc_date"))
    target = _date(entry.get("target_date"))
    resolved = bool(disc_date) and (target is None or disc_date >= target - dt.timedelta(days=1))
    if resolved:
        open(os.path.join(batch_us.REP, f"{ticker}.html"), "w", encoding="utf-8").write(res["html"])
        open(os.path.join(batch_us.REP, f"{ticker}.md"), "w", encoding="utf-8").write(res["md"])
        s = dict(s)
        s["_generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        s["_earnings_reflected_via"] = "earnings_watch_us"
        json.dump(s, open(os.path.join(batch_us.SUM, f"{ticker}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        pd = res.get("portfolio_data") or {"prices": [], "divs": []}
        json.dump(pd, open(os.path.join(batch_us.PFD, f"{ticker}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
    return resolved, None


def run(sleep, force, dry):
    os.makedirs(batch_us.REP, exist_ok=True)
    os.makedirs(batch_us.SUM, exist_ok=True)
    os.makedirs(batch_us.PFD, exist_ok=True)

    uni = load_universe()
    pending = load_pending()
    today = dt.datetime.now(dt.timezone.utc).date()

    new_count = scan_new(uni, pending, today)
    save_pending(pending)
    n_pending = sum(1 for e in pending.values() if e["status"] == "pending")
    print(f"検知（新規）{new_count}件　監視中合計 {n_pending}件")

    now = dt.datetime.now()
    due = [t for t, e in pending.items() if is_due(e, now, force)]
    print(f"うち期限到来 {len(due)}件")
    if dry:
        return

    cfg = analyze_us.load_config_us()
    resolved_n, retry_n, giveup_n, err_n = 0, 0, 0, 0
    for i, ticker in enumerate(due, 1):
        e = pending[ticker]
        try:
            resolved, err = try_resolve(ticker, e, cfg)
        except Exception as ex:
            resolved, err = False, f"{ex.__class__.__name__}: {ex}"
        if err:
            e["last_error"] = str(err)[:500]
            err_n += 1
            print(f"  x {ticker} {e.get('name')}  エラー: {err}")
        elif resolved:
            e["status"] = "resolved"
            e["resolved_at"] = now.isoformat(timespec="seconds")
            resolved_n += 1
            print(f"  o 反映確認 {ticker} {e.get('name')}")
        else:
            e["attempts"] = e.get("attempts", 0) + 1
            if e["attempts"] >= MAX_ATTEMPTS:
                e["status"] = "giveup"
                giveup_n += 1
                print(f"  ... 断念 {ticker} {e.get('name')}（{MAX_ATTEMPTS}回試行・翌晩のnightly-us.ymlに委ねる）")
            else:
                e["next_retry_at"] = (now + dt.timedelta(hours=RETRY_HOURS)).isoformat(timespec="seconds")
                retry_n += 1
                print(f"  . 未反映 {ticker} {e.get('name')}（{e['attempts']}/{MAX_ATTEMPTS}回目・次回+{RETRY_HOURS}h）")
        if i < len(due):
            time.sleep(sleep)

    cutoff = now - dt.timedelta(days=PRUNE_AFTER_DAYS)
    pruned = 0
    for ticker in list(pending.keys()):
        e = pending[ticker]
        if e.get("status") in ("resolved", "giveup"):
            ts = e.get("resolved_at") or e.get("detected_at")
            try:
                if dt.datetime.fromisoformat(ts) < cutoff:
                    del pending[ticker]
                    pruned += 1
            except Exception:
                pass

    save_pending(pending)
    print(f"\n完了: 反映{resolved_n} / 再試行{retry_n} / 断念{giveup_n} / エラー{err_n}"
          f"（古い完了分{pruned}件を整理）")


def print_status():
    pending = load_pending()
    if not pending:
        print("pending なし")
        return
    by_status = {}
    for e in pending.values():
        by_status.setdefault(e["status"], []).append(e)
    for status, items in by_status.items():
        print(f"\n[{status}] {len(items)}件")
        for e in sorted(items, key=lambda x: x.get("detected_at", "")):
            print(f"  {e['ticker']} {e.get('name','')}  予定日{e.get('target_date','')}"
                  f"  検知{e.get('detected_at','')}  試行{e.get('attempts',0)}"
                  f"  次回{e.get('next_retry_at','')}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--force", action="store_true", help="期限を無視して全pendingを処理")
    ap.add_argument("--dry", action="store_true", help="検知・対象件数を見るだけで再取得しない")
    ap.add_argument("--status", action="store_true", help="一覧表示のみ")
    args = ap.parse_args()
    if args.status:
        print_status()
        return
    run(args.sleep, args.force, args.dry)


if __name__ == "__main__":
    main()
