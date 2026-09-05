# -*- coding: utf-8 -*-
"""
米国株ユニバースの全銘柄を analyze_us.generate_us() で回してレポート＋サマリを
書き出すバッチ。日本株版 batch.py の米国版。

  python batch_us.py --codes universe_us.json           # universe_us.json の tickers を全部
  python batch_us.py --codes KO,PG,JNJ                  # カンマ区切りで直接
  python batch_us.py --codes universe_us.json --only-stale --hours 20
  python batch_us.py --codes universe_us.json --limit 30 --sleep 1.5

出力:
  site/us/reports/<TICKER>.html   … 個別レポート
  site/us/reports/<TICKER>.md
  site/us/summaries/<TICKER>.json … ランキング集計用サマリ（generate_us() の summary + 生成時刻）
  site/us/batch_log.json          … 直近の実行結果（ok/skip/fail 件数、失敗ティッカー）

レート制限対策：--sleep 秒（既定1.5）を銘柄間に必ず入れる。config は 1 回だけ読んで
使い回す（各銘柄で JSON を読み直させない）。失敗は記録して次へ進む（止めない）。
NYSE の引け（日本時間 朝5〜6時）後に回す想定。日本株バッチ（03:00 JST）とは別枠。
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import sys
import time
import traceback

import analyze_us

SITE = os.path.join(os.path.dirname(__file__), "site", "us")
REP = os.path.join(SITE, "reports")
SUM = os.path.join(SITE, "summaries")


def load_codes(spec):
    """'KO,PG' でも 'universe_us.json' でも受ける。→ [(ticker, name_or_None), ...]"""
    if os.path.isfile(spec):
        j = json.load(open(spec, encoding="utf-8"))
        items = j.get("tickers", j.get("codes", j if isinstance(j, list) else []))
        out = []
        for item in items:
            if isinstance(item, dict):
                out.append((str(item.get("ticker") or item.get("code")).upper(), item.get("name")))
            else:
                out.append((str(item).upper(), None))
        return out
    return [(c.strip().upper(), None) for c in spec.split(",") if c.strip()]


def is_stale(ticker, hours):
    p = os.path.join(SUM, f"{ticker}.json")
    if not os.path.isfile(p):
        return True
    try:
        j = json.load(open(p, encoding="utf-8"))
        gen = dt.datetime.fromisoformat(j["_generated_at"])
    except Exception:
        return True
    return (dt.datetime.now() - gen).total_seconds() > hours * 3600


def run_one(ticker, cfg):
    r = analyze_us.generate_us(ticker, cfg=cfg)
    if not r["ok"]:
        return ticker, "fail", r["error"]
    open(os.path.join(REP, f"{ticker}.html"), "w", encoding="utf-8").write(r["html"])
    open(os.path.join(REP, f"{ticker}.md"), "w", encoding="utf-8").write(r["md"])
    s = dict(r["summary"])
    s["_generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    json.dump(s, open(os.path.join(SUM, f"{ticker}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return ticker, "ok", None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="universe_us.json", help="universe_us.json か KO,PG,...")
    ap.add_argument("--sleep", type=float, default=1.5, help="銘柄間スリープ秒（レート制限対策）")
    ap.add_argument("--workers", type=int, default=1, help="並列数（3以下推奨）")
    ap.add_argument("--limit", type=int, default=0, help="先頭N件だけ（0=全部）")
    ap.add_argument("--only-stale", action="store_true", help="--hours より古いサマリだけ再生成")
    ap.add_argument("--hours", type=float, default=20)
    args = ap.parse_args()

    os.makedirs(REP, exist_ok=True)
    os.makedirs(SUM, exist_ok=True)
    cfg = analyze_us.load_config_us()

    codes = load_codes(args.codes)
    if args.only_stale:
        codes = [(c, n) for c, n in codes if is_stale(c, args.hours)]
    if args.limit:
        codes = codes[: args.limit]

    print(f"対象 {len(codes)} 銘柄  workers={args.workers}  sleep={args.sleep}s")
    t0 = time.time()
    counts = {"ok": 0, "skip": 0, "fail": 0}
    failed = []

    def emit(ticker, status, err):
        counts[status] = counts.get(status, 0) + 1
        mark = {"ok": "OK", "fail": "NG", "skip": "-"}.get(status, "?")
        line = f"  {mark} {ticker}"
        if err:
            line += f"  {str(err)[:200]}"
            failed.append({"ticker": ticker, "error": str(err)[:500]})
        print(line)

    if args.workers <= 1:
        for i, (ticker, _name) in enumerate(codes, 1):
            try:
                _, st, err = run_one(ticker, cfg)
            except Exception as e:
                st, err = "fail", f"{e.__class__.__name__}: {e}"
                traceback.print_exc()
            emit(ticker, st, err)
            if i < len(codes):
                time.sleep(args.sleep)
    else:
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {}
            for ticker, _name in codes:
                futs[ex.submit(run_one, ticker, cfg)] = ticker
                time.sleep(args.sleep)
            for fut in cf.as_completed(futs):
                ticker = futs[fut]
                try:
                    _, st, err = fut.result()
                except Exception as e:
                    st, err = "fail", f"{e.__class__.__name__}: {e}"
                emit(ticker, st, err)

    dur = time.time() - t0
    log = {"ran_at": dt.datetime.now().isoformat(timespec="seconds"),
           "seconds": round(dur, 1), "counts": counts, "failed": failed}
    json.dump(log, open(os.path.join(SITE, "batch_log.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n完了 {dur:.0f}s  ok={counts['ok']} fail={counts['fail']}  "
          f"→ site/us/summaries/  （失敗は site/us/batch_log.json）")
    if counts["fail"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
