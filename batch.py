# -*- coding: utf-8 -*-
"""
ユニバースの全銘柄を generate() で回してレポート＋サマリを書き出すバッチ。

  python batch.py --codes universe.json            # universe.json の codes を全部
  python batch.py --codes 9433,8058,2914           # カンマ区切りで直接
  python batch.py --codes universe.json --only-stale --hours 20
  python batch.py --codes universe.json --limit 30 --sleep 1.5

出力:
  site/reports/<code>.html   … 個別レポート
  site/reports/<code>.md
  site/summaries/<code>.json … ランキング集計用サマリ（generate() の summary + 生成時刻）
  site/batch_log.json        … 直近の実行結果（ok/skip/fail 件数、失敗コード）

レート制限対策：--sleep 秒（既定1.5）を銘柄間に必ず入れる。--jgb で10年国債を
1回だけ渡す（各銘柄で取りに行かせない）。失敗は記録して次へ進む（止めない）。
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import sys
import time
import traceback

import analyze

SITE = os.path.join(os.path.dirname(__file__), "site")
REP = os.path.join(SITE, "reports")
SUM = os.path.join(SITE, "summaries")


def load_codes(spec):
    """'9433,8058' でも 'universe.json' でも受ける。→ [(code, name_or_None), ...]"""
    if os.path.isfile(spec):
        j = json.load(open(spec, encoding="utf-8"))
        out = []
        for item in j.get("codes", j if isinstance(j, list) else []):
            if isinstance(item, dict):
                out.append((str(item["code"]), item.get("name")))
            else:
                out.append((str(item), None))
        return out
    return [(c.strip(), None) for c in spec.split(",") if c.strip()]


def is_stale(code, hours):
    p = os.path.join(SUM, f"{code}.json")
    if not os.path.isfile(p):
        return True
    try:
        j = json.load(open(p, encoding="utf-8"))
        gen = dt.datetime.fromisoformat(j["_generated_at"])
    except Exception:
        return True
    return (dt.datetime.now() - gen).total_seconds() > hours * 3600


def run_one(code, name, jgb, cfg):
    r = analyze.generate(code, name=name, jgb=jgb, cfg=cfg)
    if not r["ok"]:
        return code, "fail", r["error"]
    open(os.path.join(REP, f"{code}.html"), "w", encoding="utf-8").write(r["html"])
    open(os.path.join(REP, f"{code}.md"), "w", encoding="utf-8").write(r["md"])
    s = dict(r["summary"])
    s["_generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    json.dump(s, open(os.path.join(SUM, f"{code}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return code, "ok", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True, help="universe.json か 9433,8058,... ")
    ap.add_argument("--sleep", type=float, default=1.5, help="銘柄間スリープ秒（レート制限対策）")
    ap.add_argument("--workers", type=int, default=1, help="並列数（3以下推奨。バーストするとブロックされる）")
    ap.add_argument("--limit", type=int, default=0, help="先頭N件だけ（0=全部）")
    ap.add_argument("--only-stale", action="store_true", help="--hours より古いサマリだけ再生成")
    ap.add_argument("--hours", type=float, default=20)
    ap.add_argument("--jgb", type=float, default=None, help="10年国債利回り(%)。省略時は同梱値")
    args = ap.parse_args()

    os.makedirs(REP, exist_ok=True)
    os.makedirs(SUM, exist_ok=True)
    cfg = analyze.load_config()
    jgb = args.jgb if args.jgb is not None else cfg["rules"].get("market", {}).get("jgb_10y")

    codes = load_codes(args.codes)
    if args.only_stale:
        codes = [(c, n) for c, n in codes if is_stale(c, args.hours)]
    if args.limit:
        codes = codes[: args.limit]

    print(f"対象 {len(codes)} 銘柄  workers={args.workers}  sleep={args.sleep}s  jgb={jgb}")
    t0 = time.time()
    counts = {"ok": 0, "skip": 0, "fail": 0}
    failed = []

    def emit(code, status, err):
        counts[status] = counts.get(status, 0) + 1
        mark = {"ok": "✓", "fail": "✗", "skip": "-"}.get(status, "?")
        line = f"  {mark} {code}"
        if err:
            line += f"  {err}"
            failed.append({"code": code, "error": err})
        print(line)

    if args.workers <= 1:
        for i, (code, name) in enumerate(codes, 1):
            try:
                _, st, err = run_one(code, name, jgb, cfg)
            except Exception as e:
                st, err = "fail", f"{e.__class__.__name__}: {e}"
                traceback.print_exc()
            emit(code, st, err)
            if i < len(codes):
                time.sleep(args.sleep)
    else:
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {}
            for code, name in codes:
                futs[ex.submit(run_one, code, name, jgb, cfg)] = code
                time.sleep(args.sleep)
            for fut in cf.as_completed(futs):
                code = futs[fut]
                try:
                    _, st, err = fut.result()
                except Exception as e:
                    st, err = "fail", f"{e.__class__.__name__}: {e}"
                emit(code, st, err)

    dur = time.time() - t0
    log = {"ran_at": dt.datetime.now().isoformat(timespec="seconds"),
           "seconds": round(dur, 1), "counts": counts, "failed": failed}
    json.dump(log, open(os.path.join(SITE, "batch_log.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n完了 {dur:.0f}s  ok={counts['ok']} fail={counts['fail']}  "
          f"→ site/summaries/  （失敗は site/batch_log.json）")
    if counts["fail"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
