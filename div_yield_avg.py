# -*- coding: utf-8 -*-
"""
配当株ツールの母集団スクリーン（build_universe.py／build_universe_us.py）用：
「過去3年の平均配当利回り」を、株式分割を補正して計算する。

背景：Yahoo(yfinance)のデータは、株式分割のあと「配当は分割後の1株に直してあるのに、株価は
過去分が直っていない」（またはその逆）銘柄がある（例：しまむら 2026年2月の3分割）。そのまま
利回り（配当÷株価）を出すと、分割前の年だけ分割比の分だけずれる。

方針：
  1. 直近3年(+2か月)の分割を、履歴の "Stock Splits" から拾う。
  2. 分割ごとに、株価と配当が「分割後ベースに直っているか」を、分割前後の水準の比で診断する。
     分割比に近ければ「未調整」、1に近ければ「調整済み」、どちらでもなければ「判定不能」。
  3. 未調整の側を分割比で割って、分割後の1株ベースにそろえる（株価は分割日の直前数日も除く）。
  4. 判定不能の分割がある銘柄は ambiguous=True（呼び出し側は、その銘柄に3年平均の条件を適用しない）。

  evaluate(h) → {"ynow", "avg3", "years", "ambiguous", "splits"}
    ynow … 直近365日の配当 ÷ 最新株価（分割補正後。ambiguousのときは補正しない素の値）
    avg3 … 年ごと(直近1年・1年前・2年前)の「配当÷その年の平均株価」の平均。3年分そろわなければ None
"""
import datetime as dt
import math


def _diag_one(close, div, d, ratio):
    """分割日dについて (price_unadj, div_unadj) を返す。各々 True=未調整／False=調整済み／None=判定不能。"""
    lr = math.log(ratio)
    pre = close[(close.index < d - dt.timedelta(days=4)) & (close.index >= d - dt.timedelta(days=45))]
    post = close[(close.index >= d + dt.timedelta(days=3)) & (close.index < d + dt.timedelta(days=45))]
    pu = None
    if len(pre) >= 5 and len(post) >= 5:
        lo = math.log(float(pre.median()) / float(post.median()))
        if abs(lo - lr) < 0.35 * abs(lr):
            pu = True
        elif abs(lo) < 0.35 * abs(lr):
            pu = False
    dpre, dpost = div[div.index < d], div[div.index >= d]
    du = None
    if len(dpre) >= 1 and len(dpost) >= 1:
        lo2 = math.log(float(dpre.iloc[-1]) / float(dpost.iloc[0]))
        if abs(lo2 - lr) < 0.35 * abs(lr):
            du = True
        elif abs(lo2) < 0.35 * abs(lr):
            du = False
    return pu, du


def evaluate(h, today=None):
    today = today or dt.date.today()
    close = h["Close"].dropna()
    div = h["Dividends"] if "Dividends" in h else None
    div = div[div > 0] if div is not None else close.iloc[0:0]
    spl = h["Stock Splits"] if "Stock Splits" in h else close.iloc[0:0]
    spl = spl[(spl > 0) & (spl != 1)]
    since = today - dt.timedelta(days=365 * 3 + 60)
    splits = [(d, float(r)) for d, r in spl.items() if d.date() >= since]

    px = [(ix.date(), float(v)) for ix, v in close.items()]
    dv = [(ix.date(), float(v)) for ix, v in div.items()]
    raw_price = px[-1][1] if px else None
    raw_ttm = sum(v for a, v in dv if a >= today - dt.timedelta(days=365))
    raw_ynow = (raw_ttm / raw_price * 100) if raw_price else None

    ambiguous, notes = False, []
    for d, r in splits:
        pu, du = _diag_one(close, div, d, r)
        notes.append((d.date().isoformat(), r, pu, du))
        if pu is None or du is None:
            ambiguous = True
            continue
        dd = d.date()
        if pu:   # 株価が未調整：分割前の株価を分割比で割る（分割日の直前数日は中途半端に直っていることがあるので除く）
            px = [(a, b / r if a < dd - dt.timedelta(days=3) else b)
                  for a, b in px if not (dd - dt.timedelta(days=4) <= a < dd)]
        if du:   # 配当が未調整：分割前の配当を分割比で割る
            dv = [(a, b / r if a < dd else b) for a, b in dv]

    if ambiguous:
        # 判定できない分割があるときは補正しない（素の値）。呼び出し側は3年平均の条件を適用しない。
        return {"ynow": raw_ynow, "avg3": None, "years": None, "ambiguous": True, "splits": notes}

    price = px[-1][1] if px else None
    ynow = (sum(v for a, v in dv if a >= today - dt.timedelta(days=365)) / price * 100) if price else None
    years = []
    for k in range(3):
        end = today - dt.timedelta(days=365 * k)
        st = end - dt.timedelta(days=365)
        prs = [p for a, p in px if st < a <= end]
        if len(prs) < 100:        # その年の株価がほぼ無い（上場3年未満など）
            years.append(None)
            continue
        years.append(sum(v for a, v in dv if st < a <= end) / (sum(prs) / len(prs)) * 100)
    avg3 = (sum(years) / 3) if all(y is not None for y in years) else None
    return {"ynow": ynow, "avg3": avg3, "years": years, "ambiguous": False, "splits": notes}
