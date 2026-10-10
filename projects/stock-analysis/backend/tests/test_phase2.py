from datetime import date, datetime, time

import pytest

from stock_analysis import alerts
from stock_analysis.analysis import run_scoring
from stock_analysis.config import JST
from stock_analysis.db import connect, get_setting, set_setting, upsert
from stock_analysis.ingest import detect_forecast_revisions, ingest_quotes, ingest_quotes_bulk
from stock_analysis.pipeline import fetch_quotes, run_daily
from stock_analysis.portfolio import order_plan
from stock_analysis.providers.demo import DemoProvider
from stock_analysis.providers.jquants import JQuantsProvider
from stock_analysis.universe import seed_universe
from stock_analysis.verification import summary

D = date(2026, 10, 9)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "p2.db")
    seed_universe(c)
    c.execute("UPDATE securities SET active=0 WHERE code NOT IN ('6857','8035','2644')")
    c.commit()
    return c


class CountingProvider(DemoProvider):
    def __init__(self):
        self.per_code_calls = 0
        self.bulk_calls = 0
        self._in_bulk = False

    def daily_quotes(self, code, start, end):
        if not self._in_bulk:
            self.per_code_calls += 1
        return super().daily_quotes(code, start, end)

    def daily_quotes_by_date(self, d):
        self.bulk_calls += 1
        self._in_bulk = True
        try:
            return super().daily_quotes_by_date(d)
        finally:
            self._in_bulk = False


def test_bulk_fetch_used_when_history_exists(conn):
    p = CountingProvider()
    codes = ["6857", "8035", "2644"]
    for c in codes:
        ingest_quotes(conn, p, c, date(2026, 9, 1), date(2026, 10, 8))
    p.per_code_calls = 0
    set_setting(conn, "quotes_refreshed", "2026-10-08")  # 週1回の取り直しは済んでいる状態
    fetch_quotes(conn, p, codes, D, "t")
    assert p.bulk_calls == 1 and p.per_code_calls == 0
    n = conn.execute("SELECT COUNT(*) FROM daily_quotes WHERE date=?", (D.isoformat(),)).fetchone()[0]
    assert n == 3


def test_weekly_refresh_refetches_recent_days(conn):
    p = CountingProvider()
    for c in ["6857", "8035"]:
        ingest_quotes(conn, p, c, date(2026, 9, 1), date(2026, 10, 8))
    set_setting(conn, "quotes_refreshed", "2026-10-01")
    fetch_quotes(conn, p, ["6857", "8035"], D, "t")
    assert p.bulk_calls == 1 + 5
    assert get_setting(conn, "quotes_refreshed") == D.isoformat()


def test_bulk_returns_missing_codes(conn):
    class Partial(DemoProvider):
        def daily_quotes_by_date(self, d):
            return {"6857": self.daily_quotes("6857", d, d)[0]}
    assert ingest_quotes_bulk(conn, Partial(), ["6857", "8035"], D) == ["8035"]


def _fin(conn, disclosed, period_type, fop, fdiv=50.0, fy_end="2027-03-31"):
    upsert(conn, "financials", {
        "code": "6857", "fiscal_period_end": "2026-03-31" if period_type == "FY" else "2026-06-30",
        "period_type": period_type, "disclosed_at": disclosed, "forecast_operating_profit": fop,
        "forecast_dividend": fdiv, "forecast_fy_end": fy_end, "source": "test", "fetched_at": "x",
    }, ["code", "fiscal_period_end", "period_type", "disclosed_at"])


def test_forecast_revision_detection(conn):
    _fin(conn, "2026-05-12T15:00:00+09:00", "FY", 100e8)
    _fin(conn, "2026-08-05T15:00:00+09:00", "REV", 80e8, fdiv=40.0)
    _fin(conn, "2026-09-01T15:00:00+09:00", "REV", 81e8, fdiv=40.0)  # 1%変化は修正扱いしない
    assert detect_forecast_revisions(conn, "6857") == 2
    cats = sorted(r["category"] for r in conn.execute("SELECT category FROM disclosures"))
    assert cats == ["dividend", "revision_down"]
    assert detect_forecast_revisions(conn, "6857") == 0  # 再実行しても重複しない


def test_jquants_v2_parsing_and_revision_period():
    class Fake(JQuantsProvider):
        def __init__(self):
            pass

        def _get(self, path, params):
            return [
                {"Code": "68570", "DiscDate": "20260512", "DiscTime": "15:00:00", "CurPerType": "FY",
                 "CurFYEn": "2026-03-31", "CurPerEn": "2026-03-31", "DocType": "FYFinancialStatements_Consolidated_IFRS",
                 "Sales": "1000", "OP": "200", "NxFOP": "250", "NxFEPS": "300"},
                {"Code": "68570", "DiscDate": "20260805", "CurPerType": "FY", "CurFYEn": "2027-03-31",
                 "DocType": "EarnForecastRevision", "FOP": "230", "FEPS": "280"},
            ]
    rows = Fake().financial_statements("6857")
    fy, rev = rows
    assert fy["period_type"] == "FY" and fy["forecast_operating_profit"] == 250 and fy["forecast_fy_end"] == "2027-03-31"
    assert rev["period_type"] == "REV" and rev["forecast_operating_profit"] == 230 and rev["forecast_fy_end"] == "2027-03-31"
    bulk = Fake().financial_statements_by_date(D)
    assert list(bulk) == ["6857"] and len(bulk["6857"]) == 2


def test_alerts_and_dedupe(conn):
    run_daily(conn, DemoProvider(), D, datetime.combine(D, time(18, 5), JST))
    code = "6857"
    conn.execute("INSERT INTO holdings (code, quantity, avg_cost, account, updated_at) VALUES (?, 1, 1, 'specific', 'x')", (code,))
    conn.execute("UPDATE scores SET buy_score=80, price=100, target_price=120 WHERE code=? AND run_date=?", (code, D.isoformat()))
    conn.execute("INSERT INTO disclosures (code, disclosed_at, title, category, source, fetched_at) VALUES (?,?,?,?,?,?)",
                 (code, f"{D.isoformat()}T15:30:00+09:00", "業績予想の下方修正", "revision_down", "test", "x"))
    conn.commit()
    found = alerts.detect(conn, D)
    kinds = {a["kind"] for a in found if a["code"] == code}
    assert {"buy75", "target", "revision"} <= kinds
    first = alerts.record_new(conn, D, found)
    assert first and alerts.record_new(conn, D, alerts.detect(conn, D)) == []


def test_order_plan_monthly_budget_and_upper_limit(conn):
    run_daily(conn, DemoProvider(), D, datetime.combine(D, time(18, 5), JST))
    conn.execute("UPDATE scores SET buy_decision='aggressive', price=1000 WHERE code='6857'")
    conn.execute("INSERT INTO cash_balance (as_of, amount) VALUES ('2026-10-09', 10000000)")
    conn.execute("INSERT INTO holdings (code, quantity, avg_cost, account, acquired_at, updated_at) VALUES ('8035', 10, 1000, 'specific', '2026-10-01', 'x')")
    conn.commit()
    plan = order_plan(conn, D)
    assert plan["month_spent"] == 10000 and plan["month_left"] == 20000
    o = plan["buy_orders"][0]
    assert o["upper_limit_price"] == 1030.0 and o["quantity"] == 19  # 20,000 / 1,030
    assert sum(x["estimated_amount"] for x in plan["buy_orders"]) <= 20000
    assert "指値注文ができません" in plan["s_kabu_note"]


def test_method_version_archive_and_benchmark(conn):
    run_daily(conn, DemoProvider(), D, datetime.combine(D, time(18, 5), JST))
    conn.execute("UPDATE scores SET method_version='old'")
    conn.commit()
    run_scoring(conn, D)
    assert conn.execute("SELECT COUNT(*) FROM scores_archive WHERE method_version='old'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM benchmark_quotes WHERE name='TOPIX'").fetchone()[0] > 200
    s = summary(conn, 5, benchmark="TOPIX")
    assert s["benchmark_label"] == "TOPIX" and "買い85点以上(k=150 比較)" in s["rules"]
