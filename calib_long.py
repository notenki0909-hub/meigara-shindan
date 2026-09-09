# -*- coding: utf-8 -*-
"""
『10年保有できる優良企業』の校正バッチ。

  python calib_long.py jp        # site/long_summaries/*.json から
  python calib_long.py us        # site/us/long_summaries/*.json から

やること:
1. 業種ごとの EV/EBIT 中央値を算出して sector_averages_long{,_us}.json に書く
   （analyze_long.py の買い時スコア「EV/EBIT 対業種」で使う。無いと中立扱いになる）
2. 品質スコア・買い時スコアの分布と、業種グループごとの中央値を表示
   -> sector_groups_long{,_us}.json の grade_a/grade_b、buytiming_long.json の
     tim_tiers_{jp,us} を実分布に合わせて手で更新する材料にする
"""
import argparse
import glob
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = {
    "jp": {"sumdir": "site/long_summaries", "seckey": "jp_sector",
           "out": "sector_averages_long.json", "groups": "sector_groups_long.json"},
    "us": {"sumdir": "site/us/long_summaries", "seckey": "gics_sector",
           "out": "sector_averages_long_us.json", "groups": "sector_groups_long_us.json"},
}


def _num(x):
    return isinstance(x, (int, float))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["jp", "us"])
    args = ap.parse_args()
    c = CFG[args.market]

    rows = []
    for p in glob.glob(os.path.join(HERE, c["sumdir"], "*.json")):
        try:
            rows.append(json.load(open(p, encoding="utf-8")))
        except Exception:
            pass
    if not rows:
        raise SystemExit(f"{c['sumdir']} にサマリがありません。先に batch_long.py を回してください。")
    print(f"[{args.market}] サマリ {len(rows)} 件")

    # 1) 業種ごとの EV/EBIT 中央値
    by_sec = {}
    for r in rows:
        s = r.get(c["seckey"]) or r.get("jp_sector") or r.get("gics_sector")
        v = r.get("ev_ebit")
        # 極端値は中央値算出から外す（負・過大）
        if s and _num(v) and 0 < v < 80:
            by_sec.setdefault(s, []).append(v)
    ev_med = {s: round(st.median(vs), 2) for s, vs in by_sec.items() if len(vs) >= 3}
    out = {"_meta": {"説明": "業種ごとの EV/EBIT 中央値。calib_long.py が生成。"
                     "analyze_long.py の買い時スコア『EV/EBIT 対業種』の基準。",
                     "算出": f"{c['sumdir']} の {len(rows)} 銘柄、0<EV/EBIT<80 の中央値、n>=3 の業種のみ"},
           **{s: {"ev_ebit": m, "n": len(by_sec[s])} for s, m in sorted(ev_med.items())}}
    op = os.path.join(HERE, c["out"])
    json.dump(out, open(op, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"-> {op}  （{len(ev_med)} 業種）")
    for s, m in sorted(ev_med.items(), key=lambda x: x[1]):
        print(f"    EV/EBIT中央値 {m:6.1f} (n={len(by_sec[s]):3}) {s}")

    # 2) スコア分布
    gcfg = json.load(open(os.path.join(HERE, c["groups"]), encoding="utf-8"))
    gmap = {sec: g for g, secs in gcfg["groups"].items() for sec in secs}

    def dist(key, label):
        vs = sorted(r[key] for r in rows if _num(r.get(key)))
        if not vs:
            print(f"  {label}: データなし")
            return
        n = len(vs)
        pct = lambda k: vs[min(n - 1, int(n * k))]
        print(f"  {label}  n={n}  min {vs[0]:.1f}  p10 {pct(.1):.1f}  p25 {pct(.25):.1f}  "
              f"median {st.median(vs):.1f}  p60 {pct(.6):.1f}  p75 {pct(.75):.1f}  max {vs[-1]:.1f}")
        d = {}
        for r in rows:
            if not _num(r.get(key)):
                continue
            g = gmap.get(r.get(c["seckey"]) or r.get("jp_sector") or r.get("gics_sector"), "その他")
            d.setdefault(g, []).append(r[key])
        for g, xs in sorted(d.items(), key=lambda x: -st.median(x[1])):
            print(f"      グループ中央値 {st.median(xs):6.1f} (n={len(xs):3}) {g}")

    print("\n【品質スコア】（sector_groups_long の grade_a/grade_b の材料）")
    dist("q_score", "品質")
    print("\n【買い時スコア】（buytiming_long の tim_tiers_{jp,us} の材料。上位25%が『買い場』の目安）")
    dist("bt_score", "買い時")


if __name__ == "__main__":
    main()
