"""v5 採点エンジン（v4 とは独立。結果は scores_v5 に保存し、v4 の scores は変更しない）。

正式評価の条件（すべて満たすときだけ正式ランキング・注文候補に使う）:
  成長期待点と売り点数がともに正式（充足率80%以上・必須項目あり）／ 買い点数が調査前提で算定済み ／
  予測信頼度 A または B ／ 評価前提が検証済み（承認済み）／ 想定PER（過去5年中央値）に異常の目印が無い
定性評価（市場拡大・競争優位）は検証済み（会社情報・市場・計算の3点、docs/qualitative-v2-spec.md）の項目だけ正式点数に使い、
それ以外は参考点数にだけ使う（正式点数では未評価。0点にはしない）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from .. import assumptions as auto_assumptions
from .. import research
from ..db import now_iso, upsert
from ..indicators import compute_all
from ..scoring import rules as v4rules
from ..scoring.valuation import years_until
from . import confidence, growth, per_history, qualitative, risk, sell, valuation
from .features import build, company_forecast_errors

VERSION = "2026.10-v5"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scores_v5 (
    run_date TEXT NOT NULL, code TEXT NOT NULL,
    price REAL,
    eval_status TEXT NOT NULL,            -- formal / provisional / unrated
    growth_formal INTEGER, growth_reference INTEGER, growth_coverage REAL, growth_detail TEXT,
    buy_score INTEGER, buy_score_k150 INTEGER, buy_score_no_dividend INTEGER, buy_status TEXT,
    theoretical_value REAL, theoretical_value_no_dividend REAL, dividend_pv REAL, target_price REAL,
    bear_downside REAL, buy_detail TEXT,
    sell_formal INTEGER, sell_reference INTEGER, sell_coverage REAL, sell_detail TEXT,
    severe_risks TEXT, confidence TEXT, confidence_points REAL, confidence_detail TEXT,
    buy_decision TEXT, sell_decision TEXT, exit_type TEXT, reevaluate INTEGER NOT NULL DEFAULT 0,
    sector TEXT, method_version TEXT NOT NULL, inputs_hash TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (run_date, code)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _price(conn: sqlite3.Connection, code: str, d: date) -> tuple[float | None, list[float]]:
    rows = conn.execute("""SELECT date, close, COALESCE(adj_close, close) AS c FROM daily_quotes
                           WHERE code=? AND date<=? AND close IS NOT NULL ORDER BY date DESC LIMIT 260""",
                        (code, d.isoformat())).fetchall()
    closes = [r["c"] for r in reversed(rows)]
    price = rows[0]["close"] if rows and rows[0]["date"] == d.isoformat() else None
    return price, closes


def _growth_drop(conn: sqlite3.Connection, code: str, d: date, current: int | None) -> float | None:
    if current is None:
        return None
    r = conn.execute("""SELECT growth_reference FROM scores_v5 WHERE code=? AND run_date<=? AND growth_reference IS NOT NULL
                        ORDER BY run_date DESC LIMIT 1""", (code, (d - timedelta(days=90)).isoformat())).fetchone()
    return max(0.0, r[0] - current) if r else None


def _cross_check(conn: sqlite3.Connection, r: dict[str, Any] | None, f) -> tuple[float | None, tuple[int, int] | None]:
    if not r:
        return None, None
    det = r.get("detail") or {}
    rf = (det.get("latest_company_forecast") or {}).get("eps")
    db_fc = f.get("forecast_eps")
    diff = (rf / db_fc - 1) if (rf and db_fc) else None
    fy = {row["fiscal_period_end"][:4]: row["net_income"] for row in conn.execute(
        "SELECT fiscal_period_end, net_income FROM financials WHERE code=? AND period_type='FY' AND net_income IS NOT NULL",
        (f.code,))}
    checked = matched = 0
    for h in det.get("history") or []:
        y = "".join(ch for ch in (h.get("fiscal_period") or "")[:4] if ch.isdigit())
        if h.get("net_income") is None or not fy.get(y):
            continue
        checked += 1
        matched += abs(h["net_income"] / fy[y] - 1) <= 0.05
    return diff, ((matched, checked) if checked else None)


def score_one(conn: sqlite3.Connection, code: str, d: date) -> dict[str, Any]:
    price, closes = _price(conn, code, d)
    ind = compute_all(closes) if price else {}
    f = build(conn, code, d, price, ind)
    sa = research.scenario_assumptions(conn, code, d)
    r = sa["research"] if sa else None
    qual = qualitative.scores_for(conn, code, d)

    # 想定PER（方針B）: 過去5年の実績PERの中央値（弱気=25%点、強気=75%点）。調査PERでの値は比較用に残す
    research_per = sa["assumptions"]["base"].assumed_per if sa else None
    pb = per_history.per_basis(conn, code, d, f.values, research_per)
    vals: dict[str, Any] = {}
    vals_research_per: dict[str, Any] = {}
    if sa:
        for k, a in sa["assumptions"].items():
            vals_research_per[k] = valuation.value(f, a, price, d)
            if pb["applied"]:
                sa["assumptions"][k] = replace(a, assumed_per=pb[per_history.SCENARIO_KEY[k]],
                                               reason=a.reason + f" ／ v5: 想定PERを過去5年{per_history.SCENARIO_KEY[k]} {pb[per_history.SCENARIO_KEY[k]]}倍に置換")
            vals[k] = valuation.value(f, sa["assumptions"][k], price, d)
        base_a = sa["assumptions"]["base"]
        base = vals["base"]
        buy_status = "rated" if base["buy_score"] is not None else "unrated"
        if (base_a.eps_forecast or base_a.normalized_eps or 0) <= 0:
            base = {**base, "loss_making": valuation.loss_making(r.get("detail"), price)}
            buy_status = "unrated"
    else:
        a, _ = auto_assumptions.resolve(conn, code, d)
        if a is not None and a.assumed_per and pb["applied"]:
            a = replace(a, assumed_per=pb["median"])
        base = valuation.value(f, a, price, d)
        base_a = a
        buy_status = "reference" if base["buy_score"] is not None else "unrated"  # 自動推計は参考値のみ
    eps_2029 = (base_a.eps_forecast or base_a.normalized_eps) if base_a else None
    years = years_until(base_a.target_year, d, base_a.fiscal_month_end) if base_a else None

    g = growth.score(f, qual, eps_2029, years)
    qf = qual["formal"]
    if any(qf[k] != qual.get(k) for k in qf):
        # 検証済みでない定性項目は正式点数の計算では未評価（参考点数には含める）
        strict = growth.score(f, {**qual, **qf}, eps_2029, years)
        g["formal_if_qualitative_verified"] = g["formal"]
        g.update({"formal": strict["formal"], "status": strict["status"], "coverage_formal_basis": strict["coverage"],
                  "qualitative_note": "定性評価のうち検証済みでない項目は、正式点数では未評価として計算"})
    g["qualitative"] = {"reference": {k: qual.get(k) for k in qf}, "formal": qf, "verification": qual["verification"],
                        "unevaluated": qual["unevaluated"], "errors": qual["errors"], "origin": qual["origin"]}
    s = sell.score(f, base.get("v_over_p"), _growth_drop(conn, code, d, g["reference"]), qual.get("growth_outlook"))
    risks = risk.detect(f)
    diff, hist_match = _cross_check(conn, r, f)
    conf = confidence.grade(
        coverage=min(g["coverage"], s["coverage"]), has_price=price is not None,
        has_company_data=bool(f.get("history_years")), has_scenarios=sa is not None,
        forecast_eps_diff=diff, history_match=hist_match,
        research_date=(r.get("approved_at") or r.get("updated_at")) if r else None, as_of=d,
        scenario_values={k: v.get("theoretical_value") for k, v in vals.items()} if vals else None,
        forecast_errors=company_forecast_errors(conn, code, d))

    formal = (g["status"] == "formal" and s["status"] == "formal" and buy_status == "rated"
              and conf["grade"] in ("A", "B") and r is not None and r.get("status") == "verified"
              and pb["applied"] and not pb["anomalies"])
    eval_status = "formal" if formal else ("provisional" if g["reference"] is not None else "unrated")

    rs = v4rules.RuleSet()
    sell_for_decision = s["formal"] if s["formal"] is not None else s["reference"]
    buy_dec = v4rules.buy_decision(g["formal"], base.get("buy_score"), s["formal"], rs) if formal else None
    if buy_dec in ("aggressive", "normal") and risk.blocks_purchase(risks):
        buy_dec = "blocked_by_risk"
    sell_dec = v4rules.sell_decision(sell_for_decision, rs)
    fund = [it["score"] for k, it in s["items"].items() if k not in ("overvaluation", "overheating") and it["score"] is not None]
    sell_like = {"score": sell_for_decision, "fundamental_side": (sum(fund) / len(fund)) if fund else 0.0,
                 "factors": {k: {"score": it["score"]} for k, it in s["items"].items()}}
    exit_t = "stop_loss" if risks else v4rules.exit_type(sell_like, g["formal"] or g["reference"], rs)

    bear_v = vals.get("bear", {}).get("theoretical_value") if vals else None
    sector = conn.execute("SELECT sector FROM securities WHERE code=?", (code,)).fetchone()
    row = {
        "run_date": d.isoformat(), "code": code, "price": price, "eval_status": eval_status,
        "growth_formal": g["formal"], "growth_reference": g["reference"], "growth_coverage": g["coverage"],
        "growth_detail": json.dumps(g, ensure_ascii=False),
        "buy_score": base.get("buy_score") if buy_status == "rated" else None,
        "buy_score_k150": base.get("buy_score_k150") if buy_status == "rated" else None,
        "buy_score_no_dividend": base.get("buy_score_no_dividend"),
        "buy_status": buy_status,
        "theoretical_value": base.get("theoretical_value"), "theoretical_value_no_dividend": base.get("theoretical_value_no_dividend"),
        "dividend_pv": (base.get("dividend") or {}).get("pv"), "target_price": base.get("target_price"),
        "bear_downside": round(bear_v / price - 1, 4) if (bear_v and price) else None,
        "buy_detail": json.dumps({"base": base, "scenarios": vals, "reference_buy_score": base.get("buy_score"),
                                  "assumption_status": r.get("status") if r else "unconfirmed",
                                  "anchor_ratio": sa["anchor_ratio"] if sa else None,
                                  "per_basis": pb,
                                  "research_per": {"base_per": research_per,
                                                   "buy_score": (vals_research_per.get("base") or {}).get("buy_score")}}, ensure_ascii=False, default=str),
        "sell_formal": s["formal"], "sell_reference": s["reference"], "sell_coverage": s["coverage"],
        "sell_detail": json.dumps(s, ensure_ascii=False),
        "severe_risks": json.dumps(risks, ensure_ascii=False),
        "confidence": conf["grade"], "confidence_points": conf["points"], "confidence_detail": json.dumps(conf, ensure_ascii=False),
        "buy_decision": buy_dec, "sell_decision": sell_dec, "exit_type": exit_t if sell_for_decision is not None and (risks or sell_for_decision >= 65) else None,
        "reevaluate": int(bool(risks)), "sector": sector["sector"] if sector else None,
        "method_version": VERSION,
        "inputs_hash": _stable_hash({"f": f.values, "q": qual, "a": str(base_a)}),  # 同一入力の検証用
        "created_at": now_iso(),
    }
    upsert(conn, "scores_v5", row, ["run_date", "code"])
    return row


def _stable_hash(obj: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def run(conn: sqlite3.Connection, d: date, codes: list[str] | None = None) -> list[dict[str, Any]]:
    from ..universe import active_codes
    ensure_schema(conn)
    out = [score_one(conn, c, d) for c in (codes or active_codes(conn))]
    conn.commit()
    return out
