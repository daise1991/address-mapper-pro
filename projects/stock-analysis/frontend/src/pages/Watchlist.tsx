import { useState } from "react";
import { Meta, send } from "../api";
import { ErrorBox, useApi } from "../ui";

type Sec = {
  code: string; name: string; group_name: string; category: string; asset_type: string; active: number;
  listing_status: string; verified_at: string | null; note: string | null;
};
const GROUPS: Record<string, string> = { existing: "既存", added: "追加", emerging: "新興・成長", user: "手動追加" };

export default function Watchlist({ meta, onOpen }: { meta: Meta; onOpen: (c: string) => void }) {
  const { data, error, reload } = useApi<Sec[]>("/securities");
  const [f, setF] = useState({ code: "", name: "", category: "growth", asset_type: "stock" });
  const [msg, setMsg] = useState("");
  const run = async (p: Promise<unknown>, ok: string) => {
    try {
      await p;
      setMsg(ok);
      reload();
    } catch (e) {
      setMsg(`失敗: ${(e as Error).message}`);
    }
  };
  const active = data?.filter((s) => s.active).length ?? 0;
  return (
    <div className="panel">
      <h2>監視銘柄の管理（監視中 {active}銘柄）</h2>
      <ErrorBox error={error} />
      <div className="row">
        <input placeholder="コード（例 7203, 186A）" style={{ width: 160 }} value={f.code} onChange={(e) => setF({ ...f, code: e.target.value.toUpperCase() })} />
        <input placeholder="銘柄名" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <select value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}>
          {Object.entries(meta.categories).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select value={f.asset_type} onChange={(e) => setF({ ...f, asset_type: e.target.value })}>
          <option value="stock">株式</option><option value="etf">ETF</option>
        </select>
        <button className="primary" onClick={() => run(send("/securities", "POST", f), `${f.code} を追加しました（翌回の取得から反映）`)}>追加</button>
        <span>{msg}</span>
      </div>
      <div className="table-wrap"><table>
        <thead><tr><th className="l">コード</th><th className="l">銘柄名</th><th className="l">区分</th><th className="l">評価分類</th><th className="l">上場状況</th><th className="l">備考</th><th></th></tr></thead>
        <tbody>
          {data?.map((s) => (
            <tr key={s.code} style={{ opacity: s.active ? 1 : 0.5 }}>
              <td className="l click" onClick={() => onOpen(s.code)}><b>{s.code}</b></td>
              <td className="l">{s.name}{s.asset_type === "etf" && <span className="badge"> ETF</span>}</td>
              <td className="l">{GROUPS[s.group_name] ?? s.group_name}</td>
              <td className="l">
                <select value={s.category} onChange={(e) => run(send(`/securities/${s.code}`, "PATCH", { category: e.target.value }), "分類を変更しました")}>
                  {Object.entries(meta.categories).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </select>
              </td>
              <td className="l">
                <span className={`badge ${s.listing_status === "listed" ? "good" : s.listing_status === "delisted" ? "bad" : ""}`}>
                  {s.listing_status === "listed" ? "上場確認済" : s.listing_status === "delisted" ? "一覧に無し" : "未検証"}
                </span>{" "}
                <span className="muted">{s.verified_at?.slice(0, 10)}</span>
              </td>
              <td className="l muted" style={{ whiteSpace: "normal" }}>{s.note}</td>
              <td>
                {s.active ? (
                  <button onClick={() => run(send(`/securities/${s.code}`, "PATCH", { active: false }), `${s.code} を監視から外しました`)}>監視解除</button>
                ) : (
                  <button onClick={() => run(send(`/securities/${s.code}`, "PATCH", { active: true }), `${s.code} を再開しました`)}>再開</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table></div>
      <p className="muted">監視解除しても過去の採点・検証データは残ります。上場状況は毎週月曜に J-Quants の上場銘柄一覧で自動検証します。</p>
    </div>
  );
}
