# -*- coding: utf-8 -*-
"""Yahoo Finance の取得だけを行う工程（配当株ツールと10年保有ツールで、同じ銘柄を二重に取得しないため）。
  python fetch_data.py jp [--sleep 1.5] [--limit N]   # 日本株：配当株（universe.json＋更新継続中の外れ銘柄＋日株講座Gp）と10年保有（universe_long.json）の和集合
  python fetch_data.py us [--sleep 1.5] [--limit N]   # 米国株：配当株（universe_us.json＋外れ銘柄）と10年保有（universe_long_us.json）の和集合
取得結果は yf_cache.py の形式で YF_CACHE_DIR（既定 .yf_cache）に保存する。workflowは、実行の最初に前回までの保存分（Actionsのキャッシュ）を
この場所に復元してから呼ぶ＝『前回の保存分を引き継ぎ、古い銘柄（YF_CACHE_MAX_AGE_HOURS より前）だけ取り直す』。
取れなかった銘柄は、この中で数回やり直す（--passes）。それでも取れなかった銘柄は、前回のデータが残る（分析側は、古いデータに⚠を付けて使う）。
分析側（batch.py／batch_long.py など）は、同じ場所を読むだけでYahooには取りに行かない（YF_CACHE_STRICT=1）。
保存して7日を超えた銘柄・取得対象から外れた銘柄の保存は、ここで削除する（保存が増え続けないように）。
終了コード：取り直しに失敗した銘柄が2割を超えたら 1（Yahoo側の障害を知らせるため。分析は前回の保存分で続く）、それ以外は 0。"""
import argparse
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
os.environ.setdefault("YF_CACHE_DIR", os.path.join(HERE, ".yf_cache"))
os.environ.pop("YF_CACHE_READONLY", None)
import yf_cache  # noqa: E402


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


def _prune(d, codes, suffix, max_age_hours=168):
    """保存先から、取得対象から外れた銘柄・保存から max_age_hours を超えた銘柄のファイルを消す。消した件数を返す。"""
    import gzip
    import pickle
    keep = {os.path.basename(yf_cache._path(d, c, suffix)) for c in codes}
    n = 0
    for fn in os.listdir(d):
        if not fn.endswith(".pkl.gz") or not fn.startswith(("t_" if suffix == ".T" else "us_")):
            continue
        p = os.path.join(d, fn)
        drop = fn not in keep
        if not drop:
            try:
                with gzip.open(p, "rb") as f:
                    t = float(pickle.load(f).get("t", 0))
                drop = time.time() - t > max_age_hours * 3600
            except Exception:
                drop = True
        if drop:
            try:
                os.remove(p)
                n += 1
            except OSError:
                pass
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["jp", "us"])
    ap.add_argument("--sleep", type=float, default=1.5, help="取得した銘柄の後に入れるスリープ秒（レート制限対策。保存済みでスキップした銘柄では待たない）")
    ap.add_argument("--limit", type=int, default=0, help="先頭N件だけ（0=全部）")
    ap.add_argument("--passes", type=int, default=3, help="取れなかった銘柄のやり直しを含めた、最大の回数（1回目は全体、2回目以降は失敗分だけ）")
    ap.add_argument("--retry-wait", type=float, default=60.0, help="やり直しの前に待つ秒数")
    args = ap.parse_args()

    import analyze
    suffix = ".T" if args.market == "jp" else ""
    codes = target_codes(args.market)
    if args.limit:
        codes = codes[: args.limit]
    d = os.environ["YF_CACHE_DIR"]
    os.makedirs(d, exist_ok=True)
    print(f"取得対象 {len(codes)} 銘柄（{args.market}）  保存先={d}  再取得の基準={os.environ.get('YF_CACHE_MAX_AGE_HOURS', '12')}時間より前")
    t0 = time.time()
    pending = [c for c in codes if not yf_cache.is_fresh(c, suffix)]
    n_skip = len(codes) - len(pending)
    n_new = 0
    for ps in range(1, max(1, args.passes) + 1):
        if not pending:
            break
        if ps > 1:
            print(f"--- やり直し {ps}回目（{len(pending)}銘柄）。{args.retry_wait:.0f}秒待ちます", flush=True)
            time.sleep(args.retry_wait)
        failed = []
        for i, c in enumerate(pending, 1):
            try:
                data = analyze.fetch_yf(c, suffix)          # 取得して保存まで行う（YF_CACHE_DIR 設定済み・厳格モードなし）
                if not (data.get("info") or data.get("price") is not None):
                    failed.append(c)
                    print(f"  ✗ {c}  取得できず（info・株価とも空）")
                else:
                    n_new += 1
            except Exception as e:
                failed.append(c)
                print(f"  ✗ {c}  {e.__class__.__name__}: {e}")
            if i % 100 == 0:
                print(f"  … {i}/{len(pending)}  取得 {n_new}  失敗 {len(failed)}  {time.time() - t0:.0f}s", flush=True)
            time.sleep(args.sleep)
        pending = failed
    failed = pending
    n_pruned = _prune(d, codes, suffix)
    carried = [c for c in failed if yf_cache.has_entry(c, suffix)]          # 取り直せなかったが、前回のデータが残っている銘柄
    dur = time.time() - t0
    manifest = {"market": args.market, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seconds": round(dur),
                "targets": len(codes), "fetched": n_new, "skipped_fresh": n_skip, "failed": failed,
                "carried_over": carried, "pruned": n_pruned}
    json.dump(manifest, open(os.path.join(d, "_manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    msg = (f"完了 {dur:.0f}s  取得 {n_new}  スキップ（新しい保存あり）{n_skip}  取り直せず {len(failed)}"
           f"（うち前回のデータが残る {len(carried)}）  削除 {n_pruned}")
    print("\n" + msg + (f"\n  取り直せなかった銘柄: {', '.join(failed[:40])}" if failed else ""))
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(f"### 取得工程（{args.market}）\n- {msg}\n")
            if failed:
                f.write(f"- 取り直せなかった銘柄: {', '.join(failed[:60])}\n")
    if failed:
        print(f"::warning::{len(failed)}銘柄を取り直せませんでした（前回のデータが残る: {len(carried)}銘柄）")
    if codes and len(failed) > 0.2 * len(codes):
        sys.exit(1)


if __name__ == "__main__":
    main()
