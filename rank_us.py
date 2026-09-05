# -*- coding: utf-8 -*-
"""
Reads site/us/summaries/*.json and, per GICS sector, assigns
  - Sector grade A/B/C (median selection score within the sector)
  - Tier 1 / Tier 2 / Tier 3 (within-sector percentile; low coverage capped at Tier 2;
    absolute-score floor guarantees Tier 2 for selection score >= 85)
  - Direction flag up / flat / down (delta vs the previous ranking.json)
then writes site/us/ranking.json and site/us/index.html.

  python rank_us.py

日本株版 rank.py の米国版。tiering の単位は GICS11 セクターそのもの（東証33→12の
集約に相当するものは無し）。UI は英語。
"""
import datetime as dt
import glob
import html
import json
import os
import shutil
import statistics as st

import analyze_us

HERE = os.path.dirname(__file__)
SITE = os.path.join(HERE, "site", "us")
SUM = os.path.join(SITE, "summaries")
GROUPS_CFG = os.path.join(HERE, "sector_groups_us.json")
SCREEN_CFG = os.path.join(HERE, "universe_screen_us.json")
RANKING = os.path.join(SITE, "ranking.json")
INDEX = os.path.join(SITE, "index.html")

DISC = ('This page classifies a pre-screened set of US-listed dividend stocks into tiers and '
        'sector grades using scores computed mechanically from public data with fixed rules. '
        'It does not recommend or solicit the purchase or sale of any security. The operator is '
        'not a registered investment adviser. This is general educational information; make your '
        'own decisions. Figures come from yfinance (Yahoo Finance) and may be wrong, delayed or '
        'missing. See the <a href="terms.html">terms &amp; disclaimer</a>.')

T1, T2, T3, T0 = "Tier 1", "Tier 2", "Tier 3", "—"
TIER_RANK = {T1: 3, T2: 2, T3: 1, T0: 0}


def load_group_map(cfg):
    rev = {}
    for g, secs in cfg["groups"].items():
        for s in secs:
            rev[s] = g
    return rev


def tier_of(rank_idx, n, c1, c2):
    n1 = max(1, round(n * c1))
    n2 = max(1, round(n * c2))
    if rank_idx < n1:
        return T1
    if rank_idx < n1 + n2:
        return T2
    return T3


def tier_floor(sel_score):
    """Absolute-score floor. Same bar as analyze_us.SEL_TIERS[0] (85). A name judged a
    long-term-holdable dividend stock should not drop to Tier 3 merely for sitting in a
    strong sector; guarantee Tier 2. Tier 1 is left as a genuine within-sector top position."""
    if not isinstance(sel_score, (int, float)):
        return None
    hi = analyze_us.SEL_TIERS[0]
    return T2 if sel_score >= hi else None


def apply_floor(percentile_tier, sel_score):
    floor = tier_floor(sel_score)
    if floor and TIER_RANK[floor] > TIER_RANK.get(percentile_tier, 0):
        return floor
    return percentile_tier


def grade_of(median, ga, gb):
    if median is None:
        return "—"
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
    cap = gcfg.get("cap_low_coverage_at", T2)
    min_n = gcfg.get("min_group_for_tiers", 6)
    prev = load_prev_scores()

    rows = []
    for p in sorted(glob.glob(os.path.join(SUM, "*.json"))):
        try:
            s = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        sec = s.get("gics_sector") or ""
        grp = gmap.get(sec, "Other")
        rows.append({
            "code": s["code"], "name": s.get("name") or s["code"],
            "sector": sec, "group": grp,
            "sel": s.get("sel_score"), "tim": s.get("tim_score"),
            "sel_label": s.get("sel_label"), "tim_label": s.get("tim_label"),
            "cov_sel": (s.get("cov_sel") or [None, None, "—"])[2],
            "cov_tim": (s.get("cov_tim") or [None, None, "—"])[2],
            "yield": s.get("div_yield"), "total_yield": s.get("total_yield"),
            "streak_up": s.get("streak_up"), "streak_flat": s.get("streak_flat"),
            "next_earn": s.get("next_earn"), "is_simple": s.get("is_simple"),
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
            t = tier_of(i, n, c1, c2) if (tiered and r["sel"] is not None) else T0
            t = apply_floor(t, r["sel"])
            if t == T1 and r["cov_sel"] == "low":
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
        "screen": {
            "universe": scfg.get("universe_source"),
            "min_dividend_yield_pct": scfg.get("min_dividend_yield_pct"),
            "no_cut_years": scfg.get("no_cut_years"),
        },
        "counts": {"total": len(rows),
                   "Tier 1": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == T1),
                   "Tier 2": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == T2),
                   "Tier 3": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == T3)},
        "groups": groups_out, "global_top": global_top,
    }

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
    for fn in ("terms.html", "guide.html"):
        src = os.path.join(HERE, "site_us_" + fn)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(SITE, fn))

    if unchanged:
        print(f"no change (nothing to update since {prev_gen}) -> skip write")
        print(f"  {out['counts']}")
        return

    json.dump(out, open(RANKING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(INDEX, "w", encoding="utf-8").write(new_html)
    print(f"-> {RANKING}")
    print(f"-> {INDEX}")
    print(f"  {out['counts']}")


# ---------------------------------------------------------------- HTML
def _num(v, d=1):
    return f"{v:.{d}f}" if isinstance(v, (int, float)) else "—"


def _streak(u, f):
    if isinstance(u, (int, float)) and u >= 1:
        return f"{int(u)}y increases"
    if isinstance(f, (int, float)) and f >= 1:
        return f"{int(f)}y no cut"
    return ""


def _streak_val(u, f):
    if isinstance(u, (int, float)) and u >= 1:
        return u
    if isinstance(f, (int, float)) and f >= 1:
        return f
    return None


def _v(x):
    return x if isinstance(x, (int, float)) else ""


TIER_CLASS = {T1: "t1", T2: "t2", T3: "t3", T0: "t0"}
DIR_CLASS = {"↑": "up", "↓": "dn", "→": "fl"}

TERMS = {
    "tier": ("Tier (1–3)",
             "Within a GICS sector, names are ranked by selection score: the top 25% are Tier 1, "
             "the next 45% Tier 2, the rest Tier 3. A name with 'low' coverage is capped at Tier 2. "
             "Conversely, a name with a selection score of 85+ (the long-term-holdable bar) is "
             "guaranteed at least Tier 2 so it is not dragged to Tier 3 just for being in a "
             "strong sector — Tier 1 is left as a genuine within-sector top position. Sectors with "
             "fewer than 6 screened names are shown flat ('—')."),
    "grade": ("Sector grade (A/B/C)",
              "<b>A &gt; B &gt; C: A sectors hold the better-quality dividend names.</b> Judged from "
              "the median selection score of each GICS sector — median 88+ = A, 84+ = B, below = C "
              "(thresholds are calibrated to the actual distribution of the pre-screened universe "
              "and are provisional). A guide for picking hunting grounds; judge individual names by "
              "the selection score and tier."),
    "sel": ("Selection score",
            "A 0–110 score from operating performance, financial strength, cash flow and dividend "
            "durability. The quality of the dividend / whether it looks long-term holdable. "
            "85+ = top tier, 68+ = mid, 55+ = low, below = under the bar."),
    "tim": ("Timing score",
            "A 0–110 score from the dividend-yield-theory position (where the yield sits in its own "
            "history), yield level &amp; the Chowder rule, valuation vs the sector (P/E, P/B) and — "
            "for rate-sensitive sectors — the spread over the 10-year Treasury. A read on the "
            "current price, separate from quality. 98+ = cheap zone, 82+ = fair, 64+ = a bit "
            "expensive, below = expensive (thresholds calibrated to the screened universe)."),
    "yield": ("Yield",
              "Forward annual dividend / current price."),
    "streak": ("Dividend streak",
               "The longer of: consecutive years of increases ('Ny increases'), or consecutive "
               "years without a cut even if increases have paused ('Ny no cut'). Computed from the "
               "yfinance dividend history on a per-payment-rate basis (robust to shifted pay dates)."),
    "cov": ("Coverage",
            "The share of scoring metrics that had usable data for this name: high (85%+), mid "
            "(65%+), low (below). Low-coverage names have short financial/dividend history and their "
            "scores are noisier — treat as indicative."),
}


def render_index(out):
    gen = out["generated_at"]
    sc = out.get("screen", {})
    scr = (f'yield &ge; {sc.get("min_dividend_yield_pct")}% · no cut in the last '
           f'{sc.get("no_cut_years")} years · universe: {html.escape(str(sc.get("universe") or "S&P 1500"))}'
           ) if sc.get("min_dividend_yield_pct") else ""
    c = out["counts"]
    terms_json = json.dumps(TERMS, ensure_ascii=False)

    secs = []
    for g in out["groups"]:
        head = (f'<h2>{html.escape(g["name"])} '
                f'<span class="grade grade{g["grade"]} hdr" data-term="grade">grade {g["grade"]}</span> '
                f'<span class="gmeta">median {_num(g["median"])} / {g["count"]} names'
                f'{"" if g["tiered"] else " · too few to tier"}</span></h2>')
        trs = []
        for s in g["stocks"]:
            tcls = TIER_CLASS.get(s["tier"], "t0")
            dcls = DIR_CLASS.get(s["dir"], "fl")
            streak_v = _streak_val(s["streak_up"], s["streak_flat"])
            trs.append(
                f'<tr class="{tcls} r" data-tier="{s["tier"]}">'
                f'<td class="tier">{s["tier"]}<span class="dir {dcls}">{s["dir"]}</span></td>'
                f'<td class="code"><a href="reports/{s["code"]}.html">{s["code"]}</a></td>'
                f'<td class="nm">{html.escape(s["name"])}</td>'
                f'<td class="n" data-v="{_v(s["sel"])}">{_num(s["sel"],0)}</td>'
                f'<td class="n" data-v="{_v(s["tim"])}">{_num(s["tim"],0)}</td>'
                f'<td class="n" data-v="{_v(s["yield"])}">{_num(s["yield"],2)}%</td>'
                f'<td class="sk" data-v="{_v(streak_v)}">{_streak(s["streak_up"], s["streak_flat"])}</td>'
                f'<td class="cv">{s["cov_sel"]}</td>'
                f'</tr>')
        secs.append('<section class="grp">' + head + '<table><thead><tr>'
                    '<th class="hdr" data-term="tier">Tier</th><th>Ticker</th><th>Name</th>'
                    '<th class="n"><span class="hdr" data-term="sel">Sel</span><span class="sortbtn">▼</span></th>'
                    '<th class="n"><span class="hdr" data-term="tim">Tim</span><span class="sortbtn">▼</span></th>'
                    '<th class="n"><span class="hdr" data-term="yield">Yield</span><span class="sortbtn">▼</span></th>'
                    '<th><span class="hdr" data-term="streak">Streak</span><span class="sortbtn">▼</span></th>'
                    '<th class="hdr" data-term="cov">Cov</th>'
                    '</tr></thead><tbody>' + "".join(trs) + '</tbody></table></section>')

    gt = "".join(
        f'<tr class="r" data-tier="{s.get("tier","—")}"><td class="n">{i+1}</td>'
        f'<td class="tier">{s.get("tier","—")}</td>'
        f'<td class="code"><a href="reports/{s["code"]}.html">{s["code"]}</a></td>'
        f'<td class="nm">{html.escape(s["name"])}</td>'
        f'<td class="sec">{html.escape(s["group"])}</td>'
        f'<td class="n" data-v="{_v(s["sel"])}">{_num(s["sel"],0)}</td>'
        f'<td class="n" data-v="{_v(s["tim"])}">{_num(s["tim"],0)}</td></tr>'
        for i, s in enumerate(out["global_top"]))

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>US dividend stocks — tier ranking</title>
<style>
:root{{--bg:#fafafa;--card:#fff;--line:#e6e6e6;--muted:#666;--accent:#2563eb}}
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.6 -apple-system,"Segoe UI",system-ui,sans-serif;
  background:var(--bg);color:#1a1a1a}}
.wrap{{max-width:980px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:16px}}
.sub a{{color:var(--accent)}}
.summary{{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0 22px}}
.summary b{{display:block;font-size:20px}}
h2{{font-size:15px;margin:26px 0 6px;border-bottom:2px solid var(--line);padding-bottom:4px}}
.grade{{font-size:11px;padding:1px 7px;border-radius:10px;vertical-align:middle}}
.gradeA{{background:#dcfce7;color:#166534}}.gradeB{{background:#fef9c3;color:#854d0e}}
.gradeC{{background:#fee2e2;color:#991b1b}}
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
.hdr{{cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px}}
.hdr:hover{{color:var(--accent)}}
.sortbtn{{cursor:pointer;color:var(--muted);font-size:10px;margin-left:4px;user-select:none;display:inline-block}}
.sortbtn:hover{{color:var(--accent)}}
.sortbtn.active{{color:var(--accent);font-weight:700}}
.terminfo{{position:relative;margin:8px 0 18px;padding:12px 36px 12px 14px;background:#eff6ff;
  border:1px solid #bfdbfe;border-radius:8px;font-size:12.5px;line-height:1.7}}
.terminfo b{{display:block;margin-bottom:4px;font-size:13.5px}}
.ticlose{{position:absolute;top:6px;right:8px;border:none;background:none;cursor:pointer;
  font-size:15px;line-height:1;color:var(--muted);padding:4px}}
</style></head><body><div class="wrap">
<div class="topbar"><h1>US dividend stocks — tier ranking</h1><a href="terms.html">terms &amp; disclaimer</a></div>
<div class="sub">generated {gen} &nbsp;|&nbsp; screen: {scr} &nbsp;|&nbsp; <a href="guide.html">how to read this</a></div>
<div class="sub">Click a column header (Tier / Sel / Tim / Yield / Streak / Cov / grade) for its definition.</div>
<div class="summary">
  <button type="button" class="sumbtn" data-tier=""><b>{c['total']}</b>names</button>
  <button type="button" class="sumbtn" data-tier="Tier 1"><b>{c['Tier 1']}</b>Tier 1</button>
  <button type="button" class="sumbtn" data-tier="Tier 2"><b>{c['Tier 2']}</b>Tier 2</button>
  <button type="button" class="sumbtn" data-tier="Tier 3"><b>{c['Tier 3']}</b>Tier 3</button>
</div>
<div class="sub" style="margin:-14px 0 14px">Click to show only that tier (click again to clear).</div>
<div class="searchbar">
  <input id="q" type="search" placeholder="search ticker / name / sector" autocomplete="off">
  <button id="qclear" type="button">clear</button>
  <span class="hit" id="qhit"></span>
</div>
<div id="terminfo" class="terminfo" hidden>
  <button type="button" id="terminfo-close" class="ticlose" aria-label="close">✕</button>
  <div id="terminfo-body"></div>
</div>
<details id="topbox"><summary>Top 50 by selection score (all sectors)</summary>
<table><thead><tr><th class="n">#</th><th class="hdr" data-term="tier">Tier</th><th>Ticker</th><th>Name</th><th>Sector</th>
<th class="n"><span class="hdr" data-term="sel">Sel</span><span class="sortbtn">▼</span></th>
<th class="n"><span class="hdr" data-term="tim">Tim</span><span class="sortbtn">▼</span></th></tr></thead><tbody>{gt}</tbody></table>
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
    hit.textContent = filtering ? (total + ' hits') : '';
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
  var TERMS = {terms_json};
  var tibox = document.getElementById('terminfo');
  var tibody = document.getElementById('terminfo-body');
  document.querySelectorAll('.hdr').forEach(function(el){{
    el.addEventListener('click', function(e){{
      e.stopPropagation();
      var t = TERMS[el.dataset.term];
      if (!t) return;
      tibody.innerHTML = '<b>' + t[0] + '</b>' + t[1];
      tibox.hidden = false;
      tibox.scrollIntoView({{behavior:'smooth', block:'nearest'}});
    }});
  }});
  document.getElementById('terminfo-close').addEventListener('click', function(){{
    tibox.hidden = true;
  }});

  function sortTable(btn){{
    var th = btn.closest('th');
    var table = th.closest('table');
    var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
    var tbody = table.querySelector('tbody');
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    var curCol = table.getAttribute('data-sort-col');
    var curDir = table.getAttribute('data-sort-dir') || 'desc';
    var dir = (curCol == idx && curDir === 'desc') ? 'asc' : 'desc';
    rows.sort(function(a, b){{
      var av = parseFloat(a.children[idx].getAttribute('data-v'));
      var bv = parseFloat(b.children[idx].getAttribute('data-v'));
      var aNaN = isNaN(av), bNaN = isNaN(bv);
      if (aNaN && bNaN) return 0;
      if (aNaN) return 1;
      if (bNaN) return -1;
      return dir === 'asc' ? (av - bv) : (bv - av);
    }});
    rows.forEach(function(r){{ tbody.appendChild(r); }});
    table.setAttribute('data-sort-col', idx);
    table.setAttribute('data-sort-dir', dir);
    Array.prototype.forEach.call(table.querySelectorAll('.sortbtn'), function(b){{
      b.classList.remove('active'); b.textContent = '▼';
    }});
    btn.classList.add('active');
    btn.textContent = dir === 'asc' ? '▲' : '▼';
  }}
  document.querySelectorAll('.sortbtn').forEach(function(btn){{
    btn.addEventListener('click', function(e){{
      e.stopPropagation();
      sortTable(btn);
    }});
  }});
}})();
</script>
</div></body></html>"""


if __name__ == "__main__":
    main()
