import { useState } from "react";
import { api, send } from "../api";
import { ErrorBox, useApi } from "../ui";

type Job = { id: number; run_date: string; stage: string; status: string; message: string | null; started_at: string; finished_at: string | null };
type Notice = { id: number; created_at: string; kind: string; title: string; body: string; delivered: number };
type Quality = {
  issues: { code: string; date: string; field: string; issue: string; detail: string }[];
  fetch_failures: { source: string; target: string; status: string; message: string; fetched_at: string }[];
};

function urlBase64ToUint8Array(base64: string) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function subscribePush(): Promise<string> {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    return "このブラウザは Web Push に対応していません。iPhone は Safari で「ホーム画面に追加」したアプリから操作してください（iOS 16.4 以降）。";
  }
  const { public_key } = await api<{ public_key: string | null }>("/push/key");
  if (!public_key) return "サーバーに VAPID 鍵が未設定です（README 参照）。";
  const perm = await Notification.requestPermission();
  if (perm !== "granted") return "通知が許可されませんでした。";
  const reg = await navigator.serviceWorker.register("/sw.js");
  const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(public_key) });
  await send("/push/subscribe", "POST", sub.toJSON());
  return "この端末でプッシュ通知を受け取れるようになりました。";
}

type WeightRow = { label: string; default: number; current: number };
type Weights = { growth: Record<string, WeightRow>; sell: Record<string, WeightRow>; customized: boolean; note: { note: string; updated_at: string } | null };

function WeightsEditor() {
  const { data, error, reload } = useApi<Weights>("/settings/weights");
  const [edit, setEdit] = useState<Record<string, Record<string, string>>>({});
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState("");
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;
  const val = (g: "growth" | "sell", k: string) => edit[g]?.[k] ?? String(data[g][k].current);
  const save = async () => {
    const pick = (g: "growth" | "sell") =>
      Object.fromEntries(Object.keys(data[g]).map((k) => [k, Number(val(g, k))]).filter(([k, v]) => v !== data[g][k as string].default));
    try {
      const r = await send<{ message: string }>("/settings/weights", "PUT", { growth: pick("growth"), sell: pick("sell"), note });
      setMsg(r.message);
      reload();
    } catch (e) {
      setMsg(`失敗: ${(e as Error).message}`);
    }
  };
  const table = (g: "growth" | "sell", title: string) => (
    <div>
      <h2>{title}</h2>
      <table><tbody>
        {Object.entries(data[g]).map(([k, r]) => (
          <tr key={k}>
            <td className="l">{r.label}</td>
            <td><input type="number" min={0} style={{ width: 70 }} value={val(g, k)}
              onChange={(e) => setEdit({ ...edit, [g]: { ...edit[g], [k]: e.target.value } })} /></td>
            <td className="muted">暫定 {r.default}</td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
  return (
    <div className="panel">
      <h2>配点設定（成長期待点・売り点数） {data.customized ? <span className="badge info">利用者設定</span> : <span className="badge warn">暫定配点</span>}</h2>
      <p className="muted">従来の配点がある場合はここで重みを入力してください（合計は100でなくても比率で計算）。0にするとその観点は使いません。
        変更すると計算方式のバージョンが変わり、変更前の採点結果も保存されます。{data.note && `最終変更: ${data.note.updated_at.slice(0, 16)}「${data.note.note}」`}</p>
      <div className="grid">{table("growth", "A. 成長期待点")}{table("sell", "C. 売り点数")}</div>
      <div className="row">
        <input style={{ flex: 1, minWidth: 200 }} placeholder="変更理由（必須）例: 従来の配点に合わせる" value={note} onChange={(e) => setNote(e.target.value)} />
        <button className="primary" onClick={save}>配点を保存</button><span>{msg}</span>
      </div>
    </div>
  );
}

const STATUS_CLS: Record<string, string> = { done: "good", waiting: "warn", delayed: "bad", failed: "bad", skipped: "", running: "info" };

export default function System() {
  const jobs = useApi<Job[]>("/jobs");
  const notes = useApi<Notice[]>("/notifications");
  const quality = useApi<Quality>("/quality");
  const [msg, setMsg] = useState("");
  return (
    <>
      <div className="panel">
        <h2>プッシュ通知（iPhone・PC）</h2>
        <div className="row">
          <button className="primary" onClick={() => subscribePush().then(setMsg).catch((e) => setMsg(String(e)))}>この端末で通知を受け取る</button>
          <span>{msg}</span>
        </div>
        <p className="muted">毎営業日18:00に購入候補・保有株の売却検討を通知します。データ遅延時は遅延通知を送ります。</p>
      </div>
      <WeightsEditor />
      <div className="grid">
        <div className="panel">
          <h2>通知履歴</h2>
          <ErrorBox error={notes.error} />
          {notes.data?.map((n) => (
            <div key={n.id} style={{ marginBottom: 8 }}>
              <div><b>{n.title}</b> <span className="muted">{n.created_at.slice(0, 16)} {n.delivered ? "送信済" : "アプリ内のみ"}</span></div>
              <div>{n.body}</div>
            </div>
          ))}
        </div>
        <div className="panel">
          <h2>ジョブ実行履歴</h2>
          <ErrorBox error={jobs.error} />
          <div className="table-wrap"><table>
            <thead><tr><th className="l">日付</th><th className="l">段階</th><th className="l">状態</th><th className="l">内容</th><th className="l">開始</th></tr></thead>
            <tbody>
              {jobs.data?.slice(0, 40).map((j) => (
                <tr key={j.id}>
                  <td className="l">{j.run_date}</td><td className="l">{j.stage}</td>
                  <td className="l"><span className={`badge ${STATUS_CLS[j.status] ?? ""}`}>{j.status}</span></td>
                  <td className="l" style={{ whiteSpace: "normal" }}>{j.message}</td><td className="l">{j.started_at.slice(11, 16)}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </div>
      </div>
      <div className="panel">
        <h2>データ品質・取得不可</h2>
        <ErrorBox error={quality.error} />
        <div className="table-wrap"><table>
          <thead><tr><th className="l">銘柄</th><th className="l">日付</th><th className="l">項目</th><th className="l">種別</th><th className="l">詳細</th></tr></thead>
          <tbody>
            {quality.data?.issues.slice(0, 50).map((q, i) => (
              <tr key={i}><td className="l">{q.code}</td><td className="l">{q.date}</td><td className="l">{q.field}</td><td className="l"><span className="badge warn">{q.issue}</span></td><td className="l">{q.detail}</td></tr>
            ))}
            {quality.data?.fetch_failures.slice(0, 50).map((f, i) => (
              <tr key={`f${i}`}><td className="l">{f.target}</td><td className="l">{f.fetched_at.slice(0, 16)}</td><td className="l">{f.source}</td><td className="l"><span className="badge bad">取得不可</span></td><td className="l">{f.message}</td></tr>
            ))}
          </tbody>
        </table></div>
      </div>
    </>
  );
}
