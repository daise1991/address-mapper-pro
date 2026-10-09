"""テクニカル指標。入力は古い順の終値リスト（分割調整済みを使うこと）。"""
from __future__ import annotations

from typing import Sequence


def sma(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder の RSI。データ不足なら None。"""
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains += max(diff, 0.0)
        losses += max(-diff, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def compute_all(closes: Sequence[float]) -> dict[str, float | None]:
    last = closes[-1] if closes else None
    out: dict[str, float | None] = {"close": last, "rsi14": rsi(closes, 14)}
    for w in (5, 25, 75, 200):
        out[f"sma{w}"] = sma(closes, w)
    for w in (25, 200):
        m = out[f"sma{w}"]
        out[f"dev{w}"] = (last / m - 1) if (last and m) else None
    return out
