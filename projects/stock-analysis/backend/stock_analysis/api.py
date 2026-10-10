"""Web API（FastAPI）。フロントエンド（frontend/dist）も同じオリジンで配信する。

セキュリティ:
- STOCK_APP_TOKEN を設定すると /api/* は `Authorization: Bearer <token>` 必須
- トークン未設定時はループバック（127.0.0.1 / ::1）からの直接アクセスのみ許可（トンネル経由は拒否）
- 秘密情報（APIキー・VAPID秘密鍵）は API で返さない
"""
from __future__ import annotations

import hmac
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import assumptions as assumption_store
from .analysis import load_rules
from .config import BASE_DIR, JST, get_settings
from .db import connect, get_setting, now_iso, rows, set_setting, upsert
from .portfolio import order_plan, positions
from .scoring.rules import BUY_LABELS, EXIT_LABELS, SELL_LABELS, RuleSet
from .scoring.valuation import CATEGORY_DEFAULTS, Assumption
from .universe import CATEGORIES, seed_universe
from .verification import BENCHMARKS, HORIZONS, improvement_report, summary

@asynccontextmanager
async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
    conn = connect()
    seed_universe(conn)
    conn.close()
    yield


app = FastAPI(title="日本株 AI 投資分析", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-real-ip", "forwarded")


@app.middleware("http")
async def auth(request: Request, call_next):  # type: ignore[no-untyped-def]
    if request.url.path.startswith("/api/") and request.url.path not in ("/api/health",):
        token = get_settings().app_token
        if token:
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given, token):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        else:
            # トンネル（Cloudflare 等）経由の通信は送信元が 127.0.0.1 に見えるため、転送ヘッダーで外部からと判定する
            proxied = any(h in request.headers for h in PROXY_HEADERS)
            local = request.client is not None and request.client.host in ("127.0.0.1", "::1", "testclient")
            if proxied or not local:
                return JSONResponse({"detail": "STOCK_APP_TOKEN 未設定のため、このPCからのみ利用できます"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def _parse(r: dict[str, Any], *keys: str) -> dict[str, Any]:
    for k in keys:
        if r.get(k):
            r[k] = json.loads(r[k])
    return r


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    return {
        "buy_labels": BUY_LABELS, "sell_labels": SELL_LABELS, "exit_labels": EXIT_LABELS,
        "categories": {k: v["label"] for k, v in CATEGORY_DEFAULTS.items()},
        "category_defaults": CATEGORY_DEFAULTS, "horizons": HORIZONS, "benchmarks": BENCHMARKS,
    }


@app.get("/api/status")
def status(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    s = get_settings()
    latest = db.execute("SELECT MAX(run_date) AS d FROM scores").fetchone()["d"]
    today = datetime.now(JST).date().isoformat()
    return {
        "data_mode": s.data_mode,
        "latest_score_date": latest,
        "latest_final_date": get_setting(db, "latest_final_date"),
        "jobs_today": rows(db.execute(
            """SELECT stage, status, message, started_at, finished_at FROM job_runs
               WHERE id IN (SELECT MAX(id) FROM job_runs WHERE run_date=? GROUP BY stage)""", (today,))),
        "open_quality_issues": db.execute("SELECT COUNT(*) FROM data_quality_issues WHERE resolved=0").fetchone()[0],
        "unavailable_fetches_24h": db.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE status!='ok' AND fetched_at>=datetime('now','-1 day')").fetchone()[0],
        "push_configured": bool(s.vapid_public_key and s.vapid_private_key),
    }


@app.get("/api/rankings")
def rankings(run_date: str | None = None, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    d = run_date or db.execute("SELECT MAX(run_date) AS d FROM scores").fetchone()["d"]
    items = rows(db.execute(
        """SELECT s.*, sec.name, sec.category, sec.group_name, sec.asset_type, sec.listing_status
           FROM scores s JOIN securities sec ON sec.code=s.code WHERE s.run_date=? AND sec.active=1""", (d,)))
    held = {r["code"] for r in db.execute("SELECT DISTINCT code FROM holdings")}
    for r in items:
        _parse(r, "growth_detail", "buy_detail", "sell_detail", "indicators")
        r["held"] = r["code"] in held
    dates = [r["run_date"] for r in db.execute("SELECT DISTINCT run_date FROM scores ORDER BY run_date DESC LIMIT 60")]
    return {"run_date": d, "dates": dates, "items": items}


class SecurityIn(BaseModel):
    code: str = Field(pattern=r"^[0-9][0-9A-Z]{3}$")
    name: str = Field(min_length=1, max_length=100)
    category: str
    group_name: str = "user"
    asset_type: str = Field(default="stock", pattern="^(stock|etf)$")
    note: str | None = None


class SecurityPatch(BaseModel):
    name: str | None = None
    category: str | None = None
    active: bool | None = None
    note: str | None = None


@app.get("/api/securities")
def list_securities(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    return rows(db.execute("SELECT * FROM securities ORDER BY active DESC, group_name, code"))


@app.post("/api/securities", status_code=201)
def add_security(body: SecurityIn, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    if body.category not in CATEGORIES:
        raise HTTPException(400, f"category は {sorted(CATEGORIES)} のいずれか")
    if db.execute("SELECT 1 FROM securities WHERE code=?", (body.code,)).fetchone():
        db.execute("UPDATE securities SET active=1, removed_at=NULL WHERE code=?", (body.code,))
    else:
        upsert(db, "securities", {**body.model_dump(), "active": 1, "listing_status": "unverified", "added_at": now_iso()}, ["code"])
    db.commit()
    return dict(db.execute("SELECT * FROM securities WHERE code=?", (body.code,)).fetchone())


@app.patch("/api/securities/{code}")
def patch_security(code: str, body: SecurityPatch, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    if not db.execute("SELECT 1 FROM securities WHERE code=?", (code,)).fetchone():
        raise HTTPException(404)
    if body.category is not None and body.category not in CATEGORIES:
        raise HTTPException(400, "category が不正")
    for k, v in body.model_dump(exclude_none=True).items():
        if k == "active":
            db.execute("UPDATE securities SET active=?, removed_at=? WHERE code=?", (int(v), None if v else now_iso(), code))
        else:
            db.execute(f"UPDATE securities SET {k}=? WHERE code=?", (v, code))
    db.commit()
    return dict(db.execute("SELECT * FROM securities WHERE code=?", (code,)).fetchone())


@app.get("/api/securities/{code}")
def security_detail(code: str, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    sec = db.execute("SELECT * FROM securities WHERE code=?", (code,)).fetchone()
    if not sec:
        raise HTTPException(404)
    a, origin = assumption_store.load(db, code)
    return {
        "security": dict(sec),
        "scores": [_parse(r, "growth_detail", "buy_detail", "sell_detail", "indicators") for r in rows(db.execute(
            "SELECT * FROM scores WHERE code=? ORDER BY run_date DESC LIMIT 120", (code,)))],
        "quotes": rows(db.execute(
            "SELECT date, open, high, low, close, volume, adj_close, source, fetched_at FROM daily_quotes WHERE code=? ORDER BY date DESC LIMIT 260", (code,)))[::-1],
        "financials": rows(db.execute("SELECT * FROM financials WHERE code=? ORDER BY disclosed_at DESC LIMIT 20", (code,))),
        "disclosures": rows(db.execute("SELECT * FROM disclosures WHERE code=? ORDER BY disclosed_at DESC LIMIT 30", (code,))),
        "assumption": assumption_store.to_dict(a), "assumption_origin": origin,
        "assumption_history": [json.loads(r["payload"]) for r in db.execute(
            "SELECT payload FROM valuation_assumption_history WHERE code=? ORDER BY id DESC LIMIT 20", (code,))],
        "qualitative": rows(db.execute("SELECT * FROM qualitative_inputs WHERE code=?", (code,))),
        "quality_issues": rows(db.execute(
            "SELECT * FROM data_quality_issues WHERE code=? AND resolved=0 ORDER BY detected_at DESC LIMIT 30", (code,))),
    }


class AssumptionIn(BaseModel):
    method: str = Field(pattern="^(per|dcf|normalized|asset|unrated)$")
    target_year: int = Field(ge=2026, le=2035)
    fiscal_month_end: int = Field(default=3, ge=1, le=12)
    required_return: float = Field(gt=0, lt=1)
    safety_margin: float = Field(ge=0, lt=1)
    reason: str = Field(min_length=4)
    eps_forecast: float | None = None
    assumed_per: float | None = Field(default=None, gt=0)
    fcf_forecast: float | None = None
    terminal_growth: float | None = None
    net_cash_per_share: float | None = None
    normalized_eps: float | None = None
    bps_forecast: float | None = None
    target_pbr: float | None = Field(default=None, gt=0)


@app.put("/api/securities/{code}/assumption")
def put_assumption(code: str, body: AssumptionIn, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    if not db.execute("SELECT 1 FROM securities WHERE code=?", (code,)).fetchone():
        raise HTTPException(404)
    assumption_store.save(db, code, Assumption(**body.model_dump()), "manual")
    return {"ok": True}


class QualitativeIn(BaseModel):
    score: float = Field(ge=0, le=100)
    reason: str = Field(min_length=4)


@app.put("/api/securities/{code}/qualitative/{factor}")
def put_qualitative(code: str, factor: str, body: QualitativeIn, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    if factor not in ("market_expansion", "competitive_advantage", "growth_outlook"):
        raise HTTPException(400, "factor が不正")
    upsert(db, "qualitative_inputs", {"code": code, "factor": factor, "score": body.score, "reason": body.reason,
                                      "updated_at": now_iso()}, ["code", "factor"])
    db.commit()
    return {"ok": True}


class HoldingIn(BaseModel):
    code: str
    quantity: float = Field(gt=0)
    avg_cost: float = Field(ge=0)
    account: str = Field(default="specific", pattern="^(specific|general|nisa_growth)$")
    acquired_at: str | None = None
    note: str | None = None


@app.get("/api/portfolio")
def get_portfolio(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    return positions(db)


@app.post("/api/holdings", status_code=201)
def add_holding(body: HoldingIn, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    cur = db.execute("INSERT INTO holdings (code, quantity, avg_cost, account, acquired_at, note, updated_at) VALUES (?,?,?,?,?,?,?)",
                     (body.code, body.quantity, body.avg_cost, body.account, body.acquired_at, body.note, now_iso()))
    db.commit()
    return {"id": cur.lastrowid}


@app.put("/api/holdings/{hid}")
def put_holding(hid: int, body: HoldingIn, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    n = db.execute("UPDATE holdings SET code=?, quantity=?, avg_cost=?, account=?, acquired_at=?, note=?, updated_at=? WHERE id=?",
                   (body.code, body.quantity, body.avg_cost, body.account, body.acquired_at, body.note, now_iso(), hid)).rowcount
    db.commit()
    if not n:
        raise HTTPException(404)
    return {"ok": True}


@app.delete("/api/holdings/{hid}")
def delete_holding(hid: int, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    db.execute("DELETE FROM holdings WHERE id=?", (hid,))
    db.commit()
    return {"ok": True}


class CashIn(BaseModel):
    amount: float = Field(ge=0)
    note: str | None = None


@app.post("/api/cash", status_code=201)
def set_cash(body: CashIn, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    db.execute("INSERT INTO cash_balance (as_of, amount, note) VALUES (?,?,?)", (now_iso(), body.amount, body.note))
    db.commit()
    return {"ok": True}


@app.get("/api/order-plan")
def get_order_plan(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    return order_plan(db, datetime.now(JST).date())


@app.put("/api/settings/allocation")
def put_allocation(body: dict[str, float], db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    set_setting(db, "allocation_policy", body)
    db.commit()
    return {"ok": True}


@app.get("/api/settings/rules")
def get_rules(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    return load_rules(db).to_dict()


@app.put("/api/settings/rules")
def put_rules(body: dict[str, Any], db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    try:
        RuleSet(**body)
    except TypeError as e:
        raise HTTPException(400, str(e)) from e
    set_setting(db, "rules", body)
    db.commit()
    return {"ok": True}


@app.get("/api/settings/weights")
def get_weights(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    from .scoring import growth, sell
    from .analysis import load_weights
    cur = load_weights(db)
    return {
        "growth": {k: {"label": growth.LABELS[k], "default": v, "current": cur["growth"].get(k, v)} for k, v in growth.WEIGHTS.items()},
        "sell": {k: {"label": sell.LABELS[k], "default": v, "current": cur["sell"].get(k, v)} for k, v in sell.WEIGHTS.items()},
        "customized": bool(cur["growth"] or cur["sell"]),
        "note": get_setting(db, "weights_note"),
    }


class WeightsIn(BaseModel):
    growth: dict[str, float] = {}
    sell: dict[str, float] = {}
    note: str = Field(min_length=4, description="変更理由（例: 従来配点に合わせる）")


@app.put("/api/settings/weights")
def put_weights(body: WeightsIn, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    from .scoring import growth, sell
    for name, given, base in (("growth", body.growth, growth.WEIGHTS), ("sell", body.sell, sell.WEIGHTS)):
        unknown = set(given) - set(base)
        if unknown:
            raise HTTPException(400, f"{name} に不明な観点: {sorted(unknown)}")
        if any(v < 0 for v in given.values()):
            raise HTTPException(400, "重みは0以上")
        if given and sum({**base, **given}.values()) <= 0:
            raise HTTPException(400, f"{name} の重みの合計が0です")
    set_setting(db, "growth_weights", body.growth)
    set_setting(db, "sell_weights", body.sell)
    set_setting(db, "weights_note", {"note": body.note, "updated_at": now_iso()})
    db.commit()
    return {"ok": True, "message": "次回の採点から反映されます。計算方式のバージョンが変わり、変更前の結果は保存されます。"}


@app.get("/api/verification")
def get_verification(horizon: int = 60, live_only: bool = False, benchmark: str = "universe",
                     method_version: str | None = None, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    if benchmark not in BENCHMARKS:
        raise HTTPException(400, "benchmark が不正")
    return summary(db, horizon, live_only, load_rules(db), benchmark, method_version)


@app.get("/api/verification/report")
def get_improvement_report(benchmark: str = "TOPIX", db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    if benchmark not in BENCHMARKS:
        raise HTTPException(400, "benchmark が不正")
    return improvement_report(db, load_rules(db), benchmark)


@app.get("/api/jobs")
def jobs(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    return rows(db.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT 100"))


@app.get("/api/quality")
def quality(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    return {
        "issues": rows(db.execute("SELECT * FROM data_quality_issues WHERE resolved=0 ORDER BY detected_at DESC LIMIT 200")),
        "fetch_failures": rows(db.execute("SELECT * FROM fetch_log WHERE status!='ok' ORDER BY id DESC LIMIT 200")),
    }


@app.get("/api/notifications")
def notifications(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    return rows(db.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 50"))


@app.get("/api/push/key")
def push_key() -> dict[str, str | None]:
    return {"public_key": get_settings().vapid_public_key}


class PushSub(BaseModel):
    endpoint: str = Field(pattern=r"^https://")
    keys: dict[str, str]


@app.post("/api/push/subscribe", status_code=201)
def push_subscribe(body: PushSub, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    upsert(db, "push_subscriptions", {"endpoint": body.endpoint, "p256dh": body.keys.get("p256dh", ""),
                                      "auth": body.keys.get("auth", ""), "created_at": now_iso()}, ["endpoint"])
    db.commit()
    return {"ok": True}


DIST = BASE_DIR.parent / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        f = DIST / path
        if path and f.is_file() and DIST in f.resolve().parents:
            return FileResponse(f)
        return FileResponse(DIST / "index.html")



