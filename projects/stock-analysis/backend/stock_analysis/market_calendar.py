"""東証営業日判定。

優先順位: 1) market_calendar テーブル（J-Quants 取引カレンダー＝JPX公式データ）
          2) フォールバック: 土日・祝日（jpholiday）・年末年始(12/31〜1/3) を休場とみなす
フォールバックを使った場合は呼び出し側に source を返し、ジョブログに残す。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

try:
    import jpholiday
except ImportError:  # pragma: no cover
    jpholiday = None


def _fallback_is_business_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return False
    if jpholiday is not None and jpholiday.is_holiday(d):
        return False
    return True


def is_business_day(d: date, conn: sqlite3.Connection | None = None) -> tuple[bool, str]:
    if conn is not None:
        row = conn.execute(
            "SELECT is_business_day, source FROM market_calendar WHERE date=?", (d.isoformat(),)
        ).fetchone()
        if row is not None:
            return bool(row["is_business_day"]), row["source"]
    return _fallback_is_business_day(d), "fallback(jpholiday)"


def previous_business_day(d: date, conn: sqlite3.Connection | None = None) -> date:
    cur = d - timedelta(days=1)
    while not is_business_day(cur, conn)[0]:
        cur -= timedelta(days=1)
    return cur


def next_business_day(d: date, conn: sqlite3.Connection | None = None) -> date:
    cur = d + timedelta(days=1)
    while not is_business_day(cur, conn)[0]:
        cur += timedelta(days=1)
    return cur


def business_days(start: date, end: date, conn: sqlite3.Connection | None = None) -> list[date]:
    out, cur = [], start
    while cur <= end:
        if is_business_day(cur, conn)[0]:
            out.append(cur)
        cur += timedelta(days=1)
    return out
