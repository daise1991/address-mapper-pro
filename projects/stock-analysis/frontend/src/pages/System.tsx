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
