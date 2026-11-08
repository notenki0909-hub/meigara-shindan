"""Yahoo Finance 取得結果の共有キャッシュ（夜間バッチで、配当株ツールと10年保有ツールが同じ銘柄を二重に取得しないため）。

環境変数 YF_CACHE_DIR が未設定のときは何もしない（素通し）＝手元の実行・既存の挙動は変わらない。
  YF_CACHE_DIR            保存先フォルダ。設定したときだけ有効
  YF_CACHE_MAX_AGE_HOURS  この時間以内に保存されたものだけ再利用（既定 12）
  YF_CACHE_READONLY=1     読むだけ（書かない）。分析側で使う
  YF_CACHE_STRICT=1       分析側用。保存されていない銘柄でも、Yahooには取りに行かない（取得に失敗したときと同じ空のデータを返す＝
                          その銘柄は『データ取得できず』となり、前回の結果が残る）。ただし保存先に _manifest.json（取得工程が書く）が
                          無いとき（初回・キャッシュが消えたとき）は、この指定を無視して従来どおり自分で取得する（分析が全面的に止まらないための安全弁）
キャッシュの中身は {"t": 保存時刻(epoch秒), "data": fetch_yf() の返り値（dict）} をpickle＋gzipしたもの
（保存時刻はファイルの更新日時ではなく中身に持つ＝zip展開やコピーで日時が変わっても正しく古さを判定できる）。読んだあとは呼び出し側が自由に書き換えてよい
（読むたびに新しく展開するので、別の銘柄・別のツールに影響しない）。
取得に失敗した結果（infoも株価も無い）は保存しない。"""
import gzip
import os
import pickle
import re
import time


LAST_FROM_CACHE = False   # 直近の cached_fetch がキャッシュから返したか（バッチが、Yahooへ取りに行っていないときはスリープを省くために見る）


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
        with gzip.open(p, "rb") as f:
            x = pickle.load(f)
        if not isinstance(x, dict) or time.time() - float(x.get("t", 0)) > _max_age_sec():
            return None
        data = x.get("data")
        return data if isinstance(data, dict) and data.get("code") == code else None
    except Exception:
        return None


def has_entry(code, suffix=".T"):
    """保存ファイルがあるか（古さは問わない）。"""
    d = _dir()
    return bool(d) and os.path.isfile(_path(d, code, suffix))


def is_fresh(code, suffix=".T"):
    """この銘柄の取得結果が、有効なキャッシュとして残っているか（取得専用の工程で、取得済みをスキップする判定用）。"""
    return load(code, suffix) is not None


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
            pickle.dump({"t": time.time(), "data": data}, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
    except Exception:
        pass


def strict_active():
    """厳格モード（Yahooに取りに行かない）が有効か。YF_CACHE_STRICT=1 かつ、取得工程が書く _manifest.json が保存先にあるとき。"""
    d = _dir()
    return bool(d) and os.environ.get("YF_CACHE_STRICT") == "1" and os.path.isfile(os.path.join(d, "_manifest.json"))


def cached_fetch(fetch, code, suffix=".T", stub=None):
    """fetch(code, suffix) の結果を、キャッシュが有効なら再利用・保存する。
    厳格モードで保存が無いときは、fetch を呼ばず stub(code, suffix)（取得失敗と同じ形の空のデータ）を返す。"""
    global LAST_FROM_CACHE
    hit = load(code, suffix)
    if hit is not None:
        LAST_FROM_CACHE = True
        return hit
    if stub is not None and strict_active():
        LAST_FROM_CACHE = True          # Yahooに取りに行かないので、バッチはスリープ不要
        return stub(code, suffix)
    LAST_FROM_CACHE = False
    data = fetch(code, suffix)
    save(code, suffix, data)
    return data
