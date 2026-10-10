"""v5 の想定PER（方針B）: J-Quants の株価と本決算EPSから求めた過去5年の実績PERの中央値を使う。

- 各月末の株価（調整後終値）÷ その時点で公表済みの直近本決算EPS（分割補正で現在の基準にそろえる）
- 赤字（EPS≦0）の月は除外し、除外した月数を記録する
- 標準シナリオ = 中央値、弱気 = 25パーセンタイル、強気 = 75パーセンタイル
- 5年間で利益構造・事業内容が大きく変わった企業は単純な中央値が不適切な場合があるため、
  異常の目印（anomalies）を付け、正式評価にはせず要確認とする
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import date
from typing import Any

from ..scoring.features import split_ratio
from .features import cutoff_of

YEARS = 5
MIN_MONTHS = 36           # 3年分以上の月がなければ「データ不足」
PER_LOW, PER_HIGH = 5.0, 60.0
MAX_SPREAD = 2.0          # 75パーセンタイル÷25パーセンタイル
MAX_RESEARCH_GAP = 0.5    # 調査PERとの乖離（±50%）
MAX_MARGIN_SHIFT = 2.0    # 営業利益率が5年前の2倍超・半分未満
MAX_REVENUE_CAGR = 0.25


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def monthly_pers(conn: sqlite3.Connection, code: str, as_of: date) -> dict[str, Any]:
    since = date(as_of.year - YEARS, as_of.month, min(as_of.day, 28)).isoformat()
    months = conn.execute(
        """SELECT q.date, COALESCE(q.adj_close, q.close) AS c FROM daily_quotes q
           JOIN (SELECT MAX(date) AS d FROM daily_quotes WHERE code=? AND date>? AND date<=? AND close IS NOT NULL
                 GROUP BY substr(date,1,7)) m ON m.d=q.date
           WHERE q.code=? ORDER BY q.date""", (code, since, as_of.isoformat(), code)).fetchall()
    fys = conn.execute(
        """SELECT disclosed_at, eps FROM financials WHERE code=? AND period_type='FY' AND eps IS NOT NULL
           AND disclosed_at<=? ORDER BY disclosed_at""", (code, cutoff_of(as_of))).fetchall()
    # EPS を現在の株数基準にそろえる（調整後終値と同じ基準）
    eps_list = [(r["disclosed_at"][:10], r["eps"] / split_ratio(conn, code, r["disclosed_at"], as_of.isoformat())) for r in fys]
    pers, loss = [], 0
    for m in months:
        known = [e for d0, e in eps_list if d0 <= m["date"]]
        if not known or not m["c"]:
            continue
        if known[-1] <= 0:
            loss += 1
            continue
        pers.append(m["c"] / known[-1])
    return {"pers": pers, "months": len(months), "loss_months": loss}


def per_basis(conn: sqlite3.Connection, code: str, as_of: date, f_values: dict[str, Any],
              research_base_per: float | None) -> dict[str, Any]:
    h = monthly_pers(conn, code, as_of)
    pers = h["pers"]
    out: dict[str, Any] = {"policy": "B: 過去5年PER中央値", "valid_months": len(pers), "loss_months": h["loss_months"],
                           "median": None, "p25": None, "p75": None, "anomalies": [], "applied": False}
    if len(pers) < MIN_MONTHS:
        out["anomalies"].append(f"PERデータ不足（有効{len(pers)}か月／必要{MIN_MONTHS}か月）")
        return out
    med, p25, p75 = statistics.median(pers), _quantile(pers, 0.25), _quantile(pers, 0.75)
    out.update({"median": round(med, 1), "p25": round(p25, 1), "p75": round(p75, 1), "applied": True})
    a = out["anomalies"]
    if h["loss_months"]:
        a.append(f"赤字の期間あり（{h['loss_months']}か月を除外）")
    if med < PER_LOW or med > PER_HIGH:
        a.append(f"中央値が極端（{med:.1f}倍）")
    if p25 > 0 and p75 / p25 > MAX_SPREAD:
        a.append(f"PERのばらつきが大きい（{p25:.1f}〜{p75:.1f}倍）")
    first_m, last_m = f_values.get("op_margin_first"), f_values.get("op_margin_latest")
    if first_m is not None and last_m is not None:
        if (first_m <= 0) != (last_m <= 0):
            a.append("利益構造の変化（営業赤字⇔黒字の転換）")
        elif first_m > 0 and not (1 / MAX_MARGIN_SHIFT <= last_m / first_m <= MAX_MARGIN_SHIFT):
            a.append(f"利益構造の変化（営業利益率 {first_m:.1%}→{last_m:.1%}）")
    if (f_values.get("revenue_cagr_hist") or 0) > MAX_REVENUE_CAGR:
        a.append(f"事業規模の急変（売上の年平均成長率 {f_values['revenue_cagr_hist']:.0%}）")
    if research_base_per and abs(med / research_base_per - 1) > MAX_RESEARCH_GAP:
        a.append(f"調査の標準PER {research_base_per:.1f}倍と大きく乖離")
    return out


SCENARIO_KEY = {"bear": "p25", "base": "median", "bull": "p75"}
