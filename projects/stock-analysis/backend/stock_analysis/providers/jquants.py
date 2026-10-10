"""J-Quants API V2 クライアント。

- ベースURL: https://api.jquants.com/v2 、認証: ヘッダ `x-api-key`（V1 のトークン方式は廃止済み）
- 無料プランは12週間遅延のため、当日終値の取得には Light 以上が必要。
- V2 はカラム名が短縮されている（Open→O 等）。仕様変更に備え、長短どちらの名前でも読む。
- プラン外・未公表・通信失敗は DataUnavailable（＝取得不可）とする。
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any

import requests

from ..config import Settings, get_settings
from ..universe import from_jquants_code, to_jquants_code
from .base import DataUnavailable, Provider

# パスは公式移行ガイドに基づく。仕様変更時は環境変数ではなくここを更新し、テストで固定する。
PATHS = {
    "daily": "/equities/bars/daily",
    "master": "/equities/master",
    "calendar": "/markets/calendar",
    "summary": "/fins/summary",
    "earnings_calendar": "/equities/earnings-calendar",
    "topix": "/indices/bars/daily/topix",  # Light 以上
}


def _num(x: Any) -> float | None:
    if x in (None, "", "-"):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pick(r: dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in r and r[n] not in (None, ""):
            return r[n]
    return None


def _iso(d: Any) -> str | None:
    if not d:
        return None
    s = str(d)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s[:10]


def _next_year(d: str | None) -> str | None:
    if not d:
        return None
    y, m, day = d.split("-")
    return f"{int(y) + 1}-{m}-{'28' if m == '02' and day == '29' else day}"


class JQuantsProvider(Provider):
    name = "jquants"

    def __init__(self, settings: Settings | None = None, session: requests.Session | None = None):
        self.s = settings or get_settings()
        self.http = session or requests.Session()
        self.base = self.s.jquants_base_url.rstrip("/")

    def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.s.jquants_api_key:
            raise DataUnavailable("JQUANTS_API_KEY 未設定")
        headers = {"x-api-key": self.s.jquants_api_key}
        for attempt in range(4):
            out: list[dict[str, Any]] = []
            q = dict(params)
            try:
                while True:
                    r = self.http.get(f"{self.base}{path}", headers=headers, params=q, timeout=60)
                    if r.status_code == 429:
                        raise requests.RequestException("rate limited (429)")
                    if r.status_code in (400, 401, 403, 404):
                        raise DataUnavailable(f"{path} HTTP {r.status_code}: {r.text[:200]}")
                    r.raise_for_status()
                    body = r.json()
                    data = body.get("data")
                    if data is None:  # 旧形式（キー名がエンドポイント名）にも対応
                        data = next((v for k, v in body.items() if isinstance(v, list)), [])
                    out.extend(data)
                    token = body.get("pagination_key")
                    if not token:
                        return out
                    q["pagination_key"] = token
            except requests.RequestException as e:
                if attempt == 3:
                    raise DataUnavailable(f"{path} 通信失敗: {e}") from e
                time.sleep(2 ** attempt * 3)
        return []

    def trading_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        rows = self._get(PATHS["calendar"], {"from": start.isoformat(), "to": end.isoformat()})
        out = []
        for r in rows:
            div = str(_pick(r, "HolDiv", "HolidayDivision"))
            # 0=非営業日, 1=営業日, 2=半日立会, 3=非営業日(祝日取引あり)
            out.append({"date": _iso(_pick(r, "Date")), "is_business_day": div in ("1", "2")})
        return out

    def listed_info(self, codes: list[str]) -> list[dict[str, Any]]:
        rows = self._get(PATHS["master"], {})
        by_code = {from_jquants_code(str(_pick(r, "Code"))): r for r in rows}
        out = []
        for c in codes:
            r = by_code.get(c)
            out.append({
                "code": c,
                "listed": r is not None,
                "name": _pick(r, "CoName", "CompanyName") if r else None,
                "market": _pick(r, "MktNm", "MarketCodeName") if r else None,
                "sector": _pick(r, "S17Nm", "Sector17CodeName") if r else None,
            })
        return out

    def daily_quotes(self, code: str, start: date, end: date) -> list[dict[str, Any]]:
        rows = self._get(PATHS["daily"], {"code": to_jquants_code(code), "from": start.isoformat(), "to": end.isoformat()})
        return [self._bar(r) for r in rows]

    @staticmethod
    def _bar(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "date": _iso(_pick(r, "Date")),
            "open": _num(_pick(r, "O", "Open")),
            "high": _num(_pick(r, "H", "High")),
            "low": _num(_pick(r, "L", "Low")),
            "close": _num(_pick(r, "C", "Close")),
            "volume": _num(_pick(r, "Vo", "Volume")),
            "adjustment_factor": _num(_pick(r, "AdjFactor", "AdjustmentFactor")) or 1.0,
            "adj_close": _num(_pick(r, "AdjC", "AdjustmentClose")),
        }

    def daily_quotes_by_date(self, d: date) -> dict[str, dict[str, Any]] | None:
        # 日付指定なら全上場銘柄を数リクエストで取得できる（銘柄ごとより API 回数が少ない）
        rows = self._get(PATHS["daily"], {"date": d.isoformat()})
        return {from_jquants_code(str(_pick(r, "Code"))): self._bar(r) for r in rows}

    def topix_daily(self, start: date, end: date) -> list[dict[str, Any]]:
        rows = self._get(PATHS["topix"], {"from": start.isoformat(), "to": end.isoformat()})
        return [{k: v for k, v in self._bar(r).items() if k in ("date", "open", "high", "low", "close")} for r in rows]

    def financial_statements(self, code: str) -> list[dict[str, Any]]:
        return self._parse_summary(self._get(PATHS["summary"], {"code": to_jquants_code(code)}))

    def financial_statements_by_date(self, d: date) -> dict[str, list[dict[str, Any]]] | None:
        out: dict[str, list[dict[str, Any]]] = {}
        for r in self._get(PATHS["summary"], {"date": d.isoformat()}):
            code = from_jquants_code(str(_pick(r, "Code")))
            out.setdefault(code, []).extend(self._parse_summary([r]))
        return out

    def _parse_summary(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in rows:
            period = _pick(r, "CurPerType", "TypeOfCurrentPeriod")
            if period not in ("FY", "1Q", "2Q", "3Q"):
                continue
            fy_end = _iso(_pick(r, "CurFYEn", "CurrentFiscalYearEndDate"))
            per_end = _iso(_pick(r, "CurPerEn", "CurrentPeriodEndDate"))
            disc_date = _iso(_pick(r, "DiscDate", "DisclosedDate"))
            if not disc_date or not (fy_end or per_end):
                continue
            disc_time = str(_pick(r, "DiscTime", "DisclosedTime") or "15:00:00")[:8]
            doc_type = str(_pick(r, "DocType", "TypeOfDocument") or "")
            is_revision = "ForecastRevision" in doc_type  # 業績予想・配当予想の修正
            nxt = period == "FY" and not is_revision  # 本決算短信では「次期予想」を予想値として使う
            if nxt:
                forecast_fy_end = _iso(_pick(r, "NxtFYEn", "NextFiscalYearEndDate")) or _next_year(fy_end)
            else:
                forecast_fy_end = fy_end
            out.append({
                "fiscal_period_end": (fy_end if period == "FY" else per_end) or fy_end,
                # 予想修正は実績を持たないため FY と区別する（実績の比較に混ぜない）
                "period_type": "REV" if is_revision else period,
                "forecast_fy_end": forecast_fy_end,
                "disclosed_at": f"{disc_date}T{disc_time}+09:00",
                "revenue": _num(_pick(r, "Sales", "NetSales")),
                "operating_profit": _num(_pick(r, "OP", "OperatingProfit")),
                "net_income": _num(_pick(r, "NP", "Profit")),
                "eps": _num(_pick(r, "EPS", "EarningsPerShare")),
                "bps": _num(_pick(r, "BPS", "BookValuePerShare")),
                "equity": _num(_pick(r, "Eq", "Equity")),
                "total_assets": _num(_pick(r, "TA", "TotalAssets")),
                "operating_cf": _num(_pick(r, "CFO", "CashFlowsFromOperatingActivities")),
                "cash": _num(_pick(r, "CashEq", "CashAndEquivalents")),
                "interest_bearing_debt": None,  # 決算短信サマリーに無い。EDINET XBRL で補完
                "shares_outstanding": _num(_pick(r, "ShOutFY", "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock")),
                "treasury_shares": _num(_pick(r, "TrShFY", "NumberOfTreasuryStockAtTheEndOfFiscalYear")),
                "dividend_per_share": _num(_pick(r, "DivAnn", "ResultDividendPerShareAnnual")),
                "forecast_revenue": _num(_pick(r, *(("NxFSales", "NextYearForecastNetSales") if nxt else ("FSales", "ForecastNetSales")))),
                "forecast_operating_profit": _num(_pick(r, *(("NxFOP", "NextYearForecastOperatingProfit") if nxt else ("FOP", "ForecastOperatingProfit")))),
                "forecast_net_income": _num(_pick(r, *(("NxFNP", "NextYearForecastProfit") if nxt else ("FNP", "ForecastProfit")))),
                "forecast_eps": _num(_pick(r, *(("NxFEPS", "NextYearForecastEarningsPerShare") if nxt else ("FEPS", "ForecastEarningsPerShare")))),
                "forecast_dividend": _num(_pick(r, *(("NxFDivAnn", "NextYearForecastDividendPerShareAnnual") if nxt else ("FDivAnn", "ForecastDividendPerShareAnnual")))),
                "document_type": doc_type or None,
                "source_ref": _pick(r, "DiscNo", "DisclosureNumber"),
            })
        return out
