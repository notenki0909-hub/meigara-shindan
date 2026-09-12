# -*- coding: utf-8 -*-
"""
『10年保有できる優良企業』ランキングの母集団を analyze_long.generate_long() で
一括診断し、専用サマリ＋専用レポートを書き出すバッチ。

  python batch_long.py jp [--only-stale] [--hours 20] [--sleep 2.0] [--limit N]
  python batch_long.py us [...]
  python batch_long.py us --only-codes JPM,BAC,...   # 特定銘柄だけ再診断（スコアリング変更の
                                                       # 部分反映用。--only-stale の鮮度チェックより優先）
  python batch_long.py jp --only-missing-pfd         # portfolio_data が無い銘柄だけ穴埋め
                                                       # （ポートフォリオ機能の後付け導入時用）

出力（配当株ツールとは完全に別ディレクトリ。既存 site/summaries・site/reports は触らない）:
  site/long_summaries/<code>.json          site/us/long_summaries/<TICKER>.json
  site/long/reports/<code>.html            site/us/long/reports/<TICKER>.html
  site/long/portfolio_data/<code>.json     site/us/long/portfolio_data/<TICKER>.json
                                            … ポートフォリオ機能用（直近10年の日次終値・配当履歴）

analyze_long は配当を評価しない独自エンジン（品質スコア＋買い時スコア）。既存の
配当株バッチ（batch.py / batch_us.py）とはサマリのスキーマが違うため流用しない。
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))

CFG = {
    "jp": {
        "universe": "universe_long.json",
        "sum": os.path.join(HERE, "site", "long_summaries"),
        "rep": os.path.join(HERE, "site", "long", "reports"),
        "pfd": os.path.join(HERE, "site", "long", "portfolio_data"),
        "log": os.path.join(HERE, "site", "long", "batch_log.json"),
    },
    "us": {
        "universe": "universe_long_us.json",
        "sum": os.path.join(HERE, "site", "us", "long_summaries"),
        "rep": os.path.join(HERE, "site", "us", "long", "reports"),
        "pfd": os.path.join(HERE, "site", "us", "long", "portfolio_data"),
        "log": os.path.join(HERE, "site", "us", "long", "batch_log.json"),
    },
}


def _codes(path, market):
    j = json.load(open(path, encoding="utf-8"))
    items = j.get("tickers") or j.get("codes") or []
    out = []
    for it in items:
        c = str(it.get("ticker") or it.get("code"))
        out.append((c.upper() if market == "us" else c, it.get("name")))
    return out


def _fresh(path, hours):
    if not os.path.isfile(path):
        return False
    try:
        gen = dt.datetime.fromisoformat(json.load(open(path, encoding="utf-8"))["_generated_at"])
    except Exception:
        return False
    return (dt.datetime.now() - gen).total_seconds() <= hours * 3600


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["jp", "us"])
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-stale", action="store_true",
                    help="サマリが --hours より新しい銘柄はスキップ")
    ap.add_argument("--hours", type=float, default=20)
    ap.add_argument("--only-codes", type=str, default="",
                    help="カンマ区切りのコード/ティッカーだけ診断する（--only-staleより優先、"
                         "鮮度に関わらず必ず再診断）。スコアリングロジック変更を一部銘柄だけ"
                         "反映したいときに使う。")
    ap.add_argument("--only-missing-pfd", action="store_true",
                    help="portfolio_data/<code>.json が無い銘柄だけ診断する（--only-codes/"
                         "--only-staleより優先）。ポートフォリオ機能を後から追加した際、"
                         "既存サマリの鮮度に関わらず株価履歴ファイルだけ穴埋めしたい場合に使う。")
    args = ap.parse_args()

    c = CFG[args.market]
    os.makedirs(c["sum"], exist_ok=True)
    os.makedirs(c["rep"], exist_ok=True)
    os.makedirs(c["pfd"], exist_ok=True)

    import analyze_long
    if args.market == "jp":
        import analyze
        cfg = analyze.load_config()
    else:
        import analyze_us
        cfg = analyze_us.load_config_us()

    codes = _codes(os.path.join(HERE, c["universe"]), args.market)
    if args.limit:
        codes = codes[: args.limit]

    counts = {"skip": 0, "ok": 0, "fail": 0}
    failed = []
    if args.only_missing_pfd:
        todo = [(code, name) for code, name in codes
                if not os.path.isfile(os.path.join(c["pfd"], f"{code}.json"))]
    elif args.only_codes:
        want = {x.strip().upper() if args.market == "us" else x.strip()
                for x in args.only_codes.split(",") if x.strip()}
        todo = [(code, name) for code, name in codes if code in want]
    else:
        todo = [(code, name) for code, name in codes
                if not (args.only_stale and _fresh(os.path.join(c["sum"], f"{code}.json"), args.hours))]
    counts["skip"] = len(codes) - len(todo)

    print(f"[{args.market}] 母集団 {len(codes)}  skip(新しい) {counts['skip']}  "
          f"-> 今回診断 {len(todo)}  sleep={args.sleep}s")
    t0 = time.time()
    for i, (code, name) in enumerate(todo, 1):
        try:
            r = analyze_long.generate_long(code, cfg=cfg, market=args.market, name=name)
            if not r.get("ok"):
                counts["fail"] += 1
                failed.append({"code": code, "error": (r.get("error") or "")[:200]})
                print(f"  x {code}  {(r.get('error') or '')[:120]}")
            else:
                if r.get("html"):
                    open(os.path.join(c["rep"], f"{code}.html"), "w",
                         encoding="utf-8").write(r["html"])
                s = dict(r["summary"])
                s["_generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
                json.dump(s, open(os.path.join(c["sum"], f"{code}.json"), "w",
                                  encoding="utf-8"), ensure_ascii=False, indent=1)
                pd_ = r.get("portfolio_data") or {"prices": [], "divs": []}
                json.dump(pd_, open(os.path.join(c["pfd"], f"{code}.json"), "w",
                                    encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
                counts["ok"] += 1
                if i % 25 == 0:
                    print(f"  [{i}/{len(todo)}] ok {code}")
        except Exception as e:
            counts["fail"] += 1
            failed.append({"code": code, "error": f"{e.__class__.__name__}: {e}"})
            traceback.print_exc()
        if i < len(todo):
            time.sleep(args.sleep)

    dur = time.time() - t0
    os.makedirs(os.path.dirname(c["log"]), exist_ok=True)
    json.dump({"ran_at": dt.datetime.now().isoformat(timespec="seconds"),
               "seconds": round(dur, 1), "counts": counts, "failed": failed},
              open(c["log"], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"完了 {dur:.0f}s  {counts}")
    if counts["fail"] and counts["ok"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
