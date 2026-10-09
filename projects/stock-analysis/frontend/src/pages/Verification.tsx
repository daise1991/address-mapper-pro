import { useState } from "react";
import { Meta } from "../api";
import { ErrorBox, fmt, pct, useApi } from "../ui";

type Stat = { n: number; mean_return?: number; mean_excess?: number; hit_rate?: number; worst?: number };
type Resp = {
  horizon: number; live_only: boolean; n: number;
  ic: Record<string, number | null>;
  buy_buckets: Record<string, Stat>;
  rules: Record<string, Stat>;
  note: string;
};
const IC_LABELS: Record<string, string> = { buy: "買い点数", buy_v_over_p: "V/P（係数非依存）", sell: "売り点数", growth: "成長期待点" };

function StatTable({ title, rows }: { title: string; rows: Record<string, Stat> }) {
  return (
    <div className="panel">
      <h2>{title}</h2>
      <div className="table-wrap"><table>
        <thead><tr><th className="l">区分</th><th>件数</th><th>平均リターン</th><th>平均超過</th><th>的中率</th><th>最悪</th></tr></thead>
        <tbody>
          {Object.entries(rows).map(([k, s]) => (
            <tr key={k}><td className="l">{k}</td><td>{s.n}</td><td>{pct(s.mean_return)}</td><td>{pct(s.mean_excess)}</td><td>{pct(s.hit_rate, 0)}</td><td>{pct(s.worst)}</td></tr>
          ))}
        </tbody>
      </table></div>
    </div>
  );
}

export default function Verification({ meta }: { meta: Meta }) {
  const [horizon, setHorizon] = useState(60);
  const [live, setLive] = useState(false);
  const { data, error } = useApi<Resp>(`/verification?horizon=${horizon}&live_only=${live}`, [horizon, live]);
  return (
    <>
      <ErrorBox error={error} />
      <div className="panel">
        <h2>過去の採点と実際の株価</h2>
        <div className="row">
          <select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))} aria-label="期間">
            {meta.horizons.map((h) => <option key={h} value={h}>{h}営業日後</option>)}
          </select>
          <label><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> 当日保存分のみ（先読みなし）</label>
          <span className="muted">サンプル {data?.n ?? 0}件</span>
        </div>
        {data && (
          <table><tbody>
            {Object.entries(data.ic).map(([k, v]) => (
              <tr key={k}><td className="l">{IC_LABELS[k] ?? k} と将来リターンの順位相関（IC）</td><td>{fmt(v, 3)}</td></tr>
            ))}
          </tbody></table>
        )}
        <p className="muted">{data?.note} 売り系ルールの的中率は「同日ユニバース平均を下回った割合」です。</p>
      </div>
      {data && <StatTable title="売買ルールの精度比較（係数200 vs 150 を含む）" rows={data.rules} />}
      {data && <StatTable title="買い点数帯別の成績" rows={data.buy_buckets} />}
    </>
  );
}
