# -*- coding: utf-8 -*-
"""確認結果（IR BANK・決算短信・SECで手作業で確認した上書き）の整合チェック。読み取り専用。
  python check_overrides.py
配当側の fin_overrides.json／dps_overrides.json と、10年側の data_check_long.json を突き合わせて、次を一覧にする：
  A. 両方の母集団にいる銘柄で、片方にしか確認結果が無い（もう片方でも同じYahooの不備が出ているのに、扱いが割れている可能性）
  B. 両方に確認結果があるが、項目ごとの判定（status）が食い違っている
  C. 確認から180日以上たった登録（配当側 fin_overrides／dps_overrides の date。10年側 data_check_long の date）
  D. 配当側で、確認した決算期（fy）が付いていない fin_overrides の項目（期限切れの仕組みが効かない）
終了コード：問題あり 1／なし 0（手元・夜間の点検用。サイトや採点には一切影響しない）。"""
import datetime as dt
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
STALE_DAYS = 180
# 片方にしか確認結果が無いが、理由が分かっていて問題ない銘柄（理由つき。指摘の件数には数えない）
KNOWN_ONE_SIDED = {
    "8316": "10年側のみ：分割後もYahooの1株利益が分割前の基準のまま。配当側は系列内で一貫しているため、EPS成長率は影響なし。配当性向は analyze.payout_split_factor で補正済み",
}


def _load(name):
    try:
        return json.load(open(os.path.join(HERE, name), encoding="utf-8"))
    except Exception:
        return {}


def _codes(name, key):
    return {str(x["code"]) for x in (_load(name).get(key) or []) if isinstance(x, dict) and "code" in x}


def main():
    fo = {k: v for k, v in _load("fin_overrides.json").items() if not k.startswith("_") and isinstance(v, dict)}
    do = {k: v for k, v in _load("dps_overrides.json").items() if not k.startswith("_") and isinstance(v, dict)}
    dc = (_load("data_check_long.json").get("jp") or {})
    u, l = _codes("universe.json", "codes"), _codes("universe_long.json", "codes")
    both = u & l
    today = dt.date.today()
    problems = 0

    def age(d):
        try:
            return (today - dt.date.fromisoformat(str(d)[:10])).days
        except Exception:
            return None

    print("【A】両方の母集団にいる銘柄で、片方にしか確認結果が無い")
    n = 0
    for c in sorted((set(fo) | set(dc)) & both):
        a, b = set((fo.get(c) or {}).keys()), set((dc.get(c) or {}).keys())
        if a != b:
            if c in KNOWN_ONE_SIDED:
                print(f"  {c}: （既知）{KNOWN_ONE_SIDED[c]}")
                continue
            n += 1
            print(f"  {c}: 配当側 {sorted(a) or 'なし'} ／ 10年側 {sorted(b) or 'なし'}")
    print(f"  → {n}件" if n else "  → なし")
    problems += n

    print("【B】両方に確認結果があるが、判定（status）が食い違う")
    n = 0
    for c in sorted(set(fo) & set(dc)):
        for k in sorted(set(fo[c]) & set(dc[c])):
            sa, sb = fo[c][k].get("status"), dc[c][k].get("status")
            if sa != sb:
                n += 1
                print(f"  {c} {k}: 配当側 {sa} ／ 10年側 {sb}")
    print(f"  → {n}件" if n else "  → なし")
    problems += n

    print(f"【C】確認から{STALE_DAYS}日以上たった登録")
    n = 0
    for c, v in sorted(fo.items()):
        for k, it in v.items():
            a = age(it.get("date"))
            if a is not None and a >= STALE_DAYS:
                n += 1
                print(f"  配当側 fin_overrides {c} {k}: {it.get('date')}（{a}日前）")
    for c, v in sorted(do.items()):
        a = age(v.get("date"))
        if a is None or a >= STALE_DAYS:
            n += 1
            print(f"  配当側 dps_overrides {c}: 確認日 {v.get('date') or '未記録'}" + (f"（{a}日前）" if a is not None else ""))
    for c, v in sorted(dc.items()):
        for k, it in v.items():
            a = age(it.get("date"))
            if a is not None and a >= STALE_DAYS:
                n += 1
                print(f"  10年側 data_check_long {c} {k}: {it.get('date')}（{a}日前）")
    print(f"  → {n}件" if n else "  → なし")
    problems += n

    print("【D】確認した決算期（fy）が付いていない fin_overrides の項目")
    n = 0
    for c, v in sorted(fo.items()):
        for k, it in v.items():
            if not it.get("fy"):
                n += 1
                print(f"  {c} {k}")
    print(f"  → {n}件" if n else "  → なし")
    problems += n
    print(f"\n合計 {problems}件の指摘" if problems else "\n指摘なし")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
