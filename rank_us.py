# -*- coding: utf-8 -*-
"""
site/us/summaries/*.json を読み、GICS11 セクターごとに
  - 業種級 A/B/C（セクター内の銘柄選定スコア中央値）
  - 1軍/2軍/3軍（セクター内パーセンタイル。カバレッジ低は2軍止まり）
  - 方向フラグ ↑改善中／→横ばい／↓悪化中（前回 ranking.json との差）
を付けて site/us/ranking.json と site/us/index.html を書き出す。

  python rank_us.py

日本株版 rank.py の米国版。tiering の単位は GICS11 セクターそのもの（東証33→12の
集約に相当するものは無し）。UI は日本語。
"""
import datetime as dt
import glob
import html
import json
import os
import shutil
import statistics as st

import analyze_us
from analyze_us import gics_jp

HERE = os.path.dirname(__file__)
SITE = os.path.join(HERE, "site", "us")
SUM = os.path.join(SITE, "summaries")
GROUPS_CFG = os.path.join(HERE, "sector_groups_us.json")
SCREEN_CFG = os.path.join(HERE, "universe_screen_us.json")
RANKING = os.path.join(SITE, "ranking.json")
INDEX = os.path.join(SITE, "index.html")

DISC = ('本ページは、あらかじめ定めた基準で抽出した米国上場の配当銘柄について、公開データを'
        '機械的なルールで算出したスコアによる分類（1〜3軍・業種級）です。特定銘柄の売買を推奨・'
        '勧誘するものではなく、運営者は投資助言・代理業の登録を受けていません。教育目的の一般'
        '情報であり、投資判断はご自身の責任で行ってください。数値は yfinance 由来で誤り・遅延・'
        '欠損があり得ます。詳しくは<a href="terms.html">利用規約・免責事項</a>をご確認ください。')


def load_group_map(cfg):
    rev = {}
    for g, secs in cfg["groups"].items():
        for s in secs:
            rev[s] = g
    return rev


TIER_RANK = {"1軍": 3, "2軍": 2, "3軍": 1, "―": 0}


def tier_of(rank_idx, n, c1, c2):
    n1 = max(1, round(n * c1))
    n2 = max(1, round(n * c2))
    if rank_idx < n1:
        return "1軍"
    if rank_idx < n1 + n2:
        return "2軍"
    return "3軍"


def tier_floor(sel_score):
    """絶対スコアの下駄。analyze_us.SEL_TIERS[0]（85）と同じ基準で「銘柄選定で長期保有
    できる配当株と判定された銘柄が、強いセクターに属しているだけで3軍に落ちる」のを防ぐ。
    最低でも2軍を保証。1軍はあくまでセクター内での相対的な上位なので下駄では底上げしない。"""
    if not isinstance(sel_score, (int, float)):
        return None
    return "2軍" if sel_score >= analyze_us.SEL_TIERS[0] else None


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
        sec = s.get("gics_sector") or ""
        grp = gmap.get(sec, "その他")
        rows.append({
            "code": s["code"], "name": s.get("name") or s["code"],
            "sector": sec, "sector_jp": gics_jp(sec), "group": grp,
            "sel": s.get("sel_score"), "tim": s.get("tim_score"),
            "sel_label": s.get("sel_label"), "tim_label": s.get("tim_label"),
            "cov_sel": (s.get("cov_sel") or [None, None, "―"])[2],
            "cov_tim": (s.get("cov_tim") or [None, None, "―"])[2],
            "yield": s.get("div_yield"), "total_yield": s.get("total_yield"),
            "streak_up": s.get("streak_up"), "streak_flat": s.get("streak_flat"),
            "next_earn": s.get("next_earn"), "is_simple": s.get("is_simple"),
            "price": s.get("price"), "price_date": s.get("price_date"),
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
            "name": gname, "name_jp": gics_jp(gname), "grade": grade_of(median, ga, gb),
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
                   "1軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "1軍"),
                   "2軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "2軍"),
                   "3軍": sum(1 for g in groups_out for s in g["stocks"] if s["tier"] == "3軍")},
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
    for fn in ("terms.html", "guide.html", "watchlist.html", "portfolio.html"):
        src = os.path.join(HERE, "site_us_" + fn)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(SITE, fn))

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


def _price(v):
    if not isinstance(v, (int, float)):
        return "―"
    return f"${v:,.2f}" if abs(v) < 100 else f"${v:,.0f}"


def _streak(u, f):
    if isinstance(u, (int, float)) and u >= 1:
        return f"連続増配{int(u)}年"
    if isinstance(f, (int, float)) and f >= 1:
        return f"非減配{int(f)}年"
    return ""


def _streak_val(u, f):
    if isinstance(u, (int, float)) and u >= 1:
        return u
    if isinstance(f, (int, float)) and f >= 1:
        return f
    return None


def _v(x):
    return x if isinstance(x, (int, float)) else ""


# ======================================================================
# 配色テーマ（ランキング／ウォッチリスト共通）。
# 下の3つの定数は watchlist.html / rank_us.py / site_us_watchlist.html と
# 「同一内容」を貼っている（1か所直したら4か所そろえる）。
# ======================================================================
THEME_HEAD = (
    '<script>try{var t=JSON.parse(localStorage.getItem("pp_theme")||"{}");'
    'if(t.pal&&t.pal!=="genko"&&t.pal!=="custom"){document.documentElement.setAttribute("data-pal",t.pal);'
    'document.documentElement.setAttribute("data-hl","on");}'
    'if(t.pal==="custom")document.documentElement.setAttribute("data-hl","on");'
    'if(t.mode==="dark"||t.mode==="light")document.documentElement.setAttribute("data-mode",t.mode);}catch(e){}</script>'
)

THEME_CSS = """
:root{
  --bg:#fafafa;--card:#ffffff;--line:#e6e6e6;--muted:#666666;--fg:#1a1a1a;
  --accent:#2563eb;--th:#f3f4f6;--field:#f7f8fa;--info:#eff6ff;
  --t1:#166534;--t2:#854d0e;--t3:#9ca3af;--gA:#166534;--gB:#854d0e;--gC:#b91c1c;
  --wbg:#fff7ed;--wbd:#fed7aa;--wfg:#7c2d12;--ibg:#eff6ff;--ibd:#bfdbfe;
}
:root[data-mode="dark"]{
  --bg:#101216;--card:#181b22;--line:#2b303a;--muted:#98a1ad;--fg:#e6e8eb;
  --accent:#6aa4ff;--th:#1e232c;--field:#161a21;--info:#17233a;
  --t1:#4ade80;--t2:#fbbf24;--t3:#9aa1ac;--gA:#4ade80;--gB:#fbbf24;--gC:#f87171;
  --wbg:#26210f;--wbd:#5a4a1b;--wfg:#e7c06a;--ibg:#152036;--ibd:#2b3f63;
}
:root[data-pal="blueplus"]{--bg:#f7f8fa;--card:#ffffff;--line:#dfe1e6;--muted:#5b6472;--fg:#161a1f;--accent:#2563eb;--th:#eaedf3;--field:#f4f6f9;--info:#e9f0ff;}
:root[data-pal="blueplus"][data-mode="dark"]{--bg:#0d0f14;--card:#191d26;--line:#30363f;--muted:#9aa3ae;--fg:#e9ebef;--accent:#7db0ff;--th:#20262f;--field:#161b24;--info:#16233b;}
:root[data-pal="teal"]{--bg:#f2f8f6;--card:#ffffff;--line:#d7e6e1;--muted:#556661;--fg:#132019;--accent:#0d7c72;--th:#e4f1ed;--field:#f0f7f5;--info:#e2f2ee;}
:root[data-pal="teal"][data-mode="dark"]{--bg:#0b120f;--card:#152019;--line:#28372f;--muted:#8fa39c;--fg:#e2ece7;--accent:#3fd9c8;--th:#1c2b24;--field:#131d17;--info:#132720;}
:root[data-pal="slate"]{--bg:#f5f6f9;--card:#ffffff;--line:#dde1e8;--muted:#5b6577;--fg:#171d28;--accent:#334c86;--th:#e8ebf1;--field:#f3f5f8;--info:#e8ecf5;}
:root[data-pal="slate"][data-mode="dark"]{--bg:#0d0f13;--card:#181c23;--line:#2c323d;--muted:#96a0ae;--fg:#e6e8ec;--accent:#8fb0e6;--th:#1e232c;--field:#161a21;--info:#182238;}
:root[data-pal="sand"]{--bg:#f8f4ea;--card:#fffdf8;--line:#e7dcc7;--muted:#6a5f48;--fg:#221c11;--accent:#b1500a;--th:#efe5d1;--field:#f5efe0;--info:#f6ecda;}
:root[data-pal="sand"][data-mode="dark"]{--bg:#131009;--card:#201b11;--line:#372c1b;--muted:#a89d84;--fg:#ece4d3;--accent:#ffab2e;--th:#271f12;--field:#191509;--info:#241d10;}
:root[data-pal="violet"]{--bg:#f8f6fd;--card:#ffffff;--line:#e4dcf1;--muted:#635a75;--fg:#1b1626;--accent:#6d28d9;--th:#efe9fa;--field:#f5f2fc;--info:#efe9fb;}
:root[data-pal="violet"][data-mode="dark"]{--bg:#0f0d16;--card:#1c1727;--line:#302742;--muted:#a89fb8;--fg:#ebe5f4;--accent:#b79bff;--th:#231c30;--field:#171223;--info:#1e1830;}
:root[data-pal="rose"]{--bg:#fdf5f8;--card:#ffffff;--line:#efd9e2;--muted:#7a5a65;--fg:#24171d;--accent:#c2255c;--th:#fbe7ef;--field:#fbf3f6;--info:#fbe9f0;}
:root[data-pal="rose"][data-mode="dark"]{--bg:#140f12;--card:#211820;--line:#3a2b33;--muted:#b499a3;--fg:#efe3e9;--accent:#f472b6;--th:#281c24;--field:#1a1318;--info:#241722;}

#theme-bar{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:8px 0 14px;font-size:12px}
#theme-bar .tb-lbl{color:var(--muted)}
#theme-bar .sw{width:20px;height:20px;border-radius:50%;border:1px solid var(--line);cursor:pointer;padding:0;background:var(--tb-c,#ccc)}
#theme-bar .sw[data-pal="genko"]{background:linear-gradient(135deg,#fff 48%,#2563eb 52%)}
#theme-bar .sw.custom{background:conic-gradient(#e74c8b,#f0b429,#28a48b,#4f7be0,#9b5de5,#e74c8b)}
#theme-bar .sw[aria-pressed="true"]{outline:2px solid var(--fg);outline-offset:1px}
#theme-bar .tb-btn{border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:7px;padding:3px 9px;font:inherit;font-size:12px;cursor:pointer}
#theme-bar .tb-reset{color:var(--accent);border:0;background:none;text-decoration:underline;text-underline-offset:2px;cursor:pointer;font:inherit;font-size:12px}
#theme-bar .tb-sep{width:1px;align-self:stretch;background:var(--line);margin:0 3px}
#cust-panel{display:none;flex-wrap:wrap;gap:10px 16px;align-items:center;width:100%;margin-top:4px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--card)}
#cust-panel.open{display:flex}
#cust-panel label{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--fg)}
#cust-panel input[type=color]{width:40px;height:24px;border:1px solid var(--line);border-radius:5px;padding:0;background:none;cursor:pointer}
#cust-panel .cc-more{border:0;background:none;color:var(--accent);font:inherit;font-size:11.5px;text-decoration:underline;text-underline-offset:2px;cursor:pointer}
#cust-panel .cc-adv{display:none;gap:10px 16px;flex-wrap:wrap;align-items:center}
#cust-panel .cc-adv.open{display:flex}
#cust-panel .cc-warn{font-size:11px;color:var(--gC);flex-basis:100%}

:root[data-hl="on"] .sumbtn.active{box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 24%,transparent)}
:root[data-hl="on"] tr.r:has(.wlc:checked)>td{background:color-mix(in srgb,var(--accent) 10%,transparent)}
"""

THEME_BAR = """
<div id="theme-bar">
  <span class="tb-lbl">配色</span>
  <button type="button" class="sw" data-pal="genko" title="現行（そのまま）" aria-label="現行"></button>
  <button type="button" class="sw" data-pal="blueplus" style="--tb-c:#2563eb" title="ブルー＋" aria-label="ブルー＋"></button>
  <button type="button" class="sw" data-pal="teal" style="--tb-c:#0d7c72" title="ティール" aria-label="ティール"></button>
  <button type="button" class="sw" data-pal="slate" style="--tb-c:#334c86" title="スレート" aria-label="スレート"></button>
  <button type="button" class="sw" data-pal="sand" style="--tb-c:#b1500a" title="サンド" aria-label="サンド"></button>
  <button type="button" class="sw" data-pal="violet" style="--tb-c:#6d28d9" title="バイオレット" aria-label="バイオレット"></button>
  <button type="button" class="sw" data-pal="rose" style="--tb-c:#c2255c" title="ローズ" aria-label="ローズ"></button>
  <button type="button" class="sw custom" data-pal="custom" title="カスタム" aria-label="カスタム"></button>
  <span class="tb-sep"></span>
  <button type="button" class="tb-btn tb-mode">🌙 ダークへ</button>
  <button type="button" class="tb-reset">↺ 標準に戻す</button>
  <div id="cust-panel">
    <label>地色 <input type="color" id="cc-bg" value="#fafafa"></label>
    <label>アクセント <input type="color" id="cc-accent" value="#2563eb"></label>
    <button type="button" class="cc-more">＋ くわしく（面・枠・文字色も指定）</button>
    <div class="cc-adv">
      <label>面 <input type="color" id="cc-card" value="#ffffff"></label>
      <label>枠 <input type="color" id="cc-line" value="#e6e6e6"></label>
      <label>補助文字 <input type="color" id="cc-muted" value="#666666"></label>
      <label>文字色 <input type="color" id="cc-fg" value="#1a1a1a"></label>
    </div>
    <span class="cc-warn"></span>
  </div>
</div>
"""

THEME_JS = r"""
(function(){
  var PALS=['genko','blueplus','teal','slate','sand','violet','rose','custom'];
  var TKEY='pp_theme',CKEY='pp_custom',root=document.documentElement;
  var bar=document.getElementById('theme-bar'); if(!bar) return;
  function readT(){try{return JSON.parse(localStorage.getItem(TKEY)||'null')||{};}catch(e){return {};}}
  function readC(){try{return JSON.parse(localStorage.getItem(CKEY)||'null')||null;}catch(e){return null;}}
  function saveT(o){try{localStorage.setItem(TKEY,JSON.stringify(o));}catch(e){}}
  function h2r(h){h=h.replace('#','');return[parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)];}
  function r2h(a){return '#'+a.map(function(x){x=Math.max(0,Math.min(255,Math.round(x)));return x.toString(16).padStart(2,'0');}).join('');}
  function mix(a,b,t){var A=h2r(a),B=h2r(b);return r2h([0,1,2].map(function(i){return A[i]+(B[i]-A[i])*t;}));}
  function lum(h){var c=h2r(h).map(function(x){x/=255;return x<=0.03928?x/12.92:Math.pow((x+0.055)/1.055,2.4);});return 0.2126*c[0]+0.7152*c[1]+0.0722*c[2];}
  function cr(a,b){var L1=lum(a),L2=lum(b);return (Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);}
  function deriv(bg){var d=lum(bg)<0.35;return{dark:d,fg:d?mix(bg,'#ffffff',0.9):mix(bg,'#000000',0.86),card:d?mix(bg,'#ffffff',0.06):mix(bg,'#ffffff',0.72),line:d?mix(bg,'#ffffff',0.15):mix(bg,'#000000',0.1),muted:d?mix(bg,'#ffffff',0.55):mix(bg,'#000000',0.55)};}
  var CD={bg:'#fafafa',accent:'#2563eb',card:'#ffffff',line:'#e6e6e6',muted:'#666666',fg:'#1a1a1a'};
  var CVARS=['--bg','--card','--line','--muted','--fg','--accent','--th','--t1','--t2','--t3','--gA','--gB','--gC'];
  function clearCP(){CVARS.forEach(function(p){root.style.removeProperty(p);});}
  function applyCustom(c){
    var bg=c.bg,ac=c.accent,card,line,muted,fg;
    if(c.adv){card=c.card;line=c.line;muted=c.muted;fg=c.fg;}
    else if(bg.toLowerCase()===CD.bg){card=CD.card;line=CD.line;muted=CD.muted;fg=CD.fg;}
    else{var d=deriv(bg);card=d.card;line=d.line;muted=d.muted;fg=d.fg;}
    var dark=lum(bg)<0.35;
    var S=root.style;
    S.setProperty('--bg',bg);S.setProperty('--card',card);S.setProperty('--line',line);
    S.setProperty('--muted',muted);S.setProperty('--fg',fg);S.setProperty('--accent',ac);
    S.setProperty('--th',mix(card,fg,0.06));
    S.setProperty('--t1',dark?'#4ade80':'#166534');S.setProperty('--t2',dark?'#fbbf24':'#854d0e');S.setProperty('--t3',dark?'#9aa1ac':'#9ca3af');
    S.setProperty('--gA',dark?'#4ade80':'#166534');S.setProperty('--gB',dark?'#fbbf24':'#854d0e');S.setProperty('--gC',dark?'#f87171':'#b91c1c');
  }
  function apply(){
    var t=readT();
    var pal=(t.pal&&PALS.indexOf(t.pal)>=0)?t.pal:'genko';
    var mode=(t.mode==='dark')?'dark':(t.mode==='light'?'light':'');
    clearCP();
    if(pal==='custom'){
      applyCustom(readC()||Object.assign({},CD));
      root.removeAttribute('data-pal');root.removeAttribute('data-mode');root.setAttribute('data-hl','on');
    }else{
      if(pal==='genko'){root.removeAttribute('data-pal');root.removeAttribute('data-hl');}
      else{root.setAttribute('data-pal',pal);root.setAttribute('data-hl','on');}
      if(mode)root.setAttribute('data-mode',mode);else root.removeAttribute('data-mode');
    }
    bar.querySelectorAll('.sw').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.pal===pal?'true':'false');});
    var mb=bar.querySelector('.tb-mode');
    if(mb){mb.textContent=(mode==='dark')?'☀ ライトへ':'🌙 ダークへ';mb.style.display=(pal==='custom')?'none':'';}
    var cp=document.getElementById('cust-panel');
    if(cp)cp.classList.toggle('open',pal==='custom');
  }
  bar.querySelectorAll('.sw').forEach(function(b){
    b.addEventListener('click',function(){var t=readT();t.pal=b.dataset.pal;if(!('mode' in t))t.mode='';saveT(t);apply();});
  });
  var mBtn=bar.querySelector('.tb-mode');
  if(mBtn)mBtn.addEventListener('click',function(){var t=readT();t.mode=(t.mode==='dark')?'light':'dark';saveT(t);apply();});
  var rBtn=bar.querySelector('.tb-reset');
  if(rBtn)rBtn.addEventListener('click',function(){try{localStorage.removeItem(TKEY);}catch(e){}apply();});
  var cp=document.getElementById('cust-panel');
  if(cp){
    var q=function(s){return cp.querySelector(s);};
    var ci={bg:q('#cc-bg'),accent:q('#cc-accent'),card:q('#cc-card'),line:q('#cc-line'),muted:q('#cc-muted'),fg:q('#cc-fg')};
    var adv=q('.cc-adv'),more=q('.cc-more'),warn=q('.cc-warn'),advOpen=false;
    var sv=readC();
    if(sv){ci.bg.value=sv.bg||CD.bg;ci.accent.value=sv.accent||CD.accent;ci.card.value=sv.card||CD.card;ci.line.value=sv.line||CD.line;ci.muted.value=sv.muted||CD.muted;ci.fg.value=sv.fg||CD.fg;advOpen=!!sv.adv;}
    if(advOpen){adv.classList.add('open');more.textContent='－ かんたんに戻す';}
    function push(){
      var c={bg:ci.bg.value,accent:ci.accent.value,adv:advOpen,card:ci.card.value,line:ci.line.value,muted:ci.muted.value,fg:ci.fg.value};
      try{localStorage.setItem(CKEY,JSON.stringify(c));}catch(e){}
      var t=readT();t.pal='custom';saveT(t);apply();
      var bg=c.bg,fg=advOpen?c.fg:(bg.toLowerCase()===CD.bg?CD.fg:deriv(bg).fg),m=[];
      if(cr(fg,bg)<4.5)m.push('文字と背景の差が小さめ');
      if(cr(c.accent,bg)<2.4)m.push('アクセントが埋もれ気味');
      warn.textContent=m.length?'⚠ '+m.join(' ／ '):'';
    }
    cp.addEventListener('input',push);
    more.addEventListener('click',function(){
      advOpen=!advOpen;adv.classList.toggle('open',advOpen);
      more.textContent=advOpen?'－ かんたんに戻す':'＋ くわしく（面・枠・文字色も指定）';
      if(advOpen){var d=deriv(ci.bg.value);ci.card.value=d.card;ci.line.value=d.line;ci.muted.value=d.muted;ci.fg.value=d.fg;}
      push();
    });
  }
  apply();
})();
"""


TIER_CLASS = {"1軍": "t1", "2軍": "t2", "3軍": "t3", "―": "t0"}
DIR_CLASS = {"↑": "up", "↓": "dn", "→": "fl"}

TERMS = {
    "tier": ("軍（1〜3軍）",
             "GICS業種セクター内で銘柄選定スコアの高い順に、上位25%を1軍、続く45%を2軍、"
             "残りを3軍に分類。カバレッジが「低」の銘柄は最高でも2軍まで。逆に銘柄選定スコアが"
             "85以上（＝選定スコア上位の水準）の銘柄は、たまたま質の高いセクターに属しているだけで"
             "3軍に落ちないよう最低でも2軍を保証する（1軍はセクター内での相対的な上位という意味を"
             "残すため底上げしない）。対象が6銘柄未満のセクターは軍分けせず「―」。"),
    "grade": ("業種級（A/B/C）",
              "<b>A＞B＞C の順で、Aが最も質の高い配当株が揃っている業種</b>という序列。GICS11の"
              "各セクターの銘柄選定スコアの中央値をもとに判定し、中央値88以上でA、84以上でB、"
              "それ未満はC（この境界値は、スクリーニング済みの母集団の実際の分布に合わせて"
              "校正しており暫定値）。同じセクター内の銘柄でも、Aの業種の方がBやCの業種より"
              "粒ぞろいで質が高い傾向にある、という業種選びの目安。個別銘柄の良し悪しは"
              "「選定」スコアと「軍」で見る。"),
    "price": ("終値",
              "前回の取引終了時点（前営業日）の株価（米ドル）。夜間更新のため、当日ザラ場の"
              "株価とはずれます。個別ページでは同じ日の高値・安値・始値も見られます。"),
    "sel": ("選定（銘柄選定スコア）",
            "業績・財務・キャッシュフロー・配当の持続力から算出する0〜110点のスコア。"
            "配当株としての質・長期保有できるかどうかの目安。85以上＝選定スコア上位（質・"
            "持続力とも高水準）、68以上＝中位、55以上＝下位、それ未満＝基準未達。"),
    "tim": ("買い時（買い時スコア）",
            "配当利回りセオリー（自社の過去レンジ内の位置）・利回り水準とChowderルール・"
            "株価バリュエーション（PER/PBR対業種平均）・金利スプレッド（利回り − 米10年国債）"
            "から算出する0〜110点のスコア。今の株価水準の目安であり、質の評価（選定）とは"
            "別物。98以上＝割安水準、82以上＝妥当水準、64以上＝やや割高、それ未満＝割高"
            "（しきい値は母集団の分布に合わせて校正）。"),
    "yield": ("利回り",
              "会社予想の年間配当金 ÷ 現在の株価（予想配当利回り）。"),
    "streak": ("増配",
               "連続で増配している年数（「連続増配N年」）、増配は止まっていても減配して"
               "いない年数（「非減配N年」）のどちらか長い方を表示。yfinanceの配当履歴から、"
               "1回あたり配当額の前年同期比で自前計算（支払時期のズレに強い方式）。"),
    "cov": ("カバレッジ",
            "その銘柄でスコア算出に使えた指標の割合。高（85%以上）・中（65%以上）・"
            "低（それ未満）の3段階。低い銘柄は財務・配当の履歴が短い等の理由でデータが"
            "足りず、点数がぶれやすいので参考程度に見てほしいという意味。"),
}


def render_index(out):
    gen = out["generated_at"]
    sc = out.get("screen", {})
    scr = (f'予想利回り ≥ {sc.get("min_dividend_yield_pct")}% ・ 直近{sc.get("no_cut_years")}年 減配なし ・ '
           f'母集団：{html.escape(str(sc.get("universe") or "S&P1500"))}') if sc.get("min_dividend_yield_pct") else ""
    c = out["counts"]
    terms_json = json.dumps(TERMS, ensure_ascii=False)

    secs = []
    for g in out["groups"]:
        head = (f'<h2>{html.escape(g["name_jp"])} '
                f'<span class="grade grade{g["grade"]} hdr" data-term="grade">業種級 {g["grade"]}</span> '
                f'<span class="gmeta">中央値 {_num(g["median"])} ／ {g["count"]}銘柄'
                f'{"" if g["tiered"] else " ・ 少数のため軍分けなし"}</span></h2>')
        trs = []
        for s in g["stocks"]:
            tcls = TIER_CLASS.get(s["tier"], "t0")
            dcls = DIR_CLASS.get(s["dir"], "fl")
            streak_v = _streak_val(s["streak_up"], s["streak_flat"])
            trs.append(
                f'<tr class="{tcls} r" data-tier="{s["tier"]}">'
                f'<td class="wl"><input type="checkbox" class="wlc" data-code="{s["code"]}" aria-label="ウォッチリストに追加"></td>'
                f'<td class="tier">{s["tier"]}<span class="dir {dcls}">{s["dir"]}</span></td>'
                f'<td class="code"><a href="reports/{s["code"]}.html">{s["code"]}</a></td>'
                f'<td class="nm">{html.escape(s["name"])}</td>'
                f'<td class="n px" data-v="{_v(s["price"])}">{_price(s["price"])}</td>'
                f'<td class="n" data-v="{_v(s["sel"])}">{_num(s["sel"],0)}</td>'
                f'<td class="n" data-v="{_v(s["tim"])}">{_num(s["tim"],0)}</td>'
                f'<td class="n" data-v="{_v(s["yield"])}">{_num(s["yield"],2)}%</td>'
                f'<td class="sk" data-v="{_v(streak_v)}">{_streak(s["streak_up"], s["streak_flat"])}</td>'
                f'<td class="cv">{s["cov_sel"]}</td>'
                f'</tr>')
        secs.append('<section class="grp">' + head + '<table><thead><tr>'
                    '<th class="wl" title="ウォッチリストに入れる銘柄にチェック">☑</th>'
                    '<th class="hdr" data-term="tier">軍</th><th>ティッカー</th><th>銘柄</th>'
                    '<th class="n"><span class="hdr" data-term="price">終値</span><span class="sortbtn">▼</span></th>'
                    '<th class="n"><span class="hdr" data-term="sel">選定</span><span class="sortbtn">▼</span></th>'
                    '<th class="n"><span class="hdr" data-term="tim">買い時</span><span class="sortbtn">▼</span></th>'
                    '<th class="n"><span class="hdr" data-term="yield">利回り</span><span class="sortbtn">▼</span></th>'
                    '<th><span class="hdr" data-term="streak">増配</span><span class="sortbtn">▼</span></th>'
                    '<th class="hdr" data-term="cov">カバレッジ</th>'
                    '</tr></thead><tbody>' + "".join(trs) + '</tbody></table></section>')

    gt = "".join(
        f'<tr class="r" data-tier="{s.get("tier","―")}">'
        f'<td class="wl"><input type="checkbox" class="wlc" data-code="{s["code"]}" aria-label="ウォッチリストに追加"></td>'
        f'<td class="n">{i+1}</td>'
        f'<td class="tier">{s.get("tier","―")}</td>'
        f'<td class="code"><a href="reports/{s["code"]}.html">{s["code"]}</a></td>'
        f'<td class="nm">{html.escape(s["name"])}</td>'
        f'<td class="n px" data-v="{_v(s["price"])}">{_price(s["price"])}</td>'
        f'<td class="sec">{html.escape(s["sector_jp"])}</td>'
        f'<td class="n" data-v="{_v(s["sel"])}">{_num(s["sel"],0)}</td>'
        f'<td class="n" data-v="{_v(s["tim"])}">{_num(s["tim"],0)}</td></tr>'
        for i, s in enumerate(out["global_top"]))

    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{THEME_HEAD}
<title>米国株 配当株 軍分けランキング</title>
<style>
{THEME_CSS}
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.7 -apple-system,"Hiragino Kaku Gothic ProN","Meiryo",sans-serif;
  background:var(--bg);color:var(--fg)}}
.wrap{{max-width:980px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:16px}}
.sub a{{color:var(--accent)}}
.summary{{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0 22px}}
.summary b{{display:block;font-size:20px}}
h2{{font-size:15px;margin:26px 0 6px;border-bottom:2px solid var(--line);padding-bottom:4px}}
.grade{{font-size:11px;padding:1px 7px;border-radius:10px;vertical-align:middle}}
.gradeA{{background:color-mix(in srgb,var(--gA) 15%,transparent);color:var(--gA)}}
.gradeB{{background:color-mix(in srgb,var(--gB) 15%,transparent);color:var(--gB)}}
.gradeC{{background:color-mix(in srgb,var(--gC) 15%,transparent);color:var(--gC)}}
.gmeta{{font-size:11px;color:var(--muted);font-weight:normal}}
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
.sk{{font-size:11px;color:var(--muted)}}
.cv{{font-size:11px}}
.disc{{margin-top:30px;padding:12px;background:var(--wbg);border:1px solid var(--wbd);
  border-radius:8px;font-size:11.5px;color:var(--wfg)}}
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
.sumbtn.active{{border-color:var(--accent);border-width:2px;background:var(--info)}}
.sumbtn b{{display:block;font-size:20px}}
a.sumbtn{{text-decoration:none;color:inherit}}
a.sumbtn.wlnav{{border-color:var(--accent);color:var(--accent)}}
a.sumbtn.wlnav b{{color:var(--accent)}}
td.wl,th.wl{{width:34px;text-align:center;padding-left:4px;padding-right:4px}}
th.wl{{color:var(--muted);cursor:default}}
.wlc{{width:16px;height:16px;cursor:pointer;accent-color:var(--accent)}}
#wlbar{{position:fixed;left:0;right:0;bottom:0;z-index:20;display:flex;gap:10px;
  align-items:center;justify-content:center;flex-wrap:wrap;
  background:var(--card);border-top:1px solid var(--line);
  box-shadow:0 -2px 10px rgba(0,0,0,.06);padding:10px 14px;font-size:13px}}
#wlbar[hidden]{{display:none}}
#wlbar b{{color:var(--accent)}}
#wlbar button{{padding:8px 16px;border:1px solid var(--accent);border-radius:8px;
  background:var(--accent);color:#fff;font-size:13px;cursor:pointer}}
#wlbar button.ghost{{background:var(--card);color:var(--muted);border-color:var(--line)}}
body.wlon{{padding-bottom:60px}}
.hdr{{cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px}}
.hdr:hover{{color:var(--accent)}}
.sortbtn{{cursor:pointer;color:var(--muted);font-size:10px;margin-left:4px;user-select:none;display:inline-block}}
.sortbtn:hover{{color:var(--accent)}}
.sortbtn.active{{color:var(--accent);font-weight:700}}
.terminfo{{position:relative;margin:8px 0 18px;padding:12px 36px 12px 14px;background:var(--ibg);
  border:1px solid var(--ibd);border-radius:8px;font-size:12.5px;line-height:1.7}}
.terminfo b{{display:block;margin-bottom:4px;font-size:13.5px}}
.ticlose{{position:absolute;top:6px;right:8px;border:none;background:none;cursor:pointer;
  font-size:15px;line-height:1;color:var(--muted);padding:4px}}
</style></head><body><div class="wrap">
<div class="topbar"><h1>米国株 配当株 軍分けランキング</h1><a href="terms.html">利用規約・免責事項</a></div>
{THEME_BAR}
<div class="sub">生成 {gen}　｜　スクリーン：{scr}　｜　<a href="guide.html">使い方・見方</a></div>
<div class="sub">表の見出し（軍・終値・選定・買い時・利回り・増配・カバレッジ・業種級）をクリックすると説明が出ます。左端の□にチェックを入れて下部の「ウォッチリストを作成」を押すと、その銘柄だけの一覧を作れます。</div>
<div class="summary">
  <button type="button" class="sumbtn" data-tier=""><b>{c['total']}</b>銘柄</button>
  <button type="button" class="sumbtn" data-tier="1軍"><b>{c['1軍']}</b>1軍</button>
  <button type="button" class="sumbtn" data-tier="2軍"><b>{c['2軍']}</b>2軍</button>
  <button type="button" class="sumbtn" data-tier="3軍"><b>{c['3軍']}</b>3軍</button>
  <a class="sumbtn wlnav" href="watchlist.html"><b id="wlnav-n">☆</b>ウォッチリスト</a>
  <a class="sumbtn wlnav" href="portfolio.html"><b id="pfnav-n">💼</b>ポートフォリオ</a>
</div>
<div class="sub" style="margin:-14px 0 14px">クリックでその軍だけ表示（もう一度押すと解除）</div>
<div class="searchbar">
  <input id="q" type="search" placeholder="ティッカー・銘柄名・業種で検索" autocomplete="off">
  <button id="qclear" type="button">クリア</button>
  <span class="hit" id="qhit"></span>
</div>
<div id="terminfo" class="terminfo" hidden>
  <button type="button" id="terminfo-close" class="ticlose" aria-label="閉じる">✕</button>
  <div id="terminfo-body"></div>
</div>
<details id="topbox"><summary>全体 選定スコア 上位50（業種横断）</summary>
<table><thead><tr><th class="wl">☑</th><th class="n">#</th><th class="hdr" data-term="tier">軍</th><th>ティッカー</th><th>銘柄</th>
<th class="n"><span class="hdr" data-term="price">終値</span><span class="sortbtn">▼</span></th><th>業種</th>
<th class="n"><span class="hdr" data-term="sel">選定</span><span class="sortbtn">▼</span></th>
<th class="n"><span class="hdr" data-term="tim">買い時</span><span class="sortbtn">▼</span></th></tr></thead><tbody>{gt}</tbody></table>
</details>
{"".join(secs)}
<div class="disc">{DISC}</div>
<div id="wlbar" hidden><span><b id="wlcount">0</b> 銘柄を選択中</span>
<button type="button" id="wlgo">ウォッチリストを作成 →</button>
<button type="button" id="pfgo">ポートフォリオに追加 →</button>
<button type="button" id="wlclear2" class="ghost">選択をクリア</button></div>
<script>
{THEME_JS}
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

  // ---- ウォッチリスト選択 ----
  var WL_KEY = 'wl_codes_us';
  var wlSet;
  try {{
    wlSet = new Set((localStorage.getItem(WL_KEY) || '').split(',').map(function(s){{return s.trim();}}).filter(Boolean));
  }} catch(e) {{ wlSet = new Set(); }}
  var wlBar = document.getElementById('wlbar');
  var wlCount = document.getElementById('wlcount');
  var wlNav = document.getElementById('wlnav-n');
  function wlSave(){{ try {{ localStorage.setItem(WL_KEY, Array.from(wlSet).join(',')); }} catch(e) {{}} }}
  function wlBadge(){{
    wlCount.textContent = String(wlSet.size);
    if (wlNav) wlNav.textContent = wlSet.size ? String(wlSet.size) : '☆';
    wlBar.hidden = wlSet.size === 0;
    document.body.classList.toggle('wlon', wlSet.size > 0);
  }}
  function wlSync(){{
    document.querySelectorAll('.wlc').forEach(function(cb){{ cb.checked = wlSet.has(cb.dataset.code); }});
    wlBadge();
  }}
  document.addEventListener('change', function(e){{
    var cb = e.target;
    if (!cb.classList || !cb.classList.contains('wlc')) return;
    var code = cb.dataset.code;
    if (cb.checked) wlSet.add(code); else wlSet.delete(code);
    document.querySelectorAll('.wlc[data-code="' + code + '"]').forEach(function(o){{ o.checked = cb.checked; }});
    wlBadge();
    wlSave();
  }});
  document.getElementById('wlgo').addEventListener('click', function(){{
    if (!wlSet.size) return;
    wlSave();
    location.href = 'watchlist.html?codes=' + encodeURIComponent(Array.from(wlSet).join(','));
  }});
  document.getElementById('pfgo').addEventListener('click', function(){{
    if (!wlSet.size) return;
    location.href = 'portfolio.html?add=' + encodeURIComponent(Array.from(wlSet).join(','));
  }});
  document.getElementById('wlclear2').addEventListener('click', function(){{
    wlSet.clear(); wlSave(); wlSync();
  }});
  wlSync();
  // ポートフォリオの保有銘柄数バッジ（保有記録はチェック選択と別のlocalStorageキー）
  try {{
    var pfNav = document.getElementById('pfnav-n');
    if (pfNav) {{
      var pfRaw = localStorage.getItem('pf_lots_us') || '';
      var pfCodes = {{}};
      pfRaw.split(';').forEach(function(chunk){{
        var c = chunk.split(':')[0];
        if (c) pfCodes[c] = 1;
      }});
      var pfN = Object.keys(pfCodes).length;
      pfNav.textContent = pfN ? String(pfN) : '💼';
    }}
  }} catch(e) {{}}
}})();
</script>
</div></body></html>"""


if __name__ == "__main__":
    main()
