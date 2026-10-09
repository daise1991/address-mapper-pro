"""データ取得プロバイダ共通インターフェース。

各メソッドは取得できなかった場合に例外 DataUnavailable を送出する。
呼び出し側は fetch_log に「取得不可」を記録し、古いデータを最新として扱わない。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any


class DataUnavailable(Exception):
    """取得不可（未公表・未提供・通信失敗・プラン外）。"""


class Provider(ABC):
    name: str = "base"

    @abstractmethod
    def trading_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        """[{date, is_business_day}]"""

    @abstractmethod
    def listed_info(self, codes: list[str]) -> list[dict[str, Any]]:
        """[{code, name, market, listed: bool}]"""

    @abstractmethod
    def daily_quotes(self, code: str, start: date, end: date) -> list[dict[str, Any]]:
        """[{date, open, high, low, close, volume, adjustment_factor, adj_close}]"""

    @abstractmethod
    def financial_statements(self, code: str) -> list[dict[str, Any]]:
        """financials テーブルの列に揃えた dict のリスト（source / fetched_at は呼び出し側で付与）"""

    def disclosures(self, code: str, start: date, end: date) -> list[dict[str, Any]]:
        """[{disclosed_at, title, category, url}]。提供しないプロバイダは空を返す。"""
        return []


def classify_disclosure(title: str) -> str:
    t = title
    if "下方修正" in t or ("業績予想" in t and "修正" in t and ("減" in t or "下回" in t)):
        return "revision_down"
    if "上方修正" in t or ("業績予想" in t and "修正" in t and ("増" in t or "上回" in t)):
        return "revision_up"
    if "自己株式" in t and ("取得" in t or "買付" in t):
        return "buyback"
    if any(k in t for k in ("新株式発行", "公募", "第三者割当", "新株予約権", "売出し", "MSワラント", "行使価額修正")):
        return "offering"
    if "配当" in t:
        return "dividend"
    if "受注" in t:
        return "orders"
    return "other"
