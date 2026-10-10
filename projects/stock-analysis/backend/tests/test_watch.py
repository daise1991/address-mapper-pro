from datetime import date, datetime, time, timedelta

import pytest

from stock_analysis import watch
from stock_analysis.config import JST
from stock_analysis.db import connect
from stock_analysis.pipeline import run_daily
from stock_analysis.providers.demo import DemoProvider
from stock_analysis.universe import seed_universe

D = date(2026, 10, 9)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    seed_universe(c)
    c.execute("UPDATE securities SET active=0 WHERE code NOT IN ('6857','8035')")
    c.commit()
    return c


def test_watch_codes_scored_separately(conn):
    watch.add(conn, "proposal", "main", [{"code": "8035", "name": "東京エレクトロン"}, {"code": "9999", "name": "新しい候補"}], D)
    active_before = {r[0]: r[1] for r in conn.execute("SELECT code, active FROM securities")}
    r = run_daily(conn, DemoProvider(), D, datetime.combine(D, time(18, 5), JST))
    assert r["stages"]["publish"] == "done"
    # 正式の採点は正式な監視銘柄だけ。監視枠の候補は scores_watch にだけ入る
    assert {x[0] for x in conn.execute("SELECT code FROM scores WHERE run_date=?", (D.isoformat(),))} == {"6857", "8035"}
    assert [x[0] for x in conn.execute("SELECT code FROM scores_watch WHERE run_date=?", (D.isoformat(),))] == ["9999"]
    # 監視銘柄の正式な一覧は変わらない（追加された候補は active=0）
    after = {r[0]: r[1] for r in conn.execute("SELECT code, active FROM securities")}
    assert all(after[k] == v for k, v in active_before.items() if k != "9999") and after["9999"] == 0
    rep = watch.compare(conn, ["proposal"], D)
    rows = {x["code"]: x for x in rep["lists"]["proposal"]["rows"]}
    assert rows["9999"]["growth"] is not None and rows["8035"]["growth"] is not None
    assert rows["9999"]["fin"]["debt"] is None  # 有利子負債は未取得なら None（健全と断定しない）
    assert "取得不可" in watch.to_markdown(rep)


def test_research_tier_limit(conn):
    watch.add(conn, "p", "research", [{"code": f"9{i:03d}"} for i in range(10)], D)
    with pytest.raises(ValueError):
        watch.add(conn, "p", "research", [{"code": "9100"}], D)
    assert conn.execute("SELECT category FROM securities WHERE code='9000'").fetchone()[0] == "emerging_loss"


def test_remove_keeps_history(conn):
    watch.add(conn, "p", "main", [{"code": "9001", "name": "A"}], D)
    assert watch.remove(conn, "p", "main", "9001", D + timedelta(days=3), "売買代金不足")
    rows = conn.execute("SELECT tier, removed_at, reason FROM watchlists WHERE code='9001' ORDER BY tier").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("main", "2026-10-12"), ("past", None)]
    assert "売買代金不足" in rows[1][2]
    assert watch.shadow_codes(conn) == ["9001"]  # 外した後も成績を追う


def test_track_uses_only_data_until_as_of(conn):
    start = date(2026, 1, 5)
    days = [start + timedelta(days=i) for i in range(200)]
    for i, d in enumerate(days):
        conn.execute("INSERT INTO daily_quotes (code, date, close, adj_close, adjustment_factor, source, fetched_at) VALUES (?,?,?,?,1.0,'t','t')",
                     ("9001", d.isoformat(), 100 + i, 100 + i))
        conn.execute("INSERT INTO benchmark_quotes (name, date, close, source, fetched_at) VALUES ('TOPIX',?,1000,'t','t')", (d.isoformat(),))
    t = watch.track_one(conn, "9001", start.isoformat(), days[70])
    assert t["entry_price"] == 100
    assert t["1か月"]["return"] == pytest.approx(0.20) and t["1か月"]["excess"] == pytest.approx(0.20)
    assert t["3か月"]["return"] == pytest.approx(0.60)
    assert t["6か月"] is None  # 評価日より後の株価は使わない
