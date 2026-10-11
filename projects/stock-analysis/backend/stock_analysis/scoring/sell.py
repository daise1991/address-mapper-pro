"""C. 売り点数（従来方式・100点満点）。

買い点数の単純な逆数にはしない。割高度は観点の一つにすぎず、
業績・財務・希薄化・過熱・見通し悪化を独立に評価する。
観点を「バリュエーション系」と「ファンダメンタル系」に分け、
利益確定（成長は健全だが割高）と損切り・撤退（前提崩れ）を区別する。
"""
from __future__ import annotations

from typing import Any

from .features import Features, scale
from .valuation import round_half_up

WEIGHTS = {
    "overvaluation": 25,
    "slowdown": 15,
    "downward_revision": 15,
    "financial_deterioration": 15,
    "dilution": 10,
    "overheating": 10,
    "outlook_deterioration": 10,
}
LABELS = {
    "overvaluation": "株価の割高度",
    "slowdown": "業績鈍化",
    "downward_revision": "下方修正リスク",
    "financial_deterioration": "財務悪化",
    "dilution": "増資・希薄化",
    "overheating": "株価過熱",
    "outlook_deterioration": "成長見通しの悪化",
}
VALUATION_SIDE = {"overvaluation", "overheating"}
FUNDAMENTAL_SIDE = set(WEIGHTS) - VALUATION_SIDE
CRITICAL = {"downward_revision", "dilution"}
MIN_COVERAGE = 0.5


def _max(*xs: float | None) -> float | None:
    xs2 = [x for x in xs if x is not None]
    return max(xs2) if xs2 else None


def factor_scores(f: Features, v_over_p: float | None, growth_drop: float | None) -> dict[str, float | None]:
    g = f.get
    p_over_v = (1 / v_over_p) if v_over_p else None
    over = scale(p_over_v, [(0.8, 0), (1.0, 25), (1.2, 60), (1.5, 90), (2.0, 100)])

    slow = None
    fg, pg = g("forecast_op_growth"), g("op_growth")
    if fg is not None:
        slow = scale(fg, [(-0.3, 100), (-0.1, 75), (0.0, 55), (0.05, 35), (0.15, 10), (0.3, 0)])
        if pg is not None and fg < pg - 0.15:  # 成長率の大幅な減速
            slow = min(100.0, slow + 15)

    rev = None
    if g("disc_revision_down_120d") is not None or g("progress_gap") is not None:
        rev = min(100.0, 40.0 * (g("disc_revision_down_120d") or 0))
        pg2 = scale(g("progress_gap"), [(-0.25, 90), (-0.1, 55), (0.0, 20), (0.1, 0)])
        rev = _max(rev, pg2)
        if g("disc_revision_up_120d"):
            rev = max(0.0, rev - 20)

    fin = _max(
        None if g("operating_cf") is None else (70.0 if g("operating_cf") < 0 else 0.0),
        scale(g("equity_ratio"), [(0.1, 80), (0.2, 50), (0.35, 15), (0.5, 0)]),
        # 借入増は成長投資の場合もあるため単独では強く効かせない
        scale(g("debt_change_to_assets"), [(0.0, 0), (0.05, 25), (0.15, 60)]),
    )

    dil = None
    if g("share_change") is not None or g("disc_offering_120d") is not None:
        dil = _max(
            scale(g("share_change"), [(0.0, 0), (0.03, 50), (0.10, 100)]),
            70.0 if g("disc_offering_120d") else 0.0,
        )

    heat = _max(
        scale(g("rsi14"), [(60, 0), (70, 40), (80, 80), (90, 100)]),
        scale(g("dev25"), [(0.05, 0), (0.15, 50), (0.30, 100)]),
        scale(g("dev200"), [(0.3, 0), (0.6, 60), (1.0, 100)]),
    )

    outlook = _max(
        scale(growth_drop, [(0, 0), (5, 30), (10, 60), (20, 100)]),
        None if g("q_growth_outlook") is None else max(0.0, 60 - g("q_growth_outlook")) * (100 / 60),
    )
    return {
        "overvaluation": over,
        "slowdown": slow,
        "downward_revision": rev,
        "financial_deterioration": fin,
        "dilution": dil,
        "overheating": heat,
        "outlook_deterioration": outlook,
    }


def _wavg(factors: dict[str, float | None], keys: set[str], W: dict[str, float]) -> float | None:
    items = [(W[k], v) for k, v in factors.items() if k in keys and v is not None and W[k] > 0]
    if not items:
        return None
    return sum(w * v for w, v in items) / sum(w for w, _ in items)


def sell_score(f: Features, v_over_p: float | None, growth_drop: float | None = None,
               weights: dict[str, float] | None = None) -> dict[str, Any]:
    """weights で観点ごとの重みを上書きできる（settings の sell_weights。0 でその観点を使わない）。"""
    W = {**WEIGHTS, **(weights or {})}
    factors = factor_scores(f, v_over_p, growth_drop)
    avail = {k: v for k, v in factors.items() if v is not None and W[k] > 0}
    coverage = sum(W[k] for k in avail) / (sum(W.values()) or 1)
    score = None
    # 下方修正・増資などのイベント要因が強い場合は平均で薄めない
    crit = max((v for k, v in avail.items() if k in CRITICAL), default=0.0)
    if coverage >= MIN_COVERAGE:
        base = sum(W[k] * v for k, v in avail.items()) / sum(W[k] for k in avail)
        score = round_half_up(max(base, crit * 0.8) if crit >= 80 else base)
    elif crit >= 80:
        # 項目が半分そろわなくても、重大なイベント要因が出ていれば見逃さない（その 0.8 倍だけを暫定の点数にする）
        score = round_half_up(crit * 0.8)
    return {
        "score": score,
        "floor_only": coverage < MIN_COVERAGE and score is not None,
        "coverage": round(coverage, 2),
        "valuation_side": None if (x := _wavg(factors, VALUATION_SIDE, W)) is None else round(x, 1),
        "fundamental_side": None if (y := _wavg(factors, FUNDAMENTAL_SIDE, W)) is None else round(y, 1),
        "factors": {k: {"label": LABELS[k], "score": None if v is None else round(v, 1), "weight": W[k]}
                    for k, v in factors.items()},
    }
