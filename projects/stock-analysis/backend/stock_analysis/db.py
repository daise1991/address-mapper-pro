"""SQLite スキーマとアクセス補助。

全ての外部データ行に source（取得元）/ fetched_at（取得日時）を持たせ、
財務データには対象決算期・公表日時も保存する。主キー upsert で重複を排除する。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import JST, get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS securities (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    group_name TEXT NOT NULL,            -- existing / added / emerging / user
    category TEXT NOT NULL,              -- stable / growth / cyclical / emerging_loss
    asset_type TEXT NOT NULL DEFAULT 'stock',  -- stock / etf
    active INTEGER NOT NULL DEFAULT 1,
    listing_status TEXT NOT NULL DEFAULT 'unverified',  -- unverified / listed / delisted / changed
    verified_at TEXT,
    verify_source TEXT,
    note TEXT,
    added_at TEXT NOT NULL,
    removed_at TEXT
);

CREATE TABLE IF NOT EXISTS market_calendar (
    date TEXT PRIMARY KEY,
    is_business_day INTEGER NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_quotes (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    adjustment_factor REAL NOT NULL DEFAULT 1.0,
    adj_close REAL,
    is_final INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS benchmark_quotes (
    name TEXT NOT NULL,                  -- TOPIX など
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (name, date)
);

CREATE TABLE IF NOT EXISTS financials (
    code TEXT NOT NULL,
    fiscal_period_end TEXT NOT NULL,     -- 対象決算期末 (YYYY-MM-DD)
    period_type TEXT NOT NULL,           -- FY / 1Q / 2Q / 3Q / REV（予想修正のみ）
    disclosed_at TEXT NOT NULL,          -- 公表日時
    revenue REAL, operating_profit REAL, net_income REAL,
    eps REAL, bps REAL, equity REAL, total_assets REAL,
    operating_cf REAL, cash REAL, interest_bearing_debt REAL,
    shares_outstanding REAL, treasury_shares REAL,
    dividend_per_share REAL,
    forecast_revenue REAL, forecast_operating_profit REAL, forecast_net_income REAL,
    forecast_eps REAL, forecast_dividend REAL,
    forecast_fy_end TEXT,                -- 予想値が対象とする決算期末
    document_type TEXT,
    source TEXT NOT NULL,
    source_ref TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (code, fiscal_period_end, period_type, disclosed_at)
);

-- 決算短信サマリーに無い項目を有価証券報告書（EDINET XBRL）から補完する
CREATE TABLE IF NOT EXISTS edinet_balance (
    code TEXT NOT NULL,
    period_end TEXT NOT NULL,          -- 決算期末
    interest_bearing_debt REAL,        -- 借入金・社債・CPの合計（リース債務は除く）
    components TEXT,                   -- 内訳 JSON（要素ID→金額）
    doc_id TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (code, period_end)
);

CREATE TABLE IF NOT EXISTS disclosures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    disclosed_at TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,  -- revision_up / revision_down / buyback / offering / dividend / orders / other
    url TEXT,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (code, disclosed_at, title)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,    -- ok / unavailable / error
    message TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_quality_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    date TEXT,
    field TEXT NOT NULL,
    issue TEXT NOT NULL,     -- missing / anomaly / inconsistent / stale
    detail TEXT,
    detected_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    UNIQUE (code, date, field, issue)
);

CREATE TABLE IF NOT EXISTS valuation_assumptions (
    code TEXT PRIMARY KEY,
    method TEXT NOT NULL,              -- per / dcf / normalized / asset / unrated
    target_year INTEGER NOT NULL,      -- 2028 or 2029 (決算年度)
    fiscal_month_end INTEGER NOT NULL DEFAULT 3,
    eps_forecast REAL,                 -- 目標年度の予想EPS
    assumed_per REAL,
    required_return REAL NOT NULL,
    safety_margin REAL NOT NULL,
    fcf_forecast REAL, terminal_growth REAL, net_cash_per_share REAL,
    normalized_eps REAL, bps_forecast REAL, target_pbr REAL,
    reason TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'manual',  -- manual / auto（自動初期推計。利用者が上書きする前提）
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS valuation_assumption_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    payload TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qualitative_inputs (
    code TEXT NOT NULL,
    factor TEXT NOT NULL,              -- market_expansion / competitive_advantage / growth_outlook
    score REAL NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (code, factor)
);

CREATE TABLE IF NOT EXISTS scores (
    run_date TEXT NOT NULL,
    code TEXT NOT NULL,
    price REAL,
    price_date TEXT,
    growth_score INTEGER,
    growth_detail TEXT,
    buy_score INTEGER,                 -- NULL = 未算定（0点とは区別）
    buy_score_k150 INTEGER,
    buy_status TEXT NOT NULL,          -- rated / unrated
    buy_detail TEXT,
    theoretical_value REAL,
    future_price REAL,
    target_price REAL,
    sell_score INTEGER,
    sell_detail TEXT,
    buy_decision TEXT,
    sell_decision TEXT,
    exit_type TEXT,                    -- profit_taking / stop_loss / NULL
    indicators TEXT,
    method_version TEXT,               -- 計算方式のバージョン（変更前後の比較用）
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_date, code)
);

CREATE TABLE IF NOT EXISTS verifications (
    score_date TEXT NOT NULL,
    code TEXT NOT NULL,
    horizon INTEGER NOT NULL,          -- 営業日
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    exit_date TEXT NOT NULL,
    forward_return REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (score_date, code, horizon)
);

CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    quantity REAL NOT NULL,
    avg_cost REAL NOT NULL,            -- 税金・損益試算専用。評価点には使わない
    account TEXT NOT NULL DEFAULT 'specific',  -- specific / general / nisa_growth
    acquired_at TEXT,
    note TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cash_balance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,              -- running / done / waiting / delayed / skipped / failed
    message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 重複通知防止。同じ key の通知は一度だけ送る
CREATE TABLE IF NOT EXISTS alert_log (
    key TEXT PRIMARY KEY,
    run_date TEXT NOT NULL,
    code TEXT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
"""


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else get_settings().db_path
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI は依存関数とエンドポイントを別スレッドで実行することがある。接続はリクエスト単位で共有しない
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL") if str(db_path) != ":memory:" else None
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# 既存DBに後から追加した列（CREATE TABLE IF NOT EXISTS では追加されないため）
MIGRATIONS = [("scores", "method_version", "TEXT"), ("financials", "forecast_fy_end", "TEXT")]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, typ in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    # 計算方式変更前の採点結果の保管先（scores と同じ列＋保管日時）
    conn.execute("CREATE TABLE IF NOT EXISTS scores_archive AS SELECT *, '' AS archived_at FROM scores WHERE 0")
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], keys: Iterable[str]) -> None:
    cols = list(row)
    keys = list(keys)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT ({', '.join(keys)}) DO "
        + (f"UPDATE SET {updates}" if updates else "NOTHING")
    )
    conn.execute(sql, [row[c] for c in cols])


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    upsert(conn, "settings", {"key": key, "value": json.dumps(value, ensure_ascii=False)}, ["key"])


def rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]
