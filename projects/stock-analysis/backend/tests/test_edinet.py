import io
import zipfile
from datetime import date

from stock_analysis.db import connect, upsert
from stock_analysis.providers.edinet import parse_interest_bearing_debt
from stock_analysis.scoring.features import build_features

HEADER = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "単位", "値"]


def _zip(rows: list[list[str]], name: str = "XBRL_TO_CSV/jpcrp030000-asr-001_E00000-000_2026-03-31_01_2026-06-20.csv") -> bytes:
    text = "\n".join("\t".join(r) for r in [HEADER, *rows])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text.encode("utf-16"))
        z.writestr("XBRL_TO_CSV/jpaud-aar-cn-001.csv", "\t".join(HEADER).encode("utf-16"))
    return buf.getvalue()


def row(elem: str, ctx: str, value: str) -> list[str]:
    return [elem, "", ctx, "当期末", "連結", "時点", "JPY", "円", value]


def test_jgaap_debt_sum_excludes_lease_and_prior_year():
    z = _zip([
        row("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant", "100"),
        row("jppfs_cor:CurrentPortionOfLongTermLoansPayable", "CurrentYearInstant", "20"),
        row("jppfs_cor:BondsPayable", "CurrentYearInstant", "300"),
        row("jppfs_cor:LongTermLoansPayable", "CurrentYearInstant", "400"),
        row("jppfs_cor:LeaseObligationsNCL", "CurrentYearInstant", "999"),
        row("jppfs_cor:LongTermLoansPayable", "Prior1YearInstant", "777"),
        row("jppfs_cor:ProceedsFromLongTermLoansPayableFinCF", "CurrentYearDuration", "50"),
    ])
    debt, parts = parse_interest_bearing_debt(z)
    assert debt == 820
    assert "LeaseObligationsNCL" not in parts


def test_ifrs_totals_and_nonconsolidated_fallback():
    ifrs = _zip([
        row("jpigp_cor:BondsAndBorrowingsCLIFRS", "CurrentYearInstant", "150"),
        row("jpigp_cor:BondsAndBorrowingsNCLIFRS", "CurrentYearInstant", "350"),
    ])
    assert parse_interest_bearing_debt(ifrs)[0] == 500
    nc = _zip([row("jppfs_cor:ShortTermLoansPayable", "CurrentYearInstant_NonConsolidatedMember", "10")])
    assert parse_interest_bearing_debt(nc)[0] == 10
    assert parse_interest_bearing_debt(_zip([]))[0] is None


def test_features_use_edinet_debt(tmp_path):
    conn = connect(tmp_path / "e.db")
    base = {"code": "6857", "period_type": "FY", "source": "test", "fetched_at": "x", "revenue": 1000.0,
            "operating_profit": 100.0, "net_income": 70.0, "equity": 500.0, "total_assets": 1000.0, "cash": 300.0,
            "interest_bearing_debt": None}
    upsert(conn, "financials", {**base, "fiscal_period_end": "2026-03-31", "disclosed_at": "2026-05-10T15:00:00+09:00"},
           ["code", "fiscal_period_end", "period_type", "disclosed_at"])
    upsert(conn, "edinet_balance", {"code": "6857", "period_end": "2026-03-31", "interest_bearing_debt": 100.0,
                                    "components": "{}", "doc_id": "S100TEST", "source": "edinet", "fetched_at": "x"},
           ["code", "period_end"])
    f = build_features(conn, "6857", date(2026, 10, 9), 1000, {})
    assert f.get("net_cash_to_assets") == 0.2
    assert "edinet:S100TEST" in f.sources
