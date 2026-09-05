#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
銘柄診断ツール v1（日本株）
====================================================================
証券コードを1つ渡すと、その銘柄を「業績・財務・キャッシュフロー・配当・
期待（バリュエーション）・指標（テクニカル）」の分野別に自動評価し、
投資先としての要件充足度／割安・割高／成長性・安定性を判定して
HTMLレポート（＋Markdown）を書き出す。

使い方:
    python analyze.py 9433
    python analyze.py 9433 --cost 3000        # 取得単価を入れるとYOCも計算
    python analyze.py 8058 --name 三菱商事
    python analyze.py 8306 --no-irbank        # IR BANK補助を使わない

データ源:
    - 主軸 = yfinance（無料・キー不要・遅延なし）
    - 補助 = IR BANK（irbank.net）を best-effort でスクレイプし、配当の
      長期推移（連続増配年数・5年増配率）を補う。失敗しても yfinance に
      フォールバックする。

前提:
    - 東証33業種の平均・判定しきい値は 2026年8月時点のスナップショット
      （sector_averages.json / sector_rules.json）。
    - 銀行・保険・証券・REIT は財務・CFが構造的に別基準のため簡易判定。
    - 教育目的の一般情報。投資助言ではない。最終判断は自己責任で。
"""

import sys
import os
import re
import json
import math
import html
import argparse
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
TODAY = dt.date.today()

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance が未導入です。  python -m pip install yfinance  を実行してください。")

try:
    import requests
except ImportError:
    requests = None


# ====================================================================
# 小物
# ====================================================================
def load_json(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return json.load(f)


def g(d, *keys, default=None):
    """dict から最初に見つかった非 None の値を返す。"""
    for k in keys:
        if d is None:
            break
        v = d.get(k)
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            return v
    return default


def is_num(x):
    return isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x))


def safe_div(a, b):
    if not is_num(a) or not is_num(b) or b == 0:
        return None
    return a / b


def cagr(first, last, periods):
    """first→last を periods 期でならした年率（％）。符号反転・ゼロは None。"""
    if not is_num(first) or not is_num(last) or periods <= 0:
        return None
    if first <= 0 or last <= 0:
        return None
    return ((last / first) ** (1.0 / periods) - 1.0) * 100.0


def fmt_num(v, d=1):
    if not is_num(v):
        return "―"
    return f"{v:,.{d}f}"


def fmt_pct(v, d=1):
    if not is_num(v):
        return "―"
    return f"{v:,.{d}f}%"


def fmt_yen(v):
    """円の大きな金額を 兆／億 表記に。"""
    if not is_num(v):
        return "―"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}{a/1e12:.2f}兆円"
    if a >= 1e8:
        return f"{sign}{a/1e8:,.0f}億円"
    if a >= 1e4:
        return f"{sign}{a/1e4:,.0f}万円"
    return f"{sign}{a:,.0f}円"


# ====================================================================
# データ取得（yfinance）
# ====================================================================
def stmt_to_rows(df):
    """yfinance の財務諸表 DataFrame を {行ラベル: [新しい順の値]} に。"""
    rows = {}
    if df is None or getattr(df, "empty", True):
        return rows, []
    cols = list(df.columns)  # Timestamp（新しい順）
    for label in df.index:
        vals = []
        for c in cols:
            try:
                x = df.loc[label, c]
                x = float(x) if x is not None and not (isinstance(x, float) and math.isnan(x)) else None
            except Exception:
                x = None
            vals.append(x)
        rows[str(label)] = vals
    years = [c.year for c in cols]
    return rows, years


def row(rows, *labels):
    for lb in labels:
        if lb in rows:
            return rows[lb]
    return None


def fetch_yf(code):
    tk = yf.Ticker(f"{code}.T")
    d = {"code": code, "ticker": f"{code}.T"}

    try:
        d["info"] = tk.info or {}
    except Exception:
        d["info"] = {}

    # 価格
    d["price"] = None
    d["price_date"] = None
    try:
        h6 = tk.history(period="6mo", auto_adjust=False)
        cl = h6["Close"].dropna()
        if len(cl):
            d["price"] = float(cl.iloc[-1])
            d["price_date"] = cl.index[-1].date()
    except Exception:
        pass

    # 月足5年（チャート＋利回りバンド用）
    try:
        d["hist_m"] = tk.history(period="6y", interval="1mo", auto_adjust=False)
    except Exception:
        d["hist_m"] = None

    # 日足1年（テクニカル用）
    try:
        d["hist_d"] = tk.history(period="1y", interval="1d", auto_adjust=False)
    except Exception:
        d["hist_d"] = None

    # 配当（ex-date ベースの実額）
    try:
        divs = tk.dividends
        d["divs"] = [(idx.to_pydatetime().date(), float(v)) for idx, v in divs.items() if is_num(float(v))]
    except Exception:
        d["divs"] = []

    # 財務3表（年次）
    try:
        d["is_rows"], d["is_years"] = stmt_to_rows(tk.income_stmt)
    except Exception:
        d["is_rows"], d["is_years"] = {}, []
    try:
        d["bs_rows"], d["bs_years"] = stmt_to_rows(tk.balance_sheet)
    except Exception:
        d["bs_rows"], d["bs_years"] = {}, []
    try:
        d["cf_rows"], d["cf_years"] = stmt_to_rows(tk.cashflow)
    except Exception:
        d["cf_rows"], d["cf_years"] = {}, []

    # 四半期損益（直近決算の前年同期比用）
    d["q_rows"], d["q_dates"] = {}, []
    try:
        qdf = tk.quarterly_income_stmt
        if qdf is not None and not getattr(qdf, "empty", True):
            d["q_rows"], _ = stmt_to_rows(qdf)
            d["q_dates"] = [c.date().isoformat() for c in qdf.columns]
    except Exception:
        d["q_rows"], d["q_dates"] = {}, []

    # アナリスト予想・目標株価・決算日
    d["price_targets"] = {}
    d["earn_est"] = {}
    d["rev_est"] = {}
    d["earn_dates"] = []
    d["next_earn"] = None
    try:
        pt = tk.analyst_price_targets
        if isinstance(pt, dict):
            d["price_targets"] = pt
    except Exception:
        pass

    def _est_to_dict(df):
        out = {}
        if df is None or getattr(df, "empty", True):
            return out
        for p in df.index:
            row = df.loc[p]
            out[str(p)] = {k: (float(row[k]) if k in row and is_num(_f(row[k])) else None)
                           for k in ("avg", "low", "high", "growth", "yearAgoEps", "yearAgoRevenue")}
            out[str(p)]["n"] = int(row["numberOfAnalysts"]) if ("numberOfAnalysts" in row and is_num(_f(row["numberOfAnalysts"]))) else None
        return out
    try:
        d["earn_est"] = _est_to_dict(tk.earnings_estimate)
    except Exception:
        pass
    try:
        d["rev_est"] = _est_to_dict(tk.revenue_estimate)
    except Exception:
        pass
    try:
        ed = tk.earnings_dates
        if ed is not None and not ed.empty:
            for idx, r in ed.iterrows():
                rep = _f(r.get("Reported EPS"))
                est = _f(r.get("EPS Estimate"))
                sp = _f(r.get("Surprise(%)"))
                dd = idx.date()
                if is_num(rep):
                    d["earn_dates"].append({"date": dd, "est": est, "reported": rep, "surprise": sp})
                elif d["next_earn"] is None and dd >= TODAY:
                    d["next_earn"] = dd
            d["earn_dates"].sort(key=lambda x: x["date"], reverse=True)
    except Exception:
        pass
    if d["next_earn"] is None:
        try:
            cal = tk.calendar or {}
            ded = cal.get("Earnings Date")
            if isinstance(ded, list) and ded:
                d["next_earn"] = ded[0]
            elif ded:
                d["next_earn"] = ded
        except Exception:
            pass

    return d


def _f(x):
    try:
        v = float(x)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


# ====================================================================
# IR BANK 補助（best-effort。失敗したら黙って None）
# ====================================================================
def fetch_irbank_dps(code):
    """irbank.net/<code>/dividend から 決算期→1株配当 を拾う。取れたら
    [(fiscal_year:int, dps:float), ...]（古い順）、無理なら None。"""
    if requests is None:
        return None
    url = f"https://irbank.net/{code}/dividend"
    try:
        r = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "ja,en;q=0.8",
        })
        if r.status_code != 200 or not r.text:
            return None
        html = r.text
    except Exception:
        return None

    pairs = {}
    # 「2015年3月期 … 55円」「2015/03 … 55.00円」等のゆらぎを吸収
    for m in re.finditer(r"(20\d{2})\s*[年/./-]\s*0?\d{1,2}\s*月?期?[^0-9%＋+\-]{0,40}?(\d{1,4}(?:\.\d{1,2})?)\s*円", html):
        y = int(m.group(1))
        try:
            v = float(m.group(2))
        except ValueError:
            continue
        if 0 < v < 100000 and 2000 <= y <= TODAY.year:
            pairs.setdefault(y, v)  # 最初の出現を採用
    if len(pairs) < 6:
        return None
    return sorted(pairs.items())


def fetch_jgb10():
    """10年国債利回り(％)を stooq から best-effort 取得。失敗したら None。"""
    if requests is None:
        return None
    try:
        r = requests.get("https://stooq.com/q/l/?s=10jpy.b&f=sd2t2c&h&e=csv",
                         timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        lines = (r.text or "").strip().splitlines()
        if len(lines) < 2:
            return None
        val = float(lines[1].split(",")[-1])
        return val if 0 < val < 10 else None
    except Exception:
        return None


# ====================================================================
# 業種の特定
# ====================================================================
def classify_sector(info, smap):
    industry = (info.get("industry") or "").strip()
    sector = (info.get("sector") or "").strip()
    jp = smap["industry_map"].get(industry)
    src = "industry"
    if not jp:
        jp = smap["sector_fallback"].get(sector)
        src = "sector（industry未対応）"
    if not jp:
        jp = "サービス業"
        src = "既定（分類不明）"
    is_reit = industry.startswith("REIT")
    return jp, industry, sector, src, is_reit


# ====================================================================
# 指標の計算
# ====================================================================
def annual_dps_from_divs(divs, fye_month=3):
    """ex-date 実額を会計年度でまとめる。JP は 4月始まりが既定。
    返り値: [(fy:int, dps:float, n_pay:int)]（古い順、直近の未完了年は除外）。"""
    if not divs:
        return []
    cutoff = (fye_month % 12) + 1  # 3月決算→4
    buckets = {}
    for dd, v in divs:
        fy = dd.year if dd.month >= cutoff else dd.year - 1
        buckets.setdefault(fy, []).append((dd, v))
    items = sorted(buckets.items())
    if not items:
        return []
    # 直近年の完了判定：過去3年の支払回数の中央値以上あれば完了とみなす
    counts = [len(v) for _, v in items]
    prior = counts[:-1][-3:] or counts
    prior_sorted = sorted(prior)
    med = prior_sorted[len(prior_sorted) // 2]
    out = []
    for i, (fy, lst) in enumerate(items):
        n = len(lst)
        if i == len(items) - 1 and n < max(1, med):
            continue  # 未完了年は捨てる
        out.append((fy, round(sum(x[1] for x in lst), 4), n))
    return out


def clean_dps_series(yf_fy):
    """会計年度DPS列から、支払回数が異常に多い年（特殊配当で金額が膨らむ）を
    落として連続増配カウントを安定させる。返り値: [(fy, dps)]（古い順）。"""
    if not yf_fy:
        return []
    counts = [n for _, _, n in yf_fy]
    mode = max(set(counts), key=counts.count)
    return [(fy, v) for fy, v, n in yf_fy if n <= mode]


def _one_streak(vals, strict):
    s = 0
    for i in range(len(vals) - 1, 0, -1):
        cur, prev = vals[i], vals[i - 1]
        if not strict:
            # 連続非減配は保守的に：一度でも下げたら終了（ゆらぎ補修しない）
            if cur >= prev * 0.995:
                s += 1
                continue
            break
        # 連続増配（strict）：増配なら継続、横ばいは終了
        if cur > prev * 1.005:
            s += 1
            continue
        if cur >= prev * 0.995:
            break
        # ディップ。分割調整のゆらぎ（浅い＝−13%以内・1〜2年で元水準へ回復）なら橋渡し
        shallow = cur >= prev * 0.87
        recovered = any(vals[j] >= prev * 0.98 for j in range(i + 1, min(i + 3, len(vals))))
        if shallow and recovered:
            s += 1
            continue
        break                                         # 本物の減配
    return s


def find_last_cut(dps_series):
    """(fy, dps) oldest→newest から、直近の実質減配（0.5%超の下げ）があった会計年度を
    返す（無ければNone）。streak_flat と同じ判定ロジックを使い、その裏にある
    『いつ最後に減配したか』を可視化するための関数。"""
    if not dps_series or len(dps_series) < 2:
        return None
    vals = [v for _, v in dps_series]
    fys = [fy for fy, _ in dps_series]
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] >= vals[i - 1] * 0.995:
            continue
        return fys[i]
    return None


def dividend_streaks(vals):
    """oldest→newest の DPS 列から (連続増配年数, 連続非減配年数)。
    連続増配は分割調整のゆらぎを橋渡しするが、連続非減配は一度の下げで途切れる
    （＝「無減配」の主張は保守的に、実際の記録より短く出ることがある）。"""
    if len(vals) < 3:
        return None, None
    return _one_streak(vals, True), _one_streak(vals, False)


def yearly_price_mean(hist_m):
    """月足から暦年ごとの平均終値。{year: mean_close}"""
    res = {}
    if hist_m is None or getattr(hist_m, "empty", True):
        return res
    tmp = {}
    for idx, r in hist_m.iterrows():
        c = r.get("Close")
        if c is None or (isinstance(c, float) and math.isnan(c)):
            continue
        tmp.setdefault(idx.year, []).append(float(c))
    for y, xs in tmp.items():
        res[y] = sum(xs) / len(xs)
    return res


def calc_technicals(hist_d):
    out = {"rsi": None, "macd_state": None, "stoch_state": None, "rsi_score": None}
    if hist_d is None or getattr(hist_d, "empty", True) or len(hist_d) < 40:
        return out
    close = hist_d["Close"].astype(float)
    low = hist_d["Low"].astype(float)
    high = hist_d["High"].astype(float)

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, math.nan)
    rsi = 100 - 100 / (1 + rs)
    rv = rsi.dropna()
    if len(rv):
        out["rsi"] = float(rv.iloc[-1])

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    if len(macd.dropna()) > 2:
        m, s = float(macd.iloc[-1]), float(signal.iloc[-1])
        mp, sp = float(macd.iloc[-2]), float(signal.iloc[-2])
        if m > s and mp <= sp:
            out["macd_state"] = "ゴールデンクロス直後（上向き転換）"
        elif m < s and mp >= sp:
            out["macd_state"] = "デッドクロス直後（下向き転換）"
        elif m > s:
            out["macd_state"] = "シグナル上（上向き継続）"
        else:
            out["macd_state"] = "シグナル下（下向き継続）"

    low14 = low.rolling(14).min()
    high14 = high.rolling(14).max()
    fastk = 100 * (close - low14) / (high14 - low14).replace(0, math.nan)
    slowk = fastk.rolling(3).mean()
    slowd = slowk.rolling(3).mean()
    kv = slowk.dropna()
    if len(kv):
        k = float(kv.iloc[-1])
        dd = float(slowd.dropna().iloc[-1]) if len(slowd.dropna()) else None
        zone = "買われすぎ（80超）" if k >= 80 else "売られすぎ（20未満）" if k <= 20 else "中立ゾーン"
        cross = ""
        if dd is not None and len(slowk.dropna()) > 1:
            kp = float(slowk.dropna().iloc[-2])
            dp = float(slowd.dropna().iloc[-2]) if len(slowd.dropna()) > 1 else dd
            if kp <= dp and k > dd:
                cross = "・%K が %D を上抜け" if k > dd else ""
            elif kp >= dp and k < dd:
                cross = "・%K が %D を下抜け"
        out["stoch_state"] = f"%K={k:.0f} {zone}{cross}"

    r = out["rsi"]
    if r is not None:
        out["rsi_score"] = 100 if r < 30 else 80 if r < 45 else 60 if r < 60 else 40 if r < 70 else 20
    return out


def build_metrics(yd, irbank, sec_avg, is_simple, jp_sector, rate_sensitive, jgb_10y, div_policies):
    """全指標を分野別 dict に。各指標は {v, disp, ref, key}（key は rules 参照名）。"""
    info = yd["info"]
    isr, bsr, cfr = yd["is_rows"], yd["bs_rows"], yd["cf_rows"]
    price = yd["price"]

    M = {"業績": [], "財務": [], "キャッシュフロー": [], "配当": [], "期待": [], "参考": []}
    flags = []

    # ---- 業績 ----
    rev = row(isr, "Total Revenue", "Operating Revenue")
    opi = row(isr, "Operating Income", "Total Operating Income As Reported", "EBIT")
    ord_ = row(isr, "Pretax Income")
    ni = row(isr, "Net Income", "Net Income Common Stockholders", "Net Income Continuous Operations")
    eps = row(isr, "Basic EPS", "Diluted EPS")

    is_years = yd.get("is_years", []) or []
    cf_years = yd.get("cf_years", []) or []

    def series_disp(s, yfmt=fmt_yen):
        if not s:
            return "―"
        return " ← ".join(yfmt(x) for x in s if x is not None)[:120]

    def series_pairs(s, years):
        """newest-first の値列 → [(year|None, val)] を古い順に。None は除外。"""
        if not s:
            return []
        if years and len(years) == len(s):
            pr = [(y, v) for y, v in zip(years, s) if is_num(v)]
        else:
            pr = [(None, v) for v in s if is_num(v)]
        return pr[::-1]

    def growth_row(name, s, key, target, years=is_years, kind="yen"):
        if s and len([x for x in s if is_num(x)]) >= 2:
            xs = [x for x in s if is_num(x)]
            c = cagr(xs[-1], xs[0], len(xs) - 1)  # xs[0]=最新, xs[-1]=最古
            M[target].append({"name": name, "v": c, "disp": series_disp(s),
                              "ref": f"年率 {fmt_pct(c)}（直近{len(xs)}期のデータから算出）", "key": key,
                              "series": series_pairs(s, years), "series_kind": kind})
        else:
            M[target].append({"name": name, "v": None, "disp": "―", "ref": "データ不足", "key": key})

    growth_row("売上高（推移／年率）", rev, "rev_cagr", "業績")
    if eps and len([x for x in eps if is_num(x)]) >= 2:
        xs = [x for x in eps if is_num(x)]
        c = cagr(xs[-1], xs[0], len(xs) - 1)
        M["業績"].append({"name": "EPS（推移／年率）", "v": c,
                          "disp": " ← ".join(fmt_num(x, 1) + "円" for x in eps if x is not None)[:120],
                          "ref": f"年率 {fmt_pct(c)}（直近{len(xs)}期のデータから算出）", "key": "eps_cagr",
                          "series": series_pairs(eps, is_years), "series_kind": "eps"})
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
            earn_stab = 0.62  # 1期だけ赤字→黒字回復
        else:
            earn_stab = 0.0   # 複数期赤字 or 直近も赤字
    es_disp = ("―" if earn_stab is None else
               "1期赤字→回復（0.62）" if earn_stab == 0.62 else
               "赤字期あり（0.00）" if earn_stab == 0 else f"最悪の前年比 {earn_stab:.2f}")
    M["業績"].append({"name": "利益の安定度（営業利益のブレ）", "v": earn_stab, "disp": es_disp,
                      "ref": "1.00に近いほど減益年がない（安定）。0.88以上で安定・0.65未満は景気敏感。"
                             "1期だけ赤字→回復は一律0.62点、複数期赤字・直近赤字は最低の0点",
                      "key": "earnings_stability"})

    # 参考へ（採点しない）
    growth_row("営業利益（推移／年率）", opi, None, "参考")
    growth_row("当期純利益（推移／年率）", ni, None, "参考")
    if ord_:
        M["参考"].append({"name": "税引前利益（経常利益に相当・IFRS）", "v": None, "disp": series_disp(ord_),
                          "ref": "参考", "key": None, "series": series_pairs(ord_, is_years), "series_kind": "yen"})

    # ---- 財務 ----
    ta = row(bsr, "Total Assets")
    eq = row(bsr, "Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest")
    tdebt = row(bsr, "Total Debt")
    ndebt = row(bsr, "Net Debt")
    cash = row(bsr, "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments")

    ta0 = ta[0] if ta else None
    eq0 = eq[0] if eq else None
    td0 = tdebt[0] if tdebt else None
    cash0 = cash[0] if cash else None
    nd0 = ndebt[0] if ndebt else (td0 - cash0 if is_num(td0) and is_num(cash0) else None)

    equity_ratio = safe_div(eq0, ta0)
    equity_ratio = equity_ratio * 100 if equity_ratio is not None else None
    bs_years = yd.get("bs_years", []) or []
    eqr_series = []
    for i in range(min(len(eq or []), len(ta or []), len(bs_years))):
        if is_num(eq[i]) and is_num(ta[i]) and ta[i] != 0:
            eqr_series.append((bs_years[i], eq[i] / ta[i] * 100))
    eqr_series = eqr_series[::-1]
    de = safe_div(td0, eq0)
    net_de = safe_div(nd0, eq0)
    debt_ratio = safe_div(td0, ta0)
    debt_ratio = debt_ratio * 100 if debt_ratio is not None else None
    cash_ratio = safe_div(cash0, ta0)
    cash_ratio = cash_ratio * 100 if cash_ratio is not None else None

    ocf = row(cfr, "Operating Cash Flow")
    ocf0 = ocf[0] if ocf else None
    debt_to_ocf = safe_div(td0, ocf0)

    # ROIC（対業種平均）＝ EBIT×(1−税率) ÷ 投下資本
    roic = None
    ebit0 = opi[0] if opi and is_num(opi[0]) else None
    inv = row(bsr, "Invested Capital")
    inv0 = inv[0] if inv and is_num(inv[0]) else (
        (td0 + eq0 - cash0) if all(is_num(x) for x in (td0, eq0, cash0)) else None)
    taxp = row(isr, "Tax Provision")
    ptx = row(isr, "Pretax Income")
    tax_rate = 0.30
    if taxp and ptx and is_num(taxp[0]) and is_num(ptx[0]) and ptx[0] > 0:
        tr = taxp[0] / ptx[0]
        if 0.15 <= tr <= 0.50:
            tax_rate = tr
    if is_num(ebit0) and is_num(inv0) and inv0 > 0:
        roic = ebit0 * (1 - tax_rate) / inv0 * 100
    roic_vs = safe_div(roic, sec_avg.get("roic"))

    sa = sec_avg
    M["財務"].append({"name": "自己資本比率", "v": equity_ratio, "disp": fmt_pct(equity_ratio),
                      "ref": f"業種平均 {fmt_pct(sa.get('equity_ratio'))}" if sa.get('equity_ratio') is not None else "―",
                      "key": "equity_ratio", "series": eqr_series, "series_kind": "pct"})
    M["財務"].append({"name": "D/Eレシオ（有利子負債÷自己資本）", "v": de, "disp": fmt_num(de, 2) + "倍" if is_num(de) else "―",
                      "ref": "1倍未満が安全圏（インフラ・不動産は高め正常）", "key": "de"})
    M["財務"].append({"name": "ネットD/Eレシオ", "v": net_de, "disp": fmt_num(net_de, 2) + "倍" if is_num(net_de) else "―",
                      "ref": "現金控除後。マイナス＝実質無借金", "key": "net_de"})
    M["財務"].append({"name": "有利子負債 ÷ 営業CF（返済年数の目安）", "v": debt_to_ocf,
                      "disp": fmt_num(debt_to_ocf, 1) + "年" if is_num(debt_to_ocf) else "―",
                      "ref": "数年以内に返せる水準か", "key": "debt_to_ocf"})
    M["参考"].append({"name": "ROIC（投下資本利益率）", "v": None,
                      "disp": (fmt_pct(roic, 1) if is_num(roic) else "―"),
                      "ref": (f"業種中央値 {fmt_pct(sa.get('roic'))}（対平均 {fmt_num(roic_vs, 2)}倍）" if roic_vs
                              else "業種平均なし")
                             + "。業種中央値が新興企業で歪むため採点対象外、絶対ROEで見る",
                      "key": None})
    M["参考"].append({"name": "有利子負債比率（負債÷総資産）", "v": None, "disp": fmt_pct(debt_ratio),
                      "ref": "D/Eと重複するため参考", "key": None})
    M["参考"].append({"name": "現金比率（現金÷総資産）", "v": None, "disp": fmt_pct(cash_ratio),
                      "ref": "予測力が弱いため参考", "key": None})
    hedge = "ネットキャッシュ（実質無借金）＝倒産リスク低め" if is_num(nd0) and nd0 < 0 else \
            "純有利子負債あり。営業CFでの返済余力を確認" if is_num(nd0) else "―"
    M["参考"].append({"name": "倒産ヘッジ（ネット現金の有無）", "v": None, "disp": hedge, "ref": "", "key": None})

    # ---- キャッシュフロー ----
    icf = row(cfr, "Investing Cash Flow")
    fcf_flow = row(cfr, "Financing Cash Flow")
    fcf = row(cfr, "Free Cash Flow")
    capex = row(cfr, "Capital Expenditure")
    divpaid = row(cfr, "Cash Dividends Paid", "Common Stock Dividend Paid")

    ocf0 = ocf[0] if ocf else None
    icf0 = icf[0] if icf else None
    fin0 = fcf_flow[0] if fcf_flow else None
    fcf0 = fcf[0] if fcf else (ocf0 + capex[0] if is_num(ocf0) and capex and is_num(capex[0]) else None)

    M["キャッシュフロー"].append({"name": "営業CF（直近／推移）", "v": (1 if is_num(ocf0) and ocf0 > 0 else 0) if is_num(ocf0) else None,
                                    "disp": series_disp(ocf), "ref": "継続してプラス・安定が理想", "key": "ocf_positive",
                                    "series": series_pairs(ocf, cf_years), "series_kind": "yen"})
    M["参考"].append({"name": "投資CF（直近／推移）", "v": None, "disp": series_disp(icf),
                      "ref": "本業投資でマイナスが通常", "key": None,
                      "series": series_pairs(icf, cf_years), "series_kind": "yen"})
    M["参考"].append({"name": "財務CF（直近／推移）", "v": None, "disp": series_disp(fcf_flow),
                      "ref": "配当・自社株買い・返済でマイナス傾向", "key": None,
                      "series": series_pairs(fcf_flow, cf_years), "series_kind": "yen"})
    M["キャッシュフロー"].append({"name": "フリーCF（営業CF＋投資CF）", "v": (1 if is_num(fcf0) and fcf0 > 0 else 0) if is_num(fcf0) else None,
                                    "disp": series_disp(fcf), "ref": "継続プラスなら配当の原資に余裕", "key": "fcf_positive",
                                    "series": series_pairs(fcf, cf_years), "series_kind": "yen"})

    pattern = None
    if is_num(ocf0) and is_num(icf0) and is_num(fin0):
        s = lambda x: "＋" if x > 0 else "－"
        pattern = f"営業{s(ocf0)} / 投資{s(icf0)} / 財務{s(fin0)}"
        note = "健全型（本業で稼ぎ→投資と株主還元に回す）" if ocf0 > 0 and icf0 < 0 and fin0 < 0 else "要確認"
        M["参考"].append({"name": "CFの符号パターン", "v": None, "disp": f"{pattern} … {note}", "ref": "", "key": None})

    fcf_payout = None
    if divpaid and is_num(divpaid[0]) and is_num(fcf0) and fcf0 > 0:
        fcf_payout = abs(divpaid[0]) / fcf0 * 100
    M["キャッシュフロー"].append({"name": "FCF配当性向（配当支払÷フリーCF）", "v": fcf_payout, "disp": fmt_pct(fcf_payout),
                                    "ref": "安定企業で70%未満。100%超は取り崩し", "key": "fcf_payout"})

    # ---- 配当 ----
    # 会計年度DPS（yfinance） … 未完了年を除外
    fye_ts = info.get("lastFiscalYearEnd")
    fye_month = 3
    if is_num(fye_ts):
        try:
            fye_month = dt.datetime.fromtimestamp(fye_ts, dt.timezone.utc).month
        except Exception:
            fye_month = 3
    yf_fy = annual_dps_from_divs(yd["divs"], fye_month)
    dps_series = clean_dps_series(yf_fy)  # 支払回数が異常な年（特殊配当）を除外
    dps_src = "yfinance（会計年度換算）"
    if irbank and len(irbank) >= len(dps_series):
        dps_series = irbank
        dps_src = "IR BANK（決算期ベース）"

    streak_up = streak_flat = None
    dgr5 = None
    streak_capped = False
    if len(dps_series) >= 3:
        vals = [v for _, v in dps_series]
        streak_up, streak_flat = dividend_streaks(vals)
        # yfinance の JP 配当データは15年以上さかのぼると分割調整・特殊配当で
        # 崩れがち。12年以上の連続増配は「確認できた範囲、実際はより長い可能性」と注記
        if is_num(streak_up) and streak_up >= 12:
            streak_capped = True
        n = min(5, len(vals) - 1)
        if n >= 2:
            dgr5 = cagr(vals[-1 - n], vals[-1], n)

    fwd_dps = info.get("dividendRate")
    if not is_num(fwd_dps) and yd["divs"]:
        last12 = [v for d0, v in yd["divs"] if (TODAY - d0).days <= 366]
        fwd_dps = sum(last12) if last12 else None
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

    # 配当利回りの年次履歴（暦年ごとの平均株価 ÷ その年のDPS）――レンジ帯グラフ用
    pm = yearly_price_mean(yd["hist_m"])
    dps_by_cal = {}
    for d0, v in yd["divs"]:
        dps_by_cal[d0.year] = dps_by_cal.get(d0.year, 0) + v
    yy = []  # [(year, yield%)]
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
                      "ref": range_txt + "／安全圏3.5〜4%以上。推移＝各暦年の平均株価に対する利回り",
                      "key": "div_yield", "series": yy, "series_kind": "pct", "series_current": yld_fwd})
    M["配当"].append({"name": "増配率（直近5年・年率）", "v": dgr5, "disp": fmt_pct(dgr5), "ref": f"出所：{dps_src}", "key": "dgr5"})
    cap_note = "（yfinanceで追える範囲。実際はより長い可能性）" if streak_capped else ""
    st_disp = (f"{streak_up}年＋" if (streak_capped and is_num(streak_up)) else
               f"{streak_up}年" if is_num(streak_up) else "―")
    sf_disp = (f"{streak_flat}年＋" if (streak_capped and is_num(streak_flat)) else
               f"{streak_flat}年" if is_num(streak_flat) else "―")
    M["配当"].append({"name": "連続増配 年数", "v": streak_up, "disp": st_disp,
                      "ref": "10年以上で優良／20年超で王道" + cap_note, "key": "streak_up"})
    M["配当"].append({"name": "連続 非減配 年数", "v": streak_flat, "disp": sf_disp,
                      "ref": "減配なしで持続してきたか" + cap_note, "key": "streak_flat"})
    pn = sec_avg.get("payout")
    pn_txt = f"業種目安 {pn[0]}〜{pn[1]}%" if pn else "―"
    if sec_avg.get("payout_note"):
        pn_txt += f"（{sec_avg['payout_note']}）"
    # 推移は「その期の1株配当 ÷ その期のEPS」で統一（行の値＝yfinance予想/TTMと
    # 方法を合わせる。CFの配当支払額÷純利益だと前期分が混ざり景気敏感株で歪む）
    eps_yr = {y: e for y, e in zip(is_years, eps or []) if is_num(e) and e != 0}
    payout_series = []
    for fy, d in dps_series:  # dps_series = [(会計年度, 1株配当)]
        y = fy + 1 if (fy + 1) in eps_yr else (fy if fy in eps_yr else None)
        if y is not None and is_num(d):
            payout_series.append((y, d / eps_yr[y] * 100))
    M["配当"].append({"name": "配当性向（純利益ベース）", "v": payout_ni, "disp": fmt_pct(payout_ni),
                      "ref": pn_txt + "／80%超は警戒。推移＝各期の1株配当÷EPS（行の値は予想/直近12ヶ月ベース）",
                      "key": "payout_ni", "series": payout_series, "series_kind": "pct",
                      "series_current": payout_ni})
    M["配当"].append({"name": "ROE（配当の原資の効率）", "v": roe, "disp": fmt_pct(roe),
                      "ref": f"業種中央値 {fmt_pct(sec_avg.get('roe'))}／10%以上で優良" if sec_avg.get("roe") is not None else "10%以上で優良", "key": "roe"})

    # 累進配当・DOE の公式宣言（手管理リスト。宣言ありなら加点、なしは減点しない）
    pol = div_policies.get(yd["code"])
    if pol and "累進" in pol:
        dp_v, dp_disp = 106.0, f"{pol}（公式宣言あり）"
    elif pol == "DOE":
        dp_v, dp_disp = 100.0, "DOE（自己資本配当率を配当方針に明記）"
    else:
        dp_v, dp_disp = None, "公式宣言は確認できず（連続増配の実績で判断）"
    M["配当"].append({"name": "累進配当・DOE の宣言", "v": dp_v, "disp": dp_disp,
                      "ref": "手管理リスト（2026-09時点）。会社の中期経営計画・配当方針の最新開示で要確認",
                      "key": "div_policy"})

    # 減配履歴（連続非減配年数の裏側にある事実を可視化。過去の減配自体は恒久的な
    # 減点にはせず、その後の実績や累進配当・DOE宣言で現在の評価を見る）
    last_cut_fy = find_last_cut(dps_series) if len(dps_series) >= 3 else None
    if len(dps_series) < 3:
        cut_disp = "データ不足で判定不可"
    elif last_cut_fy is None:
        cut_disp = "減配歴なし（確認できたデータ範囲内）"
    else:
        yrs_txt = f"{streak_flat}年" if is_num(streak_flat) else "不明"
        if is_num(dp_v):
            cut_disp = f"最終減配 {last_cut_fy}年度（その後{yrs_txt}減配なし）。累進配当・DOE宣言があり現在は良好評価"
        elif is_num(streak_flat) and streak_flat >= 10:
            cut_disp = f"最終減配 {last_cut_fy}年度。その後{yrs_txt}間は減配なし＝現在は良好水準"
        elif is_num(streak_flat) and streak_flat >= 5:
            cut_disp = f"最終減配 {last_cut_fy}年度（{yrs_txt}前）。回復途上"
        else:
            cut_disp = f"最終減配 {last_cut_fy}年度（{yrs_txt}前）。直近の減配歴が新しく要注意"
    M["参考"].append({"name": "減配履歴", "v": None, "disp": cut_disp,
                      "ref": "「連続非減配年数」の裏側にある事実。過去に減配があってもスコアを恒久的には"
                             "下げない：その後の非減配年数が長い（目安10年以上）か、累進配当・DOE宣言が"
                             "あれば現在の評価は良好になり得る（連続非減配年数・累進配当宣言の各スコアに反映済み）",
                      "key": None})

    # 配当の中身（記念・特別配当の疑い）
    if yd["divs"]:
        recent = [v for d0, v in yd["divs"] if (TODAY - d0).days <= 365 * 3]
        if len(recent) >= 3:
            srt = sorted(recent)
            med = srt[len(srt) // 2]
            mx = max(recent)
            if med > 0 and mx > med * 1.8:
                flags.append("直近3年の配当に、通常水準の1.8倍を超える回がある＝記念配当・特別配当が混じっている可能性。普通配当だけで利回りを見直すこと。")
            else:
                flags.append("直近の配当は普通配当のみと推定（突出した回なし）。ただし会社発表の内訳で最終確認を。")
    M["参考"].append({"name": "配当の中身（記念・特別の有無）", "v": None,
                      "disp": flags[-1] if flags else "配当データなし", "ref": "機械判定は目安。決算短信で確認", "key": None})
    M["参考"].append({"name": "配当方針の一次情報", "v": None,
                      "disp": "累進配当・DOE・配当性向目標などは会社の中期経営計画／決算説明資料で確認",
                      "ref": f"https://irbank.net/{yd['code']}/dividend", "key": None})

    # ---- 期待（バリュエーション）----
    per = info.get("trailingPE")
    pbr = info.get("priceToBook")
    per = per if is_num(per) and per > 0 else None
    pbr = pbr if is_num(pbr) and pbr > 0 else None
    ey = (1 / per * 100) if per else None

    per_vs = safe_div(per, sec_avg.get("per"))
    pbr_vs = safe_div(pbr, sec_avg.get("pbr"))

    # 配当利回りの過去レンジ内の位置（yy・pm は上の 配当セクションで算出済み）
    yband_pos = None
    yrange = None
    yvals = [v for _, v in yy]
    if len(yvals) >= 3 and is_num(yld_fwd):
        lo, hi = min(yvals), max(yvals)
        yrange = (lo, hi)
        if hi > lo:
            yband_pos = max(0.0, min(1.0, (yld_fwd - lo) / (hi - lo)))

    chowder = (yld_fwd + dgr5) if is_num(yld_fwd) and is_num(dgr5) else None

    # PER の自社過去レンジ内の位置（yield_band_pos の PER 版）
    per_band_pos = None
    per_range = None
    eps_by_year = {}
    for y, e in zip(yd.get("is_years", []) or [], eps or []):
        if is_num(e) and e > 0:
            eps_by_year[y] = e
    # 業績が一時的に落ち込んだ年は PER が跳ねてレンジを壊すので除外
    eps_vals = sorted(eps_by_year.values())
    eps_med = eps_vals[len(eps_vals) // 2] if eps_vals else None
    pers = []  # [(year, PER)]
    for y in sorted(pm):
        if y == TODAY.year:
            continue
        e = eps_by_year.get(y)
        if e is None or pm[y] <= 0:
            continue
        if eps_med and e < 0.4 * eps_med:   # 業績急減の年は除外
            continue
        p_y = pm[y] / e
        if p_y > 60:                         # 外れ値の保険
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

    # 利回り − 10年国債スプレッド（金利敏感セクターのみ採点）
    rate_sens = jp_sector in rate_sensitive
    yield_spread = (yld_fwd - jgb_10y) if (rate_sens and is_num(yld_fwd) and is_num(jgb_10y)) else None

    M["期待"].append({"name": "PER（実績・対業種平均）", "v": per_vs, "disp": (fmt_num(per, 1) + "倍" if per else "―"),
                      "ref": f"業種平均 {fmt_num(sec_avg.get('per'),1)}倍（対平均 {fmt_num(per_vs,2)}倍）" if per_vs else "―", "key": "per_vs_sector"})
    M["期待"].append({"name": "PBR（実績・対業種平均）", "v": pbr_vs, "disp": (fmt_num(pbr, 2) + "倍" if pbr else "―"),
                      "ref": f"業種平均 {fmt_num(sec_avg.get('pbr'),1)}倍" + ("／1倍割れ＝東証改革の是正テーマ" if pbr and pbr < 1 else ""), "key": "pbr_vs_sector"})
    if per_range:
        M["期待"].append({"name": "PERの自社過去レンジ内の位置", "v": per_band_pos,
                          "disp": f"過去 {per_range[0]:.1f}〜{per_range[1]:.1f}倍 ／ 現在 {per:.1f}倍 ＝ 割安度 {per_band_pos*100:.0f}/100",
                          "ref": "0＝レンジ上端（高PER＝割高）／100＝下端（低PER＝割安）", "key": "per_band_pos", "rangeband": rb_per})
    else:
        M["期待"].append({"name": "PERの自社過去レンジ内の位置", "v": None, "disp": "履歴不足で算出不可",
                          "ref": "3年以上の株価・EPSが必要", "key": "per_band_pos"})
    M["参考"].append({"name": "益回り（1÷PER）", "v": None, "disp": fmt_pct(ey), "ref": "国債利回りとの比較に使う", "key": None})
    if yrange:
        M["期待"].append({"name": "配当利回りセオリー（自分の過去レンジ内の位置）", "v": yband_pos,
                          "disp": f"過去 {yrange[0]:.1f}〜{yrange[1]:.1f}% ／ 現在 {yld_fwd:.1f}% ＝ 割安度 {yband_pos*100:.0f}/100",
                          "ref": "0＝レンジ下端（低利回り＝割高）／100＝上端（高利回り＝割安）", "key": "yield_band_pos", "rangeband": rb_yield})
    else:
        M["期待"].append({"name": "配当利回りセオリー（過去レンジ内の位置）", "v": None, "disp": "履歴不足で算出不可", "ref": "5年以上の株価・配当が必要", "key": "yield_band_pos"})
    M["期待"].append({"name": "Chowderルール（利回り＋5年増配率）", "v": chowder, "disp": fmt_pct(chowder),
                      "ref": "合計12%以上で合格（公益・通信は8%）", "key": "chowder"})
    if rate_sens:
        M["期待"].append({"name": "利回り − 10年国債スプレッド", "v": yield_spread,
                          "disp": (f"{yld_fwd:.2f}% − {jgb_10y:.2f}% ＝ {yield_spread:+.2f}%" if yield_spread is not None else "利回り不明"),
                          "ref": f"「債券の代わり」需要のある業種。広いほど割安（2.5%以上が良好の目安）／国債は {fmt_num(jgb_10y,2)}%",
                          "key": "yield_spread"})
    else:
        M["期待"].append({"name": "利回り − 10年国債スプレッド", "v": None,
                          "disp": "金利敏感セクター外のため評価しない",
                          "ref": "公益・通信・不動産・鉄道・生活必需品・医薬・小売のみ採点", "key": "yield_spread"})

    # ---- テクニカル（採点しない・参考のみ）----
    tec = calc_technicals(yd["hist_d"])
    M["参考"].append({"name": "RSI(14)", "v": None,
                      "disp": (f"{tec['rsi']:.0f}" if tec["rsi"] is not None else "―") +
                              ("　売られすぎ水準" if tec["rsi"] is not None and tec["rsi"] < 30 else
                               "　買われすぎ水準" if tec["rsi"] is not None and tec["rsi"] >= 70 else "　中立"),
                      "ref": "短期すぎるため採点対象外。値動きの強弱を示す参考指標", "key": None})
    M["参考"].append({"name": "MACD(12,26,9)", "v": None, "disp": tec["macd_state"] or "―", "ref": "", "key": None})
    M["参考"].append({"name": "スローストキャスティクス(14,3,3)", "v": None, "disp": tec["stoch_state"] or "―", "ref": "", "key": None})

    ctx = {
        "per": per, "pbr": pbr, "yld_fwd": yld_fwd, "dgr5": dgr5,
        "yband_pos": yband_pos, "yrange": yrange, "dps_series": dps_series, "dps_src": dps_src,
        "op_margin": op_margin, "roe": roe, "equity_ratio": equity_ratio,
        "streak_up": streak_up, "chowder": chowder, "tec": tec,
        "rev_series": rev, "ni_series": ni, "eps_series": eps,
        "fwd_dps": fwd_dps, "payout_ni": payout_ni,
        "per_band_pos": per_band_pos, "yield_spread": yield_spread, "jgb_10y": jgb_10y,
        "roic_vs": roic_vs, "earn_stab": earn_stab,
    }
    return M, flags, ctx


# ====================================================================
# スコアリング
# ====================================================================
DOMAIN_KEYS = {
    "業績": ["rev_cagr", "eps_cagr", "op_margin", "earnings_stability"],
    "財務": ["equity_ratio", "de", "net_de", "debt_to_ocf"],
    "キャッシュフロー": ["ocf_positive", "fcf_positive", "fcf_payout"],
    "配当": ["div_yield", "dgr5", "streak_up", "streak_flat", "payout_ni", "roe", "div_policy"],
    "期待": ["per_vs_sector", "pbr_vs_sector", "per_band_pos", "yield_band_pos", "chowder", "yield_spread"],
}


def rule_for(key, jp_sector, rules):
    base = dict(rules["default"].get(key, {}))
    ov = rules["overrides"].get(jp_sector, {})
    if key in ov:
        base.update(ov[key])
    return base or None


# 理論的に負を取らない指標（▲ゾーンの下限を 0 で止める）
NONNEG_KEYS = {"streak_up", "streak_flat", "cash_ratio", "equity_ratio",
               "op_margin", "debt_ratio", "div_yield"}


def score_metric(key, v, rule):
    """good / warn の2閾値の間を直線補間して 20〜110 点を返す（階段でなくスロープ）。
      good        → 100    warn        → 60    warn−幅（下限）→ 20
      good＋幅（別格）→ 110（上限）           それ以下 → 20 でフラット
    """
    if v is None or not rule:
        return None
    d = rule.get("dir")
    if d == "skip":
        return None

    if d in ("higher_better", "lower_better"):
        g, w = rule.get("good"), rule.get("warn")
        if not is_num(g) or not is_num(w):
            return None
        s = abs(g - w)
        if s == 0:  # 営業CF±など 実質2択
            ok = (v >= g) if d == "higher_better" else (v <= g)
            return 100.0 if ok else 20.0
        if d == "higher_better":
            lo = max(w - s, 0.0) if key in NONNEG_KEYS else (w - s)
            if v >= g + s:
                return 110.0
            if v >= g:
                return 100.0 + 10.0 * (v - g) / s
            if v >= w:
                return 60.0 + 40.0 * (v - w) / s
            if v > lo and (w - lo) > 0:
                return 20.0 + 40.0 * (v - lo) / (w - lo)
            return 20.0
        else:  # lower_better
            hi = w + s
            if v <= g - s:
                return 110.0
            if v <= g:
                return 100.0 + 10.0 * (g - v) / s
            if v <= w:
                return 60.0 + 40.0 * (w - v) / s
            if v <= hi:
                return 20.0 + 40.0 * (hi - v) / s
            return 20.0

    if d == "band":
        g, w = rule.get("good"), rule.get("warn")
        if not (isinstance(g, list) and isinstance(w, list)):
            return None
        g1, w1 = g[1], w[1]
        s = w1 - g1
        if v <= g1:
            return 100.0            # 帯型は「別格」加点なし（浅く収まっても100）
        if s <= 0:
            return 20.0
        if v <= w1:
            return 60.0 + 40.0 * (w1 - v) / s
        if v <= w1 + s:
            return 20.0 + 40.0 * ((w1 + s) - v) / s
        return 20.0
    return None


def metric_label(key, v, rule):
    """◎/△/▲ のラベルは従来どおり good / warn の閾値そのもので決める（据え置き）。"""
    if v is None or not rule:
        return None
    d = rule.get("dir")
    if d == "skip":
        return None
    if d == "higher_better":
        return "good" if v >= rule["good"] else "warn" if v >= rule["warn"] else "bad"
    if d == "lower_better":
        return "good" if v <= rule["good"] else "warn" if v <= rule["warn"] else "bad"
    if d == "band":
        g, w = rule["good"], rule["warn"]
        if g[0] <= v <= g[1]:
            return "good"
        if w[0] <= v <= w[1]:
            return "warn"
        return "bad"
    return None


def score_all(M, jp_sector, rules, is_simple):
    detail = {}
    dom_scores = {}
    by_key = {}  # key -> 採点済み item（グループ集計用）
    for dom, keys in DOMAIN_KEYS.items():
        excluded = is_simple and dom in ("業績", "財務", "キャッシュフロー")
        pts = []
        rows_out = []
        for item in M.get(dom, []):
            k = item.get("key")
            if not k:
                continue
            if excluded:
                p, lab = None, None  # この業種は別基準。採点も行ごと判定もしない
            elif k == "rsi_score":
                p = item["v"] if is_num(item["v"]) else None  # 既に0〜110のスコア
                lab = "rsi"
            elif k == "div_policy":
                p = item["v"] if is_num(item["v"]) else None  # 宣言ありなら加点、なしは None
                lab = "good" if is_num(p) else None
            else:
                rl = rule_for(k, jp_sector, rules)
                p = score_metric(k, item["v"], rl)
                lab = metric_label(k, item["v"], rl)
            if p is not None:
                pts.append(p)
            row = {**item, "score": p, "label": lab}
            rows_out.append(row)
            by_key[k] = row
        detail[dom] = rows_out
        dom_scores[dom] = None if excluded else ((sum(pts) / len(pts)) if pts else None)

    # --- グループ集計 → 銘柄選定 / 買い時 の2スコア ---
    sg = rules["score_groups"]
    simple_ex = set(sg.get("simple_excludes", []))
    # 「本来は取れるべき指標」の集合。None が正常な指標（宣言なしで None の
    # div_policy、金利敏感セクター外で None の yield_spread）はカバレッジの
    # 分母に入れない＝データ欠損ではないため。
    BONUS_KEYS = {"div_policy", "yield_spread"}
    groups = {}          # group_name -> score(or None)
    heads = {}           # "選定" / "買い時" -> score(or None)
    coverage = {}        # "選定" / "買い時" -> (scored:int, possible:int)
    for head in ("選定", "買い時"):
        num = den = 0.0
        cov_scored = cov_possible = 0
        for gname, gdef in sg[head].items():
            excluded_grp = is_simple and gname in simple_ex
            gpts = [by_key[k]["score"] for k in gdef["keys"]
                    if k in by_key and is_num(by_key[k]["score"])]
            gscore = None if excluded_grp else ((sum(gpts) / len(gpts)) if gpts else None)
            groups[gname] = gscore
            if gscore is not None:
                num += gdef["weight"] * gscore
                den += gdef["weight"]
            if not excluded_grp:
                for k in gdef["keys"]:
                    s = by_key.get(k, {}).get("score")
                    if k in BONUS_KEYS:
                        if is_num(s):        # 加点キーは実際に付いた時だけ数える
                            cov_scored += 1
                            cov_possible += 1
                    else:
                        cov_possible += 1
                        if is_num(s):
                            cov_scored += 1
        heads[head] = (num / den) if den else None
        coverage[head] = (cov_scored, cov_possible)

    return dom_scores, detail, groups, heads["選定"], heads["買い時"], coverage


# ====================================================================
# 判定文
# ====================================================================
# 2026-09-03、日本の代表的な配当・優良17銘柄で校正（batch_calib.py）。
# 銘柄選定 分布: min59 p25 79 中央値90 p75 94 max103
# 買い時   分布: min50 p25 53 中央値59 p75 64 max97（市場全体が割高寄りのため低め）
SEL_TIERS = (85, 68, 55)   # 長期保有できる配当株 / 及第点 / 質に不安あり / 基準を満たさない
TIM_TIERS = (72, 57, 45)   # 買い場（割安圏） / ほぼ妥当 / やや割高 / 割高で見送り


def _tier(score, tiers):
    """score → 'hi'/'mid'/'lo'/None"""
    if not is_num(score):
        return None
    hi, mid, lo = tiers
    return "hi" if score >= hi else "mid" if score >= mid else "lo" if score >= lo else "xlo"


def cov_label(pair):
    """(scored, possible) → (割合0-1, '高'/'中'/'低')"""
    scored, possible = pair
    if possible <= 0:
        return 0.0, "―"
    r = scored / possible
    return r, ("高" if r >= 0.85 else "中" if r >= 0.65 else "低")


SEL_LABEL = {"hi": "選定スコア上位（質・持続力とも高水準）", "mid": "選定スコア中位（一部に弱点）",
             "lo": "選定スコア下位（質に不安）", "xlo": "選定スコア基準未達", None: "判定不可（データ不足）"}
TIM_LABEL = {"hi": "買い時スコア上位（割安水準）", "mid": "買い時スコア中位（妥当水準）",
             "lo": "買い時スコア下位（やや割高水準）", "xlo": "買い時スコア最下位（割高水準）",
             None: "判定不可（PER・PBR・利回り履歴が不足）"}

# 象限コメントは「選定＝質、買い時＝現在の価格水準」の組み合わせをスコアの言葉のまま
# 記述する（売買を指示する表現にしない）。判断は読み手に委ねる。
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


def verdicts(sel_score, tim_score, groups, dom_scores, ctx, sec_avg, is_simple, coverage):
    per, pbr = ctx["per"], ctx["pbr"]
    sig = []
    if per and sec_avg.get("per"):
        sig.append((sec_avg["per"] - per) / sec_avg["per"])
    if pbr and sec_avg.get("pbr"):
        sig.append((sec_avg["pbr"] - pbr) / sec_avg["pbr"])
    if ctx["yband_pos"] is not None:
        sig.append((ctx["yband_pos"] - 0.5) * 0.8)
    sig = [max(-0.5, min(0.5, s)) for s in sig]  # 1指標が支配しないよう頭打ち
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

    rc = cagr_of(ctx["rev_series"])
    ec = cagr_of(ctx["eps_series"])
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
    st = tec.get("stoch_state")
    macd = tec.get("macd_state")
    rsi = tec.get("rsi")
    short_bits = []
    if is_num(rsi):
        short_bits.append(f"RSI {rsi:.0f}" + ("（売られすぎ）" if rsi < 30 else "（買われすぎ）" if rsi >= 70 else "（中立）"))
    if macd:
        short_bits.append(macd)
    short_label = " ／ ".join(short_bits) if short_bits else "―"

    st_tier = _tier(sel_score, SEL_TIERS)
    ti_tier = _tier(tim_score, TIM_TIERS)
    quad = QUADRANT.get((st_tier or "mid", ti_tier or "mid"), "―")

    sel_cov = cov_label(coverage.get("選定", (0, 0)))
    tim_cov = cov_label(coverage.get("買い時", (0, 0)))
    if "低" in (sel_cov[1], tim_cov[1]):
        which = " と ".join(n for n, c in (("銘柄選定", sel_cov[1]), ("買い時", tim_cov[1])) if c == "低")
        quad = f"※データ不足（{which}のカバレッジ低）。点数は目安、参考程度に。　" + quad

    return {
        "選定ラベル": SEL_LABEL[st_tier],
        "買い時ラベル": TIM_LABEL[ti_tier],
        "総合コメント": quad,
        "割安・割高": val_label,
        "成長性": grow_label,
        "安定性": stab_label,
        "短期": short_label,
        "val_idx": val_idx,
        "選定cov": (coverage.get("選定", (0, 0)), sel_cov),
        "買い時cov": (coverage.get("買い時", (0, 0)), tim_cov),
    }


def cagr_of(series):
    if not series:
        return None
    xs = [x for x in series if is_num(x)]
    if len(xs) < 2:
        return None
    return cagr(xs[-1], xs[0], len(xs) - 1)


# ====================================================================
# 企業概要（yfinance info。事業内容の説明文は英語のみ＝Yahoo Financeの制約）
# ====================================================================
def build_company_overview(info):
    co = {
        "summary": info.get("longBusinessSummary"),
        "employees": info.get("fullTimeEmployees"),
        "city": info.get("city"),
        "country": info.get("country"),
        "website": info.get("website"),
    }
    co["has"] = bool(co["summary"] or co["employees"] or co["website"])
    return co


COMPANY_DISC = "※ 事業内容は yfinance（Yahoo Finance）由来の英語の説明文です。日本語の要約ではありません。"


def render_company_html(co):
    if not co or not co.get("has"):
        return ""
    rows = []
    loc = "、".join(x for x in (co.get("city"), co.get("country")) if x)
    if loc:
        rows.append(f'<div class="plain"><span class="mn">本社所在地</span>'
                    f'<span class="mv2">{html.escape(loc)}</span></div>')
    if is_num(co.get("employees")):
        rows.append(f'<div class="plain"><span class="mn">従業員数</span>'
                    f'<span class="mv2">{co["employees"]:,}名</span></div>')
    if co.get("website"):
        u = html.escape(co["website"])
        rows.append(f'<div class="plain"><span class="mn">Webサイト</span>'
                    f'<span class="mv2"><a href="{u}" target="_blank" rel="noopener">{u}</a></span></div>')
    summary_p = ""
    if co.get("summary"):
        summary_p = (f'<p class="rule"><b>事業内容（English・出典 Yahoo Finance）</b></p>'
                    f'<p style="white-space:pre-wrap">{html.escape(co["summary"])}</p>')
    return (f'<details class="chartbox"><summary>企業概要</summary>'
            f'<div class="mbody">{"".join(rows)}{summary_p}'
            f'<p class="rule" style="margin-top:10px">{COMPANY_DISC}</p></div></details>')


def render_company_md(co):
    if not co or not co.get("has"):
        return ""
    L = ["\n## 企業概要\n"]
    loc = "、".join(x for x in (co.get("city"), co.get("country")) if x)
    if loc:
        L.append(f"- **本社所在地**: {loc}")
    if is_num(co.get("employees")):
        L.append(f"- **従業員数**: {co['employees']:,}名")
    if co.get("website"):
        L.append(f"- **Webサイト**: {co['website']}")
    if co.get("summary"):
        L.append(f"\n**事業内容（English・出典 Yahoo Finance）**\n\n{co['summary']}")
    L.append(f"\n> {COMPANY_DISC}")
    return "\n".join(L)


# ====================================================================
# 直近決算・アナリスト予想（yfinance / Yahoo Finance 集計）
# ====================================================================
_RECO_JP = {"strong_buy": "強気買い", "buy": "買い", "hold": "中立",
            "underperform": "弱気", "sell": "売り", "none": "―"}


def build_earnings(yd, price):
    info = yd.get("info", {})
    ea = {"has": False, "q": [], "fwd": {}}

    eds = yd.get("earn_dates") or []
    if eds:
        last = eds[0]
        ea.update(disc_date=last["date"].isoformat(), eps_reported=last["reported"],
                  eps_est=last["est"], surprise=last["surprise"], has=True)

    qr, qd = yd.get("q_rows") or {}, yd.get("q_dates") or []

    def qrow(*labels):
        for lb in labels:
            if lb in qr:
                return qr[lb]
        return None
    # 米国株式など列が5つ以上ある場合は売上・利益の前年同期比が取れる
    for name, s in (("売上高", qrow("Total Revenue", "Operating Revenue")),
                    ("営業利益", qrow("Operating Income", "Total Operating Income As Reported", "EBIT")),
                    ("純利益", qrow("Net Income", "Net Income Common Stockholders"))):
        if s and is_num(s[0]) and len(s) >= 5 and is_num(s[4]) and s[4] != 0:
            ea["q"].append({"name": name, "val": s[0], "yoy": (s[0] / s[4] - 1) * 100})
    # 日本株の四半期は EPS 行と2列（当四半期・前年同四半期）だけのことが多い
    if not ea["q"]:
        eps_q = qrow("Diluted EPS", "Basic EPS")
        if eps_q and len(eps_q) >= 2 and is_num(eps_q[0]) and is_num(eps_q[1]) and eps_q[1] != 0:
            ea["q"].append({"name": "EPS（四半期）", "val": eps_q[0],
                            "yoy": (eps_q[0] / eps_q[1] - 1) * 100, "unit": "円"})
    if ea["q"]:
        ea["has"] = True
        ea["q_date"] = str(qd[0]) if qd else None

    sp = ea.get("surprise")
    prof = next((x["yoy"] for x in ea["q"] if x["name"] in ("営業利益", "純利益", "EPS（四半期）") and is_num(x["yoy"])), None)
    rev_yoy = next((x["yoy"] for x in ea["q"] if x["name"] == "売上高" and is_num(x["yoy"])), None)
    bits = []
    if is_num(rev_yoy) and is_num(prof):
        bits.append(("増収" if rev_yoy >= 0 else "減収") + ("増益" if prof >= 0 else "減益"))
    elif is_num(prof):
        bits.append("前年同期比 増益" if prof >= 0 else "前年同期比 減益")
    if is_num(sp):
        bits.append("市場予想を上回る" if sp >= 5 else "市場予想を下回る" if sp <= -5 else "ほぼ予想どおり")
    ea["verdict"] = "／".join(bits) if bits else "判定に必要な四半期データ・予想が不足"

    def _g(x):  # 成長率(%)。実額推定がずれて桁違いの値になることがあるので±60%で足切り
        v = x * 100 if is_num(x) else None
        return v if (is_num(v) and abs(v) <= 60) else None

    ee, re_ = yd.get("earn_est") or {}, yd.get("rev_est") or {}
    for pk, label in (("0y", "今期"), ("+1y", "来期")):
        e = ee.get(pk) or {}
        if is_num(e.get("avg")):
            ea["fwd"][pk] = {
                "label": label, "eps": e["avg"], "n": e.get("n"),
                "growth": _g(e.get("growth")),
                "per": (price / e["avg"]) if (is_num(price) and e["avg"] > 0) else None,
            }
    r0 = re_.get("0y") or {}
    if is_num(r0.get("avg")):
        ea["fwd_rev"] = {"avg": r0["avg"], "n": r0.get("n"), "growth": _g(r0.get("growth"))}
    ea["next_earn"] = yd["next_earn"].isoformat() if yd.get("next_earn") else None

    pt = yd.get("price_targets") or {}
    mean = pt.get("mean") or info.get("targetMeanPrice")
    if is_num(mean):
        ea["target"] = {
            "mean": mean, "high": pt.get("high") or info.get("targetHighPrice"),
            "low": pt.get("low") or info.get("targetLowPrice"),
            "vs": (mean / price - 1) * 100 if is_num(price) and price > 0 else None,
        }
    ea["reco"] = {"key": info.get("recommendationKey"), "mean": info.get("recommendationMean"),
                  "n": info.get("numberOfAnalystOpinions")}
    if ea.get("target") or ea["fwd"] or ea.get("next_earn"):
        ea["has"] = True
    return ea


EARN_DISC = ("※ yfinance（Yahoo Finance）のアナリスト集計。日本株は対象アナリストが少なく、"
             "予想が古い・欠損することがある。四半期のEPS実績/予想もyfinance換算で誤差あり。"
             "会社の正式な業績予想は決算短信・決算説明資料で必ず確認すること。")


def _earn_rows(ea):
    """(見出し, 本文) のリストを返す（HTML/MD 共通の素材）。"""
    R = []
    qs = list(ea.get("q") or [])
    # 四半期EPSが開示EPSとほぼ同じなら、別行にせず前年同期比だけをEPS行に添える
    epr = ea.get("eps_reported")
    merged_yoy = None
    if len(qs) == 1 and qs[0].get("unit") == "円" and is_num(epr) and epr != 0 \
            and abs(qs[0]["val"] / epr - 1) <= 0.02 and is_num(qs[0]["yoy"]):
        merged_yoy = qs[0]["yoy"]
        qs = []
    if is_num(epr):
        s = f"実績 {fmt_num(epr,1)}円"
        if is_num(merged_yoy):
            s += f"（前年同期比 {merged_yoy:+.1f}%）"
        if is_num(ea.get("eps_est")):
            s += f" ／ 事前予想 {fmt_num(ea['eps_est'],1)}円"
        if is_num(ea.get("surprise")):
            s += f" ／ サプライズ {ea['surprise']:+.1f}%"
        head = "直近決算 EPS" + (f"（開示 {ea['disc_date']}）" if ea.get("disc_date") else "")
        R.append((head, s))
    if qs:
        parts = []
        for x in qs:
            v = f"{fmt_num(x['val'],1)}円" if x.get("unit") == "円" else fmt_yen(x["val"])
            t = f"{x['name']} {v}"
            if is_num(x["yoy"]):
                t += f"（前年同期比 {x['yoy']:+.1f}%）"
            parts.append(t)
        R.append(("直近四半期" + (f"（{ea['q_date']}）" if ea.get("q_date") else ""), " ／ ".join(parts)))
    R.append(("決算の評価（機械判定）", ea.get("verdict", "―")))
    fw = []
    for pk in ("0y", "+1y"):
        f = ea["fwd"].get(pk)
        if not f:
            continue
        t = f"{f['label']}予想EPS {fmt_num(f['eps'],1)}円"
        if is_num(f["growth"]):
            t += f"（前期比 {f['growth']:+.1f}%）"
        if is_num(f["per"]):
            t += f" ／ 予想PER {fmt_num(f['per'],1)}倍"
        if is_num(f["n"]):
            t += f" ／ {int(f['n'])}名"
        fw.append(t)
    if ea.get("fwd_rev") and is_num(ea["fwd_rev"]["avg"]):
        rr = ea["fwd_rev"]
        t = f"今期予想 売上 {fmt_yen(rr['avg'])}"
        if is_num(rr["growth"]):
            t += f"（前期比 {rr['growth']:+.1f}%）"
        fw.append(t)
    if fw:
        R.append(("今後の見通し（アナリスト予想）", " ／ ".join(fw)))
    if ea.get("next_earn"):
        R.append(("次回決算 予定日", ea["next_earn"]))
    t = ea.get("target")
    if t and is_num(t.get("mean")):
        s = f"平均 {fmt_num(t['mean'],0)}円"
        if is_num(t.get("vs")):
            s += f"（現在株価比 {t['vs']:+.1f}%）"
        if is_num(t.get("high")) and is_num(t.get("low")):
            s += f" ／ 高値 {fmt_num(t['high'],0)} ・ 安値 {fmt_num(t['low'],0)}"
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


def render_earn_html(ea):
    if not ea or not ea.get("has"):
        return ""
    rows = "".join(f'<div class="plain"><span class="mn">{h}</span>'
                   f'<span class="mv2">{b}</span></div>' for h, b in _earn_rows(ea))
    return (f'<h2>直近決算とアナリスト予想</h2>'
            f'<div class="legend">{EARN_DISC}</div>{rows}')


def render_earn_md(ea):
    if not ea or not ea.get("has"):
        return ""
    L = ["\n## 直近決算とアナリスト予想\n", f"> {EARN_DISC}\n"]
    for h, b in _earn_rows(ea):
        L.append(f"- **{h}**: {b}")
    return "\n".join(L)


# ====================================================================
# SVG チャート
# ====================================================================
def nice_ticks(lo, hi, target=4):
    """[lo, hi] を目盛りに割る（1/2/2.5/5 刻み）。"""
    if not (hi > lo):
        return [lo]
    raw = (hi - lo) / max(target, 1)
    mag = 10.0 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = 10.0 * mag
    for m in (1, 2, 2.5, 5, 10):
        if raw / mag <= m:
            step = m * mag
            break
    v = math.ceil(lo / step) * step
    out = []
    while v <= hi + step * 1e-9 and len(out) < 12:
        out.append(round(v, 10))
        v += step
    return out


def _tick_fmt(v, kind):
    if kind == "pct":
        return f"{v:.1f}%" if abs(v) < 10 else f"{v:.0f}%"
    if kind in ("eps", "price", "dps"):
        return f"{v:,.0f}"
    if kind == "per":
        return f"{v:.1f}"
    a = abs(v)  # yen
    if a >= 1e12:
        return f"{v/1e12:.1f}兆"
    if a >= 1e8:
        return f"{v/1e8:,.0f}億"
    if a >= 1e4:
        return f"{v/1e4:,.0f}万"
    return f"{v:,.0f}"


def _yscale(lo, hi, py, LX, RX, kind, target=4):
    """y 軸目盛り（水平線＋左ラベル）の SVG 断片を返す。"""
    out = []
    for t in nice_ticks(lo, hi, target):
        ty = py(t)
        out.append(f'<line x1="{LX}" y1="{ty:.0f}" x2="{RX}" y2="{ty:.0f}" stroke="var(--line)" stroke-width="1"/>')
        out.append(f'<text x="{LX-4}" y="{ty+3:.0f}" class="cm" style="text-anchor:end">{_tick_fmt(t, kind)}</text>')
    return "".join(out)


def svg_price(hist_m):
    if hist_m is None or getattr(hist_m, "empty", True):
        return "<p class='muted'>株価履歴なし</p>"
    pts = [(idx.to_pydatetime().date(), float(r["Close"])) for idx, r in hist_m.iterrows()
           if r.get("Close") is not None and not (isinstance(r["Close"], float) and math.isnan(r["Close"]))]
    if len(pts) < 4:
        return "<p class='muted'>株価履歴なし</p>"
    W, H, LX, RX, TP, BT = 620, 184, 52, 606, 26, 22
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

    grid = _yscale(lo, hi, py, LX, RX, "price")
    # x 軸：年ラベル
    xlab = []
    for yr in range(xs[0].year, xs[-1].year + 1):
        d = dt.date(yr, 1, 1)
        if x0 <= d.toordinal() <= x1:
            xlab.append(f'<text x="{px(d):.0f}" y="{H-6}" class="cx">{str(yr)[-2:]}</text>')
    d = "M " + " L ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in pts)
    last = pts[-1][1]
    return (f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="株価の推移">'
            f'{grid}{"".join(xlab)}'
            f'<path d="{d}" fill="none" stroke="var(--accent)" stroke-width="2"/>'
            f'<text x="{LX}" y="14" class="ct">株価（円）　{xs[0].year}年〜　'
            f'高値 {hi:,.0f} / 安値 {lo:,.0f} / 直近 {last:,.0f}</text></svg>')


def svg_dps(dps_series, src):
    if not dps_series or len(dps_series) < 2:
        return "<p class='muted'>配当履歴なし</p>"
    data = dps_series[-14:]
    W, H, LX, RX, TP, BT = 620, 194, 48, 604, 26, 24
    vals = [v for _, v in data]
    hi = max(vals) or 1.0
    hi_ax = hi * 1.12
    n = len(data)

    def py(v):
        return TP + (1 - v / hi_ax) * (H - TP - BT)

    bw = (RX - LX) / n * 0.6
    parts = [_yscale(0.0, hi, py, LX, RX, "dps")]
    for i, (yr, v) in enumerate(data):
        x = LX + (i + 0.5) * (RX - LX) / n - bw / 2
        y = py(v)
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{H-BT-y:.1f}" fill="var(--accent)" opacity="0.85"/>')
        parts.append(f'<text x="{x+bw/2:.1f}" y="{H-6:.0f}" class="cx">{str(yr)[-2:]}</text>')
    gr = cagr(vals[0], vals[-1], len(vals) - 1)
    return (f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="1株配当の推移">'
            f'{"".join(parts)}'
            f'<text x="{LX}" y="14" class="ct">1株配当（円・{src}）　{data[0][0]}→{data[-1][0]}　年率 {fmt_pct(gr)}</text></svg>')


def _short_disp(s):
    s = re.split(r"\s*[←／/]\s*", s or "")[0].strip()
    return s[:16]


def _trend_zones(rule):
    """rule → [(lo_val, hi_val, cls, label)] の3ゾーン（値が None＝±∞ の意味）。
    scoreと同じ good/warn 閾値で ◎/△/▲ を区切る。"""
    if not rule:
        return []
    d = rule.get("dir")
    g, w = rule.get("good"), rule.get("warn")
    if d == "higher_better" and is_num(g) and is_num(w):
        return [(g, None, "hi", "◎ 良好"), (w, g, "mid", "△ 注意"), (None, w, "lo", "▲ 弱い")]
    if d == "lower_better" and is_num(g) and is_num(w):
        return [(None, g, "hi", "◎ 良好"), (g, w, "mid", "△ 注意"), (w, None, "lo", "▲ 弱い")]
    if d == "band" and isinstance(g, list) and isinstance(w, list):
        return [(g[0], g[1], "hi", "◎ 良好"), (g[1], w[1], "mid", "△ 注意"), (w[1], None, "lo", "▲ 弱い")]
    return []


def svg_trend(pairs, kind="yen", current=None, rule=None):
    """[(year|None, val)] 古い順 → 小さな折れ線（y軸目盛りつき）。負値は0基準線。
    current を渡すと右端に「現在」マーカー。rule を渡すと ◎/△/▲ ゾーンを塗り分け。"""
    pairs = [p for p in pairs if is_num(p[1])][-9:]
    if len(pairs) < 2:
        return ""
    zones = _trend_zones(rule)
    edges = [e for z in zones for e in (z[0], z[1]) if is_num(e)]
    ys = [v for _, v in pairs] + ([current] if is_num(current) else []) + edges
    lo, hi = min(ys), max(ys)
    if hi == lo:
        hi = lo + max(abs(lo) * 0.1, 1.0)
    pad = (hi - lo) * 0.16
    lo_ax, hi_ax = lo - pad, hi + pad
    has_cur = is_num(current)
    has_zone = bool(zones)
    W, H = (508 if has_zone else 456), 112
    LX, TP, BT = 48, 16, 20
    RX = 356 if has_cur else (372 if has_zone else 392)
    CX = RX + 30                      # 「現在」点の x
    n = len(pairs)
    fmt_end = (fmt_yen if kind == "yen" else
               (lambda x: fmt_num(x, 1) + "%") if kind == "pct" else
               (lambda x: fmt_num(x, 1) + "円"))

    def px(i):
        return LX + i / (n - 1) * (RX - LX)

    def py(v):
        return TP + (1 - (v - lo_ax) / (hi_ax - lo_ax)) * (H - TP - BT)

    grid_r = CX if has_cur else RX
    parts = []
    # ◎/△/▲ ゾーンの帯＋境界線
    zcol = {"hi": "var(--hi)", "mid": "var(--mid)", "lo": "var(--lo)"}
    for zl, zh, cls, lab in zones:
        y_top = TP if zh is None else py(zh)
        y_bot = (H - BT) if zl is None else py(zl)
        y0, y1 = min(y_top, y_bot), max(y_top, y_bot)
        y0 = max(TP, y0)
        y1 = min(H - BT, y1)
        if y1 - y0 > 1:
            parts.append(f'<rect x="{LX}" y="{y0:.0f}" width="{grid_r-LX}" height="{y1-y0:.0f}" fill="{zcol[cls]}" opacity="0.11"/>')
            if y1 - y0 > 10:
                parts.append(f'<text x="{grid_r+4}" y="{(y0+y1)/2+3:.0f}" class="cm" '
                             f'style="fill:{zcol[cls]}">{lab}</text>')
    for zl, zh, cls, lab in zones:
        b = zl if zl is not None else zh
        if is_num(b) and lo_ax < b < hi_ax:
            by = py(b)
            parts.append(f'<line x1="{LX}" y1="{by:.0f}" x2="{grid_r}" y2="{by:.0f}" '
                         f'stroke="{zcol[cls]}" stroke-dasharray="4 3" stroke-width="1.2"/>')
    parts.append(_yscale(lo, hi, py, LX, grid_r, kind, target=3))
    ticks = [round(t, 10) for t in nice_ticks(lo, hi, 3)]
    if not zones and lo_ax < 0 < hi_ax and 0.0 not in ticks:
        zy = py(0.0)
        parts.append(f'<line x1="{LX}" y1="{zy:.0f}" x2="{grid_r}" y2="{zy:.0f}" '
                     f'stroke="var(--inkFaint,#9aa6af)" stroke-dasharray="2 2" stroke-width="1"/>')
    poly = " ".join(f"{px(i):.0f},{py(v):.0f}" for i, (_, v) in enumerate(pairs))
    parts.append(f'<polyline points="{poly}" fill="none" stroke="var(--accent)" stroke-width="2"/>')
    for i, (y, v) in enumerate(pairs):
        parts.append(f'<circle cx="{px(i):.0f}" cy="{py(v):.0f}" r="2.4" fill="var(--accent)"/>')
        if y is not None:
            parts.append(f'<text x="{px(i):.0f}" y="{H-6}" class="cx">{str(y)[-2:]}</text>')
    for i in (0, n - 1):
        v = pairs[i][1]
        anch = "start" if i == 0 else "end"
        parts.append(f'<text x="{px(i):.0f}" y="{py(v)-6:.0f}" class="cm" '
                     f'style="text-anchor:{anch}">{fmt_end(v)}</text>')
    if has_cur:
        ly, cyv = py(pairs[-1][1]), py(current)
        parts.append(f'<line x1="{px(n-1):.0f}" y1="{ly:.0f}" x2="{CX}" y2="{cyv:.0f}" '
                     f'stroke="var(--fg)" stroke-dasharray="3 2" stroke-width="1.4"/>')
        parts.append(f'<circle cx="{CX}" cy="{cyv:.0f}" r="3" fill="var(--fg)"/>')
        parts.append(f'<text x="{CX}" y="{cyv-6:.0f}" class="cm" style="text-anchor:end">{fmt_end(current)}</text>')
        parts.append(f'<text x="{CX}" y="{H-6}" class="cx">現在</text>')
    return (f'<svg viewBox="0 0 {W} {H}" class="chart trend" role="img" aria-label="推移">'
            + "".join(parts) + "</svg>")


def svg_rangeband(rb, title):
    """RSI風。値を y 軸に、履歴レンジを 低/標準/高 の3ゾーンに塗り分け、
    年次履歴の折れ線＋「現在」マーカーを重ねる。"""
    hist = [(y, v) for y, v in (rb.get("hist") or []) if is_num(v)]
    cur = rb.get("current")
    kind = rb.get("kind", "pct")
    up_cheap = not bool(rb.get("low_is_cheap"))   # True＝上ゾーンが割安（利回り）
    if len(hist) < 3 or not is_num(cur):
        return ""
    fmt = (lambda x: fmt_num(x, 2) + "%") if kind == "pct" else (lambda x: fmt_num(x, 1) + "倍")
    vals = [v for _, v in hist]
    vmin, vmax = min(vals), max(vals)
    rng = (vmax - vmin) or 1.0
    lo_ax, hi_ax = min(vmin, cur), max(vmax, cur)
    pad = (hi_ax - lo_ax) * 0.10 or 1.0
    lo_ax -= pad
    hi_ax += pad
    span = hi_ax - lo_ax
    t1, t2 = vmin + rng / 3, vmin + 2 * rng / 3
    W, H, LX, RX, TP, BT = 476, 156, 46, 336, 18, 22
    n = len(hist)

    def py(v):
        return TP + (1 - (v - lo_ax) / span) * (H - TP - BT)

    def px(i):
        return LX + (i / max(n - 1, 1)) * (RX - LX)

    col_up = "var(--hi)" if up_cheap else "var(--lo)"
    col_dn = "var(--lo)" if up_cheap else "var(--hi)"
    if kind == "pct":
        lab_up, lab_dn = "割安（高利回り）", "割高（低利回り）"
    else:
        lab_up, lab_dn = "割高（高PER）", "割安（低PER）"
    P = []
    for y0, y1, col, lab in ((TP, py(t2), col_up, lab_up),
                             (py(t2), py(t1), "var(--na)", "標準"),
                             (py(t1), H - BT, col_dn, lab_dn)):
        P.append(f'<rect x="{LX}" y="{y0:.0f}" width="{RX-LX}" height="{max(1.0,y1-y0):.0f}" fill="{col}" opacity="0.14"/>')
        P.append(f'<text x="{RX+5}" y="{(y0+y1)/2+3:.0f}" class="cm">{lab}</text>')
    tlo, thi = min(vals + [cur]), max(vals + [cur])
    P.append(_yscale(tlo, thi, py, LX, RX, kind, target=3))
    poly = " ".join(f"{px(i):.0f},{py(v):.0f}" for i, (_, v) in enumerate(hist))
    P.append(f'<polyline points="{poly}" fill="none" stroke="var(--muted)" stroke-width="1.5"/>')
    for i, (yr, v) in enumerate(hist):
        P.append(f'<circle cx="{px(i):.0f}" cy="{py(v):.0f}" r="2.2" fill="var(--muted)"/>')
        if yr is not None:
            P.append(f'<text x="{px(i):.0f}" y="{H-6}" class="cx">{str(yr)[-2:]}</text>')
    cy = py(max(lo_ax, min(hi_ax, cur)))
    P.append(f'<line x1="{LX}" y1="{cy:.0f}" x2="{RX+10}" y2="{cy:.0f}" stroke="var(--fg)" stroke-dasharray="3 2" stroke-width="1"/>')
    P.append(f'<path d="M {RX+10} {cy-5:.0f} L {RX+20} {cy:.0f} L {RX+10} {cy+5:.0f} Z" fill="var(--fg)"/>')
    P.append(f'<text x="{RX+13}" y="{H-6}" class="cx">現在</text>')
    cheap = (cur - vmin) / rng if up_cheap else (vmax - cur) / rng
    cheap = max(0.0, min(1.0, cheap)) * 100
    P.append(f'<text x="{LX-4}" y="{TP-6:.0f}" class="ct" style="text-anchor:start">'
             f'現在 {fmt(cur)}　割安度 {cheap:.0f}/100（過去{n}年の{fmt(vmin)}〜{fmt(vmax)}のうち）</text>')
    return f'<svg viewBox="0 0 {W} {H}" class="chart trend" role="img" aria-label="{title}">' + "".join(P) + "</svg>"


def svg_score_bars(groups_def, rowmap, group_scores, title):
    """グループ／指標ごとの「点（0〜110）」を横棒で一覧。実値も右に添える。"""
    W, LX, RX = 660, 190, 468
    BW = RX - LX
    rows = []  # ("grp", 名, 点) or ("m", 名, 点, 実値short)
    for gname, gdef in groups_def.items():
        rows.append(("grp", gname, group_scores.get(gname)))
        for k in gdef["keys"]:
            it = rowmap.get(k)
            if it is None:
                continue
            rows.append(("m", it["name"], it.get("score"), _short_disp(it.get("disp", ""))))
    if not rows:
        return "<p class='muted'>データなし</p>"
    rowH, top = 21, 34
    H = top + len(rows) * rowH + 12
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{title}">']
    out.append(f'<text x="6" y="14" class="ct">{title}（棒＝点。破線＝△60／◎100）</text>')
    for gx, lb in ((60, "△60"), (100, "◎100"), (110, "")):
        x = LX + gx / 110 * BW
        out.append(f'<line x1="{x:.0f}" y1="{top-6}" x2="{x:.0f}" y2="{H-8}" '
                   f'stroke="var(--inkFaint,#9aa6af)" stroke-dasharray="3 3" stroke-width="1"/>')
        if lb:
            out.append(f'<text x="{x:.0f}" y="{top-9}" class="cx">{lb}</text>')
    y = top
    for r in rows:
        if r[0] == "grp":
            gs = r[2]
            out.append(f'<text x="6" y="{y+13}" class="cg">{r[1]}'
                       f'{"" if gs is None else f"　{gs:.0f}"}</text>')
        else:
            _, name, sc, sd = r
            out.append(f'<text x="14" y="{y+14}" class="cm">{name[:17]}</text>')
            out.append(f'<rect x="{LX}" y="{y+3}" width="{BW}" height="13" rx="2.5" fill="var(--line)"/>')
            if is_num(sc):
                w = max(2.0, min(sc, 110.0) / 110.0 * BW)
                col = "var(--hi)" if sc >= 80 else "var(--mid)" if sc >= 62 else "var(--lo)"
                out.append(f'<rect x="{LX}" y="{y+3}" width="{w:.0f}" height="13" rx="2.5" fill="{col}"/>')
                out.append(f'<text x="{RX+6}" y="{y+14}" class="cm">{sc:.0f}点　{sd}</text>')
            else:
                out.append(f'<text x="{LX+6}" y="{y+14}" class="cm">― 不採点</text>')
        y += rowH
    out.append("</svg>")
    return "".join(out)


# ====================================================================
# レポート出力
# ====================================================================
DISC = ("教育目的の一般情報であり、投資助言・特定銘柄の売買推奨ではありません。"
        "表示される「銘柄選定」「買い時」のスコア・ラベルは、公開データをあらかじめ定めた"
        "ルールで機械的に算出したものであり、投資判断の代替にはなりません。運営者は"
        "金融商品取引法上の投資助言・代理業の登録を受けていません。"
        "数値は yfinance／IR BANK 由来で誤り・遅延・欠損があり得ます。"
        "業種平均と判定しきい値は2026年8月時点の目安です。最終判断はご自身の責任で、"
        "一次情報（決算短信・有価証券報告書）をご確認ください。詳しくは利用規約・免責事項を"
        "ご確認ください。")
DISC_HTML = DISC.replace("利用規約・免責事項", '<a href="../terms.html">利用規約・免責事項</a>')


# ---- 指標の説明・判定ルール文の生成（クリックで開く用）----
METRIC_HELP = {
    "rev_cagr": {"what": "売上高の伸び（年率）。配当の一番の原資。伸びていれば増配の余力がある。減っている会社の連続増配は、配当性向の引き上げで支えているだけのことが多い。", "unit": "%"},
    "op_cagr": {"what": "本業の利益（営業利益）の伸び（年率）。売上より速く伸びていれば収益性が改善している。", "unit": "%"},
    "ni_cagr": {"what": "最終利益（純利益）の伸び（年率）。配当は基本ここから支払われる。", "unit": "%"},
    "eps_cagr": {"what": "1株あたり利益の伸び（年率）。自社株買いをすると純利益より速く伸びる。増配の持続力を見る芯。", "unit": "%"},
    "op_margin": {"what": "売上に対する本業利益の比率。高いほど価格競争力・ブランドが強い。業種で標準が大きく違う（商社は数％が普通、通信・医薬は15％超が普通）。", "unit": "%"},
    "earnings_stability": {"what": "過去の営業利益を1年ずつ振り返り、前年から最も大きく落ち込んだ年の下落率を見る指標。"
                            "1.00＝これまで減益した年が一度もない、0.75＝いちばん悪い年でも前年比75%（＝25%減益）は"
                            "維持できた、という意味（0が下限＝赤字転落）。増益の大きさよりも『大きく崩れないこと』を"
                            "配当投資家は重視するため、伸び率ではなくこの指標で見る。景気敏感株（商社・海運・鉄鋼）は"
                            "好不況で利益が大きく上下するため低め、内需ディフェンシブ（通信・食品）は高めに出やすい。"
                            "一度だけ赤字に転落しその後黒字回復した年がある場合は一律0.62点、複数期赤字または直近も"
                            "赤字の場合は最低の0点として扱う（この2つは通常の「前年比」計算とは別の固定値）。", "unit": ""},
    "equity_ratio": {"what": "総資産のうち返さなくてよい自己資本の割合。高いほど不況・金利上昇に強く、減配しにくい。銀行・保険・不動産・リースは構造的に低いのが正常。", "unit": "%"},
    "de": {"what": "有利子負債が自己資本の何倍か。1倍未満なら借金は重くない。インフラ・不動産は事業モデル上、高めでも問題ないことが多い。", "unit": "倍"},
    "net_de": {"what": "手元現金を引いた実質の借金倍率。マイナスなら実質無借金＝減配耐性が高い。", "unit": "倍"},
    "debt_ratio": {"what": "総資産に対する有利子負債の比率。低いほど財務が軽い。", "unit": "%"},
    "debt_to_ocf": {"what": "有利子負債を毎年の営業キャッシュフローで割った、返済にかかる年数の目安。数年以内なら健全。", "unit": "年"},
    "cash_ratio": {"what": "総資産に対する手元現金の比率。潤沢なら一時的な不況でも配当を維持しやすい。", "unit": "%"},
    "ocf_positive": {"what": "本業で現金を毎年きちんと稼げているか。配当の最終的な支払い原資。継続してプラスが理想。", "unit": ""},
    "fcf_positive": {"what": "営業キャッシュフローから設備投資を引いた、自由に使える現金。プラスなら配当・自社株買い・返済に余裕がある。", "unit": ""},
    "fcf_payout": {"what": "配当の支払い総額がフリーキャッシュフローの何％か。安定企業で70％未満なら無理がない。100％超は借入か現金取り崩しで払っている。", "unit": "%"},
    "div_yield": {"what": "株価に対する予想年間配当の割合。厚いほどインカムは大きいが、5％超は減配・業績悪化のサインのこともある。", "unit": "%"},
    "dgr5": {"what": "直近5年の1株配当の年平均の伸び率。プラスが続いているのが増配株の条件。", "unit": "%"},
    "streak_up": {"what": "連続で増配してきた年数。10年以上で優良、20年超で王道クラス。景気に関係なく増やせる証拠。", "unit": "年"},
    "streak_flat": {"what": "減配せずに配当を維持・増加してきた年数＝直近の減配からの経過年数。危機でも下げなかったかを見る。分割調整のゆらぎも「減配」として途切れるため、実際の無減配記録より短く出ることがある（過小評価側）。過去に一度減配していても、その経過年数が長ければ得点は上がっていく設計＝過去の減配を恒久的な減点にはしない（詳しくは「減配履歴」参照）。", "unit": "年"},
    "div_policy": {"what": "会社が『累進配当（減配しない）』や『DOE（自己資本の一定％を配当）』を配当方針として公式に掲げているか。掲げていれば減配耐性が高い。手管理リスト（2026-09時点）で、宣言ありなら加点、宣言なしは減点しない（連続増配の実績で判断する）。連続増配20年超の銘柄は連続増配年数で既に評価されるため、公式宣言のある銘柄だけを対象にしている。", "unit": ""},
    "payout_ni": {"what": "純利益のうち配当に回す割合。低め〜中位だと不況でも減配せずに済む余裕がある。高すぎると次の減益で減配リスク。", "unit": "%"},
    "roe": {"what": "自己資本をどれだけ効率よく利益に変えているか。10％以上で優良。低いと増配の元手が細い。", "unit": "%"},
    "roic_vs_sector": {"what": "投下資本（有利子負債＋自己資本−現金）をどれだけ利益に変えているかを、同じ業種の中央値と比べた倍率。ROEはレバレッジで水増しされるが、ROICは借金に頼らない『事業そのものの稼ぐ力』を映す。1.0＝業種平均並み、1.1超で優良。", "unit": "倍"},
    "per_band_pos": {"what": "今のPERが、その銘柄自身の過去数年のレンジの中でどの位置にあるか。下限（＝低PER）に近いほど、その銘柄の物差しで割安。業種平均比（PER対業種）と違い、成長性の高い銘柄でも『自分史比で安いか』を見られる。", "unit": ""},
    "yield_spread": {"what": "予想配当利回り − 10年国債利回り。公益・通信・不動産など『債券の代わり』として買われる業種は、金利が上がると相対的な魅力が下がって株価も下がる（＝利回りの絶対値だけでは割安か分からない）。スプレッドが過去より広ければ割安。2.5％以上で買い場の目安。", "unit": "%"},
    "chowder": {"what": "現在の配当利回り＋過去5年の増配率。増配スピードも含めて割安かを見る一次スクリーニング。合計12％以上（利回りが安定した公益・通信は8％以上）で合格。", "unit": "%"},
    "per_vs_sector": {"what": "株価が1株利益の何倍か（PER）を、同じ業種の平均と比べた倍率。1.0未満＝業種平均より安い。業種をまたいだ比較はしない。", "unit": "倍"},
    "pbr_vs_sector": {"what": "株価が1株純資産の何倍か（PBR）を業種平均と比べた倍率。1倍割れは東証が是正を求めているテーマでもある。", "unit": "倍"},
    "yield_band_pos": {"what": "今の配当利回りが、その銘柄自身の過去5年のレンジの中でどの位置にあるか。上限（＝高利回り）に近いほど、その銘柄の物差しで割安。", "unit": ""},
    "rsi_score": {"what": "株価の値動きの強弱（RSI）。売られすぎ（30未満）は押し目、買われすぎ（70超）は過熱。銘柄選びではなく『今 買うか少し待つか』の補助にだけ使う。", "unit": ""},
}

NAME_HELP = {
    "税引前利益（経常利益に相当・IFRS）": "IFRS採用企業には日本基準の『経常利益』がなく、最も近いのが税引前利益。営業外の損益まで含んだ利益。",
    "倒産ヘッジ（ネット現金の有無）": "手元現金が有利子負債を上回っていれば（ネットキャッシュ）、資金繰りで行き詰まるリスクが低い。",
    "投資CF（直近／推移）": "設備・買収などへの支出。成長企業は大きくマイナスになるのが普通。",
    "財務CF（直近／推移）": "配当・自社株買い・借入返済の収支。株主還元に積極的な会社はマイナスが続く。",
    "CFの符号パターン": "営業＋／投資－／財務－ が『本業で稼いで投資と株主還元に回す』健全型。",
    "配当の中身（記念・特別の有無）": "記念配当・特別配当が乗った年の利回りで判断すると、翌年それが外れて減配に見える。普通配当だけで見る。",
    "還元方針（累進配当・DOEの明記）": "『減配しない（累進配当）』『自己資本の一定％を配当する（DOE）』と会社が明言していれば減配耐性が高い。会社ごとに違うので自動判定はできない。",
    "益回り（1÷PER）": "PERの逆数。株式の『利回り』に相当し、国債利回りと比べて割高・割安の目安にする。",
    "MACD(12,26,9)": "中期の移動平均の向き。上向き転換（ゴールデンクロス）／下向き転換の判定に使う短期指標。",
    "スローストキャスティクス(14,3,3)": "直近の値幅の中で終値がどこにあるか。80超で買われすぎ、20未満で売られすぎ。",
}


def _fmt_thr(key, val):
    if not is_num(val):
        return "―"
    u = METRIC_HELP.get(key, {}).get("unit", "")
    if key in ("yield_band_pos", "per_band_pos"):
        return f"割安度 {val * 100:.0f}/100"
    if key == "earnings_stability":
        return f"{val:.2f}"
    if abs(val - round(val)) < 1e-9:
        return f"{int(round(val))}{u}"
    return f"{val:.2f}{u}" if u == "倍" else f"{val:.1f}{u}"


SCALE_NOTE = "点は good/warn の間を直線で補間（warn=60・good=100・別格=最大110・下限20）。◎△▲ は閾値どおり。"


def rule_block_html(key, rule, jp_sector):
    if key == "div_policy":
        return ("手管理リスト（dividend_policy.json・2026-09時点）で会社の配当方針を判定<br>"
                "◎ 『累進配当』を含む宣言あり … 106点<br>"
                "◎ 『DOE』のみ宣言あり … 100点<br>"
                "― 宣言が確認できない … 採点しない（減点はしない。連続増配の実績で判断）<br>"
                "<span class='muted'>※会社の中期経営計画・配当方針の最新開示で要確認。</span>")
    if not rule:
        return "判定ルールなし（参考値として表示）。"
    d = rule.get("dir")
    if d == "skip":
        return f"{jp_sector} ではこの指標を評価しません（業種の構造上、他の指標で判断します）。"
    lab = f"（{jp_sector}の基準）"
    if key in ("ocf_positive", "fcf_positive"):
        return f"{lab}<br>◎ 良好 … 直近がプラス（黒字）＝100点<br>▲ 弱い … 直近がマイナス（赤字）＝20点"
    if key == "rsi_score":
        return ("◎ 押し目 … RSI 30未満（売られすぎ）＝100点<br>○ 中立 … 30〜60＝80〜60点<br>"
                "△ やや過熱 … 60〜70＝40点<br>▲ 過熱 … 70以上（買われすぎ）＝20点")

    g, w = rule.get("good"), rule.get("warn")
    if d in ("higher_better", "lower_better"):
        s = abs(g - w)
        hi_better = (d == "higher_better")
        far = (g + s) if hi_better else (g - s)
        near = (max(w - s, 0.0) if (hi_better and key in NONNEG_KEYS) else
                (w - s) if hi_better else (w + s))
        a1, a2 = ("以上", "未満") if hi_better else ("以下", "超")
        dirword = "大きい" if hi_better else "小さい"
        return (f"{lab}・値が{dirword}ほど良い<br>"
                f"◎ 良好 … {_fmt_thr(key,g)} {a1}"
                f"（{_fmt_thr(key,far)} {a1}で「別格」＝最大110点）<br>"
                f"△ 注意 … {_fmt_thr(key,w)}〜{_fmt_thr(key,g)}（この区間を 60→100点で直線補間）<br>"
                f"▲ 弱い … {_fmt_thr(key,w)} {a2}"
                f"（{_fmt_thr(key,near)} で20点、それ以降はフラット20）<br>"
                f"<span class='muted'>※ラベル ◎△▲ は {_fmt_thr(key,g)} / {_fmt_thr(key,w)} の閾値どおり。</span>")
    if d == "band":
        u = METRIC_HELP.get(key, {}).get("unit", "")
        s = w[1] - g[1]
        return (f"{lab}<br>◎ 良好 … {g[0]:g}〜{g[1]:g}{u}（100点。浅く収まっても「別格」加点はなし）<br>"
                f"△ 注意 … {g[1]:g}〜{w[1]:g}{u}（100→60点で直線補間）<br>"
                f"▲ 弱い … {w[1]:g}{u} 超（{w[1] + s:g}{u} で20点、それ以降フラット20）")
    return "―"


def why_block_html(item, rule, jp_sector, is_simple, dom):
    key = item.get("key")
    v = item.get("v")
    score = item.get("score")
    label = item.get("label")
    disp = item.get("disp", "")
    if is_simple and dom in ("業績", "財務", "キャッシュフロー"):
        return (f"{jp_sector} ではこの分野を構造的に別基準で見るため、本ツールでは採点していません"
                "（上のルールは通常業種向けの参考）。")
    if key == "div_policy":
        if not is_num(v):
            return "累進配当・DOE の公式宣言はリストで確認できませんでした。減点はせず、連続増配の実績で判断しています。"
        return f"{disp} → 減配耐性が高い方針なので加点（{score:.0f}点／◎）。会社の最新開示で要確認。"
    if not rule:
        return "参考値のため判定していません。"
    if v is None or score is None:
        return "この指標の値を取得できなかったため判定していません（―）。"
    pt = f"{score:.0f}点"
    if key in ("ocf_positive", "fcf_positive"):
        return (f"直近がプラス（黒字）なので ◎ 良好（{pt}）。" if v > 0
                else f"直近がマイナス（赤字）なので ▲ 弱い（{pt}）。")
    if key == "rsi_score":
        m = re.match(r"\s*(\d+)", disp or "")
        rv = m.group(1) if m else "―"
        si = int(score) if is_num(score) else 0
        lbl = {100: "売られすぎ＝押し目", 80: "やや売られ気味", 60: "中立",
               40: "やや買われすぎ", 20: "買われすぎ＝過熱"}.get(si, "")
        tg = {100: "◎ 押し目", 80: "○ 中立", 60: "○ 中立", 40: "△ やや過熱", 20: "▲ 過熱"}.get(si, "")
        return f"RSI は {rv}。{lbl}なので {tg}（{pt}）。※銘柄選定には使わず、買うタイミングの補助のみ。"

    d = rule.get("dir")
    g, w = rule.get("good"), rule.get("warn")
    vs = _fmt_thr(key, v)

    if d in ("higher_better", "lower_better"):
        s = abs(g - w)
        hi_better = (d == "higher_better")
        far = (g + s) if hi_better else (g - s)
        beyond = (v >= far) if hi_better else (v <= far)
        gword = "上回って" if hi_better else "下回って"
        wword_ok = "上回る" if hi_better else "下回る"
        wword_ng = "下回る" if hi_better else "超える"
        gmiss = "には届かない" if hi_better else "は超えてしまう"
        if label == "good":
            if beyond:
                return (f"値は {vs}。良好ライン {_fmt_thr(key,g)} を大きく{gword}、"
                        f"別格ライン {_fmt_thr(key,far)} も突破したので満点級（{pt}／◎ 良好）。")
            return (f"値は {vs}。良好ライン {_fmt_thr(key,g)} を{gword}いるので ◎ 良好。"
                    f"良好ラインから別格ラインまでの距離に応じて 100〜110点 → {pt}。")
        if label == "warn":
            return (f"値は {vs}。良好ライン {_fmt_thr(key,g)} {gmiss}が、"
                    f"注意ライン {_fmt_thr(key,w)} は{wword_ok}。"
                    f"良好ラインまでの距離に応じて 60〜100点 → {pt}（△ 注意）。")
        return (f"値は {vs}。注意ライン {_fmt_thr(key,w)} を{wword_ng}ので ▲ 弱い。"
                f"注意ラインからの距離に応じて 20〜60点 → {pt}。")

    if d == "band":
        u = METRIC_HELP.get(key, {}).get("unit", "")
        if label == "good":
            return f"値は {vs}。健全域 {g[0]:g}〜{g[1]:g}{u} に収まっているので ◎ 良好（{pt}）。"
        if label == "warn":
            return (f"値は {vs}。健全域 {g[0]:g}〜{g[1]:g}{u} は外れたが許容範囲 {w[0]:g}〜{w[1]:g}{u} 内。"
                    f"健全域の端からの距離に応じて 60〜100点 → {pt}（△ 注意）。")
        return (f"値は {vs}。許容範囲 {w[0]:g}〜{w[1]:g}{u} も外れたので ▲ 弱い（{pt}）。")
    return ""


def bar(score):
    if score is None:
        return '<div class="bar"><div class="fill na" style="width:100%"></div></div><span class="sc na">評価対象外</span>'
    w = min(100.0, score)
    cls = "hi" if score >= 80 else "mid" if score >= 62 else "lo"
    return f'<div class="bar"><div class="fill {cls}" style="width:{w:.0f}%"></div></div><span class="sc {cls}">{score:.0f}</span>'


def tag(item):
    lab = item.get("label")
    sc = item.get("score")
    if lab == "rsi":
        if sc is None:
            return '<span class="t na">―</span>'
        if sc >= 80:
            return '<span class="t hi">◎ 押し目</span>'
        if sc >= 60:
            return '<span class="t mid">○ 中立</span>'
        if sc >= 40:
            return '<span class="t mid">△ やや過熱</span>'
        return '<span class="t lo">▲ 過熱</span>'
    if lab == "good":
        return '<span class="t hi">◎ 良好</span>'
    if lab == "warn":
        return '<span class="t mid">△ 注意</span>'
    if lab == "bad":
        return '<span class="t lo">▲ 弱い</span>'
    return '<span class="t na">―</span>'


KEY_DOMAIN = {k: dom for dom, ks in DOMAIN_KEYS.items() for k in ks}


def _metric_details_html(it, jp, is_simple, rules):
    dom = KEY_DOMAIN.get(it.get("key"), "")
    rule = rule_for(it["key"], jp, rules) if it.get("key") else None
    wht = METRIC_HELP.get(it.get("key"), {}).get("what", "")
    rb = rule_block_html(it.get("key"), rule, jp)
    wb = why_block_html(it, rule, jp, is_simple, dom)
    what_p = f'<p class="what"><b>見るポイント：</b>{wht}</p>' if wht else ""
    sc = it.get("score")
    sc_txt = f"{sc:.0f} / 110" if is_num(sc) else "―（不採点）"
    trend_p = ""
    sp = it.get("series")
    if sp and len([1 for _, v in sp if is_num(v)]) >= 2:
        # 推移の単位と判定ルールの単位が一致する指標だけ ◎/△/▲ ゾーンを描く
        trend_rule = rule if it.get("key") in ("op_margin", "equity_ratio", "payout_ni", "div_yield") else None
        trend_p = (f'<p class="rule"><b>推移：</b>古い→新しい'
                   + ("　（帯＝◎良好／△注意／▲弱いの区切り）" if trend_rule else "")
                   + f'</p><div class="trendwrap wide">'
                   f'{svg_trend(sp, it.get("series_kind", "yen"), it.get("series_current"), trend_rule)}</div>')
    band_p = ""
    rbd = it.get("rangeband")
    if rbd:
        bsvg = svg_rangeband(rbd, it["name"])
        if bsvg:
            band_p = (f'<p class="rule"><b>過去レンジ内の位置：</b>ゾーン塗り＝低/標準/高、'
                      f'折れ線＝年次履歴、▶＝現在</p><div class="trendwrap wide">{bsvg}</div>')
    return (
        '<details class="m"><summary>'
        f'<span class="mn">{it["name"]}</span>'
        f'<span class="mv">{it["disp"]}</span>'
        f'<span class="mr">{it.get("ref","")}</span>'
        f'<span class="mt">{tag(it)}</span></summary>'
        f'<div class="mbody">{what_p}'
        f'{band_p}'
        f'{trend_p}'
        f'<p class="rule"><b>判定ルール：</b><br>{rb}</p>'
        f'<p class="why"><b>この銘柄：</b>{wb}</p>'
        f'<p class="rule">この指標の点：<b>{sc_txt}</b>（グループスコアはこの点の平均）</p>'
        f'</div></details>')


def render_html(meta, dom_scores, detail, groups, sel_score, tim_score, vd, M, ctx, sec_avg, warnings, rules):
    jp = meta["jp_sector"]
    rowmap = {r["key"]: r for dom in detail for r in detail[dom] if r.get("key")}
    sg = rules["score_groups"]

    def head_section(head):
        out = []
        for gname, gdef in sg[head].items():
            out.append(f'<div class="domhead"><b>{gname}</b> {bar(groups.get(gname))}</div>')
            got = False
            for k in gdef["keys"]:
                it = rowmap.get(k)
                if it is None:
                    continue
                got = True
                out.append(_metric_details_html(it, jp, meta["is_simple"], rules))
            if not got:
                out.append('<div class="plain"><span class="mn">―</span>'
                           '<span class="mv2">この業種では評価対象外</span></div>')
        return "".join(out)

    sel_blocks = head_section("選定")
    tim_blocks = head_section("買い時")
    sel_chart = (
        '<details class="chartbox"><summary>銘柄選定の指標スコアを一覧グラフで見る</summary>'
        '<div class="cbody">'
        + svg_score_bars(sg["選定"], rowmap, groups, "銘柄選定 指標スコア一覧")
        + '</div></details>')

    ref_blocks = []
    for it in M.get("参考", []):
        link = it.get("ref", "")
        link_html = f' <a href="{link}">{link}</a>' if link.startswith("http") else ""
        h = NAME_HELP.get(it["name"])
        sp = it.get("series")
        trend = ""
        if sp and len([1 for _, v in sp if is_num(v)]) >= 2:
            trend = (f'<p class="rule"><b>推移：</b>古い→新しい</p>'
                     f'<div class="trendwrap">{svg_trend(sp, it.get("series_kind", "yen"))}</div>')
        if h or trend:
            ref_blocks.append(
                '<details class="m"><summary>'
                f'<span class="mn">{it["name"]}</span>'
                f'<span class="mv2">{it["disp"]}{link_html}</span></summary>'
                f'<div class="mbody">{(f"<p class=\"what\">{h}</p>" if h else "")}{trend}</div></details>')
        else:
            ref_blocks.append(
                f'<div class="plain"><span class="mn">{it["name"]}</span>'
                f'<span class="mv2">{it["disp"]}{link_html}</span></div>')

    legend = ("各行をクリックすると「見るポイント／判定ルール／この評価になった理由」が開きます。"
              "ラベルは3段階（◎良好／△注意／▲弱い）＋対象外「―」。"
              "点は good/warn の2閾値の間を直線補間（warn=60・good=100・別格ライン=最大110・下限20）。"
              "ラベルの◎△▲は閾値どおりなので『△なのに94点（＝基準ぎりぎり）』とズレることがあります。"
              "グループスコア＝その指標の点の平均、"
              "銘柄選定＝業績28・財務27・CF15・配当の持続力30％、"
              "買い時＝配当利回りセオリー38・利回り水準とChowder24・株価バリュエーション20・金利スプレッド18％の加重平均。"
              + ("　この業種は簡易判定：業績・財務・CF は不採点、銘柄選定は配当の持続力のみで出します。"
                 if meta["is_simple"] else ""))

    warn_html = ""
    if warnings:
        warn_html = '<div class="warn"><b>データ上の注意</b><ul>' + "".join(f"<li>{w}</li>" for w in warnings) + "</ul></div>"

    gauge_pos = 50
    if vd.get("val_idx") is not None:
        gauge_pos = max(2, min(98, 50 - vd["val_idx"] * 180))

    def _sc_cls(sc, tiers):
        t = _tier(sc, tiers)
        return "hi" if t == "hi" else "mid" if t == "mid" else "lo"

    sel_cls = _sc_cls(sel_score, SEL_TIERS)
    tim_cls = _sc_cls(tim_score, TIM_TIERS)
    sel_n = f"{sel_score:.0f}" if is_num(sel_score) else "―"
    tim_n = f"{tim_score:.0f}" if is_num(tim_score) else "―"
    sel_w = min(100, sel_score) if is_num(sel_score) else 0
    tim_w = min(100, tim_score) if is_num(tim_score) else 0
    (sel_sc, sel_ps), (_, sel_cl) = vd["選定cov"]
    (tim_sc, tim_ps), (_, tim_cl) = vd["買い時cov"]
    sel_cov_h = f'採点できた指標 {sel_sc}/{sel_ps}　カバレッジ<b>{sel_cl}</b>'
    tim_cov_h = f'採点できた指標 {tim_sc}/{tim_ps}　カバレッジ<b>{tim_cl}</b>'

    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{meta['name']}（{meta['code']}）銘柄診断</title>
<style>
:root{{
  --bg:#ffffff; --fg:#1d232b; --muted:#6b7683; --line:#e4e8ec; --card:#f7f9fa;
  --accent:#2f9e91; --hi:#2f9e91; --mid:#e0912f; --lo:#d1584f; --na:#c3ccd3;
}}
@media (prefers-color-scheme:dark){{
  :root{{ --bg:#161a1e; --fg:#e7ecef; --muted:#9aa6af; --line:#2c333a; --card:#1e242a;
    --accent:#4fb8ab; --hi:#4fb8ab; --mid:#e0a45a; --lo:#e07b73; --na:#4a555e; }}
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
  font-family:"Segoe UI","Hiragino Kaku Gothic ProN","Noto Sans JP",Meiryo,sans-serif;
  line-height:1.6;font-size:14px}}
.wrap{{max-width:820px;margin:0 auto;padding:26px 20px 60px}}
h1{{font-size:20px;margin:0 0 2px}}
.sub{{color:var(--muted);font-size:12.5px;margin-bottom:18px}}
.top{{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:14px 18px;margin-bottom:8px}}
.score2{{display:flex;gap:18px;flex-wrap:wrap}}
.sblk{{flex:1;min-width:250px}}
.sblk .hd{{display:flex;align-items:baseline;gap:8px;margin-bottom:3px}}
.sblk .hd b{{font-size:13px}}
.sblk .num{{font-size:26px;font-weight:700;line-height:1}}
.sblk .num.hi{{color:var(--hi)}} .sblk .num.mid{{color:var(--mid)}} .sblk .num.lo{{color:var(--lo)}}
.sblk .lab{{font-size:12.5px;font-weight:700}}
.gbar{{height:10px;border-radius:5px;background:var(--line);overflow:hidden;margin:4px 0 6px}}
.gbar>i{{display:block;height:100%}}
.gbar>i.hi{{background:var(--hi)}} .gbar>i.mid{{background:var(--mid)}} .gbar>i.lo{{background:var(--lo)}}
.sblk .sub{{font-size:11.5px;color:var(--muted)}}
.sblk .cov{{font-size:11px;color:var(--muted);margin:0 0 5px}}
.sblk .cov b{{font-weight:700}}
.sblk .cov.cov-低{{color:var(--lo)}} .sblk .cov.cov-低 b{{color:var(--lo)}}
.sblk .cov.cov-中 b{{color:var(--mid)}}
.quad{{margin:10px 0 14px;padding:10px 14px;border-left:4px solid var(--accent);
  background:var(--card);border-radius:6px;font-size:13.5px;font-weight:700}}
.lines div{{padding:3px 0;border-bottom:1px dashed var(--line)}}
.lines div:last-child{{border:0}}
.lines k{{display:inline-block;width:76px;color:var(--muted);font-size:12px}}
a{{color:var(--accent)}}
.legend{{font-size:11.5px;color:var(--muted);margin:2px 0 12px;line-height:1.5}}
.domhead{{background:var(--card);border:1px solid var(--line);border-radius:6px;
  padding:8px 10px;margin:16px 0 0;font-size:14px}}
details.m{{border-bottom:1px solid var(--line)}}
details.m>summary{{list-style:none;cursor:pointer;display:grid;
  grid-template-columns:minmax(130px,1.5fr) 1fr 1.25fr 86px;gap:10px;align-items:start;
  padding:9px 6px 9px 22px;position:relative;font-size:13px}}
details.m>summary::-webkit-details-marker{{display:none}}
details.m>summary::before{{content:"▸";position:absolute;left:6px;top:9px;color:var(--muted);
  transition:transform .15s}}
details.m[open]>summary::before{{transform:rotate(90deg)}}
details.m>summary:hover{{background:var(--card)}}
details.m .mv{{font-variant-numeric:tabular-nums}}
details.m .mr{{color:var(--muted);font-size:12px}}
details.m .mt{{text-align:right;white-space:nowrap}}
.mv2{{font-size:12.5px}}
.mbody{{padding:2px 14px 14px 22px;background:var(--card);font-size:12.5px}}
.mbody p{{margin:6px 0}}
.mbody .rule{{color:var(--muted)}}
.mbody .why{{border-left:3px solid var(--accent);padding-left:8px}}
.plain{{display:grid;grid-template-columns:minmax(130px,1fr) 2fr;gap:10px;
  padding:9px 6px 9px 22px;border-bottom:1px solid var(--line);font-size:13px}}
@media(max-width:560px){{
  details.m>summary{{grid-template-columns:1fr 80px}}
  details.m>summary .mv,details.m>summary .mr{{display:none}}
  .plain{{grid-template-columns:1fr}}
}}
.bar{{display:inline-block;width:150px;height:9px;border-radius:5px;background:var(--line);
  overflow:hidden;vertical-align:middle;margin:0 8px}}
.fill{{height:100%}} .fill.hi{{background:var(--hi)}} .fill.mid{{background:var(--mid)}}
.fill.lo{{background:var(--lo)}} .fill.na{{background:var(--na)}}
.sc{{font-weight:700}} .sc.hi{{color:var(--hi)}} .sc.mid{{color:var(--mid)}} .sc.lo{{color:var(--lo)}} .sc.na{{color:var(--muted);font-weight:400;font-size:12px}}
.t{{font-size:11.5px;padding:2px 6px;border-radius:5px;border:1px solid var(--line)}}
.t.hi{{color:var(--hi)}} .t.mid{{color:var(--mid)}} .t.lo{{color:var(--lo)}} .t.na{{color:var(--muted)}}
.gauge{{position:relative;height:30px;border-radius:6px;margin:10px 0 4px;
  background:linear-gradient(90deg,var(--lo),var(--na) 50%,var(--hi))}}
.gauge i{{position:absolute;top:-4px;width:2px;height:38px;background:var(--fg);left:{gauge_pos:.0f}%}}
.gauge b{{position:absolute;font-size:10px;color:#fff;top:7px}}
.gl{{left:8px}} .gr{{right:8px}}
h2{{font-size:15px;margin:26px 0 4px;border-left:4px solid var(--accent);padding-left:8px}}
.charts{{display:flex;gap:14px;flex-wrap:wrap}}
.chart{{flex:1;min-width:280px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px}}
.ct{{fill:var(--muted);font-size:10px}} .cx{{fill:var(--muted);font-size:9px;text-anchor:middle}}
.cm{{fill:var(--muted);font-size:9px}} .cg{{fill:var(--fg);font-size:10px;font-weight:700}}
details.chartbox{{border:1px solid var(--line);border-radius:8px;margin:8px 0 4px;background:var(--card)}}
details.chartbox>summary{{list-style:none;cursor:pointer;padding:8px 12px;font-size:12.5px;font-weight:700;position:relative}}
details.chartbox>summary::-webkit-details-marker{{display:none}}
details.chartbox>summary::before{{content:"▸";color:var(--muted);margin-right:6px;display:inline-block;transition:transform .15s}}
details.chartbox[open]>summary::before{{transform:rotate(90deg)}}
details.chartbox .cbody{{padding:0 8px 10px}}
details.chartbox .chart{{border:0;background:transparent;padding:0}}
.trendwrap{{max-width:440px;margin:2px 0 8px}}
.trendwrap.wide{{max-width:490px}}
svg.trend{{width:100%;height:auto;border:1px solid var(--line);border-radius:8px;background:var(--bg);padding:4px}}
.warn{{background:color-mix(in srgb,var(--mid) 12%,var(--bg));border:1px solid var(--mid);
  border-radius:10px;padding:10px 14px;margin:14px 0;font-size:12.5px}}
.warn ul{{margin:6px 0 0;padding-left:18px}}
.muted{{color:var(--muted)}}
.disc{{margin-top:30px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:11.5px}}
.meta2{{color:var(--muted);font-size:12px;margin:2px 0 0}}
.topbar{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}}
.topbar a{{font-size:12.5px;white-space:nowrap}}
@media print{{body{{font-size:11px}} .wrap{{max-width:none}} .topbar a{{display:none}}}}
</style></head><body><div class="wrap">

<div class="topbar"><h1>{meta['name']}（{meta['code']}）　配当株スクリーニング</h1><a href="../index.html">← 一覧へ戻る</a></div>
<div class="sub">東証33業種：<b>{meta['jp_sector']}</b>
（yfinance分類：{meta['industry'] or '―'} / {meta['sector'] or '―'}　→ {meta['sector_src']}）
{'　｜　<b>簡易判定モード</b>（財務・CF・業績は構造的に別基準のため参考表示）' if meta['is_simple'] else ''}<br>
現在株価 <b>{fmt_num(meta['price'],1)}円</b>{f"（{meta['price_date']} 終値）" if meta.get('price_date') else ''}　｜　時価総額 {fmt_yen(meta['mcap'])}　｜　レポート作成 {meta['today']}
{('　｜　取得単価 ' + fmt_num(meta['cost'],0) + '円 → YOC <b>' + fmt_pct(meta['yoc'],2) + '</b>') if meta.get('yoc') else ''}
</div>

{render_company_html(ctx.get('company'))}

{warn_html}

<div class="top">
  <div class="score2">
    <div class="sblk">
      <div class="hd"><b>銘柄選定</b><span class="num {sel_cls}">{sel_n}</span><span class="lab {sel_cls}">{vd['選定ラベル']}</span></div>
      <div class="gbar"><i class="{sel_cls}" style="width:{sel_w:.0f}%"></i></div>
      <div class="cov cov-{sel_cl}">{sel_cov_h}</div>
      <div class="sub">＝配当株としての質のスコア（業績・財務・CF・配当の持続力）<br>
        成長性：{vd['成長性']}　／　安定性：{vd['安定性']}</div>
    </div>
    <div class="sblk">
      <div class="hd"><b>買い時</b><span class="num {tim_cls}">{tim_n}</span><span class="lab {tim_cls}">{vd['買い時ラベル']}</span></div>
      <div class="gbar"><i class="{tim_cls}" style="width:{tim_w:.0f}%"></i></div>
      <div class="cov cov-{tim_cl}">{tim_cov_h}</div>
      <div class="sub">＝現在の株価水準のスコア（配当利回りセオリー・利回り水準とChowder・株価バリュエーション・金利スプレッド）<br>
        割安・割高：{vd['割安・割高']}　／　参考・短期：{vd['短期']}</div>
    </div>
  </div>
</div>
<div class="quad">→ {vd['総合コメント']}</div>

<div class="gauge"><b class="gl">割安</b><b class="gr">割高</b><i></i></div>
<div class="muted" style="font-size:11.5px">PER・PBR（対業種平均）と配当利回りの過去レンジ内の位置を合成した「割安・割高」の目安。</div>

<h2>① 銘柄選定の指標</h2>
{sel_chart}
<div class="legend">{legend}</div>
{sel_blocks}

<h2>② 買い時の指標</h2>
{tim_blocks}

<h2>推移チャート</h2>
<div class="charts">{svg_price(ctx.get('hist_m'))}{svg_dps(ctx['dps_series'], ctx['dps_src'])}</div>

{render_earn_html(ctx.get('earn'))}

<h2>参考・自動判定できない項目</h2>
<div class="legend">クリックで説明が開く行があります。</div>
{''.join(ref_blocks)}

<div class="disc">{DISC_HTML}</div>
</div></body></html>"""


def render_md(meta, dom_scores, detail, groups, sel_score, tim_score, vd, M, ctx, rules):
    jp = meta["jp_sector"]
    rowmap = {r["key"]: r for dom in detail for r in detail[dom] if r.get("key")}
    sg = rules["score_groups"]
    L = []
    L.append(f"# {meta['name']}（{meta['code']}）配当株スクリーニング\n")
    L.append(f"- 東証33業種: **{meta['jp_sector']}**（yfinance: {meta['industry']} / {meta['sector']} → {meta['sector_src']}）")
    L.append(f"- 現在株価: {fmt_num(meta['price'],1)}円"
             + (f"（{meta['price_date']} 終値）" if meta.get('price_date') else "")
             + f" ／ 時価総額: {fmt_yen(meta['mcap'])} ／ レポート作成: {meta['today']}")
    if meta.get("yoc"):
        L.append(f"- 取得単価 {fmt_num(meta['cost'],0)}円 → YOC {fmt_pct(meta['yoc'],2)}")
    if meta["is_simple"]:
        L.append("- **簡易判定モード**（銀行・保険・証券／REIT：財務・CF・業績は不採点）")
    co_md = render_company_md(ctx.get("company"))
    if co_md:
        L.append(co_md)
    sn = f"{sel_score:.0f}" if is_num(sel_score) else "―"
    tn = f"{tim_score:.0f}" if is_num(tim_score) else "―"
    (sel_sc, sel_ps), (_, sel_cl) = vd["選定cov"]
    (tim_sc, tim_ps), (_, tim_cl) = vd["買い時cov"]
    L.append(f"\n## 銘柄選定: {sn} ― {vd['選定ラベル']}")
    L.append(f"- 成長性: {vd['成長性']}　／　安定性: {vd['安定性']}")
    L.append(f"- 採点できた指標: {sel_sc}/{sel_ps}　カバレッジ: **{sel_cl}**")
    L.append(f"\n## 買い時: {tn} ― {vd['買い時ラベル']}")
    L.append(f"- 割安・割高: {vd['割安・割高']}　／　短期: {vd['短期']}")
    L.append(f"- 採点できた指標: {tim_sc}/{tim_ps}　カバレッジ: **{tim_cl}**")
    L.append(f"\n**→ {vd['総合コメント']}**\n")
    L.append("> ラベルは3段階：◎良好／△注意／▲弱い（＋対象外「―」）。"
             "点は good/warn の間を直線補間（warn=60・good=100・別格=最大110・下限20）。"
             "ラベルは閾値どおりなので『△なのに94点』とずれることがある。"
             "グループスコア＝指標の点の平均。銘柄選定＝業績28/財務27/CF15/配当の持続力30％、"
             "買い時＝配当利回りセオリー38/利回り水準とChowder24/株価バリュエーション20/金利スプレッド18％の加重平均。\n")

    for head in ("選定", "買い時"):
        L.append(f"# {'① 銘柄選定の指標' if head == '選定' else '② 買い時の指標'}\n")
        for gname, gdef in sg[head].items():
            gs = groups.get(gname)
            L.append(f"### {gname}　（スコア: {'―' if gs is None else f'{gs:.0f}'}）")
            L.append("| 指標 | 値 | 目安 | 点 | 判定・根拠 |")
            L.append("|---|---|---|---|---|")
            any_row = False
            for k in gdef["keys"]:
                it = rowmap.get(k)
                if it is None:
                    continue
                any_row = True
                sc = it.get("score")
                lab = it.get("label")
                if k == "rsi_score":
                    j = "―" if sc is None else "◎押し目" if sc >= 80 else "○中立" if sc >= 60 else "△やや過熱" if sc >= 40 else "▲過熱"
                else:
                    j = {"good": "◎良好", "warn": "△注意", "bad": "▲弱い"}.get(lab, "―")
                scn = f"{sc:.0f}" if is_num(sc) else "―"
                rule = rule_for(k, jp, rules)
                why = re.sub(r"<[^>]+>", "", why_block_html(it, rule, jp, meta["is_simple"], KEY_DOMAIN.get(k, ""))).replace("|", "／")
                L.append(f"| {it['name']} | {it['disp']} | {it.get('ref','')} | {scn} | {j}　{why} |")
            if not any_row:
                L.append("| ― | この業種では評価対象外 |  |  |  |")
            L.append("")
    em = render_earn_md(ctx.get("earn"))
    if em:
        L.append(em)

    L.append("\n## 参考・自動判定できない項目")
    for it in M.get("参考", []):
        L.append(f"- **{it['name']}**: {it['disp']}" + (f"  <{it['ref']}>" if it.get("ref", "").startswith("http") else ""))
    disc_md = DISC.replace("利用規約・免責事項", "[利用規約・免責事項](../terms.html)")
    L.append(f"\n---\n{disc_md}\n")
    return "\n".join(L)


# ====================================================================
# generate() ― 1銘柄ぶんのレポートとサマリを返す（副作用なし・バッチ/Web兼用）
# ====================================================================
_CFG_CACHE = {}


def load_config(force=False):
    """4つのJSONをまとめて読む。バッチで使い回すためプロセス内キャッシュ。"""
    if _CFG_CACHE and not force:
        return _CFG_CACHE
    _CFG_CACHE.clear()
    _CFG_CACHE.update(
        sec_avg_all=load_json("sector_averages.json"),
        smap=load_json("sector_map.json"),
        rules=load_json("sector_rules.json"),
    )
    try:
        _CFG_CACHE["div_policies"] = load_json("dividend_policy.json").get("policies", {})
    except Exception:
        _CFG_CACHE["div_policies"] = {}
    return _CFG_CACHE


def generate(code, name=None, cost=None, jgb=None, use_irbank=False, cfg=None, log=None):
    """証券コード1つを診断して {code,name,ok,error,html,md,summary} を返す。
    ファイル書き出し・print はしない。summary はランキング集計用のフラット辞書。
    jgb=None なら sector_rules.json の同梱値を使う（バッチでは呼び出し側で1回だけ
    fetch_jgb10() して渡す）。"""
    log = log or (lambda *_: None)
    code = re.sub(r"\D", "", str(code))
    res = {"code": code, "name": name, "ok": False, "error": None,
           "html": None, "md": None, "summary": None}
    if not code:
        res["error"] = "証券コードが不正"
        return res
    cfg = cfg or load_config()
    sec_avg_all, smap, rules = cfg["sec_avg_all"], cfg["smap"], cfg["rules"]
    div_policies = cfg["div_policies"]

    log(f"[1/3] yfinance 取得 {code}.T")
    try:
        yd = fetch_yf(code)
    except Exception as e:
        res["error"] = f"取得失敗: {e}"
        return res
    info = yd.get("info") or {}
    if not info and yd.get("price") is None:
        res["error"] = "データ取得できず（コード確認。日本株のみ）"
        return res

    name = name or info.get("longName") or info.get("shortName") or code
    jp_sector, industry, sector, sector_src, is_reit = classify_sector(info, smap)
    sec_avg = sec_avg_all.get(jp_sector, {})
    is_simple = bool(rules["overrides"].get(jp_sector, {}).get("_simple")) or is_reit

    irbank = None
    if use_irbank:
        try:
            irbank = fetch_irbank_dps(code)
        except Exception:
            irbank = None

    rate_sensitive = set(smap["_meta"].get("rate_sensitive", []))
    jgb_10y = jgb if is_num(jgb) else rules.get("market", {}).get("jgb_10y")
    jgb_src = "指定値" if is_num(jgb) else "同梱設定"

    log("[2/3] 採点")
    try:
        M, flags, ctx = build_metrics(yd, irbank, sec_avg, is_simple, jp_sector,
                                      rate_sensitive, jgb_10y, div_policies)
        ctx["hist_m"] = yd["hist_m"]
        ctx["jgb_src"] = jgb_src
        ctx["earn"] = build_earnings(yd, yd["price"])
        ctx["company"] = build_company_overview(info)
        dom_scores, detail, groups, sel_score, tim_score, coverage = score_all(M, jp_sector, rules, is_simple)
        vd = verdicts(sel_score, tim_score, groups, dom_scores, ctx, sec_avg, is_simple, coverage)
    except Exception as e:
        res["error"] = f"採点失敗: {e}"
        return res

    warnings = []
    for nm, key in (("銘柄選定", "選定"), ("買い時", "買い時")):
        sc, ps = coverage[key]
        _, lab = cov_label(coverage[key])
        if lab == "低" and ps:
            warnings.append(f"{nm}スコアは取得できた指標が少なく（{sc}/{ps}・カバレッジ低）、点数は目安です。")
    if not yd["is_rows"]:
        warnings.append("損益計算書を取得できず、業績・成長性の評価が限定的です。")
    if not yd["bs_rows"]:
        warnings.append("貸借対照表を取得できず、財務の評価が限定的です。")
    if not yd["divs"]:
        warnings.append("配当履歴を取得できませんでした（無配、または yfinance 未収録）。")
    if is_reit:
        warnings.append("REIT はFFO・NAV倍率・LTV・分配金の内訳で見るべき指標が別にあり、本ツール（株式用）では正しく評価できません。参考程度に。")
    if is_simple and not is_reit:
        warnings.append("銀行・保険・証券は自己資本比率・D/E・ROA・営業CFが構造的に別水準です。財務・CF・業績は採点から外し、銘柄選定は配当の持続力のみ、買い時は通常どおりの簡易判定にしています。")
    if industry and not smap["industry_map"].get(industry):
        warnings.append(f"yfinance の業種『{industry}』が対応表に無く、{sector_src} で {jp_sector} に割り当てました。業種平均との比較は目安です。")

    yoc = None
    if is_num(cost) and cost > 0 and is_num(ctx.get("fwd_dps")):
        yoc = ctx["fwd_dps"] / cost * 100

    pdate = yd.get("price_date")
    meta = {
        "code": code, "name": name, "jp_sector": jp_sector, "industry": industry, "sector": sector,
        "sector_src": sector_src, "price": yd["price"], "mcap": info.get("marketCap"),
        "price_date": pdate.isoformat() if pdate else None,
        "today": TODAY.isoformat(), "is_simple": is_simple, "cost": cost, "yoc": yoc,
    }

    log("[3/3] レンダリング")
    try:
        res["html"] = render_html(meta, dom_scores, detail, groups, sel_score, tim_score, vd, M, ctx, sec_avg, warnings, rules)
        res["md"] = render_md(meta, dom_scores, detail, groups, sel_score, tim_score, vd, M, ctx, rules)
    except Exception as e:
        res["error"] = f"描画失敗: {e}"
        return res

    rowmap = {r["key"]: r for dom in detail for r in detail[dom] if r.get("key")}

    def gv(k):
        it = rowmap.get(k)
        return it.get("v") if it else None

    sc_cov, lab_cov = cov_label(coverage["選定"])
    tc_cov, tlab_cov = cov_label(coverage["買い時"])
    ea = ctx.get("earn") or {}
    res["summary"] = {
        "code": code, "name": name, "asof": TODAY.isoformat(),
        "jp_sector": jp_sector, "industry": industry, "is_simple": is_simple, "is_reit": is_reit,
        "price": yd["price"], "price_date": meta["price_date"], "mcap": info.get("marketCap"),
        "sel_score": round(sel_score, 1) if is_num(sel_score) else None,
        "tim_score": round(tim_score, 1) if is_num(tim_score) else None,
        "sel_label": vd.get("選定ラベル"), "tim_label": vd.get("買い時ラベル"),
        "comment": vd.get("総合コメント"),
        "cov_sel": [coverage["選定"][0], coverage["選定"][1], lab_cov],
        "cov_tim": [coverage["買い時"][0], coverage["買い時"][1], tlab_cov],
        "groups": {g: (round(v, 1) if is_num(v) else None) for g, v in groups.items()},
        "div_yield": gv("div_yield"), "streak_up": gv("streak_up"), "streak_flat": gv("streak_flat"),
        "payout_ni": gv("payout_ni"), "roe": gv("roe"), "div_policy": gv("div_policy"),
        "per_vs_sector": gv("per_vs_sector"), "pbr_vs_sector": gv("pbr_vs_sector"),
        "per_band_pos": gv("per_band_pos"), "yield_band_pos": gv("yield_band_pos"),
        "next_earn": ea.get("next_earn"), "earn_disc_date": ea.get("disc_date"),
        "warnings": warnings,
    }
    res["ok"] = True
    res["name"] = name
    return res


# ====================================================================
# main
# ====================================================================
def main():
    ap = argparse.ArgumentParser(description="銘柄診断ツール v1（日本株）")
    ap.add_argument("code", help="証券コード（例: 9433）")
    ap.add_argument("--name", default=None, help="会社名（省略時は yfinance の名称）")
    ap.add_argument("--cost", type=float, default=None, help="取得単価（YOC計算用）")
    ap.add_argument("--no-irbank", action="store_true", help="IR BANK 補助を使わない")
    ap.add_argument("--jgb", type=float, default=None, help="10年国債利回り(％)を明示（金利スプレッド用）")
    args = ap.parse_args()

    code = re.sub(r"\D", "", args.code)
    if not code:
        sys.exit("証券コードを数字で指定してください（例: 9433）")

    cfg = load_config()
    jgb_10y = args.jgb
    if jgb_10y is None:
        print("[*] 10年国債利回りを取得中…")
        jgb_10y = fetch_jgb10()
    if jgb_10y is None:
        jgb_10y = cfg["rules"].get("market", {}).get("jgb_10y")

    print(f"[*] 診断中… {code}.T")
    res = generate(code, name=args.name, cost=args.cost, jgb=jgb_10y,
                   use_irbank=not args.no_irbank, cfg=cfg,
                   log=lambda m: print("   " + m))
    if not res["ok"]:
        sys.exit(f"失敗: {res['error']}")

    s = res["summary"]
    os.makedirs(OUT_DIR, exist_ok=True)
    safe_name = re.sub(r'[\\/:*?"<>|\s]+', "_", res["name"])[:40]
    base = os.path.join(OUT_DIR, f"{code}_{safe_name}")
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(res["html"])
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(res["md"])

    _s = f"{s['sel_score']:.0f}" if is_num(s["sel_score"]) else "―"
    _t = f"{s['tim_score']:.0f}" if is_num(s["tim_score"]) else "―"
    print("[完了]")
    print(f"  HTML : {base}.html")
    print(f"  MD   : {base}.md")
    print(f"  銘柄選定 {_s}（{s['sel_label']}・カバレッジ {s['cov_sel'][0]}/{s['cov_sel'][1]}({s['cov_sel'][2]})）"
          f" ／ 買い時 {_t}（{s['tim_label']}・カバレッジ {s['cov_tim'][0]}/{s['cov_tim'][1]}({s['cov_tim'][2]})）")
    print(f"  → {s['comment']}")


if __name__ == "__main__":
    main()
