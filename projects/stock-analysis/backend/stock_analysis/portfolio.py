"""保有株・買付余力・資金配分と、翌営業日の SBI証券 S株 購入判断支援。

- 取得単価は損益・税金の試算にだけ使い、評価点や判定には使わない
- S株は1株単位・成行のみ（指値不可）。夕方（13:30〜24:00）の注文は翌営業日の前場始値で約定するため、
  当日終値×(1+価格バッファ)を「購入許容上限価格」として表示する。約定価格の保証ではない
- 毎月の追加投資額（既定 30,000円）を注文案の合計上限にする
- 自動発注はしない。出力は「注文案」で、利用者が証券会社で承認・発注する
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date
from typing import Any

from .db import get_setting, rows
from .market_calendar import next_business_day

TAX_RATE = 0.20315  # 特定口座（申告分離課税）
DEFAULT_POLICY = {
    "max_weight_per_stock": 0.10,     # 1銘柄の上限（総資産比）
    "max_weight_emerging": 0.05,      # 新興赤字企業の上限
    "cash_reserve_ratio": 0.10,       # 常に残す現金比率
    "price_buffer": 0.03,             # 購入許容上限価格 = 終値 × (1 + これ)
    "monthly_budget": 30000,          # 毎月の追加投資額（円）。注文案の合計上限
    "aggressive_budget_ratio": 1.0,   # 積極購入候補は上限まで
    "normal_budget_ratio": 0.5,       # 通常購入候補は上限の半分
}


def latest_cash(conn: sqlite3.Connection) -> float:
    r = conn.execute("SELECT amount FROM cash_balance ORDER BY as_of DESC, id DESC LIMIT 1").fetchone()
    return float(r["amount"]) if r else 0.0


def latest_scores(conn: sqlite3.Connection) -> tuple[str | None, dict[str, dict[str, Any]]]:
    r = conn.execute("SELECT MAX(run_date) AS d FROM scores").fetchone()
    if not r or not r["d"]:
        return None, {}
    sc = rows(conn.execute(
        """SELECT s.*, sec.name, sec.category,
                  json_extract(s.buy_detail, '$.assumption_origin') AS assumption_origin
           FROM scores s JOIN securities sec ON sec.code=s.code WHERE s.run_date=?""", (r["d"],)))
    return r["d"], {x["code"]: x for x in sc}


def positions(conn: sqlite3.Connection) -> dict[str, Any]:
    run_date, scores = latest_scores(conn)
    hs = rows(conn.execute(
        "SELECT h.*, s.name FROM holdings h LEFT JOIN securities s ON s.code=h.code ORDER BY h.code"))
    total_mv = 0.0
    for h in hs:
        sc = scores.get(h["code"], {})
        price = sc.get("price")
        h["price"] = price
        h["market_value"] = price * h["quantity"] if price else None
        h["unrealized"] = (price - h["avg_cost"]) * h["quantity"] if price else None
        taxable = h["account"] in ("specific", "general")
        h["est_tax_if_sold"] = max(0.0, h["unrealized"] or 0.0) * TAX_RATE if taxable else 0.0
        h["sell_score"] = sc.get("sell_score")
        h["sell_decision"] = sc.get("sell_decision")
        h["exit_type"] = sc.get("exit_type")
        total_mv += h["market_value"] or 0.0
    cash = latest_cash(conn)
    total = total_mv + cash
    for h in hs:
        h["weight"] = (h["market_value"] / total) if (h["market_value"] and total) else None
    return {"as_of": run_date, "holdings": hs, "cash": cash, "market_value": total_mv, "total_assets": total}


def order_plan(conn: sqlite3.Connection, today: date) -> dict[str, Any]:
    policy = {**DEFAULT_POLICY, **(get_setting(conn, "allocation_policy", {}) or {})}
    pos = positions(conn)
    run_date, scores = latest_scores(conn)
    total, cash = pos["total_assets"], pos["cash"]
    spendable_cash = max(0.0, cash - total * policy["cash_reserve_ratio"])
    month = today.strftime("%Y-%m")
    month_spent = sum(h["quantity"] * h["avg_cost"] for h in pos["holdings"] if (h.get("acquired_at") or "").startswith(month))
    month_left = max(0.0, policy["monthly_budget"] - month_spent)
    spendable = min(spendable_cash, month_left)
    held_mv = {h["code"]: (h["market_value"] or 0.0) for h in pos["holdings"]}

    all_cands = [s for s in scores.values() if s["buy_decision"] in ("aggressive", "normal") and s["price"]]
    # 評価前提が自動推計（会社予想から機械的に作った仮の値）の銘柄は注文案に入れない
    cands = [s for s in all_cands if s.get("assumption_origin") == "manual"]
    cands.sort(key=lambda s: (s["buy_decision"] != "aggressive", -(s["buy_score"] or 0), -(s["growth_score"] or 0)))
    orders, skipped = [], []
    for s in all_cands:
        if s.get("assumption_origin") != "manual":
            skipped.append({"code": s["code"], "name": s["name"],
                            "reason": "評価前提が自動推計（前提未確認）。銘柄詳細で前提を登録すると対象になります"})
    for s in cands:
        cap_w = policy["max_weight_emerging"] if s["category"] == "emerging_loss" else policy["max_weight_per_stock"]
        ratio = policy["aggressive_budget_ratio"] if s["buy_decision"] == "aggressive" else policy["normal_budget_ratio"]
        room = max(0.0, total * cap_w * ratio - held_mv.get(s["code"], 0.0))
        budget = min(room, spendable)
        upper = s["price"] * (1 + policy["price_buffer"])
        qty = math.floor(budget / upper) if upper else 0
        if qty < 1:
            reason = "今月の投資予算の残りが不足" if budget == spendable and spendable < upper else "1銘柄の上限到達または買付余力不足"
            skipped.append({"code": s["code"], "name": s["name"], "reason": reason})
            continue
        amount = qty * upper
        spendable -= amount
        orders.append({
            "code": s["code"], "name": s["name"], "decision": s["buy_decision"],
            "growth_score": s["growth_score"], "buy_score": s["buy_score"], "sell_score": s["sell_score"],
            "last_close": s["price"], "target_price": s["target_price"], "theoretical_value": s["theoretical_value"],
            "quantity": qty, "estimated_amount": round(amount),
            "upper_limit_price": round(upper, 1),
            "note": ("狙い目価格を上回っています。翌朝の始値次第で見送りも検討" if s["target_price"] and s["price"] > s["target_price"]
                     else "狙い目価格以下"),
        })
    sells = [
        {"code": h["code"], "name": h["name"], "quantity": h["quantity"], "sell_score": h["sell_score"],
         "decision": h["sell_decision"], "exit_type": h["exit_type"], "est_tax_if_sold": round(h["est_tax_if_sold"])}
        for h in pos["holdings"] if h["sell_decision"] in ("priority", "candidate", "partial")
    ]
    nbd = next_business_day(today, conn)
    return {
        "score_date": run_date,
        "execution": f"{nbd.isoformat()} 前場始値（S株: 13:30〜翌7:00の注文）",
        "spendable_cash": round(spendable_cash),
        "monthly_budget": policy["monthly_budget"],
        "month_spent": round(month_spent),
        "month_left": round(month_left),
        "plan_budget": round(min(spendable_cash, month_left)),
        "policy": policy,
        "buy_orders": orders,
        "skipped": skipped,
        "sell_reviews": sells,
        "disclaimer": "自動発注は行いません。内容を確認し、ご自身で証券会社にて発注してください。",
        "s_kabu_note": ("S株は成行注文のみで指値注文ができません。購入許容上限価格は判断の目安であり、"
                        "その価格以下での約定は保証されません。注文後に取り消せる時間帯は SBI証券の案内で確認してください。"),
    }
