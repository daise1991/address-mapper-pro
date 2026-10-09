import { useState } from "react";
import { Meta, send } from "../api";
import { Decision, ErrorBox, fmt, pct, Score, useApi } from "../ui";

type Holding = {
  id: number; code: string; name: string | null; quantity: number; avg_cost: number; account: string;
  price: number | null; market_value: number | null; unrealized: number | null; est_tax_if_sold: number;
  weight: number | null; sell_score: number | null; sell_decision: string | null; exit_type: string | null;
};
type Resp = { as_of: string | null; holdings: Holding[]; cash: number; market_value: number; total_assets: number };
const ACCOUNTS: Record<string, string> = { specific: "特定", general: "一般", nisa_growth: "NISA成長投資枠" };

export default function Portfolio({ meta, onOpen }: { meta: Meta; onOpen: (c: string) => void }) {
  const { data, error, reload } = useApi<Resp>("/portfolio");
  const [h, setH] = useState({ code: "", quantity: "", avg_cost: "", account: "specific" });
  const [cash, setCash] = useState("");
  const [msg, setMsg] = useState("");

  const run = async (f: () => Promise<unknown>, ok: string) => {
    try {
      await f();
      setMsg(ok);
      reload();
    } catch (e) {
      setMsg(`失敗: ${(e as Error).message}`);
    }
  };

  return (
    <>
      <ErrorBox error={error} />
      <div className="panel">
        <h2>資産サマリー {data?.as_of && <span className="muted">（{data.as_of} 終値ベース）</span>}</h2>
        <div className="row">
          <span>評価額 <b>{fmt(data?.market_value)}円</b></span>
          <span>現金（買付余力） <b>{fmt(data?.cash)}円</b></span>
          <span>総資産 <b>{fmt(data?.total_assets)}円</b></span>
        </div>
        <div className="row">
          <input type="number" placeholder="現在の買付余力（円）" value={cash} onChange={(e) => setCash(e.target.value)} />
          <button onClick={() => run(() => send("/cash", "POST", { amount: Number(cash) }), "買付余力を更新しました")}>余力を更新</button>
        </div>
      </div>
      <div className="panel">
        <h2>保有株</h2>
        <div className="table-wrap"><table>
          <thead><tr><th className="l">銘柄</th><th className="l">口座</th><th>株数</th><th>取得単価</th><th>現在値</th><th>評価額</th><th>比率</th><th>含み損益</th><th>売却時税額</th><th>売り</th><th className="l">判定</th><th></th></tr></thead>
          <tbody>
            {data?.holdings.map((x) => (
              <tr key={x.id}>
                <td className="l click" onClick={() => onOpen(x.code)}><b>{x.code}</b> {x.name}</td>
                <td className="l">{ACCOUNTS[x.account] ?? x.account}</td>
                <td>{fmt(x.quantity)}</td><td>{fmt(x.avg_cost, 1)}</td><td>{fmt(x.price, 1)}</td>
                <td>{fmt(x.market_value)}</td><td>{pct(x.weight)}</td>
                <td style={{ color: (x.unrealized ?? 0) >= 0 ? "var(--good)" : "var(--bad)" }}>{fmt(x.unrealized)}</td>
                <td>{fmt(x.est_tax_if_sold)}</td>
                <td><Score v={x.sell_score} kind="sell" /></td>
                <td className="l"><Decision type="sell" value={x.sell_decision} labels={meta.sell_labels} /> {x.exit_type && <Decision type="exit" value={x.exit_type} labels={meta.exit_labels} />}</td>
                <td><button onClick={() => confirm(`${x.code} の保有記録を削除しますか？`) && run(() => send(`/holdings/${x.id}`, "DELETE"), "削除しました")}>削除</button></td>
              </tr>
            ))}
          </tbody>
        </table></div>
        <div className="row" style={{ marginTop: 8 }}>
          <input placeholder="コード" style={{ width: 80 }} value={h.code} onChange={(e) => setH({ ...h, code: e.target.value.toUpperCase() })} />
          <input type="number" placeholder="株数" style={{ width: 90 }} value={h.quantity} onChange={(e) => setH({ ...h, quantity: e.target.value })} />
          <input type="number" placeholder="取得単価" style={{ width: 110 }} value={h.avg_cost} onChange={(e) => setH({ ...h, avg_cost: e.target.value })} />
          <select value={h.account} onChange={(e) => setH({ ...h, account: e.target.value })}>
            {Object.entries(ACCOUNTS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <button className="primary" onClick={() => run(() => send("/holdings", "POST", { code: h.code, quantity: Number(h.quantity), avg_cost: Number(h.avg_cost), account: h.account }), "追加しました")}>追加</button>
          <span>{msg}</span>
        </div>
        <p className="muted">取得単価・含み損益は損益と税金（特定口座 20.315%）の試算にのみ使い、企業の評価点・売買判定には使いません。</p>
      </div>
    </>
  );
}
