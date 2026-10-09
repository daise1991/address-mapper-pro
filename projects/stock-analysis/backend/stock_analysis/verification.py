"""過去の採点結果と実際の株価の検証、売買ルールの精度比較。

- 将来リターンは調整後終値で計算（分割の影響を除く）
- 当日に保存したスコア（live）と、後から再計算したスコア（backfill）を区別する。
  backfill は評価前提が「現在の値」のため先読みバイアスを含み、参考値として扱う。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date
from statistics import mean
from typing import Any, Callable

from .db import now_iso, upsert
from .scoring.rules import RuleSet
from .scoring.valuation import buy_score_from_ratio, K_LEGACY

HORIZONS = (20, 60, 120, 250)  # 営業日（約1か月・3か月・半年・1年）


def _price_on_or_after(conn: sqlite3.Connection, code: str, after: str, offset: int) -> tuple[str, float] | None:
    r = conn.execute(
        """SELECT date, COALESCE(adj_close, close) AS c FROM daily_quotes
           WHERE code=? AND date>? AND close IS NOT NULL ORDER BY date LIMIT 1 OFFSET ?""",
        (code, after, offset - 1),
    ).fetchone()
    return (r["date"], r["c"]) if r else None


def compute_forward_returns(conn: sqlite3.Connection, horizons: tuple[int, ...] = HORIZONS) -> int:
    n = 0
    for s in conn.execute("SELECT run_date, code FROM scores WHERE price IS NOT NULL").fetchall():
        entry = conn.execute("SELECT COALESCE(adj_close, close) AS c FROM daily_quotes WHERE code=? AND date=?",
                             (s["code"], s["run_date"])).fetchone()
        if not entry or not entry["c"]:
            continue
        for h in horizons:
            if conn.execute("SELECT 1 FROM verifications WHERE score_date=? AND code=? AND horizon=?",
                            (s["run_date"], s["code"], h)).fetchone():
                continue
            ex = _price_on_or_after(conn, s["code"], s["run_date"], h)
            if not ex:
                continue
            upsert(conn, "verifications", {
                "score_date": s["run_date"], "code": s["code"], "horizon": h,
                "entry_price": entry["c"], "exit_price": ex[1], "exit_date": ex[0],
                "forward_return": ex[1] / entry["c"] - 1, "created_at": now_iso(),
            }, ["score_date", "code", "horizon"])
            n += 1
    conn.commit()
    return n


def _rank(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 5:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def _load(conn: sqlite3.Connection, horizon: int, live_only: bool) -> list[dict[str, Any]]:
    sql = """SELECT s.*, v.forward_return, json_extract(s.buy_detail, '$.v_over_p') AS v_over_p
             FROM scores s JOIN verifications v ON v.score_date=s.run_date AND v.code=s.code AND v.horizon=?"""
    if live_only:
        sql += " WHERE substr(s.created_at,1,10)=s.run_date"
    rows = [dict(r) for r in conn.execute(sql, (horizon,))]
    # 同日ユニバース平均との差（超過リターン）
    by_date: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_date[r["run_date"]].append(r["forward_return"])
    avg = {d: mean(v) for d, v in by_date.items()}
    for r in rows:
        r["excess_return"] = r["forward_return"] - avg[r["run_date"]]
    return rows


def _stats(rows: list[dict[str, Any]], sell_side: bool = False) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    rets = [r["forward_return"] for r in rows]
    ex = [r["excess_return"] for r in rows]
    hit = [x < 0 for x in ex] if sell_side else [x > 0 for x in ex]
    return {
        "n": len(rows),
        "mean_return": round(mean(rets), 4),
        "mean_excess": round(mean(ex), 4),
        "hit_rate": round(sum(hit) / len(hit), 3),
        "worst": round(min(rets), 4),
    }


def rule_signals(r: RuleSet) -> dict[str, tuple[Callable[[dict[str, Any]], bool], bool]]:
    def k150_buy(x: dict[str, Any]) -> int | None:
        return buy_score_from_ratio(x["v_over_p"], K_LEGACY) if x.get("v_over_p") is not None else None

    def normal(buy_key: Callable[[dict[str, Any]], int | None]) -> Callable[[dict[str, Any]], bool]:
        return lambda x: (x["growth_score"] or 0) >= r.normal_growth and (buy_key(x) or 0) >= r.normal_buy \
            and x["sell_score"] is not None and x["sell_score"] <= r.normal_sell_max

    return {
        "積極購入(k=200)": (lambda x: x["buy_decision"] == "aggressive", False),
        "通常購入以上(k=200)": (normal(lambda x: x["buy_score"]), False),
        "通常購入以上(k=150 比較)": (normal(k150_buy), False),
        "購入条件確認(65-74)": (lambda x: x["buy_decision"] == "check", False),
        "割安待ち": (lambda x: x["buy_decision"] == "wait_discount", False),
        "売却優先検討(85+)": (lambda x: x["sell_decision"] == "priority", True),
        "売却候補(75-84)": (lambda x: x["sell_decision"] == "candidate", True),
        "一部売却検討(65-74)": (lambda x: x["sell_decision"] == "partial", True),
        "利益確定候補": (lambda x: x["exit_type"] == "profit_taking", True),
        "損切り・撤退候補": (lambda x: x["exit_type"] == "stop_loss", True),
    }


def summary(conn: sqlite3.Connection, horizon: int = 60, live_only: bool = False, rules: RuleSet = RuleSet()) -> dict[str, Any]:
    rows = _load(conn, horizon, live_only)
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        b = r["buy_score"]
        key = "未算定" if b is None else "85+" if b >= 85 else "75-84" if b >= 75 else "65-74" if b >= 65 else "50-64" if b >= 50 else "0-49"
        buckets[key].append(r)
    rated = [r for r in rows if r["buy_score"] is not None]
    sold = [r for r in rows if r["sell_score"] is not None]
    grown = [r for r in rows if r["growth_score"] is not None]
    return {
        "horizon": horizon,
        "live_only": live_only,
        "n": len(rows),
        "ic": {
            "buy": spearman([r["buy_score"] for r in rated], [r["forward_return"] for r in rated]),
            "buy_v_over_p": spearman([r["v_over_p"] for r in rated if r["v_over_p"] is not None],
                                     [r["forward_return"] for r in rated if r["v_over_p"] is not None]),
            "sell": spearman([r["sell_score"] for r in sold], [r["forward_return"] for r in sold]),
            "growth": spearman([r["growth_score"] for r in grown], [r["forward_return"] for r in grown]),
        },
        "buy_buckets": {k: _stats(v) for k, v in sorted(buckets.items())},
        "rules": {name: _stats([r for r in rows if f(r)], sell_side) for name, (f, sell_side) in rule_signals(rules).items()},
        "note": "backfill を含む集計は評価前提が現在値のため先読みバイアスを含む。live_only=true が本来の検証値。",
    }


def backfill_scores(conn: sqlite3.Connection, start: date, end: date, step: int = 5) -> int:
    """過去営業日の採点を後から計算する（step 営業日おき）。"""
    from .analysis import run_scoring
    from .market_calendar import business_days

    days = business_days(start, end, conn)[::step]
    for d in days:
        run_scoring(conn, d)
    return len(days)
