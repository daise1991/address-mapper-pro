import { useState } from "react";
import { getToken, Meta, setToken } from "./api";
import { ErrorBox, useApi } from "./ui";
import Rankings from "./pages/Rankings";
import Candidates from "./pages/Candidates";
import Detail from "./pages/Detail";
import Watchlist from "./pages/Watchlist";
import Portfolio from "./pages/Portfolio";
import Verification from "./pages/Verification";
import System from "./pages/System";

type Status = {
  data_mode: string;
  latest_score_date: string | null;
  latest_final_date: string | null;
  jobs_today: { stage: string; status: string; message: string }[];
  open_quality_issues: number;
  unavailable_fetches_24h: number;
};

const TABS = [
  ["rankings", "ランキング"],
  ["candidates", "売買候補"],
  ["portfolio", "保有・資金"],
  ["verification", "検証"],
  ["watchlist", "銘柄管理"],
  ["system", "通知・システム"],
] as const;
type Tab = (typeof TABS)[number][0];

export default function App() {
  const [tab, setTab] = useState<Tab>("rankings");
  const [code, setCode] = useState<string | null>(null);
  const status = useApi<Status>("/status");
  const meta = useApi<Meta>("/meta");
  const [tokenInput, setTokenInput] = useState(getToken());

  const needsToken = status.error && "status" in status.error && (status.error.status === 401 || status.error.status === 403);
  const delayed = status.data?.jobs_today.find((j) => j.status === "delayed" || j.status === "failed");

  return (
    <>
      <header>
        <h1>日本株AI投資分析</h1>
        <nav>
          {TABS.map(([k, label]) => (
            <button key={k} className={tab === k && !code ? "active" : ""} onClick={() => { setTab(k); setCode(null); }}>
              {label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {needsToken && (
          <div className="panel">
            <h2>アクセストークン</h2>
            <div className="row">
              <input type="password" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} placeholder="STOCK_APP_TOKEN" />
              <button className="primary" onClick={() => { setToken(tokenInput); location.reload(); }}>保存</button>
            </div>
          </div>
        )}
        {status.data?.data_mode === "demo" && (
          <div className="banner warn">デモデータ（合成値）を表示中です。実在企業の実際の株価・業績ではありません。</div>
        )}
        {delayed && <div className="banner bad">本日の処理: {delayed.stage} が {delayed.status}（{delayed.message}）。確定していない分析は表示していません。</div>}
        {!needsToken && <ErrorBox error={status.error} />}
        {meta.data && !needsToken && (
          code ? (
            <Detail code={code} meta={meta.data} onBack={() => setCode(null)} />
          ) : tab === "rankings" ? (
            <Rankings meta={meta.data} onOpen={setCode} />
          ) : tab === "candidates" ? (
            <Candidates meta={meta.data} onOpen={setCode} />
          ) : tab === "portfolio" ? (
            <Portfolio meta={meta.data} onOpen={setCode} />
          ) : tab === "verification" ? (
            <Verification meta={meta.data} />
          ) : tab === "watchlist" ? (
            <Watchlist meta={meta.data} onOpen={setCode} />
          ) : (
            <System />
          )
        )}
        <p className="muted">
          最新採点日: {status.data?.latest_score_date ?? "—"} ／ 確定日: {status.data?.latest_final_date ?? "—"} ／ 未解決のデータ品質課題:{" "}
          {status.data?.open_quality_issues ?? "—"} ／ 本アプリは投資判断の参考情報であり、売買は利用者の判断で行ってください。
        </p>
      </main>
    </>
  );
}
