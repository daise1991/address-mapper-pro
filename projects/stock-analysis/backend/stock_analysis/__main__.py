"""コマンドライン。

  python -m stock_analysis init                 DB作成・60銘柄登録
  python -m stock_analysis demo                 デモデータ投入（STOCK_DATA_MODE=demo のときのみ）
  python -m stock_analysis run-daily [--date D] [--now]   大引け後処理を1回実行（--now は時刻条件を無視）
  python -m stock_analysis scheduler            15:55〜23:59 の間 N分おきに run-daily を呼ぶ常駐ループ
  python -m stock_analysis backfill --start D --end D [--step 5]  過去スコアの再計算（参考値）
  python -m stock_analysis serve [--host 127.0.0.1 --port 8000]
"""
from __future__ import annotations

import argparse
import json
import logging
import time as _time
from datetime import date, datetime, time, timedelta

from .config import JST, get_settings
from .db import connect
from .providers.base import Provider
from .universe import seed_universe

log = logging.getLogger("stock_analysis")


def make_provider() -> Provider:
    mode = get_settings().data_mode
    if mode == "jquants":
        from .providers.jquants import JQuantsProvider
        return JQuantsProvider()
    if mode == "demo":
        from .providers.demo import DemoProvider
        return DemoProvider()
    raise SystemExit(f"STOCK_DATA_MODE が不正: {mode}")


def cmd_demo(args: argparse.Namespace) -> None:
    s = get_settings()
    if s.data_mode != "demo":
        raise SystemExit("demo は STOCK_DATA_MODE=demo のときのみ実行できます（本番DBへの混入防止）")
    from .ingest import ingest_disclosures, ingest_financials, ingest_quotes
    from .providers.demo import DemoProvider
    from .verification import backfill_scores, compute_forward_returns

    conn = connect()
    seed_universe(conn)
    p = DemoProvider()
    end = date.fromisoformat(args.date) if args.date else datetime.now(JST).date()
    codes = [r["code"] for r in conn.execute("SELECT code FROM securities WHERE active=1")]
    for c in codes:
        ingest_quotes(conn, p, c, p.history_start, end)
        ingest_financials(conn, p, c)
        ingest_disclosures(conn, p, c, end - timedelta(days=120), end)
    n = backfill_scores(conn, end - timedelta(days=400), end, step=10)
    from .analysis import run_scoring
    from .market_calendar import is_business_day, previous_business_day
    run_scoring(conn, end if is_business_day(end, conn)[0] else previous_business_day(end, conn))
    m = compute_forward_returns(conn)
    print(f"demo: {len(codes)}銘柄, 採点 {n}日分, 検証 {m}件")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="stock_analysis")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    d = sub.add_parser("demo")
    d.add_argument("--date")
    r = sub.add_parser("run-daily")
    r.add_argument("--date")
    r.add_argument("--now", action="store_true", help="時刻条件を無視して全段階を試行")
    sub.add_parser("scheduler")
    b = sub.add_parser("backfill")
    b.add_argument("--start", required=True)
    b.add_argument("--end", required=True)
    b.add_argument("--step", type=int, default=5)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    if args.cmd == "init":
        conn = connect()
        print(f"{seed_universe(conn)} 銘柄を登録: {get_settings().db_path}")
    elif args.cmd == "demo":
        cmd_demo(args)
    elif args.cmd == "run-daily":
        from .pipeline import run_daily
        conn = connect()
        seed_universe(conn)
        d0 = date.fromisoformat(args.date) if args.date else None
        print(json.dumps(run_daily(conn, make_provider(), d0, ignore_schedule=args.now), ensure_ascii=False, indent=2))
    elif args.cmd == "scheduler":
        from .pipeline import run_daily
        s = get_settings()
        provider = make_provider()
        while True:
            now = datetime.now(JST)
            if time(15, 55) <= now.time() <= time(23, 59):
                conn = connect()
                try:
                    log.info("run_daily: %s", run_daily(conn, provider, now=now))
                finally:
                    conn.close()
            _time.sleep(s.retry_interval_min * 60 if now.time() >= time(16, 0) else 60)
    elif args.cmd == "backfill":
        from .verification import backfill_scores, compute_forward_returns
        conn = connect()
        n = backfill_scores(conn, date.fromisoformat(args.start), date.fromisoformat(args.end), args.step)
        print(f"{n}日分を再計算、検証 {compute_forward_returns(conn)}件")
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("stock_analysis.api:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
