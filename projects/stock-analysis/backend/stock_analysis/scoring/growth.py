"""A. 成長期待点（従来方式・100点満点）。

7観点をそれぞれ0〜100点で評価し、重み付き平均で100点満点に合成する。
定性観点（市場拡大・競争優位）は qualitative_inputs に根拠付きで登録した値を使う。
データのない観点は除外して再正規化し、カバー率が50%未満なら未算定とする。
"""
from __future__ import annotations

from typing import Any

from .features import Features, scale
from .valuation import round_half_up

WEIGHTS = {
    "revenue_growth": 15,
    "op_growth": 15,
    "market_expansion": 15,
    "competitive_advantage": 15,
    "profitability": 15,
    "financial_safety": 10,
    "growth_2029": 15,
}
LABELS = {
    "revenue_growth": "将来の売上成長",
    "op_growth": "営業利益成長",
    "market_expansion": "市場規模の拡大",
    "competitive_advantage": "競争優位性",
    "profitability": "事業の収益性",
    "financial_safety": "財務の安全性",
    "growth_2029": "2028〜2029年の成長可能性",
}
MIN_COVERAGE = 0.5

_GROWTH_PTS = [(-0.2, 0), (0.0, 30), (0.05, 50), (0.10, 65), (0.20, 85), (0.35, 100)]


def _mean(xs: list[float | None]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def factor_scores(f: Features) -> dict[str, float | None]:
    g = f.get
    rev = _mean([scale(g("forecast_revenue_growth"), _GROWTH_PTS), scale(g("revenue_growth"), _GROWTH_PTS)])
    op = _mean([scale(g("forecast_op_growth"), _GROWTH_PTS), scale(g("op_growth"), _GROWTH_PTS)])
    profit = _mean([
        scale(g("op_margin"), [(-0.1, 0), (0.0, 25), (0.05, 45), (0.10, 65), (0.20, 85), (0.30, 100)]),
        scale(g("roe"), [(-0.1, 0), (0.0, 25), (0.08, 55), (0.15, 80), (0.25, 100)]),
    ])
    safety = _mean([
        scale(g("equity_ratio"), [(0.1, 10), (0.3, 50), (0.5, 80), (0.7, 100)]),
        scale(g("net_cash_to_assets"), [(-0.4, 0), (-0.1, 40), (0.0, 60), (0.2, 90), (0.4, 100)]),
        None if g("operating_cf") is None else (80.0 if g("operating_cf") > 0 else 15.0),
    ])
    g2029 = _mean([
        scale(g("eps_cagr_to_target"), [(-0.1, 0), (0.0, 30), (0.08, 55), (0.15, 75), (0.25, 90), (0.40, 100)]),
        g("q_growth_outlook"),
    ])
    return {
        "revenue_growth": rev,
        "op_growth": op,
        "market_expansion": g("q_market_expansion"),
        "competitive_advantage": g("q_competitive_advantage"),
        "profitability": profit,
        "financial_safety": safety,
        "growth_2029": g2029,
    }


def growth_score(f: Features) -> dict[str, Any]:
    factors = factor_scores(f)
    total_w = sum(WEIGHTS.values())
    avail = {k: v for k, v in factors.items() if v is not None}
    coverage = sum(WEIGHTS[k] for k in avail) / total_w
    if coverage < MIN_COVERAGE:
        score = None
    else:
        score = round_half_up(sum(WEIGHTS[k] * v for k, v in avail.items()) / sum(WEIGHTS[k] for k in avail))
    return {
        "score": score,
        "coverage": round(coverage, 2),
        "factors": {k: {"label": LABELS[k], "score": None if v is None else round(v, 1), "weight": WEIGHTS[k]}
                    for k, v in factors.items()},
    }
