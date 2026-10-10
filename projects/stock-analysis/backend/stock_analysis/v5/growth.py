"""v5 成長期待点（7項目・配点は v4 と同じ）。変換表は docs/scoring-v5-plan.md の付表どおり。"""
from __future__ import annotations

from typing import Any

from .common import Item, scale, summarize
from .features import V5Features

REV_PTS = [(-0.10, 0), (0.0, 30), (0.05, 50), (0.10, 70), (0.15, 85), (0.25, 100)]
OP_PTS = [(-0.20, 0), (0.0, 30), (0.05, 45), (0.10, 65), (0.20, 85), (0.30, 100)]
OP_TREND_PTS = {"turnaround": 70.0, "loss_improving": 40.0, "worsening": 10.0}
MARGIN_PTS = [(0.0, 20), (0.05, 45), (0.10, 65), (0.20, 85), (0.30, 100)]
ROE_PTS = [(0.0, 20), (0.08, 55), (0.15, 80), (0.25, 100)]
EQUITY_PTS = [(0.1, 10), (0.3, 50), (0.5, 80), (0.7, 100)]
NETCASH_PTS = [(-0.4, 0), (0.0, 60), (0.2, 90), (0.4, 100)]
EPS2029_PTS = [(-0.10, 0), (0.0, 30), (0.08, 55), (0.15, 75), (0.25, 95)]


def score(f: V5Features, qualitative: dict[str, float | None], eps_2029: float | None, years_to_target: float | None) -> dict[str, Any]:
    g = f.get
    op_hist = scale(g("op_cagr_hist"), OP_PTS)
    if op_hist is None and g("op_trend_hist"):
        op_hist = OP_TREND_PTS[g("op_trend_hist")]
    eps_cagr = None
    base = g("latest_eps")
    if eps_2029 and base and base > 0 and eps_2029 > 0 and years_to_target and years_to_target > 0.5:
        eps_cagr = (eps_2029 / base) ** (1 / years_to_target) - 1
    f.values["eps_cagr_to_2029"] = eps_cagr
    items = [
        Item("revenue_growth", "売上成長", 15, True, [
            ("過去3〜5年の年平均成長率", 0.4, scale(g("revenue_cagr_hist"), REV_PTS)),
            ("会社予想の伸び", 0.6, scale(g("forecast_revenue_growth"), REV_PTS))]),
        Item("op_growth", "営業利益成長", 15, True, [
            ("過去3〜5年の年平均成長率", 0.4, op_hist),
            ("会社予想の伸び", 0.6, scale(g("forecast_op_growth"), OP_PTS))]),
        Item("market_expansion", "市場拡大", 15, False, [("定性評価（根拠付き）", 1.0, qualitative.get("market_expansion"))]),
        Item("competitive_advantage", "競争優位性", 15, False, [("定性評価（根拠付き）", 1.0, qualitative.get("competitive_advantage"))]),
        Item("profitability", "収益性", 15, True, [
            ("営業利益率の過去平均", 0.5, scale(g("op_margin_avg"), MARGIN_PTS)),
            ("ROEの過去平均", 0.5, scale(g("roe_avg"), ROE_PTS))]),
        Item("financial_safety", "財務安全性", 10, True, [
            ("自己資本比率", 0.4, scale(g("equity_ratio"), EQUITY_PTS)),
            ("ネットキャッシュ÷総資産", 0.4, scale(g("net_cash_to_assets"), NETCASH_PTS)),
            ("営業CF黒字年数の割合", 0.2, None if g("opcf_positive_ratio") is None else 100 * g("opcf_positive_ratio"))]),
        Item("growth_2029", "2029年の成長可能性", 15, False, [
            ("標準シナリオの2029年EPSの年率", 0.6, scale(eps_cagr, EPS2029_PTS)),
            ("成長見通し（定性）", 0.4, qualitative.get("growth_outlook"))]),
    ]
    return summarize(items)
