"""公開用の読み取り専用ランキングを、1つの集計結果（ranking.json）から作る。
同じ結果から index.html（Web画面）と report.md（定期レポート）を書き出すので、評価日・入力・版が必ずそろう。
保有・資金・売買判断の設定は出さない。

使い方: python export_public.py [出力先]   （既定 C:/Augma/ai-work/stock-public）
読むDB: 環境変数 STOCK_DB_PATH があればそれ、無ければ backend/data/stock.db（どちらも読み取り専用で開く）"""
import hashlib
import html
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("STOCK_DB_PATH") or ROOT / "backend" / "data" / "stock.db")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "C:/Augma/ai-work/stock-public")
CATEGORY = {"growth": "成長", "emerging_loss": "赤字成長", "cyclical": "景気連動", "stable": "安定"}
ORIGIN = {"research": "調査前提", "manual": "登録済みの前提", "auto": "自動初期推計"}
ASSUME = {"verified": "承認済み", "provisional": "承認前（暫定）", "review_required": "要再承認", "unconfirmed": "未確認"}
# 購入候補の点数条件（backend/stock_analysis/scoring/rules.py の通常購入候補と同じ）
RULE = {"growth": 80, "buy": 75, "sell_max": 40}
DEFINITIONS = {
    "growth": "成長期待（0〜100）: 売上・営業利益の伸び、収益性、財務の安全性、2029年までの伸びの見込みを重み付きで平均。"
              "数字のそろっていない項目は除いて計算し、そろい具合が5割未満なら算出しない。",
    "buy": "買い（0〜100）: 割安度だけを表す。理論価値÷株価 が 1 なら50点、1.25 倍以上で100点、0.75 倍以下で0点（50＋200×(理論価値÷株価−1)）。"
           "買い90点は「90%の確率で上がる」という意味ではない。成長性や危険度は含まない。",
    "sell": "売り（0〜100）: 割高度・業績鈍化・下方修正・財務悪化・増資・株価過熱・見通し悪化を重み付きで平均。"
            "下方修正か増資・希薄化が80点以上のときは、平均で薄めず「その点数×0.8」を下限にする。そろい具合が5割未満なら算出しない。",
    "tv": "理論価値: 2029年度の1株利益（EPS）×想定PER で2029年の株価を出し、必要利回りで今の価値に割り戻した値。"
          "狙い目は 理論価値×(1−安全余裕)。前提が承認前のものは暫定。",
    "confidence": "信頼度（v4・買い）: 高＝前提が承認済みで成長・売りの項目が8割以上そろう／中＝調査前提（承認前）で7割以上／"
                  "低＝自動初期推計、項目が7割未満、または重大な注意あり／なし＝買い未評価。v5 の信頼度（A〜D）は v5 の基準で別に計算。",
}


def q(conn, sql, *a):
    return conn.execute(sql, a).fetchall()


def factors(detail):
    return [{"label": x["label"], "score": x.get("score"), "weight": x.get("weight")}
            for x in (detail.get("factors") or {}).values()]


def sell_floor(sd):
    """売りが「下方修正・増資の 80 点以上 → その 0.8 倍を下限」で決まったかを判定する。"""
    fs = sd.get("factors") or {}
    avail = {k: x for k, x in fs.items() if x.get("score") is not None and x.get("weight")}
    if sd.get("score") is None or not avail:
        return None
    if sd.get("floor_only"):
        k = max((k for k in ("downward_revision", "dilution") if k in avail), key=lambda k: avail[k]["score"])
        return (f"項目のそろい具合が {sd.get('coverage', 0):.0%}（必要 50%）で平均は出せないが、{avail[k]['label']} {avail[k]['score']:.0f}点が"
                f"80点以上のため、その0.8倍 {sd['score']}点を暫定の売り点数とした")
    base = sum(x["weight"] * x["score"] for x in avail.values()) / sum(x["weight"] for x in avail.values())
    crit = max((avail[k]["score"] for k in ("downward_revision", "dilution") if k in avail), default=0)
    if crit >= 80 and crit * 0.8 > base:
        lab = next(avail[k]["label"] for k in ("downward_revision", "dilution") if k in avail and avail[k]["score"] == crit)
        return f"{lab}が{crit:.0f}点のため、重み付き平均 {base:.0f}点ではなく下限 {crit * 0.8:.0f}点を採用（固定値ではない）"
    return None


def build(conn):
    d = q(conn, "SELECT MAX(run_date) FROM scores")[0][0]
    rows = q(conn, """SELECT s.*, sec.name, sec.category, sec.asset_type,
                             v.growth_formal, v.growth_reference, v.buy_score AS v5_buy, v.sell_formal, v.sell_reference,
                             v.theoretical_value AS v5_tv, v.confidence AS v5_conf, v.severe_risks, v.buy_detail AS v5_bd,
                             v.method_version AS v5_ver
                      FROM scores s JOIN securities sec ON sec.code=s.code
                      LEFT JOIN scores_v5 v ON v.code=s.code AND v.run_date=s.run_date
                      WHERE s.run_date=? AND sec.active=1""", d)
    failed = {r[0].split(":")[1] for r in q(conn, """SELECT target FROM fetch_log f WHERE target LIKE 'financials:%' AND status!='ok'
                 AND fetched_at=(SELECT MAX(fetched_at) FROM fetch_log g WHERE g.target=f.target)""")}
    items = []
    for r in rows:
        bd = json.loads(r["buy_detail"] or "{}")
        gd, sd = json.loads(r["growth_detail"] or "{}"), json.loads(r["sell_detail"] or "{}")
        inp = bd.get("inputs") or {}
        rated = r["buy_status"] == "rated"
        risks = json.loads(r["severe_risks"] or "[]")
        etf = r["asset_type"] != "stock"
        nofin = r["code"] in failed and not q(conn, "SELECT 1 FROM financials WHERE code=? LIMIT 1", r["code"])

        def status(score, coverage=None, floor_only=False):
            if score is not None:
                return "暫定（重大要因のみで算出）" if floor_only else "算出済み"
            if etf:
                return "対象外"
            if nofin:
                return "取得失敗"
            return "データ不足" + (f"（項目のそろい具合 {coverage:.0%}・必要 50%）" if coverage is not None else "")

        if rated:
            buy_status = "算出済み" if bd.get("assumption_status") == "verified" else "暫定"
        else:
            kind = bd.get("unrated_kind") or ("対象外" if etf else "データ不足")
            buy_status = "取得失敗" if "取得失敗" in (bd.get("reason") or "") else kind
        cov = min(x for x in (gd.get("coverage"), sd.get("coverage")) if x is not None) if (gd.get("coverage") is not None and sd.get("coverage") is not None) else 0
        conf = ("なし" if not rated else
                "低" if bd.get("assumption_origin") == "auto" or cov < 0.7 or risks else
                "高" if bd.get("assumption_status") == "verified" and cov >= 0.8 else "中")
        eps = inp.get("eps_forecast") or inp.get("normalized_eps")
        tv_formula = None
        if rated:
            tv_formula = (f"{inp.get('target_year')}年度の{'正常化' if inp.get('normalized_eps') else '予想'}EPS {eps:,.1f}円 × 想定PER {inp.get('assumed_per'):g}倍"
                          f" = 将来株価 {bd.get('future_price'):,.0f}円 → 必要利回り {inp.get('required_return'):.1%} で {bd.get('years'):.2f}年 割り戻し"
                          f" = 理論価値 {r['theoretical_value']:,.0f}円")
        # 購入候補の判定（点数条件・前提の承認・重大な注意を分けて示す）
        why_not = []
        if not rated:
            why_not.append("買い未評価")
        else:
            if (r["growth_score"] or 0) < RULE["growth"]:
                why_not.append(f"成長{RULE['growth']}未満")
            if r["buy_score"] < RULE["buy"]:
                why_not.append(f"買い{RULE['buy']}未満")
            if r["sell_score"] is None or r["sell_score"] > RULE["sell_max"]:
                why_not.append(f"売り{RULE['sell_max']}超または未算定")
            if bd.get("assumption_status") != "verified":
                why_not.append("前提が未承認")
        if risks:
            why_not.append("重大な注意あり")
        b5 = json.loads(r["v5_bd"] or "{}")
        pb = b5.get("per_basis") or {}
        items.append({
            "code": r["code"], "name": r["name"], "category": r["category"], "category_ja": CATEGORY.get(r["category"], r["category"]),
            "asset_type": r["asset_type"], "price": r["price"], "price_date": r["price_date"],
            "growth": r["growth_score"], "buy": r["buy_score"] if rated else None, "sell": r["sell_score"],
            "status": {"growth": status(r["growth_score"], gd.get("coverage")), "buy": buy_status,
                       "sell": status(r["sell_score"], sd.get("coverage"), sd.get("floor_only"))},
            "unrated_reason": None if rated else bd.get("reason"),
            "confidence": conf, "coverage": {"growth": gd.get("coverage"), "sell": sd.get("coverage")},
            "theoretical_value": r["theoretical_value"] if rated else None,
            "gap_pct": round((r["theoretical_value"] / r["price"] - 1) * 100, 1) if rated and r["theoretical_value"] and r["price"] else None,
            "target_price": r["target_price"] if rated else None,
            "target_note": None if not rated else ("承認済みの前提" if bd.get("assumption_status") == "verified" else "暫定（前提が未承認のため参考）"),
            "tv_formula": tv_formula,
            "assumption": None if not rated else {
                "origin": ORIGIN.get(bd.get("assumption_origin"), bd.get("assumption_origin")),
                "status": ASSUME.get(bd.get("assumption_status"), bd.get("assumption_status")),
                "eps": eps, "eps_kind": "正常化EPS" if inp.get("normalized_eps") else "予想EPS", "target_year": inp.get("target_year"),
                "per": inp.get("assumed_per"), "required_return": inp.get("required_return"), "safety_margin": inp.get("safety_margin"),
                "years": bd.get("years"), "reason": inp.get("reason")},
            "growth_factors": factors(gd), "sell_factors": factors(sd), "sell_floor": sell_floor(sd),
            "warnings": [{"type": x["type"], "detail": x["detail"],
                          "note": "株数は決算短信の発行済株式数から計算。増資の開示（一次資料）との照合は未実施" if x["type"] == "大規模希薄化" else None}
                         for x in risks],
            "candidate": {"eligible": not why_not, "why_not": why_not},
            "data_issue": bd.get("data_issue"),
            "sources": json.loads(r["indicators"] or "{}").get("sources") or [],
            "v4_version": r["method_version"].split("（")[0],
            "v5": None if r["v5_ver"] is None else {
                "version": r["v5_ver"], "growth": r["growth_formal"] if r["growth_formal"] is not None else r["growth_reference"],
                "growth_is_reference": r["growth_formal"] is None, "buy": r["v5_buy"],
                "sell": r["sell_formal"] if r["sell_formal"] is not None else r["sell_reference"],
                "sell_is_reference": r["sell_formal"] is None, "theoretical_value": r["v5_tv"], "confidence": r["v5_conf"],
                "per_median": pb.get("median"), "per_applied": pb.get("applied"), "per_notes": pb.get("anomalies") or []},
            "previous": previous(conn, r),
        })
    items.sort(key=lambda x: (x["buy"] is None, -(x["buy"] or 0), -(x["growth"] or 0)))
    fetched = {k: q(conn, f"SELECT MAX(fetched_at) FROM {t}")[0][0] for k, t in (("quotes", "daily_quotes"), ("financials", "financials"))}
    meta = {
        "run_date": d, "price_basis": f"{d} 終値", "fetched_at": fetched, "scored_at": max(r["created_at"] for r in rows),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "v4_version": sorted({i["v4_version"] for i in items}),
        "v5_version": sorted({i["v5"]["version"] for i in items if i["v5"]}),
        "official": "v4（正式）。v5 は比較用の参考で、正式採用前",
        "update": "手動更新（自動更新の仕組みは無い。採点し直したら書き出し直す）",
        "total": len(items),
        "counts": {k: sum(i[k] is not None for i in items) for k in ("growth", "buy", "sell")},
        "status_counts": {k: _count(i["status"][k] for i in items) for k in ("growth", "buy", "sell")},
        "definitions": DEFINITIONS, "rule": RULE,
    }
    return {"meta": meta, "items": items}


def _count(xs):
    out = {}
    for x in xs:
        k = x.split("（")[0]
        out[k] = out.get(k, 0) + 1
    return out


def previous(conn, r):
    """前回の値（同じ評価日の旧版、無ければ前の評価日）と、変わった理由の区分。"""
    p = conn.execute("""SELECT method_version, price, growth_score, buy_score, buy_status, sell_score, theoretical_value, indicators
                        FROM scores_archive WHERE code=? AND run_date=? ORDER BY archived_at DESC LIMIT 1""",
                     (r["code"], r["run_date"])).fetchone() if _has(conn, "scores_archive") else None
    same_day = p is not None
    if not p:
        p = conn.execute("""SELECT method_version, price, growth_score, buy_score, buy_status, sell_score, theoretical_value, indicators
                            FROM scores WHERE code=? AND run_date<? ORDER BY run_date DESC LIMIT 1""", (r["code"], r["run_date"])).fetchone()
    if not p:
        return None
    reasons = []
    if p["method_version"].split("（")[0] != r["method_version"].split("（")[0]:
        reasons.append(f"計算方式の版の変更（{p['method_version'].split('（')[0]}→{r['method_version'].split('（')[0]}）")
    if not same_day and p["price"] != r["price"]:
        reasons.append("株価の変動")
    if json.loads(p["indicators"] or "{}").get("sources") != json.loads(r["indicators"] or "{}").get("sources"):
        reasons.append("決算・予想データの更新")
    pb = p["buy_score"] if p["buy_status"] == "rated" else None
    nb = r["buy_score"] if r["buy_status"] == "rated" else None
    ch = {k: [a, b] for k, a, b in (("growth", p["growth_score"], r["growth_score"]), ("buy", pb, nb),
                                    ("sell", p["sell_score"], r["sell_score"]), ("tv", p["theoretical_value"], r["theoretical_value"]))
          if a != b}
    return {"version": p["method_version"].split("（")[0], "changes": ch, "reasons": reasons if ch else []}


def _has(conn, t):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def num(v, digits=0):
    return "-" if v is None else f"{v:,.{digits}f}"


def score_cell(v, st):
    return num(v) if v is not None else html.escape(st.split("（")[0])


def render_html(res, fp):
    m, items = res["meta"], res["items"]
    rows = []
    for x in items:
        warn = "".join(f"<span class=w>⚠{html.escape(w['type'])}</span>" for w in x["warnings"])
        g, b, s = (-1 if v is None else v for v in (x["growth"], x["buy"], x["sell"]))
        gap = "-" if x["gap_pct"] is None else f"{x['gap_pct']:+.0f}%"
        prov = "<small class=pv>暫定</small>" if x["status"]["buy"] == "暫定" else ""
        rows.append(
            f"<tr data-code={x['code']} data-cat={x['category']} data-growth={g} data-buy={b} data-sell={s} data-warn={1 if x['warnings'] else 0}>"
            f"<td class=k></td><td class=c>{x['code']}</td><td class=n>{html.escape(x['name'])}{warn}</td><td class=t>{x['category_ja']}</td>"
            f"<td class='r p' data-l=株価>{num(x['price'])}</td>"
            f"<td class='r s growth' data-l=成長期待>{score_cell(x['growth'], x['status']['growth'])}</td>"
            f"<td class='r s buy' data-l=買い（割安度）>{score_cell(x['buy'], x['status']['buy'])}{prov}</td>"
            f"<td class='r s sell' data-l=売り>{score_cell(x['sell'], x['status']['sell'])}</td>"
            f"<td class='r v' data-l=乖離率>{gap}</td>"
            f"<td class='r v' data-l=理論価値>{num(x['theoretical_value'])}</td><td class='r v' data-l=信頼度>{x['confidence']}</td></tr>")
    data = json.dumps(res, ensure_ascii=False).replace("</", "<\\/")
    cats = "".join(f"<button data-c={k}>{v}</button>" for k, v in CATEGORY.items())
    sc = m["status_counts"]
    return f"""<!doctype html><html lang=ja><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content="noindex,nofollow">
<title>日本株 AI 評価ランキング</title>
<style>
body{{font-family:system-ui,sans-serif;margin:12px;color:#222}}h1{{font-size:18px;margin:0 0 4px}}
.note{{font-size:12px;color:#555;margin:4px 0 8px;line-height:1.6}}.warn{{background:#fff4e5;border:1px solid #f0c080;padding:8px;font-size:12px;border-radius:6px}}
details.def{{font-size:12px;color:#444;margin:6px 0}}details.def p{{margin:4px 0}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;font-size:13px}}
th,td{{border-bottom:1px solid #ddd;padding:5px 6px;white-space:nowrap}}th{{background:#f3f3f3;position:sticky;top:0}}tbody tr{{cursor:pointer}}tbody tr:hover{{background:#f6f9ff}}
.r{{text-align:right}}.k{{text-align:right;color:#888}}.bar{{margin:6px 0;display:flex;flex-wrap:wrap;gap:4px;align-items:center}}
.bar button{{border:1px solid #bbb;background:#fff;border-radius:14px;padding:5px 11px;font-size:13px}}.bar button.on{{background:#1f5fbf;color:#fff;border-color:#1f5fbf}}
td.n{{white-space:normal;min-width:10em;max-width:18em}}.w{{display:inline-block;color:#b00020;font-size:11px;font-weight:bold;margin-left:4px;white-space:nowrap}}
.pv{{color:#a06000;font-size:10px;margin-left:2px}}#cnt{{font-size:12px;color:#333}}
.k-buy td.buy,.k-growth td.growth,.k-sell td.sell{{font-weight:bold;color:#1f5fbf}}
dialog{{max-width:760px;width:94vw;border:0;border-radius:10px;padding:0;box-shadow:0 4px 24px rgba(0,0,0,.3)}}
dialog .hd{{position:sticky;top:0;background:#1f5fbf;color:#fff;padding:10px 14px;display:flex;justify-content:space-between;align-items:center}}
dialog .bd{{padding:8px 14px 16px;font-size:13px;line-height:1.6}}dialog h3{{font-size:14px;margin:12px 0 4px;border-left:4px solid #1f5fbf;padding-left:6px}}
dialog table{{font-size:12px;width:100%}}dialog td,dialog th{{white-space:normal}}dialog .x{{background:none;border:0;color:#fff;font-size:22px}}
.ng{{color:#b00020}}.ok{{color:#0a7d32}}.mut{{color:#777}}
@media (max-width:640px){{
body{{margin:0;background:#f4f5f7}}h1,.note,.warn,details.def{{margin-left:10px;margin-right:10px}}h1{{padding-top:10px}}
.ctl{{position:sticky;top:0;background:#f4f5f7;padding:4px 8px;z-index:2;box-shadow:0 2px 4px rgba(0,0,0,.08)}}
.bar{{flex-wrap:nowrap;overflow-x:auto}}.bar button{{flex:none;padding:7px 13px;font-size:14px}}
#list,#list tbody{{display:block}}#list thead{{display:none}}
#list tr{{display:flex;flex-wrap:wrap;background:#fff;margin:8px;border-radius:10px;padding:8px 10px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
#list td{{border:0;padding:2px 0;white-space:normal}}#list td.k{{font-size:15px;font-weight:bold;color:#1f5fbf;width:2em;text-align:left}}
#list td.c{{font-size:13px;color:#666;width:3.3em}}#list td.n{{flex:1 0 calc(100% - 9.5em);min-width:0;font-size:16px;font-weight:bold;max-width:none}}
#list td.t{{font-size:11px;color:#666;border:1px solid #ccc;border-radius:8px;padding:1px 6px;align-self:center}}
#list td.s{{width:33.3%}}#list td.p,#list td.v{{width:25%;order:2;font-size:14px}}#list td.s{{order:1;font-size:17px}}#list td.p,#list td.v,#list td.s{{text-align:left;padding-top:6px}}
#list td[data-l]::before{{content:attr(data-l);display:block;font-size:11px;color:#888;font-weight:normal}}
dialog{{width:100vw;max-width:100vw;height:100vh;max-height:100vh;border-radius:0;margin:0}}
}}
</style></head><body>
<h1>日本株 AI 評価ランキング</h1>
<div class=note>評価日 <b>{m['run_date']}</b>（{m['price_basis']}）／ 計算方式 <b>{' / '.join(m['v4_version'])}</b>（正式）・比較用 {' / '.join(m['v5_version'])}（参考）<br>
データ取得 株価 {m['fetched_at']['quotes'][:16].replace('T', ' ')}・決算 {m['fetched_at']['financials'][:16].replace('T', ' ')} ／ 採点 {m['scored_at'][:16].replace('T', ' ')} ／ 作成 {m['generated_at']} ／ {m['update']}<br>
全 {m['total']} 銘柄のうち算出済み: 成長 {m['counts']['growth']}・買い {m['counts']['buy']}（うち暫定 {sc['buy'].get('暫定', 0)}）・売り {m['counts']['sell']} ／ 照合番号 {fp}</div>
<div class=warn>投資の勧誘ではありません。AI による試験的な計算結果で、誤りを含むことがあります。売買はご自身の判断と責任で行ってください。</div>
<details class=def><summary>点数の意味（必ずお読みください）</summary>{''.join(f'<p>{html.escape(v)}</p>' for v in m['definitions'].values())}
<p>「未算定」等の表示は 0 点ではありません。対象外＝計算の対象でない（ETF・赤字成長企業など）／データ不足＝必要な数字がそろわない／取得失敗＝データ取得に失敗。<br>行を押すと、点数の内訳・理論価値の計算式・注意の根拠・出典を表示します。</p></details>
<div class=ctl><div class=bar id=sort><button data-k=buy class=on>買いの順</button><button data-k=growth>成長期待の順</button><button data-k=sell>売りの順</button></div>
<div class=bar id=cat><button data-c=all class=on>すべて</button>{cats}</div>
<div class=bar><label><input type=checkbox id=nowarn> ⚠注意ありを除く</label><span id=cnt></span></div></div>
<div class=wrap><table id=list><thead><tr><th>順位</th><th>コード</th><th>銘柄</th><th>分類</th><th>株価</th><th>成長期待</th><th>買い（割安度）</th><th>売り</th><th>乖離率</th><th>理論価値</th><th>信頼度</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table></div>
<dialog id=dlg><div class=hd><b id=dt></b><button class=x onclick="dlg.close()" aria-label=閉じる>×</button></div><div class=bd id=db></div></dialog>
<script id=data type=application/json>{data}</script>
<script>
const RES=JSON.parse(document.getElementById('data').textContent),BY=Object.fromEntries(RES.items.map(x=>[x.code,x]));
let key='buy',cat='all';const tb=document.querySelector('#list tbody'),nw=document.getElementById('nowarn');
const e=s=>String(s??'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]);
const n=(v,d=0)=>v==null?'-':Number(v).toLocaleString('ja-JP',{{minimumFractionDigits:d,maximumFractionDigits:d}});
function draw(){{document.body.className='k-'+key;const rs=[...tb.rows].sort((a,b)=>b.dataset[key]-a.dataset[key]||b.dataset.growth-a.dataset.growth);
let k=0,shown=0,c={{growth:0,buy:0,sell:0}};for(const r of rs){{tb.appendChild(r);const v=(cat==='all'||r.dataset.cat===cat)&&!(nw.checked&&r.dataset.warn==='1');r.hidden=!v;
if(v){{shown++;for(const x in c)if(r.dataset[x]>=0)c[x]++;}}r.cells[0].textContent=v&&r.dataset[key]>=0?++k:'-';}}
document.getElementById('cnt').textContent=`表示 ${{shown}} / 全 ${{RES.items.length}} 銘柄（算出済み 成長 ${{c.growth}}・買い ${{c.buy}}・売り ${{c.sell}}）`;}}
function pick(id,attr,set){{document.getElementById(id).onclick=ev=>{{const b=ev.target.closest('button');if(!b)return;
for(const x of ev.currentTarget.children)x.classList.toggle('on',x===b);set(b.dataset[attr]);draw();}};}}
pick('sort','k',v=>key=v);pick('cat','c',v=>cat=v);nw.onchange=draw;
const fl=fs=>'<table><tr><th>項目</th><th>点数</th><th>重み</th></tr>'+fs.map(f=>`<tr><td>${{e(f.label)}}</td><td>${{f.score==null?'<span class=mut>未算定（除いて計算）</span>':n(f.score)}}</td><td>${{f.weight}}</td></tr>`).join('')+'</table>';
function show(code){{const x=BY[code],a=x.assumption,v=x.v5,p=x.previous;let h='';
h+=`<h3>評価の状態</h3>成長期待 <b>${{x.growth??'-'}}</b>（${{e(x.status.growth)}}）／ 買い <b>${{x.buy??'-'}}</b>（${{e(x.status.buy)}}）／ 売り <b>${{x.sell??'-'}}</b>（${{e(x.status.sell)}}）<br>信頼度 ${{x.confidence}} ／ 株価 ${{n(x.price)}}円（${{e(x.price_date)}}）／ 分類 ${{e(x.category_ja)}}`;
if(x.unrated_reason)h+=`<br><b>買い未評価の理由:</b> ${{e(x.unrated_reason)}}`;
h+=`<h3>購入候補の判定</h3>`+(x.candidate.eligible?'<span class=ok>点数条件・前提の承認・注意なしをすべて満たす</span>':`<span class=ng>候補外:</span> ${{x.candidate.why_not.map(e).join('、')}}`)+`<br><span class=mut>条件: 成長${{RES.meta.rule.growth}}以上・買い${{RES.meta.rule.buy}}以上・売り${{RES.meta.rule.sell_max}}以下・前提が承認済み・重大な注意なし</span>`;
if(x.warnings.length)h+='<h3>重大な注意（v5 の判定）</h3>'+x.warnings.map(w=>`⚠ <b>${{e(w.type)}}</b>: ${{e(w.detail)}}${{w.note?`<br><span class=mut>${{e(w.note)}}</span>`:''}}`).join('<br>');
h+='<h3>理論価値の計算（v4 正式）</h3>'+(x.tv_formula?`${{e(x.tv_formula)}}<br>乖離率 ${{x.gap_pct>0?'+':''}}${{x.gap_pct}}% ／ 狙い目 ${{n(x.target_price)}}円 = 理論価値×(1−安全余裕 ${{Math.round(a.safety_margin*100)}}%)（${{e(x.target_note)}}）<br>前提: ${{e(a.origin)}}・${{e(a.status)}}<br><span class=mut>${{e(a.reason)}}</span>`:'計算していません');
h+='<h3>成長期待の内訳</h3>'+fl(x.growth_factors)+`<h3>売りの内訳</h3>`+(x.sell_floor?`<b>${{e(x.sell_floor)}}</b>`:'')+fl(x.sell_factors);
if(v)h+=`<h3>v5（比較用の参考・正式採用前）</h3>成長 ${{v.growth??'-'}}${{v.growth_is_reference?'（参考）':''}} ／ 買い ${{v.buy??'-'}} ／ 売り ${{v.sell??'-'}}${{v.sell_is_reference?'（参考）':''}} ／ 理論価値 ${{n(v.theoretical_value)}}円 ／ 信頼度 ${{e(v.confidence)}}<br>想定PER: 過去5年の実績PER中央値 ${{v.per_median??'-'}}倍（${{v.per_applied?'適用':'未適用'}}）${{v.per_notes.length?'<br><span class=mut>'+v.per_notes.map(e).join('／')+'</span>':''}}`;
if(p&&p.reasons.length)h+=`<h3>前回（${{e(p.version)}}）からの変化</h3>`+Object.entries(p.changes).map(([k,c])=>`${{({{growth:'成長',buy:'買い',sell:'売り',tv:'理論価値'}})[k]}} ${{n(c[0])}}→${{n(c[1])}}`).join('／')+`<br>理由の区分: ${{p.reasons.map(e).join('、')}}`;
if(x.data_issue)h+=`<h3>データの注意</h3>${{e(x.data_issue)}}`;
h+=`<h3>出典・データ</h3>${{x.sources.map(e).join('<br>')||'なし'}}<br><span class=mut>計算方式 ${{e(x.v4_version)}}</span>`;
document.getElementById('dt').textContent=x.code+' '+x.name;document.getElementById('db').innerHTML=h;dlg.showModal();}}
tb.onclick=ev=>{{const r=ev.target.closest('tr');if(r)show(r.dataset.code);}};draw();
const hc=decodeURIComponent(location.hash.slice(1));if(BY[hc])show(hc);  // #コード で詳細を直接開く
</script></body></html>
"""


def render_md(res, fp):
    m, items = res["meta"], res["items"]
    sc = m["status_counts"]
    L = ["# 日本株 AI 評価ランキング（定期レポート）", "",
         f"- 評価日: {m['run_date']}（{m['price_basis']}）／ 計算方式: {' / '.join(m['v4_version'])}（正式）・{' / '.join(m['v5_version'])}（参考）",
         f"- データ取得: 株価 {m['fetched_at']['quotes']}・決算 {m['fetched_at']['financials']} ／ 採点 {m['scored_at']} ／ 作成 {m['generated_at']}",
         f"- 照合番号: {fp}（Web 画面と同じ集計結果から作成）",
         f"- 全 {m['total']} 銘柄。算出済み: 成長 {m['counts']['growth']}・買い {m['counts']['buy']}・売り {m['counts']['sell']}",
         f"- 状態の内訳: 成長 {sc['growth']} ／ 買い {sc['buy']} ／ 売り {sc['sell']}", ""]
    L += ["## 点数の意味", ""] + [f"- {v}" for v in m["definitions"].values()] + [""]
    cands = [x for x in items if x["candidate"]["eligible"]]
    L += ["## 購入候補（点数条件・前提の承認・重大な注意なしをすべて満たす）", "",
          ("、".join(f"{x['name']}（{x['code']}）" for x in cands) or "なし"), ""]
    hi = [x for x in items if x["buy"] is not None and x["buy"] >= m["rule"]["buy"]]
    L += [f"## 買い{m['rule']['buy']}点以上（割安度のみ。候補とは別）", ""]
    L += [f"- {x['name']}（{x['code']}）買い{x['buy']}・成長{x['growth']}・売り{x['sell']}・信頼度{x['confidence']}"
          f"／候補外の理由: {'、'.join(x['candidate']['why_not']) or 'なし'}" for x in hi] or ["- なし"]
    L += ["", "## 一覧（買いの順）", "",
          "| 順 | コード | 銘柄 | 分類 | 株価 | 成長 | 買い | 売り | 乖離率 | 理論価値 | 信頼度 | 状態（成長/買い/売り） | 注意 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(items, 1):
        st = x["status"]
        L.append(f"| {i if x['buy'] is not None else '-'} | {x['code']} | {x['name']} | {x['category_ja']} | {num(x['price'])} | "
                 f"{num(x['growth']) if x['growth'] is not None else st['growth'].split('（')[0]} | "
                 f"{num(x['buy']) if x['buy'] is not None else st['buy']} | {num(x['sell']) if x['sell'] is not None else st['sell'].split('（')[0]} | "
                 f"{'-' if x['gap_pct'] is None else format(x['gap_pct'], '+.0f') + '%'} | {num(x['theoretical_value'])} | {x['confidence']} | "
                 f"{st['growth'].split('（')[0]}/{st['buy']}/{st['sell'].split('（')[0]} | {'・'.join(w['type'] for w in x['warnings']) or '-'} |")
    L += ["", "## 未評価・データ不足の理由", ""]
    L += [f"- {x['code']} {x['name']}: 買い={x['status']['buy']}（{x['unrated_reason']}）"
          + (f" ／ 売り={x['status']['sell']}" if x["sell"] is None else "") for x in items if x["buy"] is None]
    return "\n".join(L) + "\n"


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    res = build(conn)
    conn.close()
    fp = hashlib.sha256(json.dumps(res["items"], ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:12]
    res["meta"]["fingerprint"] = fp
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ranking.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (OUT / "index.html").write_text(render_html(res, fp), encoding="utf-8")
    (OUT / "report.md").write_text(render_md(res, fp), encoding="utf-8")
    # ChatGPT が利用者の依頼で読む場合（ChatGPT-User）だけ許可。検索・学習用の巡回は引き続き拒否
    (OUT / "robots.txt").write_text("User-agent: ChatGPT-User\nAllow: /\n\nUser-agent: *\nDisallow: /\n", encoding="utf-8")
    print(f"評価日 {res['meta']['run_date']} / {len(res['items'])} 銘柄 / 照合番号 {fp} → {OUT}")


if __name__ == "__main__":
    main()
