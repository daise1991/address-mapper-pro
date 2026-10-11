"""評価前提（予想EPS・想定PER・必要利回り・安全余裕・手法）の管理。

利用者が登録した前提（origin=manual）を最優先する。未登録の銘柄には
会社予想から機械的に作る「自動初期推計」（origin=auto）を用意し、理由を明記する。
赤字・新興赤字・ETF は自動推計せず未算定とする。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import asdict, fields
from datetime import date
from typing import Any

from .db import now_iso, upsert
from .scoring.valuation import CATEGORY_DEFAULTS, Assumption

# 自動初期推計の前提（検証用）。分類ごとの想定PERと、会社予想EPSから目標年度までの年成長率。
AUTO_PER = {"stable": 15.0, "growth": 25.0, "cyclical": 14.0}
# 景気連動で売上が年この率以上で伸びている会社は、過去の利益額ではなく「利益率の中央値×最新の売上」で正常化する
STRUCTURAL_REVENUE_CAGR = 0.15
AUTO_GROWTH = {"stable": 0.05, "growth": 0.15, "cyclical": 0.05}
DEFAULT_TARGET_YEAR = 2029


def load(conn: sqlite3.Connection, code: str) -> tuple[Assumption | None, str | None]:
    r = conn.execute("SELECT * FROM valuation_assumptions WHERE code=?", (code,)).fetchone()
    if not r:
        return None, None
    names = {f.name for f in fields(Assumption)}
    return Assumption(**{k: r[k] for k in r.keys() if k in names}), r["origin"]


def save(conn: sqlite3.Connection, code: str, a: Assumption, origin: str = "manual") -> None:
    if not a.reason or not a.reason.strip():
        raise ValueError("前提を変更する理由（reason）は必須です")
    row = {"code": code, **asdict(a), "origin": origin, "updated_at": now_iso()}
    upsert(conn, "valuation_assumptions", row, ["code"])
    conn.execute("INSERT INTO valuation_assumption_history (code, payload, changed_at) VALUES (?,?,?)",
                 (code, json.dumps(row, ensure_ascii=False), row["updated_at"]))
    conn.commit()



def unrated_reason(conn: sqlite3.Connection, code: str, as_of: date) -> tuple[str, str]:
    """前提が無く買い点数を出せないときの区分と理由（auto_baseline が None を返す条件と同じ順で判定）。
    区分: 対象外（ETF・赤字成長など自動推計の対象でない）／データ不足（決算・予想が無い、赤字）。"""
    sec = conn.execute("SELECT category, asset_type FROM securities WHERE code=?", (code,)).fetchone()
    if not sec:
        return "対象外", "銘柄が未登録"
    if sec["asset_type"] != "stock":
        return "対象外", "ETF など個別企業でないため理論価値を計算しない"
    if sec["category"] not in AUTO_PER:
        return "対象外", "赤字成長企業は利益×PER で計算できないため、個別の調査前提が必要（未作成）"
    cut = f"{as_of.isoformat()}T23:59:59+09:00"
    fy = conn.execute("""SELECT * FROM financials WHERE code=? AND period_type='FY' AND disclosed_at<=?
                         ORDER BY fiscal_period_end DESC, disclosed_at DESC LIMIT 1""", (code, cut)).fetchone()
    if not fy:
        n = conn.execute("SELECT COUNT(*) FROM financials WHERE code=?", (code,)).fetchone()[0]
        return "データ不足", "決算データが未取得（取得失敗）" if n == 0 else "本決算のデータが未取得"
    from .scoring.features import latest_forecast
    lf = latest_forecast(conn, code, fy["fiscal_period_end"], cut)
    fc = (lf and lf["forecast_eps"]) or fy["forecast_eps"]
    if sec["category"] == "cyclical":
        return "データ不足", "過去の純利益が2期未満、または正常化利益が0以下のため自動推計できない"
    if fc is None and (fy["eps"] is None or fy["eps"] <= 0):
        return "データ不足", f"会社予想EPSの公表なし・直近本決算EPS {fy['eps']}円（赤字）のため自動推計できない"
    if fc is not None and fc <= 0:
        return "データ不足", f"会社予想EPS {fc}円（赤字予想）のため自動推計できない"
    return "データ不足", "正常化利益が0以下、または過去の純利益が2期未満"


def auto_baseline(conn: sqlite3.Connection, code: str, as_of: date) -> Assumption | None:
    sec = conn.execute("SELECT category, asset_type FROM securities WHERE code=?", (code,)).fetchone()
    if not sec or sec["asset_type"] != "stock" or sec["category"] not in AUTO_PER:
        return None
    fy = conn.execute(
        """SELECT * FROM financials WHERE code=? AND period_type='FY' AND disclosed_at<=?
           ORDER BY fiscal_period_end DESC, disclosed_at DESC LIMIT 1""",
        (code, f"{as_of.isoformat()}T23:59:59+09:00"),
    ).fetchone()
    if not fy:
        return None
    from .scoring.features import latest_forecast
    lf = latest_forecast(conn, code, fy["fiscal_period_end"], f"{as_of.isoformat()}T23:59:59+09:00")
    base_eps = (lf and lf["forecast_eps"]) or fy["forecast_eps"] or fy["eps"]
    # 理由文に出す基準EPSの種類（会社予想が無い会社は直近の実績EPSで代用している）
    eps_kind = "会社予想EPS" if ((lf and lf["forecast_eps"]) or fy["forecast_eps"]) else f"直近実績EPS（{fy['fiscal_period_end'][:7]}期、会社予想の公表なし）"
    cat = sec["category"]
    fy_end = date.fromisoformat(fy["fiscal_period_end"])
    d = CATEGORY_DEFAULTS[cat]
    common = dict(target_year=DEFAULT_TARGET_YEAR, fiscal_month_end=fy_end.month,
                  required_return=d["required_return"], safety_margin=d["safety_margin"], assumed_per=AUTO_PER[cat])
    tail = (f"{d['label']}の初期仮定（必要利回り{d['required_return']:.1%}・安全余裕{d['safety_margin']:.0%}）。要個別見直し")

    if cat == "cyclical":
        # 景気循環株は直近の利益水準ではなく、過去最大5期の純利益と今期予想の中央値を「正常化利益」とする。
        # 株式分割の影響を受けないよう、EPS ではなく純利益を現在の株式数で割る
        hist = conn.execute(
            """SELECT fiscal_period_end, MAX(disclosed_at), net_income, revenue, shares_outstanding, treasury_shares
               FROM financials WHERE code=? AND period_type='FY' AND disclosed_at<=? AND net_income IS NOT NULL
               GROUP BY fiscal_period_end ORDER BY fiscal_period_end DESC LIMIT 5""",
            (code, f"{as_of.isoformat()}T23:59:59+09:00")).fetchall()
        from .scoring.features import current_shares, forecast_split_factor
        shares = current_shares(conn, code, fy, as_of)  # 決算後の株式分割を反映した株数
        if shares <= 0:
            return None
        profits = [h["net_income"] for h in hist]
        fc_ni, fc_note = (lf and lf["forecast_net_income"]), ""
        if fc_ni is None and lf and lf["forecast_eps"]:
            # 予想純利益が空欄で予想EPSだけある会社は、予想EPS（評価日の株数基準に補正）×現在の株式数で補う
            fc_ni = lf["forecast_eps"] * forecast_split_factor(conn, code, lf, as_of) * shares
            fc_note = "（今期予想純利益は予想EPS×株式数で補完）"
        if fc_ni is not None:
            profits.append(fc_ni)
        if len(profits) < 2:
            return None
        fc_part = "と今期予想" if fc_ni is not None else "（会社予想の公表なし）"
        revs = [(h["fiscal_period_end"], h["revenue"]) for h in hist if h["revenue"] and h["revenue"] > 0]
        cagr = None
        if len(revs) >= 3:
            yrs = (date.fromisoformat(revs[0][0]) - date.fromisoformat(revs[-1][0])).days / 365.25
            cagr = (revs[0][1] / revs[-1][1]) ** (1 / yrs) - 1 if yrs > 0 else None
        if cagr is not None and cagr >= STRUCTURAL_REVENUE_CAGR:
            # 事業規模が大きく変わった会社は、小さかった頃の利益額の中央値では過小になるため利益率で正常化する
            margins = [h["net_income"] / h["revenue"] for h in hist if h["revenue"] and h["revenue"] > 0]
            fc_rev = lf and lf["forecast_revenue"]
            if fc_ni is not None and fc_rev and fc_rev > 0:
                margins.append(fc_ni / fc_rev)
            rev_now = fc_rev if fc_rev and fc_rev > 0 else revs[0][1]
            margin = statistics.median(margins)
            normalized = margin * rev_now / shares
            how = (f"売上が年{cagr:.0%}で拡大しているため、過去{len(hist)}期{fc_part}の純利益率の中央値 {margin:.1%}"
                   f"×{'今期予想' if rev_now == fc_rev else '直近'}売上 {rev_now / 1e8:,.0f}億円÷現在の株式数{fc_note}")
        else:
            normalized = statistics.median(profits) / shares
            how = f"過去{len(hist)}期{fc_part}の純利益の中央値÷現在の株式数{fc_note}"
        if normalized <= 0:
            return None
        return Assumption(method="normalized", normalized_eps=normalized, **common,
                          reason=(f"自動初期推計: 正常化EPS {normalized:.1f}円（{how}）、"
                                  f"想定PER {AUTO_PER[cat]:.0f}倍、" + tail))

    if base_eps is None or base_eps <= 0:
        return None

    base_year = fy_end.year + 1  # 予想は翌期
    n = max(DEFAULT_TARGET_YEAR - base_year, 0)
    g = AUTO_GROWTH[cat]
    eps_target = base_eps * (1 + g) ** n
    return Assumption(method="per", eps_forecast=eps_target, **common,
                      reason=f"自動初期推計: {eps_kind} {base_eps:.1f}円×(1+{g:.0%})^{n}、想定PER {AUTO_PER[cat]:.0f}倍、" + tail)


def resolve(conn: sqlite3.Connection, code: str, as_of: date) -> tuple[Assumption | None, str]:
    a, origin = load(conn, code)
    if a is not None:
        return a, origin or "manual"
    return auto_baseline(conn, code, as_of), "auto"


def to_dict(a: Assumption | None) -> dict[str, Any] | None:
    return asdict(a) if a else None
