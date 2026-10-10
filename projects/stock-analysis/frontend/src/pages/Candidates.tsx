import { Meta, ScoreRow } from "../api";
import { Decision, ErrorBox, fmt, Score, useApi } from "../ui";

type Plan = {
  score_date: string | null;
  execution: string;
  spendable_cash: number;
  monthly_budget: number;
  month_spent: number;
  month_left: number;
  plan_budget: number;
  s_kabu_note: string;
  buy_orders: {
    code: string; name: string; decision: string; growth_score: number; buy_score: number; sell_score: number;
    last_close: number; target_price: number | null; upper_limit_price: number; quantity: number; estimated_amount: number; note: string;
  }[];
  skipped: { code: string; name: string; reason: string }[];
  sell_reviews: { code: string; name: string; quantity: number; sell_score: number; decision: string; exit_type: string | null; est_tax_if_sold: number }[];
  disclaimer: string;
};

function Group({ title, rows, meta, onOpen }: { title: string; rows: ScoreRow[]; meta: Meta; onOpen: (c: string) => void }) {
  return (
    <div className="panel">
      <h2>{title}（{rows.length}）</h2>
      {rows.length === 0 ? <p className="muted">該当なし</p> : (
        <div className="table-wrap"><table>
          <thead><tr><th className="l">銘柄</th><th>株価</th><th>成長</th><th>買い</th><th>売り</th><th>狙い目</th><th className="l">判定</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.code} className="click" onClick={() => onOpen(r.code)}>
                <td className="l"><b>{r.code}</b> {r.name}</td>
                <td>{fmt(r.price, 1)}</td>
                <td><Score v={r.growth_score} kind="growth" /></td>
                <td><Score v={r.buy_score} kind="buy" /></td>
                <td><Score v={r.sell_score} kind="sell" /></td>
                <td>{fmt(r.target_price)}</td>
                <td className="l">
                  <Decision type="buy" value={r.buy_decision} labels={meta.buy_labels} />{" "}
                  {r.held && <Decision type="sell" value={r.sell_decision} labels={meta.sell_labels} />}{" "}
                  {r.held && r.exit_type && <Decision type="exit" value={r.exit_type} labels={meta.exit_labels} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </div>
  );
}

export default function Candidates({ meta, onOpen }: { meta: Meta; onOpen: (c: string) => void }) {
  const { data, error } = useApi<{ items: ScoreRow[]; run_date: string }>("/rankings");
  const plan = useApi<Plan>("/order-plan");
  const items = data?.items ?? [];
  const by = (f: (r: ScoreRow) => boolean) => items.filter(f).sort((a, b) => (b.buy_score ?? 0) - (a.buy_score ?? 0));
  return (
    <>
      <ErrorBox error={error ?? plan.error} />
      <div className="panel">
        <h2>翌営業日 S株 注文案</h2>
        {plan.data && (
          <>
            <p>約定見込み: <b>{plan.data.execution}</b></p>
            <p>今月の投資予算 {fmt(plan.data.monthly_budget)}円 − 今月の購入済み {fmt(plan.data.month_spent)}円 ＝ 残り <b>{fmt(plan.data.month_left)}円</b>
              ／ 買付余力（現金留保後） {fmt(plan.data.spendable_cash)}円 → 今回の上限 <b>{fmt(plan.data.plan_budget)}円</b></p>
            <div className="banner warn">{plan.data.s_kabu_note}</div>
            {plan.data.buy_orders.length === 0 ? <p className="muted">購入候補なし、または買付余力不足</p> : (
              <div className="table-wrap"><table>
                <thead><tr><th className="l">銘柄</th><th className="l">区分</th><th>終値</th><th>狙い目</th><th>購入許容上限</th><th>株数</th><th>概算金額</th><th className="l">メモ</th></tr></thead>
                <tbody>
                  {plan.data.buy_orders.map((o) => (
                    <tr key={o.code}>
                      <td className="l"><b>{o.code}</b> {o.name}</td>
                      <td className="l"><Decision type="buy" value={o.decision} labels={meta.buy_labels} /></td>
                      <td>{fmt(o.last_close, 1)}</td><td>{fmt(o.target_price)}</td><td><b>{fmt(o.upper_limit_price, 1)}</b></td>
                      <td>{o.quantity}</td><td>{fmt(o.estimated_amount)}</td><td className="l muted">{o.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
            {plan.data.sell_reviews.length > 0 && (
              <>
                <h2 style={{ marginTop: 12 }}>保有株の売却検討</h2>
                <div className="table-wrap"><table>
                  <thead><tr><th className="l">銘柄</th><th>株数</th><th>売り</th><th className="l">判定</th><th>売却時の概算税額</th></tr></thead>
                  <tbody>
                    {plan.data.sell_reviews.map((s) => (
                      <tr key={s.code}>
                        <td className="l"><b>{s.code}</b> {s.name}</td><td>{s.quantity}</td><td><Score v={s.sell_score} kind="sell" /></td>
                        <td className="l"><Decision type="sell" value={s.decision} labels={meta.sell_labels} /> <Decision type="exit" value={s.exit_type} labels={meta.exit_labels} /></td>
                        <td>{fmt(s.est_tax_if_sold)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table></div>
              </>
            )}
            {plan.data.skipped.length > 0 && (
              <p className="muted">見送り: {plan.data.skipped.map((x) => `${x.name}（${x.reason}）`).join("、")}</p>
            )}
            <p className="muted">{plan.data.disclaimer}</p>
          </>
        )}
      </div>
      <Group title="積極購入候補" rows={by((r) => r.buy_decision === "aggressive")} meta={meta} onOpen={onOpen} />
      <Group title="通常購入候補" rows={by((r) => r.buy_decision === "normal")} meta={meta} onOpen={onOpen} />
      <Group title="購入条件の確認対象（買い65〜74）" rows={by((r) => r.buy_decision === "check")} meta={meta} onOpen={onOpen} />
      <Group title="割安待ち（成長期待は高いが買い点数が低い）" rows={by((r) => r.buy_decision === "wait_discount")} meta={meta} onOpen={onOpen} />
      <Group title="利益確定候補（成長は健全だが割高）" rows={by((r) => r.exit_type === "profit_taking")} meta={meta} onOpen={onOpen} />
      <Group title="損切り・撤退候補（業績・財務・希薄化など前提崩れ）" rows={by((r) => r.exit_type === "stop_loss")} meta={meta} onOpen={onOpen} />
    </>
  );
}
