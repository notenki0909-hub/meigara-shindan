# -*- coding: utf-8 -*-
"""
TDnet（適時開示情報）の開示一覧を見て、ユニバース内銘柄の「決算短信」を検知し
pending_earnings.json に積む。実際の再取得・解決判定は earnings_retry.py が行う
（この2本で「決算翌日反映」の検知〜反映を分担する）。
あわせて「配当修正」（配当予想の修正・増配・減配など。決算短信と別日に出るものがある）を
tdnet_div_detections.json に記録する（配当ページの取り直しの合図。yj_div_cache.py が読む）。
直前の平日分も毎回取り直す（実行が遅れた・飛んだときの取りこぼしを拾う。同じ開示は二重に登録しない）。

  python tdnet_watch.py                   # 直前の平日＋今日（JST）をポーリング
  python tdnet_watch.py --date 20260805   # 過去日を指定（テスト・取りこぼし確認用）

想定運用：平日 11:00〜17:00 JST の間、30分おきに実行（GitHub Actions cron。未実装）。
データ源：TDnet本家(release.tdnet.info)は一覧がJS依存でパースしづらいため、
第三者ミラー https://webapi.yanoshin.jp/webapi/tdnet/list/YYYYMMDD.json を使う
（無料・非公式・レスポンスは実データで確認済み。落ちていても致命的ではない設計＝
今回逃しても次回のポーリングか週次フル再構築が拾う）。
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.request

from build_universe import normalize_code

HERE = os.path.dirname(__file__)
UNIV = os.path.join(HERE, "universe.json")
PENDING = os.path.join(HERE, "pending_earnings.json")
DETECT_LOG = os.path.join(HERE, "tdnet_detections.json")
DIV_LOG = os.path.join(HERE, "tdnet_div_detections.json")   # 配当修正の開示の記録（決算短信とは別。yj_div_cache.py が読む）

JST = dt.timezone(dt.timedelta(hours=9))
# ミラーは既定だと1日300件（最新順）で打ち切る。決算ピーク日は1000件超なので limit を付けて全件取る。
TDNET_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{date}.json?limit=10000"

MAX_DETECT_LOG = 500


def today_jst():
    return dt.datetime.now(JST).date()


def prev_weekday(d):
    """直前の平日（前日分の取り直し用。週末・祝日は開示が空なので害はない）。"""
    d = d - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def is_earnings_disclosure(title):
    """『決算短信』を含むものだけを決算開示とみなす。決算説明資料・決算補足資料・
    業績予想の修正はタイトルにこの語を含まないので自然に除外される。訂正
    （『…決算短信…』の一部訂正について）は数値が変わり得るのであえて拾う。"""
    return "決算短信" in title


_DIV_REVISION_WORDS = ("配当予想", "増配", "減配", "復配", "無配", "記念配当", "特別配当")


def is_dividend_revision(title):
    """配当額に関わる開示（配当予想の修正・増配・減配・復配・無配・記念配当・特別配当。訂正も含む）。
    決算短信は earnings 側で拾うので除く。『剰余金の配当（中間配当）に関するお知らせ』のような
    配当の決定だけの開示は対象外（額は予想どおりのことが多く、違えば予想修正の開示が同時に出る）。"""
    return "決算短信" not in title and any(w in title for w in _DIV_REVISION_WORDS)


def fetch_tdnet_day(date):
    """date: 'YYYYMMDD' 文字列。→ [{'code','name','title','pubdate','doc_url'}, ...]
    失敗したら空リスト（呼び出し側で『次回に賭ける』前提）。"""
    url = TDNET_URL.format(date=date)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                body = gzip.decompress(body)
            j = json.loads(body.decode("utf-8"))
    except Exception as e:
        print(f"  ! TDnet取得失敗（{e}）→ 今回はスキップ", file=sys.stderr)
        return []
    out = []
    for it in j.get("items", []):
        t = it.get("Tdnet") or it    # 日によって {"Tdnet": {...}} でなく平たい形で返ることがある（実測：2026-11-04）
        code = normalize_code(t.get("company_code", ""))
        if not code:
            continue
        out.append({
            "code": code, "name": t.get("company_name", "").strip(),
            "title": t.get("title", ""), "pubdate": t.get("pubdate", ""),
            "doc_url": t.get("document_url", ""),
        })
    return out


def load_universe_codes():
    if not os.path.isfile(UNIV):
        sys.exit(f"{UNIV} がありません。先に build_universe.py でユニバースを作ってください。")
    j = json.load(open(UNIV, encoding="utf-8"))
    uni = {c["code"]: c["name"] for c in j.get("codes", [])}
    try:   # 日株講座Gp（kouza_pins.json）の銘柄も、母集団に入っていなくても決算短信を検知する
        import kouza
        for c, n in kouza.pinned_pairs():
            uni.setdefault(c, n or "")
    except Exception:
        pass
    return uni


def load_json_or(path, default):
    if os.path.isfile(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return default


def poll_once(date_str, uni=None):
    uni = uni or load_universe_codes()
    items = fetch_tdnet_day(date_str)
    pending = load_json_or(PENDING, {})
    detect_log = load_json_or(DETECT_LOG, [])
    div_log = load_json_or(DIV_LOG, [])
    div_seen = {(r.get("code"), r.get("pubdate"), r.get("title")) for r in div_log}

    now = dt.datetime.now(JST)
    new_count, skip_tracked, off_universe, div_new = 0, 0, 0, 0
    for it in items:
        code = it["code"]
        if code not in uni:
            off_universe += 1
            continue
        if is_dividend_revision(it["title"]):
            key = (code, it["pubdate"], it["title"])
            if key not in div_seen:        # 前日分の取り直しや30分おきの再取得で、同じ開示を二重に記録しない
                div_seen.add(key)
                div_log.append({"code": code, "name": (uni.get(code) or it["name"]),
                                "detected_at": now.isoformat(timespec="seconds"),
                                "pubdate": it["pubdate"], "title": it["title"]})
                div_new += 1
                print(f"  ✓ 配当修正 {code} {uni.get(code, it['name'])}  {it['title'][:60]}")
            continue
        if not is_earnings_disclosure(it["title"]):
            continue
        existing = pending.get(code)
        if existing and (existing.get("status") == "pending" or
                         (existing.get("pubdate") == it["pubdate"] and existing.get("title") == it["title"])):
            skip_tracked += 1
            continue  # 既に監視中（同日に複数開示があっても最初の1件で足りる）／同じ開示は解決済みでも再登録しない（前日分の取り直し対策）
        pending[code] = {
            "code": code, "name": (uni.get(code) or it["name"]),
            "detected_at": now.isoformat(timespec="seconds"),
            "pubdate": it["pubdate"], "title": it["title"], "doc_url": it["doc_url"],
            "status": "pending", "attempts": 0,
            "next_retry_at": (now + dt.timedelta(hours=6)).isoformat(timespec="seconds"),
        }
        detect_log.append({"code": code, "name": (uni.get(code) or it["name"]),
                           "detected_at": now.isoformat(timespec="seconds"),
                           "title": it["title"]})
        new_count += 1
        print(f"  ✓ 検知 {code} {uni.get(code, it['name'])}  {it['title']}")

    detect_log = detect_log[-MAX_DETECT_LOG:]
    json.dump(pending, open(PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(detect_log, open(DETECT_LOG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    div_log = div_log[-MAX_DETECT_LOG:]
    json.dump(div_log, open(DIV_LOG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    n_pending = sum(1 for e in pending.values() if e["status"] == "pending")
    print(f"[{date_str}] TDnet開示 {len(items)}件 → 決算短信 新規検知 {new_count}件"
          f"（追跡中スキップ{skip_tracked}・ユニバース外{off_universe}）・配当修正 新規{div_new}件"
          f"　監視中合計 {n_pending}件")
    return new_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="YYYYMMDD（省略時は直前の平日＋今日・JST。定時実行が遅れた・飛んだときの取りこぼしを翌日に拾うため）")
    args = ap.parse_args()
    if args.date:
        poll_once(args.date)
        return
    today = today_jst()
    uni = load_universe_codes()
    poll_once(prev_weekday(today).strftime("%Y%m%d"), uni)   # 前日分の取り直し（既に検知済みの開示は二重に登録されない）
    poll_once(today.strftime("%Y%m%d"), uni)


if __name__ == "__main__":
    main()
