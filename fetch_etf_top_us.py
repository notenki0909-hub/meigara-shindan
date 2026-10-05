# -*- coding: utf-8 -*-
"""
米国10年保有ランキングの「ETF別 上位15銘柄」用データを各運用会社の公開データから取得し、
etf_top_us.json に書き出す（rank_long.py us が読む）。月次ワークフロー monthly-etf-top-us.yml
から呼ばれる。手元でも実行できる。

  python fetch_etf_top_us.py

取得元（いずれも公開の構成銘柄データ。組入比率の降順で上位15件）:
  XL系・SPY … State Street（SSGA）の日次構成銘柄 xlsx（URLは2系統。片方が失敗したら予備を使う）
  SOXX      … iShares の latest-holdings.csv
  QQQ       … Invesco の構成銘柄 API
ファイル保存はせず、取得内容はメモリ上で処理する。

信頼性のための補強:
  ・一時的な失敗は最大3回まで待ってやり直す
  ・取得内容を検査する（15件以上／ティッカー形式／組入比率の範囲と合計／データ日付が古すぎない）
  ・yfinance（Yahooの上位10銘柄）と顔ぶれを突き合わせ、大きくずれたら異常として採用しない
    （Yahoo側は更新が遅れがちなので、一致は6/10以上を条件とし、Yahoo取得に失敗したら照合は省略）
  ・検査や取得に失敗したETFは前回の内容を残す（他のETFは更新する）。1つでも失敗したら終了コード1
  ・内容が前回と同じなら etf_top_us.json を書き換えない（無駄なコミットを作らない）
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.request

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "etf_top_us.json")
TOP_N = 15
MAX_AGE_DAYS = 10          # データ日付がこれより古ければ異常（休場の連続を考慮して余裕を持たせる）
MIN_YAHOO_OVERLAP = 6      # Yahoo上位10と取得結果の上位15の一致数の下限
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

SSGA_URLS = [
    "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-%s.xlsx",
    "https://www.ssga.com/us/en/individual/etfs/library-content/products/fund-data/etfs/us/holdings-daily-us-en-%s.xlsx",
]
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,8}$")


def _get(url, accept=None):
    h = dict(UA)
    if accept:
        h["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=90).read()


def _retry(fn, tries=3, wait=5):
    """一時的な失敗（通信エラー等）は待ってやり直す。最後の失敗はそのまま投げる。"""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as ex:  # noqa: BLE001
            last = ex
            if i < tries - 1:
                time.sleep(wait * (i + 1))
    raise last


def norm(t):
    return str(t).strip().upper().replace(".", "-").replace("/", "-").replace(" ", "")


def _top(items):
    items = [x for x in items if x["weight"] is not None]
    items.sort(key=lambda x: -x["weight"])
    return items[:TOP_N]


def _parse_ssga(content):
    rows = list(openpyxl.load_workbook(io.BytesIO(content), data_only=True).active.iter_rows(values_only=True))
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


def fetch_ssga(etf):
    errs = []
    for tmpl in SSGA_URLS:                      # 主URL→予備URLの順
        try:
            return _retry(lambda: _parse_ssga(_get(tmpl % etf.lower())))
        except Exception as ex:  # noqa: BLE001
            errs.append("%s: %s" % (tmpl.split("/")[3], ex))
    raise RuntimeError("SSGA 全URLで失敗 (%s)" % " / ".join(errs))


def fetch_soxx():
    def go():
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
    return _retry(go)


def fetch_qqq():
    def go():
        d = json.loads(_get("https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund"
                            "?idType=ticker&productType=ETF", "application/json"))
        items = [{"ticker": norm(h["ticker"]), "name": str(h.get("issuerName") or "").strip(),
                  "weight": float(h["percentageOfTotalNetAssets"])}
                 for h in d.get("holdings", []) if h.get("ticker") and h.get("percentageOfTotalNetAssets") is not None]
        return d.get("effectiveBusinessDate") or d.get("effectiveDate") or "", _top(items)
    return _retry(go)


def validate(etf, as_of, hs, today=None):
    """取得内容の検査。問題があれば ValueError（そのETFは前回の内容を残す）。"""
    today = today or dt.date.today()
    if len(hs) < TOP_N:
        raise ValueError("銘柄数が少ない(%d件)" % len(hs))
    for h in hs:
        if not TICKER_RE.match(h["ticker"]):
            raise ValueError("ティッカー形式が不正: %r" % h["ticker"])
        if not (0 < h["weight"] <= 50):
            raise ValueError("組入比率が範囲外: %s=%s" % (h["ticker"], h["weight"]))
    if len({h["ticker"] for h in hs}) != len(hs):
        raise ValueError("ティッカーが重複")
    total = sum(h["weight"] for h in hs)
    if not (10 <= total <= 100.5):
        raise ValueError("上位15の組入比率合計が不自然: %.1f%%" % total)
    try:
        d = dt.date.fromisoformat(as_of)
    except Exception:
        raise ValueError("データ日付が読み取れない: %r" % as_of)
    age = (today - d).days
    if age > MAX_AGE_DAYS or age < -1:
        raise ValueError("データ日付が古い/不正: %s（%d日前）" % (as_of, age))


def crosscheck_yahoo(etf, hs):
    """Yahooの上位10銘柄との顔ぶれ照合。大きくずれたら ValueError。Yahoo取得失敗時は照合を省略。"""
    try:
        import yfinance as yf
        top10 = [norm(t) for t in yf.Ticker(etf).funds_data.top_holdings.index][:10]
    except Exception as ex:  # noqa: BLE001
        print("  (Yahoo照合を省略: %s)" % type(ex).__name__)
        return None
    if len(top10) < 5:
        print("  (Yahoo照合を省略: 取得数 %d)" % len(top10))
        return None
    mine = {h["ticker"] for h in hs}
    overlap = sum(1 for t in top10 if t in mine)
    if overlap < MIN_YAHOO_OVERLAP:
        raise ValueError("Yahooの上位10との一致が少ない(%d/10)" % overlap)
    return overlap


def main():
    old_doc, old = {}, {}
    if os.path.isfile(OUT):
        try:
            old_doc = json.load(open(OUT, encoding="utf-8"))
            old = {e["etf"]: e for e in old_doc.get("etfs", [])}
        except Exception:
            old_doc, old = {}, {}
    res, fails = [], []
    for etf, label, provider in ETFS:
        try:
            if etf == "SOXX":
                as_of, hs = fetch_soxx()
            elif etf == "QQQ":
                as_of, hs = fetch_qqq()
            else:
                as_of, hs = fetch_ssga(etf)
            validate(etf, as_of, hs)
            ov = crosscheck_yahoo(etf, hs)
            res.append({"etf": etf, "label": label, "provider": provider, "as_of": as_of, "holdings": hs})
            print("ok   %-5s %s  top1=%s %.2f%%  yahoo一致=%s" % (
                etf, as_of, hs[0]["ticker"], hs[0]["weight"], "-" if ov is None else "%d/10" % ov))
        except Exception as ex:  # noqa: BLE001
            fails.append(etf)
            print("FAIL %-5s %s: %s（前回の内容を残します）" % (etf, type(ex).__name__, ex), file=sys.stderr)
            if etf in old:
                res.append(old[etf])
    if res != old_doc.get("etfs"):
        out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "top_n": TOP_N, "etfs": res}
        json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("更新しました:", OUT)
    else:
        print("前回から変更なし（ファイルは書き換えません）")
    if fails:
        print("失敗したETF:", ", ".join(fails), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
