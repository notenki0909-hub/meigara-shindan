# -*- coding: utf-8 -*-
"""
配当株ツール（日本株／米国株）の母集団（universe.json／universe_us.json）の四半期見直しを
「自動反映」するための部品。universe-quarterly.yml／universe-quarterly-us.yml と、
rank.py／rank_us.py／batch.py／batch_us.py が使う。
（10年保有側の universe_guard.py／universe_changes.py とは別物。依存を作らないため、
  考え方だけ参考にして配当側専用に書いた。）

  python div_universe.py guard  jp|us [--force]   # 反映前の安全確認（異常なら終了コード1）
  python div_universe.py record jp|us             # 入れ替え記録・外れた銘柄の記録を更新

■ 安全確認（guard）
  直前のコミット（git HEAD）の母集団と、作り直した母集団を比べ、次をすべて満たすときだけ終了コード0。
    ・銘柄数が前回の85〜115%の範囲
    ・追加＋除外の合計が前回の銘柄数の25%以内（かつ最低30件は許容）
    ・「非減配の不足」を理由にした除外が10銘柄以内
      （配当の支払いパターンの変更などで減配と誤判定された銘柄が大量に出ていないかの確認）
  取得元（JPXの一覧・yfinance）が壊れたまま母集団が上書きされるのを防ぐ。
  --force を付けると警告だけ出して0を返す（人が内容を確認した上で反映したいとき）。

■ 記録（record）＝ universe_history_jp.json／universe_history_us.json
  ・period_start／display_until／events … 今回の見直しで追加・除外された銘柄（NEW!と「今回の入れ替え」欄の元）。
    表示期限は次の四半期の見直し予定日（日本株は1/4/7/10月の1日、米国株は2日）。
  ・retired … 母集団から外れた銘柄。外れた日から1年後の見直し予定日（until）まで、夜間更新（batch）が
    更新を続ける（ポートフォリオ等で保有している人のため）。期限後は更新せず、画面に「更新終了」と表示。
    母集団に戻ったら記録を消す。
"""
import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
JST = dt.timezone(dt.timedelta(hours=9))

MARKETS = {
    "jp": {"label": "日本株", "universe": "universe.json", "key": "codes", "id": "code",
           "history": "universe_history_jp.json", "review_day": 1},
    "us": {"label": "米国株", "universe": "universe_us.json", "key": "tickers", "id": "ticker",
           "history": "universe_history_us.json", "review_day": 2},
}

COUNT_RANGE = (0.85, 1.15)      # 銘柄数が前回のこの範囲
CHANGE_RATIO = 0.25             # 追加＋除外の合計の上限（前回銘柄数に対する割合）
CHANGE_MIN = 30                 # 上のしきい値の下限（母集団が小さくても最低これだけは許容）
NOCUT_LIMIT = 10                # 「非減配の不足」による除外の上限
RETIRE_DAYS_KEEP = 365          # 更新終了後、記録を残す日数（画面に「更新終了」と出し続ける期間）


# ---------------------------------------------------------------- 日付
def today_jst():
    return dt.datetime.now(JST).date()


def _review_dates(market, year):
    day = MARKETS[market]["review_day"]
    return [dt.date(year, m, day) for m in (1, 4, 7, 10)]


def next_review(market, d):
    """dより後の、次の四半期見直し予定日。"""
    for y in (d.year, d.year + 1):
        for c in _review_dates(market, y):
            if c > d:
                return c
    raise AssertionError("unreachable")


def nominal_review(market, d):
    """d以前で最も新しい、四半期見直し予定日（手動実行が数日ずれても、同じ四半期の予定日に揃える）。"""
    best = None
    for y in (d.year - 1, d.year):
        for c in _review_dates(market, y):
            if c <= d:
                best = c
    return best


def retire_until(market, d):
    """外れた日dから1年後の見直し予定日（更新を続ける期限）。"""
    n = nominal_review(market, d)
    return dt.date(n.year + 1, n.month, n.day)


def jp_date(iso):
    try:
        d = dt.date.fromisoformat(str(iso))
        return f"{d.year}年{d.month}月{d.day}日"
    except Exception:
        return ""


# ---------------------------------------------------------------- 母集団ファイル
def _norm(market, x):
    return str(x).strip().upper() if market == "us" else str(x).strip()


def members(market, doc):
    """母集団ドキュメント → {id: name}"""
    cfg = MARKETS[market]
    items = doc.get(cfg["key"]) or []
    out = {}
    for i in items:
        if isinstance(i, dict):
            out[_norm(market, i.get(cfg["id"]) or i.get("code") or i.get("ticker"))] = i.get("name") or ""
        else:
            out[_norm(market, i)] = ""
    return out


def rejected_map(market, doc):
    """除外リスト → {id: 記録(dict)}"""
    cfg = MARKETS[market]
    out = {}
    for i in doc.get("rejected") or []:
        k = i.get(cfg["id"]) or i.get("code") or i.get("ticker")
        if k is not None:
            out[_norm(market, k)] = i
    return out


def _head(path):
    """git HEAD にあるファイルの内容（無ければ None）。"""
    r = subprocess.run(["git", "show", "HEAD:%s" % path], cwd=HERE, capture_output=True)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout.decode("utf-8"))


def _load(path):
    p = os.path.join(HERE, path)
    if not os.path.isfile(p):
        return None
    return json.load(open(p, encoding="utf-8"))


def _sectors(market, doc):
    """追加銘柄の業種（表示用）。米国株は母集団に入っているGICS業種、日本株は候補一覧(universe_candidates.json)の33業種。"""
    cfg = MARKETS[market]
    out = {}
    if market == "us":
        for i in doc.get(cfg["key"]) or []:
            if isinstance(i, dict):
                out[_norm(market, i.get(cfg["id"]))] = i.get("gics_sector") or ""
    else:
        cand = _load("universe_candidates.json") or {}
        for i in cand.get("candidates") or []:
            out[_norm(market, i.get("code"))] = i.get("sector") or ""
    return out


def diff_universe(market):
    """作り直した母集団（作業ツリー）と git HEAD の差分。"""
    cfg = MARKETS[market]
    new_doc = _load(cfg["universe"]) or {}
    old_doc = _head(cfg["universe"]) or {}
    new, old = members(market, new_doc), members(market, old_doc)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    return {"new": new, "old": old, "added": added, "removed": removed,
            "rej": rejected_map(market, new_doc), "sect": _sectors(market, new_doc)}


def _is_nocut(rej_rec):
    return str((rej_rec or {}).get("reason", "")).startswith("非減配")


# ---------------------------------------------------------------- 安全確認
def check(market):
    d = diff_universe(market)
    n_old, n_new = len(d["old"]), len(d["new"])
    problems = []
    nocut = [c for c in d["removed"] if _is_nocut(d["rej"].get(c))]
    if n_new == 0:
        problems.append("銘柄数が0です")
    if n_old:
        lo, hi = COUNT_RANGE
        if not (lo * n_old <= n_new <= hi * n_old):
            problems.append("銘柄数が前回の%d〜%d%%の範囲外です（%d → %d）"
                            % (round(lo * 100), round(hi * 100), n_old, n_new))
        limit = max(CHANGE_MIN, math.ceil(CHANGE_RATIO * n_old))
        total = len(d["added"]) + len(d["removed"])
        if total > limit:
            problems.append("入れ替えが多すぎます（追加%d＋除外%d＝%d件 > 上限%d件）"
                            % (len(d["added"]), len(d["removed"]), total, limit))
    if len(nocut) > NOCUT_LIMIT:
        problems.append("「非減配の不足」による除外が多すぎます（%d件 > 上限%d件。配当の支払いパターンの変更などで"
                        "減配と誤判定されている可能性があります）" % (len(nocut), NOCUT_LIMIT))
    return {"market": market, "n_old": n_old, "n_new": n_new, "added": d["added"], "removed": d["removed"],
            "names": {**d["old"], **d["new"]}, "nocut": nocut, "problems": problems}


def _fmt(codes, names):
    return "、".join("%s（%s）" % (c, names.get(c, "")) if names.get(c) else c for c in codes) or "なし"


def cmd_guard(args):
    r = check(args.market)
    lab = MARKETS[args.market]["label"]
    md = ["## 配当株ランキング 母集団の更新結果（%s）" % lab, "",
          "- 銘柄数：%d → %d" % (r["n_old"], r["n_new"]),
          "- 追加（%d件）：%s" % (len(r["added"]), _fmt(r["added"], r["names"])),
          "- 除外（%d件）：%s" % (len(r["removed"]), _fmt(r["removed"], r["names"])),
          "- うち「非減配の不足」による除外：%d件" % len(r["nocut"])]
    for p in r["problems"]:
        md.append("- ⚠ **%s**" % p)
    bad = bool(r["problems"])
    if bad and args.force:
        md += ["", "※ 安全確認で警告がありましたが、--force 指定のため自動反映を進めます。"]
    elif bad:
        md += ["", "※ 安全確認で異常を検出したため、自動反映を見送りました（内容を確認し、問題なければ"
                   "ワークフローを手動実行して force を true にしてください）。"]
    text = "\n".join(md) + "\n"
    open(os.path.join(HERE, args.out), "w", encoding="utf-8").write(text)

    summary = "%s +%d/-%d" % (lab, len(r["added"]), len(r["removed"]))
    msg = ["chore(universe%s): %s %s（%s）" % ("-us" if args.market == "us" else "", args.title,
                                                  dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"), summary), "",
           "%s：%d → %d銘柄" % (lab, r["n_old"], r["n_new"]),
           "  追加：%s" % _fmt(r["added"], r["names"]),
           "  除外：%s" % _fmt(r["removed"], r["names"])]
    open(os.path.join(HERE, args.msg), "w", encoding="utf-8").write("\n".join(msg) + "\n")
    print(text)
    if bad and not args.force:
        sys.exit(1)


# ---------------------------------------------------------------- 記録
def load_history(market):
    return _load(MARKETS[market]["history"]) or {}


def cmd_record(args, today=None):
    market = args.market
    today = today or (dt.date.fromisoformat(args.date) if getattr(args, "date", None) else today_jst())
    path = os.path.join(HERE, MARKETS[market]["history"])
    hist = load_history(market)
    d = diff_universe(market)
    retired = dict(hist.get("retired") or {})

    # 外れた銘柄：更新継続の期限を付けて記録。母集団に戻った銘柄は記録から外す。
    until = retire_until(market, today).isoformat()
    for c in d["removed"]:
        rec = d["rej"].get(c) or {}
        retired[c] = {"name": d["old"].get(c) or rec.get("name") or "",
                      "sector": rec.get("sector") or rec.get("gics_sector") or "",
                      "removed_on": today.isoformat(), "until": until,
                      "reason": rec.get("reason") or ""}
    for c in list(retired):
        if c in d["new"]:
            del retired[c]
    # 更新終了から十分たった記録は消す
    keep_after = today - dt.timedelta(days=RETIRE_DAYS_KEEP)
    for c in list(retired):
        try:
            if dt.date.fromisoformat(retired[c]["until"]) < keep_after:
                del retired[c]
        except Exception:
            del retired[c]

    out = dict(hist)
    out["retired"] = retired
    has_change = bool(d["added"] or d["removed"])
    period_over = True
    try:
        period_over = dt.date.fromisoformat(hist.get("display_until", "1970-01-01")) <= today
    except Exception:
        pass
    if has_change:
        # 四半期の見直しのたびに「期間」を新しく始める（前の期間のNEW!・外れた銘柄の記録は消える）
        out.update({
            "period_start": today.isoformat(),
            "display_until": next_review(market, today).isoformat(),
            "events": [{"date": today.isoformat(),
                        "added": [{"code": c, "name": d["new"].get(c, ""), "sector": d["sect"].get(c, "")}
                      for c in d["added"]],
                        "removed": [{"code": c, "name": d["old"].get(c, "")} for c in d["removed"]]}]})
    elif period_over:
        out.update({"period_start": today.isoformat(),
                    "display_until": next_review(market, today).isoformat(), "events": []})
    # 変化がなく期間も続いているとき（同日の再実行など）は、期間・events を触らない
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("%s：追加%d／除外%d → 記録を更新（表示期限 %s、更新継続中の外れた銘柄 %d件）"
          % (MARKETS[market]["label"], len(d["added"]), len(d["removed"]), out.get("display_until"),
             sum(1 for v in retired.values() if v["until"] >= today.isoformat())))


# ---------------------------------------------------------------- 他スクリプトが使う部品
def active_retired_codes(market, today=None):
    """夜間更新（batch）の対象に足す、更新継続中（期限内）の外れた銘柄 → [(code, name)]"""
    today = (today or today_jst()).isoformat()
    return [(c, v.get("name") or None) for c, v in sorted((load_history(market).get("retired") or {}).items())
            if v.get("until", "") >= today]


def retired_info(market, today=None):
    """ランキング用：{code: {removed_on, until, expired, reason, name, sector}}（更新終了後の記録も含む）"""
    today = (today or today_jst()).isoformat()
    out = {}
    for c, v in (load_history(market).get("retired") or {}).items():
        out[c] = {**v, "expired": v.get("until", "") < today}
    return out


def period_active(hist, today=None):
    """表示期限（次の四半期の見直し予定日）をまだ過ぎていないか。過ぎていたらNEW!・入れ替え欄は出さない。"""
    try:
        return dt.date.fromisoformat(hist.get("display_until", "")) > (today or today_jst())
    except Exception:
        return False


def new_map(market):
    """今の期間に追加された銘柄 → {code: 追加日}（表示期限後は空）"""
    out = {}
    hist = load_history(market)
    if not period_active(hist):
        return out
    for ev in hist.get("events") or []:
        for a in ev.get("added") or []:
            out[_norm(market, a["code"])] = ev["date"]
    return out


NEWBADGE_CSS = """.newbadge{font-size:10px;font-weight:700;padding:1px 6px;border-radius:10px;vertical-align:middle;
  background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent);
  border:1px solid color-mix(in srgb,var(--accent) 45%,transparent)}
.retnote{font-size:11px;color:var(--muted);white-space:normal}
#changebox table{margin-top:6px}"""


def newbadge(code, nm, display_until):
    """銘柄名の横に付けるNEW!バッジ（追加日と表示期限をツールチップに入れる）。"""
    at = nm.get(code)
    if not at:
        return ""
    title = "%sの入れ替えで追加された銘柄です" % jp_date(at)
    if display_until:
        title += "（NEW!は%sまで表示）" % jp_date(display_until)
    return ' <span class="newbadge" title="%s">NEW!</span>' % title


def retired_note(info):
    """「データの更新は〇年〇月〇日まで」の文言（更新終了後は終了の文言）。"""
    if not info:
        return ""
    if info.get("expired"):
        return "対象外（参考）：データの更新は%sで終了しました" % jp_date(info.get("until"))
    return "対象外（参考）：データの更新は%sまで" % jp_date(info.get("until"))


def retired_banner_html(info):
    """個別の診断ページの上部に出す、対象外（参考）の注記。期限後の文言は閲覧時の日付で切り替える。"""
    until, removed = info.get("until", ""), info.get("removed_on", "")
    ended = "データの更新は%sで終了しました（最後に更新した値を表示しています）。" % jp_date(until)
    return (
        '<div class="retbanner" data-until="%s" style="margin:10px 0;padding:10px 12px;border:1px solid var(--line);'
        'border-radius:8px;background:var(--card);color:var(--fg);font-size:13px;line-height:1.7">'
        '<b>対象外（参考）</b>：この銘柄は、%sの四半期見直しで配当株ランキングの母集団から外れました。'
        '<span class="retmsg">ポートフォリオ等で保有している人のため、データは%sまで更新します'
        '（期限後は更新を止め、最後の値を表示します）。</span>'
        '<script>(function(){var e=document.currentScript.parentNode,u=e.getAttribute("data-until");'
        'if(u&&u<new Date().toISOString().slice(0,10)){e.querySelector(".retmsg").textContent="%s";}})();</script>'
        '</div>' % (until, jp_date(removed), jp_date(until), ended))


def inject_banner(report_html, info):
    """診断ページ(report_html)のヘッダー（topbar）の直後に、対象外の注記を差し込む。見つからなければそのまま返す。"""
    i = report_html.find('class="topbar"')
    if i < 0:
        return report_html
    j = report_html.find("</div>", i)
    if j < 0:
        return report_html
    j += len("</div>")
    return report_html[:j] + "\n" + retired_banner_html(info) + report_html[j:]


def changebox_html(market, hist, by_code, sector_of, esc, sector_tr=lambda x: x):
    """「今回の入れ替え」欄（見出しクリックで開閉。既定は閉）。by_code＝ランキング対象の行（code→row）。"""
    if not hist or not hist.get("period_start") or not period_active(hist):
        return ""
    until = jp_date(hist.get("display_until"))
    period = jp_date(hist.get("period_start"))
    adds = [a for ev in hist.get("events") or [] for a in (ev.get("added") or [])]
    adate = {a["code"]: ev["date"] for ev in hist.get("events") or [] for a in (ev.get("added") or [])}
    rems = [(r, ev["date"]) for ev in hist.get("events") or [] for r in (ev.get("removed") or [])]
    ret = retired_info(market)
    gm = ('・NEW!は%sまで表示' % until) if until else ""
    if not adds and not rems:
        return ('<details id="changebox"><summary>今回の入れ替え <span class="gmeta">なし</span></summary>'
                '<p class="sub" style="margin:6px 0 0">前回の四半期の見直し（%s）以降、入れ替わった銘柄はありません。%s</p></details>'
                % (period, ('次の見直し予定日は%sです。' % until) if until else ""))
    note = ('追加された銘柄の「NEW!」と、この一覧は<b>%s（次の四半期の見直し予定日）まで</b>表示します。'
            '見直しのたびに、その時の入れ替えの内容に切り替わります。' % until) if until else ""
    add_rows = "".join(
        '<tr><td class="code"><a href="reports/{c}.html">{c}</a></td><td class="nm">{n}</td>'
        '<td class="sec">{s}</td>{sc}<td class="sec">{d}</td></tr>'.format(
            c=esc(a["code"]), n=esc(a.get("name") or ""), s=esc(sector_of(a["code"]) or sector_tr(a.get("sector") or "")),
            sc=('<td class="n">%s</td>' % (round(by_code[a["code"]]["sel"]) if isinstance(by_code[a["code"]].get("sel"), (int, float)) else "―"))
            if a["code"] in by_code else '<td class="sec" title="翌日の夜間更新で診断し、ランキングに反映されます">診断待ち</td>',
            d=jp_date(adate.get(a["code"]))) for a in adds) or '<tr><td colspan="5" class="sec">なし</td></tr>'
    rem_rows = "".join(
        '<tr><td class="code"><a href="reports/{c}.html">{c}</a></td><td class="nm">{n}</td>'
        '<td class="sec">{s}</td><td class="sec">{d}</td><td class="sec">{r}</td></tr>'.format(
            c=esc(r["code"]), n=esc(r.get("name") or ""), s=esc((ret.get(r["code"]) or {}).get("sector") or ""),
            d=jp_date(dte),
            r=esc(((ret.get(r["code"]) or {}).get("reason") or "母集団から外れました")
                  + ("　／　" + retired_note(ret[r["code"]]).replace("対象外（参考）：", "") if r["code"] in ret else "")))
        for r, dte in rems) or '<tr><td colspan="5" class="sec">なし</td></tr>'
    return ('<details id="changebox"><summary>今回の入れ替え <span class="gmeta">追加 %d／外れた %d%s</span></summary>'
            '<p class="sub" style="margin:6px 0 8px">前回の四半期の見直し（%s）以降に入れ替わった銘柄です。%s'
            '外れた銘柄も、ポートフォリオ等で保有している人のため、表示の期限まで夜間にデータを更新します。</p>'
            '<table><thead><tr><th>コード</th><th>銘柄</th><th>業種</th><th class="n">選定</th><th>追加日</th></tr></thead>'
            '<tbody>%s</tbody></table>'
            '<table style="margin-top:8px"><thead><tr><th>コード</th><th>銘柄</th><th>業種</th><th>外れた日</th>'
            '<th>理由・データ更新</th></tr></thead><tbody>%s</tbody></table></details>'
            % (len(adds), len(rems), gm, period, note, add_rows, rem_rows))


# ---------------------------------------------------------------- CLI
def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windowsのcp932でも「⚠」等を出力できるように
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("guard")
    g.add_argument("market", choices=["jp", "us"])
    g.add_argument("--force", action="store_true")
    g.add_argument("--out", default="universe_change.md")
    g.add_argument("--msg", default="universe_commit_msg.txt")
    g.add_argument("--title", default="四半期見直し")
    r = sub.add_parser("record")
    r.add_argument("market", choices=["jp", "us"])
    r.add_argument("--date", default=None, help="記録する日付（YYYY-MM-DD。省略時は日本時間の今日）")
    args = ap.parse_args()
    {"guard": cmd_guard, "record": cmd_record}[args.cmd](args)


if __name__ == "__main__":
    main()
