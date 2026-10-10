"""同じ日・同じ株価/財務データで v4（scores）と v5（scores_v5）を比較する。"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date
from typing import Any

BIG_CHANGE = 30  # v4→v5 でこの点数以上動いたものを「大きな変動」として一覧にする
LABELS = {"growth": "成長", "buy": "買い", "sell": "売り"}


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
                  v.buy_decision AS bd5, v.sell_decision AS sd5, v.exit_type AS et5, v.reevaluate, v.buy_detail AS bdet5,
                  v.growth_detail AS gdet5, v.price
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
        gdet = _j(r["gdet5"])
        sdet5 = _j(r["sdet5"])
        pb = bdet.get("per_basis") or {}
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
            "price": r["price"],
            "growth_formal_if_qualitative_verified": gdet.get("formal_if_qualitative_verified"),
            "qualitative": gdet.get("qualitative") or {},
            "unevaluated": sorted(set(gdet.get("unevaluated") or []) | set(sdet5.get("unevaluated") or [])),
            "per_basis": {"median": pb.get("median"), "p25": pb.get("p25"), "p75": pb.get("p75"),
                          "valid_months": pb.get("valid_months"), "anomalies": pb.get("anomalies") or [],
                          "research_base_per": (bdet.get("research_per") or {}).get("base_per"),
                          "buy_with_research_per": (bdet.get("research_per") or {}).get("buy_score")},
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
    anomalies = []
    for i in items:
        notes = []
        for key, a, b in (("growth", "v4", "v5_reference"), ("buy", "v4", "v5"), ("sell", "v4", "v5_reference")):
            x, y = i[key][a], i[key][b]
            if x is not None and y is not None and abs(y - x) >= BIG_CHANGE:
                notes.append(f"{LABELS[key]} {x}→{y}")
        if i["per_basis"]["anomalies"]:
            notes.append("PER: " + "／".join(i["per_basis"]["anomalies"]))
        if i["price"] is None:
            notes.append("当日の株価なし")
        if i["eval_status"] is None:
            notes.append("v5 の採点なし")
        if i["confidence_forced_d"]:
            notes.append(f"信頼度D強制: {i['confidence_forced_d']}")
        if i["dilution"]["v5"] and i["dilution"]["v5"] >= 50:
            notes.append(f"希薄化 {i['dilution']['v5']}（分割の誤判定でないか要確認）")
        if notes:
            anomalies.append({"code": i["code"], "name": i["name"], "notes": notes, "unevaluated": i["unevaluated"]})
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
                       "buy_removed": sorted(buy4 - buy5), "buy_added": sorted(buy5 - buy4),
                       "sell_removed": sorted(sell4 - sell5), "sell_added": sorted(sell5 - sell4)},
        "formal_count": sum(1 for i in items if i["eval_status"] == "formal"),
        "growth_formal_if_qualitative_verified": sum(1 for i in items if i["growth_formal_if_qualitative_verified"] is not None),
        "qualitative_formal": {k: sum(1 for i in items if ((i["qualitative"].get("formal") or {}).get(k)) is not None)
                               for k in ("market_expansion", "competitive_advantage")},
        "qualitative_reference": {k: sum(1 for i in items if ((i["qualitative"].get("reference") or {}).get(k)) is not None)
                                  for k in ("market_expansion", "competitive_advantage")},
        "per_applied": sum(1 for i in items if i["per_basis"]["median"] is not None),
        "per_anomaly_codes": [(i["code"], i["name"], i["per_basis"]["anomalies"]) for i in items if i["per_basis"]["anomalies"]],
        "anomalies": anomalies,
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
    c = rep["candidates"]
    L.append(f"- 正式評価: {rep['formal_count']}銘柄（定性評価の裏付け後に成長期待点が正式になり得る銘柄: {rep['growth_formal_if_qualitative_verified']}）")
    qf, qr = rep["qualitative_formal"], rep["qualitative_reference"]
    L.append(f"- 定性評価: 市場拡大 参考{qr['market_expansion']}・正式{qf['market_expansion']}銘柄 ／ "
             f"競争優位 参考{qr['competitive_advantage']}・正式{qf['competitive_advantage']}銘柄（正式でない項目は未評価、0点扱いしない）")
    L.append(f"- 想定PER（過去5年中央値）を適用: {rep['per_applied']}銘柄 ／ PERの異常の目印: {len(rep['per_anomaly_codes'])}銘柄")
    L.append(f"- 購入候補 v4 {c['v4_buy']} → v5 {c['v5_buy']}（追加 {c['buy_added']}・除外 {c['buy_removed']}）")
    L.append(f"- 売却候補 v4 {c['v4_sell']} → v5 {c['v5_sell']}（追加 {c['sell_added']}・除外 {c['sell_removed']}）")
    L.append(f"- 川崎重工: {rep['kawasaki_split_check']}")
    L.append(f"- 重大リスク: {rep['severe_risk_codes'] or 'なし'}")
    L += ["", "| 銘柄 | 状態 | 信頼度 | 成長 v4→v5(正式/参考・充足率) | 買い v4→v5(配当前／調査PER) | PER中央値(25-75%) | 弱気時 | 売り v4→v5(正式/参考・充足率) | 重大リスク |",
          "|---|---|---|---|---|---|---|---|---|"]
    for i in rep["items"]:
        g, b, s = i["growth"], i["buy"], i["sell"]
        bd = f"{b['bear_downside']:+.0%}" if b["bear_downside"] is not None else "—"
        pb = i["per_basis"]
        per = f"{pb['median']}({pb['p25']}-{pb['p75']})" if pb["median"] is not None else "—"
        L.append(f"| {i['code']} {i['name']} | {i['eval_status']} | {i['confidence']} | {g['v4']}→{g['v5_formal']}/{g['v5_reference']}（{g['coverage']}） | "
                 f"{b['v4']}→{b['v5'] if b['v5'] is not None else '未算定'}（{b['v5_no_dividend']}／{pb['buy_with_research_per']}） | {per} | {bd} | "
                 f"{s['v4']}→{s['v5_formal']}/{s['v5_reference']}（{s['coverage']}） | {'、'.join(x['type'] for x in i['severe_risks']) or '—'} |")
    L += ["", "## 定性評価（参考点数／正式点数）と検証の状態", "",
          "| 銘柄 | 市場拡大 参考/正式 | 競争優位 参考/正式 | 会社 | 市場 | 計算(市場/競争) | 正式にならない理由 |", "|---|---|---|---|---|---|---|"]
    ok = lambda x: "済" if x else "未"
    for i in rep["items"]:
        q = i["qualitative"]
        r, f, v = q.get("reference") or {}, q.get("formal") or {}, q.get("verification") or {}
        cv = v.get("calculation_verified") or {}
        why = "；".join(f"{'市場' if k == 'market_expansion' else '競争'}: {w}" for k, w in (q.get("unevaluated") or {}).items())
        fmt = lambda x: "—" if x is None else x
        L.append(f"| {i['code']} {i['name']} | {fmt(r.get('market_expansion'))}/{fmt(f.get('market_expansion'))} | "
                 f"{fmt(r.get('competitive_advantage'))}/{fmt(f.get('competitive_advantage'))} | {ok(v.get('company_verified'))} | "
                 f"{ok(v.get('market_verified'))} | {ok(cv.get('market_expansion'))}/{ok(cv.get('competitive_advantage'))} | {why or '—'} |")
    L += ["", "## 異常な点数変動・欠損データ・想定外の判定", ""]
    for a in rep["anomalies"]:
        miss = f" ／ 未評価: {'、'.join(a['unevaluated'])}" if a["unevaluated"] else ""
        L.append(f"- {a['code']} {a['name']}: {'；'.join(a['notes'])}{miss}")
    if not rep["anomalies"]:
        L.append("- なし")
    return "\n".join(L)
