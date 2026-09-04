# -*- coding: utf-8 -*-
"""
tdnet_watch.py が検知した pending_earnings.json の銘柄を再取得し、yfinance側に
新しい決算実績（earn_disc_date）が反映されたか確認する。反映されていれば
site/reports・site/summaries を更新して監視終了、まだなら次回リトライを予約する。

  python earnings_retry.py            # 期限が来たものだけ処理
  python earnings_retry.py --force    # 期限を無視して全pendingを処理（テスト用）
  python earnings_retry.py --status   # 何もせず一覧表示だけ

想定運用：6時間おきに実行（GitHub Actions cron。未実装）。tdnet_watch.py と対で
「決算翌日反映」を実現する（README/調べもの.txt 参照）。

反映確認は yfinance の earnings_dates 最新実績日（analyze.generate の
summary['earn_disc_date']）が TDnet開示日以降になったかで判定。まだなら
6時間おきに最大3日（9回）リトライし、それでも反映されなければ giveup として
週次のフルバッチに委ねる（このスクリプトは追わない）。
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import analyze
import batch  # REP/SUM のパスとファイル書き出し規約を共有するため

HERE = os.path.dirname(__file__)
PENDING = os.path.join(HERE, "pending_earnings.json")

JST = dt.timezone(dt.timedelta(hours=9))
RETRY_HOURS = 6
MAX_ATTEMPTS = 9          # 6時間 x 9 = 54時間（約3日弱の間隔違いを許容）
PRUNE_AFTER_DAYS = 14     # resolved/giveup を掃除するまでの保持日数


def load_pending():
    if not os.path.isfile(PENDING):
        return {}
    try:
        return json.load(open(PENDING, encoding="utf-8"))
    except Exception:
        return {}


def save_pending(p):
    json.dump(p, open(PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def parse_pubdate(pubdate_str):
    """TDnet の 'YYYY-MM-DD HH:MM:SS' を date に。壊れていたら None。"""
    try:
        return dt.datetime.strptime(pubdate_str[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def is_due(entry, now, force):
    if entry.get("status") != "pending":
        return False
    if force:
        return True
    try:
        return dt.datetime.fromisoformat(entry["next_retry_at"]) <= now
    except Exception:
        return True


def try_resolve(code, entry, cfg):
    """1銘柄を再取得して解決したか判定。→ (resolved: bool, error: str|None)"""
    res = analyze.generate(code, name=entry.get("name"), cfg=cfg)
    if not res["ok"]:
        return False, res["error"]
    s = res["summary"]
    new_disc = s.get("earn_disc_date")
    pub_date = parse_pubdate(entry.get("pubdate", ""))
    resolved = bool(new_disc) and (pub_date is None or
                                    dt.date.fromisoformat(new_disc) >= pub_date - dt.timedelta(days=1))
    if resolved:
        open(os.path.join(batch.REP, f"{code}.html"), "w", encoding="utf-8").write(res["html"])
        open(os.path.join(batch.REP, f"{code}.md"), "w", encoding="utf-8").write(res["md"])
        s["_generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        s["_earnings_reflected_via"] = "tdnet_retry"
        json.dump(s, open(os.path.join(batch.SUM, f"{code}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    return resolved, None


def run(sleep, force, dry):
    os.makedirs(batch.REP, exist_ok=True)
    os.makedirs(batch.SUM, exist_ok=True)
    pending = load_pending()
    if not pending:
        print("pending_earnings.json は空です（tdnet_watch.py で検知した銘柄がありません）。")
        return

    now = dt.datetime.now(JST)
    due = [c for c, e in pending.items() if is_due(e, now, force)]
    print(f"監視中 {sum(1 for e in pending.values() if e['status']=='pending')}件"
          f"　うち期限到来 {len(due)}件")
    if dry:
        return

    cfg = analyze.load_config()
    resolved_n, retry_n, giveup_n, err_n = 0, 0, 0, 0
    for i, code in enumerate(due, 1):
        e = pending[code]
        try:
            resolved, err = try_resolve(code, e, cfg)
        except Exception as ex:
            resolved, err = False, f"{ex.__class__.__name__}: {ex}"
        if err:
            e["last_error"] = err
            err_n += 1
            print(f"  ✗ {code} {e.get('name')}  エラー: {err}")
        elif resolved:
            e["status"] = "resolved"
            e["resolved_at"] = now.isoformat(timespec="seconds")
            resolved_n += 1
            print(f"  ✓ 反映確認 {code} {e.get('name')}")
        else:
            e["attempts"] = e.get("attempts", 0) + 1
            if e["attempts"] >= MAX_ATTEMPTS:
                e["status"] = "giveup"
                giveup_n += 1
                print(f"  … 断念 {code} {e.get('name')}（{MAX_ATTEMPTS}回試行・週次フル再構築に委ねる）")
            else:
                e["next_retry_at"] = (now + dt.timedelta(hours=RETRY_HOURS)).isoformat(timespec="seconds")
                retry_n += 1
                print(f"  ・未反映 {code} {e.get('name')}（{e['attempts']}/{MAX_ATTEMPTS}回目・次回+{RETRY_HOURS}h）")
        if i < len(due):
            time.sleep(sleep)

    cutoff = now - dt.timedelta(days=PRUNE_AFTER_DAYS)
    pruned = 0
    for code in list(pending.keys()):
        e = pending[code]
        if e.get("status") in ("resolved", "giveup"):
            ts = e.get("resolved_at") or e.get("detected_at")
            try:
                if dt.datetime.fromisoformat(ts) < cutoff:
                    del pending[code]
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
            print(f"  {e['code']} {e.get('name','')}  検知{e.get('detected_at','')}"
                  f"  試行{e.get('attempts',0)}  次回{e.get('next_retry_at','')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--force", action="store_true", help="期限を無視して全pendingを処理")
    ap.add_argument("--dry", action="store_true", help="対象件数を見るだけで実行しない")
    ap.add_argument("--status", action="store_true", help="一覧表示のみ")
    args = ap.parse_args()
    if args.status:
        print_status()
        return
    run(args.sleep, args.force, args.dry)


if __name__ == "__main__":
    main()
