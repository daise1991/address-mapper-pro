"""v5 の特徴量: 過去3〜5年の推移を含め、評価日時点で公表済みのデータだけから作る。

株式分割の影響を受けないよう、1株あたりの値は「純利益÷株数」や比率（配当性向など）で扱い、
株数は J-Quants の調整係数で分割を補正する。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..scoring.features import latest_forecast, split_ratio

MIN_YEARS_FOR_CAGR = 3  # 年平均成長率は3年以上の期間がある場合だけ計算する
MAX_HISTORY = 6          # 最大6期（5年間の成長率）


def cutoff_of(d: date) -> str:
    return f"{d.isoformat()}T23:59:59+09:00"


def fy_history(conn: sqlite3.Connection, code: str, as_of: date) -> list[dict[str, Any]]:
    """公表済みの本決算（各期の最新の公表）を古い順に最大6期。"""
    rows = conn.execute(
        """SELECT * FROM financials WHERE code=? AND period_type='FY' AND disclosed_at<=?
           ORDER BY fiscal_period_end DESC, disclosed_at DESC""", (code, cutoff_of(as_of))).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["fiscal_period_end"] in seen:
            continue
        seen.add(r["fiscal_period_end"])
        out.append(dict(r))
    return list(reversed(out[:MAX_HISTORY]))


def cagr(first: float | None, last: float | None, years: float) -> float | None:
    if first is None or last is None or years < MIN_YEARS_FOR_CAGR or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def _years(a: str, b: str) -> float:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days / 365.25


@dataclass
class V5Features:
    code: str
    as_of: date
    price: float | None
    values: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def get(self, k: str) -> Any:
        return self.values.get(k)


def build(conn: sqlite3.Connection, code: str, as_of: date, price: float | None, indicators: dict[str, Any]) -> V5Features:
    f = V5Features(code, as_of, price)
    v = f.values
    v.update(indicators)
    hist = fy_history(conn, code, as_of)
    v["history_years"] = len(hist)
    if not hist:
        return f
    cur = hist[-1]
    f.sources.append(f"{cur['source']}:{cur['fiscal_period_end']}@{cur['disclosed_at']}")
    first = hist[0]
    span = _years(first["fiscal_period_end"], cur["fiscal_period_end"])
    v["history_span_years"] = round(span, 2)

    # --- 過去3〜5年の推移 ---
    v["revenue_cagr_hist"] = cagr(first["revenue"], cur["revenue"], span)
    op_first, op_last = first["operating_profit"], cur["operating_profit"]
    v["op_cagr_hist"] = cagr(op_first, op_last, span)
    if v["op_cagr_hist"] is None and span >= MIN_YEARS_FOR_CAGR and op_first is not None and op_last is not None:
        # 赤字を含む期間は成長率を計算できないため、方向だけを記録する
        v["op_trend_hist"] = "turnaround" if (op_first <= 0 < op_last) else ("loss_improving" if op_last > op_first
                                                                             else "worsening")
    margins = [r["operating_profit"] / r["revenue"] for r in hist if r["revenue"] and r["operating_profit"] is not None]
    v["op_margin_avg"] = sum(margins) / len(margins) if len(margins) >= 3 else None
    v["op_margin_latest"] = margins[-1] if margins else None
    roes = [r["net_income"] / r["equity"] for r in hist if r["equity"] and r["equity"] > 0 and r["net_income"] is not None]
    v["roe_avg"] = sum(roes) / len(roes) if len(roes) >= 3 else None
    cfs = [r["operating_cf"] for r in hist if r["operating_cf"] is not None]
    v["opcf_positive_ratio"] = (sum(1 for x in cfs if x > 0) / len(cfs)) if len(cfs) >= 3 else None
    v["operating_cf"] = cur["operating_cf"]
    if cur["total_assets"]:
        v["equity_ratio"] = (cur["equity"] or 0) / cur["total_assets"] if cur["equity"] is not None else None
        debt = cur["interest_bearing_debt"]
        if debt is None:
            eb = conn.execute("SELECT interest_bearing_debt FROM edinet_balance WHERE code=? AND period_end=?",
                              (code, cur["fiscal_period_end"])).fetchone()
            debt = eb["interest_bearing_debt"] if eb else None
        if cur["cash"] is not None and debt is not None:
            v["net_cash_to_assets"] = (cur["cash"] - debt) / cur["total_assets"]
    v["equity"] = cur["equity"]

    # --- 会社予想（評価日時点の最新） ---
    lf = latest_forecast(conn, code, cur["fiscal_period_end"], cutoff_of(as_of))
    fc = {k: (lf or {}).get(k) or cur.get(k) for k in ("forecast_revenue", "forecast_operating_profit", "forecast_eps", "forecast_dividend")}
    if lf:
        f.sources.append(f"{lf['source']}:予想{lf['forecast_fy_end']}@{lf['disclosed_at']}")
    v["forecast_revenue_growth"] = (fc["forecast_revenue"] / cur["revenue"] - 1) if (fc["forecast_revenue"] and cur["revenue"]) else None
    if fc["forecast_operating_profit"] is not None and op_last:
        v["forecast_op_growth"] = (fc["forecast_operating_profit"] / op_last - 1) if op_last > 0 else None
    v["forecast_op"] = fc["forecast_operating_profit"]
    # 1株あたりの値は、決算公表後の株式分割を補正して現在の基準にそろえる
    adj_after = 1 / split_ratio(conn, code, (lf or cur)["disclosed_at"], as_of.isoformat())
    v["forecast_eps"] = fc["forecast_eps"] * adj_after if fc["forecast_eps"] else None
    v["forecast_dps"] = fc["forecast_dividend"] * adj_after if fc["forecast_dividend"] is not None else None
    v["latest_eps"] = cur["eps"] * (1 / split_ratio(conn, code, cur["disclosed_at"], as_of.isoformat())) if cur["eps"] else None
    v["forecast_fy_end"] = (lf or {}).get("forecast_fy_end") or cur.get("forecast_fy_end")

    # --- 配当性向（比率なので分割の影響を受けない）---
    payouts = [r["dividend_per_share"] / r["eps"] for r in hist[-3:]
               if r["dividend_per_share"] is not None and r["eps"] and r["eps"] > 0]
    v["payout_ratio_avg"] = min(max(sum(payouts) / len(payouts), 0.0), 0.7) if payouts else None
    v["dividend_known"] = bool(payouts) or v["forecast_dps"] is not None

    # --- 株数の増減（分割補正済み）---
    if len(hist) >= 2 and hist[-2]["shares_outstanding"] and cur["shares_outstanding"]:
        ratio = cur["shares_outstanding"] / hist[-2]["shares_outstanding"]
        ratio /= split_ratio(conn, code, hist[-2]["disclosed_at"], cur["disclosed_at"])
        if ratio >= 1.9 and abs(ratio - round(ratio)) < 0.05:
            f.sources.append(f"株数が{ratio:.1f}倍（株式分割とみなし、希薄化判定から除外）")
            v["share_change_excluded_as_split"] = round(ratio, 2)
        else:
            v["share_change"] = ratio - 1

    # --- 四半期進捗・開示 ---
    q = conn.execute("""SELECT * FROM financials WHERE code=? AND period_type IN ('1Q','2Q','3Q') AND disclosed_at<=?
                        ORDER BY disclosed_at DESC LIMIT 1""", (code, cutoff_of(as_of))).fetchone()
    if q and q["forecast_operating_profit"] and q["forecast_operating_profit"] > 0 and q["operating_profit"] is not None:
        expected = {"1Q": 0.25, "2Q": 0.5, "3Q": 0.75}[q["period_type"]]
        v["progress_gap"] = q["operating_profit"] / q["forecast_operating_profit"] - expected
    since_d = (as_of - timedelta(days=120)).isoformat()
    for cat in ("revision_down", "revision_up", "offering"):
        v[f"disc_{cat}_120d"] = conn.execute(
            "SELECT COUNT(*) FROM disclosures WHERE code=? AND category=? AND disclosed_at>? AND disclosed_at<=?",
            (code, cat, since_d, cutoff_of(as_of))).fetchone()[0]
    v["max_op_forecast_cut_120d"] = max_forecast_cut(conn, code, since_d, cutoff_of(as_of))
    return f


def max_forecast_cut(conn: sqlite3.Connection, code: str, since: str, until: str) -> float | None:
    """期間内に公表された営業利益予想の、同じ決算期の直前予想からの最大下落率（例: -0.2 = 20%減額）。"""
    rows = conn.execute("""SELECT forecast_fy_end, disclosed_at, forecast_operating_profit FROM financials
                           WHERE code=? AND forecast_fy_end IS NOT NULL AND forecast_operating_profit IS NOT NULL
                             AND disclosed_at<=? ORDER BY forecast_fy_end, disclosed_at""", (code, until)).fetchall()
    worst, prev = None, {}
    for r in rows:
        p = prev.get(r["forecast_fy_end"])
        prev[r["forecast_fy_end"]] = r["forecast_operating_profit"]
        if p and p > 0 and r["disclosed_at"] > since:
            chg = r["forecast_operating_profit"] / p - 1
            worst = chg if worst is None else min(worst, chg)
    return worst


def company_forecast_errors(conn: sqlite3.Connection, code: str, as_of: date) -> list[float]:
    """過去の会社予想（期初の営業利益予想）と実績の差。予測誤差の検証に使う（評価日時点で確定済みの期のみ）。"""
    hist = fy_history(conn, code, as_of)
    by_end = {h["fiscal_period_end"]: h for h in hist}
    errs = []
    for h in hist:
        target = h.get("forecast_fy_end")
        fc = h.get("forecast_operating_profit")
        actual = by_end.get(target, {}).get("operating_profit") if target else None
        if fc and fc > 0 and actual is not None:
            errs.append(abs(actual / fc - 1))
    return errs
