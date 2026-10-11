"""1営業日分の3種類の採点と売買判定を計算し scores に保存する。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import assumptions, research
from .db import get_setting, now_iso, upsert
from .indicators import compute_all
from .scoring.features import build_features
from .scoring.growth import growth_score
from .scoring.rules import RuleSet, buy_decision, exit_type, sell_decision
from .scoring.sell import sell_score
from .scoring.valuation import evaluate, years_until
from .universe import active_codes


# 計算方式のバージョン。採点ロジックを変えたら必ず上げる（変更前の結果は scores_archive に残る）。
# 配点・判定閾値の変更は rules:<ID> 部分が変わることで区別される
METHOD_VERSION = "2026.10-v4.4"  # v4.4: 景気連動の自動推計（赤字年でも正常化・売上急拡大は利益率で正常化・予想純利益の補完）、売りは項目不足でも下方修正・増資80点以上なら0.8倍を暫定点（2026-10-11 経営者承認） / v4.3: 計算処理の修正（観測していない開示を0件扱いにしない・未評価の理由を具体化。式・配点は v4 と同じ） / v4.2: 計算処理の修正（景気連動株の正常化EPSで、決算後の株式分割を株数に反映。式・配点は v4 と同じ） / v4.1: 計算処理の修正（分割後の基準で出た予想EPSを二重に分割補正しない。式・配点は v4 と同じ） / v2: 景気循環株の自動推計を正常化利益に変更 / v3: 調査前提の3シナリオと評価段階 / v4: 株式分割を希薄化と誤判定しない


def version_label(weights: dict[str, dict[str, float]]) -> str:
    custom = bool(weights.get("growth") or weights.get("sell"))
    return f"{METHOD_VERSION}（{'利用者設定の配点' if custom else '成長期待・売りは暫定配点'}）"


def load_rules(conn: sqlite3.Connection) -> RuleSet:
    overrides = get_setting(conn, "rules", {}) or {}
    return RuleSet(**overrides)


def load_weights(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """成長期待点・売り点数の重みの上書き（settings）。未設定なら暫定配点のまま。"""
    return {"growth": get_setting(conn, "growth_weights", {}) or {}, "sell": get_setting(conn, "sell_weights", {}) or {}}


def rules_fingerprint(rules: RuleSet, weights: dict[str, Any] | None = None) -> str:
    """判定閾値と配点の組み合わせを短いIDにする（どちらかを変えるとバージョンが変わる）。"""
    import hashlib
    payload = {"rules": rules.to_dict(), "weights": weights or {}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:8]


def closes_until(conn: sqlite3.Connection, code: str, d: date, n: int = 260) -> list[tuple[str, float]]:
    """調整後終値（無ければ終値）を古い順に返す。"""
    rows = conn.execute(
        """SELECT date, COALESCE(adj_close, close) AS c FROM daily_quotes
           WHERE code=? AND date<=? AND close IS NOT NULL ORDER BY date DESC LIMIT ?""",
        (code, d.isoformat(), n),
    ).fetchall()
    return [(r["date"], r["c"]) for r in reversed(rows)]


def _growth_drop(conn: sqlite3.Connection, code: str, d: date, current: int | None,
                 table: str = "scores") -> float | None:
    if current is None:
        return None
    r = conn.execute(
        f"""SELECT growth_score FROM {table} WHERE code=? AND run_date<=? AND growth_score IS NOT NULL
           ORDER BY run_date DESC LIMIT 1""",
        (code, (d - timedelta(days=90)).isoformat()),
    ).fetchone()
    return max(0.0, r["growth_score"] - current) if r else None



def _unrated_detail(conn: sqlite3.Connection, code: str, d: date) -> dict[str, Any]:
    kind, why = assumptions.unrated_reason(conn, code, d)
    return {"reason": why, "unrated_kind": kind}


def scoring_assumption(conn: sqlite3.Connection, code: str, d: date) -> tuple[Any, str, str, Any, dict[str, Any] | None]:
    """v4 の採点に使う前提を選ぶ（調査前提 base → 登録済み → 自動初期推計の順）。explain も同じものを使う。"""
    sa = research.scenario_assumptions(conn, code, d)
    if sa:
        return sa["assumptions"]["base"], "research", sa["research"]["status"], sa["research"]["confidence"], sa
    a, origin = assumptions.resolve(conn, code, d)
    # 利用者が画面で登録した前提は検証済み、自動初期推計・未登録は前提未確認
    return a, origin, ("verified" if (a and origin == "manual") else "unconfirmed"), None, None


def score_one(conn: sqlite3.Connection, code: str, d: date, rules: RuleSet,
              weights: dict[str, dict[str, float]] | None = None, table: str = "scores") -> dict[str, Any]:
    """table="scores_watch" は監視枠（正式な監視銘柄以外）の並行採点。計算方式は同じで、保存先だけ分ける。"""
    weights = weights if weights is not None else load_weights(conn)
    series = closes_until(conn, code, d)
    # 当日の確定終値が無い銘柄は株価依存の点数を出さない（古い株価で代用しない）
    price = series[-1][1] if series and series[-1][0] == d.isoformat() else None
    raw_close = conn.execute("SELECT close FROM daily_quotes WHERE code=? AND date=?", (code, d.isoformat())).fetchone()
    if raw_close and raw_close["close"]:
        price = raw_close["close"]
    ind = compute_all([c for _, c in series]) if price else {}

    a, origin, status, confidence, sa = scoring_assumption(conn, code, d)
    scen: dict[str, Any] = {}
    if sa:
        for k, sa_k in sa["assumptions"].items():
            v_k = evaluate(sa_k, price, d)
            scen[k] = {"eps": sa_k.eps_forecast or sa_k.normalized_eps, "per": sa_k.assumed_per,
                       "theoretical_value": v_k.theoretical_value, "target_price": v_k.target_price, "buy_score": v_k.buy_score}
    val = evaluate(a, price, d)
    target_eps = (a.eps_forecast or a.normalized_eps) if a else None
    years = years_until(a.target_year, d, a.fiscal_month_end) if a else None
    feats = build_features(conn, code, d, price, ind, target_eps, years)

    g = growth_score(feats, weights["growth"])
    s = (sell_score(feats, val.v_over_p, _growth_drop(conn, code, d, g["score"], table), weights["sell"])
         if price else {"score": None, "factors": {}})
    buy = val.buy_score
    bd = buy_decision(g["score"], buy, s["score"], rules)
    sd = sell_decision(s["score"], rules)
    et = exit_type(s, g["score"], rules)
    row = {
        "run_date": d.isoformat(), "code": code,
        "price": price, "price_date": d.isoformat() if price else None,
        "growth_score": g["score"], "growth_detail": json.dumps(g, ensure_ascii=False),
        "buy_score": buy, "buy_score_k150": val.buy_score_k150, "buy_status": val.status,
        "buy_detail": json.dumps({
            **val.to_dict(), **(_unrated_detail(conn, code, d) if a is None else {}), "assumption_origin": origin, "assumption_status": status, "confidence": confidence,
            "scenarios": scen, "anchor_ratio": sa["anchor_ratio"] if sa else None,
            "data_issue": research.data_issue_on(conn, code, d),
        }, ensure_ascii=False, default=str),
        "theoretical_value": val.theoretical_value, "future_price": val.future_price, "target_price": val.target_price,
        "sell_score": s["score"], "sell_detail": json.dumps(s, ensure_ascii=False),
        "buy_decision": bd, "sell_decision": sd, "exit_type": et,
        "indicators": json.dumps({**ind, "sources": feats.sources}, ensure_ascii=False),
        "method_version": f"{version_label(weights)} / rules:{rules_fingerprint(rules, weights)}",
        "created_at": now_iso(),
    }
    if table != "scores":
        upsert(conn, table, row, ["run_date", "code"])
        return row
    old = conn.execute("SELECT method_version FROM scores WHERE run_date=? AND code=?", (row["run_date"], code)).fetchone()
    if old and old["method_version"] != row["method_version"]:
        # 方式変更で同じ日を再計算する場合、変更前の結果を保存してから上書きする
        conn.execute("INSERT INTO scores_archive SELECT *, ? FROM scores WHERE run_date=? AND code=?",
                     (now_iso(), row["run_date"], code))
    upsert(conn, "scores", row, ["run_date", "code"])
    return row


def run_scoring(conn: sqlite3.Connection, d: date, codes: list[str] | None = None) -> list[dict[str, Any]]:
    rules = load_rules(conn)
    weights = load_weights(conn)
    out = [score_one(conn, c, d, rules, weights) for c in (codes or active_codes(conn))]
    conn.commit()
    return out
