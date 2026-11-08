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

# 「詳しい条件で絞り込む」フィルタで個別指標として公開するキー（業績4・財務5(USのみ
# interest_coverageあり)・CF3）。rowmap[k]["score"]（0〜110・常に高いほど良い）を使う。
_FILTER_METRIC_KEYS = (
    "rev_cagr", "eps_cagr", "op_margin", "earnings_stability",
    "equity_ratio", "de", "net_de", "debt_to_ocf", "interest_coverage",
    "ocf_positive", "fcf_positive", "fcf_payout",
)

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
analyze.METRIC_HELP.setdefault("q_rev_trend_us", {"what":
    "直近4四半期の売上高の推移（yfinanceの四半期決算）。品質スコアは年次決算だけで"
    "計算するため、直近1年以内に始まった変化はここでしか確認できない。急減速を検知した"
    "場合のみ品質スコアの業績グループを減点する（下の「データ上の注意」参照）。",
    "unit": ""})
analyze.METRIC_HELP.setdefault("q_opinc_trend_us", {"what":
    "直近4四半期の営業利益の推移（yfinanceの四半期決算）。売上高と同様、参考表示のみで"
    "採点には使わない。", "unit": ""})
analyze.METRIC_HELP.setdefault("q_netinc_trend_us", {"what":
    "直近4四半期の純利益の推移（yfinanceの四半期決算。営業利益が取得できない銘柄の代替）。"
    "参考表示のみで採点には使わない。", "unit": ""})

# ランキング一覧の「騰落率（展開）」欄用。yfinanceの日次終値（hist_d、株価チャート用に
# 元々取得済みのデータを流用・追加のAPIアクセス無し）から、直近終値を基準に
# 取引日ベースで遡った騰落率(%)を計算する（暦日ではなく取引日数で近似：
# 週=5・月=21・3か月=63・6か月=126・9か月=189・年=252）。直近上場等で
# 遡る分の取引日数が無い場合はNone。
_PRICE_CHG_PERIODS = (("d", 1), ("w", 5), ("m", 21), ("3m", 63), ("6m", 126), ("9m", 189), ("y", 252))


def _price_changes(hist_d):
    out = {k: None for k, _ in _PRICE_CHG_PERIODS}
    if hist_d is None or getattr(hist_d, "empty", True):
        return out
    closes = hist_d["Close"].dropna()
    if closes.empty:
        return out
    latest = float(closes.iloc[-1])
    for key, n in _PRICE_CHG_PERIODS:
        if len(closes) > n:
            base = float(closes.iloc[-1 - n])
            if base != 0:
                out[key] = (latest / base - 1) * 100
    return out


def _portfolio_data(yd):
    """ポートフォリオ機能用の軽量データ（10年保有ツール専用。配当株側の関数には依存しない）。
    prices＝[[日付, 終値]]、opens＝始値（pricesと同じ並び・同じ長さ、欠損はNone）、
    divs＝[[権利落ち日, 1株配当]]。"""
    prices, opens = [], []
    hd = yd.get("hist_d")
    if hd is not None and not getattr(hd, "empty", True):
        for idx, r in hd.iterrows():
            c = r.get("Close")
            if LC.is_num(c):
                prices.append([idx.date().isoformat(), round(float(c), 2)])
                o = r.get("Open")
                opens.append(round(float(o), 2) if LC.is_num(o) else None)
    divs = [[d0.isoformat(), round(float(v), 4)] for d0, v in (yd.get("divs") or [])]
    return {"prices": prices, "opens": opens, "divs": divs}


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


_SEC_ROE_ROA = {"jp": None, "us": None}


def _sector_roe_roa(market="jp"):
    """sector_roe_roa_long{,_us}.json（業種ごとのROE・ROA中央値。calib_long.pyが生成・表示専用）。無ければ空。"""
    if _SEC_ROE_ROA[market] is None:
        fn = "sector_roe_roa_long.json" if market == "jp" else "sector_roe_roa_long_us.json"
        p = os.path.join(HERE, fn)
        try:
            _SEC_ROE_ROA[market] = analyze.load_json(fn) if os.path.isfile(p) else {}
        except Exception:
            _SEC_ROE_ROA[market] = {}
    return _SEC_ROE_ROA[market]


_ETF_TOP = {"loaded": False, "map": {}, "asof": ""}


def _etf_ranks(code):
    """主要ETF14本の上位15銘柄（etf_top_us.json。fetch_etf_top_us.pyが月次更新）での、この銘柄の順位。
    -> ([(ETF名, ETF説明, 順位, 組入比率%)], 基準日)。米国株のみ使う。表示専用（採点には使わない）。"""
    if not _ETF_TOP["loaded"]:
        _ETF_TOP["loaded"] = True
        try:
            import json as _j
            d = _j.load(open(os.path.join(HERE, "etf_top_us.json"), encoding="utf-8"))
            for e in d.get("etfs", []):
                if e.get("as_of") and (not _ETF_TOP["asof"] or e["as_of"] < _ETF_TOP["asof"]):
                    _ETF_TOP["asof"] = e["as_of"]
                for i, h in enumerate((e.get("holdings") or [])[:15], 1):
                    t = str(h.get("ticker") or "").upper()
                    if t:
                        _ETF_TOP["map"].setdefault(t, []).append((e.get("etf"), e.get("label") or "", i, h.get("weight")))
        except Exception:
            pass
    return _ETF_TOP["map"].get(str(code).upper(), []), _ETF_TOP["asof"]


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


# 米国株のGICS11業種の日本語表示名（表示専用）。設定JSON・summary・URL・保存済みの絞り込み条件など
# 内部の値は英語のまま（業種グループ・業種中央値・sector_rules の鍵になっているため）。
GICS_JA = {
    "Information Technology": "情報技術",
    "Health Care": "ヘルスケア",
    "Financials": "金融",
    "Consumer Discretionary": "一般消費財",
    "Consumer Staples": "生活必需品",
    "Communication Services": "コミュニケーション",
    "Industrials": "資本財",
    "Energy": "エネルギー",
    "Utilities": "公益事業",
    "Materials": "素材",
    "Real Estate": "不動産",
}


def _sec_label(name, market):
    """業種名の表示用ラベル。米国株はGICSを日本語に、日本株（もともと日本語）はそのまま。"""
    return GICS_JA.get(name, name) if market == "us" else name


def _insight_extras(yd, market, is_simple, is_reit, q_score, bt_score, tiers, raw, sec_med=None, is_lease=False):
    """『見方を深める指標』と、資料（銘柄分析の基礎）の基準にもとづく注意フラグ。
    **表示専用**：品質スコア・買い時スコアの計算には一切使わない（2026-10-06決定。
    過去4〜5期のデータでは、採点に組み込める根拠が得られなかったため）。
    返り値 {"rows": [(見出し, 本文)], "flags": {...}, "raw": {...}}。取得できない項目は出さない。"""
    isr, bsr, cfr = yd.get("is_rows") or {}, yd.get("bs_rows") or {}, yd.get("cf_rows") or {}
    g = lambda rows, *lb: (analyze.row(rows, *lb) or [])
    at = lambda s, i: (s[i] if s and len(s) > i and LC.is_num(s[i]) else None)
    ni = g(isr, "Net Income", "Net Income Common Stockholders")
    rev = g(isr, "Total Revenue", "Operating Revenue")
    ta = g(bsr, "Total Assets")
    eq = g(bsr, "Stockholders Equity", "Common Stock Equity")
    shs = g(bsr, "Ordinary Shares Number", "Share Issued")
    ocf = g(cfr, "Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
    capex = g(cfr, "Capital Expenditure")
    dep = g(cfr, "Depreciation And Amortization", "Depreciation Amortization Depletion") \
        or g(isr, "Reconciled Depreciation")
    sbc = g(cfr, "Stock Based Compensation")
    # 銀行・保険・証券・REIT：CF・設備投資・自己資本比率の物差しは当てはまらない。リース・信販（is_lease）も、営業CFにリース資産・貸付債権の
    # 積み増しが入り、借入で資産を積む構造のため、CFの「足切り」・利益の質・自己資本比率の足切り・借入依存の高ROEの注意は出さない
    fin_like = bool(is_simple) or bool(is_lease)

    rows, flags, rawv = [], {}, {}
    pc = lambda v, d=1: f"{v:.{d}f}%"

    # --- 収益性：ROE・ROA・財務レバレッジ（借入に頼った高ROEの見分け）---
    ni0, eq0, ta0 = at(ni, 0), at(eq, 0), at(ta, 0)
    roe = ni0 / eq0 * 100 if LC.is_num(ni0) and LC.is_num(eq0) and eq0 > 0 else None
    roa = ni0 / ta0 * 100 if LC.is_num(ni0) and LC.is_num(ta0) and ta0 > 0 else None
    lev = ta0 / eq0 if LC.is_num(ta0) and LC.is_num(eq0) and eq0 > 0 else None
    rawv.update({"roe": roe, "roa": roa, "leverage": lev})
    if LC.is_num(roe) and LC.is_num(roa):
        note = "ROEとROAが近く、借入に頼らず稼げている" if (lev is not None and lev < 2.5) else "ROEがROAより高いのは、借入（財務レバレッジ）の効果"
        if ni0 <= 0:
            note = "純損失のため、ROE・ROAは効率の目安にならない（赤字の理由を決算資料で確認）"
        elif roe >= 10 and roa < 3:
            flags["lev_roe"] = True
            note = "ROEは高いがROAが低い＝借入に頼って高く見えている可能性（景気悪化時に返済負担が重くなる）"
        if fin_like:
            note = ("リース・信販は借入で資産を積む構造上レバレッジが大きく、ROAは低く出るのが普通" if is_lease
                    else "銀行・保険・証券・REITは構造上レバレッジが大きく、ROAは低く出るのが普通")
            flags.pop("lev_roe", None)
        rows.append(("ROE・ROA・財務レバレッジ",
                     f"ROE {pc(roe)}／ROA {pc(roa)}／総資産÷自己資本 {lev:.1f}倍　… {note}"))

    # --- ROE・ROAの目安との位置と、同じ業種（この母集団内）の中央値との比較（表示専用・採点しない）---
    sm = sec_med or {}

    def _vs_sector(v, key):
        m = sm.get(key)
        if not (LC.is_num(v) and LC.is_num(m)):
            return ""
        d = v - m
        pos = "上回る" if d > 0.05 else "下回る" if d < -0.05 else "ほぼ同じ"
        return f"　／　この業種（母集団内）の中央値 {m:.1f}%（{d:+.1f}ポイント、{pos}）"

    if LC.is_num(roe):
        if LC.is_num(ni0) and ni0 <= 0:
            lab = "赤字のため目安に当てはめない"
        else:
            lab = ("優良（10%以上）" if roe >= 10 else "合格（8%以上）" if roe >= 8 else
                   "やや低め（5〜8%）" if roe >= 5 else "低効率（5%未満）")
        lab_txt = lab if lab.startswith("赤字") else f"目安では「{lab}」（8%以上で合格・10%以上で優良・5%未満は低効率）"
        rows.append(("ROEの目安と業種比較", f"{pc(roe)}　… {lab_txt}" + _vs_sector(roe, "roe")))
    if LC.is_num(roa):
        if fin_like:
            lab = ("リース・信販は借入で資産を積む構造上低く出るため、目安をそのまま当てはめず、同業との比較で見る" if is_lease
                   else "銀行・保険・証券・REITは構造上低く出るため、目安をそのまま当てはめず、同業との比較で見る")
        elif LC.is_num(ni0) and ni0 <= 0:
            lab = "赤字のため目安に当てはめない"
        else:
            lab = ("良好（5%以上）" if roa >= 5 else "標準（3〜5%）" if roa >= 3 else "改善の余地（3%未満）")
            lab = f"目安では「{lab}」（5%以上で良好・3%未満は改善の余地）"
        rows.append(("ROAの目安と業種比較", f"{pc(roa)}　… {lab}" + _vs_sector(roa, "roa")))

    # --- 利益の質（銀行・保険・証券・REITは対象外）---
    if not fin_like:
        qs = []
        for i in range(min(3, len(ni), len(ocf))):
            n_, o_ = at(ni, i), at(ocf, i)
            if LC.is_num(n_) and LC.is_num(o_) and n_ > 0:
                qs.append(o_ / n_)
        if qs:
            rawv["ocf_ni"] = qs[0]
            tail = f"（直近{len(qs)}期：" + "／".join(f"{x:.2f}" for x in qs) + "）" if len(qs) > 1 else ""
            note = ("営業CFが純利益を上回り、利益が現金で裏付けられている" if qs[0] >= 1.0 else
                    "営業CFが純利益を下回る。続くなら売掛金・在庫の増加など、現金になっていない利益に注意")
            rows.append(("営業CF÷純利益（利益の質）", f"直近 {qs[0]:.2f}倍{tail}　… {note}"))
        ta1 = at(ta, 1)
        if LC.is_num(ni0) and LC.is_num(at(ocf, 0)) and LC.is_num(ta0) and LC.is_num(ta1) and (ta0 + ta1) > 0:
            acc = (ni0 - at(ocf, 0)) / ((ta0 + ta1) / 2) * 100
            rawv["accrual"] = acc
            rows.append(("会計発生高（（純利益−営業CF）÷平均総資産）",
                         f"{acc:+.1f}%　… 大きなプラスが続くと、利益が現金の裏付けに乏しい可能性。マイナスは現金が利益を上回る状態"))

        # --- 設備投資÷減価償却 ---
        cx0, dp0 = at(capex, 0), at(dep, 0)
        if LC.is_num(cx0) and LC.is_num(dp0) and dp0 > 0:
            r = abs(cx0) / dp0
            rawv["capex_da"] = r
            note = ("設備投資が減価償却を上回り、成長投資（または能力増強）の可能性" if r > 1.1 else
                    "設備投資が減価償却を下回り、設備の更新不足の可能性" if r < 0.9 else
                    "設備投資と減価償却がほぼ釣り合い、現状維持の水準")
            rows.append(("設備投資÷減価償却費", f"{r:.2f}倍　… {note}（業種・事業段階で解釈が変わる）"))

    # --- 株数の推移（自社株買い・希薄化）---
    s0 = at(shs, 0)
    sold = next((shs[i] for i in range(len(shs) - 1, 0, -1) if LC.is_num(shs[i])), None)
    if LC.is_num(s0) and LC.is_num(sold) and sold > 0 and len([x for x in shs if LC.is_num(x)]) >= 3:
        n_y = len([x for x in shs if LC.is_num(x)]) - 1
        ch = (s0 / sold - 1) * 100
        rawv["shares_chg_pct"] = ch
        note = ("株数が減少（自社株買いなどで1株あたりの価値が高まりやすい）" if ch < -1 else
                "株数が増加（増資や株式報酬による希薄化の可能性）" if ch > 1 else "株数はほぼ横ばい")
        rows.append((f"発行済株式数の変化（過去{n_y}年）", f"{ch:+.1f}%　… {note}"))

    # --- 米国株：株式報酬（SBC）---
    if market == "us" and not fin_like:
        sb0, rv0, oc0, cx0 = at(sbc, 0), at(rev, 0), at(ocf, 0), at(capex, 0)
        if LC.is_num(sb0) and LC.is_num(rv0) and rv0 > 0:
            line = f"SBC÷売上高 {sb0 / rv0 * 100:.1f}%"
            rawv["sbc_rev_pct"] = sb0 / rv0 * 100
            if LC.is_num(oc0) and LC.is_num(cx0):
                fcf_net = (oc0 + cx0 - sb0) / rv0 * 100
                rawv["fcf_less_sbc_pct"] = fcf_net
                line += f"／（FCF−SBC）÷売上高 {fcf_net:.1f}%"
            rows.append(("株式報酬（SBC）の重さ",
                         line + "　… SBCは従業員への実質的な報酬コスト。FCFから引いても残るかを見る"))

    # --- 注意フラグ（資料の「足切り」「バリュートラップ」）---
    cut = []
    ni3 = [at(ni, i) for i in range(3)]
    if all(LC.is_num(x) for x in ni3) and all(x < 0 for x in ni3):
        cut.append("純損失が3期連続")
    if not fin_like:
        o0, o1 = at(ocf, 0), at(ocf, 1)
        if LC.is_num(o0) and LC.is_num(o1) and o0 < 0 and o1 < 0:
            cut.append("営業CFが2期連続マイナス")
        eqr = (eq0 / ta0 * 100) if LC.is_num(eq0) and LC.is_num(ta0) and ta0 > 0 else None
        if LC.is_num(eqr) and eqr < 10:
            cut.append(f"自己資本比率が10%未満（{eqr:.1f}%）")
    if LC.is_num(eq0) and eq0 <= 0:
        cut.append("債務超過（自己資本がマイナス）")
    flags["cutoff"] = cut

    pbr = raw.get("pbr") if raw else None
    trap = []
    btier = LC.tier_label(bt_score, tiers) if LC.is_num(bt_score) else None
    qtier = LC.tier_label(q_score, LC.QUAL_TIERS) if LC.is_num(q_score) else None
    if btier == "hi" and qtier in ("lo", "xlo"):
        trap.append("買い時スコアは高い（割安圏）が、品質スコアは低め。安い理由がある可能性（バリュートラップ）")
    if LC.is_num(pbr) and pbr < 1 and LC.is_num(roe) and roe < 5:
        trap.append(f"PBR {pbr:.2f}倍（1倍割れ）かつROE {roe:.1f}%（5%未満）。PBR＝PER×ROEなので、低PBRの理由が"
                    "「稼ぐ力の弱さ」の可能性（資産を使って稼げていない）")
    flags["value_trap"] = trap
    return {"rows": rows, "flags": flags, "raw": rawv}


# ---------------------------------------------------------------- データ不備の疑い（表示専用）
# Yahoo Finance の財務データの不備を見つけて知らせる。**採点には一切使わない**（スコアも順位も変えない）。
# 配当株ツール側の自動チェック（fin_guard）の判定を、10年保有側にコピーして移植したもの（共有コードには依存しない・
# 外して採点から除く処理はしない）。2026-11-06の照合（IR BANK・SEC）で、判定の確からしさが違うため2段階に分ける：
#   high  ＝「最新期の1株利益が前の年と同じ値」（更新遅れ）。照合では日本株3/3・米国1/2が本当の不備
#   check ＝それ以外（1株利益・PER・PBRを、決算書から求め直した値とのズレ）。誤判定が多い（日本株で6割超）ので、
#           「Yahoo側が正しい場合も多い。IR BANK・決算短信（米国はSEC）で確認」と控えめに出す
def _eps_stale_or_bad_jp(is_years, eps, ni_c, sh_c, so=None):
    """最新期の1株利益が信頼できないときの種類（無ければ None）。stale＝前の年と同じ値／mismatch＝純利益÷平均株式数と食い違う／
    shift＝1株利益×株式数が前の年の純利益と一致（年別データのずれ）。
    【2026-11-06 見直し】IR BANKとの照合で誤判定が多かったため、配当株ツール側の見直し（cbfe6d17a8）に合わせて、純利益から求め直す
    判定（mismatch・shift）をしない条件を足した：①平均株式数÷発行済株式数が0.75〜1.33の外（株式分割の反映ずれなど）
    ②純利益の行がEPSの行と1年分入れ替わっている ③shiftは最新期の純利益が欠損のとき判定しない。staleはそのまま。"""
    if not eps or len(eps) < 2 or not LC.is_num(eps[0]):
        return None
    e0, e1 = eps[0], eps[1]
    if LC.is_num(e1) and e0 == e1:
        return "stale"
    n0 = ni_c[0] if ni_c and LC.is_num(ni_c[0]) else None
    n1 = ni_c[1] if ni_c and len(ni_c) > 1 and LC.is_num(ni_c[1]) else None
    s0 = sh_c[0] if sh_c and LC.is_num(sh_c[0]) and sh_c[0] > 0 else None
    if s0 is None:
        return None
    if LC.is_num(so) and so > 0 and not (0.75 <= s0 / so <= 1.33):
        return None
    e1_, s1_ = (eps[1] if len(eps) > 1 else None), (sh_c[1] if sh_c and len(sh_c) > 1 else None)
    if (LC.is_num(n0) and LC.is_num(n1) and LC.is_num(e1_) and LC.is_num(s1_) and n0 != 0 and n1 != 0
            and abs(e0 * s0 / n1 - 1) < 0.03 and abs(e1_ * s1_ / n0 - 1) < 0.03):
        return None
    if LC.is_num(n0):
        ec = n0 / s0
        if (e0 > 0) != (ec > 0) or (e0 > 0 and abs(ec / e0 - 1) > 0.20):
            return "mismatch"
    if LC.is_num(n0) and LC.is_num(n1) and n1 != 0 and abs(e0 * s0 / n1 - 1) < 0.03 and abs(e0 * s0 / n0 - 1) >= 0.03:
        return "shift"
    return None


def _quarter_pbr_jp(code, price, shares):
    """直近四半期の貸借対照表の純資産から求めたPBR（取れなければ None）。決算期末の古い純資産で外れたときの再確認にだけ使う。"""
    try:
        import io as _io
        import contextlib as _cl
        import yfinance as _yf
        with _cl.redirect_stdout(_io.StringIO()), _cl.redirect_stderr(_io.StringIO()):
            q = _yf.Ticker(f"{code}.T").quarterly_balance_sheet
        for k in ("Stockholders Equity", "Common Stock Equity"):
            if q is not None and not q.empty and k in q.index:
                v = q.loc[k].dropna()
                if len(v) and float(v.iloc[0]) > 0 and LC.is_num(shares) and shares > 0 and LC.is_num(price) and price > 0:
                    return price / (float(v.iloc[0]) / shares)
    except Exception:
        pass
    return None


def _data_notices(yd, market, code, applied=None):
    """データ不備の疑いのリスト [{tier, k, where, why, how}]。該当が無ければ []。
    yd は置き換え前のYahooのデータを渡すこと（置き換え後だと、疑いが検出されなくなる）。"""
    out = []
    info = yd.get("info") or {}
    isr, bsr = yd.get("is_rows") or {}, yd.get("bs_rows") or {}
    price = yd.get("price")
    eps = analyze.row(isr, "Basic EPS", "Diluted EPS") or []
    jp = (market == "jp")
    how_eps = (f"IR BANK（irbank.net/{code}/results）の最新期のEPSと、決算短信の1株当たり当期純利益を、Yahooの値と比べる" if jp else
               "SEC EDGAR で、この会社の最新の年次報告書（10-K）の希薄化後1株利益を、Yahooの値と比べる")
    if jp:
        kind = _eps_stale_or_bad_jp(yd.get("is_years") or [], eps,
                                    analyze.row(isr, "Net Income Common Stockholders", "Net Income") or [],
                                    analyze.row(isr, "Basic Average Shares", "Diluted Average Shares") or [],
                                    so=info.get("sharesOutstanding"))
    else:
        kind = "stale" if (len(eps) >= 2 and LC.is_num(eps[0]) and LC.is_num(eps[1]) and eps[0] == eps[1] and eps[0] != 0) else None
        if kind is None:
            try:
                # 基準は「決算書の平均株式数 ÷ （時価総額÷株価）が2.5倍超」の一方向。YahooのsharesOutstandingは、
                # 株式の種類が複数ある会社で一部の種類分しか入らず誤判定になる（NWS・TKO・SKYD）ため、全種類の合計である
                # 時価総額÷株価を使う（配当株ツール側の同じ判定の修正に合わせた。2026-11-06）
                sh = analyze.row(isr, "Diluted Average Shares", "Basic Average Shares")
                mc = info.get("marketCap")
                if sh and LC.is_num(sh[0]) and sh[0] > 0 and LC.is_num(mc) and mc > 0 and LC.is_num(price) and price > 0:
                    r = sh[0] / (mc / price)
                    if r > 2.5:
                        out.append({"tier": "check", "k": "eps", "where": "EPS（1株利益）の推移・PERの自社過去レンジ内の位置",
                                    "why": f"決算書の平均株式数が、時価総額÷株価から求めた株式数の{r:.1f}倍（通常は1倍前後）で、Yahooの年次の1株利益の桁がずれている疑いがあります。"
                                           "ただし、Yahoo側が正しい場合もあります",
                                    "how": how_eps})
            except Exception:
                pass
    if kind == "stale":
        out.append({"tier": "high", "k": "eps", "where": "EPS（1株利益）の推移",
                    "why": "最新期の1株利益が、前の年と同じ値です（更新遅れの疑い）。EPSの成長率が、実際より低く（または高く）計算されている可能性があります",
                    "how": how_eps})
    elif kind in ("mismatch", "shift"):
        msg = {"mismatch": "最新期の1株利益が、純利益÷平均株式数と大きく食い違っています（株式分割の反映ずれ・訂正前の値などの疑い）。"
                           "ただし、Yahoo側の純利益・株式数の方がずれていて、1株利益は正しい場合も多くあります",
               "shift": "最新期の1株利益×株式数が、最新期ではなく前の年の純利益と一致しています（年別データの並び違いの疑い）"}[kind]
        out.append({"tier": "check", "k": "eps", "where": "EPS（1株利益）の推移", "why": msg, "how": how_eps})
    if jp:
        per = info.get("trailingPE")
        if LC.is_num(per) and per > 0:
            try:
                qr = yd.get("q_rows") or {}
                qni = qr.get("Net Income Common Stockholders") or qr.get("Net Income")
                so = info.get("sharesOutstanding")
                qd = [dt.date.fromisoformat(d) for d in (yd.get("q_dates") or [])[:4]]
                q_ok = len(qd) >= 4 and all(75 <= (qd[i] - qd[i + 1]).days <= 105 for i in range(3))   # 日付が飛んでいる（新規上場直後など）と足しても直近12か月にならない
                if q_ok and qni and len(qni) >= 4 and all(LC.is_num(x) for x in qni[:4]) and LC.is_num(so) and so > 0 and LC.is_num(price) and price > 0:
                    ttm = sum(qni[:4])
                    if ttm > 0:
                        pt = price * so / ttm
                        if not (0.5 <= per / pt <= 2.0):
                            out.append({"tier": "check", "k": "per", "where": f"PER（Yahoo表示 {per:.1f}倍）",
                                        "why": f"直近4四半期の純利益（{ttm / 1e8:,.1f}億円）から求めたPERは{pt:.1f}倍で、Yahoo表示と{per / pt:.1f}倍ずれています。"
                                               "ただし、直近の大きな利益がYahooに未反映の場合や、一時的な損益の場合もあります",
                                        "how": f"IR BANK（irbank.net/{code}/quarter）の四半期の純利益と、決算短信の直近4四半期の純利益を確認する"})
            except Exception:
                pass
        pbr = info.get("priceToBook")
        if LC.is_num(pbr) and pbr > 0:
            try:
                eq = analyze.row(bsr, "Stockholders Equity", "Common Stock Equity")
                so = info.get("sharesOutstanding")
                if eq and LC.is_num(eq[0]) and eq[0] > 0 and LC.is_num(so) and so > 0 and LC.is_num(price) and price > 0:
                    pc = price / (eq[0] / so)
                    if not (0.6 <= pc / pbr <= 1.6):
                        qp = _quarter_pbr_jp(code, price, so)   # 決算期末の古い純資産が原因のことがあるため、直近四半期で再確認
                        if not (qp is not None and 0.6 <= qp / pbr <= 1.6):
                            out.append({"tier": "check", "k": "pbr", "where": f"PBR（Yahoo表示 {pbr:.2f}倍）",
                                        "why": f"株価÷（純資産÷発行済株式数）で求めたPBRは{pc:.2f}倍で、Yahoo表示と{pc / pbr:.2f}倍ずれています。"
                                               "ただし、照合ではYahoo側のPBRが正しい場合が多くありました（株式数のデータのずれなど）",
                                        "how": f"IR BANK（irbank.net/{code}/results）の1株純資産（BPS）で、株価÷BPSを計算して確認する"})
            except Exception:
                pass
    return _apply_data_check(out, market, code, applied)


def _apply_data_fix(yd, market, code):
    """確認してYahooの誤りが分かった値を、IR BANK・SECの値に置き換えた取得データを返す（採点の入口で使う）。
    元の yd は変えない。data_check_long.json の status=error かつ fix のある項目だけが対象。
      eps ：fix.eps（新しい年が先頭。fy, fy-1, … の年に対応）で「Basic EPS」を、その年の欄だけ置き換える（無い年はYahooのまま）。fix.eps_diluted があれば「Diluted EPS」はそちら
      eps_div：Yahooの1株利益が分割前のまま残っているとき、全年を factor で割る（expect＝Yahooの最新期の値。違えばYahooが直したとみて適用しない）
      pbr ：info の priceToBook を、その日のYahooの株価 ÷ fix.bvps（分割後の1株純資産）で求め直す
      fy：置き換えの基準になる決算期。Yahooの最新期（is_years の先頭）がこの年のときだけ置き換える
          （新しい決算が出て最新期が進んだら、古い値で新しい期を上書きしないよう、自動で止まる）
      valid_after：この日以降だけ置き換える（株価が分割後の基準になる日。それ以前は元のまま）
    返り値: (yd2, applied)。applied＝{k: 画面に出す説明}（置き換えが無ければ {}）。"""
    cfg = (LC.load_data_check().get(market) or {}).get(str(code)) or {}
    applied = {}
    if not cfg:
        return yd, applied
    isr = dict(yd.get("is_rows") or {})
    info = dict(yd.get("info") or {})
    today = dt.date.today()
    for k, c in cfg.items():
        fx = c.get("fix") if isinstance(c, dict) else None
        if not fx or c.get("status") != "error":
            continue
        va = fx.get("valid_after")
        if va and today < dt.date.fromisoformat(va):
            continue
        years = list(yd.get("is_years") or [])
        fy = fx.get("fy")
        if fy is None or not years or years[0] != fy:
            continue                                   # Yahooの最新期が、確認した決算期と違う＝置き換えない
        if k == "eps" and fx.get("eps_div"):
            f, exp = fx["eps_div"].get("factor"), fx["eps_div"].get("expect")
            lab0 = next((lb for lb in ("Basic EPS", "Diluted EPS") if isr.get(lb) is not None and len(isr[lb]) == len(years)), None)
            e0 = isr[lab0][0] if lab0 else None
            if f and exp and LC.is_num(e0) and abs(e0 / exp - 1) < 0.01:
                for lab in ("Basic EPS", "Diluted EPS"):
                    cur = isr.get(lab)
                    if cur is not None and len(cur) == len(years):
                        isr[lab] = [(x / f if LC.is_num(x) else x) for x in cur]
                applied[k] = f"1株利益（全年）を分割比{f:g}で割って、分割後の基準にそろえて採点"
        elif k == "eps" and fx.get("eps"):
            done = False
            for lab, vals in (("Basic EPS", fx["eps"]), ("Diluted EPS", fx.get("eps_diluted") or fx["eps"])):
                cur = isr.get(lab)
                if cur is not None and len(cur) == len(years):
                    cur = list(cur)
                    for i, v in enumerate(vals):
                        if (fy - i) in years:
                            cur[years.index(fy - i)] = v
                    isr[lab] = cur
                    done = True
            if done:
                v0 = fx["eps"][0]
                unit = "ドル" if market == "us" else "円"
                applied[k] = (f"1株利益を、確認した値に置き換えて採点（最新期 {v0:,.2f}{unit}"
                              + (f"、{len(fx['eps'])}年分）" if len(fx["eps"]) > 1 else "）"))
        elif k == "pbr" and fx.get("bvps"):
            price = yd.get("price")
            if LC.is_num(price) and price > 0 and fx["bvps"] > 0:
                info["priceToBook"] = price / fx["bvps"]
                applied[k] = f"PBRを、株価÷1株純資産{fx['bvps']:,.2f}円＝{price / fx['bvps']:.2f}倍に置き換えて採点"
    if not applied:
        return yd, applied
    yd2 = dict(yd)
    yd2["is_rows"] = isr
    yd2["info"] = info
    return yd2, applied


def _apply_data_check(notices, market, code, applied=None):
    """手作業の確認結果（data_check_long.json）を反映する。表示専用で、採点には影響しない。
    ok（Yahooが正しい）＝その検出を外す／error（Yahooの誤りを確認）＝tier を confirmed にして確認結果を添える／
    unclear（判定できず）＝tier はそのまま、確認結果を添える。"""
    cfg = (LC.load_data_check().get(market) or {}).get(str(code)) or {}
    if not cfg:
        return notices
    out = []
    seen_k = set()
    for d in notices:
        c = cfg.get(d.get("k"))
        if not (isinstance(c, dict) and c.get("status") in ("ok", "error", "unclear")):
            out.append(d)
            continue
        if c["status"] == "ok":
            continue
        seen_k.add(d.get("k"))
        d2 = dict(d)
        d2["confirm"] = {"status": c["status"], "note": c.get("note", ""), "date": c.get("date", "")}
        if c["status"] == "error":
            d2["tier"] = "confirmed"
            if applied and applied.get(d.get("k")):
                d2["confirm"]["replaced"] = applied[d["k"]]
        out.append(d2)
    # 自動チェックでは検出されなかったが、手作業の確認で誤りが分かって置き換えた項目も、確認結果として載せる
    wh = {"eps": "EPS（1株利益）の推移", "per": "PER", "pbr": "PBR"}
    for k, txt in (applied or {}).items():
        c = cfg.get(k)
        if k in seen_k or not isinstance(c, dict) or c.get("status") != "error":
            continue
        out.append({"tier": "confirmed", "k": k, "where": wh.get(k, k),
                    "why": "自動チェックには引っかかりませんでしたが、手作業の確認でYahooの値の誤りが分かりました",
                    "how": "", "confirm": {"status": "error", "note": c.get("note", ""), "date": c.get("date", ""), "replaced": txt}})
    return out


def _raw_lease_quality(yd, info):
    """リース・信販（日本株のその他金融業）の専用品質スコアの生の値（配当株ツールの raw_lease_quality_jp のコピー）。
    ROA(%)＝純利益÷総資産（期末）・総資産成長率(CAGR %。資金調達型のみ)・利払い余力(倍)＝（税引前利益＋支払利息）÷支払利息（上限500倍）。
    自己資本比率が30%未満＝資金調達型（借入で資産を積む）、以上＝非調達型。"""
    cfg = LC.load_lease_cfg()
    isr, bsr = yd.get("is_rows") or {}, yd.get("bs_rows") or {}
    ni = analyze.row(isr, "Net Income", "Net Income Common Stockholders") or []
    ta = analyze.row(bsr, "Total Assets") or []
    eq = analyze.row(bsr, "Stockholders Equity", "Common Stock Equity") or []
    ie = analyze.row(isr, "Interest Expense", "Interest Expense Non Operating") or []
    pt = analyze.row(isr, "Pretax Income") or []

    def g0(L):
        return L[0] if L and LC.is_num(L[0]) else None
    n0, t0, e0, i0, p0 = g0(ni), g0(ta), g0(eq), g0(ie), g0(pt)
    roa = (n0 / t0 * 100.0) if (n0 is not None and t0 and t0 > 0) else None
    eq_ratio = (e0 / t0 * 100.0) if (e0 is not None and t0 and t0 > 0) else None
    cover = None
    if p0 is not None and i0 is not None and abs(i0) > 0:
        cover = min((p0 + abs(i0)) / abs(i0), cfg.get("int_cover_cap", 500.0))
    lender = (eq_ratio is not None and eq_ratio < cfg.get("equity_ratio_lender_max", 30.0))
    return {"roa": roa, "asset_growth": (analyze.cagr_of(ta) if lender else None), "int_cover": cover,
            "lender": lender, "eq_ratio": eq_ratio}


def _raw_financial_quality(yd, info, bank=False):
    """銀行・保険・証券（is_simple かつ REIT でない）向けの品質サブスコアの素材。
    ROE・増収率(売上高CAGR)・EPS成長率(CAGR)・利益の安定度(純利益の最大減益率)。
    bank=True（日本株の銀行業）のときは、経費率（営業経費÷総収益）と貸出金の成長率（Net Loan のCAGR）も足す
    （配当株ツールの raw_financial_quality_jp(bank=True) のコピー）。"""
    isr = yd.get("is_rows") or {}
    rev = analyze.row(isr, "Total Revenue") or []
    ni = analyze.row(isr, "Net Income") or []
    eps = analyze.row(isr, "Diluted EPS", "Basic EPS") or []
    roe = info.get("returnOnEquity")
    out = {
        "roe": (roe * 100.0 if LC.is_num(roe) else None),
        "rev_growth": analyze.cagr_of(rev),
        "eps_growth": analyze.cagr_of(eps),
        "profit_stability": LC.worst_yoy_decline_pct(ni),
        "rev_series": rev, "eps_series": eps, "ni_series": ni,
    }
    if bank:
        rev_b = analyze.row(isr, "Total Revenue", "Operating Revenue") or []
        opx = analyze.row(isr, "Operating Expense") or []
        loan = analyze.row(yd.get("bs_rows") or {}, "Net Loan") or []
        o0 = opx[0] if opx and LC.is_num(opx[0]) else None
        r0 = rev_b[0] if rev_b and LC.is_num(rev_b[0]) and rev_b[0] > 0 else None
        out["cost_ratio"] = (abs(o0) / r0 * 100.0) if (o0 is not None and r0) else None
        out["loan_growth"] = analyze.cagr_of(loan) if len([x for x in loan if LC.is_num(x)]) >= 3 else None
        out["_bank"] = True
    return out


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


def _recent_quarter_decel_us(yd, rev_cagr):
    """直近四半期（対前年同期比）が年次の増収トレンドから大きく下振れ、かつ営業利益
    （取れなければ純利益）が前年同期比マイナスの場合にのみ、業績グループへの減点係数
    （1.0〜0.6）を返す。米国株専用（日本株はyfinanceの四半期データがEPS2列のみ・
    符号なしの極端な振れが多く、信頼できるしきい値を引けないため今回は対象外＝常に1.0）。
    売上高は営業利益・純利益と違って四半期ごとの振れが小さく、低base効果による異常値も
    出にくいため「下げ止まりの判定」の主軸に使い、営業利益・純利益は「実際に減益方向か」
    の確認用（両方そろって初めて発動＝片方だけのブレでは減点しない）。
    戻り値: (factor, detail dict or None)。"""
    qr = yd.get("q_rows") or {}

    def qrow(*labels):
        for lb in labels:
            if lb in qr:
                return qr[lb]
        return None

    def yoy(s):
        if s and len(s) >= 5 and LC.is_num(s[0]) and LC.is_num(s[4]) and s[4] != 0:
            return (s[0] / s[4] - 1) * 100
        return None

    rev_yoy = yoy(qrow("Total Revenue", "Operating Revenue"))
    profit_yoy = yoy(qrow("Operating Income", "Total Operating Income As Reported", "EBIT"))
    if profit_yoy is None:
        profit_yoy = yoy(qrow("Net Income", "Net Income Common Stockholders"))

    if rev_yoy is None or profit_yoy is None or not LC.is_num(rev_cagr):
        return 1.0, None
    gap = rev_yoy - rev_cagr  # 年次の増収率に対して直近四半期がどれだけ下振れているか（pt）
    detail = {"rev_yoy_q": rev_yoy, "profit_yoy_q": profit_yoy, "gap": gap}
    if profit_yoy >= 0 or gap >= -5:
        return 1.0, detail
    frac = min(1.0, (-5 - gap) / 20.0)  # gap: -5で0・-25以上で1（線形補間）
    factor = 1.0 - frac * 0.4  # 1.0（無補正）〜0.6（最大40%減点）
    return factor, detail


def _recent_quarters_trend_us(yd):
    """直近四半期（最大4件・古い順）の売上高・営業利益（取れなければ純利益）の推移を、
    _metric_details_html にそのまま渡せる「行」の形で返す（採点はしない参考表示・米国株専用）。
    戻り値: [row, ...]（データが取れた指標だけ。0件ならは空リスト）。"""
    qr = yd.get("q_rows") or {}
    qd = yd.get("q_dates") or []

    def qrow(*labels):
        for lb in labels:
            if lb in qr:
                return qr[lb]
        return None

    def qlabel(d):
        try:
            y, m = d[2:4], int(d[5:7])
            return f"{y}Q{(m - 1) // 3 + 1}"
        except Exception:
            return d

    def build_row(name, s, key):
        pairs = [(qlabel(d), v) for d, v in zip(qd, s or []) if LC.is_num(v)][:4][::-1]
        if len(pairs) < 2:
            return None
        yoy = None
        if s and len(s) >= 5 and LC.is_num(s[0]) and LC.is_num(s[4]) and s[4] != 0:
            yoy = (s[0] / s[4] - 1) * 100
        disp = " ← ".join(analyze._fmt_usd_big(v) for _, v in pairs[::-1])
        ref = (f"直近四半期は前年同期比 {yoy:+.1f}%（参考値・品質スコアの採点には使わず、"
               "急減速の判定にのみ利用）" if LC.is_num(yoy) else "参考値（採点には使いません）")
        return {"name": name, "v": None, "disp": disp, "ref": ref, "key": key,
                "series": pairs, "series_kind": "usd"}

    rows = [build_row("売上高（直近四半期）", qrow("Total Revenue", "Operating Revenue"), "q_rev_trend_us"),
            build_row("営業利益（直近四半期）",
                      qrow("Operating Income", "Total Operating Income As Reported", "EBIT"),
                      "q_opinc_trend_us")]
    if rows[1] is None:
        rows[1] = build_row("純利益（直近四半期）",
                            qrow("Net Income", "Net Income Common Stockholders"), "q_netinc_trend_us")
    return [r for r in rows if r]


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


# ---------------------------------------------------------------- クリックで開く説明（表示専用の3欄）
# 「直近決算とアナリスト予想」「参考（採点しない）」「見方を深める指標」の各行に付ける。
# 見出しの先頭が一致した最初の項目の説明を使う（見出しに日付などが付く行があるため）。
_EARN_HELP = (
    ("直近決算 EPS", "<b>意味：</b>EPS（1株あたり利益）の、直近の決算での実績です。「事前予想」は決算発表の前にアナリストが"
     "出していた平均の予想、「サプライズ」は実績が予想より何％上振れ・下振れたかです（（実績−予想）÷予想の絶対値）。"
     "日本株は、四半期のEPSが開示EPSとほぼ同じなら、前年同期比をこの行に合わせて表示します。"
     "<br><b>見方：</b>プラスなら予想より良い決算、マイナスなら予想より悪い決算です。±5％程度までは誤差の範囲で、"
     "一度の上振れ・下振れより、続けて予想を超えているかを見ます。この数字は採点に使っていません。"),
    ("直近四半期 EPS", "<b>意味：</b>直近の四半期決算での、EPS（1株あたり利益）の実績です。「事前予想」は決算発表の前に"
     "アナリストが出していた平均の予想、「サプライズ」は実績が予想より何％上振れ・下振れたかです。"
     "<br><b>見方：</b>プラスなら予想より良い決算、マイナスなら予想より悪い決算です。±5％程度までは誤差の範囲で、"
     "一度の結果より、続けて予想を超えているかを見ます。この数字は採点に使っていません。"),
    ("直近四半期", "<b>意味：</b>最新の四半期決算の、売上高・営業利益・純利益と、前年の同じ四半期と比べた増減率です。"
     "日本株は取得できる項目が少なく、EPSだけになることがあります。"
     "<br><b>見方：</b>四半期は季節による振れが大きいので、直前の四半期ではなく前年の同じ四半期と比べます。"
     "売上高・利益がともに前年を上回っているか、片方だけかを見ます。この数字は採点に使っていません"
     "（米国株の品質スコアには、急な減速を見つける別の判定があります）。"),
    ("決算の評価（機械判定）", "<b>意味：</b>前年同期比の増収・減収、増益・減益と、サプライズが±5％以上かどうかから、"
     "機械的に付けた一言です。<br><b>見方：</b>ごく簡単な目安です。決算の中身（一時的な損益、見通しの修正、"
     "セグメントごとの動き）は、決算短信・決算資料で確かめてください。"),
    ("今後の見通し", "<b>意味：</b>アナリストの平均予想です。今期・来期の予想EPS、前期からの伸び率、予想PER"
     "（現在の株価÷予想EPS）、予想した人数を表示します。会社が出している業績予想とは別のものです。"
     "<br><b>見方：</b>人数が少ない（日本株では特に）と、予想が古い・偏っていることがあります。"
     "予想PERが現在のPERより低ければ、利益の増加が見込まれていることを意味します。この数字は採点に使っていません。"),
    ("次回決算", "<b>意味：</b>次の決算発表の予定日です。<br><b>見方：</b>予定は変わることがあります。"
     "決算の前後は株価が大きく動きやすいので、買う時期を考えるときの目安にします。"),
    ("アナリスト目標株価", "<b>意味：</b>アナリストが「この程度の株価になる」と見ている価格の、平均・最高・最低です。"
     "「現在株価比」は、平均が今の株価よりどれだけ上か下かです。"
     "<br><b>見方：</b>目標株価は楽観的に偏りやすく、外れることも多いので、あくまで参考にとどめます。"
     "高値と安値の差が大きいほど、見方が割れています。この数字は採点に使っていません。"),
    ("アナリスト・レーティング", "<b>意味：</b>アナリストの投資判断の平均です。数字は1が強気（強い買い）〜5が弱気（売り）で、"
     "「対象」は集計に入ったアナリストの人数です。"
     "<br><b>見方：</b>買い推奨が多めに出る傾向があるため、数字が良くても割安とは限りません。"
     "人数が少ないときは参考程度に見ます。この数字は採点に使っていません。"),
)

_REF_HELP = (
    ("市場が織り込むFCF成長率", "<b>意味：</b>いまの株価が妥当だとすると、フリーCF（営業CF−設備投資）が今後10年、"
     "年率で何％ずつ伸びる必要があるかを逆算した値です（割引率10％、10年後以降の成長率2.5％と仮定した簡易計算）。"
     "<br><b>見方：</b>過去のフリーCFの成長率より高ければ、市場が高い期待を織り込んでいる（強気）、"
     "低ければ控えめに見ている（保守的）ということです。前提の置き方で大きく変わる参考値です。"),
    ("予想配当利回り", "<b>意味：</b>1株あたりの予想配当÷現在の株価です。"
     "<br><b>見方：</b>このツールは、配当を評価に使いません。参考として表示するだけです。"),
    ("自己資本比率", "<b>意味：</b>自己資本（返さなくてよいお金）が、総資産の何％かを表します。"
     "<br><b>見方：</b>高いほど借金に頼らない経営です。業種で水準が違いますが、目安として30％以上が安定、"
     "10％未満は債務超過に近づいていて要注意です（足切り基準）。銀行・保険・証券は構造上低く出ます。"),
    ("RSI(14)", "<b>意味：</b>直近14日間の値上がり幅と値下がり幅の比率から、買われすぎ・売られすぎを見る指標です"
     "（0〜100）。<br><b>見方：</b>70超で買われすぎ、30未満で売られすぎが目安です。短期の過熱感を見るもので、"
     "10年保有できるかの判断には使いません。"),
    ("主要ETFの上位15銘柄での順位", "<b>意味：</b>主要なETF（指数に連動する投資信託）の、組み入れ比率の上位15銘柄の中で、"
     "この銘柄が何位か、組み入れ比率はいくらかです。月次で更新します。"
     "<br><b>見方：</b>上位にあるのは、時価総額が大きく、多くの資金が機械的に買っている銘柄というだけです。"
     "事業の質や割安さを示すものではありません。この数字は採点に使っていません。"),
    ("高値からの下落率", "<b>意味：</b>直近のレンジ（表示している期間）の高値から、いまの株価が何％下がっているかです。"
     "<br><b>見方：</b>大きく下がっているときは、割安になった可能性と、悪材料が出ている可能性の両方があります。"
     "下落の理由を決算や報道で確かめてください。この数字は採点に使っていません"
     "（買い時スコアは、別の割安指標で計算しています）。"),
)

_INS_HELP = (
    ("ROE・ROA・財務レバレッジ", "<b>意味：</b>ROEは純利益÷自己資本（株主のお金で何％稼いだか）、ROAは純利益÷総資産"
     "（借入も含めた会社の資産で何％稼いだか）、財務レバレッジは総資産÷自己資本（他人資本をどれだけ使っているか）です。"
     "ROE＝ROA×財務レバレッジの関係があります。"
     "<br><b>見方：</b>ROEが高くてもROAが低く、レバレッジが高いときは、借入で数字が押し上げられている可能性があります"
     "（景気が悪くなると返済の負担が重くなる）。ROEとROAが近いなら、借入に頼らず稼げています。"),
    ("ROEの目安と業種比較", "<b>意味：</b>ROE（純利益÷自己資本）を、目安と、同じ業種の中央値と比べたものです。"
     "<br><b>見方：</b>目安は、8％以上で合格、10％以上で優良、5％未満で低効率です。業種の中央値は、このランキングの母集団内の"
     "同じ業種の銘柄から計算しています。赤字のときは目安に当てはめません。採点には使っていません。"),
    ("ROAの目安と業種比較", "<b>意味：</b>ROA（純利益÷総資産）を、目安と、同じ業種の中央値と比べたものです。"
     "<br><b>見方：</b>目安は、5％以上で良好、3％未満で改善の余地ありです。銀行・保険・証券・REITは構造上低く出るため、"
     "目安をそのまま当てはめず、同業との比較で見ます。採点には使っていません。"),
    ("営業CF÷純利益", "<b>意味：</b>営業CF（本業で実際に入ってきた現金）が、純利益の何倍かです。"
     "<br><b>見方：</b>1倍以上なら、利益が現金でしっかり裏付けられています。1倍を下回る状態が続くときは、"
     "売掛金や在庫が増えて、利益が現金になっていない可能性があります。減価償却費の大きい会社は、1倍を超えるのが普通です。"
     "赤字の年は計算しません。"),
    ("会計発生高", "<b>意味：</b>（純利益−営業CF）÷平均総資産です。利益のうち、現金の裏付けがない部分の大きさを、"
     "会社の規模に対する割合で表します。"
     "<br><b>見方：</b>大きなプラスが続くと、利益が現金に裏付けられていない可能性があります。"
     "マイナスは、現金が利益より多い状態です。一時的な振れは珍しくないので、数年続くかどうかを見ます。"),
    ("設備投資÷減価償却費", "<b>意味：</b>設備投資（工場・設備などの取得額）が、減価償却費（設備の価値の目減り分として"
     "毎年費用に計上する額）の何倍かです。"
     "<br><b>見方：</b>1.1倍を超えると、成長投資や能力増強の可能性があります。0.9倍を下回ると、設備の更新が足りていない"
     "可能性があります。その間は、現状を維持する水準です。業種や事業の段階で解釈が変わります。"),
    ("発行済株式数の変化", "<b>意味：</b>発行済みの株式数が、過去数年でどれだけ増減したかです。"
     "<br><b>見方：</b>減っていれば、自社株買いなどで1株あたりの価値が高まりやすくなります。増えていれば、増資や株式報酬による"
     "希薄化（1株あたりの価値の目減り）の可能性があります。株式分割・併合があると、見かけの数字が動くことがあります。"),
    ("株式報酬（SBC）の重さ", "<b>意味：</b>SBC（株式報酬）は、従業員に株式で支払う報酬で、実質的なコストです。"
     "売上高に対する割合と、フリーCFからSBCを引いたものの売上高比を表示します。"
     "<br><b>見方：</b>SBCは現金が出ていかないため、フリーCFを実態より良く見せることがあります。"
     "SBCを引いても、フリーCFがプラスで残るかを見ます。"),
)


def _help_for(name, table):
    for pre, text in table:
        if name.startswith(pre):
            return text
    return None


def _help_row(name, val, help_html):
    """説明がある行はクリックで開き、無い行は従来どおりの1行表示。"""
    if not help_html:
        return (f'<div class="plain"><span class="mn">{name}</span>'
                f'<span class="mv2">{val}</span></div>')
    return ('<details class="m hl"><summary>'
            f'<span class="mn">{name}</span><span class="mv2">{val}</span>'
            '<span class="mr">クリックで説明</span></summary>'
            f'<div class="mbody"><p class="what">{help_html}</p></div></details>')


def _earn_section(ea, market, base_html):
    """直近決算とアナリスト予想：各行をクリックで説明が開く形にする。
    行の素材（_earn_rows系）と注意書きは analyze / analyze_us のものをそのまま使う。"""
    if not base_html:
        return ""
    if market == "jp":
        rows = analyze._earn_rows(ea)
    else:
        import analyze_us
        rows = analyze_us._earn_rows_us(ea)
    m = re.search(r'<div class="legend">(.*?)</div>', base_html, re.S)
    legend = m.group(1) if m else ""
    esc = analyze.html.escape
    body = "".join(_help_row(esc(str(h)), esc(str(b)), _help_for(str(h), _EARN_HELP)) for h, b in rows)
    return ('<h2>直近決算とアナリスト予想</h2>'
            f'<div class="legend">{legend}（各行をクリックすると説明が開きます）</div>{body}')


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

    earn_html = _earn_section(ctx.get("earn") or {}, market, earn_html)

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
        if fq.get("_bank"):                      # 日本株の銀行業だけ：経費率・貸出金の成長率
            bx = LC.load_fq_cfg()["bank_extra"]["rules"]
            cr, lg = fq.get("cost_ratio"), fq.get("loan_growth")
            cr_s, lg_s = fqp["cost_ratio"][1], fqp["loan_growth"][1]
            cr_w = ("データを取得できませんでした（取れない指標は外して、残りで平均しています）。" if not LC.is_num(cr) else
                    f"経費率 {cr:.1f}%（" + ("良好" if cr <= bx["cost_ratio"]["good"] else "弱い" if cr > bx["cost_ratio"]["warn"] else "やや弱い") + "）。")
            lg_w = ("データを取得できませんでした（取れない指標は外して、残りで平均しています）。" if not LC.is_num(lg) else
                    f"貸出金の成長率 {lg:.1f}%（" + ("良好" if lg >= bx["loan_growth"]["good"] else "弱い" if lg < bx["loan_growth"]["warn"] else "やや弱い") + "）。")
            q_blocks.append(_detail_row(
                "fq_cost_ratio", "経費率（営業経費÷総収益）",
                f"{cr:.1f}%" if LC.is_num(cr) else "算出不可",
                f"{bx['cost_ratio']['good']}%以下＝良好 ／ {bx['cost_ratio']['warn']}%超＝弱い。低いほど効率よく稼げている（銀行業のみ）。",
                cr_w, cr_s,
                figure=(_gauge_svg(cr, bx["cost_ratio"]["good"], bx["cost_ratio"]["warn"], "lower_better", "pct", "経費率")
                        if LC.is_num(cr) else "")))
            q_blocks.append(_detail_row(
                "fq_loan_growth", "貸出金の成長率（年率）",
                f"{lg:.1f}%" if LC.is_num(lg) else "算出不可（データ不足）",
                f"{bx['loan_growth']['good']}%以上＝良好 ／ {bx['loan_growth']['warn']}%未満＝弱い。本業の貸出が伸びているか（銀行業のみ）。",
                lg_w, lg_s,
                figure=(_gauge_svg(lg, bx["loan_growth"]["good"], bx["loan_growth"]["warn"], "higher_better", "pct", "貸出金の成長率")
                        if LC.is_num(lg) else "")))
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
        lease_on = extras.get("lease_q") is not None
        for gname in ("業績", "財務", "キャッシュフロー"):
            if gname == "キャッシュフロー" and lease_on:
                q_blocks.append('<div class="domhead"><b>キャッシュフロー（この業種では採点しない・参考表示）</b></div>'
                                '<div class="legend">リース・信販は、営業CFにリース資産・貸付債権の積み増しが入り、事業が成長するほど赤字に出るため、'
                                '品質スコアには使っていません（下の「リース・信販品質」が代わりに入ります）。</div>')
            else:
                q_blocks.append(f'<div class="domhead"><b>{gname}</b> {analyze.bar(groups.get(gname))}</div>')
            rows = [r for r in (detail.get(gname) or []) if r.get("key")]
            q_blocks += ([mdh(r) for r in rows] if rows else
                         ['<div class="plain"><span class="mn">―</span>'
                          '<span class="mv2">この業種では評価対象外</span></div>'])
            if gname == "業績" and extras.get("quarter_trend_rows"):
                q_blocks += [mdh(r) for r in extras["quarter_trend_rows"]]
        if lease_on:
            lr, lp = extras["lease_raw"], extras["lease_parts"]
            lcfg = LC.load_lease_cfg()["rules"]
            lender = bool(lr.get("lender"))
            kind = "資金調達型（借入で資産を積む。自己資本比率30%未満）" if lender else "非調達型（保証・レンタル・サービスなど。自己資本比率30%以上）"
            eqr = lr.get("eq_ratio")
            eq_txt = ("（この銘柄の自己資本比率 " + format(eqr, ".1f") + "%）") if LC.is_num(eqr) else ""
            q_blocks.append(f'<div class="domhead"><b>リース・信販品質（キャッシュフローの代わり）</b> {analyze.bar(groups.get(LC.LEASE_Q_KEY))}</div>'
                            f'<div class="legend">事業の型：<b>{kind}</b>{eq_txt}。'
                            f'採点できた指標 <b>{extras.get("lease_got")}/{extras.get("lease_poss")}</b>。ROAの基準は型で違います。</div>')

            def _lq_rule(key):
                return lcfg[("roa_lender" if lender else "roa_light") if key == "roa" else key]

            def _lq_why(key, v, lab, unit):
                if not LC.is_num(v):
                    return "データを取得できませんでした（取れない指標は外して、残りで平均しています）。"
                rr = _lq_rule(key)
                w = "良好" if v >= rr["good"] else "弱い" if v < rr["warn"] else "やや弱い"
                return f"{lab} {v:.1f}{unit}（{w}）。"

            def _lq_gauge(key, v, title, kind_="pct"):
                if not LC.is_num(v):
                    return ""
                rr = _lq_rule(key)
                return _gauge_svg(v, rr["good"], rr["warn"], "higher_better", kind_, title)

            rr_roa = _lq_rule("roa")
            q_blocks.append(_detail_row(
                "lq_roa", "ROA（純利益÷総資産）",
                f"{lr['roa']:.2f}%" if LC.is_num(lr.get("roa")) else "算出不可",
                f"{rr_roa['good']}%以上＝良好 ／ {rr_roa['warn']}%未満＝弱い（{'資金調達型' if lender else '非調達型'}の基準）。",
                _lq_why("roa", lr.get("roa"), "ROA", "%"), lp["roa"][1], figure=_lq_gauge("roa", lr.get("roa"), "ROA")))
            if lender:
                rr_g = _lq_rule("asset_growth")
                q_blocks.append(_detail_row(
                    "lq_asset_growth", "総資産の成長率（年率）",
                    f"{lr['asset_growth']:.1f}%" if LC.is_num(lr.get("asset_growth")) else "算出不可",
                    f"{rr_g['good']}%以上＝良好 ／ {rr_g['warn']}%未満＝弱い。資金調達型は成長の源泉が資産の積み増しのため見る。"
                    "ただし与信リスクとは切り離された指標で、買収による成長も高く出る。",
                    _lq_why("asset_growth", lr.get("asset_growth"), "総資産成長率", "%"), lp["asset_growth"][1],
                    figure=_lq_gauge("asset_growth", lr.get("asset_growth"), "総資産の成長率")))
            rr_i = _lq_rule("int_cover")
            q_blocks.append(_detail_row(
                "lq_int_cover", "利払い余力（（税引前利益＋支払利息）÷支払利息）",
                f"{lr['int_cover']:.1f}倍" if LC.is_num(lr.get("int_cover")) else "算出不可（支払利息を取得できない）",
                f"{rr_i['good']}倍以上＝良好 ／ {rr_i['warn']}倍未満＝弱い（上限500倍）。会社によってYahooが一部の利息しか拾わない場合がある。",
                _lq_why("int_cover", lr.get("int_cover"), "利払い余力", "倍"), lp["int_cover"][1],
                figure=_lq_gauge("int_cover", lr.get("int_cover"), "利払い余力", "mul")))
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
    # svg_rangeband は kind="per"（pct以外）だとゾーン注記を「高PER」「低PER」固定で
    # 出す（analyze.py側の元々の想定がPER専用のため）。PBRのグラフではラベルだけ
    # 差し替える（analyze.pyは無改変のまま、ここで文字列置換のみ行う）。
    if pbr_rb_fig:
        pbr_rb_fig = pbr_rb_fig.replace("高PER", "高PBR").replace("低PER", "低PBR")

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
    etf_rows = extras.get("etf_rows") or []
    if etf_rows:
        asof = extras.get("etf_asof") or ""
        txt = "／".join(f"{n}（{lab}）{r}位" + (f"・組入{w:.1f}%" if LC.is_num(w) else "")
                        for n, lab, r, w in etf_rows)
        ref_rows.append(("主要ETFの上位15銘柄での順位",
                         txt + (f"　… {asof}時点（月次更新）" if asof else "")
                         + "。採点しない・参考（どのETFでも、上位15位までの順位と組入比率のみ）"))
    if LC.is_num(extras.get("drawdown")):
        ref_rows.append(("高値からの下落率（採点しない）",
                         f"直近レンジ高値から {extras['drawdown']:.0f}%"))
    ref_html = "".join(
        _help_row(n, v, _help_for(n, _REF_HELP) or analyze.NAME_HELP.get(n))
        for n, v in ref_rows)
    # 見方を深める指標（表示専用・採点しない）
    ins = extras.get("insight") or {}
    if ins.get("rows"):
        ref_html += ('<div class="legend" style="margin-top:12px"><b>見方を深める指標</b>'
                     '（銘柄分析の基本の見方。表示のみで、スコアの計算には使っていません）</div>'
                     + "".join(_help_row(n, v, _help_for(n, _INS_HELP)) for n, v in ins["rows"]))
    # 注意チェック（資料の足切り・バリュートラップ）。該当がなくても「確認した」ことを示す
    chk_html = ""
    if ins:
        fl = ins.get("flags") or {}
        items = []
        for t in fl.get("value_trap") or []:
            items.append(f"<li><b>バリュートラップ注意</b>：{t}</li>")
        if fl.get("lev_roe"):
            items.append("<li><b>借入依存の高ROE</b>：ROEは高いがROAが低く、借入に頼って高く見えている可能性があります。</li>")
        for t in fl.get("cutoff") or []:
            items.append(f"<li><b>足切り基準に該当</b>：{t}</li>")
        if items:
            chk_html = ('<div class="warn"><b>注意して見たい点（採点しない・参考）</b><ul>' + "".join(items)
                        + '</ul><div style="margin-top:6px;font-size:11px;color:var(--muted)">'
                          '目安を機械的に当てはめたもので、売買の判断ではありません。数字の裏にある理由を、'
                          '決算資料で確かめてください。「継続企業の前提に関する注記（GC注記）」の有無は'
                          'このツールでは取得できないため、有価証券報告書・10-Kで確認してください。</div></div>')

    # データ不備の疑い（Yahooの財務データ。表示専用・採点には使わない）
    dn_html = ""
    dn = extras.get("data_notice") or []
    if dn:
        esc = analyze.html.escape
        code_ = meta["code"]
        links = (f'<a href="https://irbank.net/{esc(str(code_))}/results" target="_blank" rel="noopener">IR BANK 決算まとめ</a>'
                 f'・<a href="https://irbank.net/{esc(str(code_))}/quarter" target="_blank" rel="noopener">四半期</a>'
                 '・決算短信（会社のIRページ／東証TDnet）' if market == "jp" else
                 f'<a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;CIK={esc(str(code_))}&amp;type=10-K" '
                 'target="_blank" rel="noopener">SEC EDGAR（10-K）</a>')
        def _dn_li(d):
            cf = d.get("confirm")
            if not cf:
                return (f'<li><b>{esc(d["where"])}</b>：{esc(d["why"])}<br>'
                        f'<span class="dnhow">確認の方法：{esc(d["how"])}</span></li>')
            lab = "確認結果（Yahooの誤りを確認）" if cf.get("status") == "error" else "確認結果（判定できず）"
            rep = (f'<br><span class="dnfix">▶ {esc(cf["replaced"])}</span>' if cf.get("replaced") else "")
            return (f'<li><b>{esc(d["where"])}</b>：{esc(d["why"])}<br>'
                    f'<span class="dnhow">{lab}（{esc(cf.get("date", ""))}）：{esc(cf.get("note", ""))}</span>{rep}</li>')
        for tier, title, cls in (
                ("high", "データに不備の疑い（確度が高い）", "warn"),
                ("confirmed", "Yahooのデータの誤りを確認済み（" + ("IR BANK・決算短信" if market == "jp" else "SECの10-K") + "で確認）", "warn"),
                ("check", "データの確認をおすすめする項目（Yahooの値とのズレ。Yahoo側が正しい場合も多い）", "dnote")):
            its = [d for d in dn if d.get("tier") == tier]
            if not its:
                continue
            dn_html += (f'<div class="{cls}"><b>{title}</b><ul>'
                        + "".join(_dn_li(d) for d in its)
                        + f'</ul><div class="dnfoot">確認先：{links}。'
                        + ('採点は、「▶」を付けた項目だけ確認した値に置き換えて計算しています。それ以外は、Yahooの値をそのまま使っています（表示のみ）。'
                           if any((d.get("confirm") or {}).get("replaced") for d in its) else
                           '採点は、Yahooの値をそのまま使っています（確認して誤りだったときも、点は自動では直りません。表示のみ）。')
                        + '</div></div>')

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
details.m.hl>summary{{grid-template-columns:minmax(150px,1fr) 2fr auto}}
.dnote{{margin:14px 0;padding:10px 12px;background:var(--field);border:1px solid var(--line);border-radius:8px;font-size:12px}}
.dnote ul,.warn ul{{margin:4px 0 0;padding-left:18px}}
.dnhow{{color:var(--muted)}}
.dnfix{{font-weight:700}}
.dnfoot{{margin-top:6px;font-size:11px;color:var(--muted)}}
@media(max-width:600px){{details.m.hl>summary{{grid-template-columns:1fr}}
  details.m.hl>summary .mr{{display:block}}}}
</style></head><body><div class="wrap">
<div class="topbar">
  <h1>{meta['name']}（{meta['code']}）<small>10年保有できる優良企業か</small></h1>
  <span><a href="../index.html">ランキング</a>　・　<a href="../watchlist.html">ウォッチリスト</a>　・　<a href="../portfolio.html">ポートフォリオ</a></span>
</div>
<div class="sub">現在株価 {_f(meta.get('price'),0)}{unit}（{meta.get('price_date') or '―'} 終値）　｜　{_sec_label(seckey, market)}</div>
{ohlc_line}
<div class="formula">配当を評価しないレポートです。<b>品質スコア</b>＝業績×0.28＋財務×0.27＋CF×0.15（取得できたグループで再正規化）。
<b>買い時スコア</b>＝EV/EBIT対業種・FCF利回り・PER割安度・PBR割安度を均等25%で合成（欠損は中立60）。
配当利回り・連続増配・累進配当宣言などは一切見ていません。</div>

<div class="score2">
  <div class="sblk"><div class="hd"><b>品質スコア</b><span class="num {qcls}">{qn}</span>
    <span class="lab">{qlab}</span></div>
    <div class="cov">{'採点できた指標（金融品質の内訳）' if extras.get('fq_parts') else '採点できた指標（REIT品質の内訳）' if extras.get('reit_parts') else '採点できた指標グループ'} <b>{qs}/{qp}</b>　カバレッジ <b>{ql}</b></div></div>
  <div class="sblk"><div class="hd"><b>買い時スコア</b><span class="num {bcls}">{bn}</span>
    <span class="lab">{blab}</span></div>
    <div class="cov">実データで採点 <b>{bs}/{bp}</b>　カバレッジ <b>{bl}</b>（欠損は中立60で補完）</div></div>
</div>
{warn_html}
{chk_html}
{dn_html}

<h2>会社概要</h2>
<div class="cobox">{comp_html}</div>

<h2>① 品質の指標（{'REIT向け代替指標(FFO)' if extras.get('reit_q') else '銀行・保険・証券向け代替指標' if is_fin_simple else '業績・財務・リース・信販向け専用指標（CFは採点しない）' if extras.get('lease_q') is not None else '業績・財務・キャッシュフロー'}）</h2>
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
    # 東証の英字入りコード（285A・268A等）を数字だけに欠けさせない（欠けると285.Tとなり取得に失敗する）
    code = re.sub(r"[^0-9A-Za-z]", "", str(code)).upper() if market == "jp" else str(code).upper()
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
    # 確認してYahooの誤りが分かった値は、IR BANK・SECの値に置き換えて採点する（data_check_long.json の fix）。
    # 「データの確認が必要な銘柄」の検出は、置き換え前のYahooのデータ（yd_raw）で行う。
    yd_raw = yd
    try:
        yd, fix_applied = _apply_data_fix(yd_raw, market, code)
    except Exception:
        yd, fix_applied = yd_raw, {}
    info = yd.get("info") or {}
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
            # 米国の大手証券（GS・MS）：営業CFがブローカーのバランスシートで巨額のマイナスに振れ、通常の
            # 業績・財務・CF採点だと品質が不当に低く出るため、銘柄指定で金融品質（is_simple）に切り替える
            # （financial_quality_long.json の us_force_financial。analyze_us.py 本体は無改変）。
            if (not is_simple) and (not is_reit) and code in LC.us_force_financial_tickers():
                is_simple = True
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
        is_bank_jp = bool(market == "jp" and jp_sector == "銀行業")
        fin_q = _raw_financial_quality(yd, info, bank=is_bank_jp)
        fq_score, fq_parts = LC.financial_quality_score(fin_q, bank=is_bank_jp)
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

    # リース・信販（日本株のその他金融業）：営業CFにリース資産・貸付債権の積み増しが入り、成長するほど赤字に出て、
    # キャッシュフローのグループが最低点に張り付く歪みがあるため、CFを採点せず、専用の品質スコア（lease_quality_long.json）を
    # CFのグループの重み（0.15）で入れる。配当株ツールの同じ見直し（2026-11-06）をコピーして移植。
    lease_raw = lease_q = lease_parts = None
    lease_got = lease_poss = 0
    if market == "jp" and jp_sector == "その他金融業" and not is_simple:
        lease_raw = _raw_lease_quality(yd, info)
        _lq, lease_parts, lease_got, lease_poss = LC.lease_quality_score(lease_raw)
        if _lq is not None:
            lease_q = _lq
            groups = dict(groups)
            groups[LC.LEASE_Q_KEY] = _lq
            groups["キャッシュフロー"] = None

    # 直近四半期の急減速チェック（米国株・通常の業績/財務/CF採点の銘柄のみ）。
    # 年次データだけだと、直近1年以内に始まった急減速が確定決算に反映されるまで
    # （最大1年程度）品質スコアに出ない問題への対応。詳細は_recent_quarter_decel_usを参照。
    decel_factor, decel_detail = 1.0, None
    quarter_trend_rows = []
    if market == "us" and not is_fin_simple and not is_reit_us:
        decel_factor, decel_detail = _recent_quarter_decel_us(yd, rowmap.get("rev_cagr", {}).get("v"))
        if decel_factor < 1.0 and LC.is_num(groups.get("業績")):
            groups = dict(groups)
            groups["業績"] = groups["業績"] * decel_factor
        quarter_trend_rows = _recent_quarters_trend_us(yd)

    if lease_q is not None:
        q_score = LC.quality_score_lease(groups)
        q_cov = LC.quality_coverage_lease(groups)
    elif is_fin_simple or is_reit_us:
        # 銀行・保険・証券／米国REIT：採点できるグループは専用スコア1つだけなので、専用スコア内の指標の取得数で数える
        q_score = LC.quality_score(groups)
        q_cov = LC.quality_coverage_sub(fq_parts if is_fin_simple else reit_parts, skip=LC.bank_extra_keys())
    else:
        q_score = LC.quality_score(groups)
        q_cov = LC.quality_coverage(groups)

    raw = _raw_valuation(yd)
    pbr_pairs = _pbr_pairs(yd, raw)
    bt_score, bt_cov, bt_comp, btd = _buytiming(raw, rowmap, seckey, market, pbr_pairs,
                                                is_fin_simple=is_fin_simple, is_reit=is_reit_us,
                                                reit_q=reit_q, ffo_pairs=ffo_pairs)

    # 見方を深める指標・注意フラグ（表示専用。スコアには使わない）。失敗しても本体の診断は止めない。
    try:
        insight = _insight_extras(yd, market, is_simple, is_reit, q_score, bt_score,
                                  tuple(LC.load_bt_cfg()[f"tim_tiers_{market}"]), raw,
                                  _sector_roe_roa(market).get(seckey), is_lease=(lease_q is not None))
    except Exception:
        insight = {"rows": [], "flags": {}, "raw": {}}
    # データ不備の疑い（表示専用・採点には使わない）。失敗しても本体の診断は止めない。
    try:
        data_notice = _data_notices(yd_raw, market, code, fix_applied)
    except Exception:
        data_notice = []

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
              "reit_q": reit_q, "reit_parts": reit_parts,
              "quarter_trend_rows": quarter_trend_rows, "insight": insight, "data_notice": data_notice,
              "lease_q": lease_q, "lease_raw": lease_raw, "lease_parts": lease_parts,
              "lease_got": lease_got, "lease_poss": lease_poss}
    if market == "us":
        extras["etf_rows"], extras["etf_asof"] = _etf_ranks(code)

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
                        + ("（ROE・増収率・EPS成長率・利益の安定度。銀行は経費率・貸出金の成長率も加える）" if market == "jp"
                           else "（ROE・増収率・EPS成長率・利益の安定度）") + "で評価しています。"
                        + ("営業キャッシュフローが証券会社のバランスシートの増減で巨額のマイナスに振れ、通常の指標だと品質が不当に低く出るため、"
                           "この銘柄は銘柄指定でこの専用スコアを使っています。" if (market == "us" and code in LC.us_force_financial_tickers()) else "")
                        + "買い時スコアもEV/EBIT・FCF利回りが構造的に使えないため、"
                        "PER割安度・PBR割安度のみで合成しています。")
    if lease_q is not None:
        warnings.append("リース・信販（その他金融業）は、営業CFにリース資産・貸付債権の積み増しが入り、事業が成長するほど赤字に出るため、"
                        "品質スコアではキャッシュフローを採点せず、代わりに専用の「リース・信販品質」（ROA・総資産成長率・利払い余力）を使っています。"
                        "貸倒費用・延滞率・調達構成はYahoo Financeで取れないため、補助の評価です。決算短信・有価証券報告書で確認してください。")
    if not yd.get("is_rows"):
        warnings.append("損益計算書を取得できず、業績の評価が限定的です。")
    if not is_fin_simple and not is_reit_us and not LC.is_num(btd["ev_median"]):
        warnings.append("この業種の EV/EBIT 中央値が未算出のため EV/EBIT対業種 は中立扱いです。")
    if decel_detail and decel_factor < 1.0:
        warnings.append(
            "直近四半期の増収率が年次トレンドから大きく下振れ（年率"
            f"{rowmap.get('rev_cagr', {}).get('v', 0):+.1f}%に対し直近四半期は"
            f"{decel_detail['rev_yoy_q']:+.1f}%）、かつ営業利益/純利益も前年同期比マイナス"
            f"（{decel_detail['profit_yoy_q']:+.1f}%）だったため、業績スコアを"
            f"{(1 - decel_factor) * 100:.0f}%減点しています。年次決算にはまだ反映されて"
            "いない直近の変化のため、最新の決算内容もあわせてご確認ください。")

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
                   "金融品質": groups.get("金融品質"), "REIT品質": groups.get("REIT品質"),
                   **({LC.LEASE_Q_KEY: groups.get(LC.LEASE_Q_KEY)} if lease_q is not None else {})},
        "is_fin_simple": is_fin_simple, "is_reit_us": is_reit_us,
        "current_pfo": btd.get("current_pfo"),
        "quarter_decel_factor": decel_factor if decel_factor < 1.0 else None,
        "quarter_decel_detail": decel_detail if decel_factor < 1.0 else None,
        "q_cov": q_cov[2],
        "bt_score": bt_score, "bt_cov": bt_cov[2], "bt_components": bt_comp,
        "ev_ebit": btd["ev_ebit"], "fcf_yield": btd["fcf_yield"],
        "per_band_pos": btd["per_band"], "pbr_band_pos": btd["pbr_band"],
        # 「詳しい条件で絞り込む」フィルタ用（買い時内訳の実数値表示）。ev_ebit_vs_sector＝
        # EV/EBIT÷業種中央値、per/pbr_vs_sector＝実績PER・PBR÷業種平均PER・PBR。
        "ev_ebit_vs_sector": btd["ev_vs"], "per_vs_sector": btd["per_vs"], "pbr_vs_sector": btd["pbr_vs"],
        # ランキング一覧の「騰落率（展開）」欄用（d/w/m/3m/6m/9m/y、単位%）。
        "price_chg": _price_changes(yd.get("hist_d")),
        "div_yield": rowmap.get("div_yield", {}).get("v"),  # 表示のみ（採点には不使用）
        "implied_fcf_growth": ig,
        # 見方を深める指標・注意フラグ（表示専用。採点には使わない）
        "insight": {"flags": insight["flags"], "raw": insight["raw"]},
        **({"data_notice": data_notice} if data_notice else {}),
        **({"data_fix": fix_applied} if fix_applied else {}),
        **({"lease_quality": {"score": lease_q, "lender": bool(lease_raw.get("lender")), "eq_ratio": lease_raw.get("eq_ratio"),
                              "got": lease_got, "possible": lease_poss,
                              "parts": {k: {"raw": v[0], "score": v[1]} for k, v in (lease_parts or {}).items()}}}
           if lease_q is not None else {}),
        "etf_top": ([{"etf": n, "rank": r, "weight": w} for n, _lab, r, w in extras.get("etf_rows", [])]
                    if market == "us" else None),
        # 「詳しい条件で絞り込む」フィルタ用の個別指標。score は0〜110（常に高いほど良い、
        # sector_rules(_us).json の業種別しきい値をrule_for()経由で反映済み）、raw は
        # 表示・CSV出力用の生値。is_fin_simple/is_reit_us銘柄はこれらのキーが元々
        # 採点されないためNone（フィルタ側は欠損=対象外として扱う）。
        "metric_scores": {k: rowmap.get(k, {}).get("score") for k in _FILTER_METRIC_KEYS},
        "metric_raw": {k: rowmap.get(k, {}).get("v") for k in _FILTER_METRIC_KEYS},
        "_generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        res["portfolio_data"] = _portfolio_data(yd)
    except Exception:
        res["portfolio_data"] = {"prices": [], "opens": [], "divs": []}
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
