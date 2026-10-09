from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient

from stock_analysis.config import JST


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.delenv("STOCK_APP_TOKEN", raising=False)
    from stock_analysis.api import app
    from stock_analysis.db import connect
    from stock_analysis.pipeline import run_daily
    from stock_analysis.providers.demo import DemoProvider
    from stock_analysis.universe import seed_universe

    conn = connect()
    seed_universe(conn)
    conn.execute("UPDATE securities SET active=0 WHERE code NOT IN ('6857','8035')")
    conn.commit()
    d = date(2026, 10, 9)
    run_daily(conn, DemoProvider(), d, datetime.combine(d, time(18, 5), JST))
    conn.close()
    with TestClient(app) as c:
        yield c


def test_rankings_and_detail(client):
    r = client.get("/api/rankings").json()
    assert r["run_date"] == "2026-10-09" and len(r["items"]) == 2
    d = client.get("/api/securities/6857").json()
    assert d["security"]["name"] == "アドバンテスト" and d["quotes"]


def test_manage_watchlist(client):
    assert client.post("/api/securities", json={"code": "7203", "name": "トヨタ自動車", "category": "stable"}).status_code == 201
    assert client.post("/api/securities", json={"code": "72", "name": "x", "category": "stable"}).status_code == 422
    assert client.patch("/api/securities/7203", json={"active": False}).json()["active"] == 0


def test_assumption_requires_reason(client):
    body = {"method": "per", "target_year": 2029, "required_return": 0.1, "safety_margin": 0.15,
            "eps_forecast": 500, "assumed_per": 25, "reason": ""}
    assert client.put("/api/securities/6857/assumption", json=body).status_code == 422
    body["reason"] = "AIテスター需要の拡大を前提にPER25倍"
    assert client.put("/api/securities/6857/assumption", json=body).status_code == 200
    d = client.get("/api/securities/6857").json()
    assert d["assumption_origin"] == "manual" and d["assumption_history"]


def test_portfolio_and_plan(client):
    client.post("/api/cash", json={"amount": 500000})
    client.post("/api/holdings", json={"code": "6857", "quantity": 5, "avg_cost": 1000})
    p = client.get("/api/portfolio").json()
    assert p["cash"] == 500000 and len(p["holdings"]) == 1
    plan = client.get("/api/order-plan").json()
    assert "自動発注は行いません" in plan["disclaimer"]


def test_verification_endpoint(client):
    v = client.get("/api/verification?horizon=20").json()
    assert "rules" in v and "通常購入以上(k=150 比較)" in v["rules"]


def test_token_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("STOCK_APP_TOKEN", "secret-token")
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer secret-token"}).status_code == 200
    assert client.get("/api/health").status_code == 200
