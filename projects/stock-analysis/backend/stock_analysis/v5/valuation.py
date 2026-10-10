"""v5 買い点数の理論価値: 将来株価の現在価値（v4と同じ）＋目標期末までの予想配当の現在価値。

二重計上の防止: 将来株価（2029年EPS×想定PER）は目標期末時点の株価とみなし、
目標期の配当はその株価に含まれるものとして加えない。加えるのは、評価日より後かつ目標期末より前に
権利が確定する期の配当だけ。
配当予想の根拠: 1期目は会社予想配当、2期目以降は「EPSの推移×過去3期の平均配当性向（上限70%）」。
増配は前期比+10%を上限とし、無理な増配を見込まない。配当データが無い場合は加算しない（未算定と記録）。
"""
from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from ..scoring.valuation import K_LEGACY, Assumption, buy_score_from_ratio, evaluate
from .features import V5Features

MAX_DIVIDEND_GROWTH = 0.10
PAYMENT_LAG_DAYS = 90


def _fy_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def dividend_schedule(f: V5Features, a: Assumption, scenario_eps: float, as_of: date) -> dict[str, Any]:
    target_end = _fy_end(a.target_year, a.fiscal_month_end)
    ends = [_fy_end(y, a.fiscal_month_end) for y in range(as_of.year, a.target_year + 1)]
    ends = [e for e in ends if as_of < e < target_end]
    if not f.get("dividend_known"):
        return {"status": "未算定", "reason": "配当の実績・予想データなし", "pv": 0.0, "schedule": []}
    payout = f.get("payout_ratio_avg")
    eps0 = f.get("forecast_eps") or f.get("latest_eps")
    if payout is None and f.get("forecast_dps") is not None and eps0 and eps0 > 0:
        payout = min(f.get("forecast_dps") / eps0, 0.7)
    if payout is None:
        return {"status": "未算定", "reason": "配当性向を算定できない", "pv": 0.0, "schedule": []}
    n = len(ends)
    sched, pv, prev = [], 0.0, None
    for i, end in enumerate(ends):
        # EPS は会社予想から目標期の推計値まで幾何的に推移すると仮定（どちらかが赤字なら直線）
        if eps0 and eps0 > 0 and scenario_eps > 0:
            eps_i = eps0 * (scenario_eps / eps0) ** (i / n)
        else:
            eps_i = (eps0 or 0) + ((scenario_eps - (eps0 or 0)) * i / n)
        if i == 0 and f.get("forecast_dps") is not None:
            dps = max(0.0, f.get("forecast_dps"))
            basis = "会社予想配当"
        else:
            dps = max(0.0, payout * eps_i)
            basis = f"EPS{eps_i:.1f}×配当性向{payout:.0%}"
            if prev is not None:
                dps = min(dps, prev * (1 + MAX_DIVIDEND_GROWTH))
        pay = end + timedelta(days=PAYMENT_LAG_DAYS)
        disc = (1 + a.required_return) ** ((pay - as_of).days / 365.25)
        pv += dps / disc
        sched.append({"fiscal_end": end.isoformat(), "dps": round(dps, 2), "basis": basis, "pv": round(dps / disc, 2)})
        prev = dps
    return {"status": "算定済み", "pv": round(pv, 2), "payout": round(payout, 3), "schedule": sched,
            "note": "目標期の配当は将来株価に含まれるため加算しない"}


def value(f: V5Features, a: Assumption | None, price: float | None, as_of: date) -> dict[str, Any]:
    base = evaluate(a, price, as_of)
    out: dict[str, Any] = {"status": base.status, "reason": base.reason, "method": base.method,
                           "future_price": base.future_price, "years": base.years,
                           "theoretical_value_no_dividend": base.theoretical_value,
                           "buy_score_no_dividend": base.buy_score}
    if base.status != "rated" or a is None or not price:
        out.update({"theoretical_value": None, "buy_score": None, "buy_score_k150": None, "dividend": None})
        return out
    eps = a.eps_forecast or a.normalized_eps or 0.0
    div = dividend_schedule(f, a, eps, as_of)
    v = base.theoretical_value + div["pv"]
    out.update({
        "dividend": div,
        "theoretical_value": round(v, 2),
        "target_price": round(v * (1 - a.safety_margin), 2),
        "v_over_p": round(v / price, 4),
        "buy_score": buy_score_from_ratio(v / price),
        "buy_score_k150": buy_score_from_ratio(v / price, K_LEGACY),
        "dividend_score_change": buy_score_from_ratio(v / price) - (base.buy_score or 0),
    })
    return out


def loss_making(research_item: dict[str, Any] | None, price: float | None) -> dict[str, Any]:
    """赤字企業: PER は適用しない。PSR は参考値に限り、利益率・資金繰り・増資リスクの根拠が揃わなければ未算定。
    PSR の参考値だけで割安と判定しない（正式な買い点数・注文候補には使わない）。"""
    lm = (research_item or {}).get("loss_making_valuation") or {}
    need = ("revenue_2029_per_share", "psr", "op_margin_2029", "cash_runway_years", "dilution_risk")
    missing = [k for k in need if lm.get(k) is None]
    if missing or not price:
        return {"status": "unrated", "reason": "赤字企業: PSR評価の根拠が不足（" + "、".join(missing) + "）", "reference_value": None}
    if lm["cash_runway_years"] < 2 or lm["dilution_risk"] == "high" or lm["op_margin_2029"] <= 0:
        return {"status": "unrated", "reason": "赤字企業: 資金繰り・増資リスク・黒字化の根拠が不十分", "reference_value": None}
    return {"status": "reference", "reason": "赤字企業: PSRによる参考値（正式な買い点数には使わない）",
            "reference_value": round(lm["revenue_2029_per_share"] * lm["psr"], 2)}


__all__ = ["dividend_schedule", "loss_making", "replace", "value"]
