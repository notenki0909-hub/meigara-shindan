# -*- coding: utf-8 -*-
"""
『10年保有できる優良企業』専用エンジン（日本株）。配当を評価しない。

  python analyze_long.py 7203            # トヨタを診断
  出力: out_long/<コード>_<会社名>.html

- 既存 analyze.py は**無改変**。計算部品（yfinance取得・指標構築・スコア補間・
  SVG・決算セクション）を import で流用する（analyze_us.py と同じ作り）。
- 品質スコア  = 業績0.28 / 財務0.27 / CF0.15 を再正規化（配当の持続力は使わない）
- 買い時スコア = EV/EBIT対業種・FCF利回り・PER割安度・PBR割安度 を均等25%で合成
  （buytiming_long.json）。PER割安度・PBR割安度は「自社過去レンジ位置」＋「対業種平均」の
  2サブ指標。各行はクリックで説明・判定ルール・この銘柄の位置（図解）が開く。
- 参考表示（採点しない）：自社株買い・総還元・利払い余裕度・市場が織り込むFCF成長率
  （簡易リバースDCF）・高値からの下落率・RSI/MACD 等
- 載せない：配当の持続力、1株配当チャート、減配履歴、累進配当、既存の買い時スコア

v2（2026-09-12）：銀行・保険・証券（is_simple かつ REIT でない）は専用の品質サブスコア
「金融品質」（ROE・増収率・EPS成長率・利益の安定度を均等25%で合成。
financial_quality_long.json）で対象化。買い時スコアもEV/EBIT・FCF利回りが構造的に
使えないためPER割安度・PBR割安度のみで合成する。REIT（is_reit）はFFO系の指標設計が
別途必要な規模のため引き続き対象外（summary の q_score=None）。rank_long.py が
「対象外」節に回す。
"""
import argparse
import datetime as dt
import os
import re
import sys

import analyze
import long_common as LC

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out_long")

# 既存 analyze.py の指標説明テーブルに、買い時の新指標を追記（ファイルは書き換えない）
analyze.METRIC_HELP.setdefault("ev_ebit_vs_sector", {"what":
    "企業価値（EV＝時価総額＋有利子負債−現金）が、本業の利益（EBIT＝営業利益）の何倍かを、"
    "同じ業種の中央値と比べた倍率。1.0未満なら業種平均より割安。PERと違い借金の多寡に左右され"
    "ないので、レバレッジの異なる会社を同じ土俵で比較できる。40年のバックテストで単一の割安"
    "指標として最良クラスの成績。", "unit": "倍"})
analyze.METRIC_HELP.setdefault("fcf_yield", {"what":
    "1年間に事業が生み出すフリーCF（営業CF−設備投資）が、時価総額の何％にあたるか。配当利回りの"
    "『配当なし版』で、株価に対して現金をどれだけ生んでいるかを見る。高いほど割安。利益より"
    "改ざんしにくく、40年研究で割安指標2位。負のFCF（先行投資が重い年）は中立扱い。資本集約型"
    "（インフラ・製造）は構造的に低めに出る点に注意。", "unit": "%"})
analyze.METRIC_HELP.setdefault("pbr_band_pos", {"what":
    "今のPBRが、その銘柄自身の過去数年のレンジのどこにあるか。下限（＝低PBR）に近いほど、その"
    "銘柄の物差しで割安。業種平均比（PBR対業種）と違い、常に高PBRで取引される優良企業でも"
    "『自分史比で安いか』を見られる。", "unit": ""})
# v2：銀行・保険・証券向けの品質サブスコア（業績/財務/CFの代替）
analyze.METRIC_HELP.setdefault("fq_roe", {"what":
    "自己資本利益率（ROE）。銀行・保険・証券は貸借対照表の構造上、通常の営業利益率や"
    "自己資本比率では収益性・健全性を測れないため、代わりにROEを収益性の物差しにする。", "unit": "%"})
analyze.METRIC_HELP.setdefault("fq_rev_growth", {"what":
    "売上高（銀行＝受取利息等、保険＝保険料収入、証券＝手数料・トレーディング収益）の"
    "年率成長率。事業規模が拡大しているかを見る。", "unit": "%"})
analyze.METRIC_HELP.setdefault("fq_eps_growth", {"what":
    "1株当たり利益（EPS）の年率成長率。利益成長に加え、自社株買いによる1株当たり価値の"
    "向上も反映する。", "unit": "%"})
analyze.METRIC_HELP.setdefault("fq_profit_stability", {"what":
    "直近数年の純利益について、前年比で最も大きく減益した年の下落率。0＝一度も減益して"
    "いない。金融業は市況で利益が振れやすいため、下方リスクを別途見る。", "unit": "%"})
# v2：米国REIT向けの品質・買い時サブスコア（FFOベース）
analyze.METRIC_HELP.setdefault("rq_ffo_growth", {"what":
    "FFO（Funds From Operations＝純利益＋減価償却費－不動産等売却益）の年率成長率。"
    "REITは不動産の減価償却が非現金で大きく純利益を歪めるため、業界標準のFFOで"
    "実質的な収益成長を見る。", "unit": "%"})
analyze.METRIC_HELP.setdefault("rq_rev_growth", {"what":
    "賃貸収入等、売上高の年率成長率。物件ポートフォリオが拡大しているかを見る。", "unit": "%"})
analyze.METRIC_HELP.setdefault("rq_ffo_stability", {"what":
    "直近数年のFFOについて、前年比で最も大きく減少した年の下落率。0＝一度も減少して"
    "いない。", "unit": "%"})
analyze.METRIC_HELP.setdefault("rq_interest_coverage", {"what":
    "(FFO＋支払利息)÷支払利息。REITは事業構造上、高いレバレッジ（借入依存度）が"
    "前提のため、通常の自己資本比率の代わりに利払いの余力を見る。", "unit": "倍"})
analyze.METRIC_HELP.setdefault("ffo_band_pos", {"what":
    "今のP/FFO（株価÷FFO、REIT版のPER）が、その銘柄自身の過去数年のレンジのどこに"
    "あるか。下限に近いほど自社の物差しで割安。", "unit": ""})
analyze.METRIC_HELP.setdefault("ffo_vs_sector", {"what":
    "現在のP/FFOを、S&P500のREIT業種平均のP/FFOと比べた倍率。1.0未満なら業種平均"
    "より安い。", "unit": "倍"})

_SEC_AVG_LONG = {"jp": None, "us": None}


def _sector_avg_long(market="jp"):
    """sector_averages_long{,_us}.json（業種中央値 EV/EBIT）。無ければ空。"""
    if _SEC_AVG_LONG[market] is None:
        fn = "sector_averages_long.json" if market == "jp" else "sector_averages_long_us.json"
        p = os.path.join(HERE, fn)
        try:
            _SEC_AVG_LONG[market] = analyze.load_json(fn) if os.path.isfile(p) else {}
        except Exception:
            _SEC_AVG_LONG[market] = {}
    return _SEC_AVG_LONG[market]


_PBR_REL_LONG = {"jp": None, "us": None}


def _pbr_unreliable_sectors(market="jp"):
    """pbr_reliability_long{,_us}.json（calib_long.py が生成）。
    自社株買い等で自己資本が構造的に振れ、PBRの過去レンジ自体が非定常になりやすい業種の集合。
    この集合に含まれる業種は買い時スコアからPBR割安度を除外する（_buytiming 参照）。無ければ空集合＝全業種PBRを使う。"""
    if _PBR_REL_LONG[market] is None:
        fn = "pbr_reliability_long.json" if market == "jp" else "pbr_reliability_long_us.json"
        p = os.path.join(HERE, fn)
        try:
            data = analyze.load_json(fn) if os.path.isfile(p) else {}
            _PBR_REL_LONG[market] = set(data.get("unreliable_sectors") or [])
        except Exception:
            _PBR_REL_LONG[market] = set()
    return _PBR_REL_LONG[market]


# ---------------------------------------------------------------- 生データ抽出
def _raw_valuation(yd):
    isr, bsr, cfr = yd["is_rows"], yd["bs_rows"], yd["cf_rows"]
    info = yd.get("info") or {}
    opi = analyze.row(isr, "Operating Income", "Total Operating Income As Reported", "EBIT")
    tdebt = analyze.row(bsr, "Total Debt")
    cash = analyze.row(bsr, "Cash And Cash Equivalents",
                       "Cash Cash Equivalents And Short Term Investments")
    fcf = analyze.row(cfr, "Free Cash Flow")
    ocf = analyze.row(cfr, "Operating Cash Flow")
    capex = analyze.row(cfr, "Capital Expenditure")
    f = lambda s: (s[0] if s and LC.is_num(s[0]) else None)
    fcf0 = f(fcf)
    if fcf0 is None and LC.is_num(f(ocf)) and LC.is_num(f(capex)):
        fcf0 = f(ocf) + f(capex)
    return {
        "mcap": info.get("marketCap"),
        "ebit": f(opi),
        "total_debt": f(tdebt),
        "cash": f(cash),
        "fcf": fcf0,
        "fcf_series": [x for x in (fcf or []) if LC.is_num(x)],
        "eq_series": analyze.row(bsr, "Stockholders Equity", "Common Stock Equity",
                                 "Total Equity Gross Minority Interest"),
        "shares": info.get("sharesOutstanding"),
        "per": (info.get("trailingPE") if LC.is_num(info.get("trailingPE"))
                and info.get("trailingPE") > 0 else None),
        "pbr": (info.get("priceToBook") if LC.is_num(info.get("priceToBook"))
                and info.get("priceToBook") > 0 else None),
    }


def _raw_financial_quality(yd, info):
    """銀行・保険・証券（is_simple かつ REIT でない）向けの品質サブスコアの素材。
    ROE・増収率(売上高CAGR)・EPS成長率(CAGR)・利益の安定度(純利益の最大減益率)。"""
    isr = yd.get("is_rows") or {}
    rev = analyze.row(isr, "Total Revenue") or []
    ni = analyze.row(isr, "Net Income") or []
    eps = analyze.row(isr, "Diluted EPS", "Basic EPS") or []
    roe = info.get("returnOnEquity")
    return {
        "roe": (roe * 100.0 if LC.is_num(roe) else None),
        "rev_growth": analyze.cagr_of(rev),
        "eps_growth": analyze.cagr_of(eps),
        "profit_stability": LC.worst_yoy_decline_pct(ni),
        "rev_series": rev, "eps_series": eps, "ni_series": ni,
    }


def _raw_reit_quality(yd, info):
    """米国REIT向けの品質・買い時サブスコアの素材（analyze_long.py内でのみ完結。
    analyze_us.py本体は無改変）。FFO(近似) = 純利益 + 減価償却費 − 不動産等売却益
    （NAREIT定義の簡易近似。各社公表FFOとは非支配持分等の調整分だけ厳密には一致しない）。"""
    isr = yd.get("is_rows") or {}
    ni = analyze.row(isr, "Net Income") or []
    da = analyze.row(isr, "Reconciled Depreciation",
                     "Depreciation And Amortization In Income Statement") or []
    gain = analyze.row(isr, "Gain On Sale Of Business", "Gain On Sale Of Security") or []
    rev = analyze.row(isr, "Total Revenue") or []
    ie = analyze.row(isr, "Interest Expense", "Interest Expense Non Operating") or []

    n = max(len(ni), len(da), len(gain))
    ffo = []
    for i in range(n):
        nv = ni[i] if i < len(ni) else None
        dv = da[i] if i < len(da) else None
        gv = gain[i] if i < len(gain) else None
        ffo.append(nv + dv - gv if (LC.is_num(nv) and LC.is_num(dv) and LC.is_num(gv)) else
                   nv + dv if (LC.is_num(nv) and LC.is_num(dv)) else None)
    ffo0 = ffo[0] if ffo and LC.is_num(ffo[0]) else None
    ie0 = ie[0] if ie and LC.is_num(ie[0]) and ie[0] > 0 else None
    cov = ((ffo0 + ie0) / ie0) if (LC.is_num(ffo0) and LC.is_num(ie0)) else None
    return {
        "ffo_series": ffo, "ffo0": ffo0,
        "ffo_growth": analyze.cagr_of(ffo),
        "rev_growth": analyze.cagr_of(rev),
        "ffo_stability": LC.worst_yoy_decline_pct(ffo),
        "interest_coverage": cov,
        "shares": info.get("sharesOutstanding"),
    }


def _ffo_pairs(yd, ffo_series, shares):
    """過去P/FFO ≈ 暦年平均株価 ÷（その年のFFO ÷ 現在の発行株数）。-> [(year, p_ffo)]
    _pbr_pairs と同じ簡易近似（過去の発行株数は追わず現在株数で代用）。"""
    pm = analyze.yearly_price_mean(yd["hist_m"])
    is_years = yd.get("is_years", []) or []
    if not (ffo_series and shares and len(is_years) == len(ffo_series)):
        return []
    ffo_ps = {y: (f / shares) for y, f in zip(is_years, ffo_series) if LC.is_num(f) and f > 0}
    out = []
    for y in sorted(pm):
        if y == analyze.TODAY.year or y not in ffo_ps or pm[y] <= 0:
            continue
        r = pm[y] / ffo_ps[y]
        if 0 < r < 60:
            out.append((y, r))
    return out


def _pbr_pairs(yd, raw):
    """過去PBR ≈ 暦年平均株価 ÷ （その年の自己資本 ÷ 現在の発行株数）。-> [(year, pbr)]"""
    pm = analyze.yearly_price_mean(yd["hist_m"])
    eq, shares = raw["eq_series"], raw["shares"]
    bs_years = yd.get("bs_years", []) or []
    if not (eq and shares and len(bs_years) == len(eq)):
        return []
    bvps = {y: (e / shares) for y, e in zip(bs_years, eq) if LC.is_num(e) and e > 0}
    out = []
    for y in sorted(pm):
        if y == analyze.TODAY.year or y not in bvps or pm[y] <= 0:
            continue
        r = pm[y] / bvps[y]
        if 0 < r < 20:
            out.append((y, r))
    return out


# ---------------------------------------------------------------- 図解（履歴なし指標用の簡易ゲージ）
def _gauge_svg(value, good, warn, direction, kind, title):
    """good/warn の2閾値に対して value がどこにいるかを1本帯で示す。"""
    if not LC.is_num(value):
        return ""
    fmt = (lambda x: f"{x:.2f}%") if kind == "pct" else (lambda x: f"{x:.1f}倍")
    lo = min(value, good, warn)
    hi = max(value, good, warn)
    span = (hi - lo) or 1.0
    lo -= span * 0.15
    hi += span * 0.15
    span = hi - lo
    W, H, LX, RX, TP = 476, 64, 46, 336, 16
    x = lambda v: LX + (v - lo) / span * (RX - LX)
    if direction == "higher_better":
        zones = [(lo, warn, "var(--lo)", "割高"), (warn, good, "var(--na)", "妥当"),
                 (good, hi, "var(--hi)", "割安")]
    else:
        zones = [(lo, good, "var(--hi)", "割安"), (good, warn, "var(--na)", "妥当"),
                 (warn, hi, "var(--lo)", "割高")]
    P = []
    for a, b, col, lab in zones:
        P.append(f'<rect x="{x(a):.0f}" y="{TP}" width="{max(1,x(b)-x(a)):.0f}" height="14" '
                 f'fill="{col}" opacity="0.18"/>')
        P.append(f'<text x="{(x(a)+x(b))/2:.0f}" y="{TP+26}" class="cx" '
                 f'style="text-anchor:middle">{lab}</text>')
    for v, lab in ((warn, "warn"), (good, "good")):
        P.append(f'<line x1="{x(v):.0f}" y1="{TP-3}" x2="{x(v):.0f}" y2="{TP+17}" '
                 f'stroke="var(--muted)" stroke-width="1" stroke-dasharray="2 2"/>')
    P.append(f'<path d="M {x(value):.0f} {TP-8} L {x(value)-5:.0f} {TP-16} L {x(value)+5:.0f} {TP-16} Z" '
             f'fill="var(--fg)"/>')
    P.append(f'<text x="{x(value):.0f}" y="{TP-19}" class="ct" style="text-anchor:middle">'
             f'現在 {fmt(value)}</text>')
    P.append(f'<text x="{LX}" y="{H-4}" class="cm">閾値：割安 {fmt(good)}／割高 {fmt(warn)}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" class="chart trend" role="img" aria-label="{title}">'
            + "".join(P) + "</svg>")


# ---------------------------------------------------------------- 買い時スコア
def _score(key, v):
    return LC.score_val(key, v, LC.load_bt_cfg()["rules"])


def _buytiming(raw, rowmap, seckey, market, pbr_pairs, is_fin_simple=False,
               is_reit=False, reit_q=None, ffo_pairs=None):
    bt = LC.load_bt_cfg()
    rules = bt["rules"]
    sec = _sector_avg_long(market).get(seckey, {}) if isinstance(_sector_avg_long(market), dict) else {}

    if is_reit:
        # REIT：不動産の減価償却でEBIT/純利益(PER)/簿価(PBR)が歪み、「FCF」も取得・
        # 開発投資で構造的に振れるため、いずれも使わずP/FFO割安度のみで判定する。
        r_rules = LC.load_reit_cfg()["rules"]
        ffo0 = reit_q.get("ffo0") if reit_q else None
        current_pfo = (raw["mcap"] / ffo0) if (LC.is_num(ffo0) and ffo0 > 0
                                               and LC.is_num(raw.get("mcap"))) else None
        ffo_band = (LC.price_band_pos(ffo_pairs, current_pfo, low_is_cheap=True)
                   if ffo_pairs and len(ffo_pairs) >= 3 else None)
        sec_pfo = sec.get("pfo")
        ffo_vs = (current_pfo / sec_pfo) if (LC.is_num(current_pfo) and LC.is_num(sec_pfo)
                                             and sec_pfo > 0) else None
        ffo_cheap_sc, _ = LC.composite(["ffo_band_pos", "ffo_vs_sector"],
                                       {"ffo_band_pos": ffo_band, "ffo_vs_sector": ffo_vs}, r_rules)
        comp = {"ffo_cheap": ffo_cheap_sc}
        total, scored, poss, cov = LC.buytiming_score(comp, weights={"ffo_cheap": 1.0})
        d = {
            "is_reit": True, "is_fin_simple": False, "pbr_unreliable": False,
            "current_pfo": current_pfo, "sec_pfo": sec_pfo, "ffo_vs": ffo_vs,
            "ffo_band": ffo_band, "ffo_cheap": ffo_cheap_sc, "ffo_pairs": ffo_pairs or [],
            "ffo_band_score": LC.score_val("ffo_band_pos", ffo_band, r_rules),
            "ffo_vs_score": LC.score_val("ffo_vs_sector", ffo_vs, r_rules),
            "ev_ebit": None, "ev_median": None, "ev_vs": None, "ev_score": None,
            "fcf_yield": None, "fcf_score": None,
            "per_band": None, "per_vs": None, "per_score": None, "per_band_disp": None,
            "per_vs_disp": None, "per_band_score": None, "per_vs_score": None, "per_rb": None,
            "pbr_band": None, "pbr_vs": None, "pbr_score": None, "pbr_vs_disp": None,
            "pbr_band_score": None, "pbr_vs_score": None, "pbr": None, "pbr_pairs": [],
        }
        return total, (scored, poss, cov), comp, d

    if is_fin_simple:
        # 銀行・保険・証券：EBITの概念が成立せず、「FCF」もバランスシートの
        # 資金繰り（貸出増減等）で数兆円単位に振れ株価の割安度と無関係なため、
        # 両方とも算出しない（中立フィルではなく非対象＝算出不可として扱う）。
        ev_ebit = ev_vs = ev_score = None
        fy = fy_score = None
    else:
        ev_ebit = LC.ev_over_ebit(raw["mcap"], raw["total_debt"], raw["cash"], raw["ebit"])
        med = sec.get("ev_ebit")
        ev_vs = (ev_ebit / med) if (LC.is_num(ev_ebit) and LC.is_num(med) and med > 0) else None
        ev_score = _score("ev_ebit_vs_sector", ev_vs)

        fy = LC.fcf_yield_pct(raw["fcf"], raw["mcap"])
        fy_score = _score("fcf_yield", fy if (LC.is_num(fy) and fy > 0) else
                          (0.0 if LC.is_num(fy) else None))
    med = sec.get("ev_ebit")

    per_band = rowmap.get("per_band_pos", {}).get("v")
    per_vs = rowmap.get("per_vs_sector", {}).get("v")
    per_sc, _ = LC.composite(["per_band_pos", "per_vs_sector"],
                             {"per_band_pos": per_band, "per_vs_sector": per_vs}, rules)

    pbr_band = LC.price_band_pos(pbr_pairs, raw["pbr"], low_is_cheap=True) if len(pbr_pairs) >= 3 else None
    pbr_vs = rowmap.get("pbr_vs_sector", {}).get("v")
    pbr_sc, _ = LC.composite(["pbr_band_pos", "pbr_vs_sector"],
                             {"pbr_band_pos": pbr_band, "pbr_vs_sector": pbr_vs}, rules)

    comp = {"ev_ebit_vs_sector": ev_score, "fcf_yield": fy_score,
            "per_cheap": per_sc, "pbr_cheap": pbr_sc}
    pbr_unreliable = seckey in _pbr_unreliable_sectors(market)
    if is_fin_simple and pbr_unreliable:
        weights = {"per_cheap": 1.0}
    elif is_fin_simple:
        weights = {"per_cheap": 0.5, "pbr_cheap": 0.5}
    elif pbr_unreliable:
        weights = {"ev_ebit_vs_sector": 1 / 3, "fcf_yield": 1 / 3, "per_cheap": 1 / 3}
    else:
        weights = None
    total, scored, poss, cov = LC.buytiming_score(comp, weights=weights)
    d = {
        "pbr_unreliable": pbr_unreliable, "is_fin_simple": is_fin_simple,
        "ev_ebit": ev_ebit, "ev_median": med, "ev_vs": ev_vs, "ev_score": ev_score,
        "fcf_yield": fy, "fcf_score": fy_score,
        "per_band": per_band, "per_vs": per_vs, "per_score": per_sc,
        "per_band_disp": rowmap.get("per_band_pos", {}).get("disp"),   # 「過去27〜37倍／現在42.9倍＝割安度0/100」
        "per_vs_disp": rowmap.get("per_vs_sector", {}).get("disp"),     # 生PER「42.9倍」
        "per_band_score": LC.score_val("per_band_pos", per_band, rules),
        "per_vs_score": LC.score_val("per_vs_sector", per_vs, rules),
        "per_rb": rowmap.get("per_band_pos", {}).get("rangeband"),
        "pbr_band": pbr_band, "pbr_vs": pbr_vs, "pbr_score": pbr_sc,
        "pbr_vs_disp": rowmap.get("pbr_vs_sector", {}).get("disp"),     # 生PBR「7.52倍」
        "pbr_band_score": LC.score_val("pbr_band_pos", pbr_band, rules),
        "pbr_vs_score": LC.score_val("pbr_vs_sector", pbr_vs, rules),
        "pbr": raw["pbr"], "pbr_pairs": pbr_pairs,
    }
    return total, (scored, poss, cov), comp, d


# ---------------------------------------------------------------- レンダリング
def _f(v, d=1, suf=""):
    return (f"{v:.{d}f}{suf}" if LC.is_num(v) else "―")


def _sc_span(score):
    if not LC.is_num(score):
        return '<span class="sc na">―（不採点）</span>'
    cls = "hi" if score >= 80 else "mid" if score >= 62 else "lo"
    return f'<span class="sc {cls}">{score:.0f} / 110</span>'


def _detail_row(key, name, value_disp, rule_txt, why_txt, score, figure="", subrows=None):
    """買い時の1指標。クリックで 見るポイント／判定ルール／この銘柄／図解 が開く。"""
    what = analyze.METRIC_HELP.get(key, {}).get("what", "")
    sub = ""
    if subrows:
        sub = ('<table class="subt"><thead><tr><th>内訳</th><th class="n">値</th>'
               '<th class="n">点</th></tr></thead><tbody>'
               + "".join(f'<tr><td>{k}</td><td class="n">{v}</td>'
                         f'<td class="n">{_sc_span(s)}</td></tr>' for k, v, s in subrows)
               + "</tbody></table>")
    body = (f'<div class="mbody">'
            + (f'<p class="what"><b>見るポイント：</b>{what}</p>' if what else "")
            + (f'<div class="figwrap">{figure}</div>' if figure else "")
            + sub
            + (f'<p class="rule"><b>判定ルール：</b>{rule_txt}</p>' if rule_txt else "")
            + (f'<p class="why"><b>この銘柄：</b>{why_txt}</p>' if why_txt else "")
            + f'<p class="rule">この指標の点：<b>{"" if not LC.is_num(score) else f"{score:.0f} / 110"}</b>'
              f'{"（グループ＝この点の平均）" if subrows else ""}</p>'
            + '</div>')
    return ('<details class="m"><summary>'
            f'<span class="mn">{name}</span>'
            f'<span class="mv">{value_disp}</span>'
            f'<span class="mr">クリックで図解・判定ルール</span>'
            f'<span class="mt">{_sc_span(score)}</span></summary>'
            f'{body}</details>')


def render_long_html(meta, groups, detail, q_score, q_cov, bt_score, bt_cov, btd,
                     ctx, extras, warnings, rules, market="jp"):
    seckey = meta.get("jp_sector") or meta.get("gics_sector") or ""
    is_simple = meta["is_simple"]

    if market == "jp":
        mdh = lambda it: analyze._metric_details_html(it, seckey, is_simple, rules)
        svg_price = analyze.svg_price
        earn_html = analyze.render_earn_html(ctx.get("earn") or {})
        comp_html = analyze.render_company_html(ctx.get("company") or {})
        unit = "円"
    else:
        import analyze_us
        gk = analyze_us.gics_jp(seckey)
        mdh = lambda it: analyze_us._metric_details_html_us(it, seckey, gk, is_simple, rules)
        svg_price = analyze_us.svg_price_us
        earn_html = analyze_us._earn_block_us(ctx.get("earn") or {})
        comp_html = analyze_us.render_company_html_us(ctx.get("company") or {})
        unit = "＄"

    # 品質の指標
    is_fin_simple = bool(is_simple and not meta.get("is_reit"))
    if is_fin_simple and extras.get("fin_q") and extras.get("fq_parts"):
        fq, fqp = extras["fin_q"], extras["fq_parts"]
        fq_rules = LC.load_fq_cfg()["rules"]

        def _fq_why(key, v, lab):
            if not LC.is_num(v):
                return "データを取得できませんでした。"
            r = fq_rules[key]
            good, warn = r["good"], r["warn"]
            w = "良好" if v >= good else "弱い" if v < warn else "やや弱い"
            return f"{lab} {v:.1f}%（{w}）。"

        def _fq_gauge(key, v, title):
            r = fq_rules[key]
            return _gauge_svg(v, r["good"], r["warn"], "higher_better", "pct", title) if LC.is_num(v) else ""

        q_blocks = [
            f'<div class="domhead"><b>金融品質（銀行・保険・証券向け代替指標）</b> '
            f'{analyze.bar(groups.get("金融品質"))}</div>',
            _detail_row("fq_roe", "ROE（自己資本利益率）",
                       f"{fq['roe']:.1f}%" if LC.is_num(fq['roe']) else "算出不可",
                       "10%以上＝良好 ／ 5%未満＝弱い。自己資本に対する利益創出力。",
                       _fq_why("roe", fq['roe'], "ROE"),
                       fqp["roe"][1], figure=_fq_gauge("roe", fq['roe'], "ROE")),
            _detail_row("fq_rev_growth", "増収率（売上高の年率成長）",
                       f"{fq['rev_growth']:.1f}%" if LC.is_num(fq['rev_growth']) else "算出不可（データ不足）",
                       "6%以上＝良好 ／ 0%未満（減収）＝弱い。",
                       _fq_why("rev_growth", fq['rev_growth'], "増収率"),
                       fqp["rev_growth"][1], figure=_fq_gauge("rev_growth", fq['rev_growth'], "増収率")),
            _detail_row("fq_eps_growth", "EPS成長率（1株利益の年率成長）",
                       f"{fq['eps_growth']:.1f}%" if LC.is_num(fq['eps_growth']) else "算出不可（データ不足）",
                       "8%以上＝良好 ／ 0%未満（減益）＝弱い。自社株買いの効果を含む。",
                       _fq_why("eps_growth", fq['eps_growth'], "EPS成長率"),
                       fqp["eps_growth"][1], figure=_fq_gauge("eps_growth", fq['eps_growth'], "EPS成長率")),
            _detail_row("fq_profit_stability", "利益の安定度（純利益の最大減益率）",
                       f"{fq['profit_stability']:.1f}%" if LC.is_num(fq['profit_stability']) else "算出不可（データ不足）",
                       "0%（減益なし）＝良好 ／ -30%（最大30%の減益年）＝弱い。",
                       _fq_why("profit_stability", fq['profit_stability'], "最大減益率"),
                       fqp["profit_stability"][1],
                       figure=_fq_gauge("profit_stability", fq['profit_stability'], "利益の安定度")),
        ]
    elif extras.get("reit_q") and extras.get("reit_parts"):
        rq, rqp = extras["reit_q"], extras["reit_parts"]
        rq_rules = LC.load_reit_cfg()["rules"]

        def _rq_why(key, v, lab, unit="%"):
            if not LC.is_num(v):
                return "データを取得できませんでした。"
            r = rq_rules[key]
            good, warn = r["good"], r["warn"]
            w = "良好" if v >= good else "弱い" if v < warn else "やや弱い"
            return f"{lab} {v:.1f}{unit}（{w}）。"

        def _rq_gauge(key, v, title, kind="pct"):
            r = rq_rules[key]
            return _gauge_svg(v, r["good"], r["warn"], "higher_better", kind, title) if LC.is_num(v) else ""

        q_blocks = [
            f'<div class="domhead"><b>REIT品質（FFOベースの代替指標）</b> '
            f'{analyze.bar(groups.get("REIT品質"))}</div>',
            _detail_row("rq_ffo_growth", "FFO成長率（FFOの年率成長）",
                       f"{rq['ffo_growth']:.1f}%" if LC.is_num(rq['ffo_growth']) else "算出不可（データ不足）",
                       "6%以上＝良好 ／ 0%未満（減少）＝弱い。",
                       _rq_why("ffo_growth", rq['ffo_growth'], "FFO成長率"),
                       rqp["ffo_growth"][1], figure=_rq_gauge("ffo_growth", rq['ffo_growth'], "FFO成長率")),
            _detail_row("rq_rev_growth", "増収率（賃貸収入等の年率成長）",
                       f"{rq['rev_growth']:.1f}%" if LC.is_num(rq['rev_growth']) else "算出不可（データ不足）",
                       "5%以上＝良好 ／ 0%未満（減収）＝弱い。",
                       _rq_why("rev_growth", rq['rev_growth'], "増収率"),
                       rqp["rev_growth"][1], figure=_rq_gauge("rev_growth", rq['rev_growth'], "増収率")),
            _detail_row("rq_ffo_stability", "FFOの安定度（最大減少率）",
                       f"{rq['ffo_stability']:.1f}%" if LC.is_num(rq['ffo_stability']) else "算出不可（データ不足）",
                       "0%（減少なし）＝良好 ／ -20%（最大20%の減少年）＝弱い。",
                       _rq_why("ffo_stability", rq['ffo_stability'], "最大減少率"),
                       rqp["ffo_stability"][1], figure=_rq_gauge("ffo_stability", rq['ffo_stability'], "FFOの安定度")),
            _detail_row("rq_interest_coverage", "利払い余裕度（(FFO＋支払利息)÷支払利息）",
                       f"{rq['interest_coverage']:.2f}倍" if LC.is_num(rq['interest_coverage']) else "算出不可（データ不足）",
                       "4.0倍以上＝良好 ／ 2.0倍未満＝弱い。REITは高レバレッジが前提のため利払い余力を見る。",
                       _rq_why("interest_coverage", rq['interest_coverage'], "利払い余裕度", unit="倍"),
                       rqp["interest_coverage"][1],
                       figure=_rq_gauge("interest_coverage", rq['interest_coverage'], "利払い余裕度", kind="mul")),
        ]
    else:
        q_blocks = []
        for gname in ("業績", "財務", "キャッシュフロー"):
            q_blocks.append(f'<div class="domhead"><b>{gname}</b> {analyze.bar(groups.get(gname))}</div>')
            rows = [r for r in (detail.get(gname) or []) if r.get("key")]
            q_blocks += ([mdh(r) for r in rows] if rows else
                         ['<div class="plain"><span class="mn">―</span>'
                          '<span class="mv2">この業種では評価対象外</span></div>'])
    q_html = "".join(q_blocks)

    # 買い時の指標（6行）
    r = LC.load_bt_cfg()["rules"]
    ev_fig = _gauge_svg(btd["ev_vs"], r["ev_ebit_vs_sector"]["good"], r["ev_ebit_vs_sector"]["warn"],
                        "lower_better", "mul", "EV/EBIT 対業種") if LC.is_num(btd["ev_vs"]) else ""
    fcf_fig = _gauge_svg(btd["fcf_yield"], r["fcf_yield"]["good"], r["fcf_yield"]["warn"],
                         "higher_better", "pct", "FCF利回り") if LC.is_num(btd["fcf_yield"]) else ""
    per_rb_fig = analyze.svg_rangeband(btd["per_rb"], "PERの自社過去レンジ") if btd.get("per_rb") else ""
    pbr_rb_fig = analyze.svg_rangeband(
        {"hist": btd["pbr_pairs"], "current": btd["pbr"], "kind": "per", "low_is_cheap": True},
        "PBRの自社過去レンジ") if len(btd["pbr_pairs"]) >= 3 and LC.is_num(btd["pbr"]) else ""

    def _why_vs(vs, kind):
        if not LC.is_num(vs):
            return "業種平均が取得できず判定できません。"
        w = "割安" if vs < 0.95 else "ほぼ妥当" if vs <= 1.2 else "割高"
        return f"業種平均の {vs:.2f} 倍＝{w}。"

    def _why_band(pos, rb, kind="per"):
        if not LC.is_num(pos):
            return "過去レンジを引くだけの履歴がありません。"
        u = "%" if kind == "pct" else "倍"
        cur = rb.get("current") if rb else None
        vals = [v for _, v in (rb.get("hist") or [])] if rb else []
        lo, hi = (min(vals), max(vals)) if vals else (None, None)
        head = ""
        if LC.is_num(cur) and LC.is_num(lo):
            if cur > hi:
                head = (f"現在 {cur:.1f}{u} は過去{len(vals)}年のレンジ（{lo:.1f}〜{hi:.1f}{u}）を"
                        f"上回る過去最高水準。グラフでは点線＋▶が現在値（上端の外側）、折れ線が年次実績。")
            elif cur < lo:
                head = (f"現在 {cur:.1f}{u} は過去{len(vals)}年のレンジ（{lo:.1f}〜{hi:.1f}{u}）を"
                        f"下回る過去最安水準。")
            else:
                head = f"現在 {cur:.1f}{u}（過去{len(vals)}年 {lo:.1f}〜{hi:.1f}{u} の範囲内）。"
        w = "下端寄り＝自社史比で割安" if pos >= 0.6 else "中ほど" if pos >= 0.2 else "上端寄り＝自社史比で割高"
        return f"{head}過去レンジ内の位置は割安度 {pos*100:.0f}/100（{w}）。"

    _fin_note_ev = "銀行・保険・証券はEBITの概念が成立しないため、この指標は使いません（買い時スコアはPER・PBRのみで合成）。"
    _fin_note_fcf = "銀行・保険・証券は「FCF」がバランスシートの資金繰りで大きく振れ株価の割安度と無関係なため、この指標は使いません（買い時スコアはPER・PBRのみで合成）。"
    bt_ev = [_detail_row(
        "ev_ebit_vs_sector", "EV/EBIT（対業種中央値）",
        ("対象外（銀行・保険・証券）" if btd.get("is_fin_simple") else
         f"{_f(btd['ev_ebit'],1)}倍 ／ 業種中央値 {_f(btd['ev_median'],1)}倍（対業種 {_f(btd['ev_vs'],2)}倍）"
         if LC.is_num(btd["ev_ebit"]) else "算出不可（EBIT≤0 等）"),
        "対業種 0.85倍以下＝割安 ／ 1.2倍超＝割高。EV＝時価総額＋有利子負債−現金、EBIT＝営業利益。",
        (_fin_note_ev if btd.get("is_fin_simple") else
         _why_vs(btd["ev_vs"], "mul") if LC.is_num(btd["ev_vs"]) else
         "この業種の EV/EBIT 中央値が未算出のため中立（60点）扱いです。"),
        btd["ev_score"], figure=ev_fig)]
    bt_fcf = [_detail_row(
        "fcf_yield", "FCF利回り（FCF÷時価総額）",
        ("対象外（銀行・保険・証券）" if btd.get("is_fin_simple") else
         f"{_f(btd['fcf_yield'],2)}%" if LC.is_num(btd["fcf_yield"]) else "算出不可"),
        "6%以上＝割安 ／ 3%未満＝割高。負のFCF（先行投資の重い年）は中立扱い。資本集約業種は構造的に低め。",
        (_fin_note_fcf if btd.get("is_fin_simple") else
         "負のFCFのため中立扱い。" if LC.is_num(btd["fcf_yield"]) and btd["fcf_yield"] <= 0 else
         f"時価総額に対して年 {_f(btd['fcf_yield'],2)}% の現金を生んでいます。" if LC.is_num(btd["fcf_yield"]) else
         "FCFまたは時価総額が取得できませんでした。"),
        btd["fcf_score"], figure=fcf_fig)]
    bt_per = [
        _detail_row(
            "per_band_pos", "PER 自社過去レンジ内の位置",
            btd.get("per_band_disp") or "履歴不足で算出不可",
            "0＝レンジ上端（高PER＝割高）／100＝下端（低PER＝割安）。自社の物差しで割安か。"
            "グラフの折れ線＝年次の実績PER、点線＋▶＝現在値（レンジを外れる年もある）。",
            _why_band(btd["per_band"], btd.get("per_rb"), "per"),
            btd.get("per_band_score"), figure=per_rb_fig),
        _detail_row(
            "per_vs_sector", "PER 対業種平均", btd.get("per_vs_disp") or "―",
            "1.0未満＝業種平均より安い。0.95以下で割安・1.2超で割高。業種をまたいだ比較はしない。",
            _why_vs(btd["per_vs"], "mul"), btd.get("per_vs_score")),
    ]
    bt_pbr = [
        _detail_row(
            "pbr_band_pos", "PBR 自社過去レンジ内の位置",
            (f"割安度 {btd['pbr_band']*100:.0f}/100" if LC.is_num(btd["pbr_band"]) else "履歴不足で算出不可"),
            "0＝レンジ上端（高PBR＝割高）／100＝下端（低PBR＝割安）。常に高PBRの優良企業でも自社史比で見られる。"
            "グラフの折れ線＝年次の実績PBR、点線＋▶＝現在値。",
            _why_band(btd["pbr_band"],
                      {"hist": btd["pbr_pairs"], "current": btd["pbr"]}, "per"),
            btd.get("pbr_band_score"), figure=pbr_rb_fig),
        _detail_row(
            "pbr_vs_sector", "PBR 対業種平均", btd.get("pbr_vs_disp") or "―",
            "1.0未満＝業種平均より安い。1.0以下で割安・1.4超で割高。1倍割れは東証改革の是正テーマ。",
            _why_vs(btd["pbr_vs"], "mul"), btd.get("pbr_vs_score")),
    ]
    _pc = f"{btd['per_score']:.0f}" if LC.is_num(btd.get("per_score")) else "―"
    _bc = f"{btd['pbr_score']:.0f}" if LC.is_num(btd.get("pbr_score")) else "―"
    _is_fin = bool(btd.get("is_fin_simple"))
    if _is_fin and btd.get("pbr_unreliable"):
        bt_note = ('<p class="sub">買い時スコア＝<b>PER割安度</b>のみ（100%）で合成。'
                   f'<b>PER割安度＝「PER 自社レンジ」と「PER 対業種」の平均＝{_pc}</b>。'
                   '銀行・保険・証券はEV/EBIT・FCF利回りが使えず、さらにこの業種はPBRの'
                   '過去レンジも自社株買い等で非定常なため、PER割安度のみで判定しています。'
                   '配当利回り・増配・累進配当宣言は一切使っていません。</p>')
    elif _is_fin:
        bt_note = ('<p class="sub">買い時スコア＝<b>PER割安度・PBR割安度</b>を均等50%で合成。'
                   f'<b>PER割安度＝「PER 自社レンジ」と「PER 対業種」の平均＝{_pc}</b>、'
                   f'<b>PBR割安度＝同様に{_bc}</b>。銀行・保険・証券はEBITの概念が成立せず、'
                   '「FCF」もバランスシートの資金繰りで株価の割安度と無関係に振れるため、'
                   'EV/EBIT・FCF利回りは使わずPER・PBRのみで判定しています。'
                   '配当利回り・増配・累進配当宣言は一切使っていません。</p>')
    elif btd.get("pbr_unreliable"):
        bt_note = ('<p class="sub">買い時スコア＝<b>EV/EBIT対業種・FCF利回り・PER割安度</b>を'
                   '均等33%で合成（欠損は中立60）。各行の点はその指標単体の点。'
                   f'<b>PER割安度＝「PER 自社レンジ」と「PER 対業種」の平均＝{_pc}</b>。'
                   'この業種は自社株買い等で自己資本が構造的に変動しやすく、'
                   '「PBRが過去レンジの下限に近い＝底値」という前提が成り立ちにくいため、'
                   f'買い時スコアからPBR割安度（参考値{_bc}）を除外しています。'
                   '配当利回り・増配・累進配当宣言は一切使っていません。</p>')
    else:
        bt_note = ('<p class="sub">買い時スコア＝<b>EV/EBIT対業種・FCF利回り・PER割安度・PBR割安度</b>を'
                   '均等25%で合成（欠損は中立60）。各行の点はその指標単体の点。'
                   f'<b>PER割安度＝「PER 自社レンジ」と「PER 対業種」の平均＝{_pc}</b>、'
                   f'<b>PBR割安度＝同様に{_bc}</b>。配当利回り・増配・累進配当宣言は一切使っていません。</p>')
    _ev_fcf_head_note = "（対象外・銀行/保険/証券）" if _is_fin else ""
    _pbr_head_note = "（参考・買い時スコアには不使用）" if btd.get("pbr_unreliable") else ""
    bt_blocks = [
        f'<div class="domhead"><b>EV/EBIT（対業種）{_ev_fcf_head_note}</b> {analyze.bar(btd.get("ev_score"))}</div>' + "".join(bt_ev),
        f'<div class="domhead"><b>FCF利回り{_ev_fcf_head_note}</b> {analyze.bar(btd.get("fcf_score"))}</div>' + "".join(bt_fcf),
        f'<div class="domhead"><b>PER割安度</b> {analyze.bar(btd.get("per_score"))}</div>' + "".join(bt_per),
        f'<div class="domhead"><b>PBR割安度{_pbr_head_note}</b> {analyze.bar(btd.get("pbr_score"))}</div>' + "".join(bt_pbr),
    ]
    bt_html = bt_note + "".join(bt_blocks)

    if btd.get("is_reit"):
        # REIT：上のEV/EBIT・FCF・PER・PBRの行は算出不可のプレースホルダなので使わず、
        # P/FFO割安度のみの専用セクションで完全に置き換える。
        _fpc = f"{btd['ffo_cheap']:.0f}" if LC.is_num(btd.get("ffo_cheap")) else "―"
        bt_note = ('<p class="sub">買い時スコア＝<b>P/FFO割安度</b>のみ（100%）で合成。'
                   f'<b>P/FFO割安度＝「P/FFO 自社レンジ」と「P/FFO 対業種」の平均＝{_fpc}</b>。'
                   'REITは不動産の減価償却でPER・PBR・EV/EBIT・FCF利回りが軒並み歪むため、'
                   '業界標準のFFO（純利益＋減価償却－不動産等売却益）ベースの指標のみで'
                   '判定しています。配当利回り・増配・累進配当宣言は一切使っていません。</p>')
        ffo_pairs_ = btd.get("ffo_pairs") or []
        ffo_rb_fig = (analyze.svg_rangeband(
            {"hist": ffo_pairs_, "current": btd.get("current_pfo"), "kind": "per", "low_is_cheap": True},
            "P/FFOの自社過去レンジ")
            if len(ffo_pairs_) >= 3 and LC.is_num(btd.get("current_pfo")) else "")
        bt_rows_reit = [
            _detail_row(
                "ffo_band_pos", "P/FFO 自社過去レンジ内の位置",
                (f"割安度 {btd['ffo_band']*100:.0f}/100" if LC.is_num(btd.get("ffo_band"))
                 else "履歴不足で算出不可"),
                "0＝レンジ上端（高P/FFO＝割高）／100＝下端（低P/FFO＝割安）。"
                "自社の物差しで割安か。P/FFO＝株価÷FFO（REIT版のPER）。",
                _why_band(btd.get("ffo_band"),
                          {"hist": ffo_pairs_, "current": btd.get("current_pfo")}, "per"),
                btd.get("ffo_band_score"), figure=ffo_rb_fig),
            _detail_row(
                "ffo_vs_sector", "P/FFO 対業種平均",
                f"{btd['current_pfo']:.1f}倍" if LC.is_num(btd.get("current_pfo")) else "―",
                "1.0未満＝業種平均より安い。0.95以下で割安・1.2超で割高。",
                _why_vs(btd.get("ffo_vs"), "mul"), btd.get("ffo_vs_score")),
        ]
        bt_blocks = [f'<div class="domhead"><b>P/FFO割安度</b> {analyze.bar(btd.get("ffo_cheap"))}</div>'
                    + "".join(bt_rows_reit)]
        bt_html = bt_note + "".join(bt_blocks)

    # 参考欄
    ref_rows = []
    ig = extras.get("implied_growth")
    if ig is not None:
        hist = extras.get("hist_growth")
        cmp_txt = ""
        if LC.is_num(hist):
            state = ("市場は実績超えの成長を織り込み（強気）" if ig > hist + 0.02 else
                     "市場は実績並み以下を想定（保守的）" if ig < hist - 0.02 else "市場は実績並みを想定")
            cmp_txt = f"　過去のFCF成長 年率 {hist*100:.1f}% -> {state}"
        ref_rows.append(("市場が織り込むFCF成長率（簡易リバースDCF）",
                         f"今後10年 年率 約{ig*100:.1f}%（割引率10%・ターミナル成長2.5%）{cmp_txt}"))
    # 予想配当利回り（採点には使わないが、参考として表示）
    dy = extras.get("div_yield")
    if LC.is_num(dy):
        ref_rows.append(("予想配当利回り（採点しない・参考）", f"{dy:.2f}%"))
    for nm in ("自己資本比率（自己資本÷総資産）",
               "自社株買い（直近期）", "総還元利回り（配当＋自社株買い）",
               "総還元性向（（配当＋自社株買い）÷純利益）",
               "インタレストカバレッジレシオ（EBIT÷支払利息）", "益回り（1÷PER）",
               "ROIC（投下資本利益率）", "RSI(14)", "MACD(12,26,9)"):
        it = next((r for r in extras.get("ref_src", []) if r.get("name") == nm), None)
        if it:
            ref_rows.append((it["name"], it["disp"]))
    if LC.is_num(extras.get("drawdown")):
        ref_rows.append(("高値からの下落率（採点しない）",
                         f"直近レンジ高値から {extras['drawdown']:.0f}%"))
    ref_html = "".join(
        f'<div class="plain"><span class="mn">{n}</span><span class="mv2">{v}</span></div>'
        for n, v in ref_rows)

    warn_html = ("" if not warnings else
                 '<div class="warn"><b>データ上の注意</b><ul>'
                 + "".join(f"<li>{w}</li>" for w in warnings) + "</ul></div>")

    qn = f"{q_score:.0f}" if LC.is_num(q_score) else "―"
    bn = f"{bt_score:.0f}" if LC.is_num(bt_score) else "―"
    qcls = LC.tier_label(q_score, LC.QUAL_TIERS) or "na"
    tiers = tuple(LC.load_bt_cfg()[f"tim_tiers_{market}"])
    bcls = LC.tier_label(bt_score, tiers) or "na"
    qlab = LC.QUAL_TIER_JA.get(LC.tier_label(q_score, LC.QUAL_TIERS), "―")
    blab = LC.BUY_TIER_JA.get(LC.tier_label(bt_score, tiers), "―")
    qs, qp, ql = q_cov
    bs, bp, bl = bt_cov

    ohlc = meta.get("ohlc") or {}
    ohlc_line = ""
    if ohlc:
        ohlc_line = (f'<div class="ohlc">前回の値動き（{meta.get("price_date") or "―"}）'
                     f'　終値 <b>{_f(ohlc.get("close"),1)}{unit}</b>'
                     f'　／　高値 {_f(ohlc.get("high"),1)}{unit}'
                     f'　／　安値 {_f(ohlc.get("low"),1)}{unit}'
                     f'　／　始値 {_f(ohlc.get("open"),1)}{unit}</div>')

    price_svg = svg_price(ctx["hist_m"])

    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{analyze.THEME_HEAD}
<title>{meta['name']}（{meta['code']}）10年保有できるか</title>
<style>
{analyze.THEME_CSS}
/* analyze.THEME_CSS は --hi/--mid/--lo/--na のみ定義。ここで別名・派生色を補う */
:root{{--t1:var(--hi);--t2:var(--mid);--t3:var(--lo);--gC:var(--lo);
  --field:color-mix(in srgb,var(--fg) 4%,var(--card));
  --th:color-mix(in srgb,var(--fg) 6%,var(--card));
  --wbg:color-mix(in srgb,var(--mid) 12%,var(--bg));
  --wbd:color-mix(in srgb,var(--mid) 34%,var(--line));
  --wfg:var(--fg);--info:color-mix(in srgb,var(--accent) 12%,var(--card))}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
  font-family:"Segoe UI","Hiragino Kaku Gothic ProN","Noto Sans JP",Meiryo,sans-serif;
  line-height:1.6;font-size:14px}}
.wrap{{max-width:820px;margin:0 auto;padding:24px 20px 60px}}
.topbar{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}}
.topbar a{{font-size:12.5px;color:var(--accent);white-space:nowrap}}
h1{{font-size:20px;margin:0 0 2px}}
h1 small{{font-size:13px;color:var(--muted);font-weight:normal}}
.sub{{color:var(--muted);font-size:12.5px;margin-bottom:10px}}
.ohlc{{font-size:12px;color:var(--muted);margin:2px 0 12px}}
.ohlc b{{color:var(--fg)}}
.formula{{font-size:11.5px;color:var(--muted);background:var(--field);border:1px solid var(--line);
  border-radius:8px;padding:8px 12px;margin:6px 0 12px}}
.score2{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:6px}}
.sblk{{flex:1;min-width:250px;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:13px 16px}}
.sblk .hd{{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}}
.sblk .hd b{{font-size:13px}}
.sblk .num{{font-size:26px;font-weight:700;line-height:1.1}}
.sblk .num.hi{{color:var(--t1)}} .sblk .num.mid{{color:var(--t2)}} .sblk .num.lo{{color:var(--t3)}} .sblk .num.xlo{{color:var(--gC)}} .sblk .num.na{{color:var(--muted)}}
.sblk .lab{{font-size:12.5px;font-weight:700}}
.sblk .cov{{font-size:11px;color:var(--muted);margin-top:4px}} .sblk .cov b{{font-weight:700}}
h2{{font-size:15px;margin:22px 0 6px;border-bottom:2px solid var(--line);padding-bottom:4px}}
.domhead{{display:flex;align-items:center;gap:8px;margin:14px 0 4px;font-weight:700;font-size:13px}}
.bar{{flex:1;max-width:200px;height:8px;border-radius:4px;background:var(--line);overflow:hidden}}
.bar .fill{{height:100%}} .bar .fill.hi{{background:var(--t1)}} .bar .fill.mid{{background:var(--t2)}}
.bar .fill.lo{{background:var(--t3)}} .bar .fill.na{{background:var(--line)}}
.sc{{font-weight:700;font-size:11.5px}} .sc.hi{{color:var(--t1)}} .sc.mid{{color:var(--t2)}}
.sc.lo{{color:var(--t3)}} .sc.na{{color:var(--muted);font-weight:400}}
details.m,div.plain{{border:1px solid var(--line);border-radius:8px;margin:4px 0;background:var(--card)}}
details.m>summary{{padding:8px 12px 8px 26px;cursor:pointer;display:grid;position:relative;
  grid-template-columns:minmax(130px,1.2fr) minmax(120px,1.5fr) 1.4fr auto;gap:10px;
  align-items:center;font-size:12.5px}}
div.plain{{padding:8px 12px;display:grid;grid-template-columns:minmax(150px,1fr) 2fr;
  gap:10px;align-items:center;font-size:12.5px}}
details.m>summary{{list-style:none}} details.m>summary::-webkit-details-marker{{display:none}}
details.m>summary::before{{content:"▸";color:var(--muted);position:absolute;left:11px}}
details.m[open]>summary::before{{content:"▾"}}
@media(max-width:600px){{details.m>summary{{grid-template-columns:1fr auto}}
  details.m>summary .mr{{display:none}}}}
.mn{{font-weight:700}} .mv,.mv2{{color:var(--fg)}}
.mr{{color:var(--muted);font-size:11px}}
.mt{{white-space:nowrap;text-align:right}}
.mbody{{padding:10px 14px;border-top:1px solid var(--line);font-size:12px}}
.mbody p{{margin:6px 0}} .mbody .what{{color:var(--fg)}} .mbody .rule,.mbody .why{{color:var(--muted)}}
.figwrap{{margin:8px 0;overflow-x:auto}}
.figwrap svg{{max-width:100%;height:auto}}
.chart .cm{{fill:var(--muted);font-size:9px}} .chart .cx{{fill:var(--muted);font-size:8px;text-anchor:middle}}
.chart .ct{{fill:var(--fg);font-size:9px;font-weight:700}}
table.subt{{width:100%;border-collapse:collapse;margin:6px 0}}
table.subt th,table.subt td{{padding:4px 6px;border-bottom:1px solid var(--line);font-size:11.5px}}
table.subt th{{background:var(--th);color:var(--muted);font-weight:700;text-align:left}}
table.subt td.n,table.subt th.n{{text-align:right;font-variant-numeric:tabular-nums}}
table.subt tr:last-child td{{border-bottom:none}}
.warn{{margin:14px 0;padding:10px 12px;background:var(--wbg);border:1px solid var(--wbd);
  border-radius:8px;font-size:12px;color:var(--wfg)}}
.warn ul{{margin:4px 0 0;padding-left:18px}}
.disc{{margin-top:26px;padding:12px;background:var(--wbg);border:1px solid var(--wbd);
  border-radius:8px;font-size:11.5px;color:var(--wfg)}}
.chartwrap{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;margin:8px 0;overflow-x:auto}}
.cobox{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:8px 0}}
.legend{{font-size:11px;color:var(--muted);margin:4px 0 8px}}
.mv2{{color:var(--fg)}}
</style></head><body><div class="wrap">
<div class="topbar">
  <h1>{meta['name']}（{meta['code']}）<small>10年保有できる優良企業か</small></h1>
  <span><a href="../index.html">ランキング</a>　・　<a href="../watchlist.html">ウォッチリスト</a></span>
</div>
<div class="sub">現在株価 {_f(meta.get('price'),0)}{unit}（{meta.get('price_date') or '―'} 終値）　｜　{seckey}</div>
{ohlc_line}
<div class="formula">配当を評価しないレポートです。<b>品質スコア</b>＝業績×0.28＋財務×0.27＋CF×0.15（取得できたグループで再正規化）。
<b>買い時スコア</b>＝EV/EBIT対業種・FCF利回り・PER割安度・PBR割安度を均等25%で合成（欠損は中立60）。
配当利回り・連続増配・累進配当宣言などは一切見ていません。</div>

<div class="score2">
  <div class="sblk"><div class="hd"><b>品質スコア</b><span class="num {qcls}">{qn}</span>
    <span class="lab">{qlab}</span></div>
    <div class="cov">採点できた指標グループ <b>{qs}/{qp}</b>　カバレッジ <b>{ql}</b></div></div>
  <div class="sblk"><div class="hd"><b>買い時スコア</b><span class="num {bcls}">{bn}</span>
    <span class="lab">{blab}</span></div>
    <div class="cov">実データで採点 <b>{bs}/{bp}</b>　カバレッジ <b>{bl}</b>（欠損は中立60で補完）</div></div>
</div>
{warn_html}

<h2>会社概要</h2>
<div class="cobox">{comp_html}</div>

<h2>① 品質の指標（{'REIT向け代替指標(FFO)' if extras.get('reit_q') else '銀行・保険・証券向け代替指標' if is_fin_simple else '業績・財務・キャッシュフロー'}）</h2>
{q_html}

<h2>② 買い時の指標（割安さ・配当は不使用）</h2>
{bt_html}

<h2>株価</h2>
<div class="chartwrap">{price_svg}</div>

{earn_html}

<h2>参考（採点しない）</h2>
{ref_html}

<div class="disc">本レポートは公開データを機械的なルールで評価した教育目的の一般情報です。
特定銘柄の売買を推奨するものではなく、運営者は投資助言業の登録を受けていません。
数値は yfinance 由来で誤り・遅延・欠損があり得ます。市場が織り込む成長率（リバースDCF）は
割引率・ターミナル成長の前提に強く依存する参考値です。投資判断はご自身の責任で行ってください。</div>
<script>{analyze.THEME_JS}</script>
</div></body></html>"""


# ---------------------------------------------------------------- オーケストレーション
def generate_long(code, cfg=None, market="jp", name=None):
    res = {"code": str(code), "name": None, "ok": False, "error": None,
           "html": None, "md": None, "summary": None, "portfolio_data": None}
    code = re.sub(r"\D", "", str(code)) if market == "jp" else str(code).upper()
    res["code"] = code
    if not code:
        res["error"] = "コードが不正"
        return res

    try:
        if market == "jp":
            cfg = cfg or analyze.load_config()
            yd = analyze.fetch_yf(code)
        else:
            import analyze_us
            cfg = cfg or analyze_us.load_config_us()
            yd = analyze_us.fetch_yf_us(code)
    except Exception as e:
        res["error"] = f"取得失敗: {e}"
        return res

    info = yd.get("info") or {}
    if not info and yd.get("price") is None:
        res["error"] = "データ取得できず"
        return res
    smap, rules = cfg["smap"], cfg["rules"]
    sec_avg_all = cfg["sec_avg_all"]

    try:
        if market == "jp":
            jp_sector, industry, sector, sector_src, is_reit = analyze.classify_sector(info, smap)
            seckey = jp_sector
            is_simple = bool(rules["overrides"].get(jp_sector, {}).get("_simple")) or is_reit
            rate_sensitive = set(smap["_meta"].get("rate_sensitive", []))
            M, flags, ctx = analyze.build_metrics(
                yd, None, sec_avg_all.get(jp_sector, {}), is_simple, jp_sector,
                rate_sensitive, rules.get("market", {}).get("jgb_10y"), cfg["div_policies"])
        else:
            import analyze_us
            gics_sector, industry, ysector, is_simple, is_reit, src = \
                analyze_us.classify_sector_us(info, smap)
            # analyze_us.classify_sector_us は GICS Real Estate セクター全体を
            # is_reit=True にする（CBRE・CoStar等の不動産サービス/データ会社もREIT構造
            # ではないのに含まれてしまう）。analyze_us.py 本体は無改変のまま、ここで
            # ローカルにのみ「実際にREIT構造か」を industry 文字列で判定し直す。
            if gics_sector == "Real Estate" and "REIT" not in (industry or "").upper():
                is_simple = False
                is_reit = False
            seckey = gics_sector
            jp_sector = None
            rate_sensitive = set(smap.get("rate_sensitive", []))
            ust_10y = rules.get("market", {}).get("ust_10y", 4.2)
            M, flags, ctx = analyze_us.build_metrics_us(
                yd, sec_avg_all.get(gics_sector, {}), is_simple, gics_sector,
                rate_sensitive, ust_10y)
        dom_scores, detail, groups, sel_score, tim_score, coverage = analyze.score_all(
            M, seckey, rules, is_simple)
    except Exception as e:
        import traceback
        res["error"] = f"採点失敗: {e}\n{traceback.format_exc()}"
        return res

    name = name or info.get("longName") or info.get("shortName") or code
    res["name"] = name
    ctx["hist_m"] = yd["hist_m"]
    if market == "jp":
        ctx["earn"] = analyze.build_earnings(yd, yd["price"])
    else:
        import analyze_us
        ctx["earn"] = (analyze_us.build_earnings_us(yd, yd["price"])
                       if hasattr(analyze_us, "build_earnings_us")
                       else analyze.build_earnings(yd, yd["price"]))
    ctx["company"] = analyze.build_company_overview(info)

    rowmap = {r["key"]: r for dom in detail for r in detail[dom] if r.get("key")}

    # 銀行・保険・証券（is_simpleだがREITではない）：v2で専用の品質サブスコアを追加。
    # REIT(is_reit)はFFO系の指標設計が別途必要な規模のためv2でも対象外のまま。
    is_fin_simple = bool(is_simple and not is_reit)
    fin_q = fq_parts = None
    if is_fin_simple:
        fin_q = _raw_financial_quality(yd, info)
        fq_score, fq_parts = LC.financial_quality_score(fin_q)
        groups = dict(groups)
        groups["金融品質"] = fq_score

    # 米国REIT：v2でFFOベースの専用品質・買い時サブスコアを追加（JPはJ-REITが
    # 配当株ツールの母集団に無く別タスク規模のため対象外）。
    is_reit_us = bool(is_reit and market == "us")
    reit_q = reit_parts = ffo_pairs = None
    if is_reit_us:
        reit_q = _raw_reit_quality(yd, info)
        rq_score, reit_parts = LC.reit_quality_score(reit_q)
        groups = dict(groups)
        groups["REIT品質"] = rq_score
        ffo_pairs = _ffo_pairs(yd, reit_q["ffo_series"], reit_q["shares"])

    q_score = LC.quality_score(groups)
    q_cov = LC.quality_coverage(groups)

    raw = _raw_valuation(yd)
    pbr_pairs = _pbr_pairs(yd, raw)
    bt_score, bt_cov, bt_comp, btd = _buytiming(raw, rowmap, seckey, market, pbr_pairs,
                                                is_fin_simple=is_fin_simple, is_reit=is_reit_us,
                                                reit_q=reit_q, ffo_pairs=ffo_pairs)

    ig = LC.implied_fcf_growth(raw["mcap"], raw["fcf"])
    hg = None
    xs = raw["fcf_series"]
    if len([x for x in xs if x > 0]) >= 2:
        try:
            hg = analyze.cagr(xs[-1], xs[0], len(xs) - 1) / 100.0
        except Exception:
            hg = None
    dd = None
    try:
        closes = [v for _, v in yd["hist_m"] if LC.is_num(v)]
        if closes:
            peak = max(closes[-60:] if len(closes) >= 60 else closes)
            if peak > 0 and LC.is_num(yd["price"]):
                dd = (yd["price"] / peak - 1) * 100
    except Exception:
        dd = None
    extras = {"implied_growth": ig, "hist_growth": hg,
              "ref_src": M.get("参考", []), "drawdown": dd,
              "div_yield": rowmap.get("div_yield", {}).get("v"),
              "fin_q": fin_q, "fq_parts": fq_parts,
              "reit_q": reit_q, "reit_parts": reit_parts}

    warnings = []
    if is_reit_us:
        warnings.append("REITは業績・財務・CFの代わりに、専用のFFOベース品質サブスコア"
                        "（FFO成長率・増収率・FFOの安定度・利払い余裕度）で評価しています。"
                        "買い時スコアもPER・PBR・EV/EBIT・FCF利回りが構造的に使えないため、"
                        "P/FFO割安度のみで合成しています（FFOはyfinanceのデータから算出した"
                        "近似値で、各社が公表するFFOとは厳密には一致しません）。")
    elif is_reit:
        warnings.append("REIT は業績・財務・CFを構造的に採点できないため、"
                        "品質スコアは算出していません（本ツールの対象外）。")
    elif is_fin_simple:
        warnings.append("銀行・保険・証券は業績・財務・CFの代わりに、専用の品質サブスコア"
                        "（ROE・増収率・EPS成長率・利益の安定度）で評価しています。"
                        "買い時スコアもEV/EBIT・FCF利回りが構造的に使えないため、"
                        "PER割安度・PBR割安度のみで合成しています。")
    if not yd.get("is_rows"):
        warnings.append("損益計算書を取得できず、業績の評価が限定的です。")
    if not is_fin_simple and not is_reit_us and not LC.is_num(btd["ev_median"]):
        warnings.append("この業種の EV/EBIT 中央値が未算出のため EV/EBIT対業種 は中立扱いです。")

    pd_ = yd.get("price_date")
    meta = {"code": code, "name": name, "jp_sector": jp_sector, "gics_sector": seckey,
            "price": yd["price"], "price_date": pd_.isoformat() if pd_ else None,
            "mcap": info.get("marketCap"), "ohlc": yd.get("ohlc"),
            "is_simple": bool(is_simple), "is_reit": bool(is_reit)}

    try:
        res["html"] = render_long_html(meta, groups, detail, q_score, q_cov, bt_score,
                                       bt_cov, btd, ctx, extras, warnings, rules, market)
    except Exception as e:
        import traceback
        res["error"] = f"描画失敗: {e}\n{traceback.format_exc()}"
        return res

    res["summary"] = {
        "code": code, "name": name,
        "jp_sector": jp_sector, "gics_sector": seckey if market == "us" else None,
        "is_simple": bool(is_simple), "is_reit": bool(is_reit),
        "price": yd["price"], "price_date": meta["price_date"], "mcap": info.get("marketCap"),
        "q_score": q_score,
        "groups": {"業績": groups.get("業績"), "財務": groups.get("財務"),
                   "キャッシュフロー": groups.get("キャッシュフロー"),
                   "金融品質": groups.get("金融品質"), "REIT品質": groups.get("REIT品質")},
        "is_fin_simple": is_fin_simple, "is_reit_us": is_reit_us,
        "current_pfo": btd.get("current_pfo"),
        "q_cov": q_cov[2],
        "bt_score": bt_score, "bt_cov": bt_cov[2], "bt_components": bt_comp,
        "ev_ebit": btd["ev_ebit"], "fcf_yield": btd["fcf_yield"],
        "per_band_pos": btd["per_band"], "pbr_band_pos": btd["pbr_band"],
        "div_yield": rowmap.get("div_yield", {}).get("v"),  # 表示のみ（採点には不使用）
        "implied_fcf_growth": ig,
        "_generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        res["portfolio_data"] = analyze.portfolio_data(yd)
    except Exception:
        res["portfolio_data"] = {"prices": [], "divs": []}
    res["ok"] = True
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--us", action="store_true")
    args = ap.parse_args()
    r = generate_long(args.code, market="us" if args.us else "jp")
    if not r["ok"]:
        sys.exit(r["error"])
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, f"{r['code']}_{r['name']}.html")
    open(p, "w", encoding="utf-8").write(r["html"])
    print(f"-> {p}")
    s = r["summary"]
    print(f"  品質 {s['q_score']}（{s['q_cov']}）／ 買い時 {s['bt_score']}（{s['bt_cov']}）"
          f"　EV/EBIT {s['ev_ebit']}  FCF利回り {s['fcf_yield']}"
          f"  PER割安 {s['per_band_pos']}  PBR割安 {s['pbr_band_pos']}")


if __name__ == "__main__":
    main()
