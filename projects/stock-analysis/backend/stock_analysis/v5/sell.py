"""v5 売り点数（7項目・配点は v4 と同じ）。

- 重大リスクで点数を引き上げない（重大リスクは risk.py で別に警告）
- RSI 単独では株価過熱を最大40点に抑え、移動平均乖離と平均する
"""
from __future__ import annotations

from typing import Any

from .common import Item, scale, summarize
from .features import V5Features

OVER_PTS = [(0.8, 0), (1.0, 25), (1.2, 60), (1.5, 90), (2.0, 100)]
SLOW_PTS = [(-0.30, 100), (-0.10, 75), (0.0, 55), (0.05, 35), (0.15, 10), (0.30, 0)]
PROGRESS_PTS = [(-0.25, 90), (-0.10, 55), (0.0, 20), (0.10, 0)]
EQUITY_RISK_PTS = [(0.1, 80), (0.2, 50), (0.35, 15), (0.5, 0)]
SHARE_PTS = [(0.0, 0), (0.03, 50), (0.10, 100)]
RSI_PTS = [(60, 0), (70, 40), (80, 80), (90, 100)]
DEV25_PTS = [(0.05, 0), (0.15, 50), (0.30, 100)]
DEV200_PTS = [(0.3, 0), (0.6, 60), (1.0, 100)]
DROP_PTS = [(0, 0), (5, 30), (10, 60), (20, 100)]
RSI_ONLY_CAP = 40.0


def _max(*xs: float | None) -> float | None:
    v = [x for x in xs if x is not None]
    return max(v) if v else None


def score(f: V5Features, v_over_p: float | None, growth_drop: float | None, q_outlook: float | None) -> dict[str, Any]:
    g = f.get
    over = scale(1 / v_over_p, OVER_PTS) if v_over_p else None

    slow = scale(g("forecast_op_growth"), SLOW_PTS)
    if slow is not None and g("op_cagr_hist") is not None and g("forecast_op_growth") < g("op_cagr_hist") - 0.15:
        slow = min(100.0, slow + 15)

    has_fin = g("history_years") and g("history_years") > 0
    rev_count = (min(100.0, 40.0 * g("disc_revision_down_120d"))
                 if has_fin and g("disc_revision_down_120d") is not None else None)
    if rev_count is not None and g("disc_revision_up_120d"):
        rev_count = max(0.0, rev_count - 20)

    fin = _max(None if g("operating_cf") is None else (70.0 if g("operating_cf") < 0 else 0.0),
               scale(g("equity_ratio"), EQUITY_RISK_PTS))

    dil_shares = scale(g("share_change"), SHARE_PTS)
    if g("share_change_excluded_as_split") is not None:
        dil_shares = 0.0  # 株式分割による増加は希薄化ではない
    offering = None if g("disc_offering_120d") is None else (70.0 if g("disc_offering_120d") else 0.0)

    rsi = scale(g("rsi14"), RSI_PTS)
    dev = _max(scale(g("dev25"), DEV25_PTS), scale(g("dev200"), DEV200_PTS))
    if rsi is not None and dev is not None:
        heat = (rsi + dev) / 2
    elif rsi is not None:
        heat = min(rsi, RSI_ONLY_CAP)  # RSI だけでは中長期の売りを判断しない
    else:
        heat = dev

    q = None if q_outlook is None else max(0.0, 60 - q_outlook) * (100 / 60)

    items = [
        Item("overvaluation", "割高度", 25, True, [("株価÷標準理論価値", 1.0, over)]),
        Item("slowdown", "業績鈍化", 15, True, [("予想営業利益の伸び（過去比の減速補正）", 1.0, slow)]),
        Item("downward_revision", "下方修正リスク", 15, True, [
            ("120日以内の下方修正", 0.6, rev_count),
            ("四半期進捗の遅れ", 0.4, scale(g("progress_gap"), PROGRESS_PTS))]),
        Item("financial_deterioration", "財務悪化", 15, True, [("営業CF・自己資本比率", 1.0, fin)]),
        Item("dilution", "増資・希薄化", 10, False, [
            ("株数の増加（分割補正後）", 0.6, dil_shares), ("増資の開示", 0.4, offering)]),
        Item("overheating", "株価過熱", 10, False, [("RSIと移動平均乖離", 1.0, heat)]),
        Item("outlook_deterioration", "見通し悪化", 10, False, [
            ("成長期待点の90日前からの低下", 0.6, scale(growth_drop, DROP_PTS)), ("成長見通し（定性）", 0.4, q)]),
    ]
    return summarize(items)
