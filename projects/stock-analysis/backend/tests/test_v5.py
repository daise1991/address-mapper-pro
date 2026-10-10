"""v5 の品質保証テスト（指示書 7章の各項目）。"""
import json
from datetime import date, timedelta

import pytest

from stock_analysis import research
from stock_analysis.db import connect, upsert
from stock_analysis.scoring.valuation import Assumption
from stock_analysis.universe import seed_universe
from stock_analysis.v5 import confidence, engine, growth, qualitative, risk, sell, valuation
from stock_analysis.v5.common import Item, summarize
from stock_analysis.v5.features import build

D = date(2026, 10, 9)
CODE = "1944"


def fin(conn, y, **kw):
    row = {"code": CODE, "fiscal_period_end": f"{y}-03-31", "period_type": "FY", "disclosed_at": f"{y}-05-10T15:00:00+09:00",
           "revenue": 1000e8 * 1.1 ** (y - 2021), "operating_profit": 100e8 * 1.12 ** (y - 2021),
           "net_income": 70e8 * 1.12 ** (y - 2021), "eps": 100.0 * 1.12 ** (y - 2021), "equity": 2000e8,
           "total_assets": 4000e8, "operating_cf": 120e8, "cash": 500e8, "interest_bearing_debt": 100e8,
           "shares_outstanding": 7e7, "treasury_shares": 0, "dividend_per_share": 30.0 * 1.12 ** (y - 2021),
           "forecast_revenue": 1000e8 * 1.1 ** (y - 2020), "forecast_operating_profit": 100e8 * 1.12 ** (y - 2020),
           "forecast_eps": 100.0 * 1.12 ** (y - 2020), "forecast_dividend": 30.0 * 1.12 ** (y - 2020),
           "forecast_fy_end": f"{y + 1}-03-31", "source": "test", "fetched_at": "x"}
    row.update(kw)
    upsert(conn, "financials", row, ["code", "fiscal_period_end", "period_type", "disclosed_at"])


def quotes(conn, price=1500.0, days=260):
    for i in range(days):
        d = (D - timedelta(days=days - 1 - i)).isoformat()
        upsert(conn, "daily_quotes", {"code": CODE, "date": d, "close": price, "adj_close": price, "adjustment_factor": 1.0,
                                      "is_final": 1, "source": "test", "fetched_at": "x"}, ["code", "date"])


def research_item(eps=(150.0, 200.0, 250.0), per=(12, 15, 18)):
    return {"code": CODE, "name": "きんでん", "fiscal_month_end": 3, "category": "stable", "valuation_method": "per",
            "required_return": 0.10, "safety_margin": 0.15,
            "scenarios_2029_estimate": {k: {"eps": e, "per": p, "rationale": "推計"} for k, e, p in zip(("bear", "base", "bull"), eps, per)},
            "latest_company_forecast": {"eps": 100.0 * 1.12 ** 6}, "history": [], "sources": [{"title": "t", "url": "u"}],
            "confidence": "中", "unverified": []}


QUAL = {"code": CODE, "market_expansion": {"segments": [{"segment": "s", "sales_share": 1.0, "cagr": 0.05, "base_points": 60, "url": "u"}],
                                           "adjustment": 0, "score": 60},
        "competitive_advantage": {"score": 76, "components": {k: {"points": p, "url": "u"} for k, p in
                                                             (("share", 30), ("margin_vs_peers", 15), ("technology", 12),
                                                              ("orders_customers", 9), ("midterm_plan", 10))}}}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "v5.db")
    seed_universe(c)
    for y in range(2021, 2027):
        fin(c, y)
    quotes(c)
    return c


def full_setup(conn, approve=True):
    research.import_research(conn, [research_item()], D, "test")
    qualitative.import_items(conn, [QUAL], "test")
    if approve:
        research.approve(conn, [CODE], "ok", engine.VERSION)


def test_same_input_same_score(conn):
    full_setup(conn)
    a = engine.score_one(conn, CODE, D)
    b = engine.score_one(conn, CODE, D)
    strip = lambda r: {k: v for k, v in r.items() if k != "created_at"}  # noqa: E731
    assert strip(a) == strip(b)


def test_missing_items_are_unevaluated_not_zero_and_not_formal():
    items = [Item("a", "A", 50, True, [("x", 1.0, 90.0)]), Item("b", "B", 50, False, [("y", 1.0, None)])]
    s = summarize(items)
    assert s["reference"] == 90 and s["formal"] is None and s["status"] == "provisional"
    assert "B" in s["unevaluated"]
    # 欠損の数で正式点数が上がらない: 全項目そろったときだけ正式
    items[1].components = [("y", 1.0, 10.0)]
    s2 = summarize(items)
    assert s2["status"] == "formal" and s2["formal"] == 50
    # 必須項目が欠けると充足率が高くても正式にしない
    s3 = summarize([Item("a", "A", 10, True, [("x", 1.0, None)]), Item("b", "B", 90, False, [("y", 1.0, 80.0)])])
    assert s3["status"] == "provisional" and s3["missing_required"] == ["A"]


def test_reference_scores_never_drive_formal_decisions(conn):
    research.import_research(conn, [research_item()], D, "test")  # 定性2項目なし・未承認
    r = engine.score_one(conn, CODE, D)
    assert r["eval_status"] != "formal" and r["buy_decision"] is None


def test_formal_requires_coverage_confidence_and_approval(conn):
    full_setup(conn, approve=False)
    assert engine.score_one(conn, CODE, D)["eval_status"] == "provisional"
    research.approve(conn, [CODE], "ok", engine.VERSION)
    r = engine.score_one(conn, CODE, D)
    conf = json.loads(r["confidence_detail"])
    assert r["growth_formal"] is not None and r["sell_formal"] is not None
    assert (r["eval_status"] == "formal") == (r["confidence"] in ("A", "B"))
    assert conf["insufficient_history"] in (True, False)


def test_unrated_vs_zero(conn):
    full_setup(conn)
    expensive = engine.score_one(conn, CODE, D)
    assert expensive["buy_status"] == "rated"
    # 赤字企業: PER を当てはめず未算定（0点にしない）
    research.import_research(conn, [research_item(eps=(-50.0, -10.0, 20.0))], D, "test")
    loss = engine.score_one(conn, CODE, D)
    assert loss["buy_score"] is None and loss["buy_status"] == "unrated"
    assert "PSR" in json.dumps(json.loads(loss["buy_detail"])["base"]["loss_making"], ensure_ascii=False)


def test_psr_is_reference_only_and_needs_full_evidence():
    assert valuation.loss_making({}, 1000)["status"] == "unrated"
    weak = {"loss_making_valuation": {"revenue_2029_per_share": 500, "psr": 4, "op_margin_2029": 0.1,
                                      "cash_runway_years": 1.0, "dilution_risk": "high"}}
    assert valuation.loss_making(weak, 1000)["status"] == "unrated"
    ok = {"loss_making_valuation": {"revenue_2029_per_share": 500, "psr": 4, "op_margin_2029": 0.1,
                                    "cash_runway_years": 3.0, "dilution_risk": "low"}}
    r = valuation.loss_making(ok, 1000)
    assert r["status"] == "reference" and "正式な買い点数には使わない" in r["reason"]


def test_split_is_not_dilution(conn):
    fin(conn, 2026, shares_outstanding=3.5e8)  # 1:5 分割を分割後基準で書き直した株数
    f = build(conn, CODE, D, 1500.0, {})
    assert f.get("share_change") is None and f.get("share_change_excluded_as_split") == 5.0
    s = sell.score(f, 1.0, None, None)
    assert s["items"]["dilution"]["score"] == 0.0
    assert not [x for x in risk.detect(f) if x["type"] == "大規模希薄化"]
    # 実際の増資（+12%）は検出する
    fin(conn, 2026, shares_outstanding=7e7 * 1.12)
    f2 = build(conn, CODE, D, 1500.0, {})
    assert f2.get("share_change") == pytest.approx(0.12)
    assert [x for x in risk.detect(f2) if x["type"] == "大規模希薄化"]


def test_scores_are_independent(conn):
    full_setup(conn)
    a = engine.score_one(conn, CODE, D)
    quotes(conn, price=4000.0)
    b = engine.score_one(conn, CODE, D)
    assert a["growth_reference"] == b["growth_reference"]  # 株価は成長期待点に影響しない
    assert a["buy_score"] != b["buy_score"]
    assert b["sell_reference"] != 100 - b["buy_score"]  # 売りは買いの単純な逆数ではない


def test_severe_risk_not_diluted_and_triggers_reevaluation(conn):
    full_setup(conn)
    fin(conn, 2026, operating_cf=-10e8, equity=600e8)  # 営業CF赤字・自己資本比率15%
    r = engine.score_one(conn, CODE, D)
    risks = json.loads(r["severe_risks"])
    assert any(x["type"] == "財務問題" for x in risks)
    assert r["reevaluate"] == 1 and r["exit_type"] == "stop_loss"
    assert r["buy_decision"] not in ("aggressive", "normal")
    assert r["sell_reference"] < 85  # 点数は引き上げない（警告は別に出す）


def test_rsi_alone_is_capped():
    from stock_analysis.v5.features import V5Features
    f = V5Features(CODE, D, 1000, {"rsi14": 95})
    assert sell.score(f, None, None, None)["items"]["overheating"]["score"] == 40.0


def test_no_future_information(conn):
    fin(conn, 2027, disclosed_at="2027-05-10T15:00:00+09:00", revenue=1.0)
    f = build(conn, CODE, D, 1500.0, {})
    assert f.sources[0].startswith("test:2026-03-31")


def test_dividend_no_double_count_and_capped_growth(conn):
    f = build(conn, CODE, D, 1500.0, {})
    a = Assumption(method="per", target_year=2029, fiscal_month_end=3, required_return=0.1, safety_margin=0.15,
                   eps_forecast=1000.0, assumed_per=15, reason="t")
    sch = valuation.dividend_schedule(f, a, 1000.0, D)
    ends = [x["fiscal_end"] for x in sch["schedule"]]
    assert ends == ["2027-03-31", "2028-03-31"]  # 目標期（2029/3）の配当は将来株価に含まれるため加算しない
    assert sch["schedule"][1]["dps"] <= sch["schedule"][0]["dps"] * 1.10 + 0.02  # 増配は年10%まで
    v = valuation.value(f, a, 1500.0, D)
    assert v["theoretical_value"] == pytest.approx(v["theoretical_value_no_dividend"] + sch["pv"], abs=0.01)
    assert v["dividend_score_change"] == v["buy_score"] - v["buy_score_no_dividend"]


def test_dividend_unknown_is_not_added(conn):
    conn.execute("UPDATE financials SET dividend_per_share=NULL, forecast_dividend=NULL")
    f = build(conn, CODE, D, 1500.0, {})
    a = Assumption(method="per", target_year=2029, required_return=0.1, safety_margin=0.15, eps_forecast=200.0,
                   assumed_per=15, reason="t")
    sch = valuation.dividend_schedule(f, a, 200.0, D)
    assert sch["status"] == "未算定" and sch["pv"] == 0.0


def test_confidence_insufficient_history_is_not_zero():
    common = dict(coverage=1.0, has_price=True, has_company_data=True, has_scenarios=True, forecast_eps_diff=0.0,
                  history_match=(3, 3), research_date="2026-10-01", as_of=D,
                  scenario_values={"bear": 80, "base": 100, "bull": 130})
    short = confidence.grade(forecast_errors=[], **common)
    assert short["parts"]["forecast_error"] == "検証期間不足" and short["grade"] == "A"
    bad = confidence.grade(forecast_errors=[0.5, 0.6], **common)
    assert bad["points"] < short["points"]
    forced = confidence.grade(forecast_errors=[], **{**common, "has_scenarios": False})
    assert forced["grade"] == "D" and forced["forced_d"]


def test_qualitative_rule_recomputes_and_flags_mismatch():
    item = json.loads(json.dumps(QUAL))
    item["competitive_advantage"]["score"] = 99
    item["market_expansion"]["segments"][0]["base_points"] = 88
    ev = qualitative.evaluate(item)
    assert ev["competitive_advantage"] == 76 and ev["market_expansion"] == 60
    assert any("再計算" in i for i in ev["issues"]) and any("補正" in i for i in ev["issues"])


def test_v4_scores_untouched_by_v5(conn):
    from stock_analysis.analysis import run_scoring
    run_scoring(conn, D)
    before = [tuple(r) for r in conn.execute("SELECT * FROM scores")]
    full_setup(conn)
    engine.run(conn, D)
    assert [tuple(r) for r in conn.execute("SELECT * FROM scores")] == before


def test_growth_uses_multi_year_history(conn):
    f = build(conn, CODE, D, 1500.0, {})
    assert f.get("revenue_cagr_hist") == pytest.approx(0.10, abs=1e-3)
    assert f.get("history_years") == 6 and f.get("op_margin_avg") is not None
    g = growth.score(f, {}, None, None)
    assert g["items"]["revenue_growth"]["components"][0]["score"] is not None
