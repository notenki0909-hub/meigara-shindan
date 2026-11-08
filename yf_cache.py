"""Yahoo Finance 取得結果の共有キャッシュ（夜間バッチで、配当株ツールと10年保有ツールが同じ銘柄を二重に取得しないため）。

環境変数 YF_CACHE_DIR が未設定のときは何もしない（素通し）＝手元の実行・既存の挙動は変わらない。
  YF_CACHE_DIR            保存先フォルダ。設定したときだけ有効
  YF_CACHE_MAX_AGE_HOURS  この時間以内に保存されたものだけ再利用（既定 12）
  YF_CACHE_READONLY=1     読むだけ（書かない）。後から動く側（nightly-long など）で使える
キャッシュの中身は fetch_yf() の返り値（dict）をそのままpickle＋gzipしたもの。読んだあとは呼び出し側が自由に書き換えてよい
（読むたびに新しく展開するので、別の銘柄・別のツールに影響しない）。
取得に失敗した結果（infoも株価も無い）は保存しない。"""
import gzip
import os
import pickle
import re
import time


def _dir():
    d = os.environ.get("YF_CACHE_DIR", "").strip()
    return d or None


def _path(d, code, suffix):
    mk = (suffix or "").strip(".").lower() or "us"
    return os.path.join(d, f"{mk}_{re.sub(r'[^0-9A-Za-z.-]', '_', str(code))}.pkl.gz")


def _max_age_sec():
    try:
        return float(os.environ.get("YF_CACHE_MAX_AGE_HOURS", "12")) * 3600
    except ValueError:
        return 12 * 3600


def load(code, suffix=".T"):
    d = _dir()
    if not d:
        return None
    p = _path(d, code, suffix)
    try:
        if time.time() - os.path.getmtime(p) > _max_age_sec():
            return None
        with gzip.open(p, "rb") as f:
            x = pickle.load(f)
        return x if isinstance(x, dict) and x.get("code") == code else None
    except Exception:
        return None


def save(code, suffix, data):
    d = _dir()
    if not d or os.environ.get("YF_CACHE_READONLY") == "1":
        return
    if not isinstance(data, dict) or not (data.get("info") or data.get("price") is not None):
        return                                   # 取得に失敗した結果は保存しない
    try:
        os.makedirs(d, exist_ok=True)
        p = _path(d, code, suffix)
        tmp = p + ".tmp"
        with gzip.open(tmp, "wb", compresslevel=3) as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
    except Exception:
        pass


def cached_fetch(fetch, code, suffix=".T"):
    """fetch(code, suffix) の結果を、キャッシュが有効なら再利用・保存する。"""
    hit = load(code, suffix)
    if hit is not None:
        return hit
    data = fetch(code, suffix)
    save(code, suffix, data)
    return data
