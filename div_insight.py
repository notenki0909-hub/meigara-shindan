# -*- coding: utf-8 -*-
"""
配当株ツール（日本株・米国株）専用：ROA・財務レバレッジ・注意マーク（⚠）を、決算書(yfinance)から計算する。
**表示専用**：銘柄選定スコア・買い時スコアの計算には一切使わない（2026-10-08決定。10年保有ツールの検証で、
ROE・ROA・利益の質のどれも「採点に入れてよい」根拠が得られなかったため、表示・絞り込み・並べ替えだけにする）。

10年保有ツール（analyze_long.py の _insight_extras）の考え方を参考に、配当側専用に書いたもの（依存はない）。
analyze.py の generate() / analyze_us.py の generate_us() の中から、関数内の遅延importで呼ぶ
（先頭importにしないのは、このファイルの欠落・構文エラーで、10年保有側の夜間更新まで止まらないようにするため）。

  compute(yd, M, is_simple, is_reit, sel_score, tim_score, info, sec_med) → {"rows", "flags", "raw"}
    rows … M["参考"] に足す表示行（採点に使われない key=None の行）
    flags … 注意マーク [{"k": 種類, "t": 理由}]
    raw … 概要データ(summary)に保存する値（roa・leverage・roe_stmt）
  load_sector_median(market, sector) … 業種の中央値（rank.py/rank_us.py が母集団から作る site/**/sector_roe_roa.json）
  build_sector_medians(items) … 母集団の概要データから業種ごとのROE・ROA中央値を作る（n>=5の業種のみ）

基準値：ROA＝良好5%以上・注意3%以上（10年保有ツールと同じ）。ROEは配当側の採点と同じ値
（日本株10%/6%、米国株12%/7%。sector_rules*.json の roe）を目安に使う。
"""
import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
ROA_GOOD, ROA_WARN = 5.0, 3.0
ROE_RULE = {"jp": (10.0, 6.0), "us": (12.0, 7.0)}
MIN_PEERS = 5


def _num(x):
    return isinstance(x, (int, float)) and x == x


def _row(rows, *labels):
    for lb in labels:
        if lb in rows:
            return rows[lb]
    return None


def _at(s, i):
    return s[i] if s and len(s) > i and _num(s[i]) else None


def _m_val(M, group, key):
    for it in (M.get(group) or []):
        if it.get("key") == key:
            return it.get("v")
    return None


def load_sector_median(market, sector):
    """site/sector_roe_roa.json（米国は site/us/）の {業種: {roe, roa, n}} から、sector の中央値。無ければ None。"""
    p = os.path.join(HERE, "site", "us" if market == "us" else "", "sector_roe_roa.json")
    try:
        with open(p, encoding="utf-8") as f:
            return (json.load(f) or {}).get(sector)
    except Exception:
        return None


def build_sector_medians(items):
    """items: [(業種, roe, roa, 簡易判定か)] → {業種: {"roe", "roa", "n"}}。金融等の簡易判定は除く。n>=5の業種のみ。"""
    by = {}
    for sec, roe, roa, simple in items:
        if not sec or simple:
            continue
        d = by.setdefault(sec, {"roe": [], "roa": []})
        if _num(roe):
            d["roe"].append(roe)
        if _num(roa):
            d["roa"].append(roa)
    out = {}
    for sec, d in sorted(by.items()):
        n = max(len(d["roe"]), len(d["roa"]))
        if n < MIN_PEERS:
            continue
        out[sec] = {"roe": round(statistics.median(d["roe"]), 2) if len(d["roe"]) >= MIN_PEERS else None,
                    "roa": round(statistics.median(d["roa"]), 2) if len(d["roa"]) >= MIN_PEERS else None,
                    "n": n}
    return out


def _vs_sector(v, med):
    if not (_num(v) and _num(med)):
        return ""
    d = v - med
    pos = "上回る" if d > 0.05 else "下回る" if d < -0.05 else "ほぼ同じ"
    return f"　／　この業種（母集団内）の中央値 {med:.1f}%（{d:+.1f}ポイント、{pos}）"


def compute(yd, M, is_simple, is_reit, sel_score, tim_score, info, sec_med=None, market="jp", fin_extra=False):
    isr, bsr, cfr = yd.get("is_rows") or {}, yd.get("bs_rows") or {}, yd.get("cf_rows") or {}
    ni = _row(isr, "Net Income", "Net Income Common Stockholders", "Net Income Continuous Operations") or []
    ta = _row(bsr, "Total Assets") or []
    eq = _row(bsr, "Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest") or []
    ocf = _row(cfr, "Operating Cash Flow") or []
    # 銀行・保険・証券・REIT・その他金融（リース等）：ROA・CF・自己資本比率の物差しは当てはまらない。
    # fin_extra＝簡易判定にならない金融業種（日本株の「その他金融業」、米国株のGICS Financials）
    fin_like = bool(is_simple or is_reit or fin_extra)
    sm = sec_med or {}

    ni0, eq0, ta0 = _at(ni, 0), _at(eq, 0), _at(ta, 0)
    roe = ni0 / eq0 * 100 if _num(ni0) and _num(eq0) and eq0 > 0 else None
    roa = ni0 / ta0 * 100 if _num(ni0) and _num(ta0) and ta0 > 0 else None
    lev = ta0 / eq0 if _num(ta0) and _num(eq0) and eq0 > 0 else None
    raw = {"roe_stmt": roe, "roa": roa, "leverage": lev, "fin_like": fin_like}
    rows, flags = [], []

    # --- ROA（目安と業種比較）と、財務レバレッジ（借入に頼った高ROEの見分け）---
    if _num(roa):
        if fin_like:
            lab = "銀行・保険・証券・REITは構造上低く出るため、目安をそのまま当てはめず、同業との比較で見る"
        elif _num(ni0) and ni0 <= 0:
            lab = "赤字のため目安に当てはめない"
        else:
            lab = ("良好（5%以上）" if roa >= ROA_GOOD else "標準（3〜5%）" if roa >= ROA_WARN else "改善の余地（3%未満）")
            lab = f"目安では「{lab}」（5%以上で良好・3%未満は改善の余地）"
        rows.append({"name": "ROA（総資産利益率・決算書ベース）", "v": None, "disp": f"{roa:.1f}%",
                     "ref": lab + _vs_sector(roa, sm.get("roa")), "key": None})
    if _num(lev):
        if _num(ni0) and ni0 <= 0:
            note = "純損失のため、ROE・ROAは効率の目安にならない（赤字の理由を決算資料で確認）"
        elif fin_like:
            note = "銀行・保険・証券・REITは構造上レバレッジが大きく、ROAは低く出るのが普通"
        elif _num(roe) and roe >= 10 and _num(roa) and roa < 3:
            note = "ROEは高いがROAが低い＝借入に頼って高く見えている可能性（景気悪化時に返済負担が重くなる）"
        elif lev < 2.5:
            note = "借入に頼らず稼げている（ROEとROAが近い）"
        else:
            note = "ROEがROAより高いのは、借入（財務レバレッジ）の効果"
        rows.append({"name": "財務レバレッジ（総資産÷自己資本）", "v": None, "disp": f"{lev:.1f}倍",
                     "ref": note, "key": None})

    # --- 注意マーク（資料の足切り基準＋減配リスク）。赤字などを先に判定する ---
    if _num(roe) and _num(roa) and _num(ni0) and ni0 > 0 and not fin_like and roe >= 10 and roa < 3:
        flags.append({"k": "lev_roe", "t": "ROEは高いがROAが低い（借入に頼って高く見えている可能性）"})
    n3 = [_at(ni, i) for i in range(3)]
    if all(_num(x) for x in n3) and all(x <= 0 for x in n3):
        flags.append({"k": "loss3", "t": "純損失が3期連続"})
    if _num(eq0) and eq0 < 0:
        flags.append({"k": "insolvent", "t": "債務超過（自己資本がマイナス）"})
    if not fin_like:
        if _num(_at(ocf, 0)) and _num(_at(ocf, 1)) and ocf[0] < 0 and ocf[1] < 0:
            flags.append({"k": "ocf_neg", "t": "営業キャッシュフローが2期連続マイナス"})
        if _num(eq0) and _num(ta0) and ta0 > 0 and 0 <= eq0 / ta0 < 0.10:
            flags.append({"k": "low_equity", "t": f"自己資本比率が10%未満（{eq0 / ta0 * 100:.1f}%）"})
        fp = _m_val(M, "キャッシュフロー", "fcf_payout")
        if _num(fp) and fp > 100:
            flags.append({"k": "fcf_payout", "t": f"FCF配当性向が100%超（{fp:.0f}%：フリーCFで配当を賄えていない）"})
    pn = _m_val(M, "配当", "payout_ni")
    if _num(pn) and pn > 100:
        flags.append({"k": "payout", "t": f"配当性向が100%超（{pn:.0f}%：純利益を上回る配当）"})
    # バリュートラップ注意：買い時が高い（割安に見える）のに質が低め、または PBR1倍割れかつROE5%未満
    pbr = (info or {}).get("priceToBook")
    roe_for_trap = roe if _num(roe) else _m_val(M, "配当", "roe")
    if _num(tim_score) and _num(sel_score) and tim_score >= 72 and sel_score < 68:
        flags.append({"k": "trap", "t": f"バリュートラップ注意：買い時が高い（{tim_score:.0f}）のに質が低め（選定{sel_score:.0f}）。安さの理由を確認"})
    elif _num(pbr) and 0 < pbr < 1 and _num(roe_for_trap) and roe_for_trap < 5:
        flags.append({"k": "trap", "t": f"バリュートラップ注意：PBR {pbr:.2f}倍（1倍割れ）かつROE {roe_for_trap:.1f}%（5%未満）"})
    return {"rows": rows, "flags": flags, "raw": raw}
