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
           "out": "sector_averages_long.json", "groups": "sector_groups_long.json",
           "pbr_rel": "pbr_reliability_long.json"},
    "us": {"sumdir": "site/us/long_summaries", "seckey": "gics_sector",
           "out": "sector_averages_long_us.json", "groups": "sector_groups_long_us.json",
           "pbr_rel": "pbr_reliability_long_us.json"},
}


def _num(x):
    return isinstance(x, (int, float))


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def _rank(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs, ys):
    if len(xs) < 3:
        return None
    return _pearson(_rank(xs), _rank(ys))


BT_KEYS = ["ev_ebit_vs_sector", "fcf_yield", "per_cheap", "pbr_cheap"]
BT_JA = {"ev_ebit_vs_sector": "EV/EBIT", "fcf_yield": "FCF利回り",
         "per_cheap": "PER割安度", "pbr_cheap": "PBR割安度"}


def pbr_noise_check(rows, seckey=None):
    """買い時4指標のうち PBR割安度 が他指標と重複しているだけ(ノイズ)か、
    独立した情報を持っているかの簡易チェック。将来リターンとの相関(真の予測力
    検証)ではなく、あくまで銘柄横断の内部整合性チェック（バックテスト機能は
    このツールに無いため代替手段）。"""
    comps = [r["bt_components"] for r in rows if isinstance(r.get("bt_components"), dict)]
    if len(comps) < 10:
        print("\n【PBRノイズ検証】bt_components が少なすぎるためスキップ")
        return
    print(f"\n【PBRノイズ検証】（bt_components {len(comps)}件。将来リターン予測力の検証ではなく銘柄横断の内部整合性チェック）")

    print("  1) 4指標の相関行列（Pearson・両方値がある銘柄のみで算出）")
    header = "            " + "".join(f"{BT_JA[k]:>10}" for k in BT_KEYS)
    print(header)
    for k1 in BT_KEYS:
        line = f"  {BT_JA[k1]:>10}"
        for k2 in BT_KEYS:
            pairs = [(c[k1], c[k2]) for c in comps if _num(c.get(k1)) and _num(c.get(k2))]
            if len(pairs) < 10:
                line += f"{'n/a':>10}"
                continue
            xs, ys = zip(*pairs)
            r = _pearson(list(xs), list(ys))
            line += f"{r:10.2f}" if r is not None else f"{'n/a':>10}"
        print(line)
    print("     → PBR割安度の行がPER割安度と強く相関(目安r>0.6)していれば重複情報、"
          "他の3指標のどれとも弱ければ独立情報だがノイズの可能性もある（②③で判断）")

    print("  2) 買い時スコア(4指標・運用中の missing_fill込み) と PBR抜き3指標版 の順位相関")
    full_scores, ex_pbr_scores = [], []
    for r in rows:
        c = r.get("bt_components")
        full = r.get("bt_score")
        if not isinstance(c, dict) or not _num(full):
            continue
        fill = 60  # buytiming_long.json の missing_fill と揃える
        ex_vals = [c.get(k) if _num(c.get(k)) else fill
                   for k in ("ev_ebit_vs_sector", "fcf_yield", "per_cheap")]
        full_scores.append(full)
        ex_pbr_scores.append(sum(ex_vals) / len(ex_vals))
    rho = _spearman(full_scores, ex_pbr_scores)
    if rho is not None:
        print(f"     n={len(full_scores)}  Spearman順位相関 = {rho:.3f}")
        print("     → 1.0に近いほど『PBRを入れても入れなくても銘柄の並びはほぼ変わらない＝寄与が小さい』")
    else:
        print("     データ不足")

    print("  3) PBR割安度スコアの分布と極端値（張り付き）の割合")
    pbr_vals = sorted(c["pbr_cheap"] for c in comps if _num(c.get("pbr_cheap")))
    if pbr_vals:
        n = len(pbr_vals)
        pct = lambda k: pbr_vals[min(n - 1, int(n * k))]
        extreme = sum(1 for v in pbr_vals if v <= 25 or v >= 105)
        print(f"     n={n}  min {pbr_vals[0]:.1f}  p25 {pct(.25):.1f}  median {st.median(pbr_vals):.1f}  "
              f"p75 {pct(.75):.1f}  max {pbr_vals[-1]:.1f}")
        print(f"     極端値（<=25点 or >=105点）の割合: {extreme}/{n} = {extreme / n * 100:.1f}%"
              "  ※高いほど『いつも割高/割安』に張り付く業種構造がある可能性")
    else:
        print("     データなし")

    by_sec = {}
    if seckey:
        print("  4) 極端値（張り付き）の業種別内訳（n>=3 のみ、張り付き率が高い順）")
        for r in rows:
            comp = r.get("bt_components")
            v = comp.get("pbr_cheap") if isinstance(comp, dict) else None
            if not _num(v):
                continue
            sec = r.get(seckey) or r.get("jp_sector") or r.get("gics_sector") or "?"
            d = by_sec.setdefault(sec, {"n": 0, "lo": 0, "hi": 0, "examples": []})
            d["n"] += 1
            if v <= 25:
                d["lo"] += 1
                d["examples"].append((r.get("code"), v))
            elif v >= 105:
                d["hi"] += 1
                d["examples"].append((r.get("code"), v))
        ranked = sorted(by_sec.items(),
                         key=lambda kv: -(kv[1]["lo"] + kv[1]["hi"]) / kv[1]["n"])
        for sec, d in ranked:
            if d["n"] < 3:
                continue
            extreme = d["lo"] + d["hi"]
            if extreme == 0:
                continue
            rate = extreme / d["n"] * 100
            ex_codes = ", ".join(f"{code}({v:.0f})" for code, v in d["examples"][:4])
            print(f"     {rate:5.1f}%  (n={d['n']:3}, 下限{d['lo']:2}/上限{d['hi']:2})  {sec:<28} 例: {ex_codes}")
    return by_sec


PBR_RELIABILITY_MIN_N = 10       # これ未満の業種はサンプル不足として『信頼できる』扱いのまま据え置く
PBR_RELIABILITY_THRESHOLD = 30.0  # 張り付き率(%)がこれ以上の業種は買い時スコアからPBRを除外


def write_pbr_reliability(by_sec, out_path, min_n=PBR_RELIABILITY_MIN_N,
                           threshold=PBR_RELIABILITY_THRESHOLD):
    """業種ごとのPBR張り付き率から『PBR不信頼』業種を判定し、
    analyze_long.py が読む pbr_reliability_long{,_us}.json を書く。
    不信頼業種＝買い時スコアはPBRを除いた3指標(EV/EBIT・FCF利回り・PER割安度)均等33%で合成。
    自社株買いで自己資本が構造的に振れる業種ほど張り付き率が高くなる傾向を検証済み
    （calib_long.py の『PBRノイズ検証』参照）。"""
    detail = {}
    unreliable = []
    for sec, d in by_sec.items():
        n = d["n"]
        if n < min_n:
            continue
        rate = (d["lo"] + d["hi"]) / n * 100
        detail[sec] = {"n": n, "extreme_rate_pct": round(rate, 1)}
        if rate >= threshold:
            unreliable.append(sec)
    out = {
        "_meta": {
            "説明": "業種ごとにPBR割安度スコアが張り付いた(<=25点 or >=105点)割合を測り、"
                    f"n>={min_n} かつ張り付き率>={threshold}% の業種を『PBR不信頼』として"
                    "buytiming_score から pbr_cheap を除外(3指標均等33%)する。"
                    "自社株買いによる自己資本の構造的な変動でPBRの過去レンジ自体が非定常になり、"
                    "『過去の下限に近い＝底値』という前提が崩れる業種を機械的に検出する目的。"
                    "calib_long.py 実行のたびに再算出される（手で編集しない）。",
            "min_n": min_n, "threshold_pct": threshold,
        },
        "unreliable_sectors": sorted(unreliable),
        "detail": dict(sorted(detail.items(), key=lambda kv: -kv[1]["extreme_rate_pct"])),
    }
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n-> {out_path}  PBR不信頼業種 {len(unreliable)}/{len(detail)}: {', '.join(sorted(unreliable)) or '(なし)'}")


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

    by_sec = pbr_noise_check(rows, seckey=c["seckey"])
    write_pbr_reliability(by_sec, os.path.join(HERE, c["pbr_rel"]))


if __name__ == "__main__":
    main()
