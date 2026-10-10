"""評価前提（予想EPS・想定PER・必要利回り・安全余裕・手法）の管理。

利用者が登録した前提（origin=manual）を最優先する。未登録の銘柄には
会社予想から機械的に作る「自動初期推計」（origin=auto）を用意し、理由を明記する。
赤字・新興赤字・ETF は自動推計せず未算定とする。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import asdict, fields
from datetime import date
from typing import Any

from .db import now_iso, upsert
from .scoring.valuation import CATEGORY_DEFAULTS, Assumption

# 自動初期推計の前提（検証用）。分類ごとの想定PERと、会社予想EPSから目標年度までの年成長率。
AUTO_PER = {"stable": 15.0, "growth": 25.0, "cyclical": 14.0}
AUTO_GROWTH = {"stable": 0.05, "growth": 0.15, "cyclical": 0.05}
DEFAULT_TARGET_YEAR = 2029


def load(conn: sqlite3.Connection, code: str) -> tuple[Assumption | None, str | None]:
    r = conn.execute("SELECT * FROM valuation_assumptions WHERE code=?", (code,)).fetchone()
    if not r:
        return None, None
    names = {f.name for f in fields(Assumption)}
    return Assumption(**{k: r[k] for k in r.keys() if k in names}), r["origin"]


def save(conn: sqlite3.Connection, code: str, a: Assumption, origin: str = "manual") -> None:
    if not a.reason or not a.reason.strip():
        raise ValueError("前提を変更する理由（reason）は必須です")
    row = {"code": code, **asdict(a), "origin": origin, "updated_at": now_iso()}
    upsert(conn, "valuation_assumptions", row, ["code"])
    conn.execute("INSERT INTO valuation_assumption_history (code, payload, changed_at) VALUES (?,?,?)",
                 (code, json.dumps(row, ensure_ascii=False), row["updated_at"]))
    conn.commit()


def auto_baseline(conn: sqlite3.Connection, code: str, as_of: date) -> Assumption | None:
    sec = conn.execute("SELECT category, asset_type FROM securities WHERE code=?", (code,)).fetchone()
    if not sec or sec["asset_type"] != "stock" or sec["category"] not in AUTO_PER:
        return None
    fy = conn.execute(
        """SELECT * FROM financials WHERE code=? AND period_type='FY' AND disclosed_at<=?
           ORDER BY fiscal_period_end DESC, disclosed_at DESC LIMIT 1""",
        (code, f"{as_of.isoformat()}T23:59:59+09:00"),
    ).fetchone()
    if not fy:
        return None
    from .scoring.features import latest_forecast
    lf = latest_forecast(conn, code, fy["fiscal_period_end"], f"{as_of.isoformat()}T23:59:59+09:00")
    base_eps = (lf and lf["forecast_eps"]) or fy["forecast_eps"] or fy["eps"]
    if base_eps is None or base_eps <= 0:
        return None
    cat = sec["category"]
    fy_end = date.fromisoformat(fy["fiscal_period_end"])
    d = CATEGORY_DEFAULTS[cat]
    common = dict(target_year=DEFAULT_TARGET_YEAR, fiscal_month_end=fy_end.month,
                  required_return=d["required_return"], safety_margin=d["safety_margin"], assumed_per=AUTO_PER[cat])
    tail = (f"{d['label']}の初期仮定（必要利回り{d['required_return']:.1%}・安全余裕{d['safety_margin']:.0%}）。要個別見直し")

    if cat == "cyclical":
        # 景気循環株は直近の利益水準ではなく、過去最大5期の純利益と今期予想の中央値を「正常化利益」とする。
        # 株式分割の影響を受けないよう、EPS ではなく純利益を現在の株式数で割る
        hist = conn.execute(
            """SELECT fiscal_period_end, MAX(disclosed_at), net_income, shares_outstanding, treasury_shares
               FROM financials WHERE code=? AND period_type='FY' AND disclosed_at<=? AND net_income IS NOT NULL
               GROUP BY fiscal_period_end ORDER BY fiscal_period_end DESC LIMIT 5""",
            (code, f"{as_of.isoformat()}T23:59:59+09:00")).fetchall()
        shares = (fy["shares_outstanding"] or 0) - (fy["treasury_shares"] or 0)
        profits = [h["net_income"] for h in hist]
        if lf and lf["forecast_net_income"] is not None:
            profits.append(lf["forecast_net_income"])
        if shares <= 0 or len(profits) < 2:
            return None
        normalized = statistics.median(profits) / shares
        if normalized <= 0:
            return None
        return Assumption(method="normalized", normalized_eps=normalized, **common,
                          reason=(f"自動初期推計: 正常化EPS {normalized:.1f}円（過去{len(hist)}期の純利益と今期予想の中央値÷現在の株式数）、"
                                  f"想定PER {AUTO_PER[cat]:.0f}倍、" + tail))

    base_year = fy_end.year + 1  # 予想は翌期
    n = max(DEFAULT_TARGET_YEAR - base_year, 0)
    g = AUTO_GROWTH[cat]
    eps_target = base_eps * (1 + g) ** n
    return Assumption(method="per", eps_forecast=eps_target, **common,
                      reason=f"自動初期推計: 会社予想EPS {base_eps:.1f}円×(1+{g:.0%})^{n}、想定PER {AUTO_PER[cat]:.0f}倍、" + tail)


def resolve(conn: sqlite3.Connection, code: str, as_of: date) -> tuple[Assumption | None, str]:
    a, origin = load(conn, code)
    if a is not None:
        return a, origin or "manual"
    return auto_baseline(conn, code, as_of), "auto"


def to_dict(a: Assumption | None) -> dict[str, Any] | None:
    return asdict(a) if a else None
