# -*- coding: utf-8 -*-
"""
TDnet（適時開示情報）の当日開示一覧を見て、ユニバース内銘柄の「決算短信」を検知し
pending_earnings.json に積む。実際の再取得・解決判定は earnings_retry.py が行う
（この2本で「決算翌日反映」の検知〜反映を分担する）。

  python tdnet_watch.py                   # 今日（JST）ぶんをポーリング
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

JST = dt.timezone(dt.timedelta(hours=9))
TDNET_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{date}.json"

MAX_DETECT_LOG = 500


def today_jst():
    return dt.datetime.now(JST).date()


def is_earnings_disclosure(title):
    """『決算短信』を含むものだけを決算開示とみなす。決算説明資料・決算補足資料・
    業績予想の修正はタイトルにこの語を含まないので自然に除外される。訂正
    （『…決算短信…』の一部訂正について）は数値が変わり得るのであえて拾う。"""
    return "決算短信" in title


def fetch_tdnet_day(date):
    """date: 'YYYYMMDD' 文字列。→ [{'code','name','title','pubdate','doc_url'}, ...]
    失敗したら空リスト（呼び出し側で『次回に賭ける』前提）。"""
    url = TDNET_URL.format(date=date)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  ! TDnet取得失敗（{e}）→ 今回はスキップ", file=sys.stderr)
        return []
    out = []
    for it in j.get("items", []):
        t = it.get("Tdnet", {})
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
    return {c["code"]: c["name"] for c in j.get("codes", [])}


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

    now = dt.datetime.now(JST)
    new_count, skip_tracked, off_universe = 0, 0, 0
    for it in items:
        code = it["code"]
        if code not in uni:
            off_universe += 1
            continue
        if not is_earnings_disclosure(it["title"]):
            continue
        existing = pending.get(code)
        if existing and existing.get("status") == "pending":
            skip_tracked += 1
            continue  # 既に監視中（同日に複数開示があっても最初の1件で足りる）
        pending[code] = {
            "code": code, "name": uni.get(code, it["name"]),
            "detected_at": now.isoformat(timespec="seconds"),
            "pubdate": it["pubdate"], "title": it["title"], "doc_url": it["doc_url"],
            "status": "pending", "attempts": 0,
            "next_retry_at": (now + dt.timedelta(hours=6)).isoformat(timespec="seconds"),
        }
        detect_log.append({"code": code, "name": uni.get(code, it["name"]),
                           "detected_at": now.isoformat(timespec="seconds"),
                           "title": it["title"]})
        new_count += 1
        print(f"  ✓ 検知 {code} {uni.get(code, it['name'])}  {it['title']}")

    detect_log = detect_log[-MAX_DETECT_LOG:]
    json.dump(pending, open(PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(detect_log, open(DETECT_LOG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    n_pending = sum(1 for e in pending.values() if e["status"] == "pending")
    print(f"[{date_str}] TDnet開示 {len(items)}件 → 決算短信 新規検知 {new_count}件"
          f"（追跡中スキップ{skip_tracked}・ユニバース外{off_universe}）"
          f"　監視中合計 {n_pending}件")
    return new_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYYMMDD（省略時は今日・JST）")
    args = ap.parse_args()
    date_str = args.date or today_jst().strftime("%Y%m%d")
    poll_once(date_str)


if __name__ == "__main__":
    main()
