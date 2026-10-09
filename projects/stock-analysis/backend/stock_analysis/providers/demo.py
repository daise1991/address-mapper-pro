"""開発・画面確認用の合成データ。実在企業の実データではない。

source="demo" で保存され、UI に「デモデータ」バナーを表示する。本番DBとはファイルを分ける。
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import date, timedelta
from typing import Any

from ..market_calendar import business_days, is_business_day
from .base import Provider


def _rng(code: str, salt: str = "") -> random.Random:
    return random.Random(int(hashlib.sha256(f"{code}{salt}".encode()).hexdigest()[:12], 16))


def _base_price(code: str) -> float:
    return _rng(code).uniform(300, 12000)


def _growth(code: str) -> float:
    return _rng(code, "growth").uniform(-0.05, 0.25)


class DemoProvider(Provider):
    name = "demo"
    history_start = date(2023, 1, 4)

    def trading_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        out, d = [], start
        while d <= end:
            out.append({"date": d.isoformat(), "is_business_day": is_business_day(d)[0]})
            d += timedelta(days=1)
        return out

    def listed_info(self, codes: list[str]) -> list[dict[str, Any]]:
        return [{"code": c, "listed": True, "name": None, "market": "demo"} for c in codes]

    def _series(self, code: str, end: date) -> list[tuple[date, float, float]]:
        r = _rng(code)
        price = _base_price(code)
        r.random()
        # 株価トレンドを業績成長に緩やかに連動させ、そこに銘柄固有の乖離を加える
        drift = math.log(1 + _growth(code)) / 250 + r.uniform(-0.0008, 0.0006)
        vol = r.uniform(0.012, 0.04)
        out = []
        for d in business_days(self.history_start, end):
            price *= math.exp(drift + vol * r.gauss(0, 1))
            out.append((d, round(price, 1), round(r.uniform(1e5, 5e6))))
        return out

    def daily_quotes(self, code: str, start: date, end: date) -> list[dict[str, Any]]:
        rows = []
        for d, close, volume in self._series(code, end):
            if d < start:
                continue
            r = _rng(code, d.isoformat())
            o = close * (1 + r.uniform(-0.01, 0.01))
            rows.append({
                "date": d.isoformat(), "open": round(o, 1),
                "high": round(max(o, close) * (1 + r.uniform(0, 0.015)), 1),
                "low": round(min(o, close) * (1 - r.uniform(0, 0.015)), 1),
                "close": close, "volume": volume, "adjustment_factor": 1.0, "adj_close": close,
            })
        return rows

    def financial_statements(self, code: str) -> list[dict[str, Any]]:
        r = _rng(code, "fin")
        rev = r.uniform(2e10, 3e12)
        margin = r.uniform(-0.06, 0.25)
        growth = _growth(code)
        # 株価と整合するよう、初期PERが 12〜45 倍になる株数にする
        shares = max(rev * abs(margin) * 0.68, 1e8) / (_base_price(code) / r.uniform(12, 45))
        out = []
        for year in (2023, 2024, 2025, 2026):
            rev *= 1 + growth + r.uniform(-0.08, 0.08)
            m = margin + r.uniform(-0.03, 0.03)
            op = rev * m
            ni = op * 0.68
            assets = rev * r.uniform(0.8, 1.6)
            equity = assets * r.uniform(0.25, 0.7)
            if r.random() < 0.08:
                shares *= 1.06  # 希薄化イベント
            f_growth = growth + r.uniform(-0.15, 0.1)
            out.append({
                "fiscal_period_end": f"{year}-03-31", "period_type": "FY",
                "disclosed_at": f"{year}-05-12T15:00:00+09:00",
                "revenue": rev, "operating_profit": op, "net_income": ni,
                "eps": ni / shares, "bps": equity / shares, "equity": equity, "total_assets": assets,
                "operating_cf": op * r.uniform(0.6, 1.3), "cash": assets * r.uniform(0.05, 0.3),
                "interest_bearing_debt": assets * r.uniform(0.0, 0.35),
                "shares_outstanding": shares, "treasury_shares": shares * 0.01,
                "dividend_per_share": max(0.0, ni / shares * 0.3),
                "forecast_revenue": rev * (1 + f_growth), "forecast_operating_profit": op * (1 + f_growth * 1.3),
                "forecast_net_income": ni * (1 + f_growth * 1.3), "forecast_eps": ni * (1 + f_growth * 1.3) / shares,
                "forecast_dividend": None, "document_type": "demo", "source_ref": None,
            })
        return out

    def disclosures(self, code: str, start: date, end: date) -> list[dict[str, Any]]:
        r = _rng(code, "disc")
        if r.random() < 0.12:
            d = end - timedelta(days=r.randint(5, 90))
            if d >= start:
                return [{"disclosed_at": f"{d.isoformat()}T15:30:00+09:00", "title": "業績予想の修正（下方修正）に関するお知らせ",
                         "category": "revision_down", "url": None}]
        return []
