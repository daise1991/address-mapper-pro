"""市場拡大・競争優位（定性2項目）の承認済み評価ルール。

調査結果の「構成要素」からアプリ側で点数を再計算する（調査担当の合計値をそのまま使わない）。
市場拡大: 各セグメントの市場年平均成長率→基礎点（<0%:20, 0-3%:40, 3-7%:60, 7-12%:75, 12-20%:88, 20%以上:95）
          を売上構成比で加重平均し、最速分野への集中 +5 ／ 縮小分野への集中 -5 を加える
競争優位: シェア(30/22/12/5)+同業比利益率(25/15/5)+技術(20/12/5)+受注・顧客(15/9/4)+中計の実績(10/6/2)
根拠（情報源URL・発表日）が無い要素は未評価として扱い、承認を求める。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..db import now_iso

CAGR_BANDS = [(0.0, 20), (0.03, 40), (0.07, 60), (0.12, 75), (0.20, 88)]
ALLOWED = {"share": {30, 22, 12, 5}, "margin_vs_peers": {25, 15, 5}, "technology": {20, 12, 5},
           "orders_customers": {15, 9, 4}, "midterm_plan": {10, 6, 2}}


def cagr_points(cagr: float) -> int:
    for upper, pts in CAGR_BANDS:
        if cagr < upper:
            return pts
    return 95


def evaluate(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"market_expansion": None, "competitive_advantage": None, "issues": []}
    me = item.get("market_expansion") or {}
    segs = [s for s in me.get("segments") or [] if s.get("cagr") is not None and s.get("sales_share")]
    if segs:
        total = sum(s["sales_share"] for s in segs)
        base = sum(s["sales_share"] * cagr_points(s["cagr"]) for s in segs) / total
        adj = max(-5, min(5, me.get("adjustment") or 0))
        score = max(0, min(100, round(base + adj)))
        out["market_expansion"] = score
        for s in segs:
            if cagr_points(s["cagr"]) != s.get("base_points"):
                out["issues"].append(f"市場拡大: {s['segment']} の基礎点を規則どおり {cagr_points(s['cagr'])} に補正（調査値 {s.get('base_points')}）")
            if not s.get("url"):
                out["issues"].append(f"市場拡大: {s['segment']} の情報源URLなし")
        if me.get("score") is not None and abs(me["score"] - score) > 1:
            out["issues"].append(f"市場拡大: 調査の合計 {me['score']} → 規則で再計算 {score}")
    else:
        out["issues"].append("市場拡大: 市場成長率の根拠なし（未評価）")
    ca = (item.get("competitive_advantage") or {}).get("components") or {}
    if all(k in ca for k in ALLOWED):
        pts = 0
        for k, allowed in ALLOWED.items():
            p = ca[k].get("points")
            if p not in allowed:
                out["issues"].append(f"競争優位: {k} の点数 {p} は規則外（{sorted(allowed)}）")
                p = min(allowed, key=lambda a: abs(a - (p or 0)))
            if not ca[k].get("url"):
                out["issues"].append(f"競争優位: {k} の根拠URLなし")
            pts += p
        out["competitive_advantage"] = pts
        stated = (item.get("competitive_advantage") or {}).get("score")
        if stated is not None and stated != pts:
            out["issues"].append(f"競争優位: 調査の合計 {stated} → 規則で再計算 {pts}")
    else:
        out["issues"].append("競争優位: 構成要素が不足（未評価）")
    return out


def import_items(conn: sqlite3.Connection, items: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """定性評価を調査前提に保存する（status は変えない。承認は前提と合わせて行う）。"""
    from .. import research
    research.ensure_schema(conn)
    results = []
    for it in items:
        ev = evaluate(it)
        payload = {"raw": it, "scores": {"market_expansion": ev["market_expansion"],
                                         "competitive_advantage": ev["competitive_advantage"]},
                   "issues": ev["issues"], "imported_at": now_iso(), "source": source}
        cur = conn.execute("UPDATE assumption_research SET qualitative=?, updated_at=? WHERE code=?",
                           (json.dumps(payload, ensure_ascii=False), now_iso(), it["code"]))
        if not cur.rowcount:
            ev["issues"].append("調査前提（3シナリオ）が未登録のため保存できません。先に import-research を実行してください")
        conn.execute("INSERT INTO assumption_research_history (code, event, payload, created_at) VALUES (?,?,?,?)",
                     (it["code"], f"qualitative:{source}", json.dumps(payload, ensure_ascii=False), now_iso()))
        results.append({"code": it["code"], **ev, "saved": bool(cur.rowcount)})
    conn.commit()
    return results


def scores_for(conn: sqlite3.Connection, code: str) -> dict[str, float | None]:
    """承認済みの定性評価（調査）→ 無ければ手入力（qualitative_inputs）。"""
    out: dict[str, float | None] = {"market_expansion": None, "competitive_advantage": None, "growth_outlook": None}
    r = conn.execute("SELECT qualitative FROM assumption_research WHERE code=?", (code,)).fetchone()
    if r and r["qualitative"]:
        out.update(json.loads(r["qualitative"])["scores"])
    for row in conn.execute("SELECT factor, score FROM qualitative_inputs WHERE code=?", (code,)):
        if out.get(row["factor"]) is None:
            out[row["factor"]] = row["score"]
    return out
