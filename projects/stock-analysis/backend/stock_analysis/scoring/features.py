"""採点用の特徴量を「その日時点で公表済みのデータだけ」から作る（先読み防止）。"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Sequence

from ..config import JST


def scale(x: float | None, points: Sequence[tuple[float, float]]) -> float | None:
    """区分線形の点数化。points は x 昇順の (x, score)。範囲外は端点で頭打ち。"""
    if x is None:
        return None
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _growth(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    if prev < 0:
        # 赤字からの改善は符号付きで扱えないため、改善/悪化のみ表現する
        return 1.0 if cur > prev else -1.0
    return cur / prev - 1


@dataclass
class Features:
    code: str
    as_of: date
    price: float | None = None
    values: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def get(self, key: str) -> Any:
        return self.values.get(key)


def _cutoff(as_of: date) -> str:
    # 大引け後分析なので当日 23:59 までの公表を含める
    return datetime.combine(as_of, time(23, 59, 59), JST).isoformat()


FORECAST_FIELDS = ("forecast_revenue", "forecast_operating_profit", "forecast_net_income", "forecast_eps", "forecast_dividend")


def latest_forecast(conn: sqlite3.Connection, code: str, after_fy_end: str, cutoff: str) -> dict[str, Any] | None:
    """after_fy_end より後の決算期を対象とする、cutoff 時点で最新の会社予想（四半期決算・予想修正を含む）。"""
    r = conn.execute(
        f"""SELECT {', '.join(FORECAST_FIELDS)}, forecast_fy_end, disclosed_at, source,
                   period_type, eps, shares_outstanding, treasury_shares FROM financials
            WHERE code=? AND forecast_fy_end>? AND disclosed_at<=?
              AND (forecast_operating_profit IS NOT NULL OR forecast_eps IS NOT NULL)
            ORDER BY forecast_fy_end ASC, disclosed_at DESC LIMIT 1""",
        (code, after_fy_end, cutoff)).fetchone()
    return dict(r) if r else None


def split_ratio(conn: sqlite3.Connection, code: str, after: str, until: str) -> float:
    """after〜until の間の株式分割・併合の倍率（J-Quants の調整係数の逆数の積。1:5 分割なら 5）。"""
    r = 1.0
    for row in conn.execute("""SELECT adjustment_factor FROM daily_quotes WHERE code=? AND date>? AND date<=?
                               AND adjustment_factor IS NOT NULL AND adjustment_factor!=1.0""",
                            (code, after[:10], until[:10])):
        if row["adjustment_factor"] > 0:
            r /= row["adjustment_factor"]
    return r



def current_shares(conn: sqlite3.Connection, code: str, row: Any, as_of: date) -> float:
    """決算の株数（期末時点・自己株除く）を評価日の株数基準に直す。純利益÷株数で1株あたりを出すときの共通処理。
    決算期末より後に効力が生じた分割だけを掛ける。株数がすでに分割後の基準で書き直されていれば
    （前の期の株数との比が分割倍率に近ければ）掛けない（二重補正の防止）。"""
    shares = (row["shares_outstanding"] or 0) - (row["treasury_shares"] or 0)
    s = split_ratio(conn, code, row["fiscal_period_end"], as_of.isoformat())
    if shares <= 0 or s == 1.0:
        return shares
    prev = conn.execute("""SELECT shares_outstanding FROM financials WHERE code=? AND fiscal_period_end<?
                           AND shares_outstanding>0 ORDER BY fiscal_period_end DESC, disclosed_at DESC LIMIT 1""",
                        (code, row["fiscal_period_end"])).fetchone()
    if prev and row["shares_outstanding"]:
        q = row["shares_outstanding"] / prev[0]
        if abs(math.log(q / s)) < abs(math.log(q)):
            return shares  # 株数はすでに分割後の基準
    return shares * s



def disclosure_observed(conn: sqlite3.Connection, code: str, as_of: date, days: int = 120) -> dict[str, bool]:
    """開示件数を「0件」と言えるか。見ていない期間の 0 件を 0 点にしない（データ不足として扱う）。
    revision: 下方・上方修正は会社予想の変化から作るため、直近約15か月に会社予想が無い会社は観測できない。
    offering: 増資等は EDINET の取得記録（fetch_log の edinet ok）が期間の始めまで遡れるときだけ観測済み。"""
    cutoff = f"{as_of.isoformat()}T23:59:59+09:00"
    rev = conn.execute(
        """SELECT 1 FROM financials WHERE code=? AND disclosed_at<=? AND disclosed_at>=?
             AND (forecast_operating_profit IS NOT NULL OR forecast_net_income IS NOT NULL OR forecast_eps IS NOT NULL) LIMIT 1""",
        (code, cutoff, (as_of - timedelta(days=450)).isoformat())).fetchone()
    first = conn.execute("SELECT MIN(target) FROM fetch_log WHERE source='edinet' AND status='ok'").fetchone()[0]
    return {"revision": rev is not None,
            "offering": bool(first) and first <= (as_of - timedelta(days=days)).isoformat()}


def forecast_split_factor(conn: sqlite3.Connection, code: str, fc: dict[str, Any], as_of: date) -> float:
    """会社予想（1株あたり）を評価日の株数基準に直す倍率。予想の公表日より後の分割だけを補正する。
    分割前に公表された予想でも、すでに分割後の基準で書かれていれば補正しない（二重補正の防止）。
    基準の判定：本決算の行は実績EPS÷予想EPS、四半期の行は 予想純利益÷予想EPS で出した株数÷発行済株式数。
    分割前の基準なら 1 前後、分割後の基準なら分割倍率前後になるので、近い方を採る。"""
    s = split_ratio(conn, code, fc["disclosed_at"], as_of.isoformat())
    fe = fc.get("forecast_eps")
    if s == 1.0 or not fe or fe <= 0:
        return 1 / s
    q = None
    if fc.get("period_type") == "FY" and fc.get("eps") and fc["eps"] > 0:
        q = fc["eps"] / fe
    else:
        shares = (fc.get("shares_outstanding") or 0) - (fc.get("treasury_shares") or 0)
        if fc.get("forecast_net_income") and fc["forecast_net_income"] > 0 and shares > 0:
            q = (fc["forecast_net_income"] / fe) / shares
    if q and q > 0 and abs(math.log(q / s)) < abs(math.log(q)):
        return 1.0  # 予想はすでに分割後の基準
    return 1 / s


def build_features(
    conn: sqlite3.Connection,
    code: str,
    as_of: date,
    price: float | None,
    indicators: dict[str, Any],
    eps_target: float | None = None,
    target_years: float | None = None,
) -> Features:
    f = Features(code=code, as_of=as_of, price=price)
    cutoff = _cutoff(as_of)
    fy = conn.execute(
        """SELECT * FROM financials WHERE code=? AND period_type='FY' AND disclosed_at<=?
           ORDER BY fiscal_period_end DESC, disclosed_at DESC""",
        (code, cutoff),
    ).fetchall()
    # 同一決算期の訂正は最新公表のみ使う
    seen, fy_rows = set(), []
    for r in fy:
        if r["fiscal_period_end"] in seen:
            continue
        seen.add(r["fiscal_period_end"])
        fy_rows.append(dict(r))
    v = f.values
    v.update(indicators)
    for row in fy_rows[:2]:
        if row["interest_bearing_debt"] is None:
            eb = conn.execute("SELECT interest_bearing_debt, doc_id FROM edinet_balance WHERE code=? AND period_end=?",
                              (code, row["fiscal_period_end"])).fetchone()
            if eb and eb["interest_bearing_debt"] is not None:
                row["interest_bearing_debt"] = eb["interest_bearing_debt"]
                f.sources.append(f"edinet:{eb['doc_id']}")
    if fy_rows:
        cur = fy_rows[0]
        prev = fy_rows[1] if len(fy_rows) > 1 else None
        f.sources.append(f"{cur['source']}:{cur['fiscal_period_end']}@{cur['disclosed_at']}")
        lf = latest_forecast(conn, code, cur["fiscal_period_end"], cutoff)
        fc_row = dict(cur)  # 予想の出どころ（分割補正は予想の公表日と基準で決める）
        if lf and lf["disclosed_at"] > cur["disclosed_at"]:  # 本決算後に予想が更新されていれば最新を使う
            cur.update({k: lf[k] for k in FORECAST_FIELDS if lf[k] is not None})
            f.sources.append(f"{lf['source']}:予想{lf['forecast_fy_end']}@{lf['disclosed_at']}")
            if lf["forecast_eps"] is not None:
                fc_row = dict(lf)
        v["fiscal_period_end"] = cur["fiscal_period_end"]
        v["revenue"] = cur["revenue"]
        v["operating_profit"] = cur["operating_profit"]
        v["eps"] = cur["eps"]
        v["bps"] = cur["bps"]
        if cur["revenue"]:
            v["op_margin"] = (cur["operating_profit"] or 0) / cur["revenue"]
        if cur["equity"]:
            v["roe"] = (cur["net_income"] or 0) / cur["equity"]
        if cur["total_assets"]:
            v["equity_ratio"] = (cur["equity"] or 0) / cur["total_assets"]
            if cur["cash"] is not None and cur["interest_bearing_debt"] is not None:
                v["net_cash_to_assets"] = (cur["cash"] - cur["interest_bearing_debt"]) / cur["total_assets"]
        v["operating_cf"] = cur["operating_cf"]
        v["revenue_growth"] = _growth(cur["revenue"], prev and prev["revenue"])
        v["op_growth"] = _growth(cur["operating_profit"], prev and prev["operating_profit"])
        v["forecast_revenue_growth"] = _growth(cur["forecast_revenue"], cur["revenue"])
        v["forecast_op_growth"] = _growth(cur["forecast_operating_profit"], cur["operating_profit"])
        if prev:
            if prev["interest_bearing_debt"] is not None and cur["interest_bearing_debt"] is not None and cur["total_assets"]:
                v["debt_change_to_assets"] = (cur["interest_bearing_debt"] - prev["interest_bearing_debt"]) / cur["total_assets"]
            if prev["shares_outstanding"] and cur["shares_outstanding"]:
                # 決算短信は株式分割後の基準で株数を書き直すことが多いため、分割倍率で割ってから希薄化を判定する
                ratio = cur["shares_outstanding"] / prev["shares_outstanding"]
                ratio /= split_ratio(conn, code, prev["disclosed_at"], cur["disclosed_at"])
                if ratio >= 1.9 and abs(ratio - round(ratio)) < 0.05:
                    # 整数倍の急増は株式分割の取り込み漏れとみなし、増資（希薄化）とは判定しない
                    f.sources.append(f"株数が{ratio:.1f}倍（分割の可能性。希薄化判定から除外）")
                else:
                    v["share_change"] = ratio - 1
        # 決算公表後に分割があれば、1株あたり指標を分割後の基準に直す
        adj = 1 / split_ratio(conn, code, cur["disclosed_at"], as_of.isoformat())
        if price and cur["eps"] and cur["eps"] > 0:
            v["per"] = price / (cur["eps"] * adj)
        if price and cur["bps"] and cur["bps"] > 0:
            v["pbr"] = price / (cur["bps"] * adj)
        if cur["forecast_eps"]:
            fc_adj = forecast_split_factor(conn, code, fc_row, as_of)
            base_eps = cur["forecast_eps"] * fc_adj
            if fc_adj != adj:
                f.sources.append(f"予想EPSは分割後の基準（補正倍率 {fc_adj:g}、実績は {adj:g}）")
        else:
            base_eps = (cur["eps"] or 0) * adj or None
        if eps_target and base_eps and base_eps > 0 and eps_target > 0 and target_years and target_years > 0.5:
            v["eps_cagr_to_target"] = (eps_target / base_eps) ** (1 / target_years) - 1

    # 四半期進捗率（会社予想に対する営業利益の累計進捗）
    q = conn.execute(
        """SELECT * FROM financials WHERE code=? AND period_type IN ('1Q','2Q','3Q') AND disclosed_at<=?
           ORDER BY disclosed_at DESC LIMIT 1""",
        (code, cutoff),
    ).fetchone()
    if q and q["forecast_operating_profit"] and q["forecast_operating_profit"] > 0 and q["operating_profit"] is not None:
        expected = {"1Q": 0.25, "2Q": 0.5, "3Q": 0.75}[q["period_type"]]
        v["progress_gap"] = q["operating_profit"] / q["forecast_operating_profit"] - expected

    since = datetime.combine(as_of - timedelta(days=120), time(0), JST).isoformat()
    obs = disclosure_observed(conn, code, as_of)
    for cat in ("revision_down", "revision_up", "offering", "buyback"):
        if not obs["revision" if cat.startswith("revision") else "offering"]:
            v[f"disc_{cat}_120d"] = None  # 観測していない（0件扱いにしない）
            continue
        v[f"disc_{cat}_120d"] = conn.execute(
            "SELECT COUNT(*) FROM disclosures WHERE code=? AND category=? AND disclosed_at BETWEEN ? AND ?",
            (code, cat, since, cutoff),
        ).fetchone()[0]

    for r in conn.execute("SELECT factor, score FROM qualitative_inputs WHERE code=?", (code,)):
        v[f"q_{r['factor']}"] = r["score"]
    return f
