from datetime import date, datetime

from stock_analysis.config import JST
from stock_analysis.db import connect, get_setting
from stock_analysis.screening import evaluate, run_monthly, screen, to_markdown
from stock_analysis.universe import seed_universe

D = date(2026, 10, 9)
C = {"market_keyword": "グロース", "min_revenue_growth": 0.2, "min_market_cap": 1e10, "min_turnover": 1e8,
     "turnover_days": 3, "min_listed_days": 365, "max_dilution": 0.1}


def fy(end, rev, shares, disc=None, op=1e8):
    return {"period_type": "FY", "fiscal_period_end": end, "disclosed_at": (disc or end) + "T15:00:00+09:00",
            "revenue": rev, "operating_profit": op, "shares_outstanding": shares, "treasury_shares": 0}


class Stub:
    def __init__(self):
        self.fin = {
            "1111": [fy("2025-03-31", 100e8, 1e7), fy("2026-03-31", 130e8, 1.05e7)],  # 合格（+30%、時価総額 2,100億）
            "2222": [fy("2025-03-31", 100e8, 1e7), fy("2026-03-31", 110e8, 1e7)],     # 伸び不足
            "3333": [fy("2025-03-31", 100e8, 1e7), fy("2026-03-31", 150e8, 1.3e7)],   # 株数 +30%
            "4444": [fy("2025-03-31", 100e8, 1e7), fy("2026-03-31", 150e8, 1e7)],     # 上場1年未満
            "9348": [fy("2025-03-31", 10e8, 1e7), fy("2026-03-31", 11e8, 1e7)],       # 今の新興株・伸び不足
        }

    def listed_all(self):
        return [{"code": c, "name": f"社{c}", "market": "グロース", "sector": "情報・通信業"} for c in ("1111", "2222", "3333", "4444")] + \
               [{"code": "5555", "name": "大手", "market": "プライム", "sector": "電機"}]

    def daily_quotes_by_date(self, d):
        if d.weekday() >= 5:
            return {}
        codes = ["1111", "2222", "3333", "9348", "5555"] + (["4444"] if d > date(2026, 1, 1) else [])
        return {c: {"close": 2000.0, "volume": 1e5} for c in codes}

    def daily_quotes(self, code, start, end):
        return [{"adjustment_factor": 1.0}]

    def financial_statements(self, code):
        return self.fin.get(code, [])


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    seed_universe(c)
    return c


def test_evaluate_rules():
    ev = evaluate([fy("2025-03-31", 100e8, 1e7), fy("2026-03-31", 125e8, 1e7)], 2000, 2e8, True, C)
    assert ev["passed"] and round(ev["revenue_growth"], 2) == 0.25 and ev["market_cap"] == 2e10
    ev = evaluate([fy("2026-03-31", 125e8, 1e7)], 2000, 2e8, True, C)  # 前年の本決算なし
    assert not ev["passed"] and "売上の伸び 取得不可" in ev["fails"]
    two = [fy("2025-03-31", 100e8, 1e7), fy("2026-03-31", 125e8, 2e7)]
    ev = evaluate(two, 2000, 2e8, True, C, split=2.0)  # 1:2 の株式分割 → 実質の株数増加は 0%
    assert ev["passed"] and ev["dilution"] == 0 and ev["notes"]
    ev = evaluate(two, 2000, 2e8, True, C, split=1.0)  # 分割なし → +100% の希薄化
    assert not ev["passed"] and "株数 +100%" in ev["fails"]
    ev = evaluate(two, 2000, 2e8, True, C, split=None)  # 分割を確認できない
    assert not ev["passed"] and "確認不可" in ev["fails"][0]


def test_screen_add_and_drop(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO settings (key, value) VALUES ('emerging_screen', '{\"turnover_days\": 3}')")
    rep = screen(conn, Stub(), D)
    assert rep["universe"] == 4 and rep["quote_days"] == 3
    assert [i["code"] for i in rep["add"]] == ["1111"]
    by = {i["code"]: i for i in rep["items"]}
    assert not by["2222"]["passed"] and not by["3333"]["passed"] and any("上場" in f for f in by["4444"]["fails"])
    assert "9348" in [i["code"] for i in rep["drop"]]
    md = to_markdown(rep)
    assert "1111 社1111" in md and "承認後" in md


def test_screen_does_not_change_universe(tmp_path):
    conn = _conn(tmp_path)
    before = conn.execute("SELECT code, active FROM securities ORDER BY code").fetchall()
    screen(conn, Stub(), D)
    assert conn.execute("SELECT code, active FROM securities ORDER BY code").fetchall() == before


def test_run_monthly_once_per_month(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    import stock_analysis.screening as sc
    monkeypatch.setattr(sc, "run_and_save", lambda conn, p, d, out=None: ({"add": [], "drop": []}, tmp_path / "r.md"))
    monkeypatch.setattr("stock_analysis.notify.notify", lambda *a, **k: 0)
    assert run_monthly(conn, Stub(), datetime(2026, 11, 1, 8, 0, tzinfo=JST)) is None  # 9時前
    assert run_monthly(conn, Stub(), datetime(2026, 11, 1, 9, 5, tzinfo=JST))
    assert get_setting(conn, "emerging_screen_state")["done"] == "2026-11"
    assert run_monthly(conn, Stub(), datetime(2026, 11, 20, 12, 0, tzinfo=JST)) is None  # 同じ月は1回
    assert run_monthly(conn, object(), datetime(2026, 12, 1, 9, 5, tzinfo=JST)) is None  # 全銘柄一覧の無い取得元
