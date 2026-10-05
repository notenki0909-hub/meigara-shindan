# -*- coding: utf-8 -*-
"""
10年保有ランキングの母集団（universe_long.json / universe_long_us.json）を自動で反映する前の安全確認。
universe-long-quarterly.yml と monthly-check-long-us.yml が、母集団を作り直した直後・コミット前に呼ぶ。

  python universe_guard.py jp|us|both [--force] [--out universe_change.md] [--msg universe_commit_msg.txt]

直前のコミット（git HEAD）の母集団と、作り直した母集団を比べ、次の条件をすべて満たすときだけ
終了コード0（＝自動反映してよい）にする。
  ・銘柄数が前回の90〜110％の範囲
  ・追加数・除外数がそれぞれ max(15, 前回の5％) 以下
  （Wikipediaの表の形式変更・取得失敗などで母集団が壊れたまま自動反映されるのを防ぐ。
    S&P500の通常の入れ替えは1回あたり数銘柄、日本株の四半期見直しも数十銘柄以内に収まる）
条件を外れたら終了コード1。--force を付けると警告だけ出して0を返す（人が内容を確認した上で反映したいとき）。
--out に追加・除外の一覧（Markdown。Issue・実行結果の要約に使う）、--msg にコミットメッセージを書き出す。
"""
import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = {"jp": "universe_long.json", "us": "universe_long_us.json"}
LABEL = {"jp": "日本株", "us": "米国株"}


def _codes(doc):
    items = doc.get("tickers") if doc.get("tickers") is not None else doc.get("codes")
    return {str(i.get("ticker") or i.get("code")).upper(): (i.get("name") or "") for i in (items or [])}


def _head(path):
    """git HEAD にあるファイルの内容（無ければ None）。"""
    r = subprocess.run(["git", "show", "HEAD:%s" % path], cwd=HERE, capture_output=True)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout.decode("utf-8"))


def check(market):
    path = FILES[market]
    new_doc = json.load(open(os.path.join(HERE, path), encoding="utf-8"))
    old_doc = _head(path)
    new, old = _codes(new_doc), (_codes(old_doc) if old_doc else {})
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    problems = []
    n_old, n_new = len(old), len(new)
    if n_new == 0:
        problems.append("銘柄数が0です")
    if n_old:
        if not (0.9 * n_old <= n_new <= 1.1 * n_old):
            problems.append("銘柄数が前回の90〜110%%の範囲外です（%d → %d）" % (n_old, n_new))
        limit = max(15, math.ceil(0.05 * n_old))
        if len(added) > limit:
            problems.append("追加が多すぎます（%d件 > 上限%d件）" % (len(added), limit))
        if len(removed) > limit:
            problems.append("除外が多すぎます（%d件 > 上限%d件）" % (len(removed), limit))
    return {"market": market, "path": path, "n_old": n_old, "n_new": n_new, "added": added, "removed": removed,
            "names": {**old, **new}, "problems": problems}


def _fmt_list(codes, names):
    return "、".join("%s（%s）" % (c, names.get(c, "")) if names.get(c) else c for c in codes) or "なし"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windowsのcp932でも「⚠」等を出力できるように
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["jp", "us", "both"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", default="universe_change.md")
    ap.add_argument("--msg", default="universe_commit_msg.txt")
    ap.add_argument("--title", default="母集団の見直し", help="コミットメッセージの先頭に付ける語")
    args = ap.parse_args()

    results = [check(m) for m in (["jp", "us"] if args.market == "both" else [args.market])]
    md, bad = ["## 10年保有ランキング 母集団の更新結果", ""], False
    for r in results:
        md.append("### %s（%s）" % (LABEL[r["market"]], r["path"]))
        md.append("- 銘柄数：%d → %d" % (r["n_old"], r["n_new"]))
        md.append("- 追加（%d件）：%s" % (len(r["added"]), _fmt_list(r["added"], r["names"])))
        md.append("- 除外（%d件）：%s" % (len(r["removed"]), _fmt_list(r["removed"], r["names"])))
        for p in r["problems"]:
            md.append("- ⚠ **%s**" % p)
        md.append("")
        bad = bad or bool(r["problems"])
    if bad and args.force:
        md.append("※ 安全確認で警告がありましたが、--force 指定のため自動反映を進めます。")
    elif bad:
        md.append("※ 安全確認で異常を検出したため、自動反映を見送りました（内容を確認し、問題なければ"
                  "ワークフローを手動実行して force を true にしてください）。")
    text = "\n".join(md) + "\n"
    open(os.path.join(HERE, args.out), "w", encoding="utf-8").write(text)

    summary = " / ".join("%s +%d/-%d" % (LABEL[r["market"]], len(r["added"]), len(r["removed"])) for r in results)
    msg = ["chore(universe-long): %s %s（%s）" % (args.title, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"), summary), ""]
    for r in results:
        msg.append("%s：%d → %d銘柄" % (LABEL[r["market"]], r["n_old"], r["n_new"]))
        msg.append("  追加：%s" % _fmt_list(r["added"], r["names"]))
        msg.append("  除外：%s" % _fmt_list(r["removed"], r["names"]))
    open(os.path.join(HERE, args.msg), "w", encoding="utf-8").write("\n".join(msg) + "\n")

    print(text)
    if bad and not args.force:
        sys.exit(1)


if __name__ == "__main__":
    main()
