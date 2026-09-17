# -*- coding: utf-8 -*-
"""
『10年保有できる優良企業』ランキング（配当を評価しない品質スコア版）を生成する。
配当株ランキング（rank.py / rank_us.py）とは独立。既存ファイルは書き換えない。

  python rank_long.py jp     -> site/long/index.html      + site/long/ranking.json
  python rank_long.py us     -> site/us/long/index.html   + site/us/long/ranking.json

品質スコア = 既存サマリの groups から、
  業績0.28 ・ 財務0.27 ・ キャッシュフロー0.15
を取得できたグループだけで加重平均して再正規化（0〜110）。
「配当の持続力」グループは使わない。配当利回りは参考列として表示のみ。

金融・保険・証券・REIT は analyze が業績/財務/CFを採点しない（配当の持続力のみで
sel_score を出す）ため品質スコアが None になる。これらは軍分けせず「対象外」節に出す。

サマリは site/summaries/（既存 nightly が更新）と site/long_summaries/（batch_long.py が
補完）の両方を見て、新しい方を採用する。
"""
import argparse
import datetime as dt
import glob
import html
import json
import os
import shutil
import statistics as st

import rank as _jp          # THEME_*, TIER_CLASS, DIR_CLASS を流用（配色をサイトと統一）
import rank_us as _us
import long_common as LC

HERE = os.path.dirname(os.path.abspath(__file__))

# sel_score の score_groups.選定 から「配当の持続力」を除いたもの
QUALITY_W = {"業績": 0.28, "財務": 0.27, "キャッシュフロー": 0.15}

MK = {
    "jp": {
        "theme": _jp,
        "universe": "universe_long.json",
        "sumdir": os.path.join(HERE, "site", "long_summaries"),
        "groups_cfg": "sector_groups_long.json",
        "seckey": "jp_sector",
        "out_dir": os.path.join(HERE, "site", "long"),
        "report_dirs": [("reports/", os.path.join(HERE, "site", "long", "reports"))],
        "title": "10年保有できる優良企業ランキング（日本株）",
        "screen_line": "母集団＝時価総額3,000億円以上（TOPIX500相当）。銀行・保険・証券は"
                       "専用の品質スコア（ROE・増収率・EPS成長率・利益の安定度）で評価。"
                       "品質スコア（配当は不使用）＋買い時スコア（EV/EBIT・FCF利回り・PER/PBR割安度）。",
        "terms_src": "long_terms.html",
        "guide_src": "long_guide.html",
        "portfolio_src": "long_portfolio.html",
        "unit_price": "円",
        "watch": None,
    },
    "us": {
        "theme": _us,
        "universe": "universe_long_us.json",
        "sumdir": os.path.join(HERE, "site", "us", "long_summaries"),
        "groups_cfg": "sector_groups_long_us.json",
        "seckey": "gics_sector",
        "out_dir": os.path.join(HERE, "site", "us", "long"),
        "report_dirs": [("reports/", os.path.join(HERE, "site", "us", "long", "reports"))],
        "title": "10年保有できる優良企業ランキング（米国株）",
        "screen_line": "母集団＝S&P500 メンバーシップ（黒字継続・流動性・業種代表性を"
                       "委員会が審査済み）。金融は専用の品質スコア（ROE・増収率・EPS成長率・"
                       "利益の安定度）、REITは専用のFFOベース品質スコアで評価。"
                       "品質スコア（配当は不使用）＋買い時スコア。",
        "terms_src": "long_us_terms.html",
        "guide_src": "long_us_guide.html",
        "portfolio_src": "long_us_portfolio.html",
        "unit_price": "$",
        "watch": "universe_long_watch_us.json",
    },
}

NAV_ITEMS = [
    ("利用規約・免責事項", "terms.html"),
    ("使い方・見方", "guide.html"),
    ("ランキング", "index.html"),
    ("ウォッチリスト", "watchlist.html"),
    ("ポートフォリオ", "portfolio.html"),
]


def _pagenav(current, exclude=()):
    """右上のページ間リンク。当該ページ自身と exclude で指定したページは含めない。"""
    excl = set(exclude) | {current}
    links = "".join(f'<a href="{href}">{label}</a>'
                     for label, href in NAV_ITEMS if href not in excl)
    return f'<span class="pagenav">{links}</span>'


TERMS = {
    "grade": ("業種級（A/B/C）",
              "業種グループ内の<b>品質スコア中央値</b>で A＞B＞C。Aが最も質の高い企業が揃う"
              "業種という序列（業種選びの目安）。個別銘柄の良し悪しは「品質」と「軍」で見る。"),
    "tier": ("軍（1〜3軍）",
             "業種グループ内で品質スコアの高い順に、上位25%を1軍・続く45%を2軍・残りを3軍。"
             "カバレッジ「低」は最高2軍まで。品質スコアが十分高い銘柄は、強い業種にいるだけで"
             "3軍に落ちないよう最低2軍を保証（1軍はグループ内上位の意味を残す）。6銘柄未満の"
             "グループは軍分けせず「―」。矢印は前回比の方向（↑改善／→横ばい／↓悪化）。"),
    "q": ("品質スコア（0〜110）",
          "配当を評価しない、長期保有できる優良企業かのスコア。<b>業績×0.28＋財務×0.27＋"
          "キャッシュフロー×0.15</b>（取得できたグループで再正規化）。既存の「銘柄選定スコア」"
          "から配当の持続力（連続増配・増配率・配当性向・累進配当宣言など）を除いたもの。"),
    "perf": ("業績", "売上・EPSの伸び（年率）、営業利益率、利益の安定度（営業利益のブレ）の"
             "平均点（0〜110）。品質スコアの28%。"),
    "fin": ("財務", "自己資本比率・D/E・ネットD/E・有利子負債÷営業CF（米国株はICRを含み"
            "自己資本比率は参考）の平均点。品質スコアの27%。"),
    "cf": ("CF（キャッシュフロー）", "営業CFの継続黒字・フリーCFの継続黒字・FCF配当性向の"
           "平均点。品質スコアの15%。"),
    "bt": ("買い時スコア（0〜110）",
           "今の株価が割高すぎないか。配当利回りは使わず、<b>EV/EBIT対業種・FCF利回り・"
           "PER割安度・PBR割安度を均等25%</b>で合成（欠損は中立60）。色＝買い場（割安圏）／"
           "ほぼ妥当／やや割高／割高で見送りの4段階。品質とは別物で、質の評価には混ぜない。"),
    "yield": ("利回り", "予想年間配当 ÷ 現在株価（予想配当利回り）。<b>採点には使わず参考表示のみ。</b>"),
    "cov": ("カバレッジ", "品質スコアの算出に使えたグループ数（業績・財務・CFの3つ中）。"
            "高＝3/3・中＝2/3・低＝1以下。低い銘柄は財務やCFの履歴が短く点がぶれやすい。"),
    "price": ("終値", "前営業日の終値。夜間更新のため当日ザラ場とはずれる。"),
}


DISC = ('本ページは、あらかじめ定めた基準（時価総額または指数構成）で抽出した銘柄について、'
        '公開データを機械的なルールで算出した「配当を含めない品質スコア」（業績・財務・'
        'キャッシュフロー）による分類です。配当利回りは参考表示で、採点には使っていません。'
        '銀行・保険・証券は専用の品質スコア（ROE・増収率・EPS成長率・利益の安定度）、'
        '米国REITは専用のFFOベース品質スコアで評価しています。日本のJ-REITはこのツールの'
        '母集団に含まれておらず対象外です。'
        '特定銘柄の売買を推奨・勧誘するものではなく、運営者は投資助言・代理業の登録を受けて'
        'いません。教育目的の一般情報であり、投資判断はご自身の責任で行ってください。数値は'
        'yfinance 由来で誤り・遅延・欠損があり得ます。')


# ---------------------------------------------------------------- スコア
def quality_score(groups):
    num = den = 0.0
    for g, w in QUALITY_W.items():
        v = groups.get(g)
        if isinstance(v, (int, float)):
            num += w * v
            den += w
    return round(num / den, 1) if den > 0 else None


def quality_cov(groups):
    got = sum(1 for g in QUALITY_W if isinstance(groups.get(g), (int, float)))
    lab = "高" if got >= 3 else "中" if got == 2 else "低"
    return got, len(QUALITY_W), lab


def _num(v, d=1):
    return f"{v:.{d}f}" if isinstance(v, (int, float)) else "―"


def _price(v, unit):
    if not isinstance(v, (int, float)):
        return "―"
    return (f"${v:,.2f}" if unit == "$" else f"{v:,.0f}")


def _v(x):
    return x if isinstance(x, (int, float)) else -1e9


def _fv(x):
    """「詳しい条件で絞り込む」フィルタ用data-*属性の値。欠損は空文字（JS側でNaN扱い＝
    常に対象外。_v()は列ソート用の別物で-1e9を返すため、ここでは流用しない）。"""
    return x if isinstance(x, (int, float)) else ""


# フィルタパネルで個別指標として出す業績/財務/CFの生キー（analyze_long.py の
# _FILTER_METRIC_KEYS と対応。項目名は個別レポートページのM["業績"]等の"name"と一致させる）。
# interest_coverageは米国株のみ・equity_ratioは日本株のみ値が入る（もう片方の市場では
# _RAW_RULES_BY_MARKETに閾値が無いため、フィルタパネルにも表示しない）。
FILTER_METRICS = (
    ("業績", (("rev_cagr", "売上高（推移／年率）"), ("eps_cagr", "EPS（推移／年率）"),
              ("op_margin", "営業利益率（直近）"), ("earnings_stability", "利益の安定度（営業利益のブレ）"))),
    ("財務", (("equity_ratio", "自己資本比率"), ("de", "D/Eレシオ（有利子負債÷自己資本）"),
              ("net_de", "ネットD/Eレシオ"), ("debt_to_ocf", "有利子負債 ÷ 営業CF（返済年数の目安）"),
              ("interest_coverage", "インタレストカバレッジレシオ（EBIT÷支払利息）"))),
    ("キャッシュフロー", (("ocf_positive", "営業CF（直近／推移）"), ("fcf_positive", "フリーCF（営業CF＋投資CF）"),
                    ("fcf_payout", "FCF配当性向（配当支払÷フリーCF）"))),
)
# 買い時内訳（PER割安度・PBR割安度は「自社過去レンジ内の位置」＋「対業種平均」の2つの生数値
# の平均で合成されるスコアのため、個別ページと同じくその2つに分解して実数値で絞り込む。
# FCF利回りは単一の生数値（%）を持つため他の項目と同じ形式だが、買い時セクションの先頭に
# 別途フォームを組む（FCF_YIELD_RULE参照）。key・label・rule・summary側の生値キー・
# 倍率（band_posは0〜1の生値を%表示に揃えるため100倍する）の順。
FILTER_BT_COMPONENTS = (
    ("ev_ebit_vs_sector", "EV/EBIT（対業種中央値）",
     {"dir": "lower_better", "good": 0.85, "warn": 1.2, "unit": "倍", "dec": 2}, "ev_ebit_vs_sector", 1),
    ("per_band_pos", "PER 自社過去レンジ内の位置",
     {"dir": "higher_better", "good": 60, "warn": 20, "unit": "%", "dec": 0}, "per_band_pos", 100),
    ("per_vs_sector", "PER 対業種平均",
     {"dir": "lower_better", "good": 0.95, "warn": 1.2, "unit": "倍", "dec": 2}, "per_vs_sector", 1),
    ("pbr_band_pos", "PBR 自社過去レンジ内の位置",
     {"dir": "higher_better", "good": 60, "warn": 20, "unit": "%", "dec": 0}, "pbr_band_pos", 100),
    ("pbr_vs_sector", "PBR 対業種平均",
     {"dir": "lower_better", "good": 1.0, "warn": 1.4, "unit": "倍", "dec": 2}, "pbr_vs_sector", 1),
)

# 個別ページのrule_block_htmlと同じ判定基準（sector_rules.json/sector_rules_us.jsonの
# "default"）。業種別の上書き閾値はここでは使わない（フィルタパネルは全業種共通の1枚のため、
# 個別ページのように銘柄ごとの業種別しきい値を出し分けられない。絞り込み自体は各銘柄の
# 業種別しきい値で採点済みのスコアではなく生数値を比較するため、業種によっては表示中の
# 閾値と実際の採点基準がわずかにずれる場合がある）。
_RAW_RULES_COMMON = {
    "rev_cagr": {"dir": "higher_better", "good": 3, "warn": -6, "unit": "%", "dec": 0},
    "eps_cagr": {"dir": "higher_better", "good": 5, "warn": -8, "unit": "%", "dec": 0},
    "earnings_stability": {"dir": "higher_better", "good": 0.88, "warn": 0.65, "unit": "", "dec": 2},
    "de": {"dir": "lower_better", "good": 1.0, "warn": 2.0, "unit": "倍", "dec": 2},
    "net_de": {"dir": "lower_better", "good": 0.5, "warn": 1.5, "unit": "倍", "dec": 2},
    "debt_to_ocf": {"dir": "lower_better", "good": 3, "warn": 6, "unit": "年", "dec": 0},
    "fcf_payout": {"dir": "lower_better", "good": 70, "warn": 100, "unit": "%", "dec": 0},
}
_RAW_RULES_BY_MARKET = {
    "jp": dict(_RAW_RULES_COMMON,
               op_margin={"dir": "higher_better", "good": 10, "warn": 4, "unit": "%", "dec": 0},
               equity_ratio={"dir": "higher_better", "good": 40, "warn": 25, "unit": "%", "dec": 0}),
    "us": dict(_RAW_RULES_COMMON,
               op_margin={"dir": "higher_better", "good": 12, "warn": 5, "unit": "%", "dec": 0},
               interest_coverage={"dir": "higher_better", "good": 10, "warn": 3, "unit": "倍", "dec": 2}),
}
_BINARY_METRIC_KEYS = ("ocf_positive", "fcf_positive")
_PAIRED_NUM_METRIC_KEYS = ("rev_cagr", "eps_cagr", "op_margin")  # FCF利回りは買い時側で別途扱う
FCF_YIELD_RULE = {"dir": "higher_better", "good": 6.0, "warn": 3.0, "unit": "%", "dec": 1}


def _raw_zone_num(v, unit, dec):
    if unit == "倍":
        return f"{v:.2f}{unit}"
    if dec == 0 or abs(v - round(v)) < 1e-9:
        return f"{int(round(v))}{unit}"
    return f"{v:.{dec}f}{unit}"


def _raw_zone_labels(rule):
    """個別ページのrule_block_htmlと同じ言い回しで、実数値の参照範囲を3段階分返す。"""
    d, g, w, u, dec = rule["dir"], rule["good"], rule["warn"], rule["unit"], rule["dec"]
    g_s, w_s = _raw_zone_num(g, u, dec), _raw_zone_num(w, u, dec)
    if d == "higher_better":
        return (f"良好（{g_s}以上）", f"注意（{w_s}〜{g_s}）", f"弱い（{w_s}未満）")
    return (f"良好（{g_s}以下）", f"注意（{g_s}〜{w_s}）", f"弱い（{w_s}超）")


def grade_of(median, ga, gb):
    if median is None:
        return "―"
    return "A" if median >= ga else "B" if median >= gb else "C"


def tier_of(idx, n, c1, c2):
    n1 = max(1, round(n * c1))
    n2 = max(1, round(n * c2))
    if idx < n1:
        return "1軍"
    if idx < n1 + n2:
        return "2軍"
    return "3軍"


def direction(now, prev, thr=2.0):
    if now is None or prev is None:
        return "→"
    d = now - prev
    return "↑" if d >= thr else "↓" if d <= -thr else "→"


# ---------------------------------------------------------------- データ収集
def load_summary(code, sumdir):
    p = os.path.join(sumdir, f"{code}.json")
    if not os.path.isfile(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def load_prev(out_dir):
    p = os.path.join(out_dir, "ranking.json")
    if not os.path.isfile(p):
        return {}
    try:
        j = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for g in j.get("groups", []):
        for s in g.get("stocks", []):
            out[s["code"]] = s.get("q")
    for s in j.get("excluded", []):
        out.setdefault(s["code"], None)
    return out


def build(market):
    m = MK[market]
    gcfg = json.load(open(os.path.join(HERE, m["groups_cfg"]), encoding="utf-8"))
    gmap = {}
    for gname, secs in gcfg["groups"].items():
        for s in secs:
            gmap[s] = gname
    ga, gb = gcfg["grade_a"], gcfg["grade_b"]
    c1, c2 = gcfg["tier1_pct"], gcfg["tier2_pct"]
    cap = gcfg.get("cap_low_coverage_at", "2軍")
    min_n = gcfg.get("min_group_for_tiers", 6)
    floor2 = gcfg.get("score_floor_tier2")
    excl_groups = set(gcfg.get("exclude_groups", []))
    prev = load_prev(m["out_dir"])

    univ = json.load(open(os.path.join(HERE, m["universe"]), encoding="utf-8"))
    items = univ.get("tickers") or univ.get("codes") or []

    watch, watch_new = {}, {}
    if m.get("watch"):
        wp = os.path.join(HERE, m["watch"])
        if os.path.isfile(wp):
            try:
                wd = json.load(open(wp, encoding="utf-8"))
                watch = wd.get("excluded", {}) or {}
                watch_new = wd.get("new", {}) or {}
            except Exception:
                watch, watch_new = {}, {}

    rows, excluded, missing = [], [], []
    for it in items:
        code = str(it.get("ticker") or it.get("code"))
        s = load_summary(code, m["sumdir"])
        if s is None:
            missing.append(code)
            continue
        groups = s.get("groups", {}) or {}
        q = s.get("q_score")
        sec = s.get(m["seckey"]) or it.get(m["seckey"]) or ""
        grp = gmap.get(sec, "その他")
        if grp in excl_groups:
            q = None
        if code in watch:
            q = None
        rec = {
            "code": code, "name": s.get("name") or it.get("name") or code,
            "sector": sec, "group": grp,
            "q": q, "perf": groups.get("業績"), "fin": groups.get("財務"),
            "cf": groups.get("キャッシュフロー"),
            "bt": s.get("bt_score"), "bt_cov": s.get("bt_cov"),
            "ev_ebit": s.get("ev_ebit"), "fcf_yield": s.get("fcf_yield"),
            "yield": s.get("div_yield"), "price": s.get("price"),
            "price_date": s.get("price_date"),
            "is_simple": s.get("is_simple"), "is_reit": s.get("is_reit"),
            "asof": s.get("_generated_at"), "new": code in watch_new,
            "new_at": watch_new.get(code, {}).get("detected_at"),
            "mcap": s.get("mcap"),
            "metrics_raw": s.get("metric_raw") or {},
            "bt_raw": {k: (s.get(sk) * scale) if isinstance(s.get(sk), (int, float)) else None
                       for k, _lab, _rule, sk, scale in FILTER_BT_COMPONENTS},
            "has_decel": (s.get("quarter_decel_factor") is not None) if market == "us" else None,
        }
        if q is None:
            if code in watch:
                rec["why"] = watch[code].get("reason", "対象外（月次チェック検知）")
                rec["detected_at"] = watch[code].get("detected_at")
            elif s.get("is_reit") or grp == "Real Estate":
                rec["why"] = "REIT・不動産（採点対象外）"
            elif s.get("is_simple"):
                rec["why"] = "金融（銀行・保険・証券／採点対象外）"
            else:
                rec["why"] = "データ不足（新規上場・決算データ未整備等）"
            excluded.append(rec)
        else:
            rec["cov"] = s.get("q_cov") or "―"
            rec["dir"] = direction(q, prev.get(code))
            rows.append(rec)

    # 業種グループごとに軍分け
    groups_out = []
    for gname in list(gcfg["groups"].keys()):
        gr = [r for r in rows if r["group"] == gname]
        if not gr:
            continue
        gr.sort(key=lambda r: -r["q"])
        n = len(gr)
        tiered = n >= min_n
        meds = [r["q"] for r in gr]
        median = round(st.median(meds), 1) if meds else None
        for i, r in enumerate(gr):
            t = tier_of(i, n, c1, c2) if tiered else "―"
            if floor2 and isinstance(r["q"], (int, float)) and r["q"] >= floor2 and t == "3軍":
                t = "2軍"
            if t == "1軍" and r["cov"] == "低":
                t = cap
            r["tier"] = t
        groups_out.append({"name": gname, "grade": grade_of(median, ga, gb),
                           "median": median, "count": n, "tiered": tiered, "stocks": gr})

    global_top = sorted(rows, key=lambda r: -r["q"])[:50]
    excluded.sort(key=lambda r: (r["group"], r["name"]))
    new_additions = sorted((r for r in rows if r["new"]), key=lambda r: r["code"])
    watch_excluded = sorted((r for r in excluded if "detected_at" in r), key=lambda r: r["code"])
    tt = tuple(LC.load_bt_cfg()[f"tim_tiers_{market}"])
    bt_vals = [r["bt"] for r in rows if isinstance(r.get("bt"), (int, float))]
    bt_counts = {
        "買い場": sum(1 for v in bt_vals if v >= tt[0]),
        "妥当": sum(1 for v in bt_vals if tt[1] <= v < tt[0]),
        "やや割高": sum(1 for v in bt_vals if tt[2] <= v < tt[1]),
        "割高": sum(1 for v in bt_vals if v < tt[2]),
    }

    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "market": market,
        "universe": {"count": univ.get("count"), "generated_at": univ.get("generated_at")},
        "weights": QUALITY_W,
        "counts": {
            "total": len(rows), "excluded": len(excluded), "missing": len(missing),
            "1軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "1軍"),
            "2軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "2軍"),
            "3軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "3軍"),
        },
        "buytiming_counts": bt_counts, "tim_tiers": list(tt),
        "groups": groups_out, "global_top": global_top, "excluded": excluded,
        "missing": sorted(missing),
        "new_additions": new_additions, "watch_excluded": watch_excluded,
    }
    _print_dist(out)
    return out, m


def _print_dist(out):
    allq = sorted(s["q"] for g in out["groups"] for s in g["stocks"])
    allbt = sorted(s["bt"] for g in out["groups"] for s in g["stocks"]
                   if isinstance(s.get("bt"), (int, float)))
    for lab, xs in (("品質", allq), ("買い時", allbt)):
        if xs:
            p = lambda k, xs=xs: xs[min(len(xs) - 1, int(len(xs) * k))]
            print(f"  {lab}スコア分布 n={len(xs)}  min {xs[0]:.1f}  p10 {p(.1):.1f}  "
                  f"p25 {p(.25):.1f}  median {st.median(xs):.1f}  p60 {p(.6):.1f}  "
                  f"p75 {p(.75):.1f}  max {xs[-1]:.1f}")
    print("  グループ中央値:", {g["name"]: g["median"] for g in out["groups"]})
    print(f"  {out['counts']}  買い時内訳 {out['buytiming_counts']}")


# ---------------------------------------------------------------- HTML
def _report_href(code, report_dirs):
    for rel, absdir in report_dirs:
        if os.path.isfile(os.path.join(absdir, f"{code}.html")):
            return rel + code + ".html"
    return None


def _code_cell(code, report_dirs):
    href = _report_href(code, report_dirs)
    return f'<a href="{href}">{code}</a>' if href else str(code)


def _filter_attrs_long(s, unit, grade):
    """「詳しい条件で絞り込む」パネル用のdata-*属性一式。欠損は_fv()で空文字にする
    （JS側で必ず対象外になる。良好/注意/弱い全部にチェックしても対象外は救えない仕様
    ＝配当株ツール側のbandOk()と同じ設計）。"""
    mcap = s.get("mcap")
    mcap_disp = (mcap / 1e8) if (unit != "$" and isinstance(mcap, (int, float))) else \
                (mcap / 1e9) if isinstance(mcap, (int, float)) else None  # JP:億円 US:$十億（表示単位に揃える）
    parts = [
        f'data-q="{_fv(s.get("q"))}"', f'data-bt="{_fv(s.get("bt"))}"',
        f'data-price="{_fv(s.get("price"))}"', f'data-mcap="{_fv(mcap_disp)}"',
        f'data-tier="{html.escape(str(s.get("tier") or "―"))}"',
        f'data-grade="{html.escape(str(grade or "―"))}"',
        f'data-cov="{html.escape(str(s.get("cov") or ""))}"',
        f'data-group="{html.escape(str(s.get("group") or ""))}"',
    ]
    metrics_raw = s.get("metrics_raw") or {}
    for _, items in FILTER_METRICS:
        for k, _lab in items:
            parts.append(f'data-m_{k}="{_fv(metrics_raw.get(k))}"')
    bt_raw = s.get("bt_raw") or {}
    for k, _lab, _rule, _sk, _scale in FILTER_BT_COMPONENTS:
        parts.append(f'data-bt_{k}="{_fv(bt_raw.get(k))}"')
    parts.append(f'data-fcfy="{_fv(s.get("fcf_yield"))}"')
    if s.get("has_decel") is not None:
        parts.append(f'data-decel="{1 if s.get("has_decel") else 0}"')
    return " ".join(parts)


def render(out, m):
    th = m["theme"]
    gen = out["generated_at"]
    c = out["counts"]
    unit = m["unit_price"]
    market = out["market"]
    terms_json = json.dumps(TERMS, ensure_ascii=False)
    raw_rules = _RAW_RULES_BY_MARKET[market]

    def _band_fgrp_raw(band_id, lab, rule, num_id=None):
        """実数値（%・倍・年など）で絞り込む項目用。band_idはBAND_FILTERSのidと一致させる
        （JS側は「.f_」+band_idのクラス名でチェックボックスを探す）。num_idを渡すと
        「◯◯ 以上」の数値入力も併設し、チェックボックスとは片方しか使えない排他制御を
        JS側で行う。"""
        cheap, normal, expensive = _raw_zone_labels(rule)
        checks = (f'<label><input type="checkbox" class="f_{band_id}" value="good">{cheap}</label>'
                  f'<label><input type="checkbox" class="f_{band_id}" value="mid">{normal}</label>'
                  f'<label><input type="checkbox" class="f_{band_id}" value="weak">{expensive}</label>')
        if num_id:
            return (f'<div class="fgrp"><label class="flbl" for="f_{num_id}">{html.escape(lab)}（{rule["unit"]}） 以上</label>'
                    f'<input id="f_{num_id}" type="number" step="0.1">'
                    f'<span class="fchecks">{checks}</span></div>')
        return f'<div class="fgrp"><span class="flbl">{html.escape(lab)}</span><span class="fchecks">{checks}</span></div>'

    def _binary_fgrp(k, lab, txt):
        return (f'<div class="fgrp"><span class="flbl">{html.escape(lab)}</span>'
                f'<span class="fchecks"><label><input type="checkbox" id="f_bin_{k}">{txt}</label></span></div>')

    filter_metric_domains = []
    band_filter_list = []
    paired_num_map = {}
    for dom, items in FILTER_METRICS:
        body_parts = []
        for k, lab in items:
            if k in _BINARY_METRIC_KEYS:
                body_parts.append(_binary_fgrp(k, lab, "直近プラスのみ"))
                continue
            rule = raw_rules.get(k)
            if rule is None:
                continue  # equity_ratio(米国株)・interest_coverage(日本株)は対象外
            num_id = f"num_{k}" if k in _PAIRED_NUM_METRIC_KEYS else None
            body_parts.append(_band_fgrp_raw(f"m_{k}", lab, rule, num_id))
            band_filter_list.append([f"m_{k}", f"m_{k}", rule["dir"], rule["good"], rule["warn"]])
            if num_id:
                paired_num_map[f"m_{k}"] = [num_id]
        if not body_parts:
            continue
        filter_metric_domains.append(
            f'<div class="fsec"><span class="fsech">{html.escape(dom)}</span><div class="fsecbody">{"".join(body_parts)}</div></div>')

    band_filter_list.append(["fcfy", "fcfy", FCF_YIELD_RULE["dir"], FCF_YIELD_RULE["good"], FCF_YIELD_RULE["warn"]])
    paired_num_map["fcfy"] = ["num_fcfy"]
    band_filter_list += [[f"bt_{k}", f"bt_{k}", rule["dir"], rule["good"], rule["warn"]]
                          for k, _lab, rule, _sk, _scale in FILTER_BT_COMPONENTS]
    band_filters_json = json.dumps(band_filter_list, ensure_ascii=False)
    paired_num_json = json.dumps(paired_num_map, ensure_ascii=False)
    filters_ls_key = f'pp_filters_long{"_us" if market == "us" else ""}'

    fcfy_fgrp = _band_fgrp_raw("fcfy", "FCF利回り（FCF÷時価総額）", FCF_YIELD_RULE, "num_fcfy")
    bt_body = fcfy_fgrp + "".join(_band_fgrp_raw(f"bt_{k}", lab, rule)
                                   for k, lab, rule, _sk, _scale in FILTER_BT_COMPONENTS)
    grade_opts = "".join(f'<label><input type="checkbox" class="f_grd" value="{x}">{x}</label>' for x in ("A", "B", "C"))
    cov_opts = "".join(f'<label><input type="checkbox" class="f_cov" value="{x}">{x}</label>' for x in ("高", "中", "低"))
    grp_opts = "".join(f'<label><input type="checkbox" class="f_grp" value="{html.escape(g["name"])}">{html.escape(g["name"])}</label>'
                        for g in out["groups"])
    decel_fgrp = ('<div class="fgrp"><span class="flbl">直近四半期の急減速</span>'
                  '<span class="fchecks">'
                  '<label><input type="checkbox" id="f_decel_none">フラグが無い銘柄のみ</label>'
                  '<label><input type="checkbox" id="f_decel_flag">フラグがある銘柄のみ</label>'
                  '</span></div>'
                  if market == "us" else "")

    filter_panel_html = f'''<details id="filterbox">
<summary><span>詳しい条件で絞り込む</span><button type="button" id="fclear2" class="fclear-top">条件をクリア</button><span class="fhit" id="fhit2"></span></summary>
<div class="fpanel">
  <div class="fmaj"><h3 class="fmajh">① 銘柄選定（品質スコア）</h3>
    <div class="fsec"><div class="fsecbody">
      <div class="fgrp"><label class="flbl" for="f_qsc">品質スコア 以上</label><input id="f_qsc" type="number" min="0" max="110"></div>
    </div></div>
    {"".join(filter_metric_domains)}
  </div>
  <div class="fmaj"><h3 class="fmajh">② 買い時</h3>
    <div class="fsec"><div class="fsecbody">
      <div class="fgrp"><label class="flbl" for="f_bt">買い時スコア 以上</label><input id="f_bt" type="number" min="0" max="110"></div>
    </div></div>
    <div class="fsec"><span class="fsech">買い時の内訳</span><div class="fsecbody">{bt_body}</div></div>
  </div>
  <div class="fmaj"><h3 class="fmajh">銘柄属性</h3>
    <div class="fsec"><div class="fsecbody">
      <div class="fgrp"><label class="flbl" for="f_mc">時価総額（{"億円" if unit != "$" else "10億ドル"}） 以上</label><input id="f_mc" type="number" min="0"></div>
      <div class="fgrp"><label class="flbl" for="f_pr">終値（{unit}） 以下</label><input id="f_pr" type="number" min="0"></div>
      <div class="fgrp"><span class="flbl">業種級</span><span class="fchecks">{grade_opts}</span></div>
      <div class="fgrp"><span class="flbl">カバレッジ</span><span class="fchecks">{cov_opts}</span></div>
      <div class="fgrp wide"><span class="flbl">業種グループ</span><span class="fchecks">{grp_opts}</span></div>
      {decel_fgrp}
    </div></div>
  </div>
</div>
<div class="fbar">
  <button type="button" id="fclear">条件をクリア</button>
  <span class="fhit" id="fhit"></span>
</div>
</details>
<div class="fempty" id="fempty" hidden>条件に合う銘柄がありません。条件を緩めてください。</div>'''

    tt = tuple(LC.load_bt_cfg()[f"tim_tiers_{out['market']}"])

    def _bt_cell(v):
        if not isinstance(v, (int, float)):
            return '<td class="n bt">―</td>'
        cls = "t1" if v >= tt[0] else "t2" if v >= tt[1] else "t3" if v >= tt[2] else "t4"
        return f'<td class="n bt {cls}" data-v="{v}"><b>{v:.0f}</b></td>'

    grade_by_group = {g["name"]: g["grade"] for g in out["groups"]}

    def row_html(s, with_rank=None):
        tcls = th.TIER_CLASS.get(s.get("tier", "―"), "t0")
        dcls = th.DIR_CLASS.get(s.get("dir", "→"), "fl")
        codecell = _code_cell(s["code"], m["report_dirs"])
        rk = f'<td class="n">{with_rank}</td>' if with_rank is not None else ""
        grade = grade_by_group.get(s.get("group"))
        fattrs = _filter_attrs_long(s, unit, grade)
        return (
            f'<tr class="{tcls} r" data-tier="{s.get("tier","―")}" {fattrs}>'
            + rk +
            f'<td class="wl"><input type="checkbox" class="wlc" data-code="{s["code"]}" aria-label="ウォッチ"></td>'
            f'<td class="pf"><input type="checkbox" class="pfc" data-code="{s["code"]}" aria-label="ポートフォリオに追加"></td>'
            f'<td class="tier">{s.get("tier","―")}<span class="dir {dcls}">{s.get("dir","")}</span></td>'
            f'<td class="code">{codecell}</td>'
            f'<td class="nm">{html.escape(str(s["name"]))}'
            f'{" <span class=\"newbadge\" title=\"月次チェックで新規追加（次の四半期見直しで正式反映）\">NEW!</span>" if s.get("new") else ""}'
            f'</td>'
            f'<td class="sec">{html.escape(str(s["sector"]))}</td>'
            f'<td class="n" data-v="{_v(s["q"])}"><b>{_num(s["q"],0)}</b></td>'
            f'<td class="n" data-v="{_v(s["perf"])}">{_num(s["perf"],0)}</td>'
            f'<td class="n" data-v="{_v(s["fin"])}">{_num(s["fin"],0)}</td>'
            f'<td class="n" data-v="{_v(s["cf"])}">{_num(s["cf"],0)}</td>'
            + _bt_cell(s.get("bt")) +
            f'<td class="n" data-v="{_v(s["yield"])}">{_num(s["yield"],2)}%</td>'
            f'<td class="n px" data-v="{_v(s["price"])}">{_price(s["price"], unit)}</td>'
            f'<td class="cv">{s.get("cov","―")}</td>'
            f'</tr>')

    thead = ('<thead><tr>{rk}<th class="wl" title="ウォッチリストに追加する銘柄にチェック">☑</th>'
             '<th class="pf" title="ポートフォリオに追加する銘柄にチェック">💼</th>'
             '<th class="hdr" data-term="tier">軍</th><th>コード</th><th>銘柄</th><th>業種</th>'
             '<th class="n hdr" data-term="q">品質</th>'
             '<th class="n hdr" data-term="perf">業績</th>'
             '<th class="n hdr" data-term="fin">財務</th>'
             '<th class="n hdr" data-term="cf">CF</th>'
             '<th class="n hdr" data-term="bt">買い時</th>'
             '<th class="n hdr" data-term="yield">利回り</th>'
             '<th class="n hdr" data-term="price">終値</th>'
             '<th class="hdr" data-term="cov">カバレッジ</th></tr></thead>')

    secs = []
    for g in out["groups"]:
        head = (f'<h2>{html.escape(g["name"])} '
                f'<span class="grade grade{g["grade"]} hdr" data-term="grade">業種級 {g["grade"]}</span> '
                f'<span class="gmeta">中央値 {_num(g["median"])} ／ {g["count"]}銘柄'
                f'{"" if g["tiered"] else " ・ 少数のため軍分けなし"}</span></h2>')
        trs = "".join(row_html(s) for s in g["stocks"])
        secs.append('<section class="grp">' + head + '<table>'
                    + thead.format(rk="") + '<tbody>' + trs + '</tbody></table></section>')

    gt = "".join(row_html(s, with_rank=i + 1) for i, s in enumerate(out["global_top"]))

    watch_change_html = ""
    new_adds, watch_exc = out.get("new_additions") or [], out.get("watch_excluded") or []
    if m.get("watch") and not (new_adds or watch_exc):
        watch_change_html = (
            '<section class="grp"><h2>直近の構成銘柄変更</h2>'
            '<p class="sub" style="margin:4px 0 0">データ不足（月次チェックが未実施、'
            'または前回チェック以降にS&amp;P500の構成銘柄変更なし。月次チェックは毎月1日に'
            '自動実行されます）</p></section>')
    elif m.get("watch"):
        new_rows = "".join(
            f'<tr><td class="code">{_code_cell(s["code"], m["report_dirs"])}</td>'
            f'<td class="nm">{html.escape(str(s["name"]))}</td>'
            f'<td class="sec">{html.escape(str(s["sector"]))}</td>'
            f'<td class="n" data-v="{_v(s["q"])}">{_num(s["q"],0)}</td>'
            f'<td class="sec">{s.get("new_at") or "―"}</td></tr>'
            for s in new_adds) or '<tr><td colspan="5" class="sec">なし</td></tr>'
        exc_rows = "".join(
            f'<tr><td class="code">{html.escape(str(s["code"]))}</td>'
            f'<td class="nm">{html.escape(str(s["name"]))}</td>'
            f'<td class="sec">{html.escape(str(s["sector"]))}</td>'
            f'<td class="sec">{html.escape(str(s.get("why","")))}</td>'
            f'<td class="sec">{s.get("detected_at") or "―"}</td></tr>'
            for s in watch_exc) or '<tr><td colspan="5" class="sec">なし</td></tr>'
        watch_change_html = (
            '<section class="grp">'
            f'<h2>直近の構成銘柄変更 <span class="gmeta">新規追加 {len(new_adds)}／除外 {len(watch_exc)}'
            '・月次S&amp;P500チェックで検知、次の四半期フル再構築で正式なリストに置き換わります</span></h2>'
            '<table><thead><tr><th>コード</th><th>銘柄</th><th>業種</th><th class="n">品質</th>'
            '<th>検知日</th></tr></thead><tbody>' + new_rows + '</tbody></table>'
            '<table style="margin-top:8px"><thead><tr><th>コード</th><th>銘柄</th><th>業種</th>'
            '<th>除外理由</th><th>検知日</th></tr></thead><tbody>' + exc_rows + '</tbody></table>'
            '</section>')

    exc = ""
    if out["excluded"]:
        etr = "".join(
            f'<tr class="r"><td class="code">{_code_cell(s["code"], m["report_dirs"])}</td>'
            f'<td class="nm">{html.escape(str(s["name"]))}'
            f'{" <span class=\"newbadge\" title=\"月次チェックで新規追加（次の四半期見直しで正式反映）\">NEW!</span>" if s.get("new") else ""}'
            f'</td>'
            f'<td class="sec">{html.escape(str(s["sector"]))}</td>'
            f'<td>{html.escape(s["why"])}</td>'
            f'<td class="n" data-v="{_v(s["yield"])}">{_num(s["yield"],2)}%</td>'
            f'<td class="n px" data-v="{_v(s["price"])}">{_price(s["price"], unit)}</td></tr>'
            for s in out["excluded"])
        exc = ('<h2>対象外（金融・REIT）<span class="gmeta">'
               f'{len(out["excluded"])}銘柄</span></h2>'
               '<p class="sub">銀行・保険・証券・REITは、自己資本比率やキャッシュフローが'
               '業種として構造的に異質で、現バージョンでは業績・財務・CFを採点していません。'
               '配当を除いた共通スコアで比較できないため、順位付けの対象外としています'
               '（利回りと終値のみ参考表示）。</p>'
               '<section class="grp"><table><thead><tr><th>コード</th><th>銘柄</th>'
               '<th>業種</th><th>区分</th><th class="n">利回り</th>'
               f'<th class="n">終値</th></tr></thead><tbody>{etr}</tbody></table></section>')

    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{th.THEME_HEAD}
<title>{html.escape(m["title"])}</title>
<style>
{th.THEME_CSS}
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.6 -apple-system,"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;
  background:var(--bg);color:var(--fg)}}
.wrap{{max-width:1040px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:12px}}
.sub a{{color:var(--accent)}}
.summary{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0 10px}}
.sumbtn{{font:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:8px 14px;min-width:76px;text-align:center;cursor:pointer;color:inherit}}
.sumbtn:hover{{border-color:var(--accent)}}
.sumbtn.active{{border-color:var(--accent);border-width:2px;background:var(--info)}}
.sumbtn b{{display:block;font-size:19px}}
h2{{font-size:15px;margin:26px 0 6px;border-bottom:2px solid var(--line);padding-bottom:4px}}
.grade{{font-size:11px;padding:1px 7px;border-radius:10px;vertical-align:middle}}
.newbadge{{font-size:10px;font-weight:700;padding:1px 6px;border-radius:10px;vertical-align:middle;
  background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent);
  border:1px solid color-mix(in srgb,var(--accent) 45%,transparent)}}
.gradeA{{background:color-mix(in srgb,var(--gA) 15%,transparent);color:var(--gA)}}
.gradeB{{background:color-mix(in srgb,var(--gB) 15%,transparent);color:var(--gB)}}
.gradeC{{background:color-mix(in srgb,var(--gC) 15%,transparent);color:var(--gC)}}
.grade―{{background:color-mix(in srgb,var(--muted) 15%,transparent);color:var(--muted)}}
.gmeta{{font-size:11px;color:var(--muted);font-weight:normal;margin-left:6px}}
table{{width:100%;border-collapse:collapse;background:var(--card);font-size:13px;
  border:1px solid var(--line);border-radius:8px;overflow:hidden}}
th,td{{padding:6px 8px;text-align:left;border-bottom:1px solid var(--line)}}
th{{background:var(--th);font-size:11px;color:var(--muted)}}
td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums}}
tr:last-child td{{border-bottom:none}}
.tier{{font-weight:700;white-space:nowrap}}
.t1 .tier{{color:var(--t1)}}.t2 .tier{{color:var(--t2)}}.t3 .tier{{color:var(--t3)}}
.dir{{font-weight:700;margin-left:3px}}
.dir.up{{color:var(--t1)}}.dir.dn{{color:var(--gC)}}.dir.fl{{color:var(--t3)}}
.code a{{color:var(--accent);text-decoration:none}}
.sec{{color:var(--muted);font-size:11px}}
.cv{{font-size:11px}}
td.bt.t1,td.bt.t1 b{{color:var(--t1)}}
td.bt.t2,td.bt.t2 b{{color:var(--t2)}}
td.bt.t3,td.bt.t3 b{{color:var(--t3)}}
td.bt.t4,td.bt.t4 b{{color:var(--gC)}}
td.wl,th.wl,td.pf,th.pf{{width:30px;text-align:center;padding-left:2px;padding-right:2px}}
th.wl,th.pf{{cursor:default}}
.wlc,.pfc{{width:15px;height:15px;cursor:pointer;accent-color:var(--accent)}}
#wlbar{{position:fixed;left:0;right:0;bottom:0;z-index:20;display:flex;gap:10px;
  align-items:center;justify-content:center;flex-wrap:wrap;background:var(--card);
  border-top:1px solid var(--line);box-shadow:0 -2px 10px rgba(0,0,0,.06);
  padding:10px 14px;font-size:13px}}
#wlbar b{{color:var(--accent)}}
#wlbar button{{padding:8px 16px;border:1px solid var(--accent);border-radius:8px;
  background:var(--accent);color:#fff;font-size:13px;cursor:pointer}}
#wlbar button.ghost{{background:var(--card);color:var(--muted);border-color:var(--line)}}
#wlbar[hidden]{{display:none}}
body.wlon{{padding-bottom:60px}}
.hdr{{cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px}}
.hdr:hover{{color:var(--accent)}}
a.sumbtn.wlnav{{border-color:var(--accent);color:var(--accent);text-decoration:none}}
a.sumbtn.wlnav b{{color:var(--accent)}}
.terminfo{{position:relative;margin:8px 0 18px;padding:12px 36px 12px 14px;background:var(--info);
  border:1px solid var(--accent);border-radius:8px;font-size:12.5px;line-height:1.7}}
.terminfo b{{display:block;margin-bottom:4px;font-size:13.5px}}
.terminfo[hidden]{{display:none}}
.ticlose{{position:absolute;top:6px;right:8px;border:none;background:none;cursor:pointer;
  font-size:15px;line-height:1;color:var(--muted);padding:4px}}
.disc{{margin-top:30px;padding:12px;background:var(--wbg);border:1px solid var(--wbd);
  border-radius:8px;font-size:11.5px;color:var(--wfg)}}
.searchbar{{display:flex;align-items:center;gap:8px;margin:14px 0 4px}}
.searchbar input{{flex:1;max-width:360px;padding:8px 10px;border:1px solid var(--line);
  border-radius:8px;font-size:13.5px;background:var(--card);color:inherit}}
.searchbar button{{padding:8px 12px;border:1px solid var(--line);border-radius:8px;
  background:var(--card);font-size:12.5px;cursor:pointer;color:var(--muted)}}
.searchbar .hit{{font-size:12px;color:var(--muted)}}
details{{margin:14px 0}}summary{{cursor:pointer;font-weight:600;font-size:13px}}
tr[hidden]{{display:none}}
section.grp[hidden]{{display:none}}
.topbar{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}}
.topbar a{{font-size:12.5px;color:var(--accent);white-space:nowrap}}
.pagenav{{display:flex;gap:14px;flex-wrap:wrap}}
.formula{{font-size:11.5px;color:var(--muted);background:var(--field);border:1px solid var(--line);
  border-radius:8px;padding:8px 12px;margin:6px 0 4px}}
#filterbox{{margin:6px 0 14px}}
#filterbox summary{{font-size:14px;font-weight:700;display:flex;align-items:center;
  gap:10px;flex-wrap:wrap}}
.fclear-top{{padding:5px 12px;border:1px solid var(--line);border-radius:8px;
  background:var(--card);font-size:12px;cursor:pointer;color:var(--muted)}}
.fclear-top:hover{{border-color:var(--accent);color:var(--fg)}}
#filterbox summary .fhit{{font-size:12px;font-weight:400;color:var(--muted)}}
.fpanel{{padding:14px 4px 4px}}
.fmaj{{width:100%}}
.fmaj+.fmaj{{margin-top:24px}}
.fmajh{{font-size:16px;font-weight:800;color:var(--fg);margin:0 0 10px;
  padding-bottom:5px;border-bottom:2px solid var(--accent)}}
.fsec{{width:100%;margin-top:16px}}
.fsec:first-child{{margin-top:0}}
.fsech{{font-size:13px;font-weight:800;color:var(--accent);margin:0 0 8px;
  padding-left:9px;border-left:3px solid var(--accent)}}
.fsecbody{{display:flex;flex-wrap:wrap;gap:10px 14px}}
.fgrp{{display:flex;flex-direction:column;gap:5px;min-width:130px;
  padding:8px 10px;background:var(--card);border:1px solid var(--line);border-radius:6px}}
.fgrp.wide{{min-width:220px}}
.fgrp .flbl{{font-size:12.5px;color:var(--muted);font-weight:600}}
.fgrp input[type=number]{{width:88px;padding:6px 8px;border:1px solid var(--line);
  border-radius:6px;font-size:13px;background:var(--bg);color:var(--fg)}}
.fchecks{{display:flex;flex-wrap:wrap;gap:6px 10px}}
.fchecks label{{display:inline-flex;align-items:center;gap:4px;font-size:12.5px;white-space:nowrap}}
.fchecks input{{accent-color:var(--accent)}}
.fgrp input[type=number]:disabled{{opacity:.4;cursor:not-allowed}}
.fchecks label:has(input:disabled){{opacity:.4;cursor:not-allowed}}
.fbar{{display:flex;align-items:center;gap:10px;margin:16px 4px 2px}}
.fbar button{{padding:7px 14px;border:1px solid var(--line);border-radius:8px;
  background:var(--card);font-size:12.5px;cursor:pointer;color:var(--muted)}}
.fbar button:hover{{border-color:var(--accent);color:var(--fg)}}
.fbar .fhit{{font-size:12px;color:var(--muted)}}
.fempty{{padding:20px;text-align:center;color:var(--muted);font-size:13px}}
.fempty[hidden]{{display:none}}
</style></head><body><div class="wrap">
<div class="topbar"><h1>{html.escape(m["title"])}</h1>{_pagenav("index.html", exclude=("watchlist.html", "portfolio.html"))}</div>
{th.THEME_BAR}
<div class="sub">生成 {gen}　｜　{html.escape(m["screen_line"])}</div>
<div class="formula">品質スコア ＝ 業績×0.28 ＋ 財務×0.27 ＋ キャッシュフロー×0.15（取得できた
グループだけで再正規化・0〜110）。既存の銘柄診断エンジンの「銘柄選定スコア」から
<b>配当の持続力（連続増配・増配率・配当性向・累進配当宣言など）を除いた</b>もの。
利回りは参考表示で採点に不使用。</div>
<div class="summary">
  <button type="button" class="sumbtn" data-tier=""><b>{c['total']}</b>銘柄</button>
  <button type="button" class="sumbtn" data-tier="1軍"><b>{c['1軍']}</b>1軍</button>
  <button type="button" class="sumbtn" data-tier="2軍"><b>{c['2軍']}</b>2軍</button>
  <button type="button" class="sumbtn" data-tier="3軍"><b>{c['3軍']}</b>3軍</button>
  <span class="sumbtn" style="cursor:default"><b>{c['excluded']}</b>対象外</span>
  <a class="sumbtn wlnav" href="watchlist.html"><b>☆</b>ウォッチリスト</a>
  <a class="sumbtn wlnav" href="portfolio.html"><b id="pfnav-n">💼</b>ポートフォリオ</a>
</div>
<div class="sub" style="margin:-4px 0 12px">見出し（軍・品質・業績・財務・CF・買い時・利回り・カバレッジ・業種級）をクリックすると説明が出ます。数字ボタンでその軍だけ表示（もう一度で解除）。☆にチェックを入れて下部の「ウォッチリストを作成」を、💼にチェックを入れて「ポートフォリオに追加」を押すと、それぞれ選んだ銘柄だけの一覧・保有記録の入力画面を作れます（☆と💼は別々に選べます）。</div>
<div id="terminfo" class="terminfo" hidden>
  <button type="button" id="terminfo-close" class="ticlose" aria-label="閉じる">✕</button>
  <div id="terminfo-body"></div>
</div>
<div class="searchbar">
  <input id="q" type="search" placeholder="コード・銘柄名・業種で検索" autocomplete="off">
  <button id="qclear" type="button">クリア</button>
  <span class="hit" id="qhit"></span>
  <button id="csvExport" type="button">表示中の銘柄をCSV出力</button>
</div>
{filter_panel_html}
{watch_change_html}
<details id="topbox"><summary>全体 品質スコア 上位50（業種横断）</summary>
<table><thead><tr><th class="n">#</th><th class="wl">☑</th><th class="pf">💼</th>
<th class="hdr" data-term="tier">軍</th><th>コード</th><th>銘柄</th><th>業種</th>
<th class="n hdr" data-term="q">品質</th><th class="n hdr" data-term="perf">業績</th>
<th class="n hdr" data-term="fin">財務</th><th class="n hdr" data-term="cf">CF</th>
<th class="n hdr" data-term="bt">買い時</th><th class="n hdr" data-term="yield">利回り</th>
<th class="n hdr" data-term="price">終値</th><th class="hdr" data-term="cov">カバレッジ</th></tr></thead>
<tbody>{gt}</tbody></table></details>
{"".join(secs)}
{exc}
<div class="disc">{DISC}</div>
<div id="wlbar" hidden><span>☆ <b id="wlcount">0</b>銘柄</span>
<button type="button" id="wlgo">ウォッチリストを作成 →</button>
<span>💼 <b id="pfcount">0</b>銘柄</span>
<button type="button" id="pfgo">ポートフォリオに追加 →</button>
<button type="button" id="wlclear" class="ghost">選択をクリア</button></div>
<script>
{th.THEME_JS}
(function(){{
  var q=document.getElementById('q'),hit=document.getElementById('qhit');
  var btns=document.querySelectorAll('.sumbtn[data-tier]'),activeTier='';
  var topbox=document.getElementById('topbox');
  var filterbox=document.getElementById('filterbox');
  var fempty=document.getElementById('fempty');
  var fhit=document.getElementById('fhit'),fhit2=document.getElementById('fhit2');

  var NUM_FILTERS=[['qsc','q','ge'],['bt','bt','ge'],['mc','mcap','ge'],['pr','price','le'],
    ['num_rev_cagr','m_rev_cagr','ge'],['num_eps_cagr','m_eps_cagr','ge'],
    ['num_op_margin','m_op_margin','ge'],['num_fcfy','fcfy','ge']];
  var numEls={{}};
  NUM_FILTERS.forEach(function(f){{ numEls['f_'+f[0]]=document.getElementById('f_'+f[0]); }});

  // BAND_FILTERS: [id, data属性名, 方向(higher_better/lower_better), good閾値, warn閾値]。
  // 業績・財務・CFの個別指標とFCF利回りは実数値としきい値を比較し、買い時内訳の一部
  // （EV/EBIT対業種・PER割安度・PBR割安度）はスコア（0〜110・常にhigher_better＝good=100/
  // warn=60）のまま据え置く（複数の生数値を合成した値のため単一の実数値しきい値を持たない）。
  var BAND_FILTERS={band_filters_json};
  var bandEls={{}};
  BAND_FILTERS.forEach(function(f){{ bandEls[f[0]]=document.querySelectorAll('.f_'+f[0]); }});

  var grdEls=document.querySelectorAll('.f_grd');
  var covEls=document.querySelectorAll('.f_cov');
  var grpEls=document.querySelectorAll('.f_grp');
  var ocfEl=document.getElementById('f_bin_ocf_positive');
  var fcfPosEl=document.getElementById('f_bin_fcf_positive');
  var decelNoneEl=document.getElementById('f_decel_none');
  var decelFlagEl=document.getElementById('f_decel_flag');

  function checkedVals(els){{ return Array.prototype.filter.call(els,function(e){{return e.checked;}}).map(function(e){{return e.value;}}); }}

  function bandOk(tr){{
    for(var i=0;i<BAND_FILTERS.length;i++){{
      var id=BAND_FILTERS[i][0],attr=BAND_FILTERS[i][1],dir=BAND_FILTERS[i][2],good=BAND_FILTERS[i][3],warn=BAND_FILTERS[i][4];
      var sel=checkedVals(bandEls[id]);
      if(!sel.length)continue;
      var v=parseFloat(tr.dataset[attr]);
      if(isNaN(v))return false;
      var zone=(dir==='higher_better')?(v>=good?'good':(v>=warn?'mid':'weak')):(v<=good?'good':(v<=warn?'mid':'weak'));
      if(sel.indexOf(zone)===-1)return false;
    }}
    return true;
  }}

  // 数値入力と良好/注意/弱いチェックボックスの両方を持つ項目：どちらか一方しか
  // 使えないよう、片方に値が入るともう片方を無効化する（配当株ツール側と同じ設計）。
  var PAIRED_NUM_IDS={paired_num_json};
  function syncPairDisabled(){{
    Object.keys(PAIRED_NUM_IDS).forEach(function(key){{
      var numElList=PAIRED_NUM_IDS[key].map(function(id){{return numEls['f_'+id];}}).filter(Boolean);
      var boxes=bandEls[key];
      if(!numElList.length||!boxes)return;
      var hasNum=numElList.some(function(el){{return el.value!=='';}});
      var anyChecked=checkedVals(boxes).length>0;
      numElList.forEach(function(el){{el.disabled=anyChecked;}});
      Array.prototype.forEach.call(boxes,function(b){{b.disabled=hasNum;}});
    }});
  }}
  Object.keys(PAIRED_NUM_IDS).forEach(function(key){{
    var numElList=PAIRED_NUM_IDS[key].map(function(id){{return numEls['f_'+id];}}).filter(Boolean);
    var boxes=bandEls[key];
    if(!numElList.length||!boxes)return;
    numElList.forEach(function(numEl){{
      numEl.addEventListener('input',function(){{
        if(numEl.value!=='')Array.prototype.forEach.call(boxes,function(b){{b.checked=false;}});
        syncPairDisabled();apply();
      }});
    }});
    Array.prototype.forEach.call(boxes,function(b){{
      b.addEventListener('change',function(){{
        if(b.checked)numElList.forEach(function(el){{el.value='';}});
        syncPairDisabled();apply();
      }});
    }});
  }});

  function numOk(tr){{
    for(var i=0;i<NUM_FILTERS.length;i++){{
      var id='f_'+NUM_FILTERS[i][0],attr=NUM_FILTERS[i][1],dir=NUM_FILTERS[i][2];
      var raw=numEls[id].value;
      if(raw==='')continue;
      var want=parseFloat(raw);
      var have=parseFloat(tr.dataset[attr]);
      if(isNaN(have))return false;
      if(dir==='ge'&&have<want)return false;
      if(dir==='le'&&have>want)return false;
    }}
    return true;
  }}

  function panelOk(tr){{
    if(!numOk(tr))return false;
    if(!bandOk(tr))return false;
    if(ocfEl&&ocfEl.checked&&tr.dataset.m_ocf_positive!=='1')return false;
    if(fcfPosEl&&fcfPosEl.checked&&tr.dataset.m_fcf_positive!=='1')return false;
    if((decelNoneEl&&decelNoneEl.checked)||(decelFlagEl&&decelFlagEl.checked)){{
      var want=[];
      if(decelNoneEl&&decelNoneEl.checked)want.push('0');
      if(decelFlagEl&&decelFlagEl.checked)want.push('1');
      if(want.indexOf(tr.dataset.decel||'')===-1)return false;
    }}
    var grds=checkedVals(grdEls);
    if(grds.length&&grds.indexOf(tr.dataset.grade)===-1)return false;
    var covs=checkedVals(covEls);
    if(covs.length&&covs.indexOf(tr.dataset.cov)===-1)return false;
    var grps=checkedVals(grpEls);
    if(grps.length&&grps.indexOf(tr.dataset.group)===-1)return false;
    return true;
  }}

  function panelActive(){{
    if(ocfEl&&ocfEl.checked)return true;
    if(fcfPosEl&&fcfPosEl.checked)return true;
    if((decelNoneEl&&decelNoneEl.checked)||(decelFlagEl&&decelFlagEl.checked))return true;
    if(Object.keys(bandEls).some(function(id){{return checkedVals(bandEls[id]).length;}}))return true;
    if(checkedVals(grdEls).length||checkedVals(covEls).length||checkedVals(grpEls).length)return true;
    return Object.keys(numEls).some(function(id){{return numEls[id].value!=='';}});
  }}

  var allFilterEls=Object.keys(numEls).map(function(k){{return numEls[k];}})
    .concat([].concat.apply([],Object.keys(bandEls).map(function(k){{return Array.prototype.slice.call(bandEls[k]);}})))
    .concat(Array.prototype.slice.call(grdEls),Array.prototype.slice.call(covEls),Array.prototype.slice.call(grpEls))
    .concat(ocfEl?[ocfEl]:[],fcfPosEl?[fcfPosEl]:[],decelNoneEl?[decelNoneEl]:[],decelFlagEl?[decelFlagEl]:[]);

  function apply(){{
    var needle=(q.value||'').trim().normalize('NFKC').toLowerCase();
    var pActive=panelActive();
    var filtering=!!needle||!!activeTier||pActive;
    var n=0;
    document.querySelectorAll('tr.r').forEach(function(tr){{
      var okT=!needle||tr.textContent.normalize('NFKC').toLowerCase().indexOf(needle)!==-1;
      var okTier=!activeTier||tr.dataset.tier===activeTier;
      var show=okT&&okTier&&(!pActive||panelOk(tr));
      tr.hidden=!show;if(show)n++;
    }});
    document.querySelectorAll('section.grp').forEach(function(sec){{
      var any=sec.querySelector('tbody tr.r:not([hidden])');sec.hidden=filtering&&!any;
    }});
    if(topbox){{
      var anyTop=topbox.querySelector('tbody tr.r:not([hidden])');
      topbox.hidden=filtering&&!anyTop;
    }}
    hit.textContent=filtering?(n+'件'):'';
    if(fhit)fhit.textContent=pActive?(n+'件該当'):'';
    if(fhit2)fhit2.textContent=pActive?(n+'件該当'):'';
    if(fempty)fempty.hidden=!(filtering&&n===0);
    syncUrl(needle);
  }}

  var urlTimer=null;
  function syncUrl(needle){{
    clearTimeout(urlTimer);
    urlTimer=setTimeout(function(){{
      var p=new URLSearchParams();
      if(needle)p.set('q',q.value.trim());
      if(activeTier)p.set('t',activeTier);
      Object.keys(numEls).forEach(function(id){{ if(numEls[id].value!=='')p.set(id.slice(2),numEls[id].value); }});
      if(ocfEl&&ocfEl.checked)p.set('ocf','1');
      if(fcfPosEl&&fcfPosEl.checked)p.set('fcfpos','1');
      if(decelNoneEl&&decelNoneEl.checked)p.set('dn','1');
      if(decelFlagEl&&decelFlagEl.checked)p.set('df','1');
      Object.keys(bandEls).forEach(function(id){{ var sel=checkedVals(bandEls[id]); if(sel.length)p.set(id,sel.join(',')); }});
      var grds2=checkedVals(grdEls); if(grds2.length)p.set('grd',grds2.join(','));
      var covs2=checkedVals(covEls); if(covs2.length)p.set('cov',covs2.join(','));
      var grps2=checkedVals(grpEls); if(grps2.length)p.set('grp',grps2.join(','));
      var qs=p.toString();
      var url=location.pathname+(qs?'?'+qs:'');
      history.replaceState(null,'',url);
      try{{ if(qs)localStorage.setItem('{filters_ls_key}',qs);else localStorage.removeItem('{filters_ls_key}'); }}catch(e){{}}
    }},300);
  }}

  function restoreFromUrl(){{
    var qs=location.search;
    if(!qs){{ try{{ var saved=localStorage.getItem('{filters_ls_key}'); if(saved)qs='?'+saved; }}catch(e){{}} }}
    var p=new URLSearchParams(qs);
    if(!p.toString())return;
    if(p.has('q'))q.value=p.get('q');
    if(p.has('t')){{
      activeTier=p.get('t');
      btns.forEach(function(b){{b.classList.toggle('active',b.dataset.tier===activeTier);}});
    }}
    Object.keys(numEls).forEach(function(id){{
      var key=id.slice(2);
      if(p.has(key))numEls[id].value=p.get(key);
    }});
    if(p.get('ocf')==='1'&&ocfEl)ocfEl.checked=true;
    if(p.get('fcfpos')==='1'&&fcfPosEl)fcfPosEl.checked=true;
    if(p.get('dn')==='1'&&decelNoneEl)decelNoneEl.checked=true;
    if(p.get('df')==='1'&&decelFlagEl)decelFlagEl.checked=true;
    Object.keys(bandEls).forEach(function(id){{
      if(!p.has(id))return;
      var vals=p.get(id).split(',');
      Array.prototype.forEach.call(bandEls[id],function(e){{ if(vals.indexOf(e.value)!==-1)e.checked=true; }});
    }});
    (p.get('grd')||'').split(',').forEach(function(v){{ grdEls.forEach(function(e){{if(e.value===v)e.checked=true;}}); }});
    (p.get('cov')||'').split(',').forEach(function(v){{ covEls.forEach(function(e){{if(e.value===v)e.checked=true;}}); }});
    (p.get('grp')||'').split(',').forEach(function(v){{ grpEls.forEach(function(e){{if(e.value===v)e.checked=true;}}); }});
    syncPairDisabled();
    if(panelActive()&&filterbox)filterbox.open=true;
  }}

  q.addEventListener('input',apply);
  document.getElementById('qclear').addEventListener('click',function(){{q.value='';apply();q.focus();}});
  btns.forEach(function(b){{b.addEventListener('click',function(){{
    var t=b.dataset.tier;activeTier=(activeTier===t)?'':t;
    btns.forEach(function(x){{x.classList.toggle('active',x.dataset.tier===activeTier&&activeTier!=='');}});
    apply();
  }});}});
  allFilterEls.forEach(function(el){{el.addEventListener('input',apply);el.addEventListener('change',apply);}});
  function clearAllFilters(){{
    Object.keys(numEls).forEach(function(id){{numEls[id].value='';}});
    if(ocfEl)ocfEl.checked=false;
    if(fcfPosEl)fcfPosEl.checked=false;
    if(decelNoneEl)decelNoneEl.checked=false;
    if(decelFlagEl)decelFlagEl.checked=false;
    Object.keys(bandEls).forEach(function(id){{ Array.prototype.forEach.call(bandEls[id],function(e){{e.checked=false;}}); }});
    grdEls.forEach(function(e){{e.checked=false;}});
    covEls.forEach(function(e){{e.checked=false;}});
    grpEls.forEach(function(e){{e.checked=false;}});
    syncPairDisabled();
    apply();
  }}
  document.getElementById('fclear').addEventListener('click',clearAllFilters);
  document.getElementById('fclear2').addEventListener('click',function(e){{e.preventDefault();e.stopPropagation();clearAllFilters();}});

  function csvCell(v){{
    var s=v==null?'':String(v);
    if(/[",\\r\\n]/.test(s))s='"'+s.replace(/"/g,'""')+'"';
    return s;
  }}
  function csvNum(v,digits){{
    var n=parseFloat(v);
    return isNaN(n)?'':n.toFixed(digits);
  }}
  document.getElementById('csvExport').addEventListener('click',function(){{
    var seen={{}};
    var rows=[];
    document.querySelectorAll('tr.r').forEach(function(tr){{
      if(tr.hidden)return;
      var codeEl=tr.querySelector('.code');
      if(!codeEl)return;
      var code=codeEl.textContent.trim();
      if(seen[code])return;
      seen[code]=true;
      var nameEl=tr.querySelector('.nm');
      rows.push([
        code, nameEl?nameEl.textContent.trim():'', tr.dataset.group||'',
        tr.dataset.grade||'', tr.dataset.tier||'', csvNum(tr.dataset.price,0),
        csvNum(tr.dataset.q,0), csvNum(tr.dataset.bt,0), tr.dataset.cov||''
      ]);
    }});
    var header=['コード','銘柄名','業種グループ','業種級','軍','終値','品質スコア','買い時スコア','カバレッジ'];
    var lines=[header].concat(rows).map(function(r){{return r.map(csvCell).join(',');}});
    var blob=new Blob(['﻿'+lines.join('\\r\\n')],{{type:'text/csv;charset=utf-8;'}});
    var url=URL.createObjectURL(blob);
    var a=document.createElement('a');
    a.href=url;
    a.download='meigara_shindan_long_'+new Date().toISOString().slice(0,10)+'.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function(){{URL.revokeObjectURL(url);}},1000);
  }});

  restoreFromUrl();
  syncPairDisabled();
  apply();
  // ---- ウォッチリスト／ポートフォリオ選択（別々のチェックボックス列）。選択状態は
  // localStorageに保存し、ウォッチリスト／ポートフォリオページへ移動して戻っても維持する ----
  function loadCodes(key){{
    try {{ return new Set((localStorage.getItem(key)||'').split(',').map(function(s){{return s.trim();}}).filter(Boolean)); }}
    catch(e) {{ return new Set(); }}
  }}
  var WL_KEY='wl_codes_long{"_us" if out["market"]=="us" else ""}';
  var PF_SEL_KEY='pf_sel_codes_long{"_us" if out["market"]=="us" else ""}';
  var wlSet=loadCodes(WL_KEY), pfSet=loadCodes(PF_SEL_KEY);
  var bar=document.getElementById('wlbar'),cnt=document.getElementById('wlcount');
  var pfCnt=document.getElementById('pfcount'),pfNav=document.getElementById('pfnav-n');
  function wlSave(){{ try {{ localStorage.setItem(WL_KEY, Array.from(wlSet).join(',')); }} catch(e) {{}} }}
  function pfSave(){{ try {{ localStorage.setItem(PF_SEL_KEY, Array.from(pfSet).join(',')); }} catch(e) {{}} }}
  function sync(){{
    cnt.textContent=wlSet.size;
    pfCnt.textContent=pfSet.size;
    if(pfNav)pfNav.textContent=pfSet.size?String(pfSet.size):'💼';
    bar.hidden=(wlSet.size===0&&pfSet.size===0);
    document.body.classList.toggle('wlon',wlSet.size>0||pfSet.size>0);
  }}
  function wlSync(){{
    document.querySelectorAll('.wlc').forEach(function(cb){{cb.checked=wlSet.has(cb.dataset.code);}});
    document.querySelectorAll('.pfc').forEach(function(cb){{cb.checked=pfSet.has(cb.dataset.code);}});
    sync();
  }}
  document.querySelectorAll('.wlc').forEach(function(cb){{
    cb.addEventListener('change',function(){{
      var c=cb.dataset.code;
      if(cb.checked)wlSet.add(c);else wlSet.delete(c);
      document.querySelectorAll('.wlc[data-code="'+c+'"]').forEach(function(o){{o.checked=cb.checked;}});
      sync();wlSave();
    }});
  }});
  document.querySelectorAll('.pfc').forEach(function(cb){{
    cb.addEventListener('change',function(){{
      var c=cb.dataset.code;
      if(cb.checked)pfSet.add(c);else pfSet.delete(c);
      document.querySelectorAll('.pfc[data-code="'+c+'"]').forEach(function(o){{o.checked=cb.checked;}});
      sync();pfSave();
    }});
  }});
  document.getElementById('wlclear').addEventListener('click',function(){{
    wlSet.clear();pfSet.clear();wlSave();pfSave();
    document.querySelectorAll('.wlc,.pfc').forEach(function(o){{o.checked=false;}});
    sync();
  }});
  document.getElementById('wlgo').addEventListener('click',function(){{
    if(!wlSet.size)return;
    wlSave();
    location.href='watchlist.html?codes='+encodeURIComponent(Array.from(wlSet).join(','));
  }});
  document.getElementById('pfgo').addEventListener('click',function(){{
    if(!pfSet.size)return;
    pfSave();
    location.href='portfolio.html?add='+encodeURIComponent(Array.from(pfSet).join(','));
  }});
  wlSync();
  // ---- 見出しクリックで用語説明 ----
  var TERMS={terms_json};
  var tibox=document.getElementById('terminfo'),tibody=document.getElementById('terminfo-body');
  document.querySelectorAll('.hdr').forEach(function(el){{
    el.addEventListener('click',function(e){{
      e.stopPropagation();
      var t=TERMS[el.dataset.term];if(!t)return;
      tibody.innerHTML='<b>'+t[0]+'</b>'+t[1];
      tibox.hidden=false;tibox.scrollIntoView({{behavior:'smooth',block:'nearest'}});
    }});
  }});
  document.getElementById('terminfo-close').addEventListener('click',function(){{tibox.hidden=true;}});
}})();
</script>
</div></body></html>"""


def render_watchlist(out, m):
    """?codes=A,B,C を ranking.json から引いて表にするだけの静的ページ。"""
    th = m["theme"]
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{th.THEME_HEAD}
<title>ウォッチリスト｜{html.escape(m['title'])}</title>
<style>
{th.THEME_CSS}
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.6 -apple-system,"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;
  background:var(--bg);color:var(--fg)}}
.wrap{{max-width:900px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:19px;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:12px}} .sub a{{color:var(--accent)}}
.topbar{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}}
.topbar a{{font-size:12.5px;color:var(--accent);white-space:nowrap}}
.pagenav{{display:flex;gap:14px;flex-wrap:wrap}}
.box{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0}}
.box h2{{font-size:14px;margin:0 0 8px}}
.codewrap{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}}
#codestr{{flex:1;min-width:220px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
  font:12px/1.4 monospace;background:var(--field);color:var(--fg)}}
#copystat{{font-size:12px;color:var(--gA)}}
textarea{{width:100%;min-height:52px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
  font:12px/1.5 monospace;background:var(--field);color:var(--fg);resize:vertical}}
button{{padding:7px 13px;border:1px solid var(--accent);border-radius:8px;background:var(--accent);
  color:#fff;font-size:12.5px;cursor:pointer;margin-right:6px}}
button.ghost{{background:var(--card);color:var(--muted);border-color:var(--line)}}
table{{width:100%;border-collapse:collapse;background:var(--card);font-size:13px;
  border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-top:8px}}
th,td{{padding:6px 8px;text-align:left;border-bottom:1px solid var(--line)}}
th{{background:var(--th);font-size:11px;color:var(--muted)}}
td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums}}
tr:last-child td{{border-bottom:none}}
.code a{{color:var(--accent);text-decoration:none}}
.sec{{color:var(--muted);font-size:11px}}
td.bt.t1{{color:var(--t1)}} td.bt.t2{{color:var(--t2)}} td.bt.t3{{color:var(--t3)}} td.bt.t4{{color:var(--gC)}}
.empty{{color:var(--muted);font-size:13px;padding:14px 0}}
</style></head><body><div class="wrap">
<div class="topbar"><h1>ウォッチリスト</h1>{_pagenav("watchlist.html")}</div>
<div class="sub">{html.escape(m['title'])}</div>

<div class="box">
  <h2>あなたの銘柄リスト</h2>
  <div class="codewrap">
    <input id="codestr" readonly value="">
    <button type="button" id="copy">コピー</button>
    <span id="copystat"></span>
  </div>
  <p>この<b>銘柄リストをコピーして控えておく</b>と、別の端末でも下の貼り付け欄から復元できます。</p>
  <p><b>同じ端末・同じブラウザ</b>なら、一度作成すれば次回からこのページを開くだけで復元されます（貼り付け不要）。</p>
</div>

<div class="box">
  <h2>銘柄リストを貼り付けて表示</h2>
  <p class="sub" style="margin:0 0 8px">コード／ティッカーをカンマ・空白・改行区切りで。URL の <code>?codes=</code> でも可。</p>
  <textarea id="paste" placeholder="例：7203, 9433, 6146"></textarea>
  <div style="margin-top:8px">
    <button type="button" id="show">表示</button>
    <button type="button" id="clear" class="ghost">クリア</button>
  </div>
</div>

<div id="tbl"></div>
{th.THEME_BAR}
<script>
{th.THEME_JS}
(function(){{
  var params=new URLSearchParams(location.search);
  var LS='pp_wl_long_{out["market"]}';
  var codes=(params.get('codes')||localStorage.getItem(LS)||'').split(/[\\s,]+/).filter(Boolean);
  codes=Array.from(new Set(codes));
  try{{localStorage.setItem(LS,codes.join(','));}}catch(e){{}}
  var codestr=document.getElementById('codestr');
  codestr.value=codes.join(',');
  document.getElementById('copy').onclick=function(){{
    codestr.select();
    var ok=false;
    try{{ok=document.execCommand('copy');}}catch(e){{}}
    if(navigator.clipboard){{navigator.clipboard.writeText(codestr.value).then(function(){{}},function(){{}});ok=true;}}
    var st=document.getElementById('copystat');
    st.textContent=ok?'コピーしました':'手動でコピーしてください';
    setTimeout(function(){{st.textContent='';}},2500);
  }};
  document.getElementById('show').onclick=function(){{
    var v=(document.getElementById('paste').value||'').split(/[\\s,]+/).filter(Boolean);
    v=Array.from(new Set(v.map(function(x){{return x.toUpperCase();}})));
    if(!v.length)return;
    try{{localStorage.setItem(LS,v.join(','));}}catch(e){{}}
    location.href='watchlist.html?codes='+encodeURIComponent(v.join(','));
  }};
  document.getElementById('clear').onclick=function(){{
    if(!codes.length)return;
    if(!confirm('銘柄リストをすべて削除します。よろしいですか？（事前に上の文字列を控えてください）'))return;
    try{{localStorage.removeItem(LS);}}catch(e){{}}location.href='watchlist.html';
  }};
  var tbl=document.getElementById('tbl');
  if(!codes.length){{tbl.innerHTML='<div class="empty">上の欄に銘柄コードを貼り付けて「表示」を押すか、ランキングで銘柄を選んで「ウォッチリストを作成」すると、ここに一覧が出ます。</div>';return;}}
  fetch('ranking.json').then(function(r){{return r.json();}}).then(function(j){{
    var map={{}};
    (j.groups||[]).forEach(function(g){{(g.stocks||[]).forEach(function(s){{s._grade=g.grade;s._gname=g.name;map[s.code]=s;}});}});
    (j.excluded||[]).forEach(function(s){{map[s.code]=s;s._exc=true;}});
    var tt=(j.tim_tiers||[72,57,45]);
    function btcls(v){{return v>=tt[0]?'t1':v>=tt[1]?'t2':v>=tt[2]?'t3':'t4';}}
    var rows=codes.map(function(c){{
      var s=map[c];
      if(!s)return '<tr><td class="code">'+c+'</td><td colspan="7" class="sec">ランキングに見つかりません（対象外・母集団外）</td></tr>';
      var bt=(typeof s.bt==='number')?'<td class="n bt '+btcls(s.bt)+'"><b>'+s.bt.toFixed(0)+'</b></td>':'<td class="n">―</td>';
      var q=(typeof s.q==='number')?s.q.toFixed(0):(s._exc?'対象外':'―');
      var yld=(typeof s.yield==='number')?s.yield.toFixed(2)+'%':'―';
      var px=(typeof s.price==='number')?s.price.toLocaleString():'―';
      return '<tr><td class="code"><a href="reports/'+c+'.html">'+c+'</a></td>'
        +'<td>'+(s.name||'')+'</td><td class="sec">'+(s.sector||s._gname||'')+'</td>'
        +'<td class="n"><b>'+q+'</b></td>'+bt
        +'<td class="n">'+yld+'</td><td class="n">'+px+'</td>'
        +'<td>'+(s.tier||(s._exc?'―':''))+'</td></tr>';
    }}).join('');
    tbl.innerHTML='<table><thead><tr><th>コード</th><th>銘柄</th><th>業種</th>'
      +'<th class="n">品質</th><th class="n">買い時</th><th class="n">利回り</th>'
      +'<th class="n">終値</th><th>軍</th></tr></thead><tbody>'+rows+'</tbody></table>';
  }}).catch(function(){{tbl.innerHTML='<div class="empty">ranking.json を読み込めませんでした。</div>';}});
}})();
</script>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["jp", "us"])
    args = ap.parse_args()
    out, m = build(args.market)
    os.makedirs(m["out_dir"], exist_ok=True)
    new_html = render(out, m)
    open(os.path.join(m["out_dir"], "watchlist.html"), "w",
         encoding="utf-8").write(render_watchlist(out, m))

    terms_src = os.path.join(HERE, m["terms_src"])
    if os.path.isfile(terms_src):
        shutil.copyfile(terms_src, os.path.join(m["out_dir"], "terms.html"))
    guide_src = os.path.join(HERE, m["guide_src"])
    if os.path.isfile(guide_src):
        shutil.copyfile(guide_src, os.path.join(m["out_dir"], "guide.html"))
    portfolio_src = os.path.join(HERE, m["portfolio_src"])
    if os.path.isfile(portfolio_src):
        shutil.copyfile(portfolio_src, os.path.join(m["out_dir"], "portfolio.html"))

    idx = os.path.join(m["out_dir"], "index.html")
    rjson = os.path.join(m["out_dir"], "ranking.json")

    def _mask(t, ts):
        return t.replace(ts, "GENAT", 1) if ts else t
    old = open(idx, encoding="utf-8").read() if os.path.isfile(idx) else None
    prev_gen = None
    if os.path.isfile(rjson):
        try:
            prev_gen = json.load(open(rjson, encoding="utf-8")).get("generated_at")
        except Exception:
            prev_gen = None
    if old is not None and _mask(old, prev_gen) == _mask(new_html, out["generated_at"]):
        print(f"変化なし -> 書き込みスキップ  {out['counts']}")
        return

    json.dump(out, open(rjson, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(idx, "w", encoding="utf-8").write(new_html)
    print(f"-> {idx}\n-> {rjson}")


if __name__ == "__main__":
    main()
