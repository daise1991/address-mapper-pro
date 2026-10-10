"""コマンドライン。

  python -m stock_analysis init                 DB作成・60銘柄登録
  python -m stock_analysis demo                 デモデータ投入（STOCK_DATA_MODE=demo のときのみ）
  python -m stock_analysis run-daily [--date D] [--now]   大引け後処理を1回実行（--now は時刻条件を無視）
  python -m stock_analysis scheduler            15:55〜23:59 の間 N分おきに run-daily を呼ぶ常駐ループ
  python -m stock_analysis backfill --start D --end D [--step 5]  過去スコアの再計算（参考値）
  python -m stock_analysis serve [--host 127.0.0.1 --port 8000]
  python -m stock_analysis run-all [--host 127.0.0.1 --port 8000]   Webアプリ＋スケジューラを1プロセスで常駐（Windows のログオン時起動用）
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
    from .ingest import ingest_disclosures, ingest_financials, ingest_quotes, ingest_topix
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
    ingest_topix(conn, p, p.history_start, end)
    n = backfill_scores(conn, end - timedelta(days=400), end, step=10)
    from .analysis import run_scoring
    from .market_calendar import is_business_day, previous_business_day
    run_scoring(conn, end if is_business_day(end, conn)[0] else previous_business_day(end, conn))
    m = compute_forward_returns(conn)
    print(f"demo: {len(codes)}銘柄, 採点 {n}日分, 検証 {m}件")


def scheduler_loop() -> None:
    """15:55〜23:59 の間、一定間隔で run_daily を呼ぶ。例外で止まらないようにする。"""
    from .pipeline import run_daily
    s = get_settings()
    provider = make_provider()
    while True:
        now = datetime.now(JST)
        if time(15, 55) <= now.time() <= time(23, 59):
            conn = connect()
            try:
                log.info("run_daily: %s", run_daily(conn, provider, now=now))
            except Exception:  # noqa: BLE001
                log.exception("run_daily で予期しないエラー")
            finally:
                conn.close()
        _time.sleep(s.retry_interval_min * 60 if now.time() >= time(16, 0) else 60)


def setup_logging(log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def ensure_console_streams() -> None:
    """pythonw（窓なし起動）では標準出力が None になり、uvicorn が起動時に落ちるため、ファイルへ向ける。"""
    import sys
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = get_settings().db_path.parent / "console.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 — プロセス終了まで開いたまま
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main() -> None:
    ensure_console_streams()
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
    sub.add_parser("gen-vapid", help="プッシュ通知用の鍵を生成して backend/.env に保存（未設定時のみ）")
    sub.add_parser("test-push", help="登録済みの端末にテスト通知を送る")
    ir = sub.add_parser("import-research", help="調査前提案（JSON）を暫定評価として登録し、検算結果を表示")
    ir.add_argument("path")
    ap_ = sub.add_parser("approve", help="暫定評価の前提を検証済みにする（例: approve 6857 1944 --note 確認済み）")
    ap_.add_argument("codes", nargs="+")
    ap_.add_argument("--note", required=True)
    rs = sub.add_parser("rescore", help="最新（または指定日）の採点をやり直す。計算方式を更新したあとに使う")
    rs.add_argument("--date")
    v5 = sub.add_parser("v5-run", help="v5 を参考採点（v4 の正式採点は変更しない）")
    v5.add_argument("--date")
    cv = sub.add_parser("compare-v5", help="同じ日の v4 と v5 を比較し、docs に保存")
    cv.add_argument("--date")
    cv.add_argument("--out", default=None)
    iq = sub.add_parser("import-qualitative", help="市場拡大・競争優位の調査結果を取り込む（承認済みルールで再計算）")
    iq.add_argument("path")
    ex = sub.add_parser("explain", help="銘柄の買い点数の計算根拠を表示（例: explain 5711）")
    ex.add_argument("code")
    sub.add_parser("check", help="J-Quants / EDINET の接続チェック（キー設定後に最初に実行）")
    eb = sub.add_parser("edinet-backfill", help="過去の有価証券報告書から有利子負債を取り込む（1日あたり約3秒）")
    eb.add_argument("--days", type=int, default=400)
    ra = sub.add_parser("run-all")
    ra.add_argument("--host", default="127.0.0.1")
    ra.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log-file", help="ログをファイルにも出力（run-all は既定で data/app.log）")
    args = ap.parse_args()
    log_file = args.log_file or (str(get_settings().db_path.parent / "app.log") if args.cmd in ("run-all", "scheduler") else None)
    setup_logging(log_file)

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
        scheduler_loop()
    elif args.cmd == "backfill":
        from .verification import backfill_scores, compute_forward_returns
        conn = connect()
        n = backfill_scores(conn, date.fromisoformat(args.start), date.fromisoformat(args.end), args.step)
        print(f"{n}日分を再計算、検証 {compute_forward_returns(conn)}件")
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("stock_analysis.api:app", host=args.host, port=args.port)
    elif args.cmd == "gen-vapid":
        from .config import BASE_DIR
        from .notify import write_vapid_to_env
        print(write_vapid_to_env(BASE_DIR / ".env"))
    elif args.cmd == "test-push":
        from .notify import notify
        conn = connect()
        n = conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]
        sent = notify(conn, "test", "テスト通知", "株分析アプリからの通知テストです", "/")
        print(f"登録端末 {n}台中 {sent}台に送信しました" + ("" if n else "（先に画面の『この端末で通知を受け取る』を押してください）"))
    elif args.cmd == "import-research":
        from pathlib import Path

        from . import research
        conn = connect()
        items = json.loads(Path(args.path).read_text(encoding="utf-8"))
        d0 = date.fromisoformat(conn.execute("SELECT MAX(run_date) FROM scores").fetchone()[0] or datetime.now(JST).date().isoformat())
        done = research.import_research(conn, items, d0, Path(args.path).name)
        for it in items:
            q = conn.execute("SELECT close FROM daily_quotes WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
                             (it["code"], d0.isoformat())).fetchone()
            r = research.validate(it, q["close"] if q else None, d0, live_price=bool(q))
            b = r["scenarios"]["base"]
            print(f"{it['code']} {it.get('name', '')}: 終値 {r['price']} / 標準 理論価値 {b['theoretical_value']} 狙い目 {b['target_price']}"
                  f" 買い {b['buy_score']} / 弱気 {r['scenarios']['bear']['theoretical_value']} 強気 {r['scenarios']['bull']['theoretical_value']}")
            for w in r["warnings"]:
                print(f"   ! {w}")
            for c in research.cross_check(conn, it, d0):
                print(f"   = {c}")
        from .analysis import run_scoring
        run_scoring(conn, d0)
        print(f"{len(done)}銘柄を暫定評価として登録し、{d0} の採点に反映しました")
    elif args.cmd == "approve":
        from . import research
        from .analysis import METHOD_VERSION, run_scoring
        conn = connect()
        n = research.approve(conn, [c.upper() for c in args.codes], args.note, METHOD_VERSION)
        d0 = conn.execute("SELECT MAX(run_date) FROM scores").fetchone()[0]
        if d0:
            run_scoring(conn, date.fromisoformat(d0))
        print(f"{n}銘柄を検証済みにしました")
    elif args.cmd == "rescore":
        from .analysis import run_scoring
        conn = connect()
        d0 = args.date or conn.execute("SELECT MAX(run_date) FROM scores").fetchone()[0]
        if not d0:
            raise SystemExit("採点履歴がありません。先に run-daily を実行してください")
        n = len(run_scoring(conn, date.fromisoformat(d0)))
        print(f"{d0} の {n}銘柄を採点し直しました（変更前の結果は scores_archive に保存）")
    elif args.cmd in ("v5-run", "compare-v5"):
        from .v5 import compare as v5compare
        from .v5 import engine as v5engine
        conn = connect()
        last = conn.execute("SELECT MAX(run_date) FROM scores").fetchone()[0]
        if not args.date and not last:
            raise SystemExit("v4 の採点履歴がありません。先に run-daily を実行してください")
        d0 = date.fromisoformat(args.date or last)
        rows = v5engine.run(conn, d0)
        if args.cmd == "v5-run":
            print(f"{d0} の {len(rows)}銘柄を v5 で参考採点しました（正式な採点方式は v4 のまま）")
        else:
            rep = v5compare.compare(conn, d0)
            md = v5compare.to_markdown(rep)
            from pathlib import Path
            out = Path(args.out) if args.out else get_settings().db_path.parent / f"compare_v4_v5_{d0}.md"
            out.write_text(md, encoding="utf-8")
            print(md)
            print(f"\n保存先: {out}")
    elif args.cmd == "import-qualitative":
        from pathlib import Path

        from .v5 import qualitative
        conn = connect()
        res = qualitative.import_items(conn, json.loads(Path(args.path).read_text(encoding="utf-8")), Path(args.path).name)
        for r in res:
            print(f"{r['code']}: 市場拡大 {r['market_expansion']} / 競争優位 {r['competitive_advantage']} / 保存 {'済' if r['saved'] else '不可'}")
            for i in r["issues"]:
                print(f"   ! {i}")
    elif args.cmd == "explain":
        from .checks import explain
        print("\n".join(explain(connect(), args.code.upper())))
    elif args.cmd == "check":
        from .checks import run_all
        results = run_all()
        for r in results:
            print(f"[{r['status']:^4}] {r['name']}: {r['detail']}")
        ng = [r for r in results if r["status"] in ("NG", "要確認")]
        print("\n" + ("問題ありません。" if not ng else f"{len(ng)}件の確認事項があります。この表示を Claude に貼り付けて相談してください。"))
    elif args.cmd == "edinet-backfill":
        from .providers.edinet import EdinetClient, sync_annual_reports
        from .universe import active_codes
        conn = connect()
        client = EdinetClient()
        codes = set(active_codes(conn))
        end = datetime.now(JST).date()
        total = 0
        for i in range(args.days):
            d = end - timedelta(days=i)
            docs = [x for x in client.disclosures_for(codes, d, d)]
            total += sync_annual_reports(conn, client, docs)
        print(f"有価証券報告書 {total}件から有利子負債を取り込みました")
    elif args.cmd == "run-all":
        import threading

        import uvicorn
        connect().close()
        threading.Thread(target=scheduler_loop, name="scheduler", daemon=True).start()
        log.info("Webアプリ http://%s:%s とスケジューラを起動しました", args.host, args.port)
        uvicorn.run("stock_analysis.api:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
