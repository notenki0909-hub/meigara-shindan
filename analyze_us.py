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


_MACD_EN = {
    "ゴールデンクロス直後（上向き転換）": "golden cross (turning up)",
    "デッドクロス直後（下向き転換）": "dead cross (turning down)",
    "シグナル上（上向き継続）": "above signal (uptrend)",
    "シグナル下（下向き継続）": "below signal (downtrend)",
}


def macd_en(s):
    return _MACD_EN.get(s, s) if s else s


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
            return "—"
        return " ← ".join(fmt_usd(x) for x in s if x is not None)[:120]

    def spairs(s, years):
        if not s:
            return []
        if years and len(years) == len(s):
            pr = [(y, v) for y, v in zip(years, s) if is_num(v)]
        else:
            pr = [(None, v) for v in s if is_num(v)]
        return pr[::-1]

    # ---- Operating performance ----
    rev = analyze.row(isr, "Total Revenue", "Operating Revenue")
    opi = analyze.row(isr, "Operating Income", "Total Operating Income As Reported", "EBIT")
    ni = analyze.row(isr, "Net Income", "Net Income Common Stockholders", "Net Income Continuous Operations")
    eps = analyze.row(isr, "Basic EPS", "Diluted EPS")

    def growth_row(name, s, key, target, years=is_years, kind="usd"):
        xs = [x for x in (s or []) if is_num(x)]
        if len(xs) >= 2:
            c = cagr(xs[-1], xs[0], len(xs) - 1)
            M[target].append({"name": name, "v": c, "disp": sdisp(s),
                              "ref": f"CAGR {fmt_pct(c)} (from last {len(xs)} fiscal years)", "key": key,
                              "series": spairs(s, years), "series_kind": kind})
        else:
            M[target].append({"name": name, "v": None, "disp": "—", "ref": "insufficient data", "key": key})

    growth_row("Revenue (trend / CAGR)", rev, "rev_cagr", "業績")
    xs = [x for x in (eps or []) if is_num(x)]
    if len(xs) >= 2:
        c = cagr(xs[-1], xs[0], len(xs) - 1)
        M["業績"].append({"name": "EPS (trend / CAGR)", "v": c,
                          "disp": " ← ".join(f"${fmt_num(x, 2)}" for x in eps if x is not None)[:120],
                          "ref": f"CAGR {fmt_pct(c)} (from last {len(xs)} fiscal years)", "key": "eps_cagr",
                          "series": spairs(eps, is_years), "series_kind": "eps"})
    else:
        M["業績"].append({"name": "EPS (trend / CAGR)", "v": None, "disp": "—", "ref": "insufficient data", "key": "eps_cagr"})

    op_margin = None
    if rev and opi and is_num(rev[0]) and is_num(opi[0]) and rev[0] != 0:
        op_margin = opi[0] / rev[0] * 100
    opm_series = []
    for i in range(min(len(rev or []), len(opi or []), len(is_years))):
        if is_num(rev[i]) and is_num(opi[i]) and rev[i] != 0:
            opm_series.append((is_years[i], opi[i] / rev[i] * 100))
    M["業績"].append({"name": "Operating margin (latest)", "v": op_margin, "disp": fmt_pct(op_margin),
                      "ref": "sector-dependent", "key": "op_margin",
                      "series": opm_series[::-1], "series_kind": "pct"})

    # Earnings stability = worst year-over-year of operating income
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
    es_disp = ("—" if earn_stab is None else
               "1 loss year → recovered (0.62)" if earn_stab == 0.62 else
               "loss year(s) present (0.00)" if earn_stab == 0 else f"worst YoY {earn_stab:.2f}")
    M["業績"].append({"name": "Earnings stability (operating income volatility)", "v": earn_stab, "disp": es_disp,
                      "ref": "closer to 1.00 = fewer down years. >=0.88 stable, <0.65 cyclical. "
                             "1 loss year then recovery = fixed 0.62; multiple/recent loss years = 0.00",
                      "key": "earnings_stability"})

    growth_row("Operating income (trend / CAGR)", opi, None, "参考")
    growth_row("Net income (trend / CAGR)", ni, None, "参考")

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
        icr_disp = "no debt or data unavailable"
    elif is_num(icr) and icr < 0:
        icr_disp = f"operating loss (ref {icr:.1f}x)"
    elif is_num(icr) and icr > 100:
        icr_disp = f">100x (interest expense negligible = effectively debt-free; ref {icr:.0f}x)"
    elif is_num(icr):
        icr_disp = f"{icr:.1f}x"
    else:
        icr_disp = "data unavailable"

    M["財務"].append({"name": "D/E ratio (total debt / equity)", "v": de,
                      "disp": fmt_num(de, 2) + "x" if is_num(de) else "—",
                      "ref": "under 1x is comfortable (utilities/REITs run higher, that's normal)", "key": "de"})
    M["財務"].append({"name": "Net D/E ratio", "v": net_de,
                      "disp": fmt_num(net_de, 2) + "x" if is_num(net_de) else "—",
                      "ref": "after netting cash. negative = net cash position", "key": "net_de"})
    M["財務"].append({"name": "Total debt / operating CF (years to repay)", "v": debt_to_ocf,
                      "disp": fmt_num(debt_to_ocf, 1) + " yr" if is_num(debt_to_ocf) else "—",
                      "ref": "can it be repaid within a few years", "key": "debt_to_ocf"})
    M["財務"].append({"name": "Interest coverage ratio (EBIT / interest expense)", "v": icr, "disp": icr_disp,
                      "ref": ">=10x comfortable, <3x watch. matters more when rates are high", "key": "interest_coverage"})
    M["参考"].append({"name": "Equity ratio (equity / assets)", "v": None, "disp": fmt_pct(equity_ratio),
                      "ref": "non-standard metric in the US; not scored (use D/E instead)", "key": None})
    M["参考"].append({"name": "ROIC (return on invested capital)", "v": None,
                      "disp": (fmt_pct(roic, 1) if is_num(roic) else "—"),
                      "ref": (f"sector median {fmt_pct(sec_avg.get('roic'))} (vs {fmt_num(roic_vs, 2)}x)" if roic_vs
                              else "no sector average"), "key": None})
    hedge = ("net cash (effectively debt-free) = lower solvency risk" if is_num(nd0) and nd0 < 0 else
             "net debt present; check repayment capacity from operating CF" if is_num(nd0) else "—")
    M["参考"].append({"name": "Solvency hedge (net cash?)", "v": None, "disp": hedge, "ref": "", "key": None})

    # ---- Cash flow ----
    icf = analyze.row(cfr, "Investing Cash Flow")
    fin = analyze.row(cfr, "Financing Cash Flow")
    fcf = analyze.row(cfr, "Free Cash Flow")
    capex = analyze.row(cfr, "Capital Expenditure")
    divpaid = analyze.row(cfr, "Cash Dividends Paid", "Common Stock Dividend Paid")
    icf0 = icf[0] if icf else None
    fin0 = fin[0] if fin else None
    fcf0 = fcf[0] if fcf else (ocf0 + capex[0] if is_num(ocf0) and capex and is_num(capex[0]) else None)

    M["キャッシュフロー"].append({"name": "Operating CF (latest / trend)",
                                    "v": (1 if is_num(ocf0) and ocf0 > 0 else 0) if is_num(ocf0) else None,
                                    "disp": sdisp(ocf), "ref": "should stay positive and stable", "key": "ocf_positive",
                                    "series": spairs(ocf, cf_years), "series_kind": "usd"})
    M["参考"].append({"name": "Investing CF (latest / trend)", "v": None, "disp": sdisp(icf),
                      "ref": "normally negative (reinvesting in the business)", "key": None,
                      "series": spairs(icf, cf_years), "series_kind": "usd"})
    M["参考"].append({"name": "Financing CF (latest / trend)", "v": None, "disp": sdisp(fin),
                      "ref": "tends negative with dividends / buybacks / repayment", "key": None,
                      "series": spairs(fin, cf_years), "series_kind": "usd"})

    buyback_detail = analyze.row(cfr, "Repurchase Of Capital Stock")
    buyback_net = analyze.row(cfr, "Net Common Stock Issuance")
    buyback0, buyback_src = None, None
    if buyback_detail and is_num(buyback_detail[0]):
        buyback0, buyback_src = abs(buyback_detail[0]), "detail line (Repurchase Of Capital Stock)"
    elif buyback_net and is_num(buyback_net[0]):
        buyback0 = abs(buyback_net[0]) if buyback_net[0] < 0 else 0.0
        buyback_src = "net line (issuance netted; not a standalone figure)"
    mcap = info.get("marketCap")
    buyback_yield = (buyback0 / mcap * 100) if is_num(buyback0) and is_num(mcap) and mcap > 0 else None
    M["参考"].append({"name": "Buybacks (latest year)", "v": buyback0,
                      "disp": (f"{fmt_usd(buyback0)} ({buyback_yield:.2f}% of market cap)"
                               if is_num(buyback0) and is_num(buyback_yield) else
                               fmt_usd(buyback0) if is_num(buyback0) else "no data"),
                      "ref": f"source: {buyback_src}" if buyback_src else "no matching line in yfinance", "key": None})
    M["キャッシュフロー"].append({"name": "Free CF (operating CF + investing CF)",
                                    "v": (1 if is_num(fcf0) and fcf0 > 0 else 0) if is_num(fcf0) else None,
                                    "disp": sdisp(fcf), "ref": "consistently positive = room for dividends", "key": "fcf_positive",
                                    "series": spairs(fcf, cf_years), "series_kind": "usd"})
    if is_num(ocf0) and is_num(icf0) and is_num(fin0):
        sg_ = lambda x: "+" if x > 0 else "-"
        note = "healthy (earns in the core business, then invests + returns to holders)" if ocf0 > 0 and icf0 < 0 and fin0 < 0 else "check"
        M["参考"].append({"name": "CF sign pattern", "v": None,
                          "disp": f"Op {sg_(ocf0)} / Inv {sg_(icf0)} / Fin {sg_(fin0)} … {note}", "ref": "", "key": None})
    fcf_payout = None
    if divpaid and is_num(divpaid[0]) and is_num(fcf0) and fcf0 > 0:
        fcf_payout = abs(divpaid[0]) / fcf0 * 100
    M["キャッシュフロー"].append({"name": "FCF payout ratio (dividends paid / free CF)", "v": fcf_payout,
                                    "disp": fmt_pct(fcf_payout),
                                    "ref": "under 70% for stable names. over 100% draws down reserves. REITs: skipped (use FFO)",
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
    dps_src = "yfinance (fiscal-year aggregated)"

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
    range_txt = f"sector yield guide {yb[0]:.1f}-{yb[1]:.1f}%" if yb else "—"
    M["配当"].append({"name": "Forward dividend yield", "v": yld_fwd, "disp": fmt_pct(yld_fwd, 2),
                      "ref": range_txt + ". trend = each calendar year's DPS / that year's mean price",
                      "key": "div_yield", "series": yy, "series_kind": "pct", "series_current": yld_fwd})

    # Total yield = dividend yield + buyback yield  (SCORED for US, in 配当)
    total_yield = (yld_fwd + buyback_yield) if is_num(yld_fwd) and is_num(buyback_yield) else None
    ni0 = ni[0] if ni and is_num(ni[0]) else None
    divpaid0 = abs(divpaid[0]) if divpaid and is_num(divpaid[0]) else None
    total_return_amt = (divpaid0 or 0) + (buyback0 or 0) if (is_num(divpaid0) or is_num(buyback0)) else None
    total_payout = (total_return_amt / ni0 * 100) if is_num(total_return_amt) and is_num(ni0) and ni0 > 0 else None
    M["配当"].append({"name": "Total yield (dividend + buyback)", "v": total_yield,
                      "disp": (f"{fmt_pct(yld_fwd,2)} (div) + {fmt_pct(buyback_yield,2)} (buyback) = {fmt_pct(total_yield,2)}"
                               if is_num(total_yield) else "not available (no buyback data)"),
                      "ref": "the full picture of shareholder returns. buybacks are a single-year figure, "
                             "not a recurring commitment — don't over-rely on one year", "key": "total_yield"})
    payout_disp = ("net income negligible — not meaningful" if is_num(total_payout) and total_payout > 300
                   else fmt_pct(total_payout))
    M["参考"].append({"name": "Total payout ratio ((dividend + buyback) / net income)", "v": None, "disp": payout_disp,
                      "ref": "over 100% = returning more than that year's profit", "key": None})

    M["配当"].append({"name": "Dividend growth rate (5-yr CAGR)", "v": dgr5, "disp": fmt_pct(dgr5),
                      "ref": f">=5% is the bar for US names (>=0% passes). source: {dps_src}", "key": "dgr5"})
    M["配当"].append({"name": "Consecutive years of increases", "v": streak_up,
                      "disp": f"{streak_up} yr" if is_num(streak_up) else "—",
                      "ref": "25+ = Dividend Aristocrat territory, 50+ = Dividend King", "key": "streak_up"})
    M["配当"].append({"name": "Consecutive years without a cut", "v": streak_flat,
                      "disp": f"{streak_flat} yr" if is_num(streak_flat) else "—",
                      "ref": "has the dividend held through downturns", "key": "streak_flat"})
    pn = sec_avg.get("payout")
    pn_txt = f"sector guide {pn[0]}-{pn[1]}%" if pn else "—"
    M["配当"].append({"name": "Payout ratio (net income basis)", "v": payout_ni, "disp": fmt_pct(payout_ni),
                      "ref": pn_txt + ". over 80% is a warning. REITs: use FFO basis (this GAAP number runs >100%)",
                      "key": "payout_ni"})
    M["配当"].append({"name": "ROE (efficiency of the dividend's source)", "v": roe, "disp": fmt_pct(roe),
                      "ref": f"sector median {fmt_pct(sec_avg.get('roe'))}. 10%+ is strong" if sec_avg.get("roe") is not None else "10%+ is strong",
                      "key": "roe"})

    # 減配履歴
    last_cut_fy = find_last_cut(dps_series) if len(dps_series) >= 3 else None
    if len(dps_series) < 3:
        cut_disp = "insufficient data"
    elif last_cut_fy is None:
        cut_disp = "no cut within the data window"
    else:
        yrs_txt = f"{streak_flat} yr" if is_num(streak_flat) else "?"
        if is_num(streak_flat) and streak_flat >= 10:
            cut_disp = f"last cut ~{last_cut_fy}; no cut for {yrs_txt} since = currently sound"
        elif is_num(streak_flat) and streak_flat >= 5:
            cut_disp = f"last cut ~{last_cut_fy} ({yrs_txt} ago). recovering"
        else:
            cut_disp = f"last cut ~{last_cut_fy} ({yrs_txt} ago). recent — watch"
    M["参考"].append({"name": "Dividend cut history", "v": None, "disp": cut_disp,
                      "ref": "the fact behind the 'years without a cut' streak. a past cut is not a permanent penalty "
                             "if enough clean years have followed", "key": None})

    # ---- Valuation ----
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

    M["期待"].append({"name": "P/E (trailing, vs sector)", "v": per_vs, "disp": (fmt_num(per, 1) + "x" if per else "—"),
                      "ref": f"sector avg {fmt_num(sec_avg.get('per'),1)}x (vs {fmt_num(per_vs,2)}x)" if per_vs else "—",
                      "key": "per_vs_sector"})
    M["期待"].append({"name": "P/B (vs sector)", "v": pbr_vs, "disp": (fmt_num(pbr, 2) + "x" if pbr else "—"),
                      "ref": f"sector avg {fmt_num(sec_avg.get('pbr'),1)}x" if pbr_vs else "—", "key": "pbr_vs_sector"})
    if per_range:
        M["期待"].append({"name": "P/E position within its own historical range", "v": per_band_pos,
                          "disp": f"range {per_range[0]:.1f}-{per_range[1]:.1f}x / now {per:.1f}x = cheapness {per_band_pos*100:.0f}/100",
                          "ref": "0 = top of range (high P/E = expensive) / 100 = bottom (cheap)", "key": "per_band_pos", "rangeband": rb_per})
    else:
        M["期待"].append({"name": "P/E position within its own historical range", "v": None,
                          "disp": "insufficient history", "ref": "needs 3+ years of price & EPS", "key": "per_band_pos"})
    M["参考"].append({"name": "Earnings yield (1 / P/E)", "v": None, "disp": fmt_pct(ey),
                      "ref": "compare against the 10-year Treasury", "key": None})
    if yrange:
        M["期待"].append({"name": "Dividend yield theory (position in own historical range)", "v": yband_pos,
                          "disp": f"range {yrange[0]:.1f}-{yrange[1]:.1f}% / now {yld_fwd:.1f}% = cheapness {yband_pos*100:.0f}/100",
                          "ref": "0 = bottom of range (low yield = expensive) / 100 = top (high yield = cheap)",
                          "key": "yield_band_pos", "rangeband": rb_yield})
    else:
        M["期待"].append({"name": "Dividend yield theory (position in own range)", "v": None,
                          "disp": "insufficient history", "ref": "needs 5+ years of price & dividends", "key": "yield_band_pos"})
    M["期待"].append({"name": "Chowder rule (yield + 5-yr DGR)", "v": chowder, "disp": fmt_pct(chowder),
                      "ref": "12%+ passes (8%+ for utilities/telecom)", "key": "chowder"})
    if rate_sens:
        M["期待"].append({"name": "Yield minus 10-year Treasury (spread)", "v": yield_spread,
                          "disp": (f"{yld_fwd:.2f}% - {ust_10y:.2f}% = {yield_spread:+.2f}%" if yield_spread is not None else "yield unknown"),
                          "ref": f"'bond substitute' sectors. wider = cheaper. UST 10y = {fmt_num(ust_10y,2)}%. "
                                 f"US rates are high so a spread near 0 is already attractive",
                          "key": "yield_spread"})
    else:
        M["期待"].append({"name": "Yield minus 10-year Treasury (spread)", "v": None,
                          "disp": "not a rate-sensitive sector — not scored",
                          "ref": "scored only for utilities / real estate / communication services / consumer staples", "key": "yield_spread"})

    # ---- Technicals (reference only) ----
    tec = calc_technicals(yd["hist_d"])
    M["参考"].append({"name": "RSI(14)", "v": None,
                      "disp": (f"{tec['rsi']:.0f}" if tec["rsi"] is not None else "—") +
                              ("  oversold" if tec["rsi"] is not None and tec["rsi"] < 30 else
                               "  overbought" if tec["rsi"] is not None and tec["rsi"] >= 70 else "  neutral"),
                      "ref": "too short-term to score; shown for context", "key": None})
    M["参考"].append({"name": "MACD(12,26,9)", "v": None, "disp": macd_en(tec["macd_state"]) or "—", "ref": "", "key": None})

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
SEL_LABEL = {"hi": "Selection score: top tier (quality & durability both high)",
             "mid": "Selection score: mid tier (some weak spots)",
             "lo": "Selection score: low tier (quality concerns)",
             "xlo": "Selection score: below threshold",
             None: "N/A (insufficient data)"}
TIM_LABEL = {"hi": "Timing score: top tier (cheap zone)",
             "mid": "Timing score: mid tier (fairly valued)",
             "lo": "Timing score: low tier (somewhat expensive)",
             "xlo": "Timing score: bottom tier (expensive)",
             None: "N/A (P/E, P/B, yield history insufficient)"}
QUADRANT = {
    ("hi", "hi"): "Both selection and timing are top tier.",
    ("hi", "mid"): "Selection is top tier; timing is mid (fairly valued).",
    ("hi", "lo"): "Selection is top tier, but timing is low (somewhat expensive).",
    ("hi", "xlo"): "Selection is top tier, but timing is bottom tier (expensive).",
    ("mid", "hi"): "Timing is top tier (cheap), but selection is mid (some weak spots).",
    ("mid", "mid"): "Both selection and timing are mid tier.",
    ("mid", "lo"): "Selection is mid; timing is low (somewhat expensive).",
    ("mid", "xlo"): "Selection is mid; timing is bottom tier (expensive).",
    ("lo", "hi"): "Timing is top tier (cheap), but selection is low (quality concerns) — a low score usually has a reason (possible value trap).",
    ("lo", "mid"): "Selection score is below the passing bar.",
    ("lo", "lo"): "Both selection and timing are low tier.",
    ("lo", "xlo"): "Selection is low; timing is bottom tier.",
    ("xlo", "hi"): "Timing is top tier (cheap), but selection is bottom tier — a low score usually has a reason (possible value trap).",
    ("xlo", "mid"): "Selection score is well below the bar.",
    ("xlo", "lo"): "Both selection and timing are low tier.",
    ("xlo", "xlo"): "Both selection and timing are bottom tier.",
}
DISC_HTML = (
    "Educational general information only. The 'Selection' and 'Timing' scores/labels are produced "
    "mechanically from public data using predefined rules, and are not a substitute for investment "
    "advice. The operator is not a registered investment adviser. Figures are sourced from yfinance "
    "(Yahoo Finance) and may contain errors, delays or gaps. Sector averages and thresholds are "
    "rough 2026 guides. Do your own research against primary sources (10-K, 10-Q). See the terms page."
)


def _tier(score, tiers):
    return analyze._tier(score, tiers)


def cov_label_us(pair):
    scored, possible = pair
    if possible <= 0:
        return 0.0, "N/A"
    r = scored / possible
    return r, ("high" if r >= 0.85 else "mid" if r >= 0.65 else "low")


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
        val_label = "N/A (P/E, P/B, yield history insufficient)"
    elif val_idx >= 0.12:
        val_label = "cheap zone (below sector avg / low in its own range)"
    elif val_idx <= -0.12:
        val_label = "expensive zone (above sector avg / high vs its range)"
    else:
        val_label = "roughly fair value"
    if pbr and pbr < 1:
        val_label += " / P/B below 1x"

    fin = dom_scores.get("財務")
    cf = dom_scores.get("キャッシュフロー")
    stab_vals = [x for x in (fin, cf) if x is not None]
    stab = sum(stab_vals) / len(stab_vals) if stab_vals else None
    if is_simple:
        stab_label = "not scored in simple mode (financials/CF are structurally different)"
    elif stab is None:
        stab_label = "N/A"
    else:
        stab_label = "high" if stab >= 88 else "medium" if stab >= 68 else "low (watch debt / CF)"

    rc = analyze.cagr_of(ctx["rev_series"])
    ec = analyze.cagr_of(ctx["eps_series"])
    if is_simple:
        grow_label = "not scored in simple mode (see reference section)"
    elif rc is None and ec is None:
        grow_label = "N/A"
    elif (rc or 0) > 3 and (ec or 0) > 3:
        grow_label = "expanding (revenue and EPS both up)"
    elif (rc or -99) >= 0 and (ec or -99) >= -2:
        grow_label = "flat to slightly up"
    else:
        grow_label = "shrinking (check the dividend isn't propped up by a rising payout ratio)"

    tec = ctx.get("tec") or {}
    rsi = tec.get("rsi")
    macd = tec.get("macd_state")
    bits = []
    if is_num(rsi):
        bits.append(f"RSI {rsi:.0f}" + (" (oversold)" if rsi < 30 else " (overbought)" if rsi >= 70 else " (neutral)"))
    if macd:
        bits.append(macd_en(str(macd)))
    short_label = " / ".join(bits) if bits else "—"

    st_tier = _tier(sel_score, SEL_TIERS)
    ti_tier = _tier(tim_score, TIM_TIERS)
    quad = QUADRANT.get((st_tier or "mid", ti_tier or "mid"), "—")
    sel_cov = cov_label_us(coverage.get("選定", (0, 0)))
    tim_cov = cov_label_us(coverage.get("買い時", (0, 0)))
    if "low" in (sel_cov[1], tim_cov[1]):
        which = " and ".join(n for n, c in (("selection", sel_cov[1]), ("timing", tim_cov[1])) if c == "low")
        quad = f"Note: limited data ({which} coverage low) — scores are indicative only. " + quad

    return {
        "sel_label": SEL_LABEL[st_tier], "tim_label": TIM_LABEL[ti_tier],
        "comment": quad, "valuation": val_label, "growth": grow_label,
        "stability": stab_label, "short": short_label, "val_idx": val_idx,
        "sel_cov": (coverage.get("選定", (0, 0)), sel_cov),
        "tim_cov": (coverage.get("買い時", (0, 0)), tim_cov),
    }


# ---------------------------------------------------------------- レンダリング
GROUP_LABEL_EN = {
    "配当利回りセオリー": "Dividend yield theory",
    "利回り水準とChowder": "Yield level & Chowder",
    "株価バリュエーション": "Valuation vs sector",
    "金利スプレッド": "Rate spread",
    "業績": "Operating performance",
    "財務": "Financial strength",
    "キャッシュフロー": "Cash flow",
    "配当の持続力": "Dividend durability",
}
LABEL_MARK = {"good": "◎", "warn": "△", "bad": "▲", None: "—"}


def _bar_us(score):
    if not is_num(score):
        return '<span class="bar"><i class="fill na" style="width:0"></i></span><span class="sc na">n/a</span>'
    w = max(0, min(100, score))
    cls = "hi" if score >= 90 else "mid" if score >= 65 else "lo"
    return (f'<span class="bar"><i class="fill {cls}" style="width:{w:.0f}%"></i></span>'
            f'<span class="sc {cls}">{score:.0f}</span>')


def _metric_row_us(it):
    lab = it.get("label")
    mark = LABEL_MARK.get(lab, "—")
    sc = it.get("score")
    sc_txt = f"{sc:.0f}" if is_num(sc) else "—"
    name = analyze.html.escape(str(it["name"]))
    disp = analyze.html.escape(str(it.get("disp", "—")))
    ref = analyze.html.escape(str(it.get("ref", "")))
    return (f'<details class="m"><summary>'
            f'<span class="mn">{name}</span>'
            f'<span class="mv mv2">{disp}</span>'
            f'<span class="mt"><b>{mark}</b> {sc_txt}</span></summary>'
            f'<div class="mbody"><p class="rule">{ref}</p></div></details>')


def _group_section_us(head, sg, groups, rowmap):
    out = []
    for gname, gdef in sg[head].items():
        glabel = GROUP_LABEL_EN.get(gname, gname)
        out.append(f'<div class="domhead"><b>{glabel}</b> {_bar_us(groups.get(gname))}</div>')
        got = False
        for k in gdef["keys"]:
            it = rowmap.get(k)
            if it is None:
                continue
            got = True
            out.append(_metric_row_us(it))
        if not got:
            out.append('<div class="plain"><span class="mn">—</span><span class="mv2">not evaluated for this sector</span></div>')
    return "".join(out)


def render_company_html_us(co):
    if not co or not co.get("has"):
        return ""
    rows = []
    loc = ", ".join(x for x in (co.get("city"), co.get("country")) if x)
    if loc:
        rows.append(f'<div class="plain"><span class="mn">Headquarters</span><span class="mv2">{analyze.html.escape(loc)}</span></div>')
    if is_num(co.get("employees")):
        rows.append(f'<div class="plain"><span class="mn">Employees</span><span class="mv2">{co["employees"]:,}</span></div>')
    if co.get("website"):
        u = analyze.html.escape(co["website"])
        rows.append(f'<div class="plain"><span class="mn">Website</span><span class="mv2"><a href="{u}" target="_blank" rel="noopener">{u}</a></span></div>')
    summary_p = ""
    if co.get("summary"):
        summary_p = f'<p class="rule"><b>Business (source: Yahoo Finance)</b></p><p style="white-space:pre-wrap">{analyze.html.escape(co["summary"])}</p>'
    return (f'<details class="chartbox"><summary>Company overview</summary>'
            f'<div class="mbody">{"".join(rows)}{summary_p}</div></details>')


_RECO_EN = {"strong_buy": "strong buy", "buy": "buy", "hold": "hold",
            "underperform": "underperform", "sell": "sell", "none": "—"}
_QNAME_EN = {"売上高": "Revenue", "営業利益": "Operating income", "純利益": "Net income",
             "EPS（四半期）": "EPS (quarter)"}


def _earn_rows_us(ea):
    """(heading, body) pairs from the yfinance earnings aggregate — USD / English."""
    R = []
    epr = ea.get("eps_reported")
    if is_num(epr):
        s = f"reported ${fmt_num(epr, 2)}"
        if is_num(ea.get("eps_est")):
            s += f" / estimate ${fmt_num(ea['eps_est'], 2)}"
        if is_num(ea.get("surprise")):
            s += f" / surprise {ea['surprise']:+.1f}%"
        head = "Latest quarter EPS" + (f" (disclosed {ea['disc_date']})" if ea.get("disc_date") else "")
        R.append((head, s))
    qs = [x for x in (ea.get("q") or []) if x.get("unit") != "円"]
    if qs:
        parts = []
        for x in qs:
            nm = _QNAME_EN.get(x["name"], x["name"])
            t = f"{nm} {fmt_usd(x['val'])}"
            if is_num(x.get("yoy")):
                t += f" ({x['yoy']:+.1f}% YoY)"
            parts.append(t)
        R.append(("Latest quarter" + (f" ({ea['q_date']})" if ea.get("q_date") else ""), " / ".join(parts)))
    fw = []
    for pk in ("0y", "+1y"):
        f = (ea.get("fwd") or {}).get(pk)
        if not f:
            continue
        t = f"{'this year' if pk == '0y' else 'next year'} EPS ${fmt_num(f['eps'], 2)}"
        if is_num(f.get("growth")):
            t += f" ({f['growth']:+.1f}% vs prior)"
        if is_num(f.get("per")):
            t += f" / fwd P/E {fmt_num(f['per'], 1)}x"
        if is_num(f.get("n")):
            t += f" / {int(f['n'])} analysts"
        fw.append(t)
    if ea.get("fwd_rev") and is_num(ea["fwd_rev"].get("avg")):
        rr = ea["fwd_rev"]
        t = f"this year revenue {fmt_usd(rr['avg'])}"
        if is_num(rr.get("growth")):
            t += f" ({rr['growth']:+.1f}% vs prior)"
        fw.append(t)
    if fw:
        R.append(("Analyst outlook", " / ".join(fw)))
    if ea.get("next_earn"):
        R.append(("Next report (est.)", str(ea["next_earn"])))
    t = ea.get("target")
    if t and is_num(t.get("mean")):
        s = f"mean ${fmt_num(t['mean'], 2)}"
        if is_num(t.get("vs")):
            s += f" ({t['vs']:+.1f}% vs current price)"
        if is_num(t.get("high")) and is_num(t.get("low")):
            s += f" / high ${fmt_num(t['high'], 2)} · low ${fmt_num(t['low'], 2)}"
        R.append(("Analyst target price", s))
    rc = ea.get("reco") or {}
    if rc.get("key") or is_num(rc.get("mean")):
        s = _RECO_EN.get((rc.get("key") or "none"), rc.get("key") or "—")
        if is_num(rc.get("mean")):
            s += f" ({rc['mean']:.2f} / 1=buy … 5=sell)"
        if is_num(rc.get("n")):
            s += f" / {int(rc['n'])} analysts"
        R.append(("Analyst rating", s))
    return R


def _earn_block_us(ea):
    if not ea or not ea.get("has"):
        return ""
    rows = "".join(f'<div class="plain"><span class="mn">{analyze.html.escape(h)}</span>'
                   f'<span class="mv2">{analyze.html.escape(b)}</span></div>' for h, b in _earn_rows_us(ea))
    disc = ("yfinance (Yahoo Finance) analyst aggregation. Quarterly EPS actual/estimate are yfinance "
            "conversions and carry error. Always confirm against the company's 10-Q / 10-K / press release.")
    return f'<h2>Recent earnings &amp; analyst view</h2><div class="legend">{disc}</div>{rows}'


_US_CSS = """
:root{ --bg:#ffffff; --fg:#1d232b; --muted:#6b7683; --line:#e4e8ec; --card:#f7f9fa;
  --accent:#2f9e91; --hi:#2f9e91; --mid:#e0912f; --lo:#d1584f; --na:#c3ccd3; }
@media (prefers-color-scheme:dark){ :root{ --bg:#161a1e; --fg:#e7ecef; --muted:#9aa6af;
  --line:#2c333a; --card:#1e242a; --accent:#4fb8ab; --hi:#4fb8ab; --mid:#e0a45a; --lo:#e07b73; --na:#4a555e; } }
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:"Segoe UI",system-ui,-apple-system,sans-serif;line-height:1.6;font-size:14px}
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
.sblk .cov.low b{color:var(--lo)} .sblk .cov.mid b{color:var(--mid)}
.quad{margin:10px 0 14px;padding:10px 14px;border-left:4px solid var(--accent);background:var(--card);border-radius:6px;font-size:13.5px;font-weight:700}
.legend{font-size:11.5px;color:var(--muted);margin:2px 0 12px;line-height:1.5}
.domhead{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin:16px 0 0;font-size:14px}
details.m{border-bottom:1px solid var(--line)}
details.m>summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:minmax(150px,1.4fr) 1fr 96px;gap:10px;align-items:start;padding:9px 6px 9px 22px;position:relative;font-size:13px}
details.m>summary::-webkit-details-marker{display:none}
details.m>summary::before{content:"\\25B8";position:absolute;left:6px;top:9px;color:var(--muted)}
details.m[open]>summary::before{content:"\\25BE"}
details.m>summary:hover{background:var(--card)}
details.m .mv{font-variant-numeric:tabular-nums}
details.m .mt{text-align:right;white-space:nowrap}
.mv2{font-size:12.5px}
.mbody{padding:2px 14px 14px 22px;background:var(--card);font-size:12.5px}
.mbody p{margin:6px 0} .mbody .rule{color:var(--muted)}
.plain{display:grid;grid-template-columns:minmax(150px,1fr) 2fr;gap:10px;padding:9px 6px 9px 22px;border-bottom:1px solid var(--line);font-size:13px}
.bar{display:inline-block;width:150px;height:9px;border-radius:5px;background:var(--line);overflow:hidden;vertical-align:middle;margin:0 8px}
.fill{height:100%} .fill.hi{background:var(--hi)} .fill.mid{background:var(--mid)} .fill.lo{background:var(--lo)} .fill.na{background:var(--na)}
.sc{font-weight:700} .sc.hi{color:var(--hi)} .sc.mid{color:var(--mid)} .sc.lo{color:var(--lo)} .sc.na{color:var(--muted);font-weight:400;font-size:12px}
.gauge{position:relative;height:30px;border-radius:6px;margin:10px 0 4px;background:linear-gradient(90deg,var(--lo),var(--na) 50%,var(--hi))}
.gauge i{position:absolute;top:-4px;width:2px;height:38px;background:var(--fg)}
.gauge b{position:absolute;font-size:10px;color:#fff;top:7px}
.gl{left:8px} .gr{right:8px}
.warn{background:color-mix(in srgb,var(--mid) 12%,var(--bg));border:1px solid var(--mid);border-radius:10px;padding:10px 14px;margin:14px 0;font-size:12.5px}
.warn ul{margin:6px 0 0;padding-left:18px}
.muted{color:var(--muted)}
.lines div{padding:3px 0;border-bottom:1px dashed var(--line)}
.lines div:last-child{border:0}
.lines k{display:inline-block;width:110px;color:var(--muted);font-size:12px}
.disc{margin-top:30px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:11.5px}
.topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.topbar a{font-size:12.5px;white-space:nowrap}
a{color:var(--accent)}
details.chartbox{border:1px solid var(--line);border-radius:8px;margin:8px 0 4px;background:var(--card)}
details.chartbox>summary{list-style:none;cursor:pointer;padding:8px 12px;font-size:12.5px;font-weight:700}
details.chartbox>summary::-webkit-details-marker{display:none}
details.chartbox .mbody{background:transparent}
@media(max-width:560px){ details.m>summary{grid-template-columns:1fr 84px} details.m>summary .mv{display:none} .plain{grid-template-columns:1fr} }
"""


def render_html_us(meta, detail, groups, sel_score, tim_score, vd, M, ctx, warnings, rules):
    rowmap = {r["key"]: r for dom in detail for r in detail[dom] if r.get("key")}
    sg = rules["score_groups"]
    sel_blocks = _group_section_us("選定", sg, groups, rowmap)
    tim_blocks = _group_section_us("買い時", sg, groups, rowmap)

    ref_blocks = []
    for it in M.get("参考", []):
        nm = analyze.html.escape(str(it["name"]))
        disp = analyze.html.escape(str(it.get("disp", "—")))
        ref = it.get("ref", "")
        link = f' <a href="{analyze.html.escape(ref)}" target="_blank" rel="noopener">{analyze.html.escape(ref)}</a>' if str(ref).startswith("http") else ""
        rtxt = "" if str(ref).startswith("http") else analyze.html.escape(str(ref))
        if rtxt:
            ref_blocks.append(f'<details class="m"><summary><span class="mn">{nm}</span>'
                              f'<span class="mv mv2">{disp}{link}</span><span class="mt"></span></summary>'
                              f'<div class="mbody"><p class="rule">{rtxt}</p></div></details>')
        else:
            ref_blocks.append(f'<div class="plain"><span class="mn">{nm}</span><span class="mv2">{disp}{link}</span></div>')

    warn_html = ""
    if warnings:
        warn_html = '<div class="warn"><b>Data notes</b><ul>' + "".join(f"<li>{analyze.html.escape(w)}</li>" for w in warnings) + "</ul></div>"

    gauge_pos = 50
    if vd.get("val_idx") is not None:
        gauge_pos = max(2, min(98, 50 - vd["val_idx"] * 180))

    def _cls(sc, tiers):
        t = _tier(sc, tiers)
        return "hi" if t == "hi" else "mid" if t == "mid" else "lo"

    sel_cls, tim_cls = _cls(sel_score, SEL_TIERS), _cls(tim_score, TIM_TIERS)
    sel_n = f"{sel_score:.0f}" if is_num(sel_score) else "—"
    tim_n = f"{tim_score:.0f}" if is_num(tim_score) else "—"
    sel_w = min(100, sel_score) if is_num(sel_score) else 0
    tim_w = min(100, tim_score) if is_num(tim_score) else 0
    (sel_sc, sel_ps), (_, sel_cl) = vd["sel_cov"]
    (tim_sc, tim_ps), (_, tim_cl) = vd["tim_cov"]

    simple_note = ' | <b>Simple mode</b> (banks/insurance/REITs: operating/financial/CF metrics shown for reference only)' if meta["is_simple"] else ""
    price_s = f"${fmt_num(meta['price'], 2)}" if is_num(meta["price"]) else "—"
    pdate_s = f" (close {meta['price_date']})" if meta.get("price_date") else ""
    mcap_s = fmt_usd(meta["mcap"])

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{analyze.html.escape(meta['name'])} ({meta['code']}) — dividend screening</title>
<style>{_US_CSS}</style></head><body><div class="wrap">
<div class="topbar"><h1>{analyze.html.escape(meta['name'])} ({meta['code']}) — dividend screening</h1><a href="../index.html">&larr; back to list</a></div>
<div class="sub">GICS sector: <b>{meta['gics_sector']}</b> (yfinance: {analyze.html.escape(meta['industry'] or '—')} / {analyze.html.escape(meta['sector'] or '—')}){simple_note}<br>
Price {price_s}{pdate_s} &nbsp;|&nbsp; Market cap {mcap_s} &nbsp;|&nbsp; Generated {meta['today']}</div>

{render_company_html_us(ctx.get('company'))}
{warn_html}

<div class="top"><div class="score2">
  <div class="sblk">
    <div class="hd"><b>Selection</b><span class="num {sel_cls}">{sel_n}</span><span class="lab {sel_cls}">{vd['sel_label']}</span></div>
    <div class="gbar"><i class="{sel_cls}" style="width:{sel_w:.0f}%"></i></div>
    <div class="cov {sel_cl}">scored {sel_sc}/{sel_ps} &nbsp; coverage <b>{sel_cl}</b></div>
    <div class="sub">= quality of the dividend (operating performance / financial strength / cash flow / dividend durability)<br>
      Growth: {vd['growth']} &nbsp;/&nbsp; Stability: {vd['stability']}</div>
  </div>
  <div class="sblk">
    <div class="hd"><b>Timing</b><span class="num {tim_cls}">{tim_n}</span><span class="lab {tim_cls}">{vd['tim_label']}</span></div>
    <div class="gbar"><i class="{tim_cls}" style="width:{tim_w:.0f}%"></i></div>
    <div class="cov {tim_cl}">scored {tim_sc}/{tim_ps} &nbsp; coverage <b>{tim_cl}</b></div>
    <div class="sub">= how the current price looks (yield theory / yield level &amp; Chowder / valuation vs sector / rate spread)<br>
      Valuation: {vd['valuation']} &nbsp;/&nbsp; Short-term: {vd['short']}</div>
  </div>
</div></div>
<div class="quad">&rarr; {vd['comment']}</div>

<div class="gauge"><b class="gl">cheap</b><b class="gr">expensive</b><i style="left:{gauge_pos:.0f}%"></i></div>
<div class="muted" style="font-size:11.5px">Blend of P/E &amp; P/B vs sector and the dividend yield's position in its own historical range.</div>

<h2>1. Selection metrics</h2>
<div class="legend">Click a row for the scoring rule and why it scored this way. Labels: &#9678; good / &#9651; watch / &#9650; weak / &mdash; n/a. Score is linear between the warn (60) and good (100) thresholds; a bonus line goes to 110, floor is 20. Group score = mean of its metrics. Selection weights: performance 28 / financials 27 / CF 15 / dividend durability 30. Timing weights: yield theory 38 / yield &amp; Chowder 24 / valuation 20 / rate spread 18.{' In simple mode, operating/financial/CF groups are not scored — selection comes from dividend durability only.' if meta['is_simple'] else ''}</div>
{sel_blocks}

<h2>2. Timing metrics</h2>
{tim_blocks}

{_earn_block_us(ctx.get('earn'))}

<h2>Reference (not auto-scored)</h2>
<div class="legend">Some rows expand for an explanation.</div>
{''.join(ref_blocks)}

<div class="disc">{DISC_HTML}</div>
</div></body></html>"""


def render_md_us(meta, detail, groups, sel_score, tim_score, vd, M, ctx, rules):
    sg = rules["score_groups"]
    rowmap = {r["key"]: r for dom in detail for r in detail[dom] if r.get("key")}
    L = [f"# {meta['name']} ({meta['code']}) — dividend screening", ""]
    L.append(f"- GICS sector: **{meta['gics_sector']}**"
             + (" (simple mode)" if meta["is_simple"] else ""))
    L.append(f"- Price: ${fmt_num(meta['price'], 2)}  |  Market cap: {fmt_usd(meta['mcap'])}  |  Generated: {meta['today']}")
    L.append("")
    sel_n = f"{sel_score:.0f}" if is_num(sel_score) else "—"
    tim_n = f"{tim_score:.0f}" if is_num(tim_score) else "—"
    L.append(f"## Selection {sel_n} — {vd['sel_label']}")
    L.append(f"Growth: {vd['growth']}  /  Stability: {vd['stability']}")
    L.append("")
    L.append(f"## Timing {tim_n} — {vd['tim_label']}")
    L.append(f"Valuation: {vd['valuation']}  /  Short-term: {vd['short']}")
    L.append("")
    L.append(f"> {vd['comment']}")
    for head in ("選定", "買い時"):
        L.append("")
        L.append(f"### {'Selection metrics' if head == '選定' else 'Timing metrics'}")
        for gname, gdef in sg[head].items():
            gs = groups.get(gname)
            L.append(f"\n**{GROUP_LABEL_EN.get(gname, gname)}** — {gs:.0f}" if is_num(gs) else f"\n**{GROUP_LABEL_EN.get(gname, gname)}** — n/a")
            for k in gdef["keys"]:
                it = rowmap.get(k)
                if it is None:
                    continue
                mark = LABEL_MARK.get(it.get("label"), "—")
                sc = it.get("score")
                sc_txt = f"{sc:.0f}" if is_num(sc) else "—"
                L.append(f"- {mark} {sc_txt}  {it['name']}: {it.get('disp', '—')}")
    L.append("")
    L.append("### Reference")
    for it in M.get("参考", []):
        L.append(f"- {it['name']}: {it.get('disp', '—')}")
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
        res["error"] = "no data (check the ticker; US-listed only)"
        return res

    name = info.get("longName") or info.get("shortName") or ticker
    gics, industry, ysector, is_simple, is_reit, src = classify_sector_us(info, smap)
    sec_avg = sec_avg_all.get(gics, {})

    log("[2/3] scoring")
    try:
        M, flags, ctx = build_metrics_us(yd, sec_avg, is_simple, gics, rate_sensitive, ust_10y)
        ctx["earn"] = analyze.build_earnings(yd, yd["price"])
        ctx["company"] = analyze.build_company_overview(info)
        dom_scores, detail, groups, sel_score, tim_score, coverage = analyze.score_all(M, gics, rules, is_simple)
        vd = verdicts_us(sel_score, tim_score, groups, dom_scores, ctx, sec_avg, is_simple, coverage)
    except Exception as e:
        import traceback
        res["error"] = f"scoring failed: {e}\n{traceback.format_exc()}"
        return res

    warnings = []
    for nm, key in (("Selection", "選定"), ("Timing", "買い時")):
        _, lab = cov_label_us(coverage[key])
        if lab == "low":
            warnings.append(f"{nm} score has few scored metrics (coverage low) — treat the number as indicative.")
    if not yd["is_rows"]:
        warnings.append("Income statement unavailable — operating/growth assessment is limited.")
    if not yd["bs_rows"]:
        warnings.append("Balance sheet unavailable — financial-strength assessment is limited.")
    if not yd["divs"]:
        warnings.append("No dividend history retrieved (non-payer, or not in yfinance).")
    if is_reit:
        warnings.append("REIT: FFO / NAV multiples / LTV / distribution composition matter here and this equity-oriented tool can't judge them properly. Reference only.")
    if is_simple and not is_reit:
        warnings.append("Banks / insurers are scored in simple mode: operating, financial and CF metrics are structurally different and are shown for reference only.")

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
        "groups": {GROUP_LABEL_EN.get(g, g): (round(v, 1) if is_num(v) else None) for g, v in groups.items()},
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
