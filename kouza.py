# -*- coding: utf-8 -*-
"""
日株講座Gp（講座で使う銘柄の固定リスト）の読み込み部品。
batch.py／rank.py／div_universe.py／tdnet_watch.py が使う。

■ 仕組み
  ・kouza_pins.json … 講座Gpの銘柄リスト（人が編集する）。母集団（universe.json）とは別ファイル。
    母集団のスクリーニング（利回り・時価総額・減配なし）や四半期の見直しの影響を受けないので、
    こちらから指示して外さない限り、いつまでも残る。
  ・各段階（超入門→入門→初級→中級→上級）の "codes" には、その段階で「新しく足す」銘柄だけを書く。
    表示するときは、下の段階の銘柄をすべて含めた積み上げ（超入門5社 → 入門は+3社で8社 …）にする。
  ・夜間更新（batch.py --with-kouza）が、母集団に入っていない講座Gpの銘柄も診断して、
    個別レポート（site/reports/）とサマリ（site/summaries/）を作る。
  ・母集団に入っていない銘柄は、業種別の表・全体上位50・業種の中央値・軍分けには入れず（既存ランキングに
    影響させない）、講座Gpの欄にだけ「スクリーン条件外」の印つきで載せる。

■ kouza_pins.json の書き方
  {"name": "日株講座Gp", "levels": [
      {"name": "超入門・５万円モデル", "codes": ["8058", "2914"]},
      {"name": "入門・１０万円モデル", "codes": ["9433"]},
      ...
  ]}
  codes の要素は "8058" または {"code": "8058", "name": "三菱商事"}（名前は診断前の仮表示用。省略可）。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PINS = os.path.join(HERE, "kouza_pins.json")


def _item(x):
    if isinstance(x, dict):
        code = str(x.get("code") or "").strip()
        return code, (x.get("name") or "")
    return str(x).strip(), ""


def load(path=None):
    """→ {"name": str, "levels": [{"name", "add": [code...], "cum": [code...]}], "names": {code: 仮名}}
    add＝その段階で新しく足す銘柄、cum＝下の段階を含めた積み上げ（重複なし・並びは追加順）。"""
    path = path or PINS
    try:
        doc = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {"name": "日株講座Gp", "levels": [], "names": {}}
    levels, seen, cum, names = [], set(), [], {}
    for lv in doc.get("levels") or []:
        add = []
        for it in lv.get("codes") or []:
            code, nm = _item(it)
            if not code:
                continue
            if nm:
                names.setdefault(code, nm)
            if code not in seen:
                seen.add(code)
                add.append(code)
                cum.append(code)
        levels.append({"name": lv.get("name") or "", "add": add, "cum": list(cum)})
    return {"name": doc.get("name") or "日株講座Gp", "levels": levels, "names": names}


def pinned_codes(path=None):
    """講座Gpに入っている全銘柄（重複なし・追加順）→ [code, ...]"""
    lv = load(path)["levels"]
    return list(lv[-1]["cum"]) if lv else []


def pinned_set(path=None):
    return set(pinned_codes(path))


def universe_names():
    """universe.json の母集団＋除外リスト（スクリーンに合わなかった銘柄）の日本語名 → {code: name}。
    母集団に入っていない講座Gpの銘柄も、日本語名で表示・診断できるようにする。"""
    try:
        doc = json.load(open(os.path.join(HERE, "universe.json"), encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for key in ("rejected", "codes"):
        for it in doc.get(key) or []:
            if isinstance(it, dict) and it.get("code") and it.get("name"):
                out[str(it["code"]).strip()] = it["name"]
    return out


def names(path=None):
    """講座Gpの銘柄名 → {code: name}。kouza_pins.json に書いた名前を優先し、無ければ universe.json の名前。"""
    uni = universe_names()
    out = {c: uni[c] for c in pinned_codes(path) if c in uni}
    out.update(load(path)["names"])
    return out


def pinned_pairs(path=None):
    """batch.py などの対象リスト用 → [(code, name_or_None), ...]"""
    nm = names(path)
    return [(c, nm.get(c) or None) for c in pinned_codes(path)]
