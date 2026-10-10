import { useEffect, useState } from "react";
import { Meta, ScoreRow, send } from "../api";
import { Bar, Decision, ErrorBox, fmt, pct, PriceChart, Score, ScoreHistory, StatusBadge, useApi } from "../ui";

type Assumption = {
  method: string; target_year: number; fiscal_month_end: number; required_return: number; safety_margin: number; reason: string;
  eps_forecast: number | null; assumed_per: number | null; fcf_forecast: number | null; terminal_growth: number | null;
  net_cash_per_share: number | null; normalized_eps: number | null; bps_forecast: number | null; target_pbr: number | null;
};
type Research = {
  status: string; status_label: string; confidence: string | null; confidence_reason: string | null;
  category: string | null; category_reason: string | null; valuation_method: string; target_year: number;
  scenarios: Record<string, { eps: number; per: number; rationale: string }>;
  required_return: number; safety_margin: number; rr_reason: string | null; anchor_type: string; anchor_eps: number | null;
  detail: {
    latest_company_forecast?: { fiscal_period: string; eps: number | null; disclosed_date: string; revised?: string; source_url?: string };
    history?: { fiscal_period: string; eps: number | null; op_margin: number | null; net_income: number | null; per_at_fy_end: number | null }[];
    split_notes?: string; midterm_plan?: { name: string; target_period: string; targets: string; url: string; published: string } | null;
    orders_and_drivers?: string; normalized_eps?: { value: number | null; method: string };
  } | null;
  sources: { title: string; url: string; published: string }[] | null;
  unverified: string[] | null; review_reasons: string[] | null;
  approved_at: string | null; approved_note: string | null; updated_at: string;
};
const SCEN_LABEL: Record<string, string> = { bear: "弱気", base: "標準", bull: "強気" };

function ResearchPanel({ code, r, live, onChanged }: {
  code: string; r: Research; live?: Record<string, { theoretical_value: number | null; target_price: number | null; buy_score: number | null; eps: number }>;
  onChanged: () => void;
}) {
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState("");
  const d = r.detail ?? {};
  const approve = async () => {
    try {
      await send("/research/approve", "POST", { codes: [code], note });
      setMsg("検証済みにしました");
      onChanged();
    } catch (e) {
      setMsg(`失敗: ${(e as Error).message}`);
    }
  };
  return (
    <div className="panel">
      <h2>評価前提（調査） <StatusBadge status={r.status} /> <span className="badge">信頼度 {r.confidence ?? "—"}</span></h2>
      {r.review_reasons && r.review_reasons.length > 0 && <div className="banner bad">再承認が必要: {r.review_reasons.join(" ／ ")}</div>}
      <p className="muted">2029年のEPS・PERは<b>推計値</b>であり事実ではありません。目標期: 決算期末が{r.target_year}年の期 ／ 評価方式: {r.valuation_method === "normalized" ? "正常化利益" : "PER"} ／ 必要利回り {pct(r.required_return)}・安全余裕 {pct(r.safety_margin, 0)}（{r.rr_reason}）</p>
      <div className="table-wrap"><table>
        <thead><tr><th className="l">シナリオ</th><th>2029年EPS（推計）</th><th>想定PER</th><th>当日の2029年EPS</th><th>現在理論価値</th><th>狙い目買値</th><th>買い点数</th><th className="l">根拠</th></tr></thead>
        <tbody>
          {["bear", "base", "bull"].map((k) => (
            <tr key={k}>
              <td className="l"><b>{SCEN_LABEL[k]}</b></td>
              <td>{fmt(r.scenarios[k]?.eps, 1)}</td><td>{fmt(r.scenarios[k]?.per, 1)}</td>
              <td>{fmt(live?.[k]?.eps ?? null, 1)}</td>
              <td>{fmt(live?.[k]?.theoretical_value ?? null)}</td><td>{fmt(live?.[k]?.target_price ?? null)}</td>
              <td>{live?.[k]?.buy_score ?? "—"}</td>
              <td className="l wrap" style={{ textAlign: "left", minWidth: 260 }}>{r.scenarios[k]?.rationale}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      <p className="muted">「当日の2029年EPS」は承認済みルールで自動更新した値です（基準EPS＝{r.anchor_type === "normalized" ? "正常化EPS" : "会社予想EPS"} {fmt(r.anchor_eps, 1)}円の変化率の半分を反映。±15%以上の変化で再承認を求めます）。</p>
      <div className="grid">
        <div>
          <h2>会社予想・過去推移</h2>
          {d.latest_company_forecast && <p>最新会社予想 {d.latest_company_forecast.fiscal_period}: EPS {fmt(d.latest_company_forecast.eps ?? null, 1)}円（{d.latest_company_forecast.disclosed_date}）<br /><span className="muted">{d.latest_company_forecast.revised}</span></p>}
          <table><thead><tr><th className="l">期</th><th>EPS</th><th>営業利益率</th><th>期末PER</th></tr></thead><tbody>
            {(d.history ?? []).map((h) => <tr key={h.fiscal_period}><td className="l">{h.fiscal_period}</td><td>{fmt(h.eps, 1)}</td><td>{pct(h.op_margin)}</td><td>{fmt(h.per_at_fy_end, 1)}</td></tr>)}
          </tbody></table>
          <p className="muted">{d.split_notes}</p>
          {d.normalized_eps && <p>正常化EPS {fmt(d.normalized_eps.value, 1)}円 <span className="muted">（{d.normalized_eps.method}）</span></p>}
        </div>
        <div>
          <h2>中期経営計画・受注</h2>
          {d.midterm_plan && <p><b>{d.midterm_plan.name}</b>（{d.midterm_plan.target_period}）: {d.midterm_plan.targets} <a href={d.midterm_plan.url} target="_blank" rel="noreferrer">資料</a></p>}
          <p>{d.orders_and_drivers}</p>
          <p className="muted">分類: {r.category}（{r.category_reason}）</p>
        </div>
      </div>
      <h2>使用した資料</h2>
      <ul>{(r.sources ?? []).map((x, i) => <li key={i}><a href={x.url} target="_blank" rel="noreferrer">{x.title}</a> <span className="muted">{x.published}</span></li>)}</ul>
      <h2>未確認事項</h2>
      <ul>{(r.unverified ?? []).map((x, i) => <li key={i} className="muted">{x}</li>)}</ul>
      <p className="muted">信頼度の理由: {r.confidence_reason}</p>
      {r.status !== "verified" ? (
        <div className="row">
          <input style={{ flex: 1, minWidth: 200 }} placeholder="確認内容（必須）例: 会社予想と中計を確認、PERは妥当" value={note} onChange={(e) => setNote(e.target.value)} />
          <button className="primary" onClick={approve}>この前提を検証済みにする</button><span>{msg}</span>
        </div>
      ) : (
        <p className="muted">検証済み: {r.approved_at?.slice(0, 16)}「{r.approved_note}」</p>
      )}
    </div>
  );
}

type DetailResp = {
  security: { code: string; name: string; category: string; asset_type: string; listing_status: string; verified_at: string | null; note: string | null };
  scores: ScoreRow[];
  quotes: { date: string; close: number | null; adj_close: number | null; source: string; fetched_at: string }[];
  financials: Record<string, number | string | null>[];
  disclosures: { disclosed_at: string; title: string; category: string; source: string }[];
  assumption: Assumption | null;
  assumption_origin: string | null;
  assumption_history: (Assumption & { updated_at: string; origin: string })[];
  qualitative: { factor: string; score: number; reason: string; updated_at: string }[];
  quality_issues: { date: string; field: string; issue: string; detail: string }[];
  research: Research | null;
};

const NUM_FIELDS: [keyof Assumption, string][] = [
  ["eps_forecast", "目標年度 予想EPS"], ["assumed_per", "想定PER"], ["normalized_eps", "正常化EPS"],
  ["fcf_forecast", "1株FCF(目標年度)"], ["terminal_growth", "永久成長率"], ["net_cash_per_share", "1株ネットキャッシュ"],
  ["bps_forecast", "予想BPS"], ["target_pbr", "目標PBR"],
];
const QUAL = [["market_expansion", "市場規模の拡大"], ["competitive_advantage", "競争優位性"], ["growth_outlook", "2028〜29年の成長見通し"]] as const;

function Factors({ title, f }: { title: string; f: Record<string, { label: string; score: number | null; weight: number }> }) {
  return (
    <table>
      <thead><tr><th className="l">{title}</th><th>点</th><th>重み</th></tr></thead>
      <tbody>
        {Object.entries(f).map(([k, v]) => (
          <tr key={k}><td className="l">{v.label}</td><td><Bar v={v.score} /> {v.score === null ? "—" : Math.round(v.score)}</td><td>{v.weight}</td></tr>
        ))}
      </tbody>
    </table>
  );
}

export default function Detail({ code, meta, onBack }: { code: string; meta: Meta; onBack: () => void }) {
  const { data, error, reload } = useApi<DetailResp>(`/securities/${code}`, [code]);
  const [form, setForm] = useState<Assumption | null>(null);
  const [msg, setMsg] = useState("");
  const [qual, setQual] = useState<Record<string, { score: string; reason: string }>>({});

  useEffect(() => {
    if (!data) return;
    const def = meta.category_defaults[data.security.category];
    setForm(
      data.assumption ?? {
        method: data.security.category === "emerging_loss" ? "unrated" : "per", target_year: 2029, fiscal_month_end: 3,
        required_return: def?.required_return ?? 0.1, safety_margin: def?.safety_margin ?? 0.15, reason: "",
        eps_forecast: null, assumed_per: null, fcf_forecast: null, terminal_growth: null, net_cash_per_share: null,
        normalized_eps: null, bps_forecast: null, target_pbr: null,
      },
    );
    setQual(Object.fromEntries(data.qualitative.map((q) => [q.factor, { score: String(q.score), reason: q.reason }])));
  }, [data, meta]);

  if (error) return <ErrorBox error={error} />;
  if (!data || !form) return <p className="muted">読み込み中…</p>;
  const s = data.scores[0];
  const sec = data.security;

  const saveAssumption = async () => {
    try {
      await send(`/securities/${code}/assumption`, "PUT", form);
      setMsg("評価前提を保存しました。次回の採点から反映されます。");
      reload();
    } catch (e) {
      setMsg(`保存失敗: ${(e as Error).message}`);
    }
  };
  const saveQual = async (factor: string) => {
    const q = qual[factor];
    try {
      await send(`/securities/${code}/qualitative/${factor}`, "PUT", { score: Number(q.score), reason: q.reason });
      setMsg("定性評価を保存しました。");
      reload();
    } catch (e) {
      setMsg(`保存失敗: ${(e as Error).message}`);
    }
  };
  const num = (v: string) => (v === "" ? null : Number(v));

  return (
    <>
      <button onClick={onBack}>← 戻る</button>
      <div className="panel" style={{ marginTop: 12 }}>
        <h2>{sec.code} {sec.name} <span className="badge">{meta.categories[sec.category] ?? sec.category}</span>{" "}
          {sec.asset_type === "etf" && <span className="badge">ETF</span>}{" "}
          <span className="badge">{sec.listing_status === "listed" ? "上場確認済" : sec.listing_status === "delisted" ? "上場確認不可" : "上場未検証"}</span></h2>
        {sec.note && <p className="muted">{sec.note}</p>}
        {s ? (
          <div className="row">
            <span>{s.run_date} 終値 <b>{fmt(s.price, 1)}</b></span>
            <span>成長期待 <Score v={s.growth_score} kind="growth" /></span>
            <span>買い <Score v={s.buy_score} kind="buy" /> <span className="muted">(k150: {s.buy_score_k150 ?? "—"})</span></span>
            <span>売り <Score v={s.sell_score} kind="sell" /></span>
            <Decision type="buy" value={s.buy_decision} labels={meta.buy_labels} />
            <Decision type="sell" value={s.sell_decision} labels={meta.sell_labels} />
            {s.exit_type && <Decision type="exit" value={s.exit_type} labels={meta.exit_labels} />}
          </div>
        ) : <p className="muted">採点履歴なし</p>}
        <PriceChart quotes={data.quotes} />
        <p className="muted">青: 調整後終値 / 橙: 25日線 / 緑: 75日線。株価出所: {data.quotes.at(-1)?.source ?? "—"}（取得 {data.quotes.at(-1)?.fetched_at ?? "—"}）</p>
      </div>

      {data.research && <ResearchPanel code={code} r={data.research} live={s?.buy_detail.scenarios} onChanged={reload} />}

      <div className="panel">
        <h2>過去の点数推移</h2>
        <ScoreHistory scores={data.scores} />
      </div>

      {s && (
        <div className="grid">
          <div className="panel">
            <h2>A. 成長期待点 {s.method_version?.includes("暫定") && <span className="badge warn">暫定配点</span>} <span className="muted">カバー率 {pct(s.growth_detail.coverage, 0)}</span></h2>
            <Factors title="観点" f={s.growth_detail.factors} />
          </div>
          <div className="panel">
            <h2>B. 買い点数（将来価値方式）</h2>
            <table><tbody>
              <tr><td className="l">手法</td><td className="wrap">{s.buy_detail.method}（{s.buy_detail.reason}）</td></tr>
              <tr><td className="l">評価の段階</td><td><StatusBadge status={s.buy_detail.assumption_status} /></td></tr>
              <tr><td className="l">前提の出所</td><td>{{ manual: "手動登録", research: "調査前提（標準シナリオ）", auto: "自動初期推計（参考）" }[s.buy_detail.assumption_origin] ?? s.buy_detail.assumption_origin}</td></tr>
              <tr><td className="l">将来株価</td><td>{fmt(s.future_price)}</td></tr>
              <tr><td className="l">割引年数</td><td>{fmt(s.buy_detail.years, 2)}</td></tr>
              <tr><td className="l">現在理論価値 V</td><td>{fmt(s.theoretical_value)}</td></tr>
              <tr><td className="l">V / P</td><td>{fmt(s.buy_detail.v_over_p, 3)}</td></tr>
              <tr><td className="l">狙い目買値</td><td>{fmt(s.target_price)}</td></tr>
              {s.buy_detail.inputs && <>
                <tr><td className="l">目標年度</td><td>{s.buy_detail.inputs.target_year}年度（決算{s.buy_detail.inputs.fiscal_month_end}月）</td></tr>
                <tr><td className="l">将来予想EPS</td><td>{fmt(s.buy_detail.inputs.eps_forecast ?? s.buy_detail.inputs.normalized_eps ?? null, 1)}</td></tr>
                <tr><td className="l">想定PER</td><td>{fmt(s.buy_detail.inputs.assumed_per ?? null, 1)}</td></tr>
                <tr><td className="l">必要利回り／安全余裕</td><td>{pct(s.buy_detail.inputs.required_return)}／{pct(s.buy_detail.inputs.safety_margin, 0)}</td></tr>
                <tr><td className="l">評価根拠</td><td className="wrap">{s.buy_detail.inputs.reason}</td></tr>
              </>}
              <tr><td className="l">計算方式</td><td className="wrap">{s.method_version ?? "—"}</td></tr>
            </tbody></table>
          </div>
          <div className="panel">
            <h2>C. 売り点数 {s.method_version?.includes("暫定") && <span className="badge warn">暫定配点</span>} <span className="muted">割高系 {fmt(s.sell_detail.valuation_side ?? null, 0)} / 業績・財務系 {fmt(s.sell_detail.fundamental_side ?? null, 0)}</span></h2>
            <Factors title="観点" f={s.sell_detail.factors} />
          </div>
          <div className="panel">
            <h2>テクニカル</h2>
            <table><tbody>
              {["rsi14", "sma5", "sma25", "sma75", "sma200", "dev25", "dev200"].map((k) => (
                <tr key={k}><td className="l">{k.toUpperCase()}</td><td>{k.startsWith("dev") ? pct(s.indicators[k] ?? null) : fmt(s.indicators[k] ?? null, 1)}</td></tr>
              ))}
            </tbody></table>
            <p className="muted">財務出所: {(s.indicators.sources ?? []).join(", ") || "—"}</p>
          </div>
        </div>
      )}

      <div className="panel">
        <h2>評価前提（2028〜2029年の企業価値推計）</h2>
        <p className="muted">現在: {data.assumption_origin === "manual" ? "手動登録" : "未登録（自動初期推計を使用）"}。変更理由は必須で、履歴に残ります。赤字企業にPERを当てはめず、予測できない場合は手法「未算定」を選んでください。</p>
        <div className="form">
          <label className="f">手法
            <select value={form.method} onChange={(e) => setForm({ ...form, method: e.target.value })}>
              <option value="per">PER（予想EPS×想定PER）</option>
              <option value="normalized">正常化利益（景気循環）</option>
              <option value="dcf">DCF</option>
              <option value="asset">資産価値（BPS×PBR）</option>
              <option value="unrated">未算定</option>
            </select>
          </label>
          <label className="f">目標年度
            <select value={form.target_year} onChange={(e) => setForm({ ...form, target_year: Number(e.target.value) })}>
              <option value={2028}>2028年度</option><option value={2029}>2029年度</option>
            </select>
          </label>
          <label className="f">決算月<input type="number" min={1} max={12} value={form.fiscal_month_end} onChange={(e) => setForm({ ...form, fiscal_month_end: Number(e.target.value) })} /></label>
          <label className="f">必要利回り<input type="number" step="0.005" value={form.required_return} onChange={(e) => setForm({ ...form, required_return: Number(e.target.value) })} /></label>
          <label className="f">安全余裕<input type="number" step="0.01" value={form.safety_margin} onChange={(e) => setForm({ ...form, safety_margin: Number(e.target.value) })} /></label>
          {NUM_FIELDS.map(([k, label]) => (
            <label className="f" key={k}>{label}
              <input type="number" step="any" value={(form[k] as number | null) ?? ""} onChange={(e) => setForm({ ...form, [k]: num(e.target.value) })} />
            </label>
          ))}
        </div>
        <label className="f" style={{ marginTop: 8 }}>理由（必須）
          <input value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} placeholder="例: 生成AI向けテスター需要で2029年3月期EPS 450円、PER 28倍" />
        </label>
        <div className="row" style={{ marginTop: 8 }}><button className="primary" onClick={saveAssumption}>前提を保存</button><span>{msg}</span></div>
      </div>

      <div className="panel">
        <h2>定性評価（成長期待点に使用）</h2>
        {QUAL.map(([k, label]) => (
          <div className="row" key={k}>
            <span style={{ width: 170 }}>{label}</span>
            <input type="number" min={0} max={100} style={{ width: 80 }} value={qual[k]?.score ?? ""} onChange={(e) => setQual({ ...qual, [k]: { score: e.target.value, reason: qual[k]?.reason ?? "" } })} />
            <input style={{ flex: 1, minWidth: 200 }} placeholder="根拠" value={qual[k]?.reason ?? ""} onChange={(e) => setQual({ ...qual, [k]: { score: qual[k]?.score ?? "", reason: e.target.value } })} />
            <button onClick={() => saveQual(k)}>保存</button>
          </div>
        ))}
      </div>

      <div className="panel">
        <h2>決算データ（出所・公表日時付き）</h2>
        <div className="table-wrap"><table>
          <thead><tr><th className="l">決算期</th><th className="l">区分</th><th className="l">公表</th><th>売上高</th><th>営業利益</th><th>純利益</th><th>EPS</th><th>営業CF</th><th>予想EPS</th><th className="l">出所</th></tr></thead>
          <tbody>
            {data.financials.map((f, i) => (
              <tr key={i}>
                <td className="l">{f.fiscal_period_end}</td><td className="l">{f.period_type}</td><td className="l">{String(f.disclosed_at).slice(0, 16)}</td>
                <td>{fmt((f.revenue as number) / 1e8)}億</td><td>{fmt((f.operating_profit as number) / 1e8)}億</td><td>{fmt((f.net_income as number) / 1e8)}億</td>
                <td>{fmt(f.eps as number, 1)}</td><td>{f.operating_cf === null ? "—" : `${fmt((f.operating_cf as number) / 1e8)}億`}</td><td>{fmt(f.forecast_eps as number, 1)}</td><td className="l">{f.source}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      </div>

      <div className="grid">
        <div className="panel">
          <h2>開示</h2>
          {data.disclosures.length === 0 ? <p className="muted">なし</p> : data.disclosures.map((d, i) => (
            <div key={i} className="row"><span className="muted">{d.disclosed_at.slice(0, 10)}</span><span className="badge">{d.category}</span>{d.title}<span className="muted">({d.source})</span></div>
          ))}
        </div>
        <div className="panel">
          <h2>データ品質</h2>
          {data.quality_issues.length === 0 ? <p className="muted">未解決の課題なし</p> : data.quality_issues.map((q, i) => (
            <div key={i} className="row"><span className="muted">{q.date}</span><span className="badge warn">{q.issue}</span>{q.field}: {q.detail}</div>
          ))}
        </div>
      </div>
    </>
  );
}
