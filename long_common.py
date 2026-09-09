# -*- coding: utf-8 -*-
"""
『10年保有できる優良企業』ツールの、国に依存しない計算部品。
analyze_long.py（日本株）・analyze_long_us.py（米国株）が共有する。

- 品質スコア = 業績0.28 / 財務0.27 / キャッシュフロー0.15 を取得できたグループだけで
  再正規化（0〜110）。「配当の持続力」は使わない。
- 買い時スコア = EV/EBIT対業種・FCF利回り・PER割安度・PBR割安度 を均等（各25%）で合成。
  欠損した合成指標は中立（buytiming_long.json の missing_fill）で埋める。
- 既存エンジン analyze.py は無改変。score_metric（直線補間）だけ import で流用する。

配当株ツール（analyze.py / analyze_us.py / rank.py / rank_us.py）には一切影響しない。
"""
import json
import math
import os

import analyze  # score_metric の直線補間だけ流用（無改変）

HERE = os.path.dirname(os.path.abspath(__file__))

# sel_score の score_groups.選定 から「配当の持続力」を除いたもの
QUALITY_W = {"業績": 0.28, "財務": 0.27, "キャッシュフロー": 0.15}

_BT_CACHE = None


def load_bt_cfg():
    global _BT_CACHE
    if _BT_CACHE is None:
        with open(os.path.join(HERE, "buytiming_long.json"), encoding="utf-8") as f:
            _BT_CACHE = json.load(f)
    return _BT_CACHE


def is_num(x):
    return isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x))


# ---------------------------------------------------------------- 品質スコア
def quality_score(group_scores):
    """group_scores: {"業績": float|None, "財務": ..., "キャッシュフロー": ...}"""
    num = den = 0.0
    for g, w in QUALITY_W.items():
        v = group_scores.get(g)
        if is_num(v):
            num += w * v
            den += w
    return round(num / den, 1) if den > 0 else None


def quality_coverage(group_scores):
    got = sum(1 for g in QUALITY_W if is_num(group_scores.get(g)))
    return got, len(QUALITY_W), ("高" if got >= 3 else "中" if got == 2 else "低")


# ---------------------------------------------------------------- 買い時の素材
def ev_over_ebit(mcap, total_debt, cash, ebit):
    """EV/EBIT。EV = 時価総額 + 有利子負債 − 現金。EBITが0以下なら None（割安と誤解しない）。"""
    if not (is_num(mcap) and mcap > 0 and is_num(ebit)):
        return None
    if ebit <= 0:
        return None
    ev = mcap + (total_debt or 0) - (cash or 0)
    if ev <= 0:
        return None
    return ev / ebit


def fcf_yield_pct(fcf, mcap):
    """FCF利回り(%) = FCF ÷ 時価総額 × 100。負のFCFはそのまま返す（呼び出し側で中立化）。"""
    if not (is_num(fcf) and is_num(mcap) and mcap > 0):
        return None
    return fcf / mcap * 100.0


def price_band_pos(hist_pairs, current, low_is_cheap=True):
    """[(year, ratio)] の過去レンジ内で current がどこか。返り値 0..1（1=割安側）。
    PER/PBR は low_is_cheap=True（下端＝割安＝1）。"""
    vals = [v for _, v in hist_pairs if is_num(v)]
    if len(vals) < 3 or not is_num(current):
        return None
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return None
    pos = (current - lo) / (hi - lo)          # 0=下端, 1=上端
    pos = max(0.0, min(1.0, pos))
    return (1.0 - pos) if low_is_cheap else pos


def implied_fcf_growth(mcap, fcf, disc=0.10, term_g=0.025, years=10):
    """簡易リバースDCF：現在の時価総額を正当化するのに必要な、今後<years>年の FCF 年成長率。
    その後は term_g で永久成長。判定には使わず参考表示のみ（割引率・ターミナルに敏感）。
    二分法で解く。解けなければ None。"""
    if not (is_num(mcap) and mcap > 0 and is_num(fcf) and fcf > 0):
        return None
    if disc <= term_g:
        return None

    def pv(gr):
        total, cf = 0.0, fcf
        for t in range(1, years + 1):
            cf *= (1.0 + gr)
            total += cf / (1.0 + disc) ** t
        terminal = cf * (1.0 + term_g) / (disc - term_g)
        total += terminal / (1.0 + disc) ** years
        return total

    lo, hi = -0.50, 1.00
    if pv(lo) > mcap:          # マイナス成長でも高すぎる＝解なし（下限で返す）
        return lo
    if pv(hi) < mcap:          # 100%成長でも足りない＝解なし（上限で返す）
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2
        if pv(mid) < mcap:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 4)


# ---------------------------------------------------------------- スコア合成
def score_val(key, v, rules):
    """buytiming_long.json の rule で v を 20〜110 点に。analyze.score_metric を流用。"""
    rule = rules.get(key)
    if rule is None or not is_num(v):
        return None
    return analyze.score_metric(key, v, rule)


def composite(sub_keys, raw_vals, rules):
    """PER割安度 / PBR割安度：サブ指標の点の平均。取れたものだけで平均、全部欠損なら None。
    返り値: (score|None, [(key, raw, score), ...])"""
    parts = []
    pts = []
    for k in sub_keys:
        s = score_val(k, raw_vals.get(k), rules)
        parts.append((k, raw_vals.get(k), s))
        if is_num(s):
            pts.append(s)
    return (round(sum(pts) / len(pts), 1) if pts else None), parts


def buytiming_score(comp_scores):
    """comp_scores: {"ev_ebit_vs_sector": s|None, "fcf_yield": s|None,
    "per_cheap": s|None, "pbr_cheap": s|None}。均等ウェイト、欠損は missing_fill で中立化。
    返り値: (score 0..110, scored_count, possible_count, cov_label)"""
    cfg = load_bt_cfg()
    w = cfg["weights"]
    fill = cfg.get("missing_fill", 60)
    num = den = 0.0
    scored = 0
    for k, wt in w.items():
        s = comp_scores.get(k)
        if is_num(s):
            num += wt * s
            scored += 1
        else:
            num += wt * fill          # 中立で埋める（O'Shaughnessy流：欠損=中立）
        den += wt
    total = round(num / den, 1) if den > 0 else None
    n = len(w)
    lab = "高" if scored >= n - 1 else "中" if scored >= n - 2 else "低"
    return total, scored, n, lab


# ---------------------------------------------------------------- ラベル
def tier_label(score, tiers):
    """score → 'hi' / 'mid' / 'lo' / None（tiers = (hi, mid, lo) の下限）"""
    if not is_num(score):
        return None
    hi, mid, lo = tiers
    if score >= hi:
        return "hi"
    if score >= mid:
        return "mid"
    if score >= lo:
        return "lo"
    return "xlo"


BUY_TIER_JA = {"hi": "買い場（割安圏）", "mid": "ほぼ妥当", "lo": "やや割高", "xlo": "割高で見送り"}
QUAL_TIER_JA = {"hi": "長期保有の質あり", "mid": "及第点", "lo": "質に不安", "xlo": "基準を満たさない"}
QUAL_TIERS = (85, 68, 55)
