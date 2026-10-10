"""同じ日・同じ株価/財務データで v4（scores）と v5（scores_v5）を比較する。"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date
from typing import Any


def _j(x: str | None) -> Any:
    return json.loads(x) if x else {}


def compare(conn: sqlite3.Connection, d: date) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT sec.code, sec.name, s.growth_score AS g4, s.buy_score AS b4, s.sell_score AS s4,
                  s.buy_decision AS bd4, s.sell_decision AS sd4, s.exit_type AS et4, s.sell_detail AS sdet4,
                  v.eval_status, v.growth_formal AS g5f, v.growth_reference AS g5r, v.growth_coverage AS gc,
                  v.buy_score AS b5, v.buy_score_no_dividend AS b5nd, v.buy_status, v.dividend_pv, v.bear_downside,
                  v.sell_formal AS s5f, v.sell_reference AS s5r, v.sell_coverage AS sc, v.sell_detail AS sdet5,
                  v.severe_risks, v.confidence, v.confidence_points, v.confidence_detail,
                  v.buy_decision AS bd5, v.sell_decision AS sd5, v.exit_type AS et5, v.reevaluate, v.buy_detail AS bdet5
           FROM securities sec
           LEFT JOIN scores s ON s.code=sec.code AND s.run_date=?
           LEFT JOIN scores_v5 v ON v.code=sec.code AND v.run_date=?
           WHERE sec.active=1 ORDER BY sec.code""", (d.isoformat(), d.isoformat())).fetchall()
    items = []
    for r in rows:
        r = dict(r)
        risks = _j(r["severe_risks"]) or []
        conf = _j(r["confidence_detail"])
        bdet = _j(r["bdet5"])
        items.append({
            "code": r["code"], "name": r["name"],
            "growth": {"v4": r["g4"], "v5_formal": r["g5f"], "v5_reference": r["g5r"], "coverage": r["gc"]},
            "buy": {"v4": r["b4"], "v5": r["b5"], "v5_reference": bdet.get("reference_buy_score"),
                    "v5_no_dividend": r["b5nd"], "dividend_pv": r["dividend_pv"], "status": r["buy_status"],
                    "bear_downside": r["bear_downside"]},
            "sell": {"v4": r["s4"], "v5_formal": r["s5f"], "v5_reference": r["s5r"], "coverage": r["sc"]},
            "confidence": r["confidence"], "confidence_points": r["confidence_points"],
            "confidence_forced_d": conf.get("forced_d"), "insufficient_history": conf.get("insufficient_history"),
            "severe_risks": risks, "eval_status": r["eval_status"],
            "decisions": {"v4_buy": r["bd4"], "v5_buy": r["bd5"], "v4_sell": r["sd4"], "v5_sell": r["sd5"],
                          "v4_exit": r["et4"], "v5_exit": r["et5"], "v5_reevaluate": bool(r["reevaluate"])},
            "dilution": {"v4": ((_j(r["sdet4"]).get("factors") or {}).get("dilution") or {}).get("score"),
                         "v5": ((_j(r["sdet5"]).get("items") or {}).get("dilution") or {}).get("score")},
        })

    def diffs(key: str, a: str, b: str) -> dict[str, Any]:
        pairs = [(i[key][a], i[key][b]) for i in items if i[key][a] is not None and i[key][b] is not None]
        ds = [y - x for x, y in pairs]
        return {"n": len(ds), "mean_diff": round(sum(ds) / len(ds), 1) if ds else None,
                "max_up": max(ds) if ds else None, "max_down": min(ds) if ds else None}

    buy4 = {i["code"] for i in items if i["decisions"]["v4_buy"] in ("aggressive", "normal")}
    buy5 = {i["code"] for i in items if i["decisions"]["v5_buy"] in ("aggressive", "normal")}
    sell4 = {i["code"] for i in items if i["decisions"]["v4_sell"] in ("priority", "candidate")}
    sell5 = {i["code"] for i in items if i["decisions"]["v5_sell"] in ("priority", "candidate")}
    kawasaki = next((i for i in items if i["code"] == "7012"), None)
    return {
        "date": d.isoformat(), "count": len(items),
        "growth_diff_v5ref_minus_v4": diffs("growth", "v4", "v5_reference"),
        "buy_diff_v5_minus_v4": diffs("buy", "v4", "v5"),
        "buy_dividend_effect": diffs("buy", "v5_no_dividend", "v5"),
        "sell_diff_v5ref_minus_v4": diffs("sell", "v4", "v5_reference"),
        "confidence": dict(Counter(i["confidence"] for i in items)),
        "eval_status": dict(Counter(i["eval_status"] for i in items)),
        "growth_status": {"formal": sum(1 for i in items if i["growth"]["v5_formal"] is not None),
                          "reference_only": sum(1 for i in items if i["growth"]["v5_formal"] is None and i["growth"]["v5_reference"] is not None)},
        "severe_risk_codes": [(i["code"], i["name"], [x["type"] + ":" + x["detail"] for x in i["severe_risks"]]) for i in items if i["severe_risks"]],
        "candidates": {"v4_buy": sorted(buy4), "v5_buy": sorted(buy5), "v4_sell": sorted(sell4), "v5_sell": sorted(sell5),
                       "buy_removed": sorted(buy4 - buy5), "buy_added": sorted(buy5 - buy4)},
        "kawasaki_split_check": kawasaki and {"v4_sell": kawasaki["sell"]["v4"], "v5_sell": kawasaki["sell"]["v5_reference"],
                                              "v4_dilution": kawasaki["dilution"]["v4"], "v5_dilution": kawasaki["dilution"]["v5"]},
        "items": items,
    }


def to_markdown(rep: dict[str, Any]) -> str:
    L = [f"# v4 / v5 比較（{rep['date']}・同一データ）", ""]
    L.append(f"- 評価状態: {rep['eval_status']} ／ 予測信頼度: {rep['confidence']}")
    L.append(f"- 成長期待点 v5参考−v4: {rep['growth_diff_v5ref_minus_v4']} ／ v5正式 {rep['growth_status']['formal']}銘柄・参考のみ {rep['growth_status']['reference_only']}銘柄")
    L.append(f"- 買い点数 v5−v4: {rep['buy_diff_v5_minus_v4']} ／ 配当加算の効果: {rep['buy_dividend_effect']}")
    L.append(f"- 売り点数 v5参考−v4: {rep['sell_diff_v5ref_minus_v4']}")
    L.append(f"- 購入候補 v4 {rep['candidates']['v4_buy']} → v5 {rep['candidates']['v5_buy']} ／ 売却候補 v4 {rep['candidates']['v4_sell']} → v5 {rep['candidates']['v5_sell']}")
    L.append(f"- 川崎重工: {rep['kawasaki_split_check']}")
    L.append(f"- 重大リスク: {rep['severe_risk_codes'] or 'なし'}")
    L += ["", "| 銘柄 | 状態 | 信頼度 | 成長 v4→v5(正式/参考・充足率) | 買い v4→v5(配当前) | 弱気時 | 売り v4→v5(正式/参考) | 重大リスク |",
          "|---|---|---|---|---|---|---|---|"]
    for i in rep["items"]:
        g, b, s = i["growth"], i["buy"], i["sell"]
        bd = f"{b['bear_downside']:+.0%}" if b["bear_downside"] is not None else "—"
        L.append(f"| {i['code']} {i['name']} | {i['eval_status']} | {i['confidence']} | {g['v4']}→{g['v5_formal']}/{g['v5_reference']}（{g['coverage']}） | "
                 f"{b['v4']}→{b['v5'] if b['v5'] is not None else '未算定'}（{b['v5_no_dividend']}） | {bd} | {s['v4']}→{s['v5_formal']}/{s['v5_reference']} | "
                 f"{'、'.join(x['type'] for x in i['severe_risks']) or '—'} |")
    return "\n".join(L)
