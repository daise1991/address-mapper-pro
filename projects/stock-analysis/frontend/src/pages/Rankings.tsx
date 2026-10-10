import { useMemo, useState } from "react";
import { Meta, ScoreRow } from "../api";
import { Decision, ErrorBox, fmt, Score, useApi } from "../ui";

type Resp = { run_date: string | null; dates: string[]; items: ScoreRow[] };
type SortKey = "growth_score" | "buy_score" | "sell_score" | "code" | "upside";
const VIEWS: [SortKey, string][] = [
  ["buy_score", "買い点数ランキング"],
  ["sell_score", "売り点数ランキング"],
  ["growth_score", "成長期待ランキング"],
  ["upside", "狙い目買値と株価の比較"],
  ["code", "60銘柄（コード順）"],
];

export default function Rankings({ meta, onOpen }: { meta: Meta; onOpen: (c: string) => void }) {
  const [date, setDate] = useState<string>("");
  const [group, setGroup] = useState("all");
  const [sort, setSort] = useState<SortKey>("buy_score");
  const [q, setQ] = useState("");
  const { data, error } = useApi<Resp>(`/rankings${date ? `?run_date=${date}` : ""}`, [date]);

  const items = useMemo(() => {
    const val = (r: ScoreRow): number => {
      if (sort === "upside") return r.theoretical_value && r.price ? r.theoretical_value / r.price - 1 : -Infinity;
      if (sort === "code") return 0;
      const v = r[sort];
      return v === null ? -Infinity : v;
    };
    return (data?.items ?? [])
      .filter((r) => group === "all" || r.group_name === group || (group === "held" && r.held))
      .filter((r) => !q || r.code.includes(q) || r.name.includes(q))
      .sort((a, b) => (sort === "code" ? a.code.localeCompare(b.code) : val(b) - val(a)));
  }, [data, group, sort, q]);

  const th = (k: SortKey, label: string) => (
    <th onClick={() => setSort(k)} className={k === "code" ? "l" : ""}>
      {label}
      {sort === k ? " ▼" : ""}
    </th>
  );

  return (
    <div className="panel">
      <h2>ランキング {data?.run_date && <span className="muted">（{data.run_date} 大引け）</span>}</h2>
      <ErrorBox error={error} />
      <div className="row">
        {VIEWS.map(([k, label]) => (
          <button key={k} className={sort === k ? "active" : ""} onClick={() => setSort(k)}>{label}</button>
        ))}
      </div>
      <div className="row">
        <select value={date} onChange={(e) => setDate(e.target.value)} aria-label="採点日">
          <option value="">最新</option>
          {data?.dates.map((d) => (
            <option key={d}>{d}</option>
          ))}
        </select>
        <select value={group} onChange={(e) => setGroup(e.target.value)} aria-label="グループ">
          <option value="all">全銘柄</option>
          <option value="held">保有中</option>
          <option value="existing">既存</option>
          <option value="added">追加</option>
          <option value="emerging">新興・成長</option>
          <option value="user">手動追加</option>
        </select>
        <input placeholder="コード・銘柄名" value={q} onChange={(e) => setQ(e.target.value)} />
        <span className="muted">{items.length}銘柄</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {th("code", "銘柄")}
              <th>株価</th>
              {th("growth_score", "成長期待")}
              {th("buy_score", "買い")}
              {th("sell_score", "売り")}
              <th>理論価値</th>
              <th>狙い目買値</th>
              <th>狙い目まで</th>
              {th("upside", "理論価値との差")}
              <th className="l">購入判定</th>
              <th className="l">売却判定</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.code} className="click" onClick={() => onOpen(r.code)}>
                <td className="l">
                  <b>{r.code}</b> {r.name} {r.held && <span className="badge info">保有</span>}
                  {r.listing_status === "delisted" && <span className="badge bad">上場確認不可</span>}
                </td>
                <td>{fmt(r.price, 1)}</td>
                <td><Score v={r.growth_score} kind="growth" /></td>
                <td title={`k=150: ${r.buy_score_k150 ?? "—"} / ${r.buy_detail?.reason ?? ""}`}>
                  <Score v={r.buy_score} kind="buy" />
                  {r.buy_detail?.assumption_origin === "auto" && r.buy_score !== null && <span className="muted"> 自</span>}
                </td>
                <td><Score v={r.sell_score} kind="sell" /></td>
                <td>{fmt(r.theoretical_value)}</td>
                <td>{fmt(r.target_price)}</td>
                <td style={{ color: r.target_price && r.price && r.price <= r.target_price ? "var(--good)" : undefined }}>
                  {r.target_price && r.price ? (r.price <= r.target_price ? "到達" : `あと${((1 - r.target_price / r.price) * 100).toFixed(0)}%下落`) : "—"}
                </td>
                <td>{r.theoretical_value && r.price ? `${(((r.theoretical_value / r.price) - 1) * 100).toFixed(0)}%` : "—"}</td>
                <td className="l"><Decision type="buy" value={r.buy_decision} labels={meta.buy_labels} /></td>
                <td className="l">
                  <Decision type="sell" value={r.sell_decision} labels={meta.sell_labels} />{" "}
                  {r.exit_type && <Decision type="exit" value={r.exit_type} labels={meta.exit_labels} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">成長期待点・売り点数の配点は「通知・システム → 配点設定」で確認・変更できます。「自」= 評価前提が自動初期推計（会社予想ベース）。銘柄詳細で根拠付きの前提に置き換えてください。買い点数にカーソルを合わせると係数150の比較値を表示します。</p>
    </div>
  );
}
