# -*- coding: utf-8 -*-
"""
『10年保有できる優良企業』米国版の月次軽量チェック。

S&P500の構成銘柄入れ替えは不定期（M&A・破産等のたびに随時）に起きるため、
四半期に1回のフル再構築（universe-long-quarterly.yml、全銘柄2パス診断で数時間）
を待たずに追従する。こちらは新規追加銘柄だけを診断するので数分で終わる。

  python monthly_check_long_us.py

やること:
1. build_universe_us.py の candidates() を呼び、最新のS&P500構成銘柄を
   Wikipediaから取得（universe_candidates_us.json を上書き）。
2. 現在の universe_long_us.json と比較し、新規追加／除外を検出。
3. 新規追加：batch_long.py --only-codes で診断し、universe_long_us.json に追記。
4. 除外：universe_long_us.json からは削除しない（四半期のフル再構築まで温存）。
   代わりに universe_long_watch_us.json に理由付きで記録するだけ。
   rank_long.py がこのファイルを見て、対象銘柄を「対象外」として表示する。
   （四半期フル再構築が universe_long_us.json を作り直すタイミングで、実際に
   外れていればこのwatchの記録も自然に不要になる＝手で消さなくてよい設計。
   このスクリプト自身も、fresh一覧に戻ってきた銘柄はwatchから自動的に外す。）
5. calib_long.py us・rank_long.py us でランキングへ反映。

配当株ツール（build_universe_us.py の screen/all サブコマンド、universe_us.json、
batch_us.py、rank_us.py）には一切触れない。build_universe_us.py 自体も無改変
（candidates() を import して呼ぶだけ）。
"""
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

UNIV_PATH = os.path.join(HERE, "universe_long_us.json")
WATCH_PATH = os.path.join(HERE, "universe_long_watch_us.json")
SCREEN_PATH = os.path.join(HERE, "universe_screen_long_us.json")
CAND_PATH = os.path.join(HERE, "universe_candidates_us.json")


def _load(path, default=None):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def main():
    # 1) 最新のS&P500構成銘柄を取得（配当株ツールの build_universe_us.py を
    #    import して candidates() だけ呼ぶ。配当株側のファイルには一切書き込まない）
    sys.path.insert(0, HERE)
    import build_universe_us
    build_universe_us.build_candidates()

    scr = _load(SCREEN_PATH)
    member = scr["index_membership"]
    cand = _load(CAND_PATH)["candidates"]
    fresh = {c["ticker"]: c for c in cand if member in (c.get("index") or [])}

    univ = _load(UNIV_PATH)
    current = {t["ticker"]: t for t in univ["tickers"]}

    added = sorted(set(fresh) - set(current))
    removed = sorted(set(current) - set(fresh))

    watch = _load(WATCH_PATH, {"excluded": {}})
    watch.setdefault("excluded", {})
    back = [t for t in list(watch["excluded"]) if t in fresh]

    print(f"追加候補: {len(added)} 件 {added}")
    print(f"除外候補: {len(removed)} 件 {removed}")
    print(f"復帰（暫定除外解除）: {len(back)} 件 {back}")

    if not (added or removed or back):
        print("変化なし")
        return

    today = dt.date.today().isoformat()

    # 2) 新規追加分を universe_long_us.json に追記
    if added:
        for t in added:
            univ["tickers"].append({
                "ticker": t, "name": fresh[t].get("name"),
                "gics_sector": fresh[t].get("gics_sector"),
            })
        univ["tickers"].sort(key=lambda x: x["ticker"])
        univ["count"] = len(univ["tickers"])
        univ["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        univ["_monthly_check_note"] = (
            f"{today} の月次チェックで {len(added)} 銘柄を追加。"
            "次の四半期フル再構築(universe-long-quarterly.yml)で正式なリストに置き換わる。")
        _save(UNIV_PATH, univ)
        print(f"-> {UNIV_PATH} 更新（+{len(added)}）")

    # 3) 除外候補を暫定除外リストへ（universe_long_us.json 本体からは削除しない）
    for t in removed:
        watch["excluded"][t] = {
            "reason": "S&P500から除外（月次チェック検知・次回四半期見直しで正式反映）",
            "detected_at": today,
        }
    for t in back:
        del watch["excluded"][t]
    watch["_meta"] = {
        "説明": "月次チェック(monthly_check_long_us.py)で検知した暫定除外銘柄。"
                "universe_long_us.json からは削除せず、rank_long.py がここに載っている"
                "銘柄を「対象外」として表示するだけにとどめる。次の四半期フル再構築"
                "(universe-long-quarterly.yml)で universe_long_us.json が正式に作り直され"
                "れば、このファイルの記録の要否も次回チェック時に自動で見直される。",
        "updated_at": today,
    }
    _save(WATCH_PATH, watch)
    print(f"-> {WATCH_PATH} 更新（暫定除外 {len(watch['excluded'])} 件）")

    # 4) 新規追加分を診断（既存銘柄は再診断しない＝軽量）
    if added:
        subprocess.run(
            [sys.executable, "batch_long.py", "us",
             "--only-codes", ",".join(added), "--sleep", "1.5"],
            cwd=HERE, check=True)

    # 5) 校正・ランキング再生成
    subprocess.run([sys.executable, "calib_long.py", "us"], cwd=HERE, check=True)
    subprocess.run([sys.executable, "rank_long.py", "us"], cwd=HERE, check=True)


if __name__ == "__main__":
    main()
