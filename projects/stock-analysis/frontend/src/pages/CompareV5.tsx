import { ErrorBox, fmt, pct, useApi } from "../ui";

type Item = {
  code: string; name: string; eval_status: string | null; confidence: string | null; confidence_points: number | null;
  confidence_forced_d: string[] | null; insufficient_history: boolean | null;
  growth: { v4: number | null; v5_formal: number | null; v5_reference: number | null; coverage: number | null };
  buy: { v4: number | null; v5: number | null; v5_reference: number | null; v5_no_dividend: number | null; dividend_pv: number | null; status: string | null; bear_downside: number | null };
  sell: { v4: number | null; v5_formal: number | null; v5_reference: number | null; coverage: number | null };
  severe_risks: { type: string; detail: string }[];
  decisions: { v4_buy: string | null; v5_buy: string | null; v4_sell: string | null; v5_sell: string | null; v5_reevaluate: boolean };
};
type Resp = {
  date: string | null; message?: string; items: Item[]; eval_status?: Record<string, number>; confidence?: Record<string, number>;
  growth_status?: { formal: number; reference_only: number };
  candidates?: { v4_buy: string[]; v5_buy: string[]; v4_sell: string[]; v5_sell: string[] };
  kawasaki_split_check?: Record<string, number | null> | null;
};
const STATUS: Record<string, string> = { formal: "正式評価", provisional: "暫定評価", unrated: "未算定" };

const both = (formal: number | null, ref: number | null) =>
  formal !== null ? <b>{formal}</b> : ref !== null ? <span className="muted" title="未評価項目を含む参考点数（正式ランキング・注文候補には使わない）">参考 {ref}</span> : <span className="muted">—</span>;

export default function CompareV5() {
  const { data, error } = useApi<Resp>("/v5/compare");
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;
  if (!data.date) return <div className="panel"><p>{data.message}</p></div>;
  return (
    <>
      <div className="panel">
        <h2>v4（正式）と v5（比較検証用）の比較 <span className="muted">{data.date}・同一データ</span></h2>
        <div className="banner warn">v5 は承認前の比較検証用です。正式なランキング・注文案は v4 のままです。</div>
        <p>評価状態: {Object.entries(data.eval_status ?? {}).map(([k, v]) => `${STATUS[k] ?? k} ${v}`).join("・")} ／
          予測信頼度: {Object.entries(data.confidence ?? {}).sort().map(([k, v]) => `${k} ${v}`).join("・")} ／
          成長期待点 正式 {data.growth_status?.formal}・参考のみ {data.growth_status?.reference_only}</p>
        <p>購入候補 v4 [{data.candidates?.v4_buy.join(", ")}] → v5 [{data.candidates?.v5_buy.join(", ")}] ／
          売却候補 v4 [{data.candidates?.v4_sell.join(", ")}] → v5 [{data.candidates?.v5_sell.join(", ")}]</p>
        <p className="muted">太字＝その点数単体が正式（充足率80%以上・必須項目あり）、「参考」＝未評価項目を含む参考点数。総合の正式評価（信頼度A/B・前提の承認を含む）は「状態」列。未算定は0点とは別です。</p>
      </div>
      <div className="panel"><div className="table-wrap"><table>
        <thead><tr><th className="l">銘柄</th><th className="l">状態</th><th>信頼度</th><th>成長 v4</th><th>成長 v5</th><th>充足率</th>
          <th>買い v4</th><th>買い v5</th><th>配当加算前</th><th>弱気時</th><th>売り v4</th><th>売り v5</th><th className="l">重大リスク</th></tr></thead>
        <tbody>
          {data.items.map((i) => (
            <tr key={i.code}>
              <td className="l"><b>{i.code}</b> {i.name}</td>
              <td className="l">{STATUS[i.eval_status ?? ""] ?? "—"}</td>
              <td title={(i.confidence_forced_d ?? []).join(" / ") + (i.insufficient_history ? " ／ 予測誤差: 検証期間不足" : "")}>
                <span className={`badge ${i.confidence === "A" || i.confidence === "B" ? "good" : i.confidence === "C" ? "warn" : "bad"}`}>{i.confidence ?? "—"}</span> {fmt(i.confidence_points, 0)}
              </td>
              <td>{i.growth.v4 ?? "—"}</td><td>{both(i.growth.v5_formal, i.growth.v5_reference)}</td><td>{pct(i.growth.coverage, 0)}</td>
              <td>{i.buy.v4 ?? "—"}</td>
              <td>{i.buy.v5 !== null ? <b>{i.buy.v5}</b> : i.buy.v5_reference !== null ? <span className="muted">参考 {i.buy.v5_reference}</span> : <span className="muted">未算定</span>}</td>
              <td>{i.buy.v5_no_dividend ?? "—"}</td><td>{pct(i.buy.bear_downside, 0)}</td>
              <td>{i.sell.v4 ?? "—"}</td><td>{both(i.sell.v5_formal, i.sell.v5_reference)}</td>
              <td className="l wrap" style={{ textAlign: "left" }}>{i.severe_risks.map((r) => `${r.type}: ${r.detail}`).join(" ／ ") || "—"}{i.decisions.v5_reevaluate && <span className="badge bad"> 再評価対象</span>}</td>
            </tr>
          ))}
        </tbody>
      </table></div></div>
    </>
  );
}
