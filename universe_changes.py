# -*- coding: utf-8 -*-
"""
10年保有ランキングの母集団の「入れ替え記録」（universe_changes_long.json）を更新する。
universe-long-quarterly.yml / monthly-check-long-us.yml が、安全確認（universe_guard.py）を通った直後・
コミット前に呼ぶ。rank_long.py がこのファイルを読み、追加された銘柄の「NEW!」バッジと「今回の入れ替え」
一覧（追加／外れた銘柄）を、次の四半期の見直し予定日まで表示する。

  python universe_changes.py quarterly   # 四半期の見直し：日本株・米国株とも期間をリセットして記録
  python universe_changes.py monthly     # 月次チェック：米国株の今の期間に追記

表示期間の考え方（案A）:
  ・四半期の見直しのたびに「期間」を新しく始める。期間の表示期限（display_until）は
    次の四半期の見直し予定日（1/4/7/10月の5日）。前の期間のNEW!・外れた銘柄の記録は消える。
  ・米国株の月次チェックで入れ替わった銘柄は、今の期間に追記され、同じ期限まで表示される。
  ・月次チェックで「外れた」とされた銘柄は、母集団には四半期の見直しまで残るため、母集団の差分ではなく
    universe_long_watch_us.json の excluded の増分から検出する。

直前のコミット（git HEAD）の母集団・監視ファイルと、作り直した現在のファイルを比べて差分を記録する。
"""
import argparse
import datetime as dt
import json
import os
import sys

import universe_guard as G

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "universe_changes_long.json")
WATCH = "universe_long_watch_us.json"
JST = dt.timezone(dt.timedelta(hours=9))


def next_quarterly(d):
    """dより後の、次の四半期見直し予定日（1/4/7/10月の5日）。"""
    for y in (d.year, d.year + 1):
        for m in (1, 4, 7, 10):
            c = dt.date(y, m, 5)
            if c > d:
                return c
    raise AssertionError("unreachable")


def _items(doc):
    return G._codes(doc) if doc else {}


def diff_universe(market):
    """母集団ファイルの追加・除外（git HEAD との差分）。"""
    path = G.FILES[market]
    new = _items(json.load(open(os.path.join(HERE, path), encoding="utf-8")))
    old_doc = G._head(path)
    old = _items(old_doc)
    added = [{"code": c, "name": new[c]} for c in sorted(set(new) - set(old))]
    removed = [{"code": c, "name": old[c]} for c in sorted(set(old) - set(new))]
    return added, removed


def diff_watch_excluded():
    """月次チェックが新たに「外れた」と記録した銘柄（監視ファイルのexcludedの増分）。"""
    cur_p = os.path.join(HERE, WATCH)
    cur = json.load(open(cur_p, encoding="utf-8")).get("excluded", {}) if os.path.isfile(cur_p) else {}
    old_doc = G._head(WATCH)
    old = (old_doc or {}).get("excluded", {}) or {}
    names = G._codes(json.load(open(os.path.join(HERE, G.FILES["us"]), encoding="utf-8")))
    out = []
    for code in sorted(set(cur) - set(old)):
        info = cur[code] or {}
        out.append({"code": code, "name": names.get(code) or info.get("name") or "",
                    "reason": info.get("reason") or ""})
    return out


def load():
    if os.path.isfile(OUT):
        try:
            return json.load(open(OUT, encoding="utf-8"))
        except Exception:
            pass
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["quarterly", "monthly"])
    ap.add_argument("--date", default=None, help="記録する日付（YYYY-MM-DD。省略時は日本時間の今日）")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(JST).date()
    until = next_quarterly(today).isoformat()
    data = load()

    if args.kind == "quarterly":
        for market in ("jp", "us"):
            added, removed = diff_universe(market)
            events = []
            if added or removed:
                events.append({"date": today.isoformat(), "kind": "quarterly", "added": added, "removed": removed})
            data[market] = {"period_start": today.isoformat(), "display_until": until, "events": events}
            print("%s 四半期：追加%d／除外%d → 表示期限 %s" % (market, len(added), len(removed), until))
    else:
        added, _unused = diff_universe("us")        # 月次は追加のみ母集団に反映される
        removed = diff_watch_excluded()             # 外れた銘柄は監視ファイルから検出
        blk = data.get("us") or {"period_start": today.isoformat(), "display_until": until, "events": []}
        if added or removed:
            blk["events"].append({"date": today.isoformat(), "kind": "monthly", "added": added, "removed": removed})
        data["us"] = blk
        print("us 月次：追加%d／外れた%d（期限 %s）" % (len(added), len(removed), blk["display_until"]))

    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
