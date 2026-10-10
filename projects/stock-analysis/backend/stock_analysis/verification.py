"""過去の採点結果と実際の株価の検証、売買ルールの精度比較。

- 将来リターンは調整後終値で計算（分割の影響を除く）
- 当日に保存したスコア（live）と、後から再計算したスコア（backfill）を区別する。
  backfill は評価前提が「現在の値」のため先読みバイアスを含み、参考値として扱う。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date
from statistics import mean
from typing import Any, Callable

from .db import now_iso, upsert
from .scoring.rules import RuleSet
from .scoring.valuation import buy_score_from_ratio, K_LEGACY

HORIZONS = (5, 20, 60, 120, 250)  # 営業日（約1週間・1か月・3か月・半年・1年）
BENCHMARKS = {"universe": "監視銘柄の同日平均", "TOPIX": "TOPIX", "2644": "GX半導体日株(2644)"}


def _price_on_or_after(conn: sqlite3.Connection, code: str, after: str, offset: int) -> tuple[str, float] | None:
    r = conn.execute(
        """SELECT date, COALESCE(adj_close, close) AS c FROM daily_quotes
           WHERE code=? AND date>? AND close IS NOT NULL ORDER BY date LIMIT 1 OFFSET ?""",
        (code, after, offset - 1),
    ).fetchone()
    return (r["date"], r["c"]) if r else None


def compute_forward_returns(conn: sqlite3.Connection, horizons: tuple[int, ...] = HORIZONS) -> int:
    n = 0
    for s in conn.execute("SELECT run_date, code FROM scores WHERE price IS NOT NULL").fetchall():
        entry = conn.execute("SELECT COALESCE(adj_close, close) AS c FROM daily_quotes WHERE code=? AND date=?",
                             (s["code"], s["run_date"])).fetchone()
        if not entry or not entry["c"]:
            continue
        for h in horizons:
            if conn.execute("SELECT 1 FROM verifications WHERE score_date=? AND code=? AND horizon=?",
                            (s["run_date"], s["code"], h)).fetchone():
                continue
            ex = _price_on_or_after(conn, s["code"], s["run_date"], h)
            if not ex:
                continue
            upsert(conn, "verifications", {
                "score_date": s["run_date"], "code": s["code"], "horizon": h,
                "entry_price": entry["c"], "exit_price": ex[1], "exit_date": ex[0],
                "forward_return": ex[1] / entry["c"] - 1,
                "max_drawdown": _max_drawdown(conn, s["code"], s["run_date"], ex[0], entry["c"]),
                "created_at": now_iso(),
            }, ["score_date", "code", "horizon"])
            n += 1
    # 列追加前に作られた検証行の最大下落幅を補完する
    for v in conn.execute("SELECT score_date, code, horizon, exit_date, entry_price FROM verifications WHERE max_drawdown IS NULL").fetchall():
        conn.execute("UPDATE verifications SET max_drawdown=? WHERE score_date=? AND code=? AND horizon=?",
                     (_max_drawdown(conn, v["code"], v["score_date"], v["exit_date"], v["entry_price"]),
                      v["score_date"], v["code"], v["horizon"]))
    conn.commit()
    return n


def _max_drawdown(conn: sqlite3.Connection, code: str, start: str, end: str, entry: float) -> float | None:
    """評価日の終値から、期間中の最安値（調整後終値）までの下落率。上昇し続けた場合は 0。"""
    r = conn.execute("""SELECT MIN(COALESCE(adj_close, close)) FROM daily_quotes
                        WHERE code=? AND date>? AND date<=?""", (code, start, end)).fetchone()
    return min(0.0, r[0] / entry - 1) if r and r[0] and entry else None


def _rank(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 5:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def _bench_close(conn: sqlite3.Connection, name: str, d: str) -> float | None:
    if name == "TOPIX":
        r = conn.execute("SELECT close FROM benchmark_quotes WHERE name='TOPIX' AND date=?", (d,)).fetchone()
        return r["close"] if r else None
    r = conn.execute("SELECT COALESCE(adj_close, close) AS c FROM daily_quotes WHERE code=? AND date=?", (name, d)).fetchone()
    return r["c"] if r else None


def _load(conn: sqlite3.Connection, horizon: int, live_only: bool, benchmark: str = "universe",
          method_version: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT s.*, v.forward_return, v.exit_date, v.max_drawdown, json_extract(s.buy_detail, '$.v_over_p') AS v_over_p
             FROM scores s JOIN verifications v ON v.score_date=s.run_date AND v.code=s.code AND v.horizon=?
             WHERE 1=1"""
    params: list[Any] = [horizon]
    if live_only:
        sql += " AND substr(s.created_at,1,10)=s.run_date"
    if method_version:
        sql += " AND s.method_version=?"
        params.append(method_version)
    rows = [dict(r) for r in conn.execute(sql, params)]
    if benchmark == "universe":
        by_date: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            by_date[r["run_date"]].append(r["forward_return"])
        avg = {d: mean(v) for d, v in by_date.items()}
        for r in rows:
            r["excess_return"] = r["forward_return"] - avg[r["run_date"]]
        return rows
    cache: dict[tuple[str, str], float | None] = {}
    for r in rows:
        a = cache.setdefault((r["run_date"], "a"), _bench_close(conn, benchmark, r["run_date"]))
        b = cache.setdefault((r["exit_date"], "b"), _bench_close(conn, benchmark, r["exit_date"]))
        # 比較対象の株価が無い期間は超過リターンを出さない（推測で埋めない）
        r["excess_return"] = (r["forward_return"] - (b / a - 1)) if (a and b) else None
    return rows


def _stats(rows: list[dict[str, Any]], sell_side: bool = False) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    rets = [r["forward_return"] for r in rows]
    ex = [r["excess_return"] for r in rows if r["excess_return"] is not None]
    hit = [x < 0 for x in ex] if sell_side else [x > 0 for x in ex]
    return {
        "n": len(rows),
        "mean_return": round(mean(rets), 4),
        "mean_excess": round(mean(ex), 4) if ex else None,
        "hit_rate": round(sum(hit) / len(hit), 3) if hit else None,
        "worst": round(min(rets), 4),
        "mean_max_drawdown": (round(mean(dd), 4) if (dd := [r["max_drawdown"] for r in rows if r.get("max_drawdown") is not None]) else None),
        "worst_max_drawdown": round(min(dd), 4) if dd else None,
    }


def rule_signals(r: RuleSet) -> dict[str, tuple[Callable[[dict[str, Any]], bool], bool]]:
    def k150_buy(x: dict[str, Any]) -> int | None:
        return buy_score_from_ratio(x["v_over_p"], K_LEGACY) if x.get("v_over_p") is not None else None

    def normal(buy_key: Callable[[dict[str, Any]], int | None]) -> Callable[[dict[str, Any]], bool]:
        return lambda x: (x["growth_score"] or 0) >= r.normal_growth and (buy_key(x) or 0) >= r.normal_buy \
            and x["sell_score"] is not None and x["sell_score"] <= r.normal_sell_max

    def aggressive(buy_key: Callable[[dict[str, Any]], int | None]) -> Callable[[dict[str, Any]], bool]:
        return lambda x: (x["growth_score"] or 0) >= r.aggressive_growth and (buy_key(x) or 0) >= r.aggressive_buy \
            and x["sell_score"] is not None and x["sell_score"] <= r.aggressive_sell_max

    return {
        "買い85点以上(k=200)": (lambda x: (x["buy_score"] or 0) >= 85, False),
        "買い85点以上(k=150 比較)": (lambda x: (k150_buy(x) or 0) >= 85, False),
        "買い75点以上(k=200)": (lambda x: (x["buy_score"] or 0) >= 75, False),
        "買い75点以上(k=150 比較)": (lambda x: (k150_buy(x) or 0) >= 75, False),
        "積極購入(k=200)": (aggressive(lambda x: x["buy_score"]), False),
        "積極購入(k=150 比較)": (aggressive(k150_buy), False),
        "通常購入以上(k=200)": (normal(lambda x: x["buy_score"]), False),
        "通常購入以上(k=150 比較)": (normal(k150_buy), False),
        "購入条件確認(65-74)": (lambda x: x["buy_decision"] == "check", False),
        "割安待ち": (lambda x: x["buy_decision"] == "wait_discount", False),
        "売却優先検討(85+)": (lambda x: x["sell_decision"] == "priority", True),
        "売却候補(75-84)": (lambda x: x["sell_decision"] == "candidate", True),
        "一部売却検討(65-74)": (lambda x: x["sell_decision"] == "partial", True),
        "利益確定候補": (lambda x: x["exit_type"] == "profit_taking", True),
        "損切り・撤退候補": (lambda x: x["exit_type"] == "stop_loss", True),
    }


def summary(conn: sqlite3.Connection, horizon: int = 60, live_only: bool = False, rules: RuleSet = RuleSet(),
            benchmark: str = "universe", method_version: str | None = None) -> dict[str, Any]:
    rows = _load(conn, horizon, live_only, benchmark, method_version)
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        b = r["buy_score"]
        key = "未算定" if b is None else "85+" if b >= 85 else "75-84" if b >= 75 else "65-74" if b >= 65 else "50-64" if b >= 50 else "0-49"
        buckets[key].append(r)
    rated = [r for r in rows if r["buy_score"] is not None]
    sold = [r for r in rows if r["sell_score"] is not None]
    grown = [r for r in rows if r["growth_score"] is not None]
    return {
        "horizon": horizon,
        "live_only": live_only,
        "benchmark": benchmark,
        "benchmark_label": BENCHMARKS.get(benchmark, benchmark),
        "method_version": method_version,
        "versions": [r[0] for r in conn.execute("SELECT DISTINCT method_version FROM scores WHERE method_version IS NOT NULL")],
        "n": len(rows),
        "ic": {
            "buy": spearman([r["buy_score"] for r in rated], [r["forward_return"] for r in rated]),
            "buy_v_over_p": spearman([r["v_over_p"] for r in rated if r["v_over_p"] is not None],
                                     [r["forward_return"] for r in rated if r["v_over_p"] is not None]),
            "sell": spearman([r["sell_score"] for r in sold], [r["forward_return"] for r in sold]),
            "growth": spearman([r["growth_score"] for r in grown], [r["forward_return"] for r in grown]),
        },
        "buy_buckets": {k: _stats(v) for k, v in sorted(buckets.items())},
        "rules": {name: _stats([r for r in rows if f(r)], sell_side) for name, (f, sell_side) in rule_signals(rules).items()},
        "disclaimer": "検証で有効性が確認されるまで、点数は投資成果を保証する指標ではありません。",
        "note": "後から再計算した分（backfill）は評価前提が現在の値のため先読みバイアスを含む。当日保存分のみ（live_only）が本来の検証値。"
                "超過リターン・的中率は比較対象に対する値。",
    }


def backfill_scores(conn: sqlite3.Connection, start: date, end: date, step: int = 5) -> int:
    """過去営業日の採点を後から計算する（step 営業日おき）。"""
    from .analysis import run_scoring
    from .market_calendar import business_days

    days = business_days(start, end, conn)[::step]
    for d in days:
        run_scoring(conn, d)
    return len(days)


MIN_SAMPLES = 30  # これ未満の比較は「判断保留」とする


def _compare(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    if a.get("n", 0) < MIN_SAMPLES or b.get("n", 0) < MIN_SAMPLES or a.get("mean_excess") is None or b.get("mean_excess") is None:
        return None
    return a["mean_excess"] - b["mean_excess"]


def improvement_report(conn: sqlite3.Connection, rules: RuleSet = RuleSet(), benchmark: str = "TOPIX") -> dict[str, Any]:
    """検証結果から売買ルールの改善案を作る。採点方式そのものは変更しない（提案のみ）。"""
    live_n = conn.execute(
        "SELECT COUNT(*) FROM scores s JOIN verifications v ON v.score_date=s.run_date AND v.code=s.code "
        "WHERE v.horizon=60 AND substr(s.created_at,1,10)=s.run_date").fetchone()[0]
    live_only = live_n >= MIN_SAMPLES * 3
    items: list[dict[str, Any]] = []

    for h in (20, 60, 120):
        s = summary(conn, h, live_only, rules, benchmark)
        label = {20: "1か月", 60: "3か月", 120: "6か月"}[h]
        r = s["rules"]
        for th in (75, 85):
            a, b = r.get(f"買い{th}点以上(k=200)", {}), r.get(f"買い{th}点以上(k=150 比較)", {})
            diff = _compare(a, b)
            if diff is None:
                finding = f"サンプル不足（k=200: {a.get('n', 0)}件 / k=150: {b.get('n', 0)}件）"
                suggestion = "判断保留。データが貯まるまで係数200を維持"
            elif abs(diff) < 0.005:
                finding = f"差はほぼ無し（{diff:+.1%}）"
                suggestion = "係数200を維持"
            else:
                better = "k=200" if diff > 0 else "k=150"
                finding = (f"{better} の方が比較対象との差が {abs(diff):.1%} 大きい"
                           f"（k=200: {a['mean_excess']:+.1%}, n={a['n']} / k=150: {b['mean_excess']:+.1%}, n={b['n']}）")
                suggestion = "係数200を維持" if diff > 0 else "係数150への変更を検討（変更する場合は計算方式のバージョンを上げる）"
            items.append({"topic": f"買い{th}点以上の係数比較（{label}後）", "finding": finding, "suggestion": suggestion})

        ic = s["ic"]
        if h == 60:
            if ic.get("buy") is not None:
                good = ic["buy"] > 0.03
                items.append({"topic": "買い点数の有効性（3か月後）",
                              "finding": f"買い点数と将来リターンの順位相関 IC={ic['buy']:+.3f}",
                              "suggestion": "有効に機能" if good else "効きが弱い。景気循環株の正常化利益や必要利回りの見直しを検討"})
            if ic.get("sell") is not None:
                good = ic["sell"] < -0.03
                items.append({"topic": "売り点数の有効性（3か月後）",
                              "finding": f"売り点数と将来リターンの順位相関 IC={ic['sell']:+.3f}（マイナスが望ましい）",
                              "suggestion": "有効に機能" if good else "売り点数が下落を予測できていない。割高度・過熱の重みの見直しを検討"})
            buckets = [(k, v) for k, v in s["buy_buckets"].items() if k != "未算定" and v.get("n", 0) >= 10 and v.get("mean_excess") is not None]
            order = ["0-49", "50-64", "65-74", "75-84", "85+"]
            buckets.sort(key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
            if len(buckets) >= 3:
                rho = spearman(list(range(len(buckets))), [v["mean_excess"] for _, v in buckets])
                items.append({"topic": "買い点数帯の並び（3か月後）",
                              "finding": "点数帯ごとの比較対象との差: " + "、".join(f"{k}: {v['mean_excess']:+.1%}" for k, v in buckets),
                              "suggestion": "点数が高いほど成績が良く、閾値は妥当" if rho and rho > 0.5
                              else "点数と成績の並びが崩れている。閾値（75/85）の見直しを検討"})
            for name in ("利益確定候補", "損切り・撤退候補"):
                st = r.get(name, {})
                if st.get("n", 0) >= MIN_SAMPLES and st.get("hit_rate") is not None:
                    items.append({"topic": f"{name}の的中率（3か月後）",
                                  "finding": f"比較対象を下回った割合 {st['hit_rate']:.0%}（n={st['n']}）",
                                  "suggestion": "有効" if st["hit_rate"] >= 0.55 else "区別の基準（業績・財務系の売り要因の平均50以上など）の見直しを検討"})
    return {
        "basis": "当日保存分のみ（先読みなし）" if live_only else
                 f"後から再計算した分を含む（当日保存分が {live_n}件と少ないため。先読みバイアスあり、参考値）",
        "benchmark": BENCHMARKS.get(benchmark, benchmark),
        "items": items,
        "note": "提案のみで、採点方式・閾値は変更していません。採用する場合は承認のうえ計算方式のバージョンを上げます。",
    }
