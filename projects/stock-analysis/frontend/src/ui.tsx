import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";

export function useApi<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [loading, setLoading] = useState(false);
  const reload = useCallback(() => {
    if (!path) return;
    setLoading(true);
    api<T>(path)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch(setError)
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(reload, [reload]);
  return { data, error, loading, reload };
}

export const fmt = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : v.toLocaleString("ja-JP", { maximumFractionDigits: digits, minimumFractionDigits: digits });

export const pct = (v: number | null | undefined, digits = 1) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`);

export function Score({ v, kind }: { v: number | null; kind: "growth" | "buy" | "sell" }) {
  if (v === null || v === undefined) return <span className="badge">未算定</span>;
  const cls =
    kind === "sell" ? (v >= 75 ? "bad" : v >= 65 ? "warn" : v <= 39 ? "good" : "") : v >= 85 ? "good" : v >= 75 ? "info" : v < 50 ? "" : "";
  return <span className={`score badge ${cls}`}>{v}</span>;
}

const BUY_CLS: Record<string, string> = { aggressive: "good", normal: "info", check: "warn", wait_discount: "", unrated: "", none: "" };
const SELL_CLS: Record<string, string> = { priority: "bad", candidate: "bad", partial: "warn", review: "", hold: "good", unrated: "" };

export function Decision({ type, value, labels }: { type: "buy" | "sell" | "exit"; value: string | null; labels: Record<string, string> }) {
  if (!value) return <span className="muted">—</span>;
  const cls = type === "buy" ? BUY_CLS[value] : type === "sell" ? SELL_CLS[value] : value === "stop_loss" ? "bad" : "warn";
  return <span className={`badge ${cls ?? ""}`}>{labels[value] ?? value}</span>;
}

export function Bar({ v }: { v: number | null }) {
  return (
    <span className="bar" title={v === null ? "データなし" : String(v)}>
      <span style={{ width: `${v ?? 0}%`, opacity: v === null ? 0 : 1 }} />
    </span>
  );
}

export function ErrorBox({ error }: { error: Error | null }) {
  if (!error) return null;
  return <div className="banner bad">エラー: {error.message}</div>;
}

export function PriceChart({ quotes }: { quotes: { date: string; close: number | null; adj_close: number | null }[] }) {
  const pts = quotes.map((q) => q.adj_close ?? q.close).filter((x): x is number => x !== null);
  if (pts.length < 2) return <p className="muted">株価データなし</p>;
  const ma = (w: number) => pts.map((_, i) => (i + 1 < w ? null : pts.slice(i + 1 - w, i + 1).reduce((a, b) => a + b, 0) / w));
  const min = Math.min(...pts);
  const max = Math.max(...pts);
  const W = 600;
  const H = 200;
  const x = (i: number) => (i / (pts.length - 1)) * W;
  const y = (v: number) => H - ((v - min) / (max - min || 1)) * (H - 10) - 5;
  const path = (arr: (number | null)[]) =>
    arr.map((v, i) => (v === null ? "" : `${arr[i - 1] == null ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)).join("");
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="株価チャート">
      <path d={path(pts)} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
      <path d={path(ma(25))} fill="none" stroke="var(--warn)" strokeWidth="1" />
      <path d={path(ma(75))} fill="none" stroke="var(--good)" strokeWidth="1" />
    </svg>
  );
}

type ScorePoint = { run_date: string; growth_score: number | null; buy_score: number | null; sell_score: number | null };

export function ScoreHistory({ scores }: { scores: ScorePoint[] }) {
  const pts = [...scores].sort((a, b) => a.run_date.localeCompare(b.run_date));
  if (pts.length < 2) return <p className="muted">点数の履歴がまだありません（毎営業日たまっていきます）</p>;
  const W = 600;
  const H = 160;
  const x = (i: number) => (i / (pts.length - 1)) * W;
  const y = (v: number) => H - (v / 100) * (H - 10) - 5;
  const line = (k: keyof Omit<ScorePoint, "run_date">) =>
    pts.map((p, i) => (p[k] === null ? "" : `${i === 0 || pts[i - 1][k] === null ? "M" : "L"}${x(i).toFixed(1)},${y(p[k] as number).toFixed(1)}`)).join("");
  return (
    <>
      <svg className="chart" style={{ height: 160 }} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="点数の推移">
        {[25, 50, 75].map((v) => <line key={v} x1="0" x2={W} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeWidth="1" />)}
        <path d={line("growth_score")} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
        <path d={line("buy_score")} fill="none" stroke="var(--good)" strokeWidth="1.5" />
        <path d={line("sell_score")} fill="none" stroke="var(--bad)" strokeWidth="1.5" />
      </svg>
      <p className="muted">{pts[0].run_date} 〜 {pts[pts.length - 1].run_date} ／ 青: 成長期待 ／ 緑: 買い ／ 赤: 売り（目盛り 25・50・75点）</p>
    </>
  );
}
