"""大引け後の自動分析（JST）。

  15:30 東証取引終了
  16:00 fetch    終値・出来高・企業開示の取得開始（J-Quants の日足更新は 16:30 頃のため、揃うまで再試行）
  16:30 score    評価計算
  17:00 verify   過去予測の検証
  17:30 finalize ランキング・売買候補の確定
  18:00 publish  Webアプリ更新・プッシュ通知

- 東証営業日のみ実行（J-Quants 取引カレンダー＝JPX公式。未取得時は祝日ライブラリでフォールバック）
- 全銘柄の当日確定終値がそろわない限り score 以降を実行しない。締切（既定 20:00）を過ぎたら
  「データ遅延」として通知し、その日の分析は未確定のまま残す（古い株価で代用しない）。
  遅延後もスケジューラは再取得を続け、そろった時点で残りの段階を実行する
- 各段階は冪等。スケジューラから数分おきに呼び、時刻と前段の完了で進む
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

from .analysis import run_scoring
from .config import JST, get_settings
from .db import get_setting, now_iso, rows, set_setting
from .ingest import (backfill_start, detect_gaps, ingest_disclosures, ingest_financials, ingest_quotes,
                     quotes_complete, sync_calendar, verify_listings)
from .market_calendar import is_business_day
from .notify import notify
from .providers.base import DataUnavailable, Provider
from .universe import active_codes
from .verification import compute_forward_returns, summary

STAGES: list[tuple[str, time]] = [
    ("fetch", time(16, 0)),
    ("score", time(16, 30)),
    ("verify", time(17, 0)),
    ("finalize", time(17, 30)),
    ("publish", time(18, 0)),
]


def stage_status(conn: sqlite3.Connection, d: date, stage: str) -> str | None:
    r = conn.execute("SELECT status FROM job_runs WHERE run_date=? AND stage=? ORDER BY id DESC LIMIT 1",
                     (d.isoformat(), stage)).fetchone()
    return r["status"] if r else None


def _start(conn: sqlite3.Connection, d: date, stage: str) -> int:
    cur = conn.execute("INSERT INTO job_runs (run_date, stage, status, started_at) VALUES (?,?,?,?)",
                       (d.isoformat(), stage, "running", now_iso()))
    conn.commit()
    return cur.lastrowid


def _finish(conn: sqlite3.Connection, run_id: int, status: str, message: str = "") -> None:
    conn.execute("UPDATE job_runs SET status=?, message=?, finished_at=? WHERE id=?",
                 (status, message[:2000], now_iso(), run_id))
    conn.commit()


def _edinet_disclosures(conn: sqlite3.Connection, d: date, codes: list[str], run_tag: str) -> None:
    s = get_settings()
    if not s.edinet_api_key:
        return
    from .providers.edinet import EdinetClient
    try:
        docs = EdinetClient(s).disclosures_for(set(codes), d, d)
    except DataUnavailable as e:
        conn.execute("INSERT INTO fetch_log (run_id, source, target, status, message, fetched_at) VALUES (?,?,?,?,?,?)",
                     (run_tag, "edinet", d.isoformat(), "unavailable", str(e), now_iso()))
        conn.commit()
        return
    by_code: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        by_code.setdefault(doc["code"], []).append(doc)
    for code, items in by_code.items():
        ingest_disclosures(conn, None, code, d, d, rows=items, source="edinet")  # type: ignore[arg-type]


def do_fetch(conn: sqlite3.Connection, provider: Provider, d: date, now: datetime) -> tuple[str, str]:
    codes = active_codes(conn)
    tag = f"{d.isoformat()}:fetch"
    # 月初・初回にカレンダー同期、月曜に上場状況検証
    if get_setting(conn, "calendar_synced_month") != d.strftime("%Y-%m"):
        if sync_calendar(conn, provider, d - timedelta(days=400), d + timedelta(days=370), tag):
            set_setting(conn, "calendar_synced_month", d.strftime("%Y-%m"))
            conn.commit()
    if d.weekday() == 0 or get_setting(conn, "listings_verified") is None:
        if verify_listings(conn, provider, codes, tag):
            set_setting(conn, "listings_verified", d.isoformat())
            conn.commit()
    for code in codes:
        ingest_quotes(conn, provider, code, backfill_start(conn, code, d), d, tag)
    complete, missing = quotes_complete(conn, codes, d)
    if not complete:
        deadline = time.fromisoformat(get_settings().data_deadline)
        if now.time() >= deadline:
            return "delayed", f"確定終値が未取得: {', '.join(missing)}"
        return "waiting", f"確定終値待ち（{len(missing)}銘柄）: {', '.join(missing[:10])}"
    for code in codes:
        ingest_financials(conn, provider, code, tag)
        ingest_disclosures(conn, provider, code, d - timedelta(days=7), d, tag)
        detect_gaps(conn, code, d - timedelta(days=30), d)
    _edinet_disclosures(conn, d, codes, tag)
    return "done", f"{len(codes)}銘柄の終値・決算・開示を取得"


def do_finalize(conn: sqlite3.Connection, d: date) -> str:
    sc = rows(conn.execute("SELECT code, buy_decision, sell_decision, exit_type FROM scores WHERE run_date=?", (d.isoformat(),)))
    counts: dict[str, int] = {}
    for s in sc:
        counts[f"buy:{s['buy_decision']}"] = counts.get(f"buy:{s['buy_decision']}", 0) + 1
        counts[f"sell:{s['sell_decision']}"] = counts.get(f"sell:{s['sell_decision']}", 0) + 1
    set_setting(conn, "latest_final_date", d.isoformat())
    conn.commit()
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def do_publish(conn: sqlite3.Connection, d: date) -> str:
    q = lambda where: rows(conn.execute(  # noqa: E731
        f"""SELECT s.code, sec.name, s.growth_score, s.buy_score, s.sell_score, s.exit_type FROM scores s
            JOIN securities sec ON sec.code=s.code WHERE s.run_date=? AND {where}
            ORDER BY s.buy_score DESC, s.sell_score DESC""", (d.isoformat(),)))
    buys = q("s.buy_decision IN ('aggressive','normal')")
    held = {r["code"] for r in conn.execute("SELECT DISTINCT code FROM holdings")}
    sells = [r for r in q("s.sell_decision IN ('priority','candidate','partial')") if r["code"] in held]
    lines = [f"購入候補 {len(buys)}件: " + "、".join(f"{b['name']}(買{b['buy_score']})" for b in buys[:5])]
    if sells:
        lines.append("保有株の売却検討 " + "、".join(
            f"{s['name']}(売{s['sell_score']}・{'利確' if s['exit_type'] == 'profit_taking' else '撤退' if s['exit_type'] == 'stop_loss' else '再評価'})"
            for s in sells[:5]))
    body = " / ".join(lines)
    notify(conn, "daily", f"{d.isoformat()} 大引け後分析", body, "/")
    return body


def run_daily(conn: sqlite3.Connection, provider: Provider, d: date | None = None, now: datetime | None = None,
              ignore_schedule: bool = False) -> dict[str, Any]:
    now = now or datetime.now(JST)
    d = d or now.date()
    ok, cal_src = is_business_day(d, conn)
    if not ok:
        if stage_status(conn, d, "fetch") is None:
            rid = _start(conn, d, "fetch")
            _finish(conn, rid, "skipped", f"東証休場日（{cal_src}）")
        return {"date": d.isoformat(), "status": "skipped"}

    result: dict[str, Any] = {"date": d.isoformat(), "stages": {}}
    for i, (stage, at) in enumerate(STAGES):
        status = stage_status(conn, d, stage)
        if status == "done":
            result["stages"][stage] = status
            continue
        if not ignore_schedule and now.time() < at:
            break
        if i > 0 and stage_status(conn, d, STAGES[i - 1][0]) != "done":
            break
        rid = _start(conn, d, stage)
        try:
            if stage == "fetch":
                st, msg = do_fetch(conn, provider, d, now)
                if st == "delayed" and status != "delayed":  # 遅延通知は1回だけ。以降も取得を再試行する
                    notify(conn, "delay", f"{d.isoformat()} データ遅延",
                           "確定終値がそろわないため本日の分析を確定していません。取得後に再実行します。")
            elif stage == "score":
                n = len(run_scoring(conn, d))
                st, msg = "done", f"{n}銘柄を採点"
            elif stage == "verify":
                n = compute_forward_returns(conn)
                s60 = summary(conn, 60)
                st, msg = "done", f"検証 {n}件追加 / 60日IC(買い)={s60['ic']['buy']}"
            elif stage == "finalize":
                st, msg = "done", do_finalize(conn, d)
            else:
                st, msg = "done", do_publish(conn, d)
        except Exception as e:  # noqa: BLE001 — 失敗を記録して次回再試行
            st, msg = "failed", f"{type(e).__name__}: {e}"
        _finish(conn, rid, st, msg)
        result["stages"][stage] = st
        if st != "done":
            break
    return result
