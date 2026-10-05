# -*- coding: utf-8 -*-
"""
配当王・配当貴族の固定リスト（dividend_royalty_us.json）を公開情報から更新する。

  python update_royalty_us.py            # 取得して、変化があればJSONを書き換える
  python update_royalty_us.py --dry-run   # 書き換えずに差分だけ表示

取得元
  配当貴族：Wikipedia「S&P 500 Dividend Aristocrats」の表（ティッカー列・社名列）
  配当王  ：Sure Dividend「Dividend Kings List」ページ（「社名 (ティッカー)」のリンク一覧）

壊れた取得で古いリストを消さないよう、件数の範囲と前回リストとの重なりを検査し、
外れたら書き換えず終了コード1で止まる（GitHub Actionsの失敗通知で気づける）。
このリストは表示専用（rank_us.py の「配当王／配当貴族」一覧）で、スコア計算や対象銘柄の選定には使わない。
"""
import argparse
import datetime as dt
import html
import io
import json
import os
import re
import sys

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "dividend_royalty_us.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; meigara-shindan-us/1.0)"}
URL_ARI = "https://en.wikipedia.org/wiki/S%26P_500_Dividend_Aristocrats"
URL_KING = "https://www.suredividend.com/dividend-kings/"
RANGE = {"aristocrats": (50, 90), "kings": (45, 80)}   # 現在は69/60銘柄
MIN_OVERLAP = 0.7                                       # 前回リストとの重なり（小さい方に対する割合）


def norm(t):
    return re.sub(r"\.", "-", str(t).strip().upper())


def fetch_aristocrats():
    r = requests.get(URL_ARI, headers=UA, timeout=60)
    r.raise_for_status()
    for t in pd.read_html(io.StringIO(r.text)):
        if "Ticker symbol" in t.columns and "Company" in t.columns:
            return [{"ticker": norm(a), "name": str(b).strip()}
                    for a, b in zip(t["Ticker symbol"], t["Company"])]
    raise RuntimeError("Wikipediaの貴族一覧の表が見つからない")


def fetch_kings():
    r = requests.get(URL_KING, headers=UA, timeout=60)
    r.raise_for_status()
    pat = re.compile(r'<a href="[^"]*\.pdf">([^<]+?)\s*\(([A-Z0-9.\-]+)\)</a>')
    seen, out = set(), []
    for name, tk in pat.findall(r.text):
        tk = norm(tk)
        if tk in seen:
            continue
        seen.add(tk)
        out.append({"ticker": tk, "name": html.unescape(name).strip()})
    return out


def check(key, new, old):
    lo, hi = RANGE[key]
    if not (lo <= len(new) <= hi):
        raise RuntimeError(f"{key}: 件数{len(new)}が想定範囲{lo}〜{hi}の外")
    if old:
        a, b = {e["ticker"] for e in new}, {e["ticker"] for e in old}
        ov = len(a & b) / min(len(a), len(b))
        if ov < MIN_OVERLAP:
            raise RuntimeError(f"{key}: 前回リストとの重なりが{ov:.0%}で{MIN_OVERLAP:.0%}未満")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.load(open(PATH, encoding="utf-8"))
    new = {"aristocrats": fetch_aristocrats(), "kings": fetch_kings()}
    changed = False
    for key in ("aristocrats", "kings"):
        old = cfg.get(key) or []
        check(key, new[key], old)
        o, n = {e["ticker"] for e in old}, {e["ticker"] for e in new[key]}
        print(f"{key}: {len(old)} → {len(new[key])}銘柄  追加={sorted(n - o)}  除外={sorted(o - n)}")
        if n != o:
            changed = True
    if not changed:
        print("入れ替えなし（社名表記の差のみ・変化なし）→ 書き換えスキップ")
        return
    if args.dry_run:
        print("--dry-run：書き込まない")
        return
    cfg["aristocrats"], cfg["kings"] = new["aristocrats"], new["kings"]
    cfg["取得日"] = dt.date.today().isoformat()
    json.dump(cfg, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("→ " + PATH)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"::error::配当王・貴族リストの更新に失敗: {e}", file=sys.stderr)
        sys.exit(1)
