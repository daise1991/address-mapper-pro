"""公開用の読み取り専用ランキング（index.html 1枚）を作る。
本番DBを読み取り専用で開き、最新の評価日のランキングだけを書き出す。
保有・資金・売買判断・設定は出さない。出力先は引数（既定 C:/Augma/ai-work/stock-public）。"""
import html
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "backend" / "data" / "stock.db"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "C:/Augma/ai-work/stock-public")
CATEGORY = {"growth": "成長", "emerging_loss": "赤字成長", "cyclical": "景気連動", "stable": "安定"}

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
d = conn.execute("SELECT MAX(run_date) FROM scores").fetchone()[0]
items = conn.execute(
    """SELECT s.code, sec.name, sec.category, s.price, s.growth_score, s.buy_score, s.buy_status,
              s.sell_score, s.theoretical_value, s.target_price
       FROM scores s JOIN securities sec ON sec.code=s.code
       WHERE s.run_date=? AND sec.active=1""", (d,)).fetchall()
conn.close()
# 買い点数（未評価は後ろ）→ 成長期待の順
items = sorted(items, key=lambda r: (r["buy_status"] != "rated", -(r["buy_score"] or 0), -(r["growth_score"] or 0)))


def num(v, digits=0):
    return "-" if v is None else f"{v:,.{digits}f}"


rows = []
for r in items:
    rated = r["buy_status"] == "rated"
    gap = f"{(r['theoretical_value'] / r['price'] - 1) * 100:+.0f}%" if rated and r["theoretical_value"] and r["price"] else "-"
    g, b, sl = r["growth_score"], (r["buy_score"] if rated else None), r["sell_score"]
    rows.append(
        f"<tr data-cat={r['category']} data-growth={-1 if g is None else g} data-buy={-1 if b is None else b} "
        f"data-sell={-1 if sl is None else sl}><td class=k></td><td class=c>{html.escape(r['code'])}</td>"
        f"<td class=n>{html.escape(r['name'] or '')}</td><td class=t>{CATEGORY.get(r['category'], '')}</td>"
        f"<td class='r p' data-l=株価>{num(r['price'])}</td>"
        f"<td class='r s growth' data-l=成長期待>{num(r['growth_score'])}</td>"
        f"<td class='r s buy' data-l=買い>{num(r['buy_score']) if rated else '未評価'}</td>"
        f"<td class='r s sell' data-l=売り>{num(r['sell_score'])}</td>"
        f"<td class='r v' data-l=乖離率>{gap}</td>"
        f"<td class='r v' data-l=理論価値>{num(r['theoretical_value']) if rated else '-'}</td>"
        f"<td class='r v' data-l=狙い目>{num(r['target_price']) if rated else '-'}</td></tr>")

page = f"""<!doctype html><html lang=ja><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content="noindex,nofollow">
<title>日本株 AI 評価ランキング</title>
<style>
body{{font-family:system-ui,sans-serif;margin:12px;color:#222}}h1{{font-size:18px;margin:0 0 4px}}
.note{{font-size:12px;color:#555;margin:4px 0 10px}}.warn{{background:#fff4e5;border:1px solid #f0c080;padding:8px;font-size:12px;border-radius:6px}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;font-size:13px;white-space:nowrap}}
th,td{{border-bottom:1px solid #ddd;padding:5px 6px}}th{{background:#f3f3f3;position:sticky;top:0}}
.r{{text-align:right}}.k{{text-align:right;color:#888}}.bar{{margin:6px 0;display:flex;flex-wrap:wrap;gap:4px}}
.bar button{{border:1px solid #bbb;background:#fff;border-radius:14px;padding:5px 11px;font-size:13px}}.bar button.on{{background:#1f5fbf;color:#fff;border-color:#1f5fbf}}.n{{max-width:11em;overflow:hidden;text-overflow:ellipsis}}
.k-buy td.buy,.k-growth td.growth,.k-sell td.sell{{font-weight:bold;color:#1f5fbf}}
@media (max-width:640px){{
body{{margin:0;background:#f4f5f7}}h1,.note,.warn{{margin-left:10px;margin-right:10px}}h1{{padding-top:10px}}
.ctl{{position:sticky;top:0;background:#f4f5f7;padding:4px 8px;z-index:2;box-shadow:0 2px 4px rgba(0,0,0,.08)}}
.bar{{flex-wrap:nowrap;overflow-x:auto}}.bar button{{flex:none;padding:7px 13px;font-size:14px}}
table,tbody{{display:block}}thead{{display:none}}table{{white-space:normal}}
tr{{display:flex;flex-wrap:wrap;background:#fff;margin:8px;border-radius:10px;padding:8px 10px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
td{{border:0;padding:2px 0}}td.k{{font-size:15px;font-weight:bold;color:#1f5fbf;width:2em;text-align:left}}
td.c{{font-size:13px;color:#666;width:3.3em}}td.n{{flex:1 0 calc(100% - 9.5em);min-width:0;font-size:16px;font-weight:bold;max-width:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
td.t{{font-size:11px;color:#666;border:1px solid #ccc;border-radius:8px;padding:1px 6px;align-self:center}}
td.p,td.v,td.s{{width:25%;text-align:left;font-size:15px}}td.p,td.v{{order:2}}td.s{{order:1;font-size:17px}}td.s.sell{{width:50%}}
td.s,td.p,td.v{{padding-top:6px}}td[data-l]::before{{content:attr(data-l);display:block;font-size:11px;color:#888;font-weight:normal}}
}}
</style></head><body>
<h1>日本株 AI 評価ランキング</h1>
<div class=note>評価日 {d}（平日の夕方に自動更新）／ {len(items)} 銘柄 ／ 作成 {datetime.now():%Y-%m-%d %H:%M}</div>
<div class=warn>投資の勧誘ではありません。AI による試験的な計算結果で、誤りを含むことがあります。売買はご自身の判断と責任で行ってください。</div>
<div class=note>成長期待・買い・売り は 0〜100 点（高いほどその傾向が強い）。理論価値は将来の価値から計算した今の妥当な株価、狙い目は買いを考える目安の株価（円）。乖離率は理論価値が株価より何%高いか（プラスほど割安、マイナスほど割高）。</div>
<div class=ctl><div class=bar id=sort><button data-k=buy class=on>買いの順</button><button data-k=growth>成長期待の順</button><button data-k=sell>売りの順</button></div>
<div class=bar id=cat><button data-c=all class=on>すべて</button>{"".join(f'<button data-c={k}>{v}</button>' for k, v in CATEGORY.items())}</div></div>
<div class=wrap><table><thead><tr><th>順位</th><th>コード</th><th>銘柄</th><th>分類</th><th>株価</th><th>成長期待</th><th>買い</th><th>売り</th><th>乖離率</th><th>理論価値</th><th>狙い目</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table></div>
<script>
let key='buy',cat='all';const tb=document.querySelector('tbody');
function draw(){{document.body.className='k-'+key;const rs=[...tb.rows].sort((a,b)=>b.dataset[key]-a.dataset[key]||b.dataset.growth-a.dataset.growth);
let n=0;for(const r of rs){{tb.appendChild(r);const v=cat==='all'||r.dataset.cat===cat;r.hidden=!v;
r.cells[0].textContent=v&&r.dataset[key]>=0?++n:'-';}}}}
function pick(id,attr,set){{document.getElementById(id).onclick=e=>{{const b=e.target.closest('button');if(!b)return;
for(const x of e.currentTarget.children)x.classList.toggle('on',x===b);set(b.dataset[attr]);draw();}};}}
pick('sort','k',v=>key=v);pick('cat','c',v=>cat=v);draw();
</script></body></html>
"""
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "index.html").write_text(page, encoding="utf-8")
(OUT / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
print(f"評価日 {d} / {len(items)} 銘柄 → {OUT / 'index.html'}")
