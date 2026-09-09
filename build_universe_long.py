# -*- coding: utf-8 -*-
"""
『10年保有できる優良企業』ランキングの母集団（universe_long.json / universe_long_us.json）を作る。

配当株版（build_universe.py）とは独立。既存ファイルは一切書き換えない。

  python build_universe_long.py jp      # universe.json の取得済みデータを流用 → universe_long.json
  python build_universe_long.py us      # universe_candidates_us.json の index タグ → universe_long_us.json

方針:
- JP: universe.json（配当株スクリーンの副産物）に time_cap/sector が全銘柄分そろっている。
      codes は時価総額をサマリ(site/summaries/<code>.json)から、rejected は mcap_oku から取る。
      → 時価総額 >= min_market_cap_oku かつ 金融/REIT 以外を通す。ネットワークは
      サマリが無い通過銘柄の時だけ fast_info を1回だけ叩く（通常ほぼ0件）。
- US: universe_candidates_us.json（S&P500/400/600 を Wikipedia から取得済み）の
      index 配列に "S&P500" を含む銘柄をそのまま母集団にする。数値スクリーンなし。
      金融/REIT は母集団に残し、rank_long_us.py の採点段階で振り分ける。

四半期の universe-long-quarterly.yml が、universe.json / universe_candidates_us.json を
最新化した直後にこれを走らせて universe_long*.json を作り直す。
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _summary_mcap_oku(code, subdir=""):
    """site/[subdir/]summaries/<code>.json の mcap(円) を 億円 で返す。無ければ None。"""
    for base in (os.path.join(SITE, subdir, "summaries"),
                 os.path.join(SITE, subdir, "long_summaries")):
        p = os.path.join(base, f"{code}.json")
        if os.path.isfile(p):
            try:
                m = _load(p).get("mcap")
                if isinstance(m, (int, float)) and m > 0:
                    return m / 1e8
            except Exception:
                pass
    return None


# ---------------------------------------------------------------- JP
def build_jp():
    scr = _load(os.path.join(HERE, "universe_screen_long.json"))
    univ = _load(os.path.join(HERE, "universe.json"))
    floor = scr["min_market_cap_oku"]
    ex_sec = set(scr.get("exclude_sectors", []))

    # rejected: mcap_oku と sector を持っている（配当スクリーンの副産物）
    rej_info = {r["code"]: r for r in univ.get("rejected", [])}
    # codes の sector は universe_candidates.json から拾う（あれば）
    cand_sec = {}
    cpath = os.path.join(HERE, "universe_candidates.json")
    if os.path.isfile(cpath):
        for c in _load(cpath).get("candidates", []):
            cand_sec[c["code"]] = c.get("sector")

    passed, excluded = [], []
    seen = set()

    def consider(code, name, sector, mcap_oku, mcap_src):
        if code in seen:
            return
        seen.add(code)
        rec = {"code": code, "name": name, "sector": sector,
               "mcap_oku": round(mcap_oku) if isinstance(mcap_oku, (int, float)) else None,
               "mcap_src": mcap_src}
        if sector in ex_sec:
            rec["reason"] = f"金融は対象外（{sector}）"
            excluded.append(rec)
            return
        if not isinstance(mcap_oku, (int, float)):
            rec["reason"] = "時価総額不明"
            excluded.append(rec)
            return
        if mcap_oku < floor:
            rec["reason"] = f"時価総額 {rec['mcap_oku']}億 < {floor}億"
            excluded.append(rec)
            return
        passed.append({"code": code, "name": name})

    # 1) 配当株ユニバースの通過銘柄（時価総額はサマリから）
    for c in univ.get("codes", []):
        code, name = c["code"], c["name"]
        sec = None
        sp = os.path.join(SITE, "summaries", f"{code}.json")
        if os.path.isfile(sp):
            try:
                sec = _load(sp).get("jp_sector")
            except Exception:
                sec = None
        sec = sec or cand_sec.get(code)
        m = _summary_mcap_oku(code)
        src = "summary"
        if m is None:
            m = _fast_info_mcap_oku_jp(code)
            src = "fast_info"
        consider(code, name, sec, m, src)

    # 2) 配当株スクリーンで落ちた銘柄（mcap_oku は取得済み）
    for code, r in rej_info.items():
        consider(code, r.get("name"), r.get("sector") or cand_sec.get(code),
                 r.get("mcap_oku"), "rejected_cache")

    passed.sort(key=lambda x: x["code"])
    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "market": "jp",
        "screen": scr,
        "source": "universe.json (codes + rejected) の取得済みデータを流用",
        "count": len(passed),
        "codes": passed,
        "excluded_count": len(excluded),
        "excluded": sorted(excluded, key=lambda x: x["code"]),
    }
    outp = os.path.join(HERE, "universe_long.json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"→ {outp}  通過 {len(passed)} / 除外 {len(excluded)}")
    _reason_hist(excluded)


def _fast_info_mcap_oku_jp(code):
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        fi = yf.Ticker(f"{code}.T").fast_info
        m = fi.get("marketCap") or fi.get("market_cap")
        return (m / 1e8) if m else None
    except Exception:
        return None


# ---------------------------------------------------------------- US
def build_us():
    scr = _load(os.path.join(HERE, "universe_screen_long_us.json"))
    member = scr["index_membership"]
    cpath = os.path.join(HERE, "universe_candidates_us.json")
    if not os.path.isfile(cpath):
        sys.exit(f"{cpath} がありません。先に `python build_universe_us.py candidates --jpx ...` "
                 "（Wikipedia取得）を実行してください。")
    cand = _load(cpath).get("candidates", [])

    passed = []
    for c in cand:
        if member in (c.get("index") or []):
            passed.append({"ticker": c["ticker"], "name": c.get("name"),
                           "gics_sector": c.get("gics_sector")})
    passed.sort(key=lambda x: x["ticker"])
    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "market": "us",
        "screen": scr,
        "source": f"universe_candidates_us.json の index に {member} を含む銘柄",
        "count": len(passed),
        "tickers": passed,
    }
    outp = os.path.join(HERE, "universe_long_us.json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"→ {outp}  {len(passed)} 銘柄（{member} メンバーシップ）")
    from collections import Counter
    print("  業種内訳:", dict(Counter(p["gics_sector"] for p in passed)))


# ---------------------------------------------------------------- utils
def _reason_hist(excluded):
    from collections import Counter
    c = Counter((e.get("reason") or "?").split(" ")[0].split("（")[0] for e in excluded)
    print("  除外理由:", dict(c))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["jp", "us"])
    args = ap.parse_args()
    if args.market == "jp":
        build_jp()
    else:
        build_us()


if __name__ == "__main__":
    main()
