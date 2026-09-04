# -*- coding: utf-8 -*-
"""
JPX の上場銘柄一覧から、公開版が診断できる配当株ユニバース（universe.json）を作る。

  # JPX から東証上場銘柄一覧を落とす（月末更新）:
  #   https://www.jpx.co.jp/markets/statistics-equities/misc/01.html
  # 2026-09時点は data_j.xlsx（openpyxlでそのまま読める。直接URLは
  #   .../misc/tvdivq0000001vg2-att/data_j.xlsx で curl 可・universe-quarterly.yml が自動取得）。
  # 旧形式の .xls を渡された場合はこの環境では直読みできないので、Excel で開いて
  # CSV(UTF-8) か .xlsx で保存し直す。

  python build_universe.py candidates --jpx data_j.xlsx    # または data_j.csv
      → universe_candidates.json（市場区分＋ファンド除外だけ。ネットワーク不要）

  python build_universe.py screen --sleep 1.5
      → universe.json（利回り・時価総額・減配なし の数値スクリーンを yfinance で適用）
        candidates を1件ずつ history 1回で軽く判定。--limit / --resume 対応。

  python build_universe.py all --jpx data_j.csv --sleep 1.5     # 上2つを続けて

スクリーンの数値は universe_screen.json（利回り下限・時価総額下限・減配ルール）。
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import analyze

HERE = os.path.dirname(__file__)
CAND = os.path.join(HERE, "universe_candidates.json")
UNIV = os.path.join(HERE, "universe.json")
SCREEN = os.path.join(HERE, "universe_screen.json")


# ---------------------------------------------------------------- JPX一覧
JPX_COLS = {  # data_j の列名（表記ゆれに一応対応）
    "code": ["コード", "Code", "銘柄コード"],
    "name": ["銘柄名", "Name", "銘柄名称"],
    "market": ["市場・商品区分", "Section/Products", "市場区分"],
    "sector33": ["33業種区分", "33 Sector(name)", "業種"],
}


def _pick(cols, cands):
    for c in cands:
        if c in cols:
            return c
    return None


def normalize_code(raw):
    """JPX の『コード』は通常4桁。内部コードで5桁（末尾0）になっている
    エクスポートもあるので 4桁に寄せる。将来の英数字4桁はそのまま。"""
    s = str(raw).strip()
    if s.isdigit() and len(s) == 5 and s.endswith("0"):
        return s[:4]
    if s.isdigit() and len(s) > 4:
        return s[:4]
    return s


def read_jpx(path):
    import pandas as pd
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        for enc in ("utf-8-sig", "cp932", "utf-8"):
            try:
                df = pd.read_csv(path, dtype=str, encoding=enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                df = None
        if df is None:
            sys.exit("CSV の文字コードを判定できませんでした。UTF-8 で保存し直してください。")
    elif ext == ".xlsx":
        df = pd.read_excel(path, dtype=str, engine="openpyxl")
    elif ext == ".xls":
        try:
            df = pd.read_excel(path, dtype=str)
        except Exception:
            sys.exit("この環境では .xls を直接読めません。Excel で開いて "
                     "CSV(UTF-8) か .xlsx で保存し直してから渡してください。")
    else:
        sys.exit(f"未対応の拡張子: {ext}（.csv / .xlsx / .xls）")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def build_candidates(jpx_path, markets):
    df = read_jpx(jpx_path)
    cols = list(df.columns)
    c_code = _pick(cols, JPX_COLS["code"])
    c_name = _pick(cols, JPX_COLS["name"])
    c_mkt = _pick(cols, JPX_COLS["market"])
    c_sec = _pick(cols, JPX_COLS["sector33"])
    if not all([c_code, c_name, c_mkt, c_sec]):
        sys.exit(f"必要な列が見つかりません。検出した列: {cols}")

    out = []
    for _, r in df.iterrows():
        code = normalize_code(r[c_code])
        mkt = str(r[c_mkt]).strip()
        sec = str(r[c_sec]).strip()
        if not code or code.lower() == "nan":
            continue
        # 内国株式のプライム/スタンダードだけ。ETF・REIT・インフラF・PRO・外国株は除外
        if "内国株式" not in mkt:
            continue
        if not any(mkt.startswith(m) for m in markets):
            continue
        if sec in ("-", "", "nan", "－"):
            continue
        out.append({"code": code, "name": str(r[c_name]).strip(),
                    "market": mkt, "sector": sec})
    j = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
         "source": os.path.basename(jpx_path), "markets": markets,
         "count": len(out), "candidates": out}
    json.dump(j, open(CAND, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"→ {CAND}  ({len(out)} 銘柄)")
    return j


# ---------------------------------------------------------------- 数値スクリーン
def prescreen_one(code, scr, policies):
    """history 1回で 利回り・時価総額・減配なし を判定。→ (ok:bool, info:dict)"""
    import yfinance as yf
    tk = yf.Ticker(f"{code}.T")
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
    mcap_oku = (mcap / 1e8) if mcap else None

    divs = []
    if "Dividends" in h:
        dser = h["Dividends"]
        dser = dser[dser > 0]
        divs = [(idx.to_pydatetime().date(), float(v)) for idx, v in dser.items()]
    cutoff = (dt.date.today() - dt.timedelta(days=365))
    ttm = sum(v for d, v in divs if d >= cutoff)
    yld = (ttm / price * 100) if price else None

    yf_fy = analyze.annual_dps_from_divs(divs)
    clean = analyze.clean_dps_series(yf_fy)
    vals = [v for _, v in clean]
    flat_streak = analyze._one_streak(vals, False) if vals else 0

    info = {"price": round(price, 1), "yield": round(yld, 2) if yld else None,
            "mcap_oku": round(mcap_oku) if mcap_oku else None,
            "nocut_years": flat_streak}

    if yld is None or yld < scr["min_dividend_yield_pct"]:
        info["reason"] = f"利回り {info['yield']}% < {scr['min_dividend_yield_pct']}%"
        return False, info
    if mcap_oku is None or mcap_oku < scr["min_market_cap_oku"]:
        info["reason"] = f"時価総額 {info['mcap_oku']}億 < {scr['min_market_cap_oku']}億"
        return False, info
    if str(code) not in policies and flat_streak < scr["no_cut_years"]:
        info["reason"] = f"非減配 {flat_streak}年 < {scr['no_cut_years']}年"
        return False, info
    return True, info


def run_screen(sleep, limit, resume):
    if not os.path.isfile(CAND):
        sys.exit(f"{CAND} がありません。先に `candidates` を実行してください。")
    scr = json.load(open(SCREEN, encoding="utf-8"))
    cand = json.load(open(CAND, encoding="utf-8"))["candidates"]
    try:
        policies = analyze.load_json("dividend_policy.json").get("policies", {})
    except Exception:
        policies = {}

    prev_pass, prev_rej = {}, {}
    if resume and os.path.isfile(UNIV):
        j = json.load(open(UNIV, encoding="utf-8"))
        prev_pass = {c["code"]: c for c in j.get("codes", [])}
        prev_rej = {c["code"]: c for c in j.get("rejected", [])}

    if limit:
        cand = cand[:limit]
    print(f"数値スクリーン {len(cand)} 銘柄  sleep={sleep}s")

    passed, rejected = [], []
    for i, c in enumerate(cand, 1):
        code = c["code"]
        if resume and (code in prev_pass or code in prev_rej):
            (passed if code in prev_pass else rejected).append(
                prev_pass.get(code) or prev_rej.get(code))
            continue
        try:
            ok, info = prescreen_one(code, scr, policies)
        except Exception as e:
            ok, info = False, {"reason": f"例外:{e}"}
        rec = {"code": code, "name": c["name"], "sector": c["sector"], **info}
        if ok:
            passed.append({"code": code, "name": c["name"]})
            print(f"  ✓ {code} {c['name']}  利回り{info.get('yield')}% {info.get('mcap_oku')}億")
        else:
            rejected.append(rec)
            if i % 50 == 0 or ok:
                print(f"  [{i}/{len(cand)}] ✗ {code} {info.get('reason')}")
        if i < len(cand):
            time.sleep(sleep)

    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
           "screen": scr, "count": len(passed),
           "codes": passed, "rejected": rejected}
    json.dump(out, open(UNIV, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {UNIV}  通過 {len(passed)} / 除外 {len(rejected)}")


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("candidates")
    p1.add_argument("--jpx", required=True, help="data_j.csv / .xlsx")
    p2 = sub.add_parser("screen")
    p2.add_argument("--sleep", type=float, default=1.5)
    p2.add_argument("--limit", type=int, default=0)
    p2.add_argument("--resume", action="store_true", help="既存 universe.json の判定を引き継ぐ")
    p3 = sub.add_parser("all")
    p3.add_argument("--jpx", required=True)
    p3.add_argument("--sleep", type=float, default=1.5)
    p3.add_argument("--limit", type=int, default=0)
    p3.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    scr = json.load(open(SCREEN, encoding="utf-8"))
    markets = scr.get("markets", ["プライム", "スタンダード"])

    if args.cmd == "candidates":
        build_candidates(args.jpx, markets)
    elif args.cmd == "screen":
        run_screen(args.sleep, args.limit, args.resume)
    elif args.cmd == "all":
        build_candidates(args.jpx, markets)
        run_screen(args.sleep, args.limit, args.resume)


if __name__ == "__main__":
    main()
