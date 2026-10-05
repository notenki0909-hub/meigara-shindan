# -*- coding: utf-8 -*-
"""
米国10年保有ランキングの「ETF別 上位15銘柄」用データを各運用会社の公開データから取得し、
etf_top_us.json に書き出す（rank_long.py us が読む）。

  python fetch_etf_top_us.py

取得元（いずれも公開の構成銘柄データ。組入比率の降順で上位15件）:
  XL系・SPY … State Street（SSGA）の日次構成銘柄 xlsx
  SOXX      … iShares の latest-holdings.csv
  QQQ       … Invesco の構成銘柄 API
ファイル保存はせず、取得内容はメモリ上で処理する。取得に失敗したETFは前回の内容を残す。
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.request

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "etf_top_us.json")
TOP_N = 15
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# (ETF, 日本語ラベル, 提供元)
ETFS = [
    ("XLK", "情報技術", "State Street"),
    ("XLF", "金融", "State Street"),
    ("XLI", "資本財・サービス", "State Street"),
    ("XLB", "素材", "State Street"),
    ("XLY", "一般消費財・サービス", "State Street"),
    ("XLE", "エネルギー", "State Street"),
    ("XLC", "コミュニケーション・サービス", "State Street"),
    ("XLV", "ヘルスケア", "State Street"),
    ("XLP", "生活必需品", "State Street"),
    ("XLU", "公益事業", "State Street"),
    ("XLRE", "不動産", "State Street"),
    ("SOXX", "半導体（iShares）", "iShares"),
    ("SPY", "S&P500", "State Street"),
    ("QQQ", "Nasdaq-100", "Invesco"),
]


def _get(url, accept=None):
    h = dict(UA)
    if accept:
        h["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=90).read()


def norm(t):
    return str(t).strip().upper().replace(".", "-").replace("/", "-").replace(" ", "")


def _top(items):
    items = [x for x in items if x["weight"] is not None]
    items.sort(key=lambda x: -x["weight"])
    return items[:TOP_N]


def fetch_ssga(etf):
    url = ("https://www.ssga.com/library-content/products/fund-data/etfs/us/"
           "holdings-daily-us-en-%s.xlsx" % etf.lower())
    rows = list(openpyxl.load_workbook(io.BytesIO(_get(url)), data_only=True).active.iter_rows(values_only=True))
    as_of = ""
    for r in rows[:8]:
        if r and r[0] == "Holdings:" and r[1]:
            as_of = dt.datetime.strptime(str(r[1]).replace("As of ", "").strip(), "%d-%b-%Y").date().isoformat()
    hi = next(i for i, r in enumerate(rows) if r and r[0] == "Name" and "Ticker" in [str(c) for c in r])
    hdr = [str(c) for c in rows[hi]]
    ti, wi = hdr.index("Ticker"), hdr.index("Weight")
    items = []
    for r in rows[hi + 1:]:
        if r and r[0] and r[ti] and str(r[ti]).strip() not in ("-", "") and isinstance(r[wi], (int, float)):
            items.append({"ticker": norm(r[ti]), "name": str(r[0]).strip(), "weight": float(r[wi])})
    return as_of, _top(items)


def fetch_soxx():
    raw = _get("https://www.ishares.com/us/products/239705/ishares-semiconductor-etf/latest-holdings.csv").decode("utf-8-sig")
    lines = raw.splitlines()
    as_of = dt.datetime.strptime(lines[1].split(",", 1)[1].strip('"'), "%b %d, %Y").date().isoformat()
    hi = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    items = []
    for r in csv.DictReader(lines[hi:]):
        if r.get("Asset Class") == "Equity" and r.get("Ticker") not in ("-", ""):
            items.append({"ticker": norm(r["Ticker"]), "name": r["Name"].strip(),
                          "weight": float(r["Weight (%)"].replace(",", ""))})
    return as_of, _top(items)


def fetch_qqq():
    d = json.loads(_get("https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund"
                        "?idType=ticker&productType=ETF", "application/json"))
    items = [{"ticker": norm(h["ticker"]), "name": str(h.get("issuerName") or "").strip(),
              "weight": float(h["percentageOfTotalNetAssets"])}
             for h in d.get("holdings", []) if h.get("ticker") and h.get("percentageOfTotalNetAssets") is not None]
    return d.get("effectiveBusinessDate") or d.get("effectiveDate") or "", _top(items)


def main():
    old = {}
    if os.path.isfile(OUT):
        try:
            old = {e["etf"]: e for e in json.load(open(OUT, encoding="utf-8")).get("etfs", [])}
        except Exception:
            old = {}
    res, fails = [], []
    for etf, label, provider in ETFS:
        try:
            if etf == "SOXX":
                as_of, hs = fetch_soxx()
            elif etf == "QQQ":
                as_of, hs = fetch_qqq()
            else:
                as_of, hs = fetch_ssga(etf)
            if len(hs) < TOP_N:
                raise ValueError("holdings too few: %d" % len(hs))
            res.append({"etf": etf, "label": label, "provider": provider, "as_of": as_of, "holdings": hs})
            print("ok  %-5s %s  top1=%s %.2f%%" % (etf, as_of, hs[0]["ticker"], hs[0]["weight"]))
        except Exception as ex:
            fails.append(etf)
            print("FAIL %-5s %s: %s" % (etf, type(ex).__name__, ex), file=sys.stderr)
            if etf in old:
                res.append(old[etf])
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "top_n": TOP_N, "etfs": res}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("wrote", OUT, "etfs:", len(res), "failed:", fails)
    if len(res) < len(ETFS):
        sys.exit(1)


if __name__ == "__main__":
    main()
