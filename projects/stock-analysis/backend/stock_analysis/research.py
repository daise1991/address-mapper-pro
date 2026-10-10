"""調査に基づく評価前提（弱気・標準・強気の3シナリオ）と、承認済みルールによる自動更新。

評価の3段階（画面表示）:
  前提未確認 (unconfirmed) : 調査に基づく前提が無い（自動初期推計のみ、または未算定）
  暫定評価   (provisional) : 推計は作成済みだが利用者の検証前、または重要な変化で再承認待ち
  検証済み   (verified)    : 根拠と計算を利用者が確認済み。注文候補の対象

承認済みの自動更新ルール（RULE_VERSION）:
  - 2029年EPS（各シナリオ）は登録時の「基準EPS」（会社予想EPS、景気循環株は正常化EPS）に連動させる。
    基準EPSが変わったら、その変化率の ELASTICITY 乗だけ 2029年EPS を自動で増減する（0.5 = 半分だけ反映）
  - 基準EPSの変化が REVIEW_THRESHOLD 以上、検証後に下方修正の開示、計算方式の変更、のいずれかで
    「要再承認」として暫定評価に戻し、通知する（それ以外は承認なしで毎日自動更新）
2029年の EPS・PER は事実ではなく推計値である。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import date
from typing import Any

from .db import now_iso, rows, upsert
from .scoring.valuation import Assumption

RULE_VERSION = "R1"
ELASTICITY = 0.5
REVIEW_THRESHOLD = 0.15
FACTOR_BOUNDS = (0.5, 1.5)
SCENARIOS = ("bear", "base", "bull")
STATUS_LABELS = {"unconfirmed": "前提未確認", "provisional": "暫定評価", "review_required": "暫定評価（要再承認）",
                 "verified": "検証済み"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS assumption_research (
    code TEXT PRIMARY KEY,
    status TEXT NOT NULL,                 -- provisional / review_required / verified
    confidence TEXT,                      -- 高 / 中 / 低
    category TEXT, category_reason TEXT,
    valuation_method TEXT NOT NULL,       -- per / normalized
    fiscal_month_end INTEGER NOT NULL,
    target_year INTEGER NOT NULL,
    scenarios TEXT NOT NULL,              -- {bear|base|bull: {eps, per, rationale}}（推計値）
    required_return REAL NOT NULL, safety_margin REAL NOT NULL, rr_reason TEXT,
    anchor_type TEXT NOT NULL,            -- company_forecast / normalized
    anchor_eps REAL,                      -- 登録時の基準EPS
    detail TEXT,                          -- 会社予想・過去推移・中計・受注・正常化EPS など調査結果の全文 JSON
    sources TEXT, unverified TEXT, confidence_reason TEXT,
    review_reasons TEXT,                  -- 要再承認の理由 JSON 配列
    approved_at TEXT, approved_method_version TEXT, approved_note TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assumption_research_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _history(conn: sqlite3.Connection, code: str, event: str, payload: Any) -> None:
    conn.execute("INSERT INTO assumption_research_history (code, event, payload, created_at) VALUES (?,?,?,?)",
                 (code, event, json.dumps(payload, ensure_ascii=False, default=str), now_iso()))


def normalized_eps_from_db(conn: sqlite3.Connection, code: str, as_of: date) -> float | None:
    """過去最大5期の純利益と最新予想の中央値 ÷ 現在の株式数（自己株除く）。株式分割の影響を受けない。"""
    from .scoring.features import latest_forecast
    cutoff = f"{as_of.isoformat()}T23:59:59+09:00"
    hist = conn.execute(
        """SELECT fiscal_period_end, MAX(disclosed_at) AS d, net_income, shares_outstanding, treasury_shares
           FROM financials WHERE code=? AND period_type='FY' AND disclosed_at<=? AND net_income IS NOT NULL
           GROUP BY fiscal_period_end ORDER BY fiscal_period_end DESC LIMIT 5""", (code, cutoff)).fetchall()
    if not hist:
        return None
    shares = (hist[0]["shares_outstanding"] or 0) - (hist[0]["treasury_shares"] or 0)
    profits = [h["net_income"] for h in hist]
    lf = latest_forecast(conn, code, hist[0]["fiscal_period_end"], cutoff)
    if lf and lf["forecast_net_income"] is not None:
        profits.append(lf["forecast_net_income"])
    if shares <= 0 or len(profits) < 2:
        return None
    v = statistics.median(profits) / shares
    return v if v > 0 else None


def current_anchor(conn: sqlite3.Connection, code: str, anchor_type: str, as_of: date) -> float | None:
    if anchor_type == "normalized":
        return normalized_eps_from_db(conn, code, as_of)
    from .scoring.features import latest_forecast
    cutoff = f"{as_of.isoformat()}T23:59:59+09:00"
    fy = conn.execute("""SELECT fiscal_period_end, forecast_eps FROM financials WHERE code=? AND period_type='FY'
                         AND disclosed_at<=? ORDER BY fiscal_period_end DESC, disclosed_at DESC LIMIT 1""",
                      (code, cutoff)).fetchone()
    if not fy:
        return None
    lf = latest_forecast(conn, code, fy["fiscal_period_end"], cutoff)
    v = (lf and lf["forecast_eps"]) or fy["forecast_eps"]
    return v if v and v > 0 else None


def import_research(conn: sqlite3.Connection, items: list[dict[str, Any]], as_of: date, source: str) -> list[str]:
    """調査結果（docs の JSON 形式）を暫定評価として登録する。既存の検証済みも暫定に戻す（内容が変わるため）。"""
    ensure_schema(conn)
    done = []
    for it in items:
        code = it["code"]
        sc = it.get("scenarios_2029_estimate") or {}
        if not all(sc.get(k, {}).get("eps") and sc.get(k, {}).get("per") for k in SCENARIOS):
            raise ValueError(f"{code}: 弱気・標準・強気の EPS と PER が揃っていません")
        method = "normalized" if it.get("valuation_method") == "normalized" else "per"
        anchor_type = "normalized" if method == "normalized" else "company_forecast"
        anchor = current_anchor(conn, code, anchor_type, as_of)
        if anchor is None:  # DB に無ければ調査値を基準にする
            anchor = ((it.get("normalized_eps") or {}).get("value") if anchor_type == "normalized"
                      else (it.get("latest_company_forecast") or {}).get("eps"))
        detail_keys = ("latest_company_forecast", "history", "split_notes", "midterm_plan", "orders_and_drivers",
                       "normalized_eps", "approx_price", "name")
        row = {
            "code": code, "status": "provisional", "confidence": it.get("confidence"),
            "category": it.get("category"), "category_reason": it.get("category_reason"),
            "valuation_method": method, "fiscal_month_end": int(it.get("fiscal_month_end") or 3),
            "target_year": int(it.get("target_year") or 2029),
            "scenarios": json.dumps({k: sc[k] for k in SCENARIOS}, ensure_ascii=False),
            "required_return": float(it["required_return"]), "safety_margin": float(it["safety_margin"]),
            "rr_reason": it.get("rr_reason"), "anchor_type": anchor_type, "anchor_eps": anchor,
            "detail": json.dumps({k: it.get(k) for k in detail_keys}, ensure_ascii=False),
            "sources": json.dumps(it.get("sources") or [], ensure_ascii=False),
            "unverified": json.dumps(it.get("unverified") or [], ensure_ascii=False),
            "confidence_reason": it.get("confidence_reason"), "review_reasons": "[]",
            "approved_at": None, "approved_method_version": None, "approved_note": None,
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        upsert(conn, "assumption_research", row, ["code"])
        _history(conn, code, f"import:{source}", row)
        done.append(code)
    conn.commit()
    return done


def approve(conn: sqlite3.Connection, codes: list[str], note: str, method_version: str) -> int:
    ensure_schema(conn)
    n = 0
    for code in codes:
        cur = conn.execute(
            """UPDATE assumption_research SET status='verified', approved_at=?, approved_method_version=?,
               approved_note=?, review_reasons='[]', updated_at=? WHERE code=?""",
            (now_iso(), method_version, note, now_iso(), code))
        if cur.rowcount:
            _history(conn, code, "approve", {"note": note, "method_version": method_version})
            n += 1
    conn.commit()
    return n


def get(conn: sqlite3.Connection, code: str) -> dict[str, Any] | None:
    ensure_schema(conn)
    r = conn.execute("SELECT * FROM assumption_research WHERE code=?", (code,)).fetchone()
    if not r:
        return None
    out = dict(r)
    for k in ("scenarios", "detail", "sources", "unverified", "review_reasons"):
        out[k] = json.loads(out[k]) if out.get(k) else None
    out["status_label"] = STATUS_LABELS.get(out["status"], out["status"])
    return out


def scenario_assumptions(conn: sqlite3.Connection, code: str, as_of: date) -> dict[str, Any] | None:
    """承認済みルールで当日の各シナリオの前提を作る。基準EPSの変化を ELASTICITY だけ反映する。"""
    r = get(conn, code)
    if not r:
        return None
    now_anchor = current_anchor(conn, code, r["anchor_type"], as_of)
    ratio = (now_anchor / r["anchor_eps"]) if (now_anchor and r["anchor_eps"]) else 1.0
    factor = min(max(ratio ** ELASTICITY, FACTOR_BOUNDS[0]), FACTOR_BOUNDS[1])
    out = {}
    for k in SCENARIOS:
        s = r["scenarios"][k]
        eps = float(s["eps"]) * factor
        out[k] = Assumption(
            method=r["valuation_method"], target_year=r["target_year"], fiscal_month_end=r["fiscal_month_end"],
            required_return=r["required_return"], safety_margin=r["safety_margin"],
            eps_forecast=eps if r["valuation_method"] == "per" else None,
            normalized_eps=eps if r["valuation_method"] == "normalized" else None,
            assumed_per=float(s["per"]),
            reason=(f"調査前提（{k}・推計値）: 2029年EPS {float(s['eps']):.1f}円×基準EPS連動 {factor:.3f}"
                    f" ／ 想定PER {float(s['per']):.1f}倍。{s.get('rationale') or ''}"),
        )
    return {"research": r, "assumptions": out, "anchor_now": now_anchor, "anchor_ratio": ratio, "factor": factor}


def monitor(conn: sqlite3.Connection, d: date, method_version: str) -> list[dict[str, Any]]:
    """検証済みの前提を毎日点検し、重要な変化があれば要再承認に戻す。戻したものを返す（通知用）。"""
    ensure_schema(conn)
    changed = []
    for r in rows(conn.execute("SELECT * FROM assumption_research WHERE status='verified'")):
        reasons = []
        now_anchor = current_anchor(conn, r["code"], r["anchor_type"], d)
        if now_anchor and r["anchor_eps"]:
            ratio = now_anchor / r["anchor_eps"]
            if abs(ratio - 1) >= REVIEW_THRESHOLD:
                reasons.append(f"基準EPSが登録時から{ratio - 1:+.0%}変化（{r['anchor_eps']:.1f}→{now_anchor:.1f}円）")
        if r["approved_at"]:
            n = conn.execute("""SELECT COUNT(*) FROM disclosures WHERE code=? AND category='revision_down'
                                AND disclosed_at>? AND disclosed_at<=?""",
                             (r["code"], r["approved_at"], f"{d.isoformat()}T23:59:59+09:00")).fetchone()[0]
            if n:
                reasons.append(f"検証後に下方修正の開示 {n}件")
        if r["approved_method_version"] and r["approved_method_version"] != method_version:
            reasons.append(f"計算方式の変更（{r['approved_method_version']}→{method_version}）")
        if reasons:
            conn.execute("UPDATE assumption_research SET status='review_required', review_reasons=?, updated_at=? WHERE code=?",
                         (json.dumps(reasons, ensure_ascii=False), now_iso(), r["code"]))
            _history(conn, r["code"], "review_required", reasons)
            changed.append({"code": r["code"], "reasons": reasons})
    conn.commit()
    return changed


def data_issue_on(conn: sqlite3.Connection, code: str, d: date) -> str | None:
    r = conn.execute("""SELECT field, issue, detail FROM data_quality_issues WHERE code=? AND date=? AND resolved=0
                        AND issue IN ('anomaly','inconsistent','missing') LIMIT 1""", (code, d.isoformat())).fetchone()
    return f"{r['field']}:{r['issue']} {r['detail']}" if r else None


# --- 前提案の自動検証（取込前・毎日の点検で共通に使う） -------------------------------
def validate(item: dict[str, Any], price: float | None, as_of: date, live_price: bool = False) -> dict[str, Any]:
    """調査前提案を検算し、各シナリオの理論価値と、要確認の警告を返す。"""
    from .scoring.valuation import evaluate, years_until

    sc = item["scenarios_2029_estimate"]
    method = "normalized" if item.get("valuation_method") == "normalized" else "per"
    fme = int(item.get("fiscal_month_end") or 3)
    results: dict[str, Any] = {}
    for k in SCENARIOS:
        a = Assumption(method=method, target_year=int(item.get("target_year") or 2029), fiscal_month_end=fme,
                       required_return=float(item["required_return"]), safety_margin=float(item["safety_margin"]),
                       eps_forecast=sc[k]["eps"] if method == "per" else None,
                       normalized_eps=sc[k]["eps"] if method == "normalized" else None,
                       assumed_per=sc[k]["per"], reason="validate")
        v = evaluate(a, price, as_of)
        results[k] = {"eps": sc[k]["eps"], "per": sc[k]["per"], "future_price": v.future_price,
                      "theoretical_value": v.theoretical_value, "target_price": v.target_price,
                      "v_over_p": v.v_over_p, "buy_score": v.buy_score, "buy_score_k150": v.buy_score_k150}
    warnings: list[str] = []
    fc = (item.get("latest_company_forecast") or {}).get("eps")
    norm = (item.get("normalized_eps") or {}).get("value")
    bear, base, bull = (sc[k]["eps"] for k in SCENARIOS)
    if not (bear <= base <= bull) or not (sc["bear"]["per"] <= sc["base"]["per"] <= sc["bull"]["per"]):
        warnings.append("シナリオの大小関係（弱気≦標準≦強気）が崩れている")
    years = years_until(int(item.get("target_year") or 2029), as_of, fme)
    if price:
        # 損益分岐: 現在の株価が理論価値と等しくなる（買い点数50点）ための標準EPSでの必要PER
        disc = (1 + float(item["required_return"])) ** years
        results["base"]["breakeven_per"] = round(price * disc / base, 1)
        results["base"]["per_for_buy75"] = round(price * 1.125 * disc / base, 1)
    if fc and fc > 0 and years > 0:
        cagr = (base / fc) ** (1 / max(years - 0.5, 0.5)) - 1
        results["base"]["implied_growth_from_forecast"] = round(cagr, 3)
        if cagr > 0.25:
            warnings.append(f"標準EPSは会社予想から年率{cagr:.0%}の成長が前提（高すぎないか）")
    if item.get("category") == "cyclical" and norm:
        ratio = base / norm
        results["base"]["base_over_normalized"] = round(ratio, 2)
        if ratio > 1.5:
            warnings.append(f"景気循環株で標準EPSが正常化EPSの{ratio:.1f}倍（ピーク延長でないか根拠の確認が必要）")
        if fc and base > fc * 1.0 and ratio > 1.3:
            warnings.append("景気循環株で標準EPSが今期の会社予想（ピーク圏）を上回る")
    hist_pers = [h["per_at_fy_end"] for h in item.get("history") or [] if h.get("per_at_fy_end")]
    if hist_pers and sc["base"]["per"] > max(hist_pers) * 1.1:
        warnings.append(f"標準PER {sc['base']['per']}倍が過去の期末PERの最大 {max(hist_pers)}倍を上回る")
    ap = item.get("approx_price") or {}
    if ap.get("date") and not live_price:
        age = (as_of - date.fromisoformat(ap["date"])).days
        if age > 14:
            warnings.append(f"調査時の株価が{age}日前のもの（アプリでは当日終値で再計算）")
    if item.get("confidence") == "低":
        warnings.append("信頼度が低い（一次資料で未照合の数値が多い）")
    if len(item.get("unverified") or []) >= 6:
        warnings.append(f"未確認事項が{len(item['unverified'])}件")
    return {"code": item["code"], "price": price, "scenarios": results, "warnings": warnings}


def cross_check(conn: sqlite3.Connection, item: dict[str, Any], as_of: date) -> list[str]:
    """調査値（検索ベース）を J-Quants の公式データと突き合わせる。株式分割の影響を受けないよう純利益で比較する。"""
    out: list[str] = []
    fc = item.get("latest_company_forecast") or {}
    db_fc = current_anchor(conn, item["code"], "company_forecast", as_of)
    if fc.get("eps") and db_fc:
        diff = fc["eps"] / db_fc - 1
        out.append(("一致" if abs(diff) < 0.05 else "要確認") + f": 会社予想EPS 調査 {fc['eps']}円 / J-Quants {db_fc:.1f}円（差 {diff:+.0%}）")
    elif db_fc:
        out.append(f"参考: J-Quants の会社予想EPS {db_fc:.1f}円（調査値なし）")
    fy = {r["fiscal_period_end"][:7]: r["net_income"] for r in conn.execute(
        """SELECT fiscal_period_end, net_income FROM financials WHERE code=? AND period_type='FY' AND net_income IS NOT NULL
           GROUP BY fiscal_period_end HAVING disclosed_at=MAX(disclosed_at)""", (item["code"],))}
    fme = int(item.get("fiscal_month_end") or 3)
    checked = mismatched = 0
    for h in item.get("history") or []:
        ni = h.get("net_income")
        y = "".join(ch for ch in h.get("fiscal_period", "")[:4] if ch.isdigit())
        key = f"{y}-{fme:02d}"
        if ni is None or key not in fy or not fy[key]:
            continue
        checked += 1
        if abs(ni / fy[key] - 1) > 0.05:
            mismatched += 1
            out.append(f"要確認: {h['fiscal_period']} 純利益 調査 {ni / 1e8:,.0f}億円 / J-Quants {fy[key] / 1e8:,.0f}億円")
    if checked:
        out.append(f"過去の純利益: {checked}期を照合、{mismatched}期で5%超の差")
    else:
        out.append("過去の純利益: J-Quants データと照合できる期なし")
    return out
