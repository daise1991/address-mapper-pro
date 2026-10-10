"""売買判定ルール。閾値は settings テーブルで上書きでき、検証で比較できるよう版管理する。

取得単価・含み益は判定に使わない（評価は企業と株価のみで決める）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class RuleSet:
    name: str = "v1"
    aggressive_growth: int = 85
    aggressive_buy: int = 85
    aggressive_sell_max: int = 30
    normal_growth: int = 80
    normal_buy: int = 75
    normal_sell_max: int = 40
    check_buy_min: int = 65
    sell_priority: int = 85
    sell_candidate: int = 75
    sell_partial: int = 65
    sell_review: int = 40
    stop_loss_fundamental: float = 50.0   # ファンダメンタル系売り要因の平均がこれ以上なら撤退側
    stop_loss_growth_floor: int = 60      # 成長期待点がこれ未満なら前提崩れ

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


BUY_LABELS = {
    "aggressive": "積極購入候補",
    "normal": "通常購入候補",
    "check": "購入条件の確認対象",
    "wait_discount": "割安待ち",
    "unrated": "未算定",
    "none": "対象外",
    "above_target": "狙い目価格待ち",
}
SELL_LABELS = {
    "priority": "売却優先検討",
    "candidate": "売却候補",
    "partial": "一部売却検討",
    "review": "保有再評価",
    "hold": "原則保有",
    "unrated": "未算定",
}
EXIT_LABELS = {"profit_taking": "利益確定候補", "stop_loss": "損切り・撤退候補"}


def buy_decision(growth: int | None, buy: int | None, sell: int | None, r: RuleSet = RuleSet(),
                 price: float | None = None, target_price: float | None = None, require_target: bool = False) -> str:
    """require_target=True（v5 の参考計算だけ）のとき、購入候補は株価が狙い目価格以下であることも条件にする。"""
    d = _buy_decision(growth, buy, sell, r)
    if require_target and d in ("aggressive", "normal") and (price is None or target_price is None or price > target_price):
        return "above_target"
    return d


def _buy_decision(growth: int | None, buy: int | None, sell: int | None, r: RuleSet) -> str:
    if buy is None:
        return "unrated"
    if growth is None or sell is None:
        return "unrated"
    if growth >= r.aggressive_growth and buy >= r.aggressive_buy and sell <= r.aggressive_sell_max:
        return "aggressive"
    if growth >= r.normal_growth and buy >= r.normal_buy and sell <= r.normal_sell_max:
        return "normal"
    if growth >= r.normal_growth and sell <= r.normal_sell_max and r.check_buy_min <= buy < r.normal_buy:
        return "check"
    if growth >= r.normal_growth and buy < r.check_buy_min:
        return "wait_discount"
    return "none"


def sell_decision(sell: int | None, r: RuleSet = RuleSet()) -> str:
    if sell is None:
        return "unrated"
    if sell >= r.sell_priority:
        return "priority"
    if sell >= r.sell_candidate:
        return "candidate"
    if sell >= r.sell_partial:
        return "partial"
    if sell >= r.sell_review:
        return "review"
    return "hold"


def exit_type(sell_detail: dict[str, Any], growth: int | None, r: RuleSet = RuleSet()) -> str | None:
    """売り点数が一部売却以上のとき、利益確定か損切り・撤退かを判定する。"""
    sell = sell_detail.get("score")
    if sell is None or sell < r.sell_partial:
        return None
    fund = sell_detail.get("fundamental_side") or 0.0
    factors = sell_detail.get("factors", {})
    critical = any(
        (factors.get(k, {}).get("score") or 0) >= 80
        for k in ("downward_revision", "financial_deterioration", "dilution")
    )
    if critical or fund >= r.stop_loss_fundamental or (growth is not None and growth < r.stop_loss_growth_floor):
        return "stop_loss"
    return "profit_taking"
