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


def _v4_v5(text):
    v4, _, rest = text.partition("【v5 参考")
    return v4, rest


def test_explain_shows_same_result_as_ranking(conn):
    from stock_analysis.checks import _n, explain
    from stock_analysis.v5 import engine
    research.import_research(conn, items(), D, "test")
    run_scoring(conn, D)
    engine.run(conn, D, ["6857"])
    sc = conn.execute("SELECT * FROM scores WHERE code='6857' AND run_date=?", (D.isoformat(),)).fetchone()
    v5 = conn.execute("SELECT * FROM scores_v5 WHERE code='6857' AND run_date=?", (D.isoformat(),)).fetchone()
    inp = json.loads(sc["buy_detail"])["inputs"]
    text = "\n".join(explain(conn, "6857", D))
    v4, v5_text = _v4_v5(text)
    # ランキング（scores）と同じ点数・理論価値・前提を表示し、採点と同じ前提で再計算して一致する
    assert (f"成長期待 {_n(sc['growth_score'])} ／ 買い {sc['buy_score']} ／ 売り {_n(sc['sell_score'])}"
            f" ／ 理論価値 {sc['theoretical_value']}円") in v4
    assert "使った前提: 調査前提（暫定・承認前）" in v4 and "保存値と一致" in v4
    assert f"年EPS {inp['eps_forecast'] or inp['normalized_eps']}円" in v4
    assert f"想定PER {inp['assumed_per']}倍" in v4 and f"必要利回り {inp['required_return']}" in v4
    assert sc["method_version"] in v4 and f"評価日 {D}" in text
    # 自動初期推計は別枠で「採点には使っていない」と明記する
    assert "自動初期推計（採点には使っていない）" in text and "自動初期推計（採点" not in v4
    # v4 と v5 を混ぜない
    assert v5["method_version"] in v5_text and v5["method_version"] not in v4
    assert f"理論価値 {_n(v5['theoretical_value'], '円')}" in v5_text


def test_explain_never_shows_missing_as_zero(conn):
    from stock_analysis.checks import _n, explain
    assert _n(None) == "未算定" and _n(0) == "0"
    run_scoring(conn, D)
    det = json.loads(conn.execute("SELECT growth_detail FROM scores WHERE code='6857'").fetchone()[0])
    missing = [x["label"] for x in det["factors"].values() if x["score"] is None]
    text = "\n".join(explain(conn, "6857", D))
    assert missing and all(f"{m}=未算定" in text and f"{m}=0" not in text for m in missing)


def _split_case(conn, code, s, fy_forecast_post_split=False, quarterly=True):
    """本決算（分割前の EPS 400・株数 1億）→ 1:s 分割 → （任意）分割後の基準で出た四半期の予想 EPS 100。"""
    rows = [("2026-03-31", "FY", "2026-05-12T15:00:00+09:00", 400.0, (400.0 / s) if fy_forecast_post_split else 400.0, 1e8, None)]
    if quarterly:
        rows.append(("2026-06-30", "1Q", "2026-07-31T15:00:00+09:00", 25.0, 400.0 / s, 1e8 * s, 400.0 * 1e8))
    for end, pt, at, eps, fe, sh, fni in rows:
        upsert(conn, "financials", {"code": code, "fiscal_period_end": end, "period_type": pt, "disclosed_at": at,
                                    "eps": eps, "forecast_eps": fe, "forecast_fy_end": "2027-03-31", "forecast_net_income": fni,
                                    "forecast_operating_profit": 1e9, "shares_outstanding": sh, "source": "test", "fetched_at": "x"},
               ["code", "fiscal_period_end", "period_type", "disclosed_at"])
    upsert(conn, "daily_quotes", {"code": code, "date": "2026-06-29", "close": 1000.0, "adjustment_factor": 1 / s,
                                  "is_final": 1, "source": "test", "fetched_at": "x"}, ["code", "date"])


@pytest.mark.parametrize("s", [2, 3, 4, 5, 10])
@pytest.mark.parametrize("case", ["quarterly_after_split", "fy_already_post_split", "fy_pre_split_only"])
def test_split_units_are_unified(conn, s, case):
    """分割 1:2・1:4・1:5・1:10 で、予想EPS・実績EPS（PER）を分割後の基準にそろえ、二重に補正しない（5802 型を含む）。"""
    from stock_analysis.scoring.features import build_features
    from stock_analysis.v5.features import build as build_v5
    code = f"99{s:02d}"
    _split_case(conn, code, s, fy_forecast_post_split=(case == "fy_already_post_split"), quarterly=(case == "quarterly_after_split"))
    f = build_features(conn, code, D, 1000.0, {}, 440.0 / s, 2.475)
    assert f.get("per") == pytest.approx(1000.0 / (400.0 / s))  # 分割前の実績 EPS を 1/s にして比べる
    assert f.get("eps_cagr_to_target") == pytest.approx((440.0 / 400.0) ** (1 / 2.475) - 1, abs=1e-3)  # 予想 400/s が基準
    assert f.get("share_change") is None  # 株数の増加を増資と判定しない
    v = build_v5(conn, code, D, 1000.0, {}).values
    assert v.get("forecast_eps") == pytest.approx(400.0 / s) and v.get("latest_eps") == pytest.approx(400.0 / s)


@pytest.mark.parametrize("s", [2, 3, 4, 5, 10])
@pytest.mark.parametrize("restated", [False, True])
def test_normalized_eps_uses_post_split_shares(conn, s, restated):
    """景気連動株の正常化EPS（自動推計・調査の基準値）は、決算後の分割を株数に反映する（8035・285A 型）。
    株数がすでに分割後の基準で書き直されていれば二重に補正しない。"""
    from stock_analysis.assumptions import auto_baseline
    code = f"98{s:02d}"
    upsert(conn, "securities", {"code": code, "name": "分割テスト", "group_name": "user", "category": "cyclical",
                                "asset_type": "stock", "active": 0, "added_at": "x"}, ["code"])
    for end, at, sh in (("2025-03-31", "2025-05-12T15:00:00+09:00", 1e8),
                        ("2026-03-31", "2026-05-12T15:00:00+09:00", 1e8 * s if restated else 1e8)):
        upsert(conn, "financials", {"code": code, "fiscal_period_end": end, "period_type": "FY", "disclosed_at": at,
                                    "eps": 400.0, "net_income": 4e10, "shares_outstanding": sh, "source": "test", "fetched_at": "x"},
               ["code", "fiscal_period_end", "period_type", "disclosed_at"])
    upsert(conn, "daily_quotes", {"code": code, "date": "2026-09-29", "close": 1000.0, "adjustment_factor": 1 / s,
                                  "is_final": 1, "source": "test", "fetched_at": "x"}, ["code", "date"])
    assert auto_baseline(conn, code, D).normalized_eps == pytest.approx(400.0 / s)
    assert research.normalized_eps_from_db(conn, code, D) == pytest.approx(400.0 / s)
    # 分割前の評価日では補正しない
    assert research.normalized_eps_from_db(conn, code, date(2026, 9, 28)) == pytest.approx(400.0 / (s if restated else 1))



@pytest.mark.parametrize("old_anchor", [400.0, 40.0])
def test_anchor_on_same_share_basis(conn, old_anchor):
    """登録時の正常化EPSが分割前の株数で計算されていても（5801・5802 型。400円）、正しい基準（40円）でも、
    1:10 の分割を利益の減少と見誤らない（基準EPS連動は 1 のまま）。"""
    test_normalized_eps_uses_post_split_shares(conn, 10, False)
    code = "9810"
    sc = {k: {"eps": 50.0, "per": 14.0} for k in research.SCENARIOS}
    research.import_research(conn, [{"code": code, "valuation_method": "normalized", "scenarios_2029_estimate": sc,
                                     "required_return": 0.08, "safety_margin": 0.2}], D, "test")
    conn.execute("UPDATE assumption_research SET anchor_eps=? WHERE code=?", (old_anchor, code))
    r = research.scenario_assumptions(conn, code, D)
    assert r["anchor_now"] == pytest.approx(40.0)
    assert r["anchor_ratio"] == pytest.approx(1.0)


def _bare_stock(conn, code, category="cyclical", asset_type="stock"):
    upsert(conn, "securities", {"code": code, "name": "テスト", "group_name": "user", "category": category,
                                "asset_type": asset_type, "active": 0, "added_at": "x"}, ["code"])


def _fy(conn, code, end, at, **kw):
    upsert(conn, "financials", {"code": code, "fiscal_period_end": end, "period_type": "FY", "disclosed_at": at,
                                "source": "test", "fetched_at": "x", **kw}, ["code", "fiscal_period_end", "period_type", "disclosed_at"])


def test_unobserved_disclosures_are_not_zero(conn):
    """会社予想を出さない会社（ルネサス型）の下方修正件数、EDINET を見ていない期間の増資件数は 0 ではなく未観測。"""
    from stock_analysis.scoring.features import build_features, disclosure_observed
    from stock_analysis.scoring.sell import sell_score
    from stock_analysis.v5.features import build as v5_build
    from stock_analysis.v5.sell import score as v5_sell
    code = "9901"
    _bare_stock(conn, code)
    _fy(conn, code, "2025-12-31", "2026-02-10T15:00:00+09:00", eps=-28.0, net_income=-5e10, shares_outstanding=1e9)
    assert disclosure_observed(conn, code, D) == {"revision": False, "offering": False}
    f = build_features(conn, code, D, 2000.0, {})
    assert f.values["disc_revision_down_120d"] is None and f.values["disc_offering_120d"] is None
    s = sell_score(f, None, None, {})
    assert s["factors"]["downward_revision"]["score"] is None
    v = v5_build(conn, code, D, 2000.0, {})
    assert v.get("disc_revision_down_120d") is None and v.get("disc_offering_120d") is None
    items = v5_sell(v, None, None, None)
    assert json.dumps(items, ensure_ascii=False, default=str).count("増資の開示") >= 1  # 項目は残り、値は未観測
    # 会社予想があり、EDINET を期間の始めから見ていれば 0 件は観測済みの 0
    _fy(conn, code, "2026-12-31", "2026-08-10T15:00:00+09:00", forecast_eps=50.0)
    conn.execute("INSERT INTO fetch_log (run_id, source, target, status, message, fetched_at) VALUES ('t','edinet','2026-05-01','ok','0 docs','x')")
    assert disclosure_observed(conn, code, D) == {"revision": True, "offering": True}
    assert build_features(conn, code, D, 2000.0, {}).values["disc_offering_120d"] == 0


@pytest.mark.parametrize("case,kind,word", [
    ("etf", "対象外", "ETF"), ("loss_growth", "対象外", "赤字成長"), ("nofin", "データ不足", "取得失敗"),
    ("loss_noforecast", "データ不足", "会社予想EPSの公表なし"), ("cyc_one_year", "データ不足", "2期未満")])
def test_unrated_reason(conn, case, kind, word):
    """買いの未評価は「評価前提が未登録」とひとまとめにせず、区分と理由を出す。"""
    from stock_analysis.assumptions import auto_baseline, unrated_reason
    code = "9902"
    _bare_stock(conn, code, category={"loss_growth": "emerging_loss", "loss_noforecast": "growth"}.get(case, "cyclical"),
                asset_type="etf" if case == "etf" else "stock")
    if case in ("loss_noforecast", "cyc_one_year"):
        _fy(conn, code, "2025-12-31", "2026-02-10T15:00:00+09:00", eps=-28.0, net_income=-5e10, shares_outstanding=1e9)
    assert auto_baseline(conn, code, D) is None
    k, why = unrated_reason(conn, code, D)
    assert k == kind and word in why


def test_split_flag_only_when_older_prices_not_refetched(conn):
    """全期間を取り直したときは調整後終値がそろっているので「再取得が必要」を記録しない。途中からの取得だけ記録する。"""
    from stock_analysis.ingest import ingest_quotes

    class P(DemoProvider):
        def daily_quotes(self, code, start, end):
            return [{"date": "2026-10-08", "open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0, "adj_close": 500.0, "adjustment_factor": 1.0},
                    {"date": "2026-10-09", "open": 500.0, "high": 500.0, "low": 500.0, "close": 500.0, "adj_close": 500.0, "adjustment_factor": 0.5}]
    code = "9903"
    _bare_stock(conn, code)
    q = "SELECT COUNT(*) FROM data_quality_issues WHERE code=? AND field='adjustment_factor'"
    ingest_quotes(conn, P(), code, date(2021, 1, 1), D)
    assert conn.execute(q, (code,)).fetchone()[0] == 0
    upsert(conn, "daily_quotes", {"code": code, "date": "2026-10-01", "close": 1000.0, "is_final": 1, "source": "t",
                                  "fetched_at": "x"}, ["code", "date"])
    ingest_quotes(conn, P(), code, date(2026, 10, 8), D)
    assert conn.execute(q, (code,)).fetchone()[0] == 1


def test_cyclical_auto_baseline_loss_year_and_structural_growth(conn):
    """景気連動：直近が赤字でも過去の純利益の中央値で正常化する（ルネサス型）。
    売上が年15%以上で伸びた会社は、利益率の中央値×今期予想売上で正常化し、予想純利益が空なら予想EPS×株数で補う（AIメカテック型）。"""
    from stock_analysis.assumptions import auto_baseline
    code = "9904"
    _bare_stock(conn, code)
    for y, ni in ((2021, 2e11), (2022, 3e11), (2023, 3.4e11), (2024, 2.2e11), (2025, -5e10)):
        _fy(conn, code, f"{y}-12-31", f"{y + 1}-02-10T15:00:00+09:00", revenue=1.3e12, net_income=ni,
            eps=ni / 1e9, shares_outstanding=1e9)
    a = auto_baseline(conn, code, D)
    assert a is not None and a.method == "normalized" and a.normalized_eps == pytest.approx(220.0)

    code = "9905"
    _bare_stock(conn, code)
    for y, rev, ni in ((2022, 1.5e10, 1.2e9), (2023, 1.5e10, 1e8), (2024, 2.1e10, 3e8), (2025, 3.5e10, 3.5e9)):
        _fy(conn, code, f"{y}-06-30", f"{y}-08-08T15:00:00+09:00", revenue=rev, net_income=ni, eps=ni / 1e7,
            shares_outstanding=1e7, forecast_revenue=4e10 if y == 2025 else None, forecast_eps=500.0 if y == 2025 else None,
            forecast_fy_end="2026-06-30" if y == 2025 else None)
    a = auto_baseline(conn, code, D)
    margins = [1.2e9 / 1.5e10, 1e8 / 1.5e10, 3e8 / 2.1e10, 3.5e9 / 3.5e10, 5e9 / 4e10]  # 最後は予想EPS×株数÷予想売上
    import statistics
    assert a.normalized_eps == pytest.approx(statistics.median(margins) * 4e10 / 1e7)
    assert "利益率" in a.reason and "補完" in a.reason


def test_sell_critical_event_scored_even_with_low_coverage():
    """売りの項目が半分そろわなくても、増資・下方修正が 80 点以上なら 0.8 倍を暫定の売り点数にする（ELEMENTS 型）。"""
    from unittest.mock import patch

    from stock_analysis.scoring import sell
    fs = {k: None for k in sell.WEIGHTS}
    fs["dilution"] = 100.0
    with patch.object(sell, "factor_scores", return_value=fs):
        r = sell.sell_score(None, None)
    assert r["score"] == 80 and r["floor_only"] and r["coverage"] < sell.MIN_COVERAGE
    fs["dilution"] = 70.0
    with patch.object(sell, "factor_scores", return_value=fs):
        assert sell.sell_score(None, None)["score"] is None


def test_v5_per_median_not_applied_with_anomaly(conn):
    """v5：過去5年PERに注意（赤字期間など）があれば中央値で置き換えない。"""
    from unittest.mock import patch

    from stock_analysis.v5 import per_history
    pers = [5.4] * 40
    with patch.object(per_history, "monthly_pers", return_value={"pers": pers, "loss_months": 12}):
        out = per_history.per_basis(conn, "9906", D, {}, None)
    assert out["median"] == 5.4 and out["applied"] is False
    with patch.object(per_history, "monthly_pers", return_value={"pers": [12.0] * 40, "loss_months": 0}):
        assert per_history.per_basis(conn, "9906", D, {}, None)["applied"] is True
