from datetime import date

from stock_analysis.checks import check_jquants
from stock_analysis.providers.base import DataUnavailable


class FakeJQ:
    def __init__(self, delayed=False, rename=False):
        self.delayed = delayed
        self.rename = rename

    def _get(self, path, params):
        if path == "/markets/calendar":
            return [{"Date": "2026-10-09", "HolDiv": "1"}]
        if path == "/equities/master":
            return [{"Code": "68570", "CoName": "アドバンテスト"}]
        if path == "/equities/bars/daily":
            if self.delayed and "code" in params and params.get("from", "") > "2026-08-01":
                return []
            if self.delayed and "date" in params:
                raise DataUnavailable("403")
            row = {"Date": "2026-10-09", "C": 100, "Vo": 1, "AdjC": 100, "AdjFactor": 1}
            if self.rename:
                row = {"Date": "2026-10-09", "ClosePrice": 100}
            return [row]
        if path == "/indices/bars/daily/topix":
            return [{"Date": "2026-10-09", "C": 3000}]
        if path == "/fins/summary":
            return [{"DiscDate": "2026-05-12", "CurPerType": "FY", "DocType": "FY", "Sales": 1, "OP": 1, "NP": 1,
                     "EPS": 1, "Eq": 1, "TA": 1, "CurFYEn": "2026-03-31"}]
        return []


def _by(results):
    return {r["name"].split("（")[0]: r for r in results}


def test_check_ok_paid_plan():
    r = _by(check_jquants(FakeJQ(), date(2026, 10, 10)))
    assert r["当日終値の取得可否"]["status"] == "OK"
    assert r["決算サマリー"]["status"] == "OK"
    assert r["60銘柄の上場確認"]["status"] == "要確認"  # フェイクの一覧は1銘柄だけ


def test_check_detects_free_plan_delay_and_renamed_fields():
    r = _by(check_jquants(FakeJQ(delayed=True), date(2026, 10, 10)))
    assert r["当日終値の取得可否"]["status"] == "NG" and "無料プラン" in r["当日終値の取得可否"]["detail"]
    r2 = _by(check_jquants(FakeJQ(rename=True), date(2026, 10, 10)))
    assert r2["日足"]["status"] == "要確認" and "C / Close" in r2["日足"]["detail"]
