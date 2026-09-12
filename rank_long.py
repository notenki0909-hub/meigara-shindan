# -*- coding: utf-8 -*-
"""
『10年保有できる優良企業』ランキング（配当を評価しない品質スコア版）を生成する。
配当株ランキング（rank.py / rank_us.py）とは独立。既存ファイルは書き換えない。

  python rank_long.py jp     -> site/long/index.html      + site/long/ranking.json
  python rank_long.py us     -> site/us/long/index.html   + site/us/long/ranking.json

品質スコア = 既存サマリの groups から、
  業績0.28 ・ 財務0.27 ・ キャッシュフロー0.15
を取得できたグループだけで加重平均して再正規化（0〜110）。
「配当の持続力」グループは使わない。配当利回りは参考列として表示のみ。

金融・保険・証券・REIT は analyze が業績/財務/CFを採点しない（配当の持続力のみで
sel_score を出す）ため品質スコアが None になる。これらは軍分けせず「対象外」節に出す。

サマリは site/summaries/（既存 nightly が更新）と site/long_summaries/（batch_long.py が
補完）の両方を見て、新しい方を採用する。
"""
import argparse
import datetime as dt
import glob
import html
import json
import os
import shutil
import statistics as st

import rank as _jp          # THEME_*, TIER_CLASS, DIR_CLASS を流用（配色をサイトと統一）
import rank_us as _us
import long_common as LC

HERE = os.path.dirname(os.path.abspath(__file__))

# sel_score の score_groups.選定 から「配当の持続力」を除いたもの
QUALITY_W = {"業績": 0.28, "財務": 0.27, "キャッシュフロー": 0.15}

MK = {
    "jp": {
        "theme": _jp,
        "universe": "universe_long.json",
        "sumdir": os.path.join(HERE, "site", "long_summaries"),
        "groups_cfg": "sector_groups_long.json",
        "seckey": "jp_sector",
        "out_dir": os.path.join(HERE, "site", "long"),
        "report_dirs": [("reports/", os.path.join(HERE, "site", "long", "reports"))],
        "title": "10年保有できる優良企業ランキング（日本株）",
        "screen_line": "母集団＝時価総額3,000億円以上（TOPIX500相当）。銀行・保険・証券は"
                       "専用の品質スコア（ROE・増収率・EPS成長率・利益の安定度）で評価。"
                       "品質スコア（配当は不使用）＋買い時スコア（EV/EBIT・FCF利回り・PER/PBR割安度）。",
        "terms_src": "long_terms.html",
        "guide_src": "long_guide.html",
        "portfolio_src": "long_portfolio.html",
        "unit_price": "円",
        "watch": None,
    },
    "us": {
        "theme": _us,
        "universe": "universe_long_us.json",
        "sumdir": os.path.join(HERE, "site", "us", "long_summaries"),
        "groups_cfg": "sector_groups_long_us.json",
        "seckey": "gics_sector",
        "out_dir": os.path.join(HERE, "site", "us", "long"),
        "report_dirs": [("reports/", os.path.join(HERE, "site", "us", "long", "reports"))],
        "title": "10年保有できる優良企業ランキング（米国株）",
        "screen_line": "母集団＝S&P500 メンバーシップ（黒字継続・流動性・業種代表性を"
                       "委員会が審査済み）。金融は専用の品質スコア（ROE・増収率・EPS成長率・"
                       "利益の安定度）、REITは専用のFFOベース品質スコアで評価。"
                       "品質スコア（配当は不使用）＋買い時スコア。",
        "terms_src": "long_us_terms.html",
        "guide_src": "long_us_guide.html",
        "portfolio_src": "long_us_portfolio.html",
        "unit_price": "$",
        "watch": "universe_long_watch_us.json",
    },
}

NAV_ITEMS = [
    ("利用規約・免責事項", "terms.html"),
    ("使い方・見方", "guide.html"),
    ("ランキング", "index.html"),
    ("ウォッチリスト", "watchlist.html"),
    ("ポートフォリオ", "portfolio.html"),
]


def _pagenav(current, exclude=()):
    """右上のページ間リンク。当該ページ自身と exclude で指定したページは含めない。"""
    excl = set(exclude) | {current}
    links = "".join(f'<a href="{href}">{label}</a>'
                     for label, href in NAV_ITEMS if href not in excl)
    return f'<span class="pagenav">{links}</span>'


TERMS = {
    "grade": ("業種級（A/B/C）",
              "業種グループ内の<b>品質スコア中央値</b>で A＞B＞C。Aが最も質の高い企業が揃う"
              "業種という序列（業種選びの目安）。個別銘柄の良し悪しは「品質」と「軍」で見る。"),
    "tier": ("軍（1〜3軍）",
             "業種グループ内で品質スコアの高い順に、上位25%を1軍・続く45%を2軍・残りを3軍。"
             "カバレッジ「低」は最高2軍まで。品質スコアが十分高い銘柄は、強い業種にいるだけで"
             "3軍に落ちないよう最低2軍を保証（1軍はグループ内上位の意味を残す）。6銘柄未満の"
             "グループは軍分けせず「―」。矢印は前回比の方向（↑改善／→横ばい／↓悪化）。"),
    "q": ("品質スコア（0〜110）",
          "配当を評価しない、長期保有できる優良企業かのスコア。<b>業績×0.28＋財務×0.27＋"
          "キャッシュフロー×0.15</b>（取得できたグループで再正規化）。既存の「銘柄選定スコア」"
          "から配当の持続力（連続増配・増配率・配当性向・累進配当宣言など）を除いたもの。"),
    "perf": ("業績", "売上・EPSの伸び（年率）、営業利益率、利益の安定度（営業利益のブレ）の"
             "平均点（0〜110）。品質スコアの28%。"),
    "fin": ("財務", "自己資本比率・D/E・ネットD/E・有利子負債÷営業CF（米国株はICRを含み"
            "自己資本比率は参考）の平均点。品質スコアの27%。"),
    "cf": ("CF（キャッシュフロー）", "営業CFの継続黒字・フリーCFの継続黒字・FCF配当性向の"
           "平均点。品質スコアの15%。"),
    "bt": ("買い時スコア（0〜110）",
           "今の株価が割高すぎないか。配当利回りは使わず、<b>EV/EBIT対業種・FCF利回り・"
           "PER割安度・PBR割安度を均等25%</b>で合成（欠損は中立60）。色＝買い場（割安圏）／"
           "ほぼ妥当／やや割高／割高で見送りの4段階。品質とは別物で、質の評価には混ぜない。"),
    "yield": ("利回り", "予想年間配当 ÷ 現在株価（予想配当利回り）。<b>採点には使わず参考表示のみ。</b>"),
    "cov": ("カバレッジ", "品質スコアの算出に使えたグループ数（業績・財務・CFの3つ中）。"
            "高＝3/3・中＝2/3・低＝1以下。低い銘柄は財務やCFの履歴が短く点がぶれやすい。"),
    "price": ("終値", "前営業日の終値。夜間更新のため当日ザラ場とはずれる。"),
}


DISC = ('本ページは、あらかじめ定めた基準（時価総額または指数構成）で抽出した銘柄について、'
        '公開データを機械的なルールで算出した「配当を含めない品質スコア」（業績・財務・'
        'キャッシュフロー）による分類です。配当利回りは参考表示で、採点には使っていません。'
        '銀行・保険・証券は専用の品質スコア（ROE・増収率・EPS成長率・利益の安定度）、'
        '米国REITは専用のFFOベース品質スコアで評価しています。日本のJ-REITはこのツールの'
        '母集団に含まれておらず対象外です。'
        '特定銘柄の売買を推奨・勧誘するものではなく、運営者は投資助言・代理業の登録を受けて'
        'いません。教育目的の一般情報であり、投資判断はご自身の責任で行ってください。数値は'
        'yfinance 由来で誤り・遅延・欠損があり得ます。')


# ---------------------------------------------------------------- スコア
def quality_score(groups):
    num = den = 0.0
    for g, w in QUALITY_W.items():
        v = groups.get(g)
        if isinstance(v, (int, float)):
            num += w * v
            den += w
    return round(num / den, 1) if den > 0 else None


def quality_cov(groups):
    got = sum(1 for g in QUALITY_W if isinstance(groups.get(g), (int, float)))
    lab = "高" if got >= 3 else "中" if got == 2 else "低"
    return got, len(QUALITY_W), lab


def _num(v, d=1):
    return f"{v:.{d}f}" if isinstance(v, (int, float)) else "―"


def _price(v, unit):
    if not isinstance(v, (int, float)):
        return "―"
    return (f"${v:,.2f}" if unit == "$" else f"{v:,.0f}")


def _v(x):
    return x if isinstance(x, (int, float)) else -1e9


def grade_of(median, ga, gb):
    if median is None:
        return "―"
    return "A" if median >= ga else "B" if median >= gb else "C"


def tier_of(idx, n, c1, c2):
    n1 = max(1, round(n * c1))
    n2 = max(1, round(n * c2))
    if idx < n1:
        return "1軍"
    if idx < n1 + n2:
        return "2軍"
    return "3軍"


def direction(now, prev, thr=2.0):
    if now is None or prev is None:
        return "→"
    d = now - prev
    return "↑" if d >= thr else "↓" if d <= -thr else "→"


# ---------------------------------------------------------------- データ収集
def load_summary(code, sumdir):
    p = os.path.join(sumdir, f"{code}.json")
    if not os.path.isfile(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def load_prev(out_dir):
    p = os.path.join(out_dir, "ranking.json")
    if not os.path.isfile(p):
        return {}
    try:
        j = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for g in j.get("groups", []):
        for s in g.get("stocks", []):
            out[s["code"]] = s.get("q")
    for s in j.get("excluded", []):
        out.setdefault(s["code"], None)
    return out


def build(market):
    m = MK[market]
    gcfg = json.load(open(os.path.join(HERE, m["groups_cfg"]), encoding="utf-8"))
    gmap = {}
    for gname, secs in gcfg["groups"].items():
        for s in secs:
            gmap[s] = gname
    ga, gb = gcfg["grade_a"], gcfg["grade_b"]
    c1, c2 = gcfg["tier1_pct"], gcfg["tier2_pct"]
    cap = gcfg.get("cap_low_coverage_at", "2軍")
    min_n = gcfg.get("min_group_for_tiers", 6)
    floor2 = gcfg.get("score_floor_tier2")
    excl_groups = set(gcfg.get("exclude_groups", []))
    prev = load_prev(m["out_dir"])

    univ = json.load(open(os.path.join(HERE, m["universe"]), encoding="utf-8"))
    items = univ.get("tickers") or univ.get("codes") or []

    watch, watch_new = {}, {}
    if m.get("watch"):
        wp = os.path.join(HERE, m["watch"])
        if os.path.isfile(wp):
            try:
                wd = json.load(open(wp, encoding="utf-8"))
                watch = wd.get("excluded", {}) or {}
                watch_new = wd.get("new", {}) or {}
            except Exception:
                watch, watch_new = {}, {}

    rows, excluded, missing = [], [], []
    for it in items:
        code = str(it.get("ticker") or it.get("code"))
        s = load_summary(code, m["sumdir"])
        if s is None:
            missing.append(code)
            continue
        groups = s.get("groups", {}) or {}
        q = s.get("q_score")
        sec = s.get(m["seckey"]) or it.get(m["seckey"]) or ""
        grp = gmap.get(sec, "その他")
        if grp in excl_groups:
            q = None
        if code in watch:
            q = None
        rec = {
            "code": code, "name": s.get("name") or it.get("name") or code,
            "sector": sec, "group": grp,
            "q": q, "perf": groups.get("業績"), "fin": groups.get("財務"),
            "cf": groups.get("キャッシュフロー"),
            "bt": s.get("bt_score"), "bt_cov": s.get("bt_cov"),
            "ev_ebit": s.get("ev_ebit"), "fcf_yield": s.get("fcf_yield"),
            "yield": s.get("div_yield"), "price": s.get("price"),
            "price_date": s.get("price_date"),
            "is_simple": s.get("is_simple"), "is_reit": s.get("is_reit"),
            "asof": s.get("_generated_at"), "new": code in watch_new,
        }
        if q is None:
            if code in watch:
                rec["why"] = watch[code].get("reason", "対象外（月次チェック検知）")
            elif s.get("is_reit") or grp == "Real Estate":
                rec["why"] = "REIT・不動産（採点対象外）"
            elif s.get("is_simple"):
                rec["why"] = "金融（銀行・保険・証券／採点対象外）"
            else:
                rec["why"] = "データ不足（新規上場・決算データ未整備等）"
            excluded.append(rec)
        else:
            rec["cov"] = s.get("q_cov") or "―"
            rec["dir"] = direction(q, prev.get(code))
            rows.append(rec)

    # 業種グループごとに軍分け
    groups_out = []
    for gname in list(gcfg["groups"].keys()):
        gr = [r for r in rows if r["group"] == gname]
        if not gr:
            continue
        gr.sort(key=lambda r: -r["q"])
        n = len(gr)
        tiered = n >= min_n
        meds = [r["q"] for r in gr]
        median = round(st.median(meds), 1) if meds else None
        for i, r in enumerate(gr):
            t = tier_of(i, n, c1, c2) if tiered else "―"
            if floor2 and isinstance(r["q"], (int, float)) and r["q"] >= floor2 and t == "3軍":
                t = "2軍"
            if t == "1軍" and r["cov"] == "低":
                t = cap
            r["tier"] = t
        groups_out.append({"name": gname, "grade": grade_of(median, ga, gb),
                           "median": median, "count": n, "tiered": tiered, "stocks": gr})

    global_top = sorted(rows, key=lambda r: -r["q"])[:50]
    excluded.sort(key=lambda r: (r["group"], r["name"]))
    tt = tuple(LC.load_bt_cfg()[f"tim_tiers_{market}"])
    bt_vals = [r["bt"] for r in rows if isinstance(r.get("bt"), (int, float))]
    bt_counts = {
        "買い場": sum(1 for v in bt_vals if v >= tt[0]),
        "妥当": sum(1 for v in bt_vals if tt[1] <= v < tt[0]),
        "やや割高": sum(1 for v in bt_vals if tt[2] <= v < tt[1]),
        "割高": sum(1 for v in bt_vals if v < tt[2]),
    }

    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "market": market,
        "universe": {"count": univ.get("count"), "generated_at": univ.get("generated_at")},
        "weights": QUALITY_W,
        "counts": {
            "total": len(rows), "excluded": len(excluded), "missing": len(missing),
            "1軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "1軍"),
            "2軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "2軍"),
            "3軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "3軍"),
        },
        "buytiming_counts": bt_counts, "tim_tiers": list(tt),
        "groups": groups_out, "global_top": global_top, "excluded": excluded,
        "missing": sorted(missing),
    }
    _print_dist(out)
    return out, m


def _print_dist(out):
    allq = sorted(s["q"] for g in out["groups"] for s in g["stocks"])
    allbt = sorted(s["bt"] for g in out["groups"] for s in g["stocks"]
                   if isinstance(s.get("bt"), (int, float)))
    for lab, xs in (("品質", allq), ("買い時", allbt)):
        if xs:
            p = lambda k, xs=xs: xs[min(len(xs) - 1, int(len(xs) * k))]
            print(f"  {lab}スコア分布 n={len(xs)}  min {xs[0]:.1f}  p10 {p(.1):.1f}  "
                  f"p25 {p(.25):.1f}  median {st.median(xs):.1f}  p60 {p(.6):.1f}  "
                  f"p75 {p(.75):.1f}  max {xs[-1]:.1f}")
    print("  グループ中央値:", {g["name"]: g["median"] for g in out["groups"]})
    print(f"  {out['counts']}  買い時内訳 {out['buytiming_counts']}")


# ---------------------------------------------------------------- HTML
def _report_href(code, report_dirs):
    for rel, absdir in report_dirs:
        if os.path.isfile(os.path.join(absdir, f"{code}.html")):
            return rel + code + ".html"
    return None


def _code_cell(code, report_dirs):
    href = _report_href(code, report_dirs)
    return f'<a href="{href}">{code}</a>' if href else str(code)


def render(out, m):
    th = m["theme"]
    gen = out["generated_at"]
    c = out["counts"]
    unit = m["unit_price"]
    terms_json = json.dumps(TERMS, ensure_ascii=False)

    tt = tuple(LC.load_bt_cfg()[f"tim_tiers_{out['market']}"])

    def _bt_cell(v):
        if not isinstance(v, (int, float)):
            return '<td class="n bt">―</td>'
        cls = "t1" if v >= tt[0] else "t2" if v >= tt[1] else "t3" if v >= tt[2] else "t4"
        return f'<td class="n bt {cls}" data-v="{v}"><b>{v:.0f}</b></td>'

    def row_html(s, with_rank=None):
        tcls = th.TIER_CLASS.get(s.get("tier", "―"), "t0")
        dcls = th.DIR_CLASS.get(s.get("dir", "→"), "fl")
        codecell = _code_cell(s["code"], m["report_dirs"])
        rk = f'<td class="n">{with_rank}</td>' if with_rank is not None else ""
        return (
            f'<tr class="{tcls} r" data-tier="{s.get("tier","―")}">'
            + rk +
            f'<td class="wl"><input type="checkbox" class="wlc" data-code="{s["code"]}" aria-label="ウォッチ"></td>'
            f'<td class="pf"><input type="checkbox" class="pfc" data-code="{s["code"]}" aria-label="ポートフォリオに追加"></td>'
            f'<td class="tier">{s.get("tier","―")}<span class="dir {dcls}">{s.get("dir","")}</span></td>'
            f'<td class="code">{codecell}</td>'
            f'<td class="nm">{html.escape(str(s["name"]))}'
            f'{" <span class=\"newbadge\" title=\"月次チェックで新規追加（次の四半期見直しで正式反映）\">NEW!</span>" if s.get("new") else ""}'
            f'</td>'
            f'<td class="sec">{html.escape(str(s["sector"]))}</td>'
            f'<td class="n" data-v="{_v(s["q"])}"><b>{_num(s["q"],0)}</b></td>'
            f'<td class="n" data-v="{_v(s["perf"])}">{_num(s["perf"],0)}</td>'
            f'<td class="n" data-v="{_v(s["fin"])}">{_num(s["fin"],0)}</td>'
            f'<td class="n" data-v="{_v(s["cf"])}">{_num(s["cf"],0)}</td>'
            + _bt_cell(s.get("bt")) +
            f'<td class="n" data-v="{_v(s["yield"])}">{_num(s["yield"],2)}%</td>'
            f'<td class="n px" data-v="{_v(s["price"])}">{_price(s["price"], unit)}</td>'
            f'<td class="cv">{s.get("cov","―")}</td>'
            f'</tr>')

    thead = ('<thead><tr>{rk}<th class="wl" title="ウォッチリストに追加する銘柄にチェック">☑</th>'
             '<th class="pf" title="ポートフォリオに追加する銘柄にチェック">💼</th>'
             '<th class="hdr" data-term="tier">軍</th><th>コード</th><th>銘柄</th><th>業種</th>'
             '<th class="n hdr" data-term="q">品質</th>'
             '<th class="n hdr" data-term="perf">業績</th>'
             '<th class="n hdr" data-term="fin">財務</th>'
             '<th class="n hdr" data-term="cf">CF</th>'
             '<th class="n hdr" data-term="bt">買い時</th>'
             '<th class="n hdr" data-term="yield">利回り</th>'
             '<th class="n hdr" data-term="price">終値</th>'
             '<th class="hdr" data-term="cov">カバレッジ</th></tr></thead>')

    secs = []
    for g in out["groups"]:
        head = (f'<h2>{html.escape(g["name"])} '
                f'<span class="grade grade{g["grade"]} hdr" data-term="grade">業種級 {g["grade"]}</span> '
                f'<span class="gmeta">中央値 {_num(g["median"])} ／ {g["count"]}銘柄'
                f'{"" if g["tiered"] else " ・ 少数のため軍分けなし"}</span></h2>')
        trs = "".join(row_html(s) for s in g["stocks"])
        secs.append('<section class="grp">' + head + '<table>'
                    + thead.format(rk="") + '<tbody>' + trs + '</tbody></table></section>')

    gt = "".join(row_html(s, with_rank=i + 1) for i, s in enumerate(out["global_top"]))

    exc = ""
    if out["excluded"]:
        etr = "".join(
            f'<tr class="r"><td class="code">{_code_cell(s["code"], m["report_dirs"])}</td>'
            f'<td class="nm">{html.escape(str(s["name"]))}'
            f'{" <span class=\"newbadge\" title=\"月次チェックで新規追加（次の四半期見直しで正式反映）\">NEW!</span>" if s.get("new") else ""}'
            f'</td>'
            f'<td class="sec">{html.escape(str(s["sector"]))}</td>'
            f'<td>{html.escape(s["why"])}</td>'
            f'<td class="n" data-v="{_v(s["yield"])}">{_num(s["yield"],2)}%</td>'
            f'<td class="n px" data-v="{_v(s["price"])}">{_price(s["price"], unit)}</td></tr>'
            for s in out["excluded"])
        exc = ('<h2>対象外（金融・REIT）<span class="gmeta">'
               f'{len(out["excluded"])}銘柄</span></h2>'
               '<p class="sub">銀行・保険・証券・REITは、自己資本比率やキャッシュフローが'
               '業種として構造的に異質で、現バージョンでは業績・財務・CFを採点していません。'
               '配当を除いた共通スコアで比較できないため、順位付けの対象外としています'
               '（利回りと終値のみ参考表示）。</p>'
               '<section class="grp"><table><thead><tr><th>コード</th><th>銘柄</th>'
               '<th>業種</th><th>区分</th><th class="n">利回り</th>'
               f'<th class="n">終値</th></tr></thead><tbody>{etr}</tbody></table></section>')

    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{th.THEME_HEAD}
<title>{html.escape(m["title"])}</title>
<style>
{th.THEME_CSS}
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.6 -apple-system,"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;
  background:var(--bg);color:var(--fg)}}
.wrap{{max-width:1040px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:12px}}
.sub a{{color:var(--accent)}}
.summary{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0 10px}}
.sumbtn{{font:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:8px 14px;min-width:76px;text-align:center;cursor:pointer;color:inherit}}
.sumbtn:hover{{border-color:var(--accent)}}
.sumbtn.active{{border-color:var(--accent);border-width:2px;background:var(--info)}}
.sumbtn b{{display:block;font-size:19px}}
h2{{font-size:15px;margin:26px 0 6px;border-bottom:2px solid var(--line);padding-bottom:4px}}
.grade{{font-size:11px;padding:1px 7px;border-radius:10px;vertical-align:middle}}
.newbadge{{font-size:10px;font-weight:700;padding:1px 6px;border-radius:10px;vertical-align:middle;
  background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent);
  border:1px solid color-mix(in srgb,var(--accent) 45%,transparent)}}
.gradeA{{background:color-mix(in srgb,var(--gA) 15%,transparent);color:var(--gA)}}
.gradeB{{background:color-mix(in srgb,var(--gB) 15%,transparent);color:var(--gB)}}
.gradeC{{background:color-mix(in srgb,var(--gC) 15%,transparent);color:var(--gC)}}
.grade―{{background:color-mix(in srgb,var(--muted) 15%,transparent);color:var(--muted)}}
.gmeta{{font-size:11px;color:var(--muted);font-weight:normal;margin-left:6px}}
table{{width:100%;border-collapse:collapse;background:var(--card);font-size:13px;
  border:1px solid var(--line);border-radius:8px;overflow:hidden}}
th,td{{padding:6px 8px;text-align:left;border-bottom:1px solid var(--line)}}
th{{background:var(--th);font-size:11px;color:var(--muted)}}
td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums}}
tr:last-child td{{border-bottom:none}}
.tier{{font-weight:700;white-space:nowrap}}
.t1 .tier{{color:var(--t1)}}.t2 .tier{{color:var(--t2)}}.t3 .tier{{color:var(--t3)}}
.dir{{font-weight:700;margin-left:3px}}
.dir.up{{color:var(--t1)}}.dir.dn{{color:var(--gC)}}.dir.fl{{color:var(--t3)}}
.code a{{color:var(--accent);text-decoration:none}}
.sec{{color:var(--muted);font-size:11px}}
.cv{{font-size:11px}}
td.bt.t1,td.bt.t1 b{{color:var(--t1)}}
td.bt.t2,td.bt.t2 b{{color:var(--t2)}}
td.bt.t3,td.bt.t3 b{{color:var(--t3)}}
td.bt.t4,td.bt.t4 b{{color:var(--gC)}}
td.wl,th.wl,td.pf,th.pf{{width:30px;text-align:center;padding-left:2px;padding-right:2px}}
th.wl,th.pf{{cursor:default}}
.wlc,.pfc{{width:15px;height:15px;cursor:pointer;accent-color:var(--accent)}}
#wlbar{{position:fixed;left:0;right:0;bottom:0;z-index:20;display:flex;gap:10px;
  align-items:center;justify-content:center;flex-wrap:wrap;background:var(--card);
  border-top:1px solid var(--line);box-shadow:0 -2px 10px rgba(0,0,0,.06);
  padding:10px 14px;font-size:13px}}
#wlbar b{{color:var(--accent)}}
#wlbar button{{padding:8px 16px;border:1px solid var(--accent);border-radius:8px;
  background:var(--accent);color:#fff;font-size:13px;cursor:pointer}}
#wlbar button.ghost{{background:var(--card);color:var(--muted);border-color:var(--line)}}
#wlbar[hidden]{{display:none}}
body.wlon{{padding-bottom:60px}}
.hdr{{cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px}}
.hdr:hover{{color:var(--accent)}}
a.sumbtn.wlnav{{border-color:var(--accent);color:var(--accent);text-decoration:none}}
a.sumbtn.wlnav b{{color:var(--accent)}}
.terminfo{{position:relative;margin:8px 0 18px;padding:12px 36px 12px 14px;background:var(--info);
  border:1px solid var(--accent);border-radius:8px;font-size:12.5px;line-height:1.7}}
.terminfo b{{display:block;margin-bottom:4px;font-size:13.5px}}
.terminfo[hidden]{{display:none}}
.ticlose{{position:absolute;top:6px;right:8px;border:none;background:none;cursor:pointer;
  font-size:15px;line-height:1;color:var(--muted);padding:4px}}
.disc{{margin-top:30px;padding:12px;background:var(--wbg);border:1px solid var(--wbd);
  border-radius:8px;font-size:11.5px;color:var(--wfg)}}
.searchbar{{display:flex;align-items:center;gap:8px;margin:14px 0 4px}}
.searchbar input{{flex:1;max-width:360px;padding:8px 10px;border:1px solid var(--line);
  border-radius:8px;font-size:13.5px;background:var(--card);color:inherit}}
.searchbar button{{padding:8px 12px;border:1px solid var(--line);border-radius:8px;
  background:var(--card);font-size:12.5px;cursor:pointer;color:var(--muted)}}
.searchbar .hit{{font-size:12px;color:var(--muted)}}
details{{margin:14px 0}}summary{{cursor:pointer;font-weight:600;font-size:13px}}
tr[hidden]{{display:none}}
section.grp[hidden]{{display:none}}
.topbar{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}}
.topbar a{{font-size:12.5px;color:var(--accent);white-space:nowrap}}
.pagenav{{display:flex;gap:14px;flex-wrap:wrap}}
.formula{{font-size:11.5px;color:var(--muted);background:var(--field);border:1px solid var(--line);
  border-radius:8px;padding:8px 12px;margin:6px 0 4px}}
</style></head><body><div class="wrap">
<div class="topbar"><h1>{html.escape(m["title"])}</h1>{_pagenav("index.html", exclude=("watchlist.html", "portfolio.html"))}</div>
{th.THEME_BAR}
<div class="sub">生成 {gen}　｜　{html.escape(m["screen_line"])}</div>
<div class="formula">品質スコア ＝ 業績×0.28 ＋ 財務×0.27 ＋ キャッシュフロー×0.15（取得できた
グループだけで再正規化・0〜110）。既存の銘柄診断エンジンの「銘柄選定スコア」から
<b>配当の持続力（連続増配・増配率・配当性向・累進配当宣言など）を除いた</b>もの。
利回りは参考表示で採点に不使用。</div>
<div class="summary">
  <button type="button" class="sumbtn" data-tier=""><b>{c['total']}</b>銘柄</button>
  <button type="button" class="sumbtn" data-tier="1軍"><b>{c['1軍']}</b>1軍</button>
  <button type="button" class="sumbtn" data-tier="2軍"><b>{c['2軍']}</b>2軍</button>
  <button type="button" class="sumbtn" data-tier="3軍"><b>{c['3軍']}</b>3軍</button>
  <span class="sumbtn" style="cursor:default"><b>{c['excluded']}</b>対象外</span>
  <a class="sumbtn wlnav" href="watchlist.html"><b>☆</b>ウォッチリスト</a>
  <a class="sumbtn wlnav" href="portfolio.html"><b id="pfnav-n">💼</b>ポートフォリオ</a>
</div>
<div class="sub" style="margin:-4px 0 12px">見出し（軍・品質・業績・財務・CF・買い時・利回り・カバレッジ・業種級）をクリックすると説明が出ます。数字ボタンでその軍だけ表示（もう一度で解除）。☆にチェックを入れて下部の「ウォッチリストを作成」を、💼にチェックを入れて「ポートフォリオに追加」を押すと、それぞれ選んだ銘柄だけの一覧・保有記録の入力画面を作れます（☆と💼は別々に選べます）。</div>
<div id="terminfo" class="terminfo" hidden>
  <button type="button" id="terminfo-close" class="ticlose" aria-label="閉じる">✕</button>
  <div id="terminfo-body"></div>
</div>
<div class="searchbar">
  <input id="q" type="search" placeholder="コード・銘柄名・業種で検索" autocomplete="off">
  <button id="qclear" type="button">クリア</button>
  <span class="hit" id="qhit"></span>
</div>
<details id="topbox"><summary>全体 品質スコア 上位50（業種横断）</summary>
<table><thead><tr><th class="n">#</th><th class="wl">☑</th><th class="pf">💼</th>
<th class="hdr" data-term="tier">軍</th><th>コード</th><th>銘柄</th><th>業種</th>
<th class="n hdr" data-term="q">品質</th><th class="n hdr" data-term="perf">業績</th>
<th class="n hdr" data-term="fin">財務</th><th class="n hdr" data-term="cf">CF</th>
<th class="n hdr" data-term="bt">買い時</th><th class="n hdr" data-term="yield">利回り</th>
<th class="n hdr" data-term="price">終値</th><th class="hdr" data-term="cov">カバレッジ</th></tr></thead>
<tbody>{gt}</tbody></table></details>
{"".join(secs)}
{exc}
<div class="disc">{DISC}</div>
<div id="wlbar" hidden><span>☆ <b id="wlcount">0</b>銘柄</span>
<button type="button" id="wlgo">ウォッチリストを作成 →</button>
<span>💼 <b id="pfcount">0</b>銘柄</span>
<button type="button" id="pfgo">ポートフォリオに追加 →</button>
<button type="button" id="wlclear" class="ghost">選択をクリア</button></div>
<script>
{th.THEME_JS}
(function(){{
  var q=document.getElementById('q'),hit=document.getElementById('qhit');
  var btns=document.querySelectorAll('.sumbtn[data-tier]'),activeTier='';
  function apply(){{
    var needle=(q.value||'').trim().normalize('NFKC').toLowerCase();
    var n=0;
    document.querySelectorAll('tr.r').forEach(function(tr){{
      var okT=!needle||tr.textContent.normalize('NFKC').toLowerCase().indexOf(needle)!==-1;
      var okTier=!activeTier||tr.dataset.tier===activeTier;
      var show=okT&&okTier;tr.hidden=!show;if(show)n++;
    }});
    document.querySelectorAll('section.grp').forEach(function(sec){{
      var any=sec.querySelector('tbody tr.r:not([hidden])');sec.hidden=!any;
    }});
    hit.textContent=(needle||activeTier)?(n+'件'):'';
  }}
  q.addEventListener('input',apply);
  document.getElementById('qclear').addEventListener('click',function(){{q.value='';apply();}});
  btns.forEach(function(b){{b.addEventListener('click',function(){{
    var t=b.dataset.tier;activeTier=(activeTier===t)?'':t;
    btns.forEach(function(x){{x.classList.toggle('active',x.dataset.tier===activeTier&&activeTier!=='');}});
    apply();
  }});}});
  // ---- ウォッチリスト／ポートフォリオ選択（別々のチェックボックス列）。選択状態は
  // localStorageに保存し、ウォッチリスト／ポートフォリオページへ移動して戻っても維持する ----
  function loadCodes(key){{
    try {{ return new Set((localStorage.getItem(key)||'').split(',').map(function(s){{return s.trim();}}).filter(Boolean)); }}
    catch(e) {{ return new Set(); }}
  }}
  var WL_KEY='wl_codes_long{"_us" if out["market"]=="us" else ""}';
  var PF_SEL_KEY='pf_sel_codes_long{"_us" if out["market"]=="us" else ""}';
  var wlSet=loadCodes(WL_KEY), pfSet=loadCodes(PF_SEL_KEY);
  var bar=document.getElementById('wlbar'),cnt=document.getElementById('wlcount');
  var pfCnt=document.getElementById('pfcount'),pfNav=document.getElementById('pfnav-n');
  function wlSave(){{ try {{ localStorage.setItem(WL_KEY, Array.from(wlSet).join(',')); }} catch(e) {{}} }}
  function pfSave(){{ try {{ localStorage.setItem(PF_SEL_KEY, Array.from(pfSet).join(',')); }} catch(e) {{}} }}
  function sync(){{
    cnt.textContent=wlSet.size;
    pfCnt.textContent=pfSet.size;
    if(pfNav)pfNav.textContent=pfSet.size?String(pfSet.size):'💼';
    bar.hidden=(wlSet.size===0&&pfSet.size===0);
    document.body.classList.toggle('wlon',wlSet.size>0||pfSet.size>0);
  }}
  function wlSync(){{
    document.querySelectorAll('.wlc').forEach(function(cb){{cb.checked=wlSet.has(cb.dataset.code);}});
    document.querySelectorAll('.pfc').forEach(function(cb){{cb.checked=pfSet.has(cb.dataset.code);}});
    sync();
  }}
  document.querySelectorAll('.wlc').forEach(function(cb){{
    cb.addEventListener('change',function(){{
      var c=cb.dataset.code;
      if(cb.checked)wlSet.add(c);else wlSet.delete(c);
      document.querySelectorAll('.wlc[data-code="'+c+'"]').forEach(function(o){{o.checked=cb.checked;}});
      sync();wlSave();
    }});
  }});
  document.querySelectorAll('.pfc').forEach(function(cb){{
    cb.addEventListener('change',function(){{
      var c=cb.dataset.code;
      if(cb.checked)pfSet.add(c);else pfSet.delete(c);
      document.querySelectorAll('.pfc[data-code="'+c+'"]').forEach(function(o){{o.checked=cb.checked;}});
      sync();pfSave();
    }});
  }});
  document.getElementById('wlclear').addEventListener('click',function(){{
    wlSet.clear();pfSet.clear();wlSave();pfSave();
    document.querySelectorAll('.wlc,.pfc').forEach(function(o){{o.checked=false;}});
    sync();
  }});
  document.getElementById('wlgo').addEventListener('click',function(){{
    if(!wlSet.size)return;
    wlSave();
    location.href='watchlist.html?codes='+encodeURIComponent(Array.from(wlSet).join(','));
  }});
  document.getElementById('pfgo').addEventListener('click',function(){{
    if(!pfSet.size)return;
    pfSave();
    location.href='portfolio.html?add='+encodeURIComponent(Array.from(pfSet).join(','));
  }});
  wlSync();
  // ---- 見出しクリックで用語説明 ----
  var TERMS={terms_json};
  var tibox=document.getElementById('terminfo'),tibody=document.getElementById('terminfo-body');
  document.querySelectorAll('.hdr').forEach(function(el){{
    el.addEventListener('click',function(e){{
      e.stopPropagation();
      var t=TERMS[el.dataset.term];if(!t)return;
      tibody.innerHTML='<b>'+t[0]+'</b>'+t[1];
      tibox.hidden=false;tibox.scrollIntoView({{behavior:'smooth',block:'nearest'}});
    }});
  }});
  document.getElementById('terminfo-close').addEventListener('click',function(){{tibox.hidden=true;}});
}})();
</script>
</div></body></html>"""


def render_watchlist(out, m):
    """?codes=A,B,C を ranking.json から引いて表にするだけの静的ページ。"""
    th = m["theme"]
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{th.THEME_HEAD}
<title>ウォッチリスト｜{html.escape(m['title'])}</title>
<style>
{th.THEME_CSS}
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.6 -apple-system,"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;
  background:var(--bg);color:var(--fg)}}
.wrap{{max-width:900px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:19px;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:12px}} .sub a{{color:var(--accent)}}
.topbar{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}}
.topbar a{{font-size:12.5px;color:var(--accent);white-space:nowrap}}
.pagenav{{display:flex;gap:14px;flex-wrap:wrap}}
.box{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:12px 0}}
.box h2{{font-size:14px;margin:0 0 8px}}
.codewrap{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}}
#codestr{{flex:1;min-width:220px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
  font:12px/1.4 monospace;background:var(--field);color:var(--fg)}}
#copystat{{font-size:12px;color:var(--gA)}}
textarea{{width:100%;min-height:52px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
  font:12px/1.5 monospace;background:var(--field);color:var(--fg);resize:vertical}}
button{{padding:7px 13px;border:1px solid var(--accent);border-radius:8px;background:var(--accent);
  color:#fff;font-size:12.5px;cursor:pointer;margin-right:6px}}
button.ghost{{background:var(--card);color:var(--muted);border-color:var(--line)}}
table{{width:100%;border-collapse:collapse;background:var(--card);font-size:13px;
  border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-top:8px}}
th,td{{padding:6px 8px;text-align:left;border-bottom:1px solid var(--line)}}
th{{background:var(--th);font-size:11px;color:var(--muted)}}
td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums}}
tr:last-child td{{border-bottom:none}}
.code a{{color:var(--accent);text-decoration:none}}
.sec{{color:var(--muted);font-size:11px}}
td.bt.t1{{color:var(--t1)}} td.bt.t2{{color:var(--t2)}} td.bt.t3{{color:var(--t3)}} td.bt.t4{{color:var(--gC)}}
.empty{{color:var(--muted);font-size:13px;padding:14px 0}}
</style></head><body><div class="wrap">
<div class="topbar"><h1>ウォッチリスト</h1>{_pagenav("watchlist.html")}</div>
<div class="sub">{html.escape(m['title'])}</div>

<div class="box">
  <h2>あなたの銘柄リスト</h2>
  <div class="codewrap">
    <input id="codestr" readonly value="">
    <button type="button" id="copy">コピー</button>
    <span id="copystat"></span>
  </div>
  <p>この<b>銘柄リストをコピーして控えておく</b>と、別の端末でも下の貼り付け欄から復元できます。</p>
  <p><b>同じ端末・同じブラウザ</b>なら、一度作成すれば次回からこのページを開くだけで復元されます（貼り付け不要）。</p>
</div>

<div class="box">
  <h2>銘柄リストを貼り付けて表示</h2>
  <p class="sub" style="margin:0 0 8px">コード／ティッカーをカンマ・空白・改行区切りで。URL の <code>?codes=</code> でも可。</p>
  <textarea id="paste" placeholder="例：7203, 9433, 6146"></textarea>
  <div style="margin-top:8px">
    <button type="button" id="show">表示</button>
    <button type="button" id="clear" class="ghost">クリア</button>
  </div>
</div>

<div id="tbl"></div>
{th.THEME_BAR}
<script>
{th.THEME_JS}
(function(){{
  var params=new URLSearchParams(location.search);
  var LS='pp_wl_long_{out["market"]}';
  var codes=(params.get('codes')||localStorage.getItem(LS)||'').split(/[\\s,]+/).filter(Boolean);
  codes=Array.from(new Set(codes));
  try{{localStorage.setItem(LS,codes.join(','));}}catch(e){{}}
  var codestr=document.getElementById('codestr');
  codestr.value=codes.join(',');
  document.getElementById('copy').onclick=function(){{
    codestr.select();
    var ok=false;
    try{{ok=document.execCommand('copy');}}catch(e){{}}
    if(navigator.clipboard){{navigator.clipboard.writeText(codestr.value).then(function(){{}},function(){{}});ok=true;}}
    var st=document.getElementById('copystat');
    st.textContent=ok?'コピーしました':'手動でコピーしてください';
    setTimeout(function(){{st.textContent='';}},2500);
  }};
  document.getElementById('show').onclick=function(){{
    var v=(document.getElementById('paste').value||'').split(/[\\s,]+/).filter(Boolean);
    v=Array.from(new Set(v.map(function(x){{return x.toUpperCase();}})));
    if(!v.length)return;
    try{{localStorage.setItem(LS,v.join(','));}}catch(e){{}}
    location.href='watchlist.html?codes='+encodeURIComponent(v.join(','));
  }};
  document.getElementById('clear').onclick=function(){{
    if(!codes.length)return;
    if(!confirm('銘柄リストをすべて削除します。よろしいですか？（事前に上の文字列を控えてください）'))return;
    try{{localStorage.removeItem(LS);}}catch(e){{}}location.href='watchlist.html';
  }};
  var tbl=document.getElementById('tbl');
  if(!codes.length){{tbl.innerHTML='<div class="empty">上の欄に銘柄コードを貼り付けて「表示」を押すか、ランキングで銘柄を選んで「ウォッチリストを作成」すると、ここに一覧が出ます。</div>';return;}}
  fetch('ranking.json').then(function(r){{return r.json();}}).then(function(j){{
    var map={{}};
    (j.groups||[]).forEach(function(g){{(g.stocks||[]).forEach(function(s){{s._grade=g.grade;s._gname=g.name;map[s.code]=s;}});}});
    (j.excluded||[]).forEach(function(s){{map[s.code]=s;s._exc=true;}});
    var tt=(j.tim_tiers||[72,57,45]);
    function btcls(v){{return v>=tt[0]?'t1':v>=tt[1]?'t2':v>=tt[2]?'t3':'t4';}}
    var rows=codes.map(function(c){{
      var s=map[c];
      if(!s)return '<tr><td class="code">'+c+'</td><td colspan="7" class="sec">ランキングに見つかりません（対象外・母集団外）</td></tr>';
      var bt=(typeof s.bt==='number')?'<td class="n bt '+btcls(s.bt)+'"><b>'+s.bt.toFixed(0)+'</b></td>':'<td class="n">―</td>';
      var q=(typeof s.q==='number')?s.q.toFixed(0):(s._exc?'対象外':'―');
      var yld=(typeof s.yield==='number')?s.yield.toFixed(2)+'%':'―';
      var px=(typeof s.price==='number')?s.price.toLocaleString():'―';
      return '<tr><td class="code"><a href="reports/'+c+'.html">'+c+'</a></td>'
        +'<td>'+(s.name||'')+'</td><td class="sec">'+(s.sector||s._gname||'')+'</td>'
        +'<td class="n"><b>'+q+'</b></td>'+bt
        +'<td class="n">'+yld+'</td><td class="n">'+px+'</td>'
        +'<td>'+(s.tier||(s._exc?'―':''))+'</td></tr>';
    }}).join('');
    tbl.innerHTML='<table><thead><tr><th>コード</th><th>銘柄</th><th>業種</th>'
      +'<th class="n">品質</th><th class="n">買い時</th><th class="n">利回り</th>'
      +'<th class="n">終値</th><th>軍</th></tr></thead><tbody>'+rows+'</tbody></table>';
  }}).catch(function(){{tbl.innerHTML='<div class="empty">ranking.json を読み込めませんでした。</div>';}});
}})();
</script>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["jp", "us"])
    args = ap.parse_args()
    out, m = build(args.market)
    os.makedirs(m["out_dir"], exist_ok=True)
    new_html = render(out, m)
    open(os.path.join(m["out_dir"], "watchlist.html"), "w",
         encoding="utf-8").write(render_watchlist(out, m))

    terms_src = os.path.join(HERE, m["terms_src"])
    if os.path.isfile(terms_src):
        shutil.copyfile(terms_src, os.path.join(m["out_dir"], "terms.html"))
    guide_src = os.path.join(HERE, m["guide_src"])
    if os.path.isfile(guide_src):
        shutil.copyfile(guide_src, os.path.join(m["out_dir"], "guide.html"))
    portfolio_src = os.path.join(HERE, m["portfolio_src"])
    if os.path.isfile(portfolio_src):
        shutil.copyfile(portfolio_src, os.path.join(m["out_dir"], "portfolio.html"))

    idx = os.path.join(m["out_dir"], "index.html")
    rjson = os.path.join(m["out_dir"], "ranking.json")

    def _mask(t, ts):
        return t.replace(ts, "GENAT", 1) if ts else t
    old = open(idx, encoding="utf-8").read() if os.path.isfile(idx) else None
    prev_gen = None
    if os.path.isfile(rjson):
        try:
            prev_gen = json.load(open(rjson, encoding="utf-8")).get("generated_at")
        except Exception:
            prev_gen = None
    if old is not None and _mask(old, prev_gen) == _mask(new_html, out["generated_at"]):
        print(f"変化なし -> 書き込みスキップ  {out['counts']}")
        return

    json.dump(out, open(rjson, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(idx, "w", encoding="utf-8").write(new_html)
    print(f"-> {idx}\n-> {rjson}")


if __name__ == "__main__":
    main()
