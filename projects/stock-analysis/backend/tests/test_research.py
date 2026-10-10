import json
from datetime import date, datetime, time
from pathlib import Path

import pytest

from stock_analysis import research
from stock_analysis.analysis import METHOD_VERSION, run_scoring
from stock_analysis.config import JST
from stock_analysis.db import connect, upsert
from stock_analysis.pipeline import run_daily
from stock_analysis.providers.demo import DemoProvider
from stock_analysis.universe import seed_universe

D = date(2026, 10, 9)
BATCH = Path(__file__).resolve().parents[2] / "assumptions" / "batch1_2026-10.json"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "r.db")
    seed_universe(c)
    c.execute("UPDATE securities SET active=0 WHERE code NOT IN ('6857','1944')")
    c.commit()
    run_daily(c, DemoProvider(), D, datetime.combine(D, time(18, 5), JST))
    return c


def items():
    return [i for i in json.loads(BATCH.read_text(encoding="utf-8")) if i["code"] in ("6857", "1944")]


def test_batch_file_is_complete_and_valid():
    data = json.loads(BATCH.read_text(encoding="utf-8"))
    assert len(data) == 10
    for it in data:
        assert all(it["scenarios_2029_estimate"][k]["eps"] > 0 for k in research.SCENARIOS)
        assert it["sources"] and it["confidence"] in ("高", "中", "低")
        r = research.validate(it, it["approx_price"]["price"], D)
        assert r["scenarios"]["base"]["theoretical_value"] > 0


def test_import_scores_scenarios_as_provisional(conn):
    research.import_research(conn, items(), D, "test")
    run_scoring(conn, D)
    bd = json.loads(conn.execute("SELECT buy_detail FROM scores WHERE code='6857'").fetchone()[0])
    assert bd["assumption_status"] == "provisional" and bd["assumption_origin"] == "research"
    sc = bd["scenarios"]
    assert sc["bear"]["theoretical_value"] < sc["base"]["theoretical_value"] < sc["bull"]["theoretical_value"]
    assert bd["anchor_ratio"] == pytest.approx(1.0)  # 取込時の会社予想を基準にする


def test_approve_then_auto_update_and_review_triggers(conn):
    its = items()
    its[[i["code"] for i in its].index("6857")]["valuation_method"] = "per"  # 会社予想EPS基準の経路を検証
    research.import_research(conn, its, D, "test")
    assert research.approve(conn, ["6857", "1944"], "確認済み", METHOD_VERSION) == 2
    assert research.monitor(conn, D, METHOD_VERSION) == []  # 変化なしなら承認不要で継続
    r = research.get(conn, "6857")
    # 会社予想が10%上がる → 自動更新（2029年EPSは半分の約4.9%だけ連動）、承認は不要
    anchor = r["anchor_eps"]
    upsert(conn, "financials", {"code": "6857", "fiscal_period_end": "2026-06-30", "period_type": "1Q",
                                "disclosed_at": "2026-10-01T15:00:00+09:00", "forecast_eps": anchor * 1.10,
                                "forecast_operating_profit": 1.0, "forecast_fy_end": "2027-03-31",
                                "source": "test", "fetched_at": "x"}, ["code", "fiscal_period_end", "period_type", "disclosed_at"])
    sa = research.scenario_assumptions(conn, "6857", D)
    assert sa["factor"] == pytest.approx(1.10 ** 0.5)
    assert research.monitor(conn, D, METHOD_VERSION) == []
    # 20%上振れ → 要再承認
    conn.execute("UPDATE financials SET forecast_eps=? WHERE code='6857' AND period_type='1Q'", (anchor * 1.20,))
    flagged = research.monitor(conn, D, METHOD_VERSION)
    assert flagged[0]["code"] == "6857" and "基準EPS" in flagged[0]["reasons"][0]
    assert research.get(conn, "6857")["status"] == "review_required"
    # 計算方式の変更でも再承認
    assert {f["code"] for f in research.monitor(conn, D, "2099-new")} == {"1944"}


def test_order_plan_only_verified_research(conn):
    from stock_analysis.portfolio import candidate_checks
    research.import_research(conn, items(), D, "test")
    run_scoring(conn, D)
    s = dict(conn.execute("SELECT * FROM scores WHERE code='1944'").fetchone())
    s["price"] = 1.0
    s["theoretical_value"] = 100.0
    failed = [c["label"] for c in candidate_checks(conn, s, D.isoformat()) if not c["ok"]]
    assert any("検証済みでない（暫定評価）" in f for f in failed)
    research.approve(conn, ["1944"], "ok", METHOD_VERSION)
    run_scoring(conn, D)
    s = dict(conn.execute("SELECT * FROM scores WHERE code='1944'").fetchone())
    s["price"] = 1.0
    s["theoretical_value"] = 100.0
    s["sell_detail"] = "{}"
    assert all(c["ok"] for c in candidate_checks(conn, s, D.isoformat()))


def test_cross_check_against_official_data(conn):
    it = {"code": "1944", "fiscal_month_end": 3, "latest_company_forecast": {"eps": 999.0},
          "history": [{"fiscal_period": "2026年3月期", "net_income": 1.0}]}
    upsert(conn, "financials", {"code": "1944", "fiscal_period_end": "2026-03-31", "period_type": "FY",
                                "disclosed_at": "2026-05-10T15:00:00+09:00", "net_income": 100.0, "forecast_eps": 400.0,
                                "forecast_fy_end": "2027-03-31", "source": "test", "fetched_at": "x"},
           ["code", "fiscal_period_end", "period_type", "disclosed_at"])
    out = research.cross_check(conn, it, D)
    assert any(o.startswith("要確認: 会社予想EPS") for o in out)
    assert any("2026年3月期 純利益" in o for o in out)
