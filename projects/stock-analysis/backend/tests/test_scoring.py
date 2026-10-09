from datetime import date

import pytest

from stock_analysis.indicators import rsi, sma
from stock_analysis.scoring.features import Features, scale
from stock_analysis.scoring.rules import RuleSet, buy_decision, exit_type, sell_decision
from stock_analysis.scoring.sell import sell_score
from stock_analysis.scoring.valuation import Assumption, buy_score_from_ratio, evaluate, round_half_up, years_until


@pytest.mark.parametrize("ratio,k,expected", [
    (1.0, 200, 50), (1.25, 200, 100), (1.5, 200, 100), (0.75, 200, 0), (0.5, 200, 0),
    (1.1, 200, 70), (0.9, 200, 30), (1.1, 150, 65), (1.0025, 200, 51),  # 50.5 → 51（四捨五入）
])
def test_buy_score_formula(ratio, k, expected):
    assert buy_score_from_ratio(ratio, k) == expected


def test_round_half_up_not_bankers():
    assert round_half_up(0.5) == 1 and round_half_up(2.5) == 3 and round_half_up(70.49) == 70


def _a(**kw):
    base = dict(method="per", target_year=2029, required_return=0.10, safety_margin=0.15, reason="test",
                eps_forecast=200.0, assumed_per=20.0)
    base.update(kw)
    return Assumption(**base)


def test_future_value_and_target_price():
    as_of = date(2026, 3, 31)
    v = evaluate(_a(), price=3000.0, as_of=as_of)
    years = years_until(2029, as_of)
    assert v.status == "rated"
    assert v.future_price == 4000.0
    assert v.theoretical_value == pytest.approx(4000 / 1.1 ** years, abs=0.01)
    assert v.target_price == pytest.approx(v.theoretical_value * 0.85, abs=0.01)
    assert v.buy_score == buy_score_from_ratio(v.theoretical_value / 3000)
    assert v.buy_score_k150 == buy_score_from_ratio(v.theoretical_value / 3000, 150)


def test_loss_making_is_unrated_not_zero():
    v = evaluate(_a(eps_forecast=-50.0), price=1000.0, as_of=date(2026, 10, 9))
    assert v.status == "unrated" and v.buy_score is None
    assert "赤字" in v.reason


def test_missing_price_is_unrated():
    assert evaluate(_a(), price=None, as_of=date(2026, 10, 9)).buy_score is None


def test_dcf_and_asset_methods():
    d = evaluate(_a(method="dcf", fcf_forecast=100.0, terminal_growth=0.02, net_cash_per_share=50.0),
                 price=1000.0, as_of=date(2026, 10, 9))
    assert d.status == "rated" and d.future_price == pytest.approx(100 * 1.02 / 0.08 + 50)
    a = evaluate(_a(method="asset", bps_forecast=2000.0, target_pbr=0.8), price=1000.0, as_of=date(2026, 10, 9))
    assert a.future_price == 1600.0
    bad = evaluate(_a(method="dcf", fcf_forecast=100.0, terminal_growth=0.12), price=1000.0, as_of=date(2026, 10, 9))
    assert bad.status == "unrated"


def test_fiscal_month_changes_years():
    as_of = date(2026, 10, 9)
    assert years_until(2029, as_of, 12) > years_until(2029, as_of, 3)


@pytest.mark.parametrize("g,b,s,expected", [
    (85, 85, 30, "aggressive"),
    (85, 85, 31, "normal"),
    (80, 75, 40, "normal"),
    (80, 74, 40, "check"),
    (80, 65, 40, "check"),
    (90, 64, 20, "wait_discount"),
    (79, 90, 10, "none"),
    (80, 80, 41, "none"),
    (85, None, 10, "unrated"),
])
def test_buy_rules(g, b, s, expected):
    assert buy_decision(g, b, s) == expected


@pytest.mark.parametrize("s,expected", [
    (85, "priority"), (84, "candidate"), (75, "candidate"), (74, "partial"), (65, "partial"),
    (64, "review"), (40, "review"), (39, "hold"), (None, "unrated"),
])
def test_sell_rules(s, expected):
    assert sell_decision(s) == expected


def test_exit_type_distinguishes_profit_taking_and_stop_loss():
    rich = {"score": 80, "fundamental_side": 10.0, "factors": {"overvaluation": {"score": 100}}}
    broken = {"score": 80, "fundamental_side": 20.0, "factors": {"downward_revision": {"score": 90}}}
    assert exit_type(rich, growth=85) == "profit_taking"
    assert exit_type(broken, growth=85) == "stop_loss"
    assert exit_type(rich, growth=50) == "stop_loss"  # 成長前提の崩れ
    assert exit_type({"score": 50}, growth=85) is None


def test_sell_is_not_inverse_of_buy():
    """同じ割高度でも、業績・財務の状況で売り点数は変わる。"""
    healthy = Features("X", date(2026, 10, 9), 1000, {
        "forecast_op_growth": 0.2, "op_growth": 0.2, "operating_cf": 1, "equity_ratio": 0.6,
        "share_change": 0.0, "rsi14": 55, "dev25": 0.02, "disc_revision_down_120d": 0, "progress_gap": 0.05})
    weak = Features("Y", date(2026, 10, 9), 1000, {
        "forecast_op_growth": -0.2, "op_growth": 0.1, "operating_cf": -1, "equity_ratio": 0.15,
        "share_change": 0.08, "rsi14": 55, "dev25": 0.02, "disc_revision_down_120d": 1, "progress_gap": -0.2})
    s1 = sell_score(healthy, v_over_p=1.0)
    s2 = sell_score(weak, v_over_p=1.0)
    assert s1["score"] < 40 <= 65 <= s2["score"]
    assert exit_type(s2, growth=70) == "stop_loss"


def test_scale_and_indicators():
    assert scale(5, [(0, 0), (10, 100)]) == 50
    assert scale(-1, [(0, 0), (10, 100)]) == 0 and scale(99, [(0, 0), (10, 100)]) == 100
    assert sma([1, 2, 3, 4], 2) == 3.5 and sma([1], 5) is None
    assert rsi(list(range(1, 30))) == 100.0
    assert rsi([10] * 5) is None


def test_custom_ruleset_thresholds():
    r = RuleSet(normal_buy=70)
    assert buy_decision(80, 72, 30, r) == "normal"
