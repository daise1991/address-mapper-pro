"""予測信頼度 A〜D。

配点（合計100）:
  データ充足率 30 / 公式情報との整合性 25 / データ更新日 15 / 予測幅 15 / 過去の予測誤差 15
過去の予測誤差の検証データが2件未満の場合は「検証期間不足」とし、0点にせず配点から除外して
残り85点満点を100点に換算する。
境界: A ≥85、B ≥70、C ≥50、D <50。必須データの欠落・公式データとの重大な矛盾は強制的に D。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .common import scale

BOUNDS = (("A", 85), ("B", 70), ("C", 50))
MIN_ERROR_SAMPLES = 2


def grade(*, coverage: float, has_price: bool, has_company_data: bool, has_scenarios: bool,
          forecast_eps_diff: float | None, history_match: tuple[int, int] | None,
          research_date: str | None, as_of: date, scenario_values: dict[str, float | None] | None,
          forecast_errors: list[float]) -> dict[str, Any]:
    reasons_d = []
    if not has_price:
        reasons_d.append("当日の確定終値なし")
    if not has_company_data:
        reasons_d.append("決算データなし")
    if not has_scenarios:
        reasons_d.append("3シナリオの評価前提なし")
    if forecast_eps_diff is not None and abs(forecast_eps_diff) > 0.20:
        reasons_d.append(f"会社予想EPSが公式データと{forecast_eps_diff:+.0%}乖離")

    parts: dict[str, Any] = {}
    parts["coverage"] = round(scale(coverage, [(0.5, 0), (1.0, 30)]), 1)
    if forecast_eps_diff is None and history_match is None:
        consistency = 0.0
    else:
        eps_pts = 15.0 if forecast_eps_diff is None else scale(abs(forecast_eps_diff), [(0.05, 15), (0.10, 10), (0.20, 5)])
        hist_pts = 10.0 * (history_match[0] / history_match[1]) if history_match and history_match[1] else 5.0
        consistency = eps_pts + hist_pts
    parts["consistency"] = round(consistency, 1)
    if research_date:
        age = (as_of - date.fromisoformat(research_date[:10])).days
        parts["freshness"] = round(scale(age, [(90, 15), (365, 0)]), 1)
    else:
        parts["freshness"] = 0.0
    if scenario_values and all(scenario_values.get(k) for k in ("bear", "base", "bull")):
        width = (scenario_values["bull"] - scenario_values["bear"]) / scenario_values["base"]
        parts["range"] = round(scale(width, [(0.6, 15), (1.0, 10), (1.6, 5), (2.5, 0)]), 1)
        parts["range_width"] = round(width, 2)
    else:
        parts["range"] = 0.0
    if len(forecast_errors) >= MIN_ERROR_SAMPLES:
        err = sum(forecast_errors) / len(forecast_errors)
        parts["forecast_error"] = round(scale(err, [(0.05, 15), (0.10, 10), (0.20, 5), (0.40, 0)]), 1)
        parts["forecast_error_mean"] = round(err, 3)
        max_total = 100.0
    else:
        parts["forecast_error"] = "検証期間不足"
        max_total = 85.0
    total = sum(v for k, v in parts.items() if k in ("coverage", "consistency", "freshness", "range", "forecast_error")
                and isinstance(v, (int, float)))
    points = round(total * 100 / max_total, 1)
    g = "D"
    for name, th in BOUNDS:
        if points >= th:
            g = name
            break
    if reasons_d:
        g = "D"
    return {"grade": g, "points": points, "parts": parts, "forced_d": reasons_d,
            "insufficient_history": max_total < 100}
