"""データ取得・保存・品質チェック。

- 主キー upsert で重複排除（再取得は同じ行を上書き）
- 取得失敗は fetch_log に status='unavailable' で記録。既存の古い行を「最新」として流用しない
- 異常値（前日比±30%超で分割調整なし、OHLC不整合、出来高0）を data_quality_issues に記録
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from .db import now_iso, upsert
from .market_calendar import business_days
from .providers.base import DataUnavailable, Provider, classify_disclosure

PRICE_JUMP_THRESHOLD = 0.30


def log_fetch(conn: sqlite3.Connection, run_id: str | None, source: str, target: str, status: str, message: str = "") -> None:
    conn.execute(
        "INSERT INTO fetch_log (run_id, source, target, status, message, fetched_at) VALUES (?,?,?,?,?,?)",
        (run_id, source, target, status, message[:500], now_iso()),
    )


def record_issue(conn: sqlite3.Connection, code: str, d: str | None, field: str, issue: str, detail: str) -> None:
    conn.execute(
        """INSERT INTO data_quality_issues (code, date, field, issue, detail, detected_at) VALUES (?,?,?,?,?,?)
           ON CONFLICT (code, date, field, issue) DO UPDATE SET detail=excluded.detail, detected_at=excluded.detected_at""",
        (code, d, field, issue, detail, now_iso()),
    )


def sync_calendar(conn: sqlite3.Connection, provider: Provider, start: date, end: date, run_id: str | None = None) -> bool:
    try:
        rows = provider.trading_calendar(start, end)
    except DataUnavailable as e:
        log_fetch(conn, run_id, provider.name, "calendar", "unavailable", str(e))
        conn.commit()
        return False
    ts = now_iso()
    for r in rows:
        upsert(conn, "market_calendar", {"date": r["date"], "is_business_day": int(r["is_business_day"]),
                                         "source": provider.name, "fetched_at": ts}, ["date"])
    log_fetch(conn, run_id, provider.name, "calendar", "ok", f"{len(rows)} days")
    conn.commit()
    return True


def verify_listings(conn: sqlite3.Connection, provider: Provider, codes: list[str], run_id: str | None = None) -> list[dict[str, Any]]:
    try:
        info = provider.listed_info(codes)
    except DataUnavailable as e:
        log_fetch(conn, run_id, provider.name, "listed_info", "unavailable", str(e))
        conn.commit()
        return []
    ts = now_iso()
    for r in info:
        conn.execute(
            "UPDATE securities SET listing_status=?, verified_at=?, verify_source=?, sector=COALESCE(?, sector) WHERE code=?",
            ("listed" if r["listed"] else "delisted", ts, provider.name, r.get("sector"), r["code"]),
        )
    log_fetch(conn, run_id, provider.name, "listed_info", "ok", f"{len(info)} codes")
    conn.commit()
    return info


def check_quotes(conn: sqlite3.Connection, code: str, rows: list[dict[str, Any]], prev_close: float | None) -> None:
    for r in rows:
        d = r["date"]
        o, h, l, c = r["open"], r["high"], r["low"], r["close"]
        if c is None:
            record_issue(conn, code, d, "close", "missing", "終値なし（売買不成立の可能性）")
        elif None not in (o, h, l) and not (l <= min(o, c) and max(o, c) <= h):
            record_issue(conn, code, d, "ohlc", "inconsistent", f"O={o} H={h} L={l} C={c}")
        if r.get("volume") == 0:
            record_issue(conn, code, d, "volume", "anomaly", "出来高0")
        if c is not None and prev_close:
            change = c / prev_close - 1
            if abs(change) > PRICE_JUMP_THRESHOLD and (r.get("adjustment_factor") or 1.0) == 1.0:
                record_issue(conn, code, d, "close", "anomaly", f"前日比 {change:+.1%}（分割調整なし）")
        if c is not None:
            prev_close = c


def ingest_quotes(conn: sqlite3.Connection, provider: Provider, code: str, start: date, end: date,
                  run_id: str | None = None, fetch_code: str | None = None) -> int:
    """fetch_code を指定すると、そのコードで取得して code として保存する（コード変更前の旧コード連結用）。"""
    try:
        rows = provider.daily_quotes(fetch_code or code, start, end)
    except DataUnavailable as e:
        log_fetch(conn, run_id, provider.name, f"quotes:{code}", "unavailable", str(e))
        conn.commit()
        return 0
    prev = conn.execute("SELECT close FROM daily_quotes WHERE code=? AND date<? ORDER BY date DESC LIMIT 1",
                        (code, start.isoformat())).fetchone()
    check_quotes(conn, code, rows, prev["close"] if prev else None)
    ts = now_iso()
    for r in rows:
        upsert(conn, "daily_quotes", {
            "code": code, **{k: r.get(k) for k in ("date", "open", "high", "low", "close", "volume", "adjustment_factor", "adj_close")},
            "is_final": 1, "source": f"{provider.name}(旧{fetch_code})" if fetch_code else provider.name, "fetched_at": ts,
        }, ["code", "date"])
    # 分割・併合があれば過去の調整後終値を更新させるため再取得を促す
    if any((r.get("adjustment_factor") or 1.0) != 1.0 for r in rows):
        record_issue(conn, code, end.isoformat(), "adjustment_factor", "anomaly", "株式分割・併合を検出。全期間の再取得が必要")
    log_fetch(conn, run_id, provider.name, f"quotes:{code}", "ok", f"{len(rows)} rows")
    conn.commit()
    return len(rows)


def ingest_financials(conn: sqlite3.Connection, provider: Provider, code: str, run_id: str | None = None,
                      rows: list[dict[str, Any]] | None = None) -> int:
    if rows is None:
        try:
            rows = provider.financial_statements(code)
        except DataUnavailable as e:
            log_fetch(conn, run_id, provider.name, f"financials:{code}", "unavailable", str(e))
            conn.commit()
            return 0
    ts = now_iso()
    for r in rows:
        upsert(conn, "financials", {"code": code, **r, "source": provider.name, "fetched_at": ts},
               ["code", "fiscal_period_end", "period_type", "disclosed_at"])
    detect_forecast_revisions(conn, code)
    log_fetch(conn, run_id, provider.name, f"financials:{code}", "ok", f"{len(rows)} rows")
    conn.commit()
    return len(rows)


REVISION_THRESHOLD = 0.03  # 予想値の変化が3%未満なら修正イベントとして扱わない


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return new / abs(old) - 1 if old > 0 else (1.0 if new > old else -1.0)


def detect_forecast_revisions(conn: sqlite3.Connection, code: str) -> int:
    """会社予想の変化（決算短信・予想修正の開示）から、業績・配当予想の修正イベントを disclosures に登録する。

    同じ対象決算期の直前の予想と比較し、営業利益（無ければ純利益）予想が3%以上動いたら上方/下方修正とする。
    """
    rows = conn.execute(
        """SELECT * FROM financials WHERE code=? AND forecast_fy_end IS NOT NULL
           ORDER BY forecast_fy_end, disclosed_at""", (code,)).fetchall()
    n = 0
    prev: dict[str, sqlite3.Row] = {}
    for r in rows:
        p = prev.get(r["forecast_fy_end"])
        prev[r["forecast_fy_end"]] = r
        if p is None:
            continue
        events = []
        key, label = ("forecast_operating_profit", "営業利益") if r["forecast_operating_profit"] is not None \
            and p["forecast_operating_profit"] is not None else ("forecast_net_income", "純利益")
        chg = _pct(r[key], p[key])
        if chg is not None and abs(chg) >= REVISION_THRESHOLD:
            events.append(("revision_up" if chg > 0 else "revision_down",
                           f"業績予想の{'上方' if chg > 0 else '下方'}修正（{r['forecast_fy_end'][:7]}期 {label}予想 "
                           f"{p[key] / 1e8:,.0f}億→{r[key] / 1e8:,.0f}億, {chg:+.0%}）"))
        dchg = _pct(r["forecast_dividend"], p["forecast_dividend"])
        if dchg is not None and abs(dchg) >= REVISION_THRESHOLD:
            events.append(("dividend", f"配当予想の{'増額' if dchg > 0 else '減額'}修正（{p['forecast_dividend']}円→{r['forecast_dividend']}円）"))
        for cat, title in events:
            cur = conn.execute(
                """INSERT INTO disclosures (code, disclosed_at, title, category, url, source, fetched_at)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT (code, disclosed_at, title) DO NOTHING""",
                (code, r["disclosed_at"], title, cat, r["source_ref"], f"{r['source']}:forecast", now_iso()))
            n += cur.rowcount
    conn.commit()
    return n


def ingest_quotes_bulk(conn: sqlite3.Connection, provider: Provider, codes: list[str], d: date,
                       run_id: str | None = None) -> list[str] | None:
    """日付指定で全銘柄を一括取得する。対応しないプロバイダは None、取れなかった銘柄はリストで返す。"""
    try:
        by_code = provider.daily_quotes_by_date(d)
    except DataUnavailable as e:
        log_fetch(conn, run_id, provider.name, f"quotes_bulk:{d}", "unavailable", str(e))
        conn.commit()
        return list(codes)
    if by_code is None:
        return None
    ts = now_iso()
    for code in codes:
        r = by_code.get(code)
        if r is None:
            continue
        prev = conn.execute("SELECT close FROM daily_quotes WHERE code=? AND date<? ORDER BY date DESC LIMIT 1",
                            (code, d.isoformat())).fetchone()
        check_quotes(conn, code, [r], prev["close"] if prev else None)
        upsert(conn, "daily_quotes", {
            "code": code, **{k: r.get(k) for k in ("date", "open", "high", "low", "close", "volume", "adjustment_factor", "adj_close")},
            "is_final": 1, "source": provider.name, "fetched_at": ts,
        }, ["code", "date"])
        if (r.get("adjustment_factor") or 1.0) != 1.0:
            record_issue(conn, code, d.isoformat(), "adjustment_factor", "anomaly", "株式分割・併合を検出。全期間を再取得")
    missing = [c for c in codes if c not in by_code]
    log_fetch(conn, run_id, provider.name, f"quotes_bulk:{d}", "ok", f"{len(codes) - len(missing)}/{len(codes)} codes")
    conn.commit()
    return missing


def ingest_financials_by_date(conn: sqlite3.Connection, provider: Provider, codes: list[str], d: date,
                              run_id: str | None = None) -> bool:
    """指定日に開示された決算・予想修正を全銘柄分まとめて取得する。対応しなければ False。"""
    try:
        by_code = provider.financial_statements_by_date(d)
    except DataUnavailable as e:
        log_fetch(conn, run_id, provider.name, f"financials_bulk:{d}", "unavailable", str(e))
        conn.commit()
        return True
    if by_code is None:
        return False
    hit = 0
    for code in codes:
        if by_code.get(code):
            ingest_financials(conn, provider, code, run_id, rows=by_code[code])
            hit += 1
    log_fetch(conn, run_id, provider.name, f"financials_bulk:{d}", "ok", f"{hit} codes disclosed")
    conn.commit()
    return True


def ingest_topix(conn: sqlite3.Connection, provider: Provider, start: date, end: date, run_id: str | None = None) -> int:
    try:
        rows = provider.topix_daily(start, end)
    except DataUnavailable as e:
        log_fetch(conn, run_id, provider.name, "topix", "unavailable", str(e))
        conn.commit()
        return 0
    ts = now_iso()
    for r in rows:
        upsert(conn, "benchmark_quotes", {"name": "TOPIX", **r, "source": provider.name, "fetched_at": ts}, ["name", "date"])
    log_fetch(conn, run_id, provider.name, "topix", "ok", f"{len(rows)} rows")
    conn.commit()
    return len(rows)


def ingest_disclosures(conn: sqlite3.Connection, provider: Provider, code: str, start: date, end: date,
                       run_id: str | None = None, rows: list[dict[str, Any]] | None = None, source: str | None = None) -> int:
    if rows is None:
        try:
            rows = provider.disclosures(code, start, end)
        except DataUnavailable as e:
            log_fetch(conn, run_id, provider.name, f"disclosures:{code}", "unavailable", str(e))
            conn.commit()
            return 0
    ts = now_iso()
    for r in rows:
        conn.execute(
            """INSERT INTO disclosures (code, disclosed_at, title, category, url, source, fetched_at) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (code, disclosed_at, title) DO NOTHING""",
            (code, r["disclosed_at"], r["title"], r.get("category") or classify_disclosure(r["title"]), r.get("url"),
             source or provider.name, ts),
        )
    conn.commit()
    return len(rows)


def quotes_complete(conn: sqlite3.Connection, codes: list[str], d: date) -> tuple[bool, list[str]]:
    """当日の確定終値が全銘柄そろっているか。欠けている銘柄を返す。"""
    have = {r["code"] for r in conn.execute(
        "SELECT code FROM daily_quotes WHERE date=? AND is_final=1 AND close IS NOT NULL", (d.isoformat(),))}
    missing = [c for c in codes if c not in have]
    return not missing, missing


def detect_gaps(conn: sqlite3.Connection, code: str, start: date, end: date) -> list[str]:
    have = {r["date"] for r in conn.execute("SELECT date FROM daily_quotes WHERE code=? AND date BETWEEN ? AND ?",
                                            (code, start.isoformat(), end.isoformat()))}
    gaps = [d.isoformat() for d in business_days(start, end, conn) if d.isoformat() not in have]
    for g in gaps[-20:]:
        record_issue(conn, code, g, "quote", "missing", "営業日の株価なし")
    conn.commit()
    return gaps


def backfill_start(conn: sqlite3.Connection, code: str, end: date, years: int = 2) -> date:
    row = conn.execute("SELECT MAX(date) AS d FROM daily_quotes WHERE code=?", (code,)).fetchone()
    if row and row["d"]:
        return date.fromisoformat(row["d"]) - timedelta(days=7)  # 直近1週間は取り直して訂正を反映
    return end - timedelta(days=365 * years)
