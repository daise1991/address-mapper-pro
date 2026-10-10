"""個別条件の通知判定（18:00 の定時通知に追加して送る）。

条件（指示書 7章）:
  buy75        新たに買い点数75点以上になった
  criteria     成長期待80以上・買い75以上・売り40以下を新たに満たした
  sell75       新たに売り点数75点以上になった（保有株のみ）
  revision     保有株に業績下方修正が発表された
  dilution     保有株に増資・重大な財務悪化が発生した
  target       狙い目買値に到達した（終値 ≤ 狙い目価格）

「新たに」は前回の採点日との比較で判定する。alert_log の key で同じ内容の重複通知を防ぐ。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from .db import now_iso

LABELS = {
    "buy75": "買い75点以上",
    "criteria": "購入基準を満たした",
    "sell75": "売り75点以上",
    "revision": "下方修正",
    "dilution": "増資・財務悪化",
    "target": "狙い目買値に到達",
    "daily": "定時",
}


def _prev_run_date(conn: sqlite3.Connection, d: date) -> str | None:
    r = conn.execute("SELECT MAX(run_date) FROM scores WHERE run_date<?", (d.isoformat(),)).fetchone()
    return r[0] if r else None


def detect(conn: sqlite3.Connection, d: date) -> list[dict[str, Any]]:
    today = {r["code"]: dict(r) for r in conn.execute(
        """SELECT s.*, sec.name FROM scores s JOIN securities sec ON sec.code=s.code
           WHERE s.run_date=? AND sec.active=1""", (d.isoformat(),))}
    prev_date = _prev_run_date(conn, d)
    prev = {r["code"]: dict(r) for r in conn.execute("SELECT * FROM scores WHERE run_date=?", (prev_date,))} if prev_date else {}
    held = {r["code"] for r in conn.execute("SELECT DISTINCT code FROM holdings")}
    since = f"{prev_date}T23:59:59+09:00" if prev_date else f"{d.isoformat()}T00:00:00+09:00"
    until = f"{d.isoformat()}T23:59:59+09:00"
    out: list[dict[str, Any]] = []

    def add(kind: str, code: str, key_suffix: str, msg: str) -> None:
        out.append({"kind": kind, "code": code, "key": f"{kind}:{code}:{key_suffix}", "message": msg})

    for code, s in today.items():
        p = prev.get(code, {})
        name = f"{s['name']}({code})"
        # 買い系の通知は、評価前提を登録した銘柄だけ（自動推計の仮の値では通知しない）
        confirmed = json.loads(s["buy_detail"] or "{}").get("assumption_origin") == "manual"
        if confirmed and (s["buy_score"] or 0) >= 75 and (p.get("buy_score") or 0) < 75:
            add("buy75", code, d.isoformat(), f"{name} 買い{s['buy_score']}点（前回{p.get('buy_score') if p.get('buy_score') is not None else '—'}）")
        if confirmed and s["buy_decision"] in ("aggressive", "normal") and p.get("buy_decision") not in ("aggressive", "normal"):
            add("criteria", code, d.isoformat(),
                f"{name} 成長{s['growth_score']}・買い{s['buy_score']}・売り{s['sell_score']}で購入基準を満たしました")
        if code in held and (s["sell_score"] or 0) >= 75 and (p.get("sell_score") or 0) < 75:
            kind = "利益確定" if s["exit_type"] == "profit_taking" else "損切り・撤退" if s["exit_type"] == "stop_loss" else "要確認"
            add("sell75", code, d.isoformat(), f"{name} 売り{s['sell_score']}点（{kind}候補）")
        if confirmed and s["target_price"] and s["price"] and s["price"] <= s["target_price"]:
            was = p.get("target_price") and p.get("price") and p["price"] <= p["target_price"]
            if not was:
                # 同じ狙い目価格（100円単位）での再通知は抑止する
                add("target", code, f"{round(s['target_price'], -2):.0f}",
                    f"{name} 終値{s['price']:,.0f}円が狙い目買値{s['target_price']:,.0f}円以下になりました")
        if code in held:
            fin = (json.loads(s["sell_detail"] or "{}").get("factors", {}).get("financial_deterioration", {}) or {}).get("score")
            pfin = (json.loads(p.get("sell_detail") or "{}").get("factors", {}).get("financial_deterioration", {}) or {}).get("score")
            if fin is not None and fin >= 80 and (pfin is None or pfin < 80):
                add("dilution", code, d.isoformat(), f"{name} 財務悪化の評価が{fin:.0f}点に上昇しました")

    for r in conn.execute(
            """SELECT d.code, d.category, d.title, d.disclosed_at, sec.name FROM disclosures d
               JOIN securities sec ON sec.code=d.code
               WHERE d.disclosed_at>? AND d.disclosed_at<=? AND d.category IN ('revision_down','offering')""",
            (since, until)).fetchall():
        if r["code"] not in held:
            continue
        kind = "revision" if r["category"] == "revision_down" else "dilution"
        add(kind, r["code"], f"{r['disclosed_at']}:{r['title'][:40]}", f"{r['name']}({r['code']}) {r['title']}")
    return out


def record_new(conn: sqlite3.Connection, d: date, alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """未通知のものだけを alert_log に登録して返す（重複通知防止）。"""
    fresh = []
    for a in alerts:
        cur = conn.execute(
            "INSERT INTO alert_log (key, run_date, code, kind, message, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT (key) DO NOTHING",
            (a["key"], d.isoformat(), a["code"], a["kind"], a["message"], now_iso()))
        if cur.rowcount:
            fresh.append(a)
    conn.commit()
    return fresh


def format_body(alerts: list[dict[str, Any]], limit: int = 8) -> str:
    lines = [f"【{LABELS[a['kind']]}】{a['message']}" for a in alerts[:limit]]
    if len(alerts) > limit:
        lines.append(f"ほか{len(alerts) - limit}件はアプリで確認")
    return "\n".join(lines)
