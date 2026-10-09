"""将来価値方式の買い点数。

    将来株価 F = 予想EPS × 想定PER                 （method=per）
    現在理論価値 V = F ÷ (1 + 必要利回り)^年数
    買い点数 = max(0, min(100, 50 + k × (V/P − 1)))   k=200（本番）, k=150（比較用）
    狙い目価格 = V × (1 − 安全余裕)

予測できない／手法が適用できない場合は status="unrated"（未算定）を返し、0点とは区別する。
"""
from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# 分類ごとの初期仮定（検証用）。個別企業ごとに valuation_assumptions で上書きし、理由を保存する。
CATEGORY_DEFAULTS: dict[str, dict[str, Any]] = {
    "stable": {"required_return": 0.09, "range": (0.08, 0.10), "safety_margin": 0.10, "label": "安定黒字企業"},
    "growth": {"required_return": 0.11, "range": (0.10, 0.12), "safety_margin": 0.15, "label": "高成長黒字企業"},
    "cyclical": {"required_return": 0.135, "range": (0.12, 0.15), "safety_margin": 0.20, "label": "景気循環企業"},
    "emerging_loss": {"required_return": 0.15, "range": (0.15, None), "safety_margin": 0.25, "label": "新興赤字企業"},
}

K_PRIMARY = 200
K_LEGACY = 150


def round_half_up(x: float) -> int:
    """小数点以下四捨五入（Python の round は偶数丸めなので使わない）。

    浮動小数の誤差（50.4999999…）で境界が崩れないよう、先に小数9桁へ丸める。
    """
    return int(Decimal(str(round(x, 9))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def buy_score_from_ratio(v_over_p: float, k: float = K_PRIMARY) -> int:
    return round_half_up(max(0.0, min(100.0, 50 + k * (v_over_p - 1))))


@dataclass
class Assumption:
    method: str                         # per / dcf / normalized / asset / unrated
    target_year: int
    required_return: float
    safety_margin: float
    reason: str
    eps_forecast: float | None = None
    assumed_per: float | None = None
    fcf_forecast: float | None = None   # 1株あたりFCF（目標年度）
    terminal_growth: float | None = None
    net_cash_per_share: float | None = None
    normalized_eps: float | None = None
    bps_forecast: float | None = None
    target_pbr: float | None = None
    fiscal_month_end: int = 3           # 決算月（例: メルカリ6月、GNI 12月）


@dataclass
class Valuation:
    status: str                         # rated / unrated
    method: str
    reason: str
    future_price: float | None = None
    years: float | None = None
    theoretical_value: float | None = None
    target_price: float | None = None
    v_over_p: float | None = None
    buy_score: int | None = None
    buy_score_k150: int | None = None
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def years_until(target_year: int, as_of: date, fiscal_month_end: int = 3) -> float:
    """目標決算期末（例: 2029年3月期 → 2029-03-31 を target_year=2029 で表現）までの年数。

    将来株価は「目標年度の業績が見えた時点」の株価とみなし、決算期末までの年数で割り引く。
    """
    end = date(target_year, fiscal_month_end, calendar.monthrange(target_year, fiscal_month_end)[1])
    return max((end - as_of).days / 365.25, 0.0)


def _unrated(method: str, reason: str, **inputs: Any) -> Valuation:
    return Valuation(status="unrated", method=method, reason=reason, inputs=inputs)


def future_price(a: Assumption) -> tuple[float | None, str]:
    if a.method == "per":
        if a.eps_forecast is None or a.assumed_per is None:
            return None, "予想EPSまたは想定PERが未設定"
        if a.eps_forecast <= 0:
            return None, "予想EPSが赤字のためPERを適用しない"
        return a.eps_forecast * a.assumed_per, "予想EPS×想定PER"
    if a.method == "normalized":
        if a.normalized_eps is None or a.assumed_per is None:
            return None, "正常化EPSまたは想定PERが未設定"
        if a.normalized_eps <= 0:
            return None, "正常化EPSが赤字"
        return a.normalized_eps * a.assumed_per, "景気循環を均した正常化EPS×想定PER"
    if a.method == "dcf":
        if a.fcf_forecast is None or a.terminal_growth is None:
            return None, "FCF予想または永久成長率が未設定"
        if a.fcf_forecast <= 0 or a.terminal_growth >= a.required_return:
            return None, "FCFが赤字、または永久成長率≥必要利回り"
        # 目標年度時点の継続価値（ゴードン成長モデル）＋ネットキャッシュ
        tv = a.fcf_forecast * (1 + a.terminal_growth) / (a.required_return - a.terminal_growth)
        return tv + (a.net_cash_per_share or 0.0), "DCF（目標年度FCFの永久成長＋ネットキャッシュ）"
    if a.method == "asset":
        if a.bps_forecast is None or a.target_pbr is None:
            return None, "予想BPSまたは目標PBRが未設定"
        if a.bps_forecast <= 0:
            return None, "債務超過見込み"
        return a.bps_forecast * a.target_pbr, "予想BPS×目標PBR（資産価値）"
    return None, "評価手法が未設定（未算定）"


def evaluate(a: Assumption | None, price: float | None, as_of: date) -> Valuation:
    if a is None:
        return _unrated("unrated", "評価前提が未登録")
    if price is None or price <= 0:
        return _unrated(a.method, "確定株価が取得不可")
    fp, how = future_price(a)
    if fp is None:
        return _unrated(a.method, how)
    years = years_until(a.target_year, as_of, a.fiscal_month_end)
    v = fp / (1 + a.required_return) ** years
    ratio = v / price
    return Valuation(
        status="rated",
        method=a.method,
        reason=how,
        future_price=round(fp, 2),
        years=round(years, 3),
        theoretical_value=round(v, 2),
        target_price=round(v * (1 - a.safety_margin), 2),
        v_over_p=round(ratio, 4),
        buy_score=buy_score_from_ratio(ratio, K_PRIMARY),
        buy_score_k150=buy_score_from_ratio(ratio, K_LEGACY),
        inputs={**asdict(a), "price": price},
    )
