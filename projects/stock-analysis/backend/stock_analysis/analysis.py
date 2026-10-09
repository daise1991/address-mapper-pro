"""1営業日分の3種類の採点と売買判定を計算し scores に保存する。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import assumptions
from .db import get_setting, now_iso, upsert
from .indicators import compute_all
from .scoring.features import build_features
from .scoring.growth import growth_score
from .scoring.rules import RuleSet, buy_decision, exit_type, sell_decision
from .scoring.sell import sell_score
from .scoring.valuation import evaluate, years_until
from .universe import active_codes


def load_rules(conn: sqlite3.Connection) -> RuleSet:
    overrides = get_setting(conn, "rules", {}) or {}
    return RuleSet(**overrides)


def closes_until(conn: sqlite3.Connection, code: str, d: date, n: int = 260) -> list[tuple[str, float]]:
    """調整後終値（無ければ終値）を古い順に返す。"""
    rows = conn.execute(
        """SELECT date, COALESCE(adj_close, close) AS c FROM daily_quotes
           WHERE code=? AND date<=? AND close IS NOT NULL ORDER BY date DESC LIMIT ?""",
        (code, d.isoformat(), n),
    ).fetchall()
    return [(r["date"], r["c"]) for r in reversed(rows)]


def _growth_drop(conn: sqlite3.Connection, code: str, d: date, current: int | None) -> float | None:
    if current is None:
        return None
    r = conn.execute(
        """SELECT growth_score FROM scores WHERE code=? AND run_date<=? AND growth_score IS NOT NULL
           ORDER BY run_date DESC LIMIT 1""",
        (code, (d - timedelta(days=90)).isoformat()),
    ).fetchone()
    return max(0.0, r["growth_score"] - current) if r else None


def score_one(conn: sqlite3.Connection, code: str, d: date, rules: RuleSet) -> dict[str, Any]:
    series = closes_until(conn, code, d)
    # 当日の確定終値が無い銘柄は株価依存の点数を出さない（古い株価で代用しない）
    price = series[-1][1] if series and series[-1][0] == d.isoformat() else None
    raw_close = conn.execute("SELECT close FROM daily_quotes WHERE code=? AND date=?", (code, d.isoformat())).fetchone()
    if raw_close and raw_close["close"]:
        price = raw_close["close"]
    ind = compute_all([c for _, c in series]) if price else {}

    a, origin = assumptions.resolve(conn, code, d)
    val = evaluate(a, price, d)
    target_eps = (a.eps_forecast or a.normalized_eps) if a else None
    years = years_until(a.target_year, d, a.fiscal_month_end) if a else None
    feats = build_features(conn, code, d, price, ind, target_eps, years)

    g = growth_score(feats)
    s = sell_score(feats, val.v_over_p, _growth_drop(conn, code, d, g["score"])) if price else {"score": None, "factors": {}}
    buy = val.buy_score
    bd = buy_decision(g["score"], buy, s["score"], rules)
    sd = sell_decision(s["score"], rules)
    et = exit_type(s, g["score"], rules)
    row = {
        "run_date": d.isoformat(), "code": code,
        "price": price, "price_date": d.isoformat() if price else None,
        "growth_score": g["score"], "growth_detail": json.dumps(g, ensure_ascii=False),
        "buy_score": buy, "buy_score_k150": val.buy_score_k150, "buy_status": val.status,
        "buy_detail": json.dumps({**val.to_dict(), "assumption_origin": origin}, ensure_ascii=False, default=str),
        "theoretical_value": val.theoretical_value, "future_price": val.future_price, "target_price": val.target_price,
        "sell_score": s["score"], "sell_detail": json.dumps(s, ensure_ascii=False),
        "buy_decision": bd, "sell_decision": sd, "exit_type": et,
        "indicators": json.dumps({**ind, "sources": feats.sources}, ensure_ascii=False),
        "created_at": now_iso(),
    }
    upsert(conn, "scores", row, ["run_date", "code"])
    return row


def run_scoring(conn: sqlite3.Connection, d: date, codes: list[str] | None = None) -> list[dict[str, Any]]:
    rules = load_rules(conn)
    out = [score_one(conn, c, d, rules) for c in (codes or active_codes(conn))]
    conn.commit()
    return out
