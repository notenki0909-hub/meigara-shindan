# -*- coding: utf-8 -*-
"""
米国株版・銘柄診断エンジン。日本株版 analyze.py の国に依存しない部品
（財務諸表パース・スコア補間・SVG・連続増配計算 等）を import で再利用し、
米国株固有の部分（.T サフィックス不要／GICS11セクター／通貨USD表示／
米10年国債スプレッド／Total Yield・インタレストカバレッジの採点化／
英語ラベル）を上書きする。

設計方針（2026-09-05・ユーザーと確認）：
- 生きている日本株ツール（analyze.py）には触らない。fetch_yf に suffix 引数を
  1つ足しただけ。
- 外部の「配当貴族/王リスト」には依存しない。連続非減配年数は yfinance の
  配当履歴から nocut_streak_rate() で自前計算する（build_universe_us.py と共有）。
- 設定は _us.json（sector_rules_us.json / sector_averages_us.json /
  sector_map_us.json）。dividend_policy.json 相当は不要。

進捗（このファイル）：
  [x] load_config_us / classify_sector_us / fmt_usd / nocut_streak_rate
  [ ] build_metrics_us（指標計算。analyze.build_metrics を米国株向けに）
  [ ] generate_us（診断のエントリ）
  [ ] render_html_us / render_md_us（英語・USD）
"""
import datetime as dt
import os

import analyze
from analyze import (  # 国に依存しない再利用部品
    is_num, safe_div, cagr, fmt_num, fmt_pct, stmt_to_rows, row,
    clean_dps_series, _one_streak, find_last_cut, dividend_streaks,
    yearly_price_mean, calc_technicals, nice_ticks,
)

HERE = os.path.dirname(os.path.abspath(__file__))
TODAY = dt.date.today()

# 日本版 analyze.py の指標説明テーブルに、米国株だけで採点する指標を追記
# （追加のみ。日本版のMには interest_coverage / total_yield キーが出ないため無害）。
analyze.METRIC_HELP.setdefault("interest_coverage", {
    "what": "本業の利益（EBIT）で支払利息を何倍まかなえるかを見る指標。例えばEBIT100億ドル・"
            "支払利息10億ドルなら10倍＝利払いに十分な余裕がある。D/Eレシオが『借金の大きさ』を"
            "見るのに対し、こちらは『今の利益でその借金の利払いをどれだけ楽に賄えているか』を見る。"
            "金利が上がるほど利払い負担が重くなるため、金利上昇局面で重要性が増す。米国株では"
            "財務グループの採点対象。目安は10倍以上で余裕、3倍未満は要注意。", "unit": "倍"})
analyze.METRIC_HELP.setdefault("total_yield", {
    "what": "配当利回りだけでなく、自社株買い額を時価総額で割った『自社株買い利回り』も合算した"
            "数字。例えば配当利回り3%＋自社株買い利回り2%＝総還元利回り5%。米国企業は自社株買いに"
            "よる株主還元が大きく、配当利回りだけ見ると還元姿勢を見落とすため、米国株では配当の"
            "持続力グループの採点対象にしている。自社株買いは単年の実施額で毎年続くとは限らない"
            "点に注意（1年に頼りすぎない）。", "unit": "%"})
analyze.KEY_DOMAIN.setdefault("interest_coverage", "財務")
analyze.KEY_DOMAIN.setdefault("total_yield", "配当")


# ---------------------------------------------------------------- 設定
_CFG_US_CACHE = {}


def load_config_us(force=False):
    """米国株版の3つのJSONをまとめて読む。プロセス内キャッシュ。"""
    if _CFG_US_CACHE and not force:
        return _CFG_US_CACHE
    _CFG_US_CACHE.clear()
    _CFG_US_CACHE.update(
        sec_avg_all=analyze.load_json("sector_averages_us.json"),
        smap=analyze.load_json("sector_map_us.json"),
        rules=analyze.load_json("sector_rules_us.json"),
    )
    return _CFG_US_CACHE


# ---------------------------------------------------------------- 通貨表示
def fmt_usd(v):
    """USDの大きな金額を B（十億）／M（百万）表記に。"""
    if not is_num(v):
        return "—"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}${a/1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a/1e6:,.0f}M"
    if a >= 1e3:
        return f"{sign}${a/1e3:,.1f}K"
    return f"{sign}${a:,.0f}"


# ---------------------------------------------------------------- 業種
def classify_sector_us(info, smap):
    """yfinance の sector（Yahoo系11分類）を GICS11 正式名へ。
    Financials のうち銀行・保険・与信系（industry で判定）は簡易判定に落とす。
    返り値: (gics_sector, industry, yahoo_sector, is_simple, is_reit, src)
    """
    industry = (info.get("industry") or "").strip()
    ysector = (info.get("sector") or "").strip()
    gics = smap["sector_rename"].get(ysector)
    src = "sector_rename"
    if not gics:
        # フォールバック：既に GICS 正式名で来ている場合はそのまま
        known = {
            "Information Technology", "Health Care", "Financials",
            "Consumer Discretionary", "Consumer Staples", "Materials",
            "Communication Services", "Industrials", "Energy", "Utilities",
            "Real Estate",
        }
        if ysector in known:
            gics, src = ysector, "sector（既にGICS名）"
        else:
            gics, src = "Industrials", "既定（分類不明）"

    is_reit = "REIT" in industry.upper() or gics == "Real Estate"
    fin_like = industry in set(smap.get("financial_like_industries", []))
    is_simple = bool(fin_like or is_reit)
    return gics, industry, ysector, is_simple, is_reit, src


# ---------------------------------------------------------------- 連続非減配年数
def nocut_streak_rate(divs, max_years=60):
    """連続非減配年数を『1回あたり配当額の前年同期比』で数える。
    米国株は決算期・支払スケジュールが会社ごとにバラバラで、暦年/会計年度で
    まとめると支払タイミングのズレで偽の減配が出る（build_universe_us.py と
    同じロジック。詳細分析用に max_years を長め＝25年に）。"""
    if not divs:
        return 0
    ds = sorted(divs)
    anchor0 = ds[-1][0]
    rates = []
    for k in range(max_years + 1):
        anchor = anchor0 - dt.timedelta(days=365 * k)
        best = min(ds, key=lambda p: abs((p[0] - anchor).days))
        if abs((best[0] - anchor).days) > 120:
            break
        rates.append(best[1])
    s = 0
    for i in range(len(rates) - 1):
        if rates[i] >= rates[i + 1] * 0.995:
            s += 1
        else:
            break
    return s


def up_streak_rate(divs, max_years=60):
    """連続増配年数（前年同期比で増えている年を数える）。横ばいで打ち切り。"""
    if not divs:
        return 0
    ds = sorted(divs)
    anchor0 = ds[-1][0]
    rates = []
    for k in range(max_years + 1):
        anchor = anchor0 - dt.timedelta(days=365 * k)
        best = min(ds, key=lambda p: abs((p[0] - anchor).days))
        if abs((best[0] - anchor).days) > 120:
            break
        rates.append(best[1])
    s = 0
    for i in range(len(rates) - 1):
        if rates[i] > rates[i + 1] * 1.005:
            s += 1
        else:
            break
    return s


# ---------------------------------------------------------------- fetch
def fetch_yf_us(ticker):
    """analyze.fetch_yf を suffix='' で呼ぶだけ（.T を付けない）。"""
    return analyze.fetch_yf(ticker, suffix="")


# ---------------------------------------------------------------- 指標計算
def build_metrics_us(yd, sec_avg, is_simple, gics_sector, rate_sensitive, ust_10y):
    """全指標を分野別 dict に。analyze.build_metrics の米国株版。
    - 通貨表示は fmt_usd、ラベルは英語。
    - IR BANK は使わない。div_policy（累進配当宣言リスト）も無し。
    - interest_coverage を財務グループで、total_yield を配当グループで採点化。
    - 連続増配/非減配は rate 方式（決算期のズレに強い）で計算。
    返り値: (M, flags, ctx)  M のキーは analyze 版と同じ（業績/財務/キャッシュフロー/配当/期待/参考）。
    """
    info = yd["info"]
    isr, bsr, cfr = yd["is_rows"], yd["bs_rows"], yd["cf_rows"]
    price = yd["price"]
    M = {"業績": [], "財務": [], "キャッシュフロー": [], "配当": [], "期待": [], "参考": []}
    flags = []

    is_years = yd.get("is_years", []) or []
    cf_years = yd.get("cf_years", []) or []
    bs_years = yd.get("bs_years", []) or []

    def sdisp(s):
        if not s:
            return "―"
        return " ← ".join(fmt_usd(x) for x in s if x is not None)[:120]

    def spairs(s, years):
        if not s:
            return []
        if years and len(years) == len(s):
            pr = [(y, v) for y, v in zip(years, s) if is_num(v)]
        else:
            pr = [(None, v) for v in s if is_num(v)]
        return pr[::-1]

    # ---- 業績 ----
    rev = analyze.row(isr, "Total Revenue", "Operating Revenue")
    opi = analyze.row(isr, "Operating Income", "Total Operating Income As Reported", "EBIT")
    ni = analyze.row(isr, "Net Income", "Net Income Common Stockholders", "Net Income Continuous Operations")
    eps = analyze.row(isr, "Basic EPS", "Diluted EPS")

    def growth_row(name, s, key, target, years=is_years, kind="usd"):
        xs = [x for x in (s or []) if is_num(x)]
        if len(xs) >= 2:
            c = cagr(xs[-1], xs[0], len(xs) - 1)
            M[target].append({"name": name, "v": c, "disp": sdisp(s),
                              "ref": f"年率 {fmt_pct(c)}（直近{len(xs)}期のデータから算出）", "key": key,
                              "series": spairs(s, years), "series_kind": kind})
        else:
            M[target].append({"name": name, "v": None, "disp": "―", "ref": "データ不足", "key": key})

    growth_row("売上高（推移／年率）", rev, "rev_cagr", "業績")
    xs = [x for x in (eps or []) if is_num(x)]
    if len(xs) >= 2:
        c = cagr(xs[-1], xs[0], len(xs) - 1)
        M["業績"].append({"name": "EPS（推移／年率）", "v": c,
                          "disp": " ← ".join(f"${fmt_num(x, 2)}" for x in eps if x is not None)[:120],
                          "ref": f"年率 {fmt_pct(c)}（直近{len(xs)}期のデータから算出）", "key": "eps_cagr",
                          "series": spairs(eps, is_years), "series_kind": "eps_usd"})
    else:
        M["業績"].append({"name": "EPS（推移／年率）", "v": None, "disp": "―", "ref": "データ不足", "key": "eps_cagr"})

    op_margin = None
    if rev and opi and is_num(rev[0]) and is_num(opi[0]) and rev[0] != 0:
        op_margin = opi[0] / rev[0] * 100
    opm_series = []
    for i in range(min(len(rev or []), len(opi or []), len(is_years))):
        if is_num(rev[i]) and is_num(opi[i]) and rev[i] != 0:
            opm_series.append((is_years[i], opi[i] / rev[i] * 100))
    M["業績"].append({"name": "営業利益率（直近）", "v": op_margin, "disp": fmt_pct(op_margin),
                      "ref": "業種により水準が違う", "key": "op_margin",
                      "series": opm_series[::-1], "series_kind": "pct"})

    # 利益の安定度＝営業利益の「最悪の前年比」（1に近いほどブレが小さい）
    earn_stab = None
    oip_chrono = [x for x in (opi or [])[::-1] if is_num(x)]
    if len(oip_chrono) >= 3:
        neg = sum(1 for x in oip_chrono if x <= 0)
        if neg == 0:
            earn_stab = min(oip_chrono[i] / oip_chrono[i - 1] for i in range(1, len(oip_chrono)))
        elif neg == 1 and oip_chrono[-1] > 0:
            earn_stab = 0.62
        else:
            earn_stab = 0.0
    es_disp = ("―" if earn_stab is None else
               "1期赤字→回復（0.62）" if earn_stab == 0.62 else
               "赤字期あり（0.00）" if earn_stab == 0 else f"最悪の前年比 {earn_stab:.2f}")
    M["業績"].append({"name": "利益の安定度（営業利益のブレ）", "v": earn_stab, "disp": es_disp,
                      "ref": "1.00に近いほど減益年がない（安定）。0.88以上で安定・0.65未満は景気敏感。"
                             "1期だけ赤字→回復は一律0.62点、複数期赤字・直近赤字は最低の0点",
                      "key": "earnings_stability"})

    growth_row("営業利益（推移／年率）", opi, None, "参考")
    growth_row("当期純利益（推移／年率）", ni, None, "参考")

    # ---- Balance sheet / financial strength ----
    ta = analyze.row(bsr, "Total Assets")
    eq = analyze.row(bsr, "Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest")
    tdebt = analyze.row(bsr, "Total Debt")
    ndebt = analyze.row(bsr, "Net Debt")
    cash = analyze.row(bsr, "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments")
    ta0 = ta[0] if ta else None
    eq0 = eq[0] if eq else None
    td0 = tdebt[0] if tdebt else None
    cash0 = cash[0] if cash else None
    nd0 = ndebt[0] if ndebt else (td0 - cash0 if is_num(td0) and is_num(cash0) else None)

    equity_ratio = safe_div(eq0, ta0)
    equity_ratio = equity_ratio * 100 if equity_ratio is not None else None
    de = safe_div(td0, eq0)
    net_de = safe_div(nd0, eq0)
    debt_ratio = safe_div(td0, ta0)
    debt_ratio = debt_ratio * 100 if debt_ratio is not None else None

    ocf = analyze.row(cfr, "Operating Cash Flow")
    ocf0 = ocf[0] if ocf else None
    debt_to_ocf = safe_div(td0, ocf0)

    # ROIC (vs sector) = EBIT*(1-tax) / invested capital
    roic = None
    ebit0 = opi[0] if opi and is_num(opi[0]) else None
    inv = analyze.row(bsr, "Invested Capital")
    inv0 = inv[0] if inv and is_num(inv[0]) else (
        (td0 + eq0 - cash0) if all(is_num(x) for x in (td0, eq0, cash0)) else None)
    taxp = analyze.row(isr, "Tax Provision")
    ptx = analyze.row(isr, "Pretax Income")
    tax_rate = 0.21
    if taxp and ptx and is_num(taxp[0]) and is_num(ptx[0]) and ptx[0] > 0:
        tr = taxp[0] / ptx[0]
        if 0.05 <= tr <= 0.45:
            tax_rate = tr
    if is_num(ebit0) and is_num(inv0) and inv0 > 0:
        roic = ebit0 * (1 - tax_rate) / inv0 * 100
    roic_vs = safe_div(roic, sec_avg.get("roic"))

    # Interest coverage = EBIT / |interest expense|  (SCORED for US, in 財務)
    ebit_row = analyze.row(isr, "EBIT") or opi
    int_exp = analyze.row(isr, "Interest Expense", "Interest Expense Non Operating")
    icr = None
    if ebit_row and is_num(ebit_row[0]) and int_exp and is_num(int_exp[0]) and int_exp[0] != 0:
        icr = ebit_row[0] / abs(int_exp[0])
    if not int_exp or not is_num(int_exp[0]) or int_exp[0] == 0:
        icr_disp = "無借金またはデータなし"
    elif is_num(icr) and icr < 0:
        icr_disp = f"営業損益が赤字（利払い以前の問題。参考値 {icr:.1f}倍）"
    elif is_num(icr) and icr > 100:
        icr_disp = f"100倍超（支払利息がごく僅か＝実質無借金水準。参考値 {icr:.0f}倍）"
    elif is_num(icr):
        icr_disp = f"{icr:.1f}倍"
    else:
        icr_disp = "データなし"

    M["財務"].append({"name": "D/Eレシオ（有利子負債÷自己資本）", "v": de,
                      "disp": fmt_num(de, 2) + "倍" if is_num(de) else "―",
                      "ref": "1倍未満が安全圏（公益・REITは高め正常）", "key": "de"})
    M["財務"].append({"name": "ネットD/Eレシオ", "v": net_de,
                      "disp": fmt_num(net_de, 2) + "倍" if is_num(net_de) else "―",
                      "ref": "現金控除後。マイナス＝実質無借金", "key": "net_de"})
    M["財務"].append({"name": "有利子負債 ÷ 営業CF（返済年数の目安）", "v": debt_to_ocf,
                      "disp": fmt_num(debt_to_ocf, 1) + "年" if is_num(debt_to_ocf) else "―",
                      "ref": "数年以内に返せる水準か", "key": "debt_to_ocf"})
    M["財務"].append({"name": "インタレストカバレッジレシオ（EBIT÷支払利息）", "v": icr, "disp": icr_disp,
                      "ref": "10倍以上で余裕、3倍未満は利払い負担に注意。金利上昇局面で重要性が増す（米国株は採点対象）", "key": "interest_coverage"})
    M["参考"].append({"name": "自己資本比率（自己資本÷総資産）", "v": None, "disp": fmt_pct(equity_ratio),
                      "ref": "米国では非標準指標のため採点対象外（財務はD/Eで見る）", "key": None})
    M["参考"].append({"name": "ROIC（投下資本利益率）", "v": None,
                      "disp": (fmt_pct(roic, 1) if is_num(roic) else "―"),
                      "ref": (f"業種中央値 {fmt_pct(sec_avg.get('roic'))}（対平均 {fmt_num(roic_vs, 2)}倍）" if roic_vs
                              else "業種平均なし"), "key": None})
    hedge = ("ネットキャッシュ（実質無借金）＝倒産リスク低め" if is_num(nd0) and nd0 < 0 else
             "純有利子負債あり。営業CFでの返済余力を確認" if is_num(nd0) else "―")
    M["参考"].append({"name": "倒産ヘッジ（ネット現金の有無）", "v": None, "disp": hedge, "ref": "", "key": None})

    # ---- キャッシュフロー ----
    icf = analyze.row(cfr, "Investing Cash Flow")
    fin = analyze.row(cfr, "Financing Cash Flow")
    fcf = analyze.row(cfr, "Free Cash Flow")
    capex = analyze.row(cfr, "Capital Expenditure")
    divpaid = analyze.row(cfr, "Cash Dividends Paid", "Common Stock Dividend Paid")
    icf0 = icf[0] if icf else None
    fin0 = fin[0] if fin else None
    fcf0 = fcf[0] if fcf else (ocf0 + capex[0] if is_num(ocf0) and capex and is_num(capex[0]) else None)

    M["キャッシュフロー"].append({"name": "営業CF（直近／推移）",
                                    "v": (1 if is_num(ocf0) and ocf0 > 0 else 0) if is_num(ocf0) else None,
                                    "disp": sdisp(ocf), "ref": "継続してプラス・安定が理想", "key": "ocf_positive",
                                    "series": spairs(ocf, cf_years), "series_kind": "usd"})
    M["参考"].append({"name": "投資CF（直近／推移）", "v": None, "disp": sdisp(icf),
                      "ref": "本業投資でマイナスが通常", "key": None,
                      "series": spairs(icf, cf_years), "series_kind": "usd"})
    M["参考"].append({"name": "財務CF（直近／推移）", "v": None, "disp": sdisp(fin),
                      "ref": "配当・自社株買い・返済でマイナス傾向", "key": None,
                      "series": spairs(fin, cf_years), "series_kind": "usd"})

    buyback_detail = analyze.row(cfr, "Repurchase Of Capital Stock")
    buyback_net = analyze.row(cfr, "Net Common Stock Issuance")
    buyback0, buyback_src = None, None
    if buyback_detail and is_num(buyback_detail[0]):
        buyback0, buyback_src = abs(buyback_detail[0]), "詳細項目（Repurchase Of Capital Stock）"
    elif buyback_net and is_num(buyback_net[0]):
        buyback0 = abs(buyback_net[0]) if buyback_net[0] < 0 else 0.0
        buyback_src = "純額項目（新株発行との差引。個別の金額ではない）"
    mcap = info.get("marketCap")
    buyback_yield = (buyback0 / mcap * 100) if is_num(buyback0) and is_num(mcap) and mcap > 0 else None
    M["参考"].append({"name": "自社株買い（直近期）", "v": buyback0,
                      "disp": (f"{fmt_usd(buyback0)}（時価総額比 {buyback_yield:.2f}%）"
                               if is_num(buyback0) and is_num(buyback_yield) else
                               fmt_usd(buyback0) if is_num(buyback0) else "データなし"),
                      "ref": f"出所：{buyback_src}" if buyback_src else "yfinanceに該当項目なし", "key": None})
    M["キャッシュフロー"].append({"name": "フリーCF（営業CF＋投資CF）",
                                    "v": (1 if is_num(fcf0) and fcf0 > 0 else 0) if is_num(fcf0) else None,
                                    "disp": sdisp(fcf), "ref": "継続プラスなら配当の原資に余裕", "key": "fcf_positive",
                                    "series": spairs(fcf, cf_years), "series_kind": "usd"})
    if is_num(ocf0) and is_num(icf0) and is_num(fin0):
        sg_ = lambda x: "＋" if x > 0 else "－"
        note = "健全型（本業で稼ぎ→投資と株主還元に回す）" if ocf0 > 0 and icf0 < 0 and fin0 < 0 else "要確認"
        M["参考"].append({"name": "CFの符号パターン", "v": None,
                          "disp": f"営業{sg_(ocf0)} / 投資{sg_(icf0)} / 財務{sg_(fin0)} … {note}", "ref": "", "key": None})
    fcf_payout = None
    if divpaid and is_num(divpaid[0]) and is_num(fcf0) and fcf0 > 0:
        fcf_payout = abs(divpaid[0]) / fcf0 * 100
    M["キャッシュフロー"].append({"name": "FCF配当性向（配当支払÷フリーCF）", "v": fcf_payout,
                                    "disp": fmt_pct(fcf_payout),
                                    "ref": "安定企業で70%未満。100%超は取り崩し。REITは対象外（FFOで見る）",
                                    "key": "fcf_payout"})

    # ---- Dividend ----
    divs = yd["divs"]
    fye_ts = info.get("lastFiscalYearEnd")
    fye_month = 12
    if is_num(fye_ts):
        try:
            fye_month = dt.datetime.fromtimestamp(fye_ts, dt.timezone.utc).month
        except Exception:
            fye_month = 12
    yf_fy = analyze.annual_dps_from_divs(divs, fye_month)
    dps_series = clean_dps_series(yf_fy)
    dps_src = "yfinance（会計年度換算）"

    # 連続増配/非減配は rate 方式（決算期のズレに強い）
    streak_up = up_streak_rate(divs)
    streak_flat = nocut_streak_rate(divs)
    dgr5 = None
    if len(dps_series) >= 3:
        vals = [v for _, v in dps_series]
        n = min(5, len(vals) - 1)
        if n >= 2:
            dgr5 = cagr(vals[-1 - n], vals[-1], n)
    # 分割時のゆらぎで dgr が壊れることがある。rate 方式のペア列からも1つ推定して低い方を採る保険
    ds = sorted(divs)
    if ds:
        a0 = ds[-1][0]
        r0 = ds[-1][1]
        r5 = min(ds, key=lambda p: abs((p[0] - (a0 - dt.timedelta(days=365 * 5))).days))
        if abs((r5[0] - (a0 - dt.timedelta(days=365 * 5))).days) <= 150 and r5[1] > 0:
            dgr5_rate = cagr(r5[1], r0, 5)
            if is_num(dgr5) and is_num(dgr5_rate):
                dgr5 = min(dgr5, dgr5_rate) if abs(dgr5) > 60 else dgr5
            elif dgr5 is None:
                dgr5 = dgr5_rate

    fwd_dps = info.get("dividendRate")
    last12 = [v for d0, v in divs if (TODAY - d0).days <= 366]
    last12_sum = sum(last12) if last12 else None
    if not is_num(fwd_dps):
        fwd_dps = last12_sum
    elif is_num(last12_sum) and last12_sum > 0 and fwd_dps < last12_sum * 0.7:
        fwd_dps = last12_sum
    yld_fwd = safe_div(fwd_dps, price)
    yld_fwd = yld_fwd * 100 if yld_fwd is not None else None
    if yld_fwd is None:
        raw = info.get("dividendYield")
        if is_num(raw):
            yld_fwd = raw if raw > 1 else raw * 100

    payout_ni = info.get("payoutRatio")
    payout_ni = payout_ni * 100 if is_num(payout_ni) else None
    if payout_ni is None and eps and is_num(eps[0]) and is_num(fwd_dps) and eps[0] > 0:
        payout_ni = fwd_dps / eps[0] * 100
    roe = info.get("returnOnEquity")
    roe = roe * 100 if is_num(roe) else None

    pm = yearly_price_mean(yd["hist_m"])
    dps_by_cal = {}
    for d0, v in divs:
        dps_by_cal[d0.year] = dps_by_cal.get(d0.year, 0) + v
    yy = []
    for y in sorted(pm):
        if y == TODAY.year:
            continue
        if y in dps_by_cal and pm[y] > 0 and dps_by_cal[y] > 0:
            yy.append((y, dps_by_cal[y] / pm[y] * 100))
    rb_yield = ({"hist": yy, "current": yld_fwd, "kind": "pct", "low_is_cheap": False}
                if len(yy) >= 3 and is_num(yld_fwd) else None)

    yb = sec_avg.get("yield")
    range_txt = f"業種の利回り目安 {yb[0]:.1f}〜{yb[1]:.1f}%" if yb else "―"
    M["配当"].append({"name": "予想配当利回り", "v": yld_fwd, "disp": fmt_pct(yld_fwd, 2),
                      "ref": range_txt + "／米国はS&P500平均が約1.2%と低い。推移＝各暦年の平均株価に対する利回り",
                      "key": "div_yield", "series": yy, "series_kind": "pct", "series_current": yld_fwd})

    # 総還元利回り＝配当利回り＋自社株買い利回り（米国株は採点対象）
    total_yield = (yld_fwd + buyback_yield) if is_num(yld_fwd) and is_num(buyback_yield) else None
    ni0 = ni[0] if ni and is_num(ni[0]) else None
    divpaid0 = abs(divpaid[0]) if divpaid and is_num(divpaid[0]) else None
    total_return_amt = (divpaid0 or 0) + (buyback0 or 0) if (is_num(divpaid0) or is_num(buyback0)) else None
    total_payout = (total_return_amt / ni0 * 100) if is_num(total_return_amt) and is_num(ni0) and ni0 > 0 else None
    M["配当"].append({"name": "総還元利回り（配当＋自社株買い）", "v": total_yield,
                      "disp": (f"{fmt_pct(yld_fwd,2)}（配当）＋{fmt_pct(buyback_yield,2)}（自社株買い）＝{fmt_pct(total_yield,2)}"
                               if is_num(total_yield) else "算出不可（自社株買いデータなし）"),
                      "ref": "配当だけでは見えない株主還元の全体像。米国株は自社株買いが主な還元手段のため採点対象。"
                             "自社株買いは単年の実施額で毎年続くとは限らない点に注意", "key": "total_yield"})
    payout_disp = ("純利益僅少のため参考にならず" if is_num(total_payout) and total_payout > 300
                   else fmt_pct(total_payout))
    M["参考"].append({"name": "総還元性向（（配当＋自社株買い）÷純利益）", "v": None, "disp": payout_disp,
                      "ref": "100%超はその期の利益以上を還元＝内部留保の取り崩し", "key": None})

    M["配当"].append({"name": "増配率（直近5年・年率）", "v": dgr5, "disp": fmt_pct(dgr5),
                      "ref": f"5%以上が目安（0%以上で及第）／出所：{dps_src}", "key": "dgr5"})
    M["配当"].append({"name": "連続増配 年数", "v": streak_up,
                      "disp": f"{streak_up}年" if is_num(streak_up) else "―",
                      "ref": "25年以上で配当貴族、50年以上で配当王の水準", "key": "streak_up"})
    M["配当"].append({"name": "連続 非減配 年数", "v": streak_flat,
                      "disp": f"{streak_flat}年" if is_num(streak_flat) else "―",
                      "ref": "不況局面でも減配せず持続してきたか", "key": "streak_flat"})
    pn = sec_avg.get("payout")
    pn_txt = f"業種目安 {pn[0]}〜{pn[1]}%" if pn else "―"
    M["配当"].append({"name": "配当性向（純利益ベース）", "v": payout_ni, "disp": fmt_pct(payout_ni),
                      "ref": pn_txt + "／80%超は警戒。REITはFFOベースで見る（このGAAP値は100%超が普通）",
                      "key": "payout_ni"})
    M["配当"].append({"name": "ROE（配当の原資の効率）", "v": roe, "disp": fmt_pct(roe),
                      "ref": f"業種中央値 {fmt_pct(sec_avg.get('roe'))}／10%以上で優良" if sec_avg.get("roe") is not None else "10%以上で優良",
                      "key": "roe"})

    # 減配履歴
    last_cut_fy = find_last_cut(dps_series) if len(dps_series) >= 3 else None
    if len(dps_series) < 3:
        cut_disp = "データ不足で判定不可"
    elif last_cut_fy is None:
        cut_disp = "減配歴なし（確認できたデータ範囲内）"
    else:
        yrs_txt = f"{streak_flat}年" if is_num(streak_flat) else "不明"
        if is_num(streak_flat) and streak_flat >= 10:
            cut_disp = f"最終減配 {last_cut_fy}年頃。その後{yrs_txt}間は減配なし＝現在は良好水準"
        elif is_num(streak_flat) and streak_flat >= 5:
            cut_disp = f"最終減配 {last_cut_fy}年頃（{yrs_txt}前）。回復途上"
        else:
            cut_disp = f"最終減配 {last_cut_fy}年頃（{yrs_txt}前）。直近の減配歴が新しく要注意"
    M["参考"].append({"name": "減配履歴", "v": None, "disp": cut_disp,
                      "ref": "「連続非減配年数」の裏側にある事実。過去に減配があっても、その後の非減配年数が"
                             "長ければ現在の評価は良好になり得る", "key": None})

    # ---- 期待（バリュエーション）----
    per = info.get("trailingPE")
    pbr = info.get("priceToBook")
    per = per if is_num(per) and per > 0 else None
    pbr = pbr if is_num(pbr) and pbr > 0 else None
    ey = (1 / per * 100) if per else None
    per_vs = safe_div(per, sec_avg.get("per"))
    pbr_vs = safe_div(pbr, sec_avg.get("pbr"))

    yband_pos = yrange = None
    yvals = [v for _, v in yy]
    if len(yvals) >= 3 and is_num(yld_fwd):
        lo, hi = min(yvals), max(yvals)
        yrange = (lo, hi)
        if hi > lo:
            yband_pos = max(0.0, min(1.0, (yld_fwd - lo) / (hi - lo)))

    chowder = (yld_fwd + dgr5) if is_num(yld_fwd) and is_num(dgr5) else None

    per_band_pos = per_range = None
    eps_by_year = {y: e for y, e in zip(yd.get("is_years", []) or [], eps or []) if is_num(e) and e > 0}
    eps_vals = sorted(eps_by_year.values())
    eps_med = eps_vals[len(eps_vals) // 2] if eps_vals else None
    pers = []
    for y in sorted(pm):
        if y == TODAY.year:
            continue
        e = eps_by_year.get(y)
        if e is None or pm[y] <= 0:
            continue
        if eps_med and e < 0.4 * eps_med:
            continue
        p_y = pm[y] / e
        if p_y > 80:
            continue
        pers.append((y, p_y))
    pvals = [v for _, v in pers]
    if len(pvals) >= 3 and per:
        plo, phi = min(pvals), max(pvals)
        per_range = (plo, phi)
        if phi > plo:
            per_band_pos = max(0.0, min(1.0, (phi - per) / (phi - plo)))
    rb_per = ({"hist": pers, "current": per, "kind": "per", "low_is_cheap": True}
              if len(pvals) >= 3 and per else None)

    rate_sens = gics_sector in rate_sensitive
    yield_spread = (yld_fwd - ust_10y) if (rate_sens and is_num(yld_fwd) and is_num(ust_10y)) else None

    M["期待"].append({"name": "PER（実績・対業種平均）", "v": per_vs, "disp": (fmt_num(per, 1) + "倍" if per else "―"),
                      "ref": f"業種平均 {fmt_num(sec_avg.get('per'),1)}倍（対平均 {fmt_num(per_vs,2)}倍）" if per_vs else "―",
                      "key": "per_vs_sector"})
    M["期待"].append({"name": "PBR（実績・対業種平均）", "v": pbr_vs, "disp": (fmt_num(pbr, 2) + "倍" if pbr else "―"),
                      "ref": f"業種平均 {fmt_num(sec_avg.get('pbr'),1)}倍" if pbr_vs else "―", "key": "pbr_vs_sector"})
    if per_range:
        M["期待"].append({"name": "PERの自社過去レンジ内の位置", "v": per_band_pos,
                          "disp": f"過去 {per_range[0]:.1f}〜{per_range[1]:.1f}倍 ／ 現在 {per:.1f}倍 ＝ 割安度 {per_band_pos*100:.0f}/100",
                          "ref": "0＝レンジ上端（高PER＝割高）／100＝下端（低PER＝割安）", "key": "per_band_pos", "rangeband": rb_per})
    else:
        M["期待"].append({"name": "PERの自社過去レンジ内の位置", "v": None,
                          "disp": "履歴不足で算出不可", "ref": "3年以上の株価・EPSが必要", "key": "per_band_pos"})
    M["参考"].append({"name": "益回り（1÷PER）", "v": None, "disp": fmt_pct(ey),
                      "ref": "米10年国債利回りとの比較に使う", "key": None})
    if yrange:
        M["期待"].append({"name": "配当利回りセオリー（自分の過去レンジ内の位置）", "v": yband_pos,
                          "disp": f"過去 {yrange[0]:.1f}〜{yrange[1]:.1f}% ／ 現在 {yld_fwd:.1f}% ＝ 割安度 {yband_pos*100:.0f}/100",
                          "ref": "0＝レンジ下端（低利回り＝割高）／100＝上端（高利回り＝割安）",
                          "key": "yield_band_pos", "rangeband": rb_yield})
    else:
        M["期待"].append({"name": "配当利回りセオリー（過去レンジ内の位置）", "v": None,
                          "disp": "履歴不足で算出不可", "ref": "5年以上の株価・配当が必要", "key": "yield_band_pos"})
    M["期待"].append({"name": "Chowderルール（利回り＋5年増配率）", "v": chowder, "disp": fmt_pct(chowder),
                      "ref": "合計12%以上で合格（公益・通信等は8%）", "key": "chowder"})
    if rate_sens:
        M["期待"].append({"name": "利回り − 10年国債スプレッド", "v": yield_spread,
                          "disp": (f"{yld_fwd:.2f}% − {ust_10y:.2f}% ＝ {yield_spread:+.2f}%" if yield_spread is not None else "利回り不明"),
                          "ref": f"「債券の代わり」需要のある業種。広いほど割安。米10年国債 ＝ {fmt_num(ust_10y,2)}%。"
                                 f"米国は国債利回りが高く、スプレッドが0付近でも割安の目安",
                          "key": "yield_spread"})
    else:
        M["期待"].append({"name": "利回り − 10年国債スプレッド", "v": None,
                          "disp": "金利敏感セクター外のため評価しない",
                          "ref": "公益・不動産・通信サービス・生活必需品のみ採点", "key": "yield_spread"})

    # ---- テクニカル（採点しない・参考のみ）----
    tec = calc_technicals(yd["hist_d"])
    M["参考"].append({"name": "RSI(14)", "v": None,
                      "disp": (f"{tec['rsi']:.0f}" if tec["rsi"] is not None else "―") +
                              ("　売られすぎ水準" if tec["rsi"] is not None and tec["rsi"] < 30 else
                               "　買われすぎ水準" if tec["rsi"] is not None and tec["rsi"] >= 70 else "　中立"),
                      "ref": "短期すぎるため採点対象外。値動きの強弱を示す参考指標", "key": None})
    M["参考"].append({"name": "MACD(12,26,9)", "v": None, "disp": tec["macd_state"] or "―", "ref": "", "key": None})

    ctx = {
        "per": per, "pbr": pbr, "yld_fwd": yld_fwd, "dgr5": dgr5,
        "yband_pos": yband_pos, "yrange": yrange, "dps_series": dps_series, "dps_src": dps_src,
        "op_margin": op_margin, "roe": roe, "equity_ratio": equity_ratio,
        "streak_up": streak_up, "streak_flat": streak_flat, "chowder": chowder, "tec": tec,
        "rev_series": rev, "ni_series": ni, "eps_series": eps,
        "fwd_dps": fwd_dps, "payout_ni": payout_ni,
        "per_band_pos": per_band_pos, "yield_spread": yield_spread, "ust_10y": ust_10y,
        "roic_vs": roic_vs, "earn_stab": earn_stab, "icr": icr, "total_yield": total_yield,
        "buyback_yield": buyback_yield,
    }
    return M, flags, ctx


SEL_TIERS = (85, 68, 55)
# TIM は 2026-09-06 の全501銘柄バッチで校正。緩めたしきい値（米国債利回りが高いので
# yield_spread を大きく緩和した等）の積み上げで素点が高めに出て、旧 (72,57,45) では
# 母集団の中央値(91.5)が「割安圏」ラベルになっていた。分布 p73/p38/p13 ≒ (98,82,64) に
# 引き上げ、「割安圏」＝実際に上位1/4 という意味に揃えた。SEL 側は絶対的な質のバーとして
# 日本版と同じ (85,68,55) を維持（母集団がスクリーニング済みで高めに出るのは想定どおり）。
TIM_TIERS = (98, 82, 64)
SEL_LABEL = {"hi": "選定スコア上位（質・持続力とも高水準）", "mid": "選定スコア中位（一部に弱点）",
             "lo": "選定スコア下位（質に不安）", "xlo": "選定スコア基準未達", None: "判定不可（データ不足）"}
TIM_LABEL = {"hi": "買い時スコア上位（割安水準）", "mid": "買い時スコア中位（妥当水準）",
             "lo": "買い時スコア下位（やや割高水準）", "xlo": "買い時スコア最下位（割高水準）",
             None: "判定不可（PER・PBR・利回り履歴が不足）"}
QUADRANT = {
    ("hi", "hi"): "選定・買い時とも上位水準。",
    ("hi", "mid"): "選定は上位水準、買い時は中位（妥当水準）。",
    ("hi", "lo"): "選定は上位水準だが、買い時は下位（やや割高水準）。",
    ("hi", "xlo"): "選定は上位水準だが、買い時は最下位（割高水準）。",
    ("mid", "hi"): "買い時は上位（割安水準）だが、選定は中位（一部に弱点）。",
    ("mid", "mid"): "選定・買い時とも中位水準。",
    ("mid", "lo"): "選定は中位、買い時は下位（やや割高水準）。",
    ("mid", "xlo"): "選定は中位、買い時は最下位（割高水準）。",
    ("lo", "hi"): "買い時は上位（割安水準）だが、選定は下位（質に不安）。"
                   "低評価には理由があることが多い（バリュートラップの可能性）。",
    ("lo", "mid"): "選定スコアが基準未達の水準。",
    ("lo", "lo"): "選定・買い時とも下位水準。",
    ("lo", "xlo"): "選定は下位、買い時は最下位水準。",
    ("xlo", "hi"): "買い時は上位（割安水準）だが、選定は最下位水準。"
                    "低評価には理由があることが多い（バリュートラップの可能性）。",
    ("xlo", "mid"): "選定スコアが基準を大きく下回る水準。",
    ("xlo", "lo"): "選定・買い時とも下位水準。",
    ("xlo", "xlo"): "選定・買い時とも最下位水準。",
}
DISC_HTML = (
    "本ページは教育目的の一般情報です。「銘柄選定」「買い時」のスコア・ラベルは、あらかじめ定めた"
    "計算ルールで公開データから機械的に算出したものであり、投資助言ではありません。運営者は金融商品"
    "取引法上の投資助言・代理業の登録を受けていません。数値は yfinance（Yahoo Finance）由来で誤り・"
    "遅延・欠損があり得ます。業種平均・判定しきい値は2026年時点の目安です。正確な情報は各社の"
    "10-K・10-Q 等の一次情報でご確認ください。詳しくは"
    '<a href="../terms.html">利用規約・免責事項</a>を参照。'
)


def _tier(score, tiers):
    return analyze._tier(score, tiers)


def cov_label_us(pair):
    scored, possible = pair
    if possible <= 0:
        return 0.0, "―"
    r = scored / possible
    return r, ("高" if r >= 0.85 else "中" if r >= 0.65 else "低")


def verdicts_us(sel_score, tim_score, groups, dom_scores, ctx, sec_avg, is_simple, coverage):
    per, pbr = ctx["per"], ctx["pbr"]
    sig = []
    if per and sec_avg.get("per"):
        sig.append((sec_avg["per"] - per) / sec_avg["per"])
    if pbr and sec_avg.get("pbr"):
        sig.append((sec_avg["pbr"] - pbr) / sec_avg["pbr"])
    if ctx["yband_pos"] is not None:
        sig.append((ctx["yband_pos"] - 0.5) * 0.8)
    sig = [max(-0.5, min(0.5, s)) for s in sig]
    val_idx = sum(sig) / len(sig) if sig else None
    if val_idx is None:
        val_label = "判定不可（PER・PBR・利回り履歴が不足）"
    elif val_idx >= 0.12:
        val_label = "割安圏（業種平均・自分の過去レンジ比で安い）"
    elif val_idx <= -0.12:
        val_label = "割高圏（業種平均・過去レンジ比で高い）"
    else:
        val_label = "ほぼ妥当な水準"
    if pbr and pbr < 1:
        val_label += "／PBR1倍割れ"

    fin = dom_scores.get("財務")
    cf = dom_scores.get("キャッシュフロー")
    stab_vals = [x for x in (fin, cf) if x is not None]
    stab = sum(stab_vals) / len(stab_vals) if stab_vals else None
    if is_simple:
        stab_label = "簡易判定では未評価（財務・CFは構造的に別基準）"
    elif stab is None:
        stab_label = "判定不可"
    else:
        stab_label = "高い" if stab >= 88 else "中程度" if stab >= 68 else "低い（負債・CFに注意）"

    rc = analyze.cagr_of(ctx["rev_series"])
    ec = analyze.cagr_of(ctx["eps_series"])
    if is_simple:
        grow_label = "簡易判定では未評価（業績は参考欄に表示）"
    elif rc is None and ec is None:
        grow_label = "判定不可"
    elif (rc or 0) > 3 and (ec or 0) > 3:
        grow_label = "拡大（増収かつ増益）"
    elif (rc or -99) >= 0 and (ec or -99) >= -2:
        grow_label = "横ばい〜微増"
    else:
        grow_label = "縮小傾向（増配を配当性向の引き上げで支えていないか確認）"

    tec = ctx.get("tec") or {}
    rsi = tec.get("rsi")
    macd = tec.get("macd_state")
    bits = []
    if is_num(rsi):
        bits.append(f"RSI {rsi:.0f}" + ("（売られすぎ）" if rsi < 30 else "（買われすぎ）" if rsi >= 70 else "（中立）"))
    if macd:
        bits.append(str(macd))
    short_label = " ／ ".join(bits) if bits else "―"

    st_tier = _tier(sel_score, SEL_TIERS)
    ti_tier = _tier(tim_score, TIM_TIERS)
    quad = QUADRANT.get((st_tier or "mid", ti_tier or "mid"), "―")
    sel_cov = cov_label_us(coverage.get("選定", (0, 0)))
    tim_cov = cov_label_us(coverage.get("買い時", (0, 0)))
    if "低" in (sel_cov[1], tim_cov[1]):
        which = " と ".join(n for n, c in (("銘柄選定", sel_cov[1]), ("買い時", tim_cov[1])) if c == "低")
        quad = f"※データ不足（{which}のカバレッジ低）。点数は目安、参考程度に。　" + quad

    return {
        "sel_label": SEL_LABEL[st_tier], "tim_label": TIM_LABEL[ti_tier],
        "comment": quad, "valuation": val_label, "growth": grow_label,
        "stability": stab_label, "short": short_label, "val_idx": val_idx,
        "sel_cov": (coverage.get("選定", (0, 0)), sel_cov),
        "tim_cov": (coverage.get("買い時", (0, 0)), tim_cov),
    }


# ---------------------------------------------------------------- レンダリング
# GICS11 は設定JSONのキー・summaryの保存値としては英語のまま。表示だけ日本語に。
GICS_JP = {
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


def gics_jp(name):
    return GICS_JP.get(name, name or "―")


LABEL_MARK = {"good": "◎", "warn": "△", "bad": "▲", None: "―"}


def _metric_details_html_us(it, gics_key, gics_disp, is_simple, rules):
    """日本版 analyze._metric_details_html の米国版。ルール参照は英語GICSキー、
    表示は日本語セクター名。見るポイント／過去レンジ／推移／判定ルール／この銘柄／点。"""
    key = it.get("key")
    dom = analyze.KEY_DOMAIN.get(key, "")
    rule = analyze.rule_for(key, gics_key, rules) if key else None
    wht = analyze.METRIC_HELP.get(key, {}).get("what", "")
    rb = analyze.rule_block_html(key, rule, gics_disp)
    wb = analyze.why_block_html(it, rule, gics_disp, is_simple, dom)
    what_p = f'<p class="what"><b>見るポイント：</b>{wht}</p>' if wht else ""
    sc = it.get("score")
    sc_txt = f"{sc:.0f} / 110" if is_num(sc) else "―（不採点）"

    trend_p = ""
    sp = it.get("series")
    if sp and len([1 for _, v in sp if is_num(v)]) >= 2:
        trend_rule = rule if key in ("op_margin", "payout_ni", "div_yield", "total_yield") else None
        trend_p = ('<p class="rule"><b>推移：</b>古い→新しい'
                   + ("　（帯＝◎良好／△注意／▲弱いの区切り）" if trend_rule else "")
                   + '</p><div class="trendwrap wide">'
                   + analyze.svg_trend(sp, it.get("series_kind", "usd"), it.get("series_current"), trend_rule)
                   + '</div>')
    band_p = ""
    rbd = it.get("rangeband")
    if rbd:
        bsvg = analyze.svg_rangeband(rbd, it["name"])
        if bsvg:
            band_p = ('<p class="rule"><b>過去レンジ内の位置：</b>塗り分けたゾーン（割安・標準・割高。'
                      'どちらが割安かは帯の右のラベルを参照）に、折れ線＝年次の実績推移、▶＝現在の値を'
                      f'重ねたグラフ。</p><div class="trendwrap wide">{bsvg}</div>')
    return (
        '<details class="m"><summary>'
        f'<span class="mn">{analyze.html.escape(it["name"])}</span>'
        f'<span class="mv">{analyze.html.escape(str(it.get("disp","")))}</span>'
        f'<span class="mr">{analyze.html.escape(str(it.get("ref","")))}</span>'
        f'<span class="mt">{analyze.tag(it)}</span></summary>'
        f'<div class="mbody">{what_p}{band_p}{trend_p}'
        f'<p class="rule"><b>判定ルール：</b><br>{rb}</p>'
        f'<p class="why"><b>この銘柄：</b>{wb}</p>'
        f'<p class="rule">この指標の点：<b>{sc_txt}</b>（グループスコアはこの点の平均）</p>'
        '</div></details>')


def _group_section_us(head, sg, groups, rowmap, gics_key, gics_disp, is_simple, rules):
    out = []
    for gname, gdef in sg[head].items():
        out.append(f'<div class="domhead"><b>{analyze.html.escape(gname)}</b> {analyze.bar(groups.get(gname))}</div>')
        got = False
        for k in gdef["keys"]:
            it = rowmap.get(k)
            if it is None:
                continue
            got = True
            out.append(_metric_details_html_us(it, gics_key, gics_disp, is_simple, rules))
        if not got:
            out.append('<div class="plain"><span class="mn">―</span><span class="mv2">この業種では評価対象外</span></div>')
    return "".join(out)


def svg_price_us(hist_m):
    """日本版 svg_price の米国版（$ 表記）。"""
    import math
    if hist_m is None or getattr(hist_m, "empty", True):
        return "<p class='muted'>株価履歴なし</p>"
    pts = [(idx.to_pydatetime().date(), float(r["Close"])) for idx, r in hist_m.iterrows()
           if r.get("Close") is not None and not (isinstance(r["Close"], float) and math.isnan(r["Close"]))]
    if len(pts) < 4:
        return "<p class='muted'>株価履歴なし</p>"
    W, H, LX, RX, TP, BT = 620, 184, 56, 606, 26, 22
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = xs[0].toordinal(), xs[-1].toordinal()
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.08 or 1.0
    lo_ax, hi_ax = lo - pad, hi + pad

    def px(d):
        return LX + (d.toordinal() - x0) / (x1 - x0 or 1) * (RX - LX)

    def py(v):
        return TP + (1 - (v - lo_ax) / (hi_ax - lo_ax)) * (H - TP - BT)

    grid = analyze._yscale(lo, hi, py, LX, RX, "usd_px")
    xlab = []
    for yr in range(xs[0].year, xs[-1].year + 1):
        d = dt.date(yr, 1, 1)
        if x0 <= d.toordinal() <= x1:
            xlab.append(f'<text x="{px(d):.0f}" y="{H-6}" class="cx">{str(yr)[-2:]}</text>')
    dpath = "M " + " L ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in pts)
    last = pts[-1][1]
    return (f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="株価の推移">'
            f'{grid}{"".join(xlab)}'
            f'<path d="{dpath}" fill="none" stroke="var(--accent)" stroke-width="2"/>'
            f'<text x="{LX}" y="14" class="ct">株価（$）　{xs[0].year}年〜　'
            f'高値 ${hi:,.2f} / 安値 ${lo:,.2f} / 直近 ${last:,.2f}</text></svg>')


def svg_dps_us(dps_series, src):
    """日本版 svg_dps の米国版（$ 表記）。"""
    if not dps_series or len(dps_series) < 2:
        return "<p class='muted'>配当履歴なし</p>"
    data = dps_series[-14:]
    W, H, LX, RX, TP, BT = 620, 194, 52, 604, 26, 24
    vals = [v for _, v in data]
    hi = max(vals) or 1.0
    hi_ax = hi * 1.12
    n = len(data)

    def py(v):
        return TP + (1 - v / hi_ax) * (H - TP - BT)

    bw = (RX - LX) / n * 0.6
    parts = [analyze._yscale(0.0, hi, py, LX, RX, "usd_px")]
    for i, (yr, v) in enumerate(data):
        x = LX + (i + 0.5) * (RX - LX) / n - bw / 2
        y = py(v)
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{H-BT-y:.1f}" fill="var(--accent)" opacity="0.85"/>')
        parts.append(f'<text x="{x+bw/2:.1f}" y="{H-6:.0f}" class="cx">{str(yr)[-2:]}</text>')
    gr = cagr(vals[0], vals[-1], len(vals) - 1)
    return (f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="1株配当の推移">'
            f'{"".join(parts)}'
            f'<text x="{LX}" y="14" class="ct">1株配当（$・{src}）　{data[0][0]}→{data[-1][0]}　年率 {fmt_pct(gr)}</text></svg>')


def render_company_html_us(co):
    if not co or not co.get("has"):
        return ""
    rows = []
    loc = "、".join(x for x in (co.get("city"), co.get("country")) if x)
    if loc:
        rows.append(f'<div class="plain"><span class="mn">本社所在地</span><span class="mv2">{analyze.html.escape(loc)}</span></div>')
    if is_num(co.get("employees")):
        rows.append(f'<div class="plain"><span class="mn">従業員数</span><span class="mv2">{co["employees"]:,}名</span></div>')
    if co.get("website"):
        u = analyze.html.escape(co["website"])
        rows.append(f'<div class="plain"><span class="mn">Webサイト</span><span class="mv2"><a href="{u}" target="_blank" rel="noopener">{u}</a></span></div>')
    summary_p = ""
    if co.get("summary"):
        summary_p = (f'<p class="rule"><b>事業内容（English・出典 Yahoo Finance）</b></p>'
                     f'<p style="white-space:pre-wrap">{analyze.html.escape(co["summary"])}</p>')
    return (f'<details class="chartbox"><summary>企業概要</summary>'
            f'<div class="mbody">{"".join(rows)}{summary_p}'
            f'<p class="rule" style="margin-top:10px">※ 事業内容は yfinance（Yahoo Finance）由来の英語の説明文です。</p>'
            f'</div></details>')


_RECO_JP = {"strong_buy": "強気買い", "buy": "買い", "hold": "中立",
            "underperform": "弱気", "sell": "売り", "none": "―"}
_QNAME_JP = {"売上高": "売上高", "営業利益": "営業利益", "純利益": "純利益",
             "EPS（四半期）": "EPS（四半期）"}


def _earn_rows_us(ea):
    """(見出し, 本文) のリスト。yfinance の決算集計を USD／日本語で。"""
    R = []
    epr = ea.get("eps_reported")
    if is_num(epr):
        s = f"実績 ${fmt_num(epr, 2)}"
        if is_num(ea.get("eps_est")):
            s += f" ／ 事前予想 ${fmt_num(ea['eps_est'], 2)}"
        if is_num(ea.get("surprise")):
            s += f" ／ サプライズ {ea['surprise']:+.1f}%"
        head = "直近四半期 EPS" + (f"（開示 {ea['disc_date']}）" if ea.get("disc_date") else "")
        R.append((head, s))
    qs = [x for x in (ea.get("q") or []) if x.get("unit") != "円"]
    if qs:
        parts = []
        for x in qs:
            nm = _QNAME_JP.get(x["name"], x["name"])
            t = f"{nm} {fmt_usd(x['val'])}"
            if is_num(x.get("yoy")):
                t += f"（前年同期比 {x['yoy']:+.1f}%）"
            parts.append(t)
        R.append(("直近四半期" + (f"（{ea['q_date']}）" if ea.get("q_date") else ""), " ／ ".join(parts)))
    fw = []
    for pk in ("0y", "+1y"):
        f = (ea.get("fwd") or {}).get(pk)
        if not f:
            continue
        t = f"{'今期' if pk == '0y' else '来期'}予想EPS ${fmt_num(f['eps'], 2)}"
        if is_num(f.get("growth")):
            t += f"（前期比 {f['growth']:+.1f}%）"
        if is_num(f.get("per")):
            t += f" ／ 予想PER {fmt_num(f['per'], 1)}倍"
        if is_num(f.get("n")):
            t += f" ／ {int(f['n'])}名"
        fw.append(t)
    if ea.get("fwd_rev") and is_num(ea["fwd_rev"].get("avg")):
        rr = ea["fwd_rev"]
        t = f"今期予想 売上 {fmt_usd(rr['avg'])}"
        if is_num(rr.get("growth")):
            t += f"（前期比 {rr['growth']:+.1f}%）"
        fw.append(t)
    if fw:
        R.append(("今後の見通し（アナリスト予想）", " ／ ".join(fw)))
    if ea.get("next_earn"):
        R.append(("次回決算 予定日", str(ea["next_earn"])))
    t = ea.get("target")
    if t and is_num(t.get("mean")):
        s = f"平均 ${fmt_num(t['mean'], 2)}"
        if is_num(t.get("vs")):
            s += f"（現在株価比 {t['vs']:+.1f}%）"
        if is_num(t.get("high")) and is_num(t.get("low")):
            s += f" ／ 高値 ${fmt_num(t['high'], 2)} ・ 安値 ${fmt_num(t['low'], 2)}"
        R.append(("アナリスト目標株価", s))
    rc = ea.get("reco") or {}
    if rc.get("key") or is_num(rc.get("mean")):
        s = _RECO_JP.get((rc.get("key") or "none"), rc.get("key") or "―")
        if is_num(rc.get("mean")):
            s += f"（{rc['mean']:.2f}／1=強気・5=弱気）"
        if is_num(rc.get("n")):
            s += f" ／ 対象 {int(rc['n'])}名"
        R.append(("アナリスト・レーティング", s))
    return R


def _earn_block_us(ea):
    if not ea or not ea.get("has"):
        return ""
    rows = "".join(f'<div class="plain"><span class="mn">{analyze.html.escape(h)}</span>'
                   f'<span class="mv2">{analyze.html.escape(b)}</span></div>' for h, b in _earn_rows_us(ea))
    disc = ("yfinance（Yahoo Finance）のアナリスト集計。四半期のEPS実績/予想もyfinance換算で誤差あり。"
            "会社の正式な業績は決算資料（10-Q・10-K・プレスリリース）で必ず確認すること。")
    return f'<h2>直近決算とアナリスト予想</h2><div class="legend">{disc}</div>{rows}'


_US_CSS = """
:root{ --bg:#ffffff; --fg:#1d232b; --muted:#6b7683; --line:#e4e8ec; --card:#f7f9fa;
  --accent:#2f9e91; --hi:#2f9e91; --mid:#e0912f; --lo:#d1584f; --na:#c3ccd3; }
@media (prefers-color-scheme:dark){ :root{ --bg:#161a1e; --fg:#e7ecef; --muted:#9aa6af;
  --line:#2c333a; --card:#1e242a; --accent:#4fb8ab; --hi:#4fb8ab; --mid:#e0a45a; --lo:#e07b73; --na:#4a555e; } }
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,"Hiragino Kaku Gothic ProN","Noto Sans JP",Meiryo,"Segoe UI",sans-serif;
  line-height:1.7;font-size:14px}
.wrap{max-width:820px;margin:0 auto;padding:26px 20px 60px}
h1{font-size:20px;margin:0 0 2px}
h2{font-size:15px;margin:26px 0 4px;border-left:4px solid var(--accent);padding-left:8px}
.sub{color:var(--muted);font-size:12.5px;margin-bottom:18px}
.top{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin-bottom:8px}
.score2{display:flex;gap:18px;flex-wrap:wrap}
.sblk{flex:1;min-width:250px}
.sblk .hd{display:flex;align-items:baseline;gap:8px;margin-bottom:3px}
.sblk .hd b{font-size:13px}
.sblk .num{font-size:26px;font-weight:700;line-height:1}
.sblk .num.hi{color:var(--hi)} .sblk .num.mid{color:var(--mid)} .sblk .num.lo{color:var(--lo)}
.sblk .lab{font-size:12.5px;font-weight:700}
.gbar{height:10px;border-radius:5px;background:var(--line);overflow:hidden;margin:4px 0 6px}
.gbar>i{display:block;height:100%}
.gbar>i.hi{background:var(--hi)} .gbar>i.mid{background:var(--mid)} .gbar>i.lo{background:var(--lo)}
.sblk .sub{font-size:11.5px;color:var(--muted)}
.sblk .cov{font-size:11px;color:var(--muted);margin:0 0 5px}
.sblk .cov b{font-weight:700}
.sblk .cov.low{color:var(--lo)} .sblk .cov.low b{color:var(--lo)}
.sblk .cov.mid b{color:var(--mid)}
.quad{margin:10px 0 14px;padding:10px 14px;border-left:4px solid var(--accent);background:var(--card);border-radius:6px;font-size:13.5px;font-weight:700}
.legend{font-size:11.5px;color:var(--muted);margin:2px 0 12px;line-height:1.5}
.domhead{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin:16px 0 0;font-size:14px}
details.m{border-bottom:1px solid var(--line)}
details.m>summary{list-style:none;cursor:pointer;display:grid;
  grid-template-columns:minmax(130px,1.5fr) 1fr 1.25fr 86px;gap:10px;align-items:start;
  padding:9px 6px 9px 22px;position:relative;font-size:13px}
details.m>summary::-webkit-details-marker{display:none}
details.m>summary::before{content:"\\25B8";position:absolute;left:6px;top:9px;color:var(--muted);transition:transform .15s}
details.m[open]>summary::before{transform:rotate(90deg)}
details.m>summary:hover{background:var(--card)}
details.m .mv{font-variant-numeric:tabular-nums}
details.m .mr{color:var(--muted);font-size:12px}
details.m .mt{text-align:right;white-space:nowrap}
.mv2{font-size:12.5px}
.mbody{padding:2px 14px 14px 22px;background:var(--card);font-size:12.5px}
.mbody p{margin:6px 0}
.mbody .rule{color:var(--muted)}
.mbody .what{}
.mbody .why{border-left:3px solid var(--accent);padding-left:8px}
.plain{display:grid;grid-template-columns:minmax(130px,1fr) 2fr;gap:10px;padding:9px 6px 9px 22px;border-bottom:1px solid var(--line);font-size:13px}
@media(max-width:560px){
  details.m>summary{grid-template-columns:1fr 80px}
  details.m>summary .mv,details.m>summary .mr{display:none}
  .plain{grid-template-columns:1fr}
}
.bar{display:inline-block;width:150px;height:9px;border-radius:5px;background:var(--line);overflow:hidden;vertical-align:middle;margin:0 8px}
.fill{height:100%} .fill.hi{background:var(--hi)} .fill.mid{background:var(--mid)} .fill.lo{background:var(--lo)} .fill.na{background:var(--na)}
.sc{font-weight:700} .sc.hi{color:var(--hi)} .sc.mid{color:var(--mid)} .sc.lo{color:var(--lo)} .sc.na{color:var(--muted);font-weight:400;font-size:12px}
.t{font-size:11.5px;padding:2px 6px;border-radius:5px;border:1px solid var(--line)}
.t.hi{color:var(--hi)} .t.mid{color:var(--mid)} .t.lo{color:var(--lo)} .t.na{color:var(--muted)}
.gauge{position:relative;height:30px;border-radius:6px;margin:10px 0 4px;background:linear-gradient(90deg,var(--lo),var(--na) 50%,var(--hi))}
.gauge i{position:absolute;top:-4px;width:2px;height:38px;background:var(--fg)}
.gauge b{position:absolute;font-size:10px;color:#fff;top:7px}
.gl{left:8px} .gr{right:8px}
.charts{display:flex;gap:14px;flex-wrap:wrap}
.chart{flex:1;min-width:280px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px}
.ct{fill:var(--muted);font-size:10px} .cx{fill:var(--muted);font-size:9px;text-anchor:middle}
.cm{fill:var(--muted);font-size:9px} .cg{fill:var(--fg);font-size:10px;font-weight:700}
details.chartbox{border:1px solid var(--line);border-radius:8px;margin:8px 0 4px;background:var(--card)}
details.chartbox>summary{list-style:none;cursor:pointer;padding:8px 12px;font-size:12.5px;font-weight:700;position:relative}
details.chartbox>summary::-webkit-details-marker{display:none}
details.chartbox>summary::before{content:"\\25B8";color:var(--muted);margin-right:6px;display:inline-block;transition:transform .15s}
details.chartbox[open]>summary::before{transform:rotate(90deg)}
details.chartbox .cbody{padding:0 8px 10px}
details.chartbox .chart{border:0;background:transparent;padding:0}
details.chartbox .mbody{background:transparent}
.trendwrap{max-width:440px;margin:2px 0 8px}
.trendwrap.wide{max-width:490px}
svg.trend{width:100%;height:auto;border:1px solid var(--line);border-radius:8px;background:var(--bg);padding:4px}
.warn{background:color-mix(in srgb,var(--mid) 12%,var(--bg));border:1px solid var(--mid);border-radius:10px;padding:10px 14px;margin:14px 0;font-size:12.5px}
.warn ul{margin:6px 0 0;padding-left:18px}
.muted{color:var(--muted)}
.disc{margin-top:30px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:11.5px}
.topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.topbar a{font-size:12.5px;white-space:nowrap}
a{color:var(--accent)}
@media print{body{font-size:11px} .wrap{max-width:none} .topbar a{display:none}}
"""


def render_html_us(meta, detail, groups, sel_score, tim_score, vd, M, ctx, warnings, rules):
    rowmap = {r["key"]: r for dom in detail for r in detail[dom] if r.get("key")}
    sg = rules["score_groups"]
    gk = meta["gics_sector"]
    gd = gics_jp(gk)
    is_simple = meta["is_simple"]
    sel_blocks = _group_section_us("選定", sg, groups, rowmap, gk, gd, is_simple, rules)
    tim_blocks = _group_section_us("買い時", sg, groups, rowmap, gk, gd, is_simple, rules)
    sel_chart = (
        '<details class="chartbox"><summary>銘柄選定の指標スコアを一覧グラフで見る</summary>'
        '<div class="cbody">'
        + analyze.svg_score_bars(sg["選定"], rowmap, groups, "銘柄選定 指標スコア一覧")
        + '</div></details>')

    ref_blocks = []
    for it in M.get("参考", []):
        nm = analyze.html.escape(str(it["name"]))
        disp = analyze.html.escape(str(it.get("disp", "―")))
        ref = it.get("ref", "")
        link = f' <a href="{analyze.html.escape(ref)}" target="_blank" rel="noopener">{analyze.html.escape(ref)}</a>' if str(ref).startswith("http") else ""
        rtxt = "" if str(ref).startswith("http") else analyze.html.escape(str(ref))
        h = analyze.NAME_HELP.get(it["name"])
        sp = it.get("series")
        trend = ""
        if sp and len([1 for _, v in sp if is_num(v)]) >= 2:
            trend = ('<p class="rule"><b>推移：</b>古い→新しい</p>'
                     f'<div class="trendwrap">{analyze.svg_trend(sp, it.get("series_kind", "usd"))}</div>')
        if h or trend or rtxt:
            body = (f'<p class="what">{h}</p>' if h else "") + trend
            if rtxt and not h:
                body = f'<p class="rule">{rtxt}</p>' + body
            ref_blocks.append(f'<details class="m"><summary><span class="mn">{nm}</span>'
                              f'<span class="mv2">{disp}{link}</span></summary>'
                              f'<div class="mbody">{body}</div></details>')
        else:
            ref_blocks.append(f'<div class="plain"><span class="mn">{nm}</span><span class="mv2">{disp}{link}</span></div>')

    warn_html = ""
    if warnings:
        warn_html = '<div class="warn"><b>データに関する注意</b><ul>' + "".join(f"<li>{analyze.html.escape(w)}</li>" for w in warnings) + "</ul></div>"

    gauge_pos = 50
    if vd.get("val_idx") is not None:
        gauge_pos = max(2, min(98, 50 - vd["val_idx"] * 180))

    def _cls(sc, tiers):
        t = _tier(sc, tiers)
        return "hi" if t == "hi" else "mid" if t == "mid" else "lo"

    sel_cls, tim_cls = _cls(sel_score, SEL_TIERS), _cls(tim_score, TIM_TIERS)
    sel_n = f"{sel_score:.0f}" if is_num(sel_score) else "―"
    tim_n = f"{tim_score:.0f}" if is_num(tim_score) else "―"
    sel_w = min(100, sel_score) if is_num(sel_score) else 0
    tim_w = min(100, tim_score) if is_num(tim_score) else 0
    (sel_sc, sel_ps), (_, sel_cl) = vd["sel_cov"]
    (tim_sc, tim_ps), (_, tim_cl) = vd["tim_cov"]
    cov_cls = {"高": "", "中": "mid", "低": "low", "―": ""}

    simple_note = '　｜　<b>簡易判定</b>（銀行・保険・REIT：業績／財務／CFは参考表示のみ）' if meta["is_simple"] else ""
    price_s = f"${fmt_num(meta['price'], 2)}" if is_num(meta["price"]) else "―"
    pdate_s = f"（終値 {meta['price_date']}）" if meta.get("price_date") else ""
    mcap_s = fmt_usd(meta["mcap"])
    simple_legend = "　簡易判定の銘柄は 業績／財務／CF グループを採点せず、銘柄選定は配当の持続力のみで算出。" if meta['is_simple'] else ""

    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{analyze.html.escape(meta['name'])}（{meta['code']}）｜米国株 配当スクリーニング</title>
<style>{_US_CSS}</style></head><body><div class="wrap">
<div class="topbar"><h1>{analyze.html.escape(meta['name'])}（{meta['code']}）</h1><a href="../index.html">← 一覧へ戻る</a></div>
<div class="sub">GICS業種：<b>{gics_jp(meta['gics_sector'])}</b>（{analyze.html.escape(meta['gics_sector'])}／yfinance：{analyze.html.escape(meta['industry'] or '―')}）{simple_note}<br>
株価 {price_s}{pdate_s} &nbsp;｜&nbsp; 時価総額 {mcap_s} &nbsp;｜&nbsp; 生成 {meta['today']}</div>

{render_company_html_us(ctx.get('company'))}
{warn_html}

<div class="top"><div class="score2">
  <div class="sblk">
    <div class="hd"><b>銘柄選定</b><span class="num {sel_cls}">{sel_n}</span><span class="lab {sel_cls}">{vd['sel_label']}</span></div>
    <div class="gbar"><i class="{sel_cls}" style="width:{sel_w:.0f}%"></i></div>
    <div class="cov {cov_cls.get(sel_cl,'')}">採点 {sel_sc}/{sel_ps} &nbsp; カバレッジ <b>{sel_cl}</b></div>
    <div class="sub">＝配当株としての質（業績／財務／キャッシュフロー／配当の持続力）<br>
      成長性：{vd['growth']} &nbsp;／&nbsp; 安定性：{vd['stability']}</div>
  </div>
  <div class="sblk">
    <div class="hd"><b>買い時</b><span class="num {tim_cls}">{tim_n}</span><span class="lab {tim_cls}">{vd['tim_label']}</span></div>
    <div class="gbar"><i class="{tim_cls}" style="width:{tim_w:.0f}%"></i></div>
    <div class="cov {cov_cls.get(tim_cl,'')}">採点 {tim_sc}/{tim_ps} &nbsp; カバレッジ <b>{tim_cl}</b></div>
    <div class="sub">＝今の株価水準（配当利回りセオリー／利回り水準とChowder／株価バリュエーション／金利スプレッド）<br>
      割安・割高：{vd['valuation']} &nbsp;／&nbsp; 短期：{vd['short']}</div>
  </div>
</div></div>
<div class="quad">→ {vd['comment']}</div>

<div class="gauge"><b class="gl">割安</b><b class="gr">割高</b><i style="left:{gauge_pos:.0f}%"></i></div>
<div class="muted" style="font-size:11.5px">PER・PBRの対業種平均と、配当利回りの過去レンジ内の位置を合成した目安。</div>

<h2>① 銘柄選定の指標</h2>
{sel_chart}
<div class="legend">各行をクリックすると「見るポイント／判定ルール／この評価になった理由」が開きます。ラベルは3段階（◎良好／△注意／▲弱い）＋対象外「―」。点は good/warn の2閾値の間を直線補間（warn=60・good=100・別格ライン=最大110・下限20）。ラベルの◎△▲は閾値どおりなので『△なのに94点（＝基準ぎりぎり）』とズレることがあります。グループスコア＝その指標の点の平均、銘柄選定＝業績28・財務27・CF15・配当の持続力30％、買い時＝配当利回りセオリー38・利回り水準とChowder24・株価バリュエーション20・金利スプレッド18％の加重平均。{simple_legend}</div>
{sel_blocks}

<h2>② 買い時の指標</h2>
{tim_blocks}

<h2>推移チャート</h2>
<div class="charts">{svg_price_us(ctx.get('hist_m'))}{svg_dps_us(ctx.get('dps_series'), ctx.get('dps_src', 'yfinance'))}</div>

{_earn_block_us(ctx.get('earn'))}

<h2>参考・自動判定できない項目</h2>
<div class="legend">クリックで説明が開く行があります。</div>
{''.join(ref_blocks)}

<div class="disc">{DISC_HTML}</div>
</div></body></html>"""


def render_md_us(meta, detail, groups, sel_score, tim_score, vd, M, ctx, rules):
    sg = rules["score_groups"]
    rowmap = {r["key"]: r for dom in detail for r in detail[dom] if r.get("key")}
    L = [f"# {meta['name']}（{meta['code']}）｜米国株 配当スクリーニング", ""]
    L.append(f"- GICS業種：**{gics_jp(meta['gics_sector'])}**（{meta['gics_sector']}）"
             + ("（簡易判定）" if meta["is_simple"] else ""))
    L.append(f"- 株価：${fmt_num(meta['price'], 2)}  ｜  時価総額：{fmt_usd(meta['mcap'])}  ｜  生成：{meta['today']}")
    L.append("")
    sel_n = f"{sel_score:.0f}" if is_num(sel_score) else "―"
    tim_n = f"{tim_score:.0f}" if is_num(tim_score) else "―"
    L.append(f"## 銘柄選定 {sel_n} ― {vd['sel_label']}")
    L.append(f"成長性：{vd['growth']}  ／  安定性：{vd['stability']}")
    L.append("")
    L.append(f"## 買い時 {tim_n} ― {vd['tim_label']}")
    L.append(f"割安・割高：{vd['valuation']}  ／  短期：{vd['short']}")
    L.append("")
    L.append(f"> {vd['comment']}")
    for head in ("選定", "買い時"):
        L.append("")
        L.append(f"### {'銘柄選定の指標' if head == '選定' else '買い時の指標'}")
        for gname, gdef in sg[head].items():
            gs = groups.get(gname)
            L.append(f"\n**{gname}** ― {gs:.0f}" if is_num(gs) else f"\n**{gname}** ― ―")
            for k in gdef["keys"]:
                it = rowmap.get(k)
                if it is None:
                    continue
                mark = LABEL_MARK.get(it.get("label"), "―")
                sc = it.get("score")
                sc_txt = f"{sc:.0f}" if is_num(sc) else "―"
                L.append(f"- {mark} {sc_txt}  {it['name']}：{it.get('disp', '―')}")
    L.append("")
    L.append("### 参考")
    for it in M.get("参考", []):
        L.append(f"- {it['name']}：{it.get('disp', '―')}")
    L.append("")
    L.append(f"---\n{DISC_HTML}")
    return "\n".join(L)


# ---------------------------------------------------------------- generate
def generate_us(ticker, cfg=None, log=None):
    """1ティッカーを診断して {code,name,ok,error,html,md,summary} を返す。
    副作用なし（ファイル書き出し・print はしない）。日本版 analyze.generate の米国版。"""
    log = log or (lambda *_: None)
    ticker = str(ticker).strip().upper()
    res = {"code": ticker, "name": None, "ok": False, "error": None, "html": None, "md": None, "summary": None}
    if not ticker or not all(c.isalnum() or c in ".-" for c in ticker):
        res["error"] = "invalid ticker"
        return res
    cfg = cfg or load_config_us()
    sec_avg_all, smap, rules = cfg["sec_avg_all"], cfg["smap"], cfg["rules"]
    rate_sensitive = set(smap.get("rate_sensitive", []))
    ust_10y = rules.get("market", {}).get("ust_10y", 4.2)

    log(f"[1/3] yfinance fetch {ticker}")
    try:
        yd = fetch_yf_us(ticker)
    except Exception as e:
        res["error"] = f"fetch failed: {e}"
        return res
    info = yd.get("info") or {}
    if not info and yd.get("price") is None:
        res["error"] = "データなし（ティッカーを確認。米国上場銘柄のみ対応）"
        return res

    name = info.get("longName") or info.get("shortName") or ticker
    gics, industry, ysector, is_simple, is_reit, src = classify_sector_us(info, smap)
    sec_avg = sec_avg_all.get(gics, {})

    log("[2/3] scoring")
    try:
        M, flags, ctx = build_metrics_us(yd, sec_avg, is_simple, gics, rate_sensitive, ust_10y)
        ctx["earn"] = analyze.build_earnings(yd, yd["price"])
        ctx["company"] = analyze.build_company_overview(info)
        ctx["hist_m"] = yd.get("hist_m")
        dom_scores, detail, groups, sel_score, tim_score, coverage = analyze.score_all(M, gics, rules, is_simple)
        vd = verdicts_us(sel_score, tim_score, groups, dom_scores, ctx, sec_avg, is_simple, coverage)
    except Exception as e:
        import traceback
        res["error"] = f"scoring failed: {e}\n{traceback.format_exc()}"
        return res

    warnings = []
    for nm, key in (("銘柄選定", "選定"), ("買い時", "買い時")):
        _, lab = cov_label_us(coverage[key])
        if lab == "低":
            warnings.append(f"{nm}スコアは採点できた指標が少ない（カバレッジ低）。点数は目安程度に。")
    if not yd["is_rows"]:
        warnings.append("損益計算書が取得できず、業績・成長性の評価が限定的です。")
    if not yd["bs_rows"]:
        warnings.append("貸借対照表が取得できず、財務の評価が限定的です。")
    if not yd["divs"]:
        warnings.append("配当履歴を取得できませんでした（無配、またはyfinance未収録）。")
    if is_reit:
        warnings.append("REIT：FFO・NAV倍率・LTV・分配金の内訳が重要で、株式向けのこのツールでは正しく評価できません。参考程度に。")
    if is_simple and not is_reit:
        warnings.append("銀行・保険は簡易判定です。業績・財務・CFの指標は構造的に別基準のため参考表示のみです。")

    pdate = yd.get("price_date")
    meta = {
        "code": ticker, "name": name, "gics_sector": gics, "industry": industry, "sector": ysector,
        "price": yd["price"], "mcap": info.get("marketCap"),
        "price_date": pdate.isoformat() if pdate else None,
        "today": TODAY.isoformat(), "is_simple": is_simple,
    }

    log("[3/3] rendering")
    try:
        res["html"] = render_html_us(meta, detail, groups, sel_score, tim_score, vd, M, ctx, warnings, rules)
        res["md"] = render_md_us(meta, detail, groups, sel_score, tim_score, vd, M, ctx, rules)
    except Exception as e:
        import traceback
        res["error"] = f"render failed: {e}\n{traceback.format_exc()}"
        return res

    rowmap = {r["key"]: r for dom in detail for r in detail[dom] if r.get("key")}

    def gv(k):
        it = rowmap.get(k)
        return it.get("v") if it else None

    _, lab_sel = cov_label_us(coverage["選定"])
    _, lab_tim = cov_label_us(coverage["買い時"])
    ea = ctx.get("earn") or {}
    res["summary"] = {
        "code": ticker, "name": name, "asof": TODAY.isoformat(),
        "gics_sector": gics, "industry": industry, "is_simple": is_simple, "is_reit": is_reit,
        "price": yd["price"], "price_date": meta["price_date"], "mcap": info.get("marketCap"),
        "sel_score": round(sel_score, 1) if is_num(sel_score) else None,
        "tim_score": round(tim_score, 1) if is_num(tim_score) else None,
        "sel_label": vd.get("sel_label"), "tim_label": vd.get("tim_label"),
        "comment": vd.get("comment"),
        "cov_sel": [coverage["選定"][0], coverage["選定"][1], lab_sel],
        "cov_tim": [coverage["買い時"][0], coverage["買い時"][1], lab_tim],
        "groups": {g: (round(v, 1) if is_num(v) else None) for g, v in groups.items()},
        "div_yield": gv("div_yield"), "total_yield": gv("total_yield"),
        "streak_up": gv("streak_up"), "streak_flat": gv("streak_flat"),
        "payout_ni": gv("payout_ni"), "roe": gv("roe"), "chowder": gv("chowder"),
        "interest_coverage": gv("interest_coverage"),
        "per_vs_sector": gv("per_vs_sector"), "pbr_vs_sector": gv("pbr_vs_sector"),
        "per_band_pos": gv("per_band_pos"), "yield_band_pos": gv("yield_band_pos"),
        "next_earn": ea.get("next_earn"),
        "warnings": warnings,
    }
    res["ok"] = True
    res["name"] = name
    return res


def _selftest(tickers):
    """build_metrics_us → analyze.score_all を1銘柄ずつ流して 2スコアを表示。"""
    cfg = load_config_us()
    sec_avg_all, smap, rules = cfg["sec_avg_all"], cfg["smap"], cfg["rules"]
    rate_sensitive = set(smap.get("rate_sensitive", []))
    ust = rules.get("market", {}).get("ust_10y", 4.2)
    for t in tickers:
        try:
            yd = fetch_yf_us(t)
        except Exception as e:
            print(f"{t}: fetch failed {e}")
            continue
        info = yd.get("info") or {}
        gics, ind, ys, simple, reit, src = classify_sector_us(info, smap)
        sec_avg = sec_avg_all.get(gics, {})
        try:
            M, flags, ctx = build_metrics_us(yd, sec_avg, simple, gics, rate_sensitive, ust)
            dom, detail, groups, sel, tim, cov = analyze.score_all(M, gics, rules, simple)
        except Exception as e:
            import traceback
            print(f"{t}: ERROR {e}")
            traceback.print_exc()
            continue
        yv = ctx.get("yld_fwd")
        ty = ctx.get("total_yield")
        icr = ctx.get("icr")
        print(f"{t:6s} {gics:24s} simple={str(simple):5s}  SEL={sel and round(sel,1)}  TIM={tim and round(tim,1)}  "
              f"yield={yv and round(yv,2)}%  total_yield={ty and round(ty,2)}%  icr={icr and round(icr,1)}x  "
              f"cov(sel)={cov['選定']} cov(tim)={cov['買い時']}")


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args and args[0] == "sector":
        import yfinance as yf
        cfg = load_config_us()
        smap = cfg["smap"]
        for t in args[1:] or ["AAPL", "JPM", "O", "AWR", "V", "SPGI"]:
            info = yf.Ticker(t).info or {}
            gics, ind, ys, simple, reit, src = classify_sector_us(info, smap)
            print(f"{t:6s} yahoo={ys!r:22s} industry={ind!r:34s} -> {gics!r:24s} simple={simple} reit={reit}")
    else:
        _selftest(args or ["KO", "PG", "JNJ", "O", "AWR", "XOM", "JPM", "MSFT", "T", "MMM"])
