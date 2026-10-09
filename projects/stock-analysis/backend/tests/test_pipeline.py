from datetime import date, datetime, time

import pytest

from stock_analysis.config import JST
from stock_analysis.db import connect
from stock_analysis.ingest import ingest_financials, ingest_quotes, quotes_complete
from stock_analysis.market_calendar import is_business_day
from stock_analysis.pipeline import run_daily, stage_status
from stock_analysis.portfolio import order_plan, positions
from stock_analysis.providers.base import DataUnavailable, classify_disclosure
from stock_analysis.providers.demo import DemoProvider
from stock_analysis.scoring.features import build_features
from stock_analysis.universe import seed_universe

D = date(2026, 10, 9)  # 金曜・営業日


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    seed_universe(c)
    # テストを速くするため3銘柄に絞る
    c.execute("UPDATE securities SET active=0 WHERE code NOT IN ('6857','8035','186A')")
    c.commit()
    return c


class MissingTodayProvider(DemoProvider):
    """当日分がまだ配信されていない状況を再現する。"""

    def daily_quotes(self, code, start, end):
        return [r for r in super().daily_quotes(code, start, end) if r["date"] != D.isoformat()]


class FailingProvider(DemoProvider):
    def financial_statements(self, code):
        raise DataUnavailable("plan")


def test_calendar_fallback():
    assert is_business_day(date(2026, 10, 9))[0]
    assert not is_business_day(date(2026, 10, 12))[0]  # スポーツの日
    assert not is_business_day(date(2026, 12, 31))[0] and not is_business_day(date(2027, 1, 4 - 1))[0]
    assert not is_business_day(date(2026, 10, 10))[0]


def test_holiday_is_skipped(conn):
    r = run_daily(conn, DemoProvider(), date(2026, 10, 12), datetime(2026, 10, 12, 18, 0, tzinfo=JST))
    assert r["status"] == "skipped"
    assert stage_status(conn, date(2026, 10, 12), "fetch") == "skipped"


def test_full_run(conn):
    r = run_daily(conn, DemoProvider(), D, datetime.combine(D, time(18, 5), JST))
    assert r["stages"] == {k: "done" for k in ("fetch", "score", "verify", "finalize", "publish")}
    n = conn.execute("SELECT COUNT(*) FROM scores WHERE run_date=?", (D.isoformat(),)).fetchone()[0]
    assert n == 3
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    # ETF・新興赤字は未算定（NULL）で、0点にしない
    r186 = conn.execute("SELECT buy_score, buy_status FROM scores WHERE code='186A'").fetchone()
    assert r186["buy_score"] is None and r186["buy_status"] == "unrated"


def test_stages_wait_for_schedule(conn):
    r = run_daily(conn, DemoProvider(), D, datetime.combine(D, time(16, 10), JST))
    assert r["stages"] == {"fetch": "done"}


def test_no_scoring_without_final_close_and_delay_notice(conn):
    p = MissingTodayProvider()
    r = run_daily(conn, p, D, datetime.combine(D, time(17, 0), JST))
    assert r["stages"] == {"fetch": "waiting"}
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0
    r = run_daily(conn, p, D, datetime.combine(D, time(20, 1), JST))
    assert r["stages"] == {"fetch": "delayed"}
    run_daily(conn, p, D, datetime.combine(D, time(20, 30), JST))
    kinds = [x["kind"] for x in conn.execute("SELECT kind FROM notifications")]
    assert kinds == ["delay"]  # 遅延通知は1回だけ
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0
    # データがそろえば遅延後でも確定まで進む
    r = run_daily(conn, DemoProvider(), D, datetime.combine(D, time(21, 0), JST))
    assert r["stages"]["publish"] == "done"


def test_unavailable_is_logged_not_faked(conn):
    ingest_financials(conn, FailingProvider(), "6857")
    row = conn.execute("SELECT status FROM fetch_log WHERE target='financials:6857'").fetchone()
    assert row["status"] == "unavailable"
    assert conn.execute("SELECT COUNT(*) FROM financials").fetchone()[0] == 0


def test_dedupe_on_refetch(conn):
    p = DemoProvider()
    ingest_quotes(conn, p, "6857", date(2026, 9, 1), D)
    n1 = conn.execute("SELECT COUNT(*) FROM daily_quotes").fetchone()[0]
    ingest_quotes(conn, p, "6857", date(2026, 9, 1), D)
    assert conn.execute("SELECT COUNT(*) FROM daily_quotes").fetchone()[0] == n1
    ok, missing = quotes_complete(conn, ["6857", "8035"], D)
    assert not ok and missing == ["8035"]


def test_features_have_no_lookahead(conn):
    ingest_financials(conn, DemoProvider(), "6857")
    early = build_features(conn, "6857", date(2025, 1, 1), 1000, {})
    late = build_features(conn, "6857", D, 1000, {})
    assert early.get("fiscal_period_end") == "2024-03-31"
    assert late.get("fiscal_period_end") == "2026-03-31"


def test_cost_basis_does_not_change_scores_and_plan(conn):
    run_daily(conn, DemoProvider(), D, datetime.combine(D, time(18, 5), JST))
    before = [tuple(r) for r in conn.execute("SELECT code, growth_score, buy_score, sell_score FROM scores")]
    conn.execute("INSERT INTO holdings (code, quantity, avg_cost, account, updated_at) VALUES ('6857', 10, 1, 'specific', 'x')")
    conn.execute("INSERT INTO cash_balance (as_of, amount) VALUES ('2026-10-09', 1000000)")
    conn.commit()
    from stock_analysis.analysis import run_scoring
    run_scoring(conn, D)
    after = [tuple(r) for r in conn.execute("SELECT code, growth_score, buy_score, sell_score FROM scores")]
    assert before == after
    pos = positions(conn)
    h = pos["holdings"][0]
    assert h["unrealized"] > 0 and h["est_tax_if_sold"] == pytest.approx(h["unrealized"] * 0.20315)
    plan = order_plan(conn, D)
    assert plan["execution"].startswith("2026-10-13")  # 10/12 は祝日
    for o in plan["buy_orders"]:
        assert o["quantity"] >= 1 and isinstance(o["quantity"], int)


def test_classify_disclosure():
    assert classify_disclosure("通期業績予想の下方修正に関するお知らせ") == "revision_down"
    assert classify_disclosure("第三者割当による新株式発行に関するお知らせ") == "offering"
    assert classify_disclosure("自己株式の取得に係る事項の決定に関するお知らせ") == "buyback"
