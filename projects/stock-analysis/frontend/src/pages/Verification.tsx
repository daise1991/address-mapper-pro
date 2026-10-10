import { useState } from "react";
import { Meta } from "../api";
import { ErrorBox, fmt, pct, useApi } from "../ui";

type Stat = { n: number; mean_return?: number; mean_excess?: number; hit_rate?: number; worst?: number; mean_max_drawdown?: number; worst_max_drawdown?: number };
type Resp = {
  horizon: number; live_only: boolean; n: number; benchmark_label: string; versions: string[];
  ic: Record<string, number | null>;
  buy_buckets: Record<string, Stat>;
  rules: Record<string, Stat>;
  note: string;
  disclaimer?: string;
};
const HORIZON_LABELS: Record<number, string> = { 5: "1週間後", 20: "1か月後", 60: "3か月後", 120: "6か月後", 250: "1年後" };
const IC_LABELS: Record<string, string> = { buy: "買い点数", buy_v_over_p: "V/P（係数非依存）", sell: "売り点数", growth: "成長期待点" };

function StatTable({ title, rows }: { title: string; rows: Record<string, Stat> }) {
  return (
    <div className="panel">
      <h2>{title}</h2>
      <div className="table-wrap"><table>
        <thead><tr><th className="l">区分</th><th>件数</th><th>平均リターン</th><th>比較対象との差</th><th>的中率</th><th>最悪</th><th>平均最大下落</th><th>最大下落（最悪）</th></tr></thead>
        <tbody>
          {Object.entries(rows).map(([k, s]) => (
            <tr key={k}><td className="l">{k}</td><td>{s.n}</td><td>{pct(s.mean_return)}</td><td>{pct(s.mean_excess)}</td><td>{pct(s.hit_rate, 0)}</td><td>{pct(s.worst)}</td><td>{pct(s.mean_max_drawdown)}</td><td>{pct(s.worst_max_drawdown)}</td></tr>
          ))}
        </tbody>
      </table></div>
    </div>
  );
}

type Report = { basis: string; benchmark: string; items: { topic: string; finding: string; suggestion: string }[]; note: string };

function ImprovementReport({ bench }: { bench: string }) {
  const { data, error } = useApi<Report>(`/verification/report?benchmark=${bench}`, [bench]);
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;
  return (
    <div className="panel">
      <h2>売買ルールの改善案</h2>
      <p className="muted">集計の基準: {data.basis} ／ 比較対象: {data.benchmark}</p>
      <div className="table-wrap"><table>
        <thead><tr><th className="l">項目</th><th className="l">結果</th><th className="l">提案</th></tr></thead>
        <tbody>
          {data.items.map((i, n) => (
            <tr key={n}><td className="l">{i.topic}</td><td className="l wrap" style={{ textAlign: "left" }}>{i.finding}</td><td className="l wrap" style={{ textAlign: "left" }}>{i.suggestion}</td></tr>
          ))}
        </tbody>
      </table></div>
      <p className="muted">{data.note}</p>
    </div>
  );
}

export default function Verification({ meta }: { meta: Meta }) {
  const [horizon, setHorizon] = useState(60);
  const [live, setLive] = useState(false);
  const [bench, setBench] = useState("TOPIX");
  const [ver, setVer] = useState("");
  const q = `/verification?horizon=${horizon}&live_only=${live}&benchmark=${bench}${ver ? `&method_version=${encodeURIComponent(ver)}` : ""}`;
  const { data, error } = useApi<Resp>(q, [q]);
  return (
    <>
      <ErrorBox error={error} />
      <div className="panel">
        <h2>過去の採点と実際の株価</h2>
        {data?.disclaimer && <div className="banner warn">{data.disclaimer}</div>}
        <div className="row">
          <select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))} aria-label="期間">
            {meta.horizons.map((h) => <option key={h} value={h}>{HORIZON_LABELS[h] ?? `${h}営業日後`}</option>)}
          </select>
          <select value={bench} onChange={(e) => setBench(e.target.value)} aria-label="比較対象">
            {Object.entries(meta.benchmarks).map(([k, v]) => <option key={k} value={k}>比較: {v}</option>)}
          </select>
          <select value={ver} onChange={(e) => setVer(e.target.value)} aria-label="計算方式">
            <option value="">全ての計算方式</option>
            {data?.versions.map((v) => <option key={v} value={v}>{v}</option>)}
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
        <p className="muted">{data?.note} 比較対象: {data?.benchmark_label}。買い系の的中率は比較対象を上回った割合、売り系は下回った割合です。</p>
      </div>
      <ImprovementReport bench={bench} />
      {data && <StatTable title="売買ルールの精度比較（係数200 vs 150 を含む）" rows={data.rules} />}
      {data && <StatTable title="買い点数帯別の成績" rows={data.buy_buckets} />}
    </>
  );
}
