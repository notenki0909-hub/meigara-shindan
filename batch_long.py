# -*- coding: utf-8 -*-
"""
『10年保有できる優良企業』ランキングの母集団のうち、既存の配当株バッチが
カバーしていない銘柄だけを診断して site/long_summaries/ に書き出すバッチ。

  python batch_long.py jp [--only-stale] [--hours 20] [--sleep 2.0] [--limit N]
  python batch_long.py us [...]

設計:
- 出力先は site/long_summaries/（JP）・site/us/long_summaries/（US）。
  既存の site/summaries/・site/us/summaries/ には一切書かない
  （＝配当株ランキング rank.py / rank_us.py に影響を与えない）。
- 既存の site/summaries/<code>.json が新しければ再取得しない（rank_long がそちらを読む）。
  母集団のうち配当株ユニバースにも入っている銘柄は、既存 nightly が毎晩更新するので
  ここでは触らない。ここが「取得済みデータの流用」。
- analyze.generate() / analyze_us.generate_us() をそのまま使う（エンジン無改変）。
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
        "primary_sum": os.path.join(HERE, "site", "summaries"),
        "primary_rep": os.path.join(HERE, "site", "reports"),
        "long_sum": os.path.join(HERE, "site", "long_summaries"),
        "long_rep": os.path.join(HERE, "site", "long", "reports"),
        "log": os.path.join(HERE, "site", "long", "batch_log.json"),
    },
    "us": {
        "universe": "universe_long_us.json",
        "primary_sum": os.path.join(HERE, "site", "us", "summaries"),
        "primary_rep": os.path.join(HERE, "site", "us", "reports"),
        "long_sum": os.path.join(HERE, "site", "us", "long_summaries"),
        "long_rep": os.path.join(HERE, "site", "us", "long", "reports"),
        "log": os.path.join(HERE, "site", "us", "long", "batch_log.json"),
    },
}


def _codes(path, market):
    j = json.load(open(path, encoding="utf-8"))
    items = j.get("tickers") or j.get("codes") or []
    out = []
    for it in items:
        c = str(it.get("ticker") or it.get("code"))
        if market == "us":
            c = c.upper()
        out.append((c, it.get("name")))
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
                    help="long_summaries 側が --hours より新しければskip（primaryは常にskip）")
    ap.add_argument("--hours", type=float, default=20)
    args = ap.parse_args()

    c = CFG[args.market]
    os.makedirs(c["long_sum"], exist_ok=True)
    os.makedirs(c["long_rep"], exist_ok=True)

    if args.market == "jp":
        import analyze
        cfg = analyze.load_config()
        jgb = cfg["rules"].get("market", {}).get("jgb_10y")

        def gen(code, name):
            return analyze.generate(code, name=name, jgb=jgb, cfg=cfg)
    else:
        import analyze_us
        cfg = analyze_us.load_config_us()

        def gen(code, name):
            return analyze_us.generate_us(code, cfg=cfg)

    codes = _codes(os.path.join(HERE, c["universe"]), args.market)
    if args.limit:
        codes = codes[: args.limit]

    counts = {"reuse": 0, "skip": 0, "ok": 0, "fail": 0}
    failed = []
    todo = []
    for code, name in codes:
        if _fresh(os.path.join(c["primary_sum"], f"{code}.json"), args.hours):
            counts["reuse"] += 1
            continue
        if args.only_stale and _fresh(os.path.join(c["long_sum"], f"{code}.json"), args.hours):
            counts["skip"] += 1
            continue
        todo.append((code, name))

    print(f"[{args.market}] 母集団 {len(codes)}  流用(既存サマリ) {counts['reuse']}  "
          f"long側skip {counts['skip']}  → 今回診断 {len(todo)}  sleep={args.sleep}s")
    t0 = time.time()
    for i, (code, name) in enumerate(todo, 1):
        try:
            r = gen(code, name)
            if not r.get("ok"):
                counts["fail"] += 1
                failed.append({"code": code, "error": r.get("error")})
                print(f"  ✗ {code}  {r.get('error')}")
            else:
                # 個別レポート(.html)は、配当株バッチ側（site/reports/）に既にあるなら
                # 書かない（rank_long.py が ../reports/ へリンクするので重複を持たない）。
                # .md はこのランキングからは参照しないので生成しない（リポジトリ肥大の抑制）。
                has_shared = os.path.isfile(os.path.join(c["primary_rep"], f"{code}.html"))
                if not has_shared and r.get("html"):
                    open(os.path.join(c["long_rep"], f"{code}.html"), "w",
                         encoding="utf-8").write(r["html"])
                s = dict(r["summary"])
                s["_generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
                json.dump(s, open(os.path.join(c["long_sum"], f"{code}.json"), "w",
                                  encoding="utf-8"), ensure_ascii=False, indent=1)
                counts["ok"] += 1
                if i % 25 == 0:
                    print(f"  [{i}/{len(todo)}] ✓ {code}")
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
    if counts["fail"] and counts["ok"] == 0 and counts["reuse"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
