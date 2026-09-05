# -*- coding: utf-8 -*-
"""
米国株版：S&P500+400+600（S&P1500構成銘柄）から、公開版が診断できる
配当株ユニバース（universe_us.json）を作る。

日本株版 build_universe.py と違い、外部の「配当貴族/王リスト」には依存しない
（2026-09-05の検証で、dripinvesting.orgのCCCリストは自動取得できないことが
判明したため）。連続非減配年数は yfinanceの配当履歴から
「1回あたり配当額の前年同期比」で自前計算する（nocut_streak_rate。
米国株は決算期がバラバラで暦年集計だと偽の減配が出るため）。

  python build_universe_us.py candidates
      → universe_candidates_us.json（S&P500+400+600をWikipediaから取得。
        ネットワーク必要・yfinance不要・軽い）

  python build_universe_us.py screen --sleep 2.0
      → universe_us.json（利回り・連続非減配年数 の数値スクリーンをyfinanceで適用）
        --limit / --resume 対応。

  python build_universe_us.py all --sleep 2.0     # 上2つを続けて

スクリーンの数値は universe_screen_us.json（利回り下限・減配ルール。
時価総額はS&P600採用基準（時価総額8.5億〜55億ドル程度）で担保されるため
別途の下限は設けない＝2026-09-05にユーザーと確認済み）。
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(__file__)
CAND = os.path.join(HERE, "universe_candidates_us.json")
UNIV = os.path.join(HERE, "universe_us.json")
SCREEN = os.path.join(HERE, "universe_screen_us.json")

WIKI_SOURCES = [
    ("S&P500", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"),
    ("S&P400", "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"),
    ("S&P600", "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"),
]
UA = {"User-Agent": "Mozilla/5.0 (compatible; meigara-shindan-us/1.0)"}


# ---------------------------------------------------------------- S&P1500一覧
def normalize_ticker(sym):
    """WikipediaはBRK.B/BF.Bのようにドット表記。yfinanceはハイフン表記。"""
    return re.sub(r"\.", "-", str(sym).strip().upper())


def build_candidates():
    import pandas as pd
    import requests

    seen = {}
    counts = {}
    for idx_name, url in WIKI_SOURCES:
        html = requests.get(url, headers=UA, timeout=30).text
        df = pd.read_html(io.StringIO(html))[0]
        cols = {c: c for c in df.columns}
        c_sym = "Symbol" if "Symbol" in cols else None
        c_name = "Security" if "Security" in cols else None
        c_sec = "GICS Sector" if "GICS Sector" in cols else None
        if not (c_sym and c_name and c_sec):
            sys.exit(f"{idx_name}: 想定した列が見つかりません。検出した列: {list(df.columns)}")
        n = 0
        for _, r in df.iterrows():
            sym = normalize_ticker(r[c_sym])
            if not sym or sym.lower() == "nan":
                continue
            if sym not in seen:
                seen[sym] = {
                    "ticker": sym,
                    "name": str(r[c_name]).strip(),
                    "gics_sector": str(r[c_sec]).strip(),
                    "index": [idx_name],
                }
                n += 1
            else:
                if idx_name not in seen[sym]["index"]:
                    seen[sym]["index"].append(idx_name)
        counts[idx_name] = n
        print(f"  {idx_name}: {len(df)}行 → 新規{n}銘柄")

    out = sorted(seen.values(), key=lambda x: x["ticker"])
    j = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": "Wikipedia (List of S&P 500/400/600 companies)",
        "counts_by_index": counts,
        "count": len(out),
        "candidates": out,
    }
    json.dump(j, open(CAND, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"→ {CAND}  ({len(out)} 銘柄・重複除去後)")
    return j


# ---------------------------------------------------------------- 連続非減配年数
def nocut_streak_rate(divs, max_years=12):
    """連続非減配年数を『1回あたりの配当額の前年同期比』で数える。
    米国株は決算期・支払スケジュールが会社ごとにバラバラで、暦年や会計年度で
    まとめると支払タイミングのズレで偽の減配が出る（例：Accentureは四半期
    配当だが最近スケジュールを変更しており、暦年集計だと直近年が過少になる）。
    最後の支払日を起点に、そこからk年前に最も近い支払を1件ずつ拾って比較する
    ことで、タイミングのズレに強くする。※特別配当が年アンカー付近にあると
    ノイズになり得るが、これは粗いスクリーン用。詳細分析側で別途精査する。"""
    if not divs:
        return 0
    ds = sorted(divs)
    anchor0 = ds[-1][0]
    rates = []
    for k in range(max_years + 1):
        anchor = anchor0 - dt.timedelta(days=365 * k)
        best = min(ds, key=lambda p: abs((p[0] - anchor).days))
        if abs((best[0] - anchor).days) > 120:
            break  # そこまで遡れる支払がない
        rates.append(best[1])
    s = 0
    for i in range(len(rates) - 1):
        if rates[i] >= rates[i + 1] * 0.995:
            s += 1
        else:
            break
    return s


# ---------------------------------------------------------------- 数値スクリーン
def prescreen_one(ticker, scr):
    """history 1回で 利回り・連続非減配年数 を判定。→ (ok:bool, info:dict)
    日本株版と違い時価総額の下限は設けない（S&P600採用基準で担保済み）。"""
    import yfinance as yf

    tk = yf.Ticker(ticker)
    try:
        h = tk.history(period="7y", auto_adjust=False, actions=True)
    except Exception as e:
        return False, {"reason": f"取得失敗:{e}"}
    if h is None or h.empty or "Close" not in h:
        return False, {"reason": "履歴なし"}
    close = h["Close"].dropna()
    if close.empty:
        return False, {"reason": "株価なし"}
    price = float(close.iloc[-1])

    mcap = None
    try:
        mcap = tk.fast_info.get("marketCap") or tk.fast_info.get("market_cap")
    except Exception:
        pass
    mcap_musd = (mcap / 1e6) if mcap else None

    divs = []
    if "Dividends" in h:
        dser = h["Dividends"]
        dser = dser[dser > 0]
        divs = [(idx.to_pydatetime().date(), float(v)) for idx, v in dser.items()]
    cutoff = dt.date.today() - dt.timedelta(days=365)
    ttm = sum(v for d, v in divs if d >= cutoff)
    yld = (ttm / price * 100) if price else None

    flat_streak = nocut_streak_rate(divs)

    info = {
        "price": round(price, 2),
        "yield": round(yld, 2) if yld else None,
        "mcap_musd": round(mcap_musd) if mcap_musd else None,
        "nocut_years": flat_streak,
    }

    if yld is None or yld < scr["min_dividend_yield_pct"]:
        info["reason"] = f"利回り {info['yield']}% < {scr['min_dividend_yield_pct']}%"
        return False, info
    if flat_streak < scr["no_cut_years"]:
        info["reason"] = f"非減配 {flat_streak}年 < {scr['no_cut_years']}年"
        return False, info
    return True, info


def run_screen(sleep, limit, resume):
    if not os.path.isfile(CAND):
        sys.exit(f"{CAND} がありません。先に `candidates` を実行してください。")
    scr = json.load(open(SCREEN, encoding="utf-8"))
    cand = json.load(open(CAND, encoding="utf-8"))["candidates"]

    prev_pass, prev_rej = {}, {}
    if resume and os.path.isfile(UNIV):
        j = json.load(open(UNIV, encoding="utf-8"))
        prev_pass = {c["ticker"]: c for c in j.get("tickers", [])}
        prev_rej = {c["ticker"]: c for c in j.get("rejected", [])}

    if limit:
        cand = cand[:limit]
    print(f"数値スクリーン {len(cand)} 銘柄  sleep={sleep}s")

    passed, rejected = [], []
    for i, c in enumerate(cand, 1):
        ticker = c["ticker"]
        if resume and (ticker in prev_pass or ticker in prev_rej):
            (passed if ticker in prev_pass else rejected).append(
                prev_pass.get(ticker) or prev_rej.get(ticker))
            continue
        try:
            ok, info = prescreen_one(ticker, scr)
        except Exception as e:
            ok, info = False, {"reason": f"例外:{e}"}
        rec = {"ticker": ticker, "name": c["name"], "gics_sector": c["gics_sector"], **info}
        if ok:
            passed.append(rec)
            print(f"  [OK] {ticker} {c['name']}  利回り{info.get('yield')}% 非減配{info.get('nocut_years')}年")
        else:
            rejected.append(rec)
            if i % 50 == 0:
                print(f"  [{i}/{len(cand)}] skip {ticker} {info.get('reason')}")
        if i < len(cand):
            time.sleep(sleep)

    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "screen": scr,
        "count": len(passed),
        "tickers": passed,
        "rejected": rejected,
    }
    json.dump(out, open(UNIV, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {UNIV}  通過 {len(passed)} / 除外 {len(rejected)}")


# ---------------------------------------------------------------- CLI
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("candidates")
    p2 = sub.add_parser("screen")
    p2.add_argument("--sleep", type=float, default=2.0)
    p2.add_argument("--limit", type=int, default=0)
    p2.add_argument("--resume", action="store_true", help="既存 universe_us.json の判定を引き継ぐ")
    p3 = sub.add_parser("all")
    p3.add_argument("--sleep", type=float, default=2.0)
    p3.add_argument("--limit", type=int, default=0)
    p3.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    if args.cmd == "candidates":
        build_candidates()
    elif args.cmd == "screen":
        run_screen(args.sleep, args.limit, args.resume)
    elif args.cmd == "all":
        build_candidates()
        run_screen(args.sleep, args.limit, args.resume)


if __name__ == "__main__":
    main()
