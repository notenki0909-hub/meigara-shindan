# -*- coding: utf-8 -*-
"""
米国株ツールの校正レポート。site/us/summaries/*.json（batch_us.py の出力）を読み、
  - SEL / TIM スコアの分布（全体・GICSセクター別のパーセンタイル）
  - SEL_TIERS / TIM_TIERS（analyze_us.py）の目安カット点
  - sector_groups_us.json の grade_a / grade_b の候補（セクター中央値の tercile）
  - 定番の優良増配株・過去の減配銘柄が想定どおりの高さ／低さに来ているかのチェック
を出力する。値を書き換えはしない（人が確認して手で反映する）。

  python batch_calib_us.py
  python batch_calib_us.py --sum site/us/summaries
"""
import argparse
import glob
import json
import os
import statistics as st

HERE = os.path.dirname(__file__)

# 「質の高い連続増配株」＝SELは上位に来てほしい参照銘柄
REF_QUALITY = ["PG", "JNJ", "KO", "PEP", "CL", "MMM", "EMR", "ADP", "MCD", "LOW",
               "TGT", "GD", "ITW", "SYY", "ABT", "MDT", "GPC", "SWK", "NUE", "CINF",
               "PNR", "DOV", "APD", "ECL", "SHW", "CTAS", "ROP", "CB", "AFL", "O"]
# 過去に減配・配当据え置き崩れ等で SEL が低め〜中位に落ちるべき参照銘柄
REF_WEAK = ["MMM", "T", "VFC", "WBA", "LUMN", "IRM", "F", "DOW", "APA", "MOS"]


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    i = max(0, min(len(s) - 1, round((p / 100) * (len(s) - 1))))
    return s[i]


def dist_line(name, xs):
    if not xs:
        return f"{name:26s}  n=0"
    return (f"{name:26s}  n={len(xs):3d}  "
            f"min={min(xs):5.1f}  p10={pct(xs,10):5.1f}  p25={pct(xs,25):5.1f}  "
            f"med={st.median(xs):5.1f}  p75={pct(xs,75):5.1f}  p90={pct(xs,90):5.1f}  max={max(xs):5.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sum", default=os.path.join(HERE, "site", "us", "summaries"))
    args = ap.parse_args()

    rows = []
    for p in sorted(glob.glob(os.path.join(args.sum, "*.json"))):
        try:
            s = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        rows.append(s)
    if not rows:
        print(f"no summaries under {args.sum} — run batch_us.py first")
        return

    sel = [r["sel_score"] for r in rows if isinstance(r.get("sel_score"), (int, float))]
    tim = [r["tim_score"] for r in rows if isinstance(r.get("tim_score"), (int, float))]
    by_sec = {}
    for r in rows:
        by_sec.setdefault(r.get("gics_sector") or "?", []).append(r)

    print(f"=== US calibration report ===  summaries: {len(rows)}\n")

    print("-- SEL distribution --")
    print(dist_line("ALL", sel))
    for sname in sorted(by_sec):
        xs = [r["sel_score"] for r in by_sec[sname] if isinstance(r.get("sel_score"), (int, float))]
        print(dist_line(sname, xs))

    print("\n-- TIM distribution --")
    print(dist_line("ALL", tim))
    for sname in sorted(by_sec):
        xs = [r["tim_score"] for r in by_sec[sname] if isinstance(r.get("tim_score"), (int, float))]
        print(dist_line(sname, xs))

    # tier カット点の目安：上位25% / 上位25+45%=70% の位置のスコア
    import analyze_us
    print("\n-- SEL_TIERS candidate (analyze_us.py) --")
    print(f"  current: {analyze_us.SEL_TIERS}")
    print(f"  分布ベース目安: hi≈p75={pct(sel,75):.0f}  mid≈p30={pct(sel,30):.0f}  lo≈p10={pct(sel,10):.0f}")
    print("-- TIM_TIERS candidate --")
    print(f"  current: {analyze_us.TIM_TIERS}")
    print(f"  分布ベース目安: hi≈p75={pct(tim,75):.0f}  mid≈p40={pct(tim,40):.0f}  lo≈p15={pct(tim,15):.0f}")

    # grade_a / grade_b：セクター中央値の tercile
    med_by_sec = []
    for sname, rr in by_sec.items():
        xs = [r["sel_score"] for r in rr if isinstance(r.get("sel_score"), (int, float))]
        if xs:
            med_by_sec.append((sname, round(st.median(xs), 1), len(xs)))
    med_by_sec.sort(key=lambda t: -t[1])
    print("\n-- sector SEL medians (for grade_a/grade_b in sector_groups_us.json) --")
    for sname, m, n in med_by_sec:
        print(f"  {sname:26s}  median={m:5.1f}  (n={n})")
    ms = sorted(m for _, m, _ in med_by_sec)
    if len(ms) >= 3:
        ga = ms[max(0, len(ms) - round(len(ms) / 3) - 1)]
        gb = ms[max(0, round(len(ms) / 3))]
        try:
            gj = json.load(open(os.path.join(HERE, "sector_groups_us.json"), encoding="utf-8"))
            print(f"  current: grade_a={gj.get('grade_a')}  grade_b={gj.get('grade_b')}")
        except Exception:
            pass
        print(f"  tercile目安: grade_a≈{ga:.0f}  grade_b≈{gb:.0f}")

    # 参照銘柄チェック
    idx = {r["code"]: r for r in rows}
    print("\n-- reference: quality compounders (want SEL high, ideally top ~third of ALL) --")
    sel_p66 = pct(sel, 66)
    for t in REF_QUALITY:
        r = idx.get(t)
        if not r:
            continue
        v = r.get("sel_score")
        flag = "" if isinstance(v, (int, float)) and v >= sel_p66 else "  <-- LOW?"
        print(f"  {t:5s} SEL={v!s:6s} TIM={r.get('tim_score')!s:6s} simple={r.get('is_simple')!s:5s}{flag}")
    print(f"  (ALL p66 SEL = {sel_p66:.1f})")

    print("\n-- reference: past cutters / stressed (want SEL not in top third) --")
    for t in REF_WEAK:
        r = idx.get(t)
        if not r:
            continue
        v = r.get("sel_score")
        flag = "  <-- HIGH?" if isinstance(v, (int, float)) and v >= sel_p66 else ""
        print(f"  {t:5s} SEL={v!s:6s} TIM={r.get('tim_score')!s:6s} simple={r.get('is_simple')!s:5s}{flag}")

    # simple / coverage の分布
    n_simple = sum(1 for r in rows if r.get("is_simple"))
    n_lowcov = sum(1 for r in rows if (r.get("cov_sel") or [None, None, ""])[2] == "low")
    print(f"\n-- misc --  simple={n_simple}/{len(rows)}  low-coverage(SEL)={n_lowcov}/{len(rows)}")


if __name__ == "__main__":
    main()
