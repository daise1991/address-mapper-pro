"""採点用の特徴量を「その日時点で公表済みのデータだけ」から作る（先読み防止）。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Sequence

from ..config import JST


def scale(x: float | None, points: Sequence[tuple[float, float]]) -> float | None:
    """区分線形の点数化。points は x 昇順の (x, score)。範囲外は端点で頭打ち。"""
    if x is None:
        return None
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _growth(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    if prev < 0:
        # 赤字からの改善は符号付きで扱えないため、改善/悪化のみ表現する
        return 1.0 if cur > prev else -1.0
    return cur / prev - 1


@dataclass
class Features:
    code: str
    as_of: date
    price: float | None = None
    values: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def get(self, key: str) -> Any:
        return self.values.get(key)


def _cutoff(as_of: date) -> str:
    # 大引け後分析なので当日 23:59 までの公表を含める
    return datetime.combine(as_of, time(23, 59, 59), JST).isoformat()


def build_features(
    conn: sqlite3.Connection,
    code: str,
    as_of: date,
    price: float | None,
    indicators: dict[str, Any],
    eps_target: float | None = None,
    target_years: float | None = None,
) -> Features:
    f = Features(code=code, as_of=as_of, price=price)
    cutoff = _cutoff(as_of)
    fy = conn.execute(
        """SELECT * FROM financials WHERE code=? AND period_type='FY' AND disclosed_at<=?
           ORDER BY fiscal_period_end DESC, disclosed_at DESC""",
        (code, cutoff),
    ).fetchall()
    # 同一決算期の訂正は最新公表のみ使う
    seen, fy_rows = set(), []
    for r in fy:
        if r["fiscal_period_end"] in seen:
            continue
        seen.add(r["fiscal_period_end"])
        fy_rows.append(dict(r))
    v = f.values
    v.update(indicators)
    if fy_rows:
        cur = fy_rows[0]
        prev = fy_rows[1] if len(fy_rows) > 1 else None
        f.sources.append(f"{cur['source']}:{cur['fiscal_period_end']}@{cur['disclosed_at']}")
        v["fiscal_period_end"] = cur["fiscal_period_end"]
        v["revenue"] = cur["revenue"]
        v["operating_profit"] = cur["operating_profit"]
        v["eps"] = cur["eps"]
        v["bps"] = cur["bps"]
        if cur["revenue"]:
            v["op_margin"] = (cur["operating_profit"] or 0) / cur["revenue"]
        if cur["equity"]:
            v["roe"] = (cur["net_income"] or 0) / cur["equity"]
        if cur["total_assets"]:
            v["equity_ratio"] = (cur["equity"] or 0) / cur["total_assets"]
            if cur["cash"] is not None and cur["interest_bearing_debt"] is not None:
                v["net_cash_to_assets"] = (cur["cash"] - cur["interest_bearing_debt"]) / cur["total_assets"]
        v["operating_cf"] = cur["operating_cf"]
        v["revenue_growth"] = _growth(cur["revenue"], prev and prev["revenue"])
        v["op_growth"] = _growth(cur["operating_profit"], prev and prev["operating_profit"])
        v["forecast_revenue_growth"] = _growth(cur["forecast_revenue"], cur["revenue"])
        v["forecast_op_growth"] = _growth(cur["forecast_operating_profit"], cur["operating_profit"])
        if prev:
            if prev["interest_bearing_debt"] is not None and cur["interest_bearing_debt"] is not None and cur["total_assets"]:
                v["debt_change_to_assets"] = (cur["interest_bearing_debt"] - prev["interest_bearing_debt"]) / cur["total_assets"]
            if prev["shares_outstanding"] and cur["shares_outstanding"]:
                v["share_change"] = cur["shares_outstanding"] / prev["shares_outstanding"] - 1
        if price and cur["eps"] and cur["eps"] > 0:
            v["per"] = price / cur["eps"]
        if price and cur["bps"] and cur["bps"] > 0:
            v["pbr"] = price / cur["bps"]
        base_eps = cur["forecast_eps"] or cur["eps"]
        if eps_target and base_eps and base_eps > 0 and eps_target > 0 and target_years and target_years > 0.5:
            v["eps_cagr_to_target"] = (eps_target / base_eps) ** (1 / target_years) - 1

    # 四半期進捗率（会社予想に対する営業利益の累計進捗）
    q = conn.execute(
        """SELECT * FROM financials WHERE code=? AND period_type IN ('1Q','2Q','3Q') AND disclosed_at<=?
           ORDER BY disclosed_at DESC LIMIT 1""",
        (code, cutoff),
    ).fetchone()
    if q and q["forecast_operating_profit"] and q["forecast_operating_profit"] > 0 and q["operating_profit"] is not None:
        expected = {"1Q": 0.25, "2Q": 0.5, "3Q": 0.75}[q["period_type"]]
        v["progress_gap"] = q["operating_profit"] / q["forecast_operating_profit"] - expected

    since = datetime.combine(as_of - timedelta(days=120), time(0), JST).isoformat()
    for cat in ("revision_down", "revision_up", "offering", "buyback"):
        v[f"disc_{cat}_120d"] = conn.execute(
            "SELECT COUNT(*) FROM disclosures WHERE code=? AND category=? AND disclosed_at BETWEEN ? AND ?",
            (code, cat, since, cutoff),
        ).fetchone()[0]

    for r in conn.execute("SELECT factor, score FROM qualitative_inputs WHERE code=?", (code,)):
        v[f"q_{r['factor']}"] = r["score"]
    return f
