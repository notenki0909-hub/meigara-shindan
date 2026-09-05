# -*- coding: utf-8 -*-
"""
site/summaries/*.json を読み、業種グループごとに
  - 業種級 A/B/C（グループ内の銘柄選定スコア中央値）
  - 1軍/2軍/3軍（グループ内パーセンタイル。カバレッジ低は2軍止まり）
  - 方向フラグ ↑改善中／→横ばい／↓悪化中（前回 ranking.json との差）
を付けて site/ranking.json と site/index.html を書き出す。

  python rank.py
"""
import datetime as dt
import glob
import html
import json
import os
import shutil
import statistics as st

import analyze

HERE = os.path.dirname(__file__)
SITE = os.path.join(HERE, "site")
SUM = os.path.join(SITE, "summaries")
GROUPS_CFG = os.path.join(HERE, "sector_groups.json")
SCREEN_CFG = os.path.join(HERE, "universe_screen.json")
RANKING = os.path.join(SITE, "ranking.json")
INDEX = os.path.join(SITE, "index.html")

DISC = ('本ページは、あらかじめ定めた基準で抽出した銘柄について、公開データを機械的な'
        'ルールで算出したスコアによる分類（1〜3軍・業種級）です。特定銘柄の売買を推奨・'
        '勧誘するものではなく、運営者は投資助言・代理業の登録を受けていません。教育目的の'
        '一般情報であり、投資判断はご自身の責任で行ってください。数値は yfinance 由来で'
        '誤り・遅延・欠損があり得ます。詳しくは<a href="terms.html">利用規約・免責事項</a>を'
        'ご確認ください。')


def load_group_map(cfg):
    rev = {}
    for g, secs in cfg["groups"].items():
        for s in secs:
            rev[s] = g
    return rev


TIER_RANK = {"1軍": 3, "2軍": 2, "3軍": 1, "―": 0}


def tier_of(rank_idx, n, c1, c2):
    """0始まりの順位 → 軍（グループ内パーセンタイル）。c1,c2 は割合。"""
    n1 = max(1, round(n * c1))
    n2 = max(1, round(n * c2))
    if rank_idx < n1:
        return "1軍"
    if rank_idx < n1 + n2:
        return "2軍"
    return "3軍"


def tier_floor(sel_score):
    """絶対スコアの下駄。analyze.py の SEL_TIERS(85/68/55=長期保有/及第点/質に不安あり)と
    同じ基準で「銘柄選定で長期保有できる配当株と判定された銘柄が、強いグループに
    属しているというだけで3軍に落ちる」のを防ぐ。グループ内で相対的に弱くても
    絶対評価が良ければ2軍を保証。1軍はあくまで『そのグループ内でも上位』の意味を残す
    ので下駄では底上げしない。"""
    if not isinstance(sel_score, (int, float)):
        return None
    hi, mid, lo = analyze.SEL_TIERS  # 85, 68, 55
    if sel_score >= hi:
        return "2軍"
    return None


def apply_floor(percentile_tier, sel_score):
    floor = tier_floor(sel_score)
    if floor and TIER_RANK[floor] > TIER_RANK.get(percentile_tier, 0):
        return floor
    return percentile_tier


def grade_of(median, ga, gb):
    if median is None:
        return "―"
    return "A" if median >= ga else "B" if median >= gb else "C"


def load_prev_scores():
    if not os.path.isfile(RANKING):
        return {}
    try:
        j = json.load(open(RANKING, encoding="utf-8"))
        out = {}
        for grp in j.get("groups", []):
            for s in grp.get("stocks", []):
                out[s["code"]] = s.get("sel")
        for s in j.get("global_top", []):
            out.setdefault(s["code"], s.get("sel"))
        return out
    except Exception:
        return {}


def direction(now, prev, thr=2.0):
    if now is None or prev is None:
        return "→"
    d = now - prev
    return "↑" if d >= thr else "↓" if d <= -thr else "→"


def main():
    gcfg = json.load(open(GROUPS_CFG, encoding="utf-8"))
    scfg = json.load(open(SCREEN_CFG, encoding="utf-8")) if os.path.isfile(SCREEN_CFG) else {}
    gmap = load_group_map(gcfg)
    ga, gb = gcfg["grade_a"], gcfg["grade_b"]
    c1, c2 = gcfg["tier1_pct"], gcfg["tier2_pct"]
    cap = gcfg.get("cap_low_coverage_at", "2軍")
    min_n = gcfg.get("min_group_for_tiers", 6)
    prev = load_prev_scores()

    rows = []
    for p in sorted(glob.glob(os.path.join(SUM, "*.json"))):
        try:
            s = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        sec = s.get("jp_sector") or ""
        grp = gmap.get(sec, "その他")
        rows.append({
            "code": s["code"], "name": s.get("name") or s["code"],
            "sector": sec, "group": grp,
            "sel": s.get("sel_score"), "tim": s.get("tim_score"),
            "sel_label": s.get("sel_label"), "tim_label": s.get("tim_label"),
            "cov_sel": (s.get("cov_sel") or [None, None, "―"])[2],
            "cov_tim": (s.get("cov_tim") or [None, None, "―"])[2],
            "yield": s.get("div_yield"), "streak_up": s.get("streak_up"),
            "streak_flat": s.get("streak_flat"), "next_earn": s.get("next_earn"),
            "asof": s.get("_generated_at") or s.get("asof"),
        })

    groups_out = []
    for gname in list(gcfg["groups"].keys()):
        gr = [r for r in rows if r["group"] == gname]
        if not gr:
            continue
        gr.sort(key=lambda r: (r["sel"] is None, -(r["sel"] or 0)))
        sels = [r["sel"] for r in gr if isinstance(r["sel"], (int, float))]
        median = round(st.median(sels), 1) if sels else None
        n = len(gr)
        tiered = n >= min_n
        for i, r in enumerate(gr):
            t = tier_of(i, n, c1, c2) if (tiered and r["sel"] is not None) else "―"
            t = apply_floor(t, r["sel"])
            if t == "1軍" and r["cov_sel"] == "低":
                t = cap
            r["tier"] = t
            r["dir"] = direction(r["sel"], prev.get(r["code"]))
        groups_out.append({
            "name": gname, "grade": grade_of(median, ga, gb),
            "median": median, "count": n, "tiered": tiered, "stocks": gr,
        })

    global_top = sorted(
        [r for r in rows if isinstance(r["sel"], (int, float))],
        key=lambda r: -r["sel"])[:50]

    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "screen": {k: scfg.get(k) for k in
                   ("markets", "min_dividend_yield_pct", "min_market_cap_oku", "no_cut_years")},
        "counts": {"total": len(rows),
                   "1軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "1軍"),
                   "2軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "2軍"),
                   "3軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "3軍")},
        "groups": groups_out, "global_top": global_top,
    }
    # generated_at は毎回変わるので、それをマスクした上でHTML本文が前回と同じなら
    # 書き込みをスキップする（でないと earnings-retry/nightly が「何も変わっていない」
    # 実行でも毎回コミット＆Cloudflareデプロイを起こしてしまう＝設計上の「変化があった
    # 時だけコミット」を破る）。HTML全体で比較するのは、スコア等のデータだけでなく
    # render_index() 自体（テンプレート）を直した場合もちゃんと差分として拾うため
    # （generated_atだけをキーにJSONを比較する旧実装だと、テンプレート変更が
    # 無視されてしまっていた＝実際に2026-09-05に踏んだ不具合）。
    new_html = render_index(out)
    gen = out["generated_at"]

    def _mask(html_text, ts):
        return html_text.replace(ts, "GENAT", 1) if ts else html_text

    old_html = None
    if os.path.isfile(INDEX):
        try:
            old_html = open(INDEX, encoding="utf-8").read()
        except Exception:
            old_html = None
    prev_gen = None
    if os.path.isfile(RANKING):
        try:
            prev_gen = json.load(open(RANKING, encoding="utf-8")).get("generated_at")
        except Exception:
            prev_gen = None
    unchanged = old_html is not None and _mask(old_html, prev_gen) == _mask(new_html, gen)

    os.makedirs(SITE, exist_ok=True)
    terms_src = os.path.join(HERE, "terms.html")
    if os.path.isfile(terms_src):
        shutil.copyfile(terms_src, os.path.join(SITE, "terms.html"))

    if unchanged:
        print(f"変化なし（前回 {prev_gen} から更新すべき内容がない）→ 書き込みスキップ")
        print(f"  {out['counts']}")
        return

    json.dump(out, open(RANKING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(INDEX, "w", encoding="utf-8").write(new_html)
    print(f"→ {RANKING}")
    print(f"→ {INDEX}")
    print(f"  {out['counts']}")


# ---------------------------------------------------------------- HTML
def _num(v, d=1):
    return f"{v:.{d}f}" if isinstance(v, (int, float)) else "―"


def _streak(u, f):
    if isinstance(u, (int, float)) and u >= 1:
        return f"連続増配{int(u)}年"
    if isinstance(f, (int, float)) and f >= 1:
        return f"非減配{int(f)}年"
    return ""


TIER_CLASS = {"1軍": "t1", "2軍": "t2", "3軍": "t3", "―": "t0"}
DIR_CLASS = {"↑": "up", "↓": "dn", "→": "fl"}


def render_index(out):
    gen = out["generated_at"]
    sc = out.get("screen", {})
    scr = (f'利回り≥{sc.get("min_dividend_yield_pct")}% ・ 時価総額≥{sc.get("min_market_cap_oku")}億 ・ '
           f'直近{sc.get("no_cut_years")}年減配なし（累進配当/DOE宣言は例外）') if sc.get("min_dividend_yield_pct") else ""
    c = out["counts"]

    secs = []
    for g in out["groups"]:
        head = (f'<h2>{html.escape(g["name"])} '
                f'<span class="grade grade{g["grade"]}">業種級 {g["grade"]}</span> '
                f'<span class="gmeta">中央値 {_num(g["median"])} ／ {g["count"]}銘柄'
                f'{"" if g["tiered"] else " ・ 少数のため軍分けなし"}</span></h2>')
        trs = []
        for s in g["stocks"]:
            tcls = TIER_CLASS.get(s["tier"], "t0")
            dcls = DIR_CLASS.get(s["dir"], "fl")
            trs.append(
                f'<tr class="{tcls} r" data-tier="{s["tier"]}">'
                f'<td class="tier">{s["tier"]}<span class="dir {dcls}">{s["dir"]}</span></td>'
                f'<td class="code"><a href="reports/{s["code"]}.html">{s["code"]}</a></td>'
                f'<td class="nm">{html.escape(s["name"])}</td>'
                f'<td class="sec">{html.escape(s["sector"])}</td>'
                f'<td class="n">{_num(s["sel"],0)}</td>'
                f'<td class="n">{_num(s["tim"],0)}</td>'
                f'<td class="n">{_num(s["yield"],2)}%</td>'
                f'<td class="sk">{_streak(s["streak_up"], s["streak_flat"])}</td>'
                f'<td class="cv">{s["cov_sel"]}</td>'
                f'</tr>')
        secs.append('<section class="grp">' + head + '<table><thead><tr>'
                    '<th>軍</th><th>コード</th><th>銘柄</th><th>業種</th>'
                    '<th>選定</th><th>買い時</th><th>利回り</th><th>増配</th><th>カバレッジ</th>'
                    '</tr></thead><tbody>' + "".join(trs) + '</tbody></table></section>')

    gt = "".join(
        f'<tr class="r" data-tier="{s.get("tier","―")}"><td class="n">{i+1}</td>'
        f'<td class="tier">{s.get("tier","―")}</td>'
        f'<td class="code"><a href="reports/{s["code"]}.html">{s["code"]}</a></td>'
        f'<td class="nm">{html.escape(s["name"])}</td>'
        f'<td class="sec">{html.escape(s["group"])}</td>'
        f'<td class="n">{_num(s["sel"],0)}</td><td class="n">{_num(s["tim"],0)}</td></tr>'
        for i, s in enumerate(out["global_top"]))

    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>配当株 軍分けランキング</title>
<style>
:root{{--bg:#fafafa;--card:#fff;--line:#e6e6e6;--muted:#666;--accent:#2563eb}}
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.6 -apple-system,"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;
  background:var(--bg);color:#1a1a1a}}
.wrap{{max-width:980px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:16px}}
.summary{{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0 22px}}
.summary b{{display:block;font-size:20px}}
.summary div{{background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:8px 14px;min-width:78px;text-align:center}}
h2{{font-size:15px;margin:26px 0 6px;border-bottom:2px solid var(--line);padding-bottom:4px}}
.grade{{font-size:11px;padding:1px 7px;border-radius:10px;vertical-align:middle}}
.gradeA{{background:#dcfce7;color:#166534}}.gradeB{{background:#fef9c3;color:#854d0e}}
.gradeC{{background:#fee2e2;color:#991b1b}}.grade―{{background:#eee;color:#666}}
.gmeta{{font-size:11px;color:var(--muted);font-weight:normal}}
table{{width:100%;border-collapse:collapse;background:var(--card);font-size:13px;
  border:1px solid var(--line);border-radius:8px;overflow:hidden}}
th,td{{padding:6px 8px;text-align:left;border-bottom:1px solid var(--line)}}
th{{background:#f3f4f6;font-size:11px;color:#444}}
td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums}}
tr:last-child td{{border-bottom:none}}
.tier{{font-weight:700;white-space:nowrap}}
.t1 .tier{{color:#166534}}.t2 .tier{{color:#854d0e}}.t3 .tier{{color:#9ca3af}}
.dir{{font-weight:700;margin-left:3px}}
.dir.up{{color:#16a34a}}.dir.dn{{color:#dc2626}}.dir.fl{{color:#cbd5e1}}
.code a{{color:var(--accent);text-decoration:none}}
.sec{{color:var(--muted);font-size:11px}}
.sk{{font-size:11px;color:#555}}
.cv{{font-size:11px}}
.disc{{margin-top:30px;padding:12px;background:#fff7ed;border:1px solid #fed7aa;
  border-radius:8px;font-size:11.5px;color:#7c2d12}}
details{{margin:14px 0}}summary{{cursor:pointer;font-weight:600;font-size:13px}}
.topbar{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}}
.topbar a{{font-size:12.5px;color:var(--accent);white-space:nowrap}}
.searchbar{{display:flex;align-items:center;gap:8px;margin:16px 0 6px}}
.searchbar input{{flex:1;max-width:360px;padding:8px 10px;border:1px solid var(--line);
  border-radius:8px;font-size:13.5px;background:var(--card)}}
.searchbar button{{padding:8px 12px;border:1px solid var(--line);border-radius:8px;
  background:var(--card);font-size:12.5px;cursor:pointer;color:var(--muted)}}
.searchbar .hit{{font-size:12px;color:var(--muted);white-space:nowrap}}
tr[hidden]{{display:none}}
section.grp[hidden],details[hidden]{{display:none}}
.sumbtn{{font:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:8px 14px;min-width:78px;text-align:center;cursor:pointer;color:inherit}}
.sumbtn:hover{{border-color:var(--accent)}}
.sumbtn.active{{border-color:var(--accent);border-width:2px;background:#eff6ff}}
.sumbtn b{{display:block;font-size:20px}}
</style></head><body><div class="wrap">
<div class="topbar"><h1>配当株 軍分けランキング</h1><a href="terms.html">利用規約・免責事項</a></div>
<div class="sub">生成 {gen}　｜　スクリーン：{scr}</div>
<div class="summary">
  <button type="button" class="sumbtn" data-tier=""><b>{c['total']}</b>銘柄</button>
  <button type="button" class="sumbtn" data-tier="1軍"><b>{c['1軍']}</b>1軍</button>
  <button type="button" class="sumbtn" data-tier="2軍"><b>{c['2軍']}</b>2軍</button>
  <button type="button" class="sumbtn" data-tier="3軍"><b>{c['3軍']}</b>3軍</button>
</div>
<div class="sub" style="margin:-14px 0 14px">クリックでその軍だけ表示（もう一度押すと解除）</div>
<div class="searchbar">
  <input id="q" type="search" placeholder="銘柄コード・銘柄名・業種で検索" autocomplete="off">
  <button id="qclear" type="button">クリア</button>
  <span class="hit" id="qhit"></span>
</div>
<details id="topbox"><summary>全体 選定スコア 上位50（業種横断）</summary>
<table><thead><tr><th class="n">#</th><th>軍</th><th>コード</th><th>銘柄</th><th>グループ</th>
<th class="n">選定</th><th class="n">買い時</th></tr></thead><tbody>{gt}</tbody></table>
</details>
{"".join(secs)}
<div class="disc">{DISC}</div>
<script>
(function(){{
  var q = document.getElementById('q');
  var hit = document.getElementById('qhit');
  var topbox = document.getElementById('topbox');
  var sumbtns = document.querySelectorAll('.sumbtn');
  var activeTier = '';
  function apply(){{
    var needle = q.value.trim().normalize('NFKC').toLowerCase();
    var filtering = !!needle || !!activeTier;
    var total = 0;
    document.querySelectorAll('tr.r').forEach(function(tr){{
      var textOk = !needle || tr.textContent.normalize('NFKC').toLowerCase().indexOf(needle) !== -1;
      var tierOk = !activeTier || tr.dataset.tier === activeTier;
      var show = textOk && tierOk;
      tr.hidden = !show;
      if (show) total++;
    }});
    document.querySelectorAll('section.grp').forEach(function(sec){{
      var any = sec.querySelector('tr.r:not([hidden])');
      sec.hidden = filtering && !any;
    }});
    if (topbox) {{
      var anyTop = topbox.querySelector('tr.r:not([hidden])');
      topbox.hidden = filtering && !anyTop;
    }}
    hit.textContent = filtering ? (total + '件ヒット') : '';
  }}
  q.addEventListener('input', apply);
  document.getElementById('qclear').addEventListener('click', function(){{
    q.value = ''; apply(); q.focus();
  }});
  sumbtns.forEach(function(btn){{
    btn.addEventListener('click', function(){{
      var t = btn.dataset.tier;
      activeTier = (activeTier === t) ? '' : t;
      sumbtns.forEach(function(b){{ b.classList.toggle('active', b.dataset.tier === activeTier && activeTier !== ''); }});
      apply();
    }});
  }});
}})();
</script>
</div></body></html>"""


if __name__ == "__main__":
    main()
