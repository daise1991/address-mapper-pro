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
from .ingest import (backfill_start, detect_gaps, ingest_disclosures, ingest_financials, ingest_financials_by_date,
                     ingest_quotes, ingest_quotes_bulk, ingest_topix, quotes_complete, sync_calendar, verify_listings)
from .market_calendar import business_days, is_business_day, previous_business_day
from .notify import notify
from .providers.base import DataUnavailable, Provider
from .universe import CODE_PREDECESSORS, active_codes
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
    from .providers.edinet import EdinetClient, sync_annual_reports
    client = EdinetClient(s)
    try:
        docs = client.disclosures_for(set(codes), d, d)
    except DataUnavailable as e:
        conn.execute("INSERT INTO fetch_log (run_id, source, target, status, message, fetched_at) VALUES (?,?,?,?,?,?)",
                     (run_tag, "edinet", d.isoformat(), "unavailable", str(e), now_iso()))
        conn.commit()
        return
    conn.execute("INSERT INTO fetch_log (run_id, source, target, status, message, fetched_at) VALUES (?,?,?,?,?,?)",
                 (run_tag, "edinet", d.isoformat(), "ok", f"{len(docs)} docs", now_iso()))
    by_code: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        by_code.setdefault(doc["code"], []).append(doc)
    for code, items in by_code.items():
        ingest_disclosures(conn, None, code, d, d, rows=items, source="edinet")  # type: ignore[arg-type]
    sync_annual_reports(conn, client, docs)  # 有価証券報告書が出ていれば有利子負債を補完


def fetch_quotes(conn: sqlite3.Connection, provider: Provider, codes: list[str], d: date, tag: str) -> None:
    """履歴がある銘柄は日付指定の一括取得、新規・長期欠損の銘柄は銘柄ごとに過去分から取得する。"""
    last = {r["code"]: r["d"] for r in conn.execute("SELECT code, MAX(date) AS d FROM daily_quotes GROUP BY code")}
    recent_cut = (d - timedelta(days=14)).isoformat()
    per_code = [c for c in codes if not last.get(c) or last[c] < recent_cut]
    bulk = [c for c in codes if c not in per_code]
    if bulk:
        start = min(date.fromisoformat(last[c]) for c in bulk) + timedelta(days=1)
        for day in business_days(start, d, conn):
            targets = [c for c in bulk if last[c] < day.isoformat()]
            missing = ingest_quotes_bulk(conn, provider, targets, day, tag)
            if missing is None:  # 一括取得に非対応
                per_code += bulk
                break
    for code in dict.fromkeys(per_code):
        start = backfill_start(conn, code, d, years=5)
        if code in CODE_PREDECESSORS:  # コード変更銘柄は切替日より前を旧コードで取得して連結
            old, since = CODE_PREDECESSORS[code]
            switch = date.fromisoformat(since)
            if start < switch:
                ingest_quotes(conn, provider, code, start, switch - timedelta(days=1), tag, fetch_code=old)
                start = switch
        ingest_quotes(conn, provider, code, start, d, tag)
    # 週1回、直近5営業日を取り直して取引所側の訂正を反映する（日付指定の一括取得で5回のみ）
    last_refresh = get_setting(conn, "quotes_refreshed")
    if bulk and (not last_refresh or date.fromisoformat(last_refresh) <= d - timedelta(days=7)):
        recent = business_days(d - timedelta(days=10), d - timedelta(days=1), conn)[-5:]
        if all(ingest_quotes_bulk(conn, provider, bulk, day, tag) is not None for day in recent):
            set_setting(conn, "quotes_refreshed", d.isoformat())
            conn.commit()
    # 当日に分割・併合があった銘柄は調整後終値を全期間で取り直す
    for r in conn.execute("SELECT code FROM daily_quotes WHERE date=? AND adjustment_factor!=1.0", (d.isoformat(),)).fetchall():
        ingest_quotes(conn, provider, r["code"], d - timedelta(days=365 * 5), d, tag)


def fetch_financials(conn: sqlite3.Connection, provider: Provider, codes: list[str], d: date, tag: str) -> None:
    """決算は開示日単位で一括取得（前営業日分も再取得し、18時以降に確定した速報を拾う）。未取得の銘柄は全履歴を取得。"""
    have = {r["code"] for r in conn.execute("SELECT DISTINCT code FROM financials")}
    days = [previous_business_day(d, conn), d]
    supported = all(ingest_financials_by_date(conn, provider, codes, day, tag) for day in days)
    for code in codes:
        if not supported or code not in have:
            ingest_financials(conn, provider, code, tag)


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
    fetch_quotes(conn, provider, codes, d, tag)
    complete, missing = quotes_complete(conn, codes, d)
    if not complete:
        deadline = time.fromisoformat(get_settings().data_deadline)
        if now.time() >= deadline:
            return "delayed", f"確定終値が未取得: {', '.join(missing)}"
        return "waiting", f"確定終値待ち（{len(missing)}銘柄）: {', '.join(missing[:10])}"
    fetch_financials(conn, provider, codes, d, tag)
    last_topix = conn.execute("SELECT MAX(date) FROM benchmark_quotes WHERE name='TOPIX'").fetchone()[0]
    ingest_topix(conn, provider, date.fromisoformat(last_topix) if last_topix else d - timedelta(days=365 * 5), d, tag)
    for code in codes:
        ingest_disclosures(conn, provider, code, d - timedelta(days=7), d, tag)
        detect_gaps(conn, code, d - timedelta(days=30), d)
    _edinet_disclosures(conn, d, codes, tag)
    extra = ""
    try:  # 監視枠（正式な監視銘柄以外）。失敗しても正式の処理は止めない
        from . import watch
        extra = " / " + watch.fetch(conn, provider, d, tag)
    except Exception as e:  # noqa: BLE001
        conn.execute("INSERT INTO fetch_log (run_id, source, target, status, message, fetched_at) VALUES (?,?,?,?,?,?)",
                     (tag, "watch", "fetch", "error", str(e)[:500], now_iso()))
    return "done", f"{len(codes)}銘柄の終値・決算・開示を取得{extra}"


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
    buys = q("s.buy_decision IN ('aggressive','normal') AND json_extract(s.buy_detail, '$.assumption_status')='verified'")
    unconfirmed = len(q("s.buy_decision IN ('aggressive','normal') AND COALESCE(json_extract(s.buy_detail, '$.assumption_status'),'')!='verified'"))
    held = {r["code"] for r in conn.execute("SELECT DISTINCT code FROM holdings")}
    sells = [r for r in q("s.sell_decision IN ('priority','candidate','partial')") if r["code"] in held]
    lines = [f"購入候補 {len(buys)}件: " + "、".join(f"{b['name']}(買{b['buy_score']})" for b in buys[:5])]
    if unconfirmed:
        lines.append(f"暫定評価・前提未確認の候補 {unconfirmed}件（注文候補の対象外）")
    if sells:
        lines.append("保有株の売却検討 " + "、".join(
            f"{s['name']}(売{s['sell_score']}・{'利確' if s['exit_type'] == 'profit_taking' else '撤退' if s['exit_type'] == 'stop_loss' else '再評価'})"
            for s in sells[:5]))
    body = " / ".join(lines)
    from . import alerts
    if alerts.record_new(conn, d, [{"kind": "daily", "code": None, "key": f"daily:{d.isoformat()}", "message": body}]):
        notify(conn, "daily", f"{d.isoformat()} 大引け後分析", body, "/")
    fresh = alerts.record_new(conn, d, alerts.detect(conn, d))
    if fresh:
        notify(conn, "alert", f"{d.isoformat()} 注目アラート {len(fresh)}件", alerts.format_body(fresh), "/")
    return body + (f" / アラート{len(fresh)}件" if fresh else "")


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
                from . import research
                from .analysis import METHOD_VERSION
                flagged = research.monitor(conn, d, METHOD_VERSION)
                n = len(run_scoring(conn, d))
                try:  # 監視枠の並行採点（scores_watch。正式の採点・候補・通知には使わない）
                    from . import watch
                    watch.score(conn, d)
                except Exception as e:  # noqa: BLE001
                    conn.execute("INSERT INTO fetch_log (run_id, source, target, status, message, fetched_at) VALUES (?,?,?,?,?,?)",
                                 (f"{d}:score", "watch", "scoring", "error", str(e)[:500], now_iso()))
                try:  # v5 は比較検証用。既定では実行しない（STOCK_V5_SHADOW=1 のときだけ。通知・候補には使わない）
                    if get_settings().v5_shadow:
                        from .v5 import engine as v5engine
                        v5engine.run(conn, d)
                except Exception as e:  # noqa: BLE001
                    conn.execute("INSERT INTO fetch_log (run_id, source, target, status, message, fetched_at) VALUES (?,?,?,?,?,?)",
                                 (f"{d}:score", "v5", "scoring", "error", str(e)[:500], now_iso()))
                st, msg = "done", f"{n}銘柄を採点" + (f" / 要再承認 {len(flagged)}件" if flagged else "")
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
