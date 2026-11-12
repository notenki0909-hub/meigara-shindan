# -*- coding: utf-8 -*-
"""Yahoo!ファイナンス（日本）の配当ページの取得結果の保存（配当株ツールの夜間更新だけが使う）。

配当利回りの「みんかぶ方式」（analyze.py）は、会社予想の年間配当と決算期ごとの実績配当を
Yahoo!ファイナンス（日本）の配当ページから読む。この値が変わるのは決算・配当修正・株式分割のときだけなので、
毎晩700銘柄を取りに行かず、保存しておいて必要な銘柄だけ取り直す。

環境変数 YJ_DIV_CACHE_FILE が未設定のときは何もしない（毎回取得＝従来どおり）。手元の実行は保存ファイルを汚さない。
  YJ_DIV_CACHE_FILE   保存先のJSON。設定したときだけ有効（workflow：nightly.yml / earnings-retry.yml）
  YJ_DIV_DETECT_FILE  決算短信の検知記録（既定: tdnet_detections.json。テスト用に差し替え可）

取り直す条件（どれか）:
  1. 保存が無い（初回）。このとき次回の取り直し日を銘柄ごとにずらす（14日目に一斉に取りに行かないため）
  2. 期限（既定14日）が来た
  3. 株式分割：yfinance の最新の分割日が、保存したときと違う（過去の配当の調整後の値が変わるため）
  4. 決算短信を検知した（tdnet_detections.json）のに、まだ反映を確認できていない
     Yahoo側への反映の遅れは測っていないので、前回の取得から11時間以上あけて、値が変わるまで最大3回取り直す。
     値が変わった時点で「反映済み」とし、その遅れ（検知から何時間後か）を lag_h に記録する（あとで遅れを測るため）。
     3回取り直しても変わらなければ「配当に変更なし」とみなして終える（次は14日の見直しで拾う）。
取得に失敗したとき: 保存分が45日以内ならそれを使う（数字が推計に戻って揺れないように）。無ければ None（従来の計算へ）。
保存の形式: 銘柄ごとに1行（git の差分・競合を小さくするため）。
"""
import atexit
import datetime as dt
import json
import os
import threading
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REFRESH_DAYS = 14          # 定期の見直し間隔（最悪でも14日で取り直す）
STALE_USE_DAYS = 45        # 取得に失敗したとき、保存分をここまで使う
DET_MAX_TRIES = 3          # 決算短信の検知後、値が変わるまで取り直す最大回数
DET_MIN_GAP_H = 11         # 検知後の取り直しは、前回の取得からこの時間以上あける
DET_WINDOW_DAYS = 7        # 検知からこの日数を過ぎたら、もう追わない
FLUSH_EVERY = 20           # 更新がこの件数たまるごとに書き出す（途中で止まっても失わないため）

_LOCK = threading.RLock()
_ST = {"path": None, "ents": None, "dirty": 0, "det": None, "atexit": False}


def _path():
    p = os.environ.get("YJ_DIV_CACHE_FILE", "").strip()
    return p or None


def _utc(s):
    """ISO文字列 → タイムゾーン付きdatetime。読めなければ None。タイムゾーン無しはUTC扱い。"""
    try:
        d = dt.datetime.fromisoformat(s)
    except Exception:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _entries(p):
    if _ST["path"] != p or _ST["ents"] is None:
        ents = {}
        try:
            with open(p, encoding="utf-8") as f:
                x = json.load(f)
            if isinstance(x, dict):
                ents = {k: v for k, v in x.items() if isinstance(v, dict)}
        except Exception:
            ents = {}
        _ST.update(path=p, ents=ents, dirty=0)
        if not _ST["atexit"]:
            atexit.register(flush)
            _ST["atexit"] = True
    return _ST["ents"]


def _detections():
    """銘柄コード → 決算短信を最後に検知した時刻（tdnet_detections.json）。無い・壊れていれば空。"""
    if _ST["det"] is None:
        out = {}
        f = os.environ.get("YJ_DIV_DETECT_FILE", "").strip() or os.path.join(HERE, "tdnet_detections.json")
        try:
            with open(f, encoding="utf-8") as fh:
                for r in json.load(fh):
                    t = _utc(str(r.get("detected_at", "")))
                    c = str(r.get("code", ""))
                    if t and c and (c not in out or t > out[c]):
                        out[c] = t
        except Exception:
            out = {}
        _ST["det"] = out
    return _ST["det"]


def flush():
    """保存ファイルに書き出す（銘柄ごとに1行・コード順・同じ内容なら同じ文字列）。"""
    with _LOCK:
        p, ents = _ST["path"], _ST["ents"]
        if not p or ents is None or not _ST["dirty"]:
            return
        lines = [f'{json.dumps(c, ensure_ascii=False)}: {json.dumps(ents[c], ensure_ascii=False, separators=(",", ":"))}'
                 for c in sorted(ents)]
        text = "{\n" + ",\n".join(lines) + "\n}\n"
        try:
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, p)
            _ST["dirty"] = 0
        except Exception:
            pass


def _sig(e):
    return json.dumps([e.get("forecast"), e.get("actual")], sort_keys=True)


def _to_yj(e):
    fc = e.get("forecast")
    return {"forecast": (str(fc[0]), float(fc[1])) if fc else None,
            "actual": [(str(a[0]), float(a[1])) for a in (e.get("actual") or [])]}


def _need(e, det, split_sig, now):
    """取り直す理由（文字列）。要らなければ None。"""
    if e is None:
        return "new"
    fa = _utc(e.get("fetched_at", ""))
    if fa is None:
        return "broken"
    try:
        if now.date() >= dt.date.fromisoformat(e.get("due", "")):
            return "due"
    except Exception:
        return "broken"
    if (e.get("split") or "") != (split_sig or ""):
        return "split"
    if det is not None:
        done = _utc(e.get("det_done", "")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        if det > done and det <= now and (now - det) <= dt.timedelta(days=DET_WINDOW_DAYS) \
                and (now - fa) >= dt.timedelta(hours=DET_MIN_GAP_H):
            return "earnings"
    return None


def _build(code, raw, old, det, split_sig, now):
    fc = raw.get("forecast")
    new = {"fetched_at": now.isoformat(timespec="seconds"),
           "forecast": [str(fc[0]), float(fc[1])] if fc else None,
           "actual": [[str(a[0]), float(a[1])] for a in (raw.get("actual") or [])],
           "split": split_sig or "",
           "det_done": (old or {}).get("det_done", ""),
           "det_tries": int((old or {}).get("det_tries", 0)),
           "lag_h": (old or {}).get("lag_h")}
    if old is None:
        off = 1 + zlib.crc32(code.encode()) % REFRESH_DAYS      # 初回は取り直し日を1〜14日後にばらす
        new["due"] = (now.date() + dt.timedelta(days=off)).isoformat()
    else:
        new["due"] = (now.date() + dt.timedelta(days=REFRESH_DAYS)).isoformat()
    if det is not None:
        done = _utc(new["det_done"]) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        if det > done:
            if old is None:
                # 初回の取得。検知から3日以上たっていれば反映済みとみなす。直近なら、あとで取り直して確かめる
                if now - det > dt.timedelta(days=3):
                    new["det_done"], new["det_tries"] = det.isoformat(timespec="seconds"), 0
            elif _sig(old) != _sig(new):
                new["det_done"], new["det_tries"] = det.isoformat(timespec="seconds"), 0
                new["lag_h"] = round((now - det).total_seconds() / 3600, 1)
            else:
                new["det_tries"] += 1
                if new["det_tries"] >= DET_MAX_TRIES or (now - det) > dt.timedelta(days=DET_WINDOW_DAYS):
                    new["det_done"], new["det_tries"] = det.isoformat(timespec="seconds"), 0
    return new


def get(code, fetch, split_sig="", now=None):
    """配当ページの取得結果（analyze.fetch_yahoojp_dividend と同じ形）と、その出どころを返す。
    戻り値: (yj_div または None, 出どころ)。出どころは "nocache"（保存なしで取得）/ "cache"（保存分）/
    "fetched:<理由>"（取り直した）/ "stale"（取得に失敗したので保存分）/ "fail"（取れず）。
    fetch(code) は呼び出し側が渡す取得関数。split_sig は yfinance の最新の分割日（無ければ ""）。"""
    p = _path()
    if not p:
        return fetch(code), "nocache"
    now = now or dt.datetime.now(dt.timezone.utc)
    with _LOCK:
        e = _entries(p).get(code)
        det = _detections().get(code)
    why = _need(e, det, split_sig, now)
    if why is None:
        return _to_yj(e), "cache"
    raw = fetch(code)                                   # ネットワークは排他の外で行う
    with _LOCK:
        if not raw or (not raw.get("forecast") and not raw.get("actual")):
            fa = _utc((e or {}).get("fetched_at", ""))
            if e is not None and fa is not None and (now - fa) <= dt.timedelta(days=STALE_USE_DAYS):
                return _to_yj(e), "stale"
            return None, "fail"
        _entries(p)[code] = _build(code, raw, e, det, split_sig, now)
        _ST["dirty"] += 1
        n = _ST["dirty"]
    if n >= FLUSH_EVERY:
        flush()
    return raw, f"fetched:{why}"


def status(now=None):
    """保存の状況（件数・取り直し日の分布・反映の遅れ）を文字列で返す。"""
    p = _path()
    if not p:
        return "YJ_DIV_CACHE_FILE が未設定です"
    now = now or dt.datetime.now(dt.timezone.utc)
    ents = _entries(p)
    if not ents:
        return "保存なし"
    by_due = {}
    for e in ents.values():
        by_due[e.get("due", "?")] = by_due.get(e.get("due", "?"), 0) + 1
    lags = sorted(e["lag_h"] for e in ents.values() if isinstance(e.get("lag_h"), (int, float)))
    out = [f"保存 {len(ents)}銘柄", "取り直し日の分布: " + ", ".join(f"{k}:{v}" for k, v in sorted(by_due.items())[:20])]
    if lags:
        out.append(f"決算検知→Yahoo反映の遅れ(h): 件数{len(lags)} 中央値{lags[len(lags)//2]} 最大{lags[-1]}")
    return "\n".join(out)


if __name__ == "__main__":
    print(status())
