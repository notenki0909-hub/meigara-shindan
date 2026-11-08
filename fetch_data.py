# -*- coding: utf-8 -*-
"""Yahoo Finance の取得だけを行う工程（配当株ツールと10年保有ツールで、同じ銘柄を二重に取得しないため）。
  python fetch_data.py jp [--sleep 1.5] [--limit N]   # 日本株：配当株（universe.json＋更新継続中の外れ銘柄＋日株講座Gp）と10年保有（universe_long.json）の和集合
  python fetch_data.py us [--sleep 1.5] [--limit N]   # 米国株：配当株（universe_us.json＋外れ銘柄）と10年保有（universe_long_us.json）の和集合
取得結果は yf_cache.py の形式で YF_CACHE_DIR（既定 .yf_cache）に保存する。分析側（batch.py／batch_long.py など）は、同じフォルダを
YF_CACHE_DIR に指定して読むだけで、Yahooへ取りに行かない（取れていない銘柄だけ、従来どおり自分で取得する＝取得に失敗しても分析は止まらない）。
保存済みで新しい（YF_CACHE_MAX_AGE_HOURS 以内）銘柄はスキップするので、途中で止まっても再実行で続きから取れる。
終了コード：全部成功 0／一部失敗 0（失敗は一覧を出す。分析側が自分で取り直す）／失敗が2割を超えたら 1（Yahoo側の障害を知らせるため）。"""
import argparse
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
os.environ.setdefault("YF_CACHE_DIR", os.path.join(HERE, ".yf_cache"))
os.environ.pop("YF_CACHE_READONLY", None)


def _items(path, key_names):
    if not os.path.isfile(path):
        return []
    j = json.load(open(path, encoding="utf-8"))
    out = []
    for it in next((j[k] for k in key_names if k in j), []):
        out.append(str(it.get("ticker") or it.get("code")) if isinstance(it, dict) else str(it))
    return out


def target_codes(market):
    """取得対象（重複なし・順序は配当株→10年保有のみ）。分析側が実際に取りに行く銘柄の和集合。"""
    seen, out = set(), []

    def add(c):
        c = c.upper() if market == "us" else c
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    import div_universe
    if market == "jp":
        for c in _items("universe.json", ("codes",)):
            add(c)
        for c, _n in div_universe.active_retired_codes("jp"):
            add(c)
        import kouza
        for c, _n in kouza.pinned_pairs():
            add(c)
        for c in _items("universe_long.json", ("codes",)):
            add(c)
    else:
        for c in _items("universe_us.json", ("tickers", "codes")):
            add(c)
        for c, _n in div_universe.active_retired_codes("us"):
            add(c)
        for c in _items("universe_long_us.json", ("tickers", "codes")):
            add(c)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["jp", "us"])
    ap.add_argument("--sleep", type=float, default=1.5, help="取得した銘柄の後に入れるスリープ秒（レート制限対策。保存済みでスキップした銘柄では待たない）")
    ap.add_argument("--limit", type=int, default=0, help="先頭N件だけ（0=全部）")
    args = ap.parse_args()

    import yf_cache
    import analyze
    suffix = ".T" if args.market == "jp" else ""
    codes = target_codes(args.market)
    if args.limit:
        codes = codes[: args.limit]
    d = os.environ["YF_CACHE_DIR"]
    print(f"取得対象 {len(codes)} 銘柄（{args.market}）  保存先={d}  再利用の上限={os.environ.get('YF_CACHE_MAX_AGE_HOURS', '12')}時間")
    t0 = time.time()
    n_new = n_skip = 0
    failed = []
    for i, c in enumerate(codes, 1):
        if yf_cache.is_fresh(c, suffix):
            n_skip += 1
            continue
        try:
            data = analyze.fetch_yf(c, suffix)          # 取得して保存まで行う（YF_CACHE_DIR 設定済み）
            if not (data.get("info") or data.get("price") is not None):
                failed.append(c)
                print(f"  ✗ {c}  取得できず（info・株価とも空）")
            else:
                n_new += 1
        except Exception as e:
            failed.append(c)
            print(f"  ✗ {c}  {e.__class__.__name__}: {e}")
            traceback.print_exc()
        if i % 100 == 0:
            print(f"  … {i}/{len(codes)}  取得 {n_new}  スキップ {n_skip}  失敗 {len(failed)}  {time.time() - t0:.0f}s", flush=True)
        time.sleep(args.sleep)
    dur = time.time() - t0
    manifest = {"market": args.market, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seconds": round(dur),
                "targets": len(codes), "fetched": n_new, "skipped_fresh": n_skip, "failed": failed}
    os.makedirs(d, exist_ok=True)
    json.dump(manifest, open(os.path.join(d, "_manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n完了 {dur:.0f}s  取得 {n_new}  スキップ（保存済み）{n_skip}  失敗 {len(failed)}" + (f"  失敗: {', '.join(failed[:30])}" if failed else ""))
    if codes and len(failed) > 0.2 * len(codes):
        sys.exit(1)


if __name__ == "__main__":
    main()
