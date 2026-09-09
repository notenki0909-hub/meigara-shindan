# -*- coding: utf-8 -*-
"""
『10年保有できる優良企業』ランキング（配当を評価しない品質スコア版）を生成する。
配当株ランキング（rank.py / rank_us.py）とは独立。既存ファイルは書き換えない。

  python rank_long.py jp     → site/long/index.html      + site/long/ranking.json
  python rank_long.py us     → site/us/long/index.html   + site/us/long/ranking.json

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
import statistics as st

import rank as _jp          # THEME_*, TIER_CLASS, DIR_CLASS を流用（配色をサイトと統一）
import rank_us as _us

HERE = os.path.dirname(os.path.abspath(__file__))

# sel_score の score_groups.選定 から「配当の持続力」を除いたもの
QUALITY_W = {"業績": 0.28, "財務": 0.27, "キャッシュフロー": 0.15}

MK = {
    "jp": {
        "theme": _jp,
        "universe": "universe_long.json",
        "sumdirs": [os.path.join(HERE, "site", "summaries"),
                    os.path.join(HERE, "site", "long_summaries")],
        "groups_cfg": "sector_groups_long.json",
        "seckey": "jp_sector",
        "out_dir": os.path.join(HERE, "site", "long"),
        "report_dirs": [("reports/", os.path.join(HERE, "site", "long", "reports")),
                        ("../reports/", os.path.join(HERE, "site", "reports"))],
        "title": "10年保有できる優良企業ランキング（日本株）",
        "screen_line": "母集団＝時価総額3,000億円以上（TOPIX500相当）／金融・REITを除く。"
                       "配当利回りは条件に使わず表示のみ。",
        "nav": [("../index.html", "配当株ランキング（日本）"),
                ("../us/long/index.html", "10年保有（米国株）")],
        "unit_price": "円",
    },
    "us": {
        "theme": _us,
        "universe": "universe_long_us.json",
        "sumdirs": [os.path.join(HERE, "site", "us", "summaries"),
                    os.path.join(HERE, "site", "us", "long_summaries")],
        "groups_cfg": "sector_groups_long_us.json",
        "seckey": "gics_sector",
        "out_dir": os.path.join(HERE, "site", "us", "long"),
        "report_dirs": [("reports/", os.path.join(HERE, "site", "us", "long", "reports")),
                        ("../reports/", os.path.join(HERE, "site", "us", "reports"))],
        "title": "10年保有できる優良企業ランキング（米国株）",
        "screen_line": "母集団＝S&P500 メンバーシップ（黒字継続・流動性・業種代表性を"
                       "委員会が審査済み）／金融・REITは対象外。配当利回りは表示のみ。",
        "nav": [("../index.html", "配当株ランキング（米国）"),
                ("../../long/index.html", "10年保有（日本株）")],
        "unit_price": "$",
    },
}

DISC = ('本ページは、あらかじめ定めた基準（時価総額または指数構成）で抽出した銘柄について、'
        '公開データを機械的なルールで算出した「配当を含めない品質スコア」（業績・財務・'
        'キャッシュフロー）による分類です。配当利回りは参考表示で、採点には使っていません。'
        '銀行・保険・証券・REITは、このスコアの算出対象外のため「対象外」として掲載しています。'
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
def load_summary(code, sumdirs):
    best, best_gen = None, None
    for d in sumdirs:
        p = os.path.join(d, f"{code}.json")
        if not os.path.isfile(p):
            continue
        try:
            s = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        gen = s.get("_generated_at") or ""
        if best is None or gen > (best_gen or ""):
            best, best_gen = s, gen
    return best


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

    rows, excluded, missing = [], [], []
    for it in items:
        code = str(it.get("ticker") or it.get("code"))
        s = load_summary(code, m["sumdirs"])
        if s is None:
            missing.append(code)
            continue
        groups = s.get("groups", {}) or {}
        q = quality_score(groups)
        sec = s.get(m["seckey"]) or it.get(m["seckey"]) or ""
        grp = gmap.get(sec, "その他")
        if grp in excl_groups:
            q = None
        rec = {
            "code": code, "name": s.get("name") or it.get("name") or code,
            "sector": sec, "group": grp,
            "q": q, "perf": groups.get("業績"), "fin": groups.get("財務"),
            "cf": groups.get("キャッシュフロー"),
            "yield": s.get("div_yield"), "price": s.get("price"),
            "price_date": s.get("price_date"),
            "is_simple": s.get("is_simple"), "is_reit": s.get("is_reit"),
            "asof": s.get("_generated_at"),
        }
        if q is None:
            if s.get("is_reit") or grp == "Real Estate":
                rec["why"] = "REIT・不動産（採点対象外）"
            else:
                rec["why"] = "金融（銀行・保険・証券／採点対象外）"
            excluded.append(rec)
        else:
            got, poss, cov = quality_cov(groups)
            rec["cov"] = cov
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
        "groups": groups_out, "global_top": global_top, "excluded": excluded,
        "missing": sorted(missing),
    }
    _print_dist(out)
    return out, m


def _print_dist(out):
    allq = sorted(s["q"] for g in out["groups"] for s in g["stocks"])
    if allq:
        p = lambda k: allq[min(len(allq) - 1, int(len(allq) * k))]
        print(f"  品質スコア分布 n={len(allq)}  min {allq[0]}  p10 {p(.1)}  "
              f"p25 {p(.25)}  median {st.median(allq)}  p75 {p(.75)}  max {allq[-1]}")
    print("  グループ中央値:", {g["name"]: g["median"] for g in out["groups"]})
    print(f"  {out['counts']}")


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

    def row_html(s, with_rank=None):
        tcls = th.TIER_CLASS.get(s.get("tier", "―"), "t0")
        dcls = th.DIR_CLASS.get(s.get("dir", "→"), "fl")
        codecell = _code_cell(s["code"], m["report_dirs"])
        rk = f'<td class="n">{with_rank}</td>' if with_rank is not None else ""
        return (
            f'<tr class="{tcls} r" data-tier="{s.get("tier","―")}">'
            + rk +
            f'<td class="tier">{s.get("tier","―")}<span class="dir {dcls}">{s.get("dir","")}</span></td>'
            f'<td class="code">{codecell}</td>'
            f'<td class="nm">{html.escape(str(s["name"]))}</td>'
            f'<td class="sec">{html.escape(str(s["sector"]))}</td>'
            f'<td class="n" data-v="{_v(s["q"])}"><b>{_num(s["q"],0)}</b></td>'
            f'<td class="n" data-v="{_v(s["perf"])}">{_num(s["perf"],0)}</td>'
            f'<td class="n" data-v="{_v(s["fin"])}">{_num(s["fin"],0)}</td>'
            f'<td class="n" data-v="{_v(s["cf"])}">{_num(s["cf"],0)}</td>'
            f'<td class="n" data-v="{_v(s["yield"])}">{_num(s["yield"],2)}%</td>'
            f'<td class="n px" data-v="{_v(s["price"])}">{_price(s["price"], unit)}</td>'
            f'<td class="cv">{s.get("cov","―")}</td>'
            f'</tr>')

    thead = ('<thead><tr>{rk}<th>軍</th><th>コード</th><th>銘柄</th><th>業種</th>'
             '<th class="n">品質</th><th class="n">業績</th><th class="n">財務</th>'
             '<th class="n">CF</th><th class="n">利回り</th>'
             f'<th class="n">終値</th><th>カバレッジ</th></tr></thead>')

    secs = []
    for g in out["groups"]:
        head = (f'<h2>{html.escape(g["name"])} '
                f'<span class="grade grade{g["grade"]}">業種級 {g["grade"]}</span> '
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
            f'<td class="nm">{html.escape(str(s["name"]))}</td>'
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

    nav = " ・ ".join(f'<a href="{u}">{html.escape(t)}</a>' for u, t in m["nav"])

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
.formula{{font-size:11.5px;color:var(--muted);background:var(--field);border:1px solid var(--line);
  border-radius:8px;padding:8px 12px;margin:6px 0 4px}}
</style></head><body><div class="wrap">
<div class="topbar"><h1>{html.escape(m["title"])}</h1><span class="topbar">{nav}</span></div>
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
</div>
<div class="sub" style="margin:-4px 0 12px">数字ボタンでその軍だけ表示（もう一度で解除）。品質＝配当抜きの質、業績/財務/CF＝その内訳。</div>
<div class="searchbar">
  <input id="q" type="search" placeholder="コード・銘柄名・業種で検索" autocomplete="off">
  <button id="qclear" type="button">クリア</button>
  <span class="hit" id="qhit"></span>
</div>
<details id="topbox"><summary>全体 品質スコア 上位50（業種横断）</summary>
<table><thead><tr><th class="n">#</th><th>軍</th><th>コード</th><th>銘柄</th><th>業種</th>
<th class="n">品質</th><th class="n">業績</th><th class="n">財務</th><th class="n">CF</th>
<th class="n">利回り</th><th class="n">終値</th><th>カバレッジ</th></tr></thead>
<tbody>{gt}</tbody></table></details>
{"".join(secs)}
{exc}
<div class="disc">{DISC}</div>
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
        print(f"変化なし → 書き込みスキップ  {out['counts']}")
        return

    json.dump(out, open(rjson, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(idx, "w", encoding="utf-8").write(new_html)
    print(f"→ {idx}\n→ {rjson}")


if __name__ == "__main__":
    main()
