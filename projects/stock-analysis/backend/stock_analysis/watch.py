"""監視枠（主力・高成長研究・過去）と並行採点・成績追跡。

- 正式な監視銘柄（securities.active）・注文候補・通知は変えない。監視枠の採点は scores_watch に分けて保存する。
- 計算方式は正式（v4）と同じ。採点はその日の情報だけで行い、過去の日の点数を後から作り直さない。
- 枠から外しても行は消さない（removed_at を付け、過去監視枠へ移して成績を追い続ける）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from .db import now_iso
from .universe import active_codes

TIERS = {"main": "主力監視枠", "research": "高成長研究枠", "past": "過去監視枠"}
RESEARCH_MAX = 10
HORIZONS = {"1か月": 20, "3か月": 60, "6か月": 120}  # 営業日

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlists (
    list_name TEXT NOT NULL,           -- 例: current-2026-10（現20）/ proposal-2026-10（新20案）
    tier TEXT NOT NULL,                -- main / research / past
    code TEXT NOT NULL,
    name TEXT,
    reason TEXT,
    added_at TEXT NOT NULL,            -- 枠に入れた日（成績の起点）
    removed_at TEXT,                   -- 枠から外した日（行は消さない）
    created_at TEXT NOT NULL,
    PRIMARY KEY (list_name, tier, code, added_at)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("CREATE TABLE IF NOT EXISTS scores_watch AS SELECT * FROM scores WHERE 0")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_scores_watch ON scores_watch(run_date, code)")


def add(conn: sqlite3.Connection, list_name: str, tier: str, items: list[dict[str, Any]], d: date) -> int:
    """items: [{code, name, reason, category?}]。研究枠は1つの案につき最大10銘柄。"""
    if tier not in TIERS:
        raise ValueError(f"枠の種類が不正: {tier}")
    if tier == "research":
        n = conn.execute("SELECT COUNT(*) FROM watchlists WHERE list_name=? AND tier='research' AND removed_at IS NULL",
                         (list_name,)).fetchone()[0]
        if n + len(items) > RESEARCH_MAX:
            raise ValueError(f"高成長研究枠は最大{RESEARCH_MAX}銘柄（現在{n}、追加{len(items)}）")
    added = 0
    for it in items:
        cur = conn.execute(
            "INSERT OR IGNORE INTO watchlists (list_name, tier, code, name, reason, added_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (list_name, tier, it["code"], it.get("name"), it.get("reason"), d.isoformat(), now_iso()))
        added += cur.rowcount
        # 株価・決算の取得と採点に必要な銘柄情報。正式な監視銘柄にはしない（active=0）
        conn.execute(
            """INSERT OR IGNORE INTO securities (code, name, group_name, category, active, note, added_at)
               VALUES (?,?,?,?,0,?,?)""",
            (it["code"], it.get("name") or it["code"], "watch",
             it.get("category") or ("emerging_loss" if tier == "research" else "growth"),
             "監視枠（正式な監視銘柄ではない）", now_iso()))
    conn.commit()
    return added


def remove(conn: sqlite3.Connection, list_name: str, tier: str, code: str, d: date, reason: str) -> bool:
    """枠から外す。行は消さず、過去監視枠へ移して成績を追い続ける。"""
    cur = conn.execute("UPDATE watchlists SET removed_at=? WHERE list_name=? AND tier=? AND code=? AND removed_at IS NULL",
                       (d.isoformat(), list_name, tier, code))
    if cur.rowcount and tier != "past":
        name = conn.execute("SELECT name FROM watchlists WHERE list_name=? AND tier=? AND code=? ORDER BY added_at DESC",
                            (list_name, tier, code)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO watchlists (list_name, tier, code, name, reason, added_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (list_name, "past", code, name[0] if name else None, f"{TIERS[tier]}から除外: {reason}", d.isoformat(), now_iso()))
    conn.commit()
    return bool(cur.rowcount)


def members(conn: sqlite3.Connection, list_name: str, tier: str | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM watchlists WHERE list_name=? AND removed_at IS NULL"
    args: list[Any] = [list_name]
    if tier:
        q += " AND tier=?"
        args.append(tier)
    return conn.execute(q + " ORDER BY tier, code", args).fetchall()


def shadow_codes(conn: sqlite3.Connection) -> list[str]:
    """監視枠にあり、正式な監視銘柄ではない銘柄（並行採点・成績追跡の対象）。"""
    active = set(active_codes(conn))
    return [r[0] for r in conn.execute("SELECT DISTINCT code FROM watchlists WHERE removed_at IS NULL ORDER BY code")
            if r[0] not in active]


def fetch(conn: sqlite3.Connection, provider: Any, d: date, tag: str) -> str:
    """正式銘柄の取得が終わった後に行う。失敗しても正式の処理は止めない。"""
    from .pipeline import fetch_financials, fetch_quotes
    codes = shadow_codes(conn)
    if not codes:
        return "監視枠の追加取得なし"
    fetch_quotes(conn, provider, codes, d, tag)
    fetch_financials(conn, provider, codes, d, tag)
    return f"監視枠 {len(codes)}銘柄の終値・決算を取得"


def score(conn: sqlite3.Connection, d: date) -> int:
    from .analysis import load_rules, load_weights, score_one
    rules, weights = load_rules(conn), load_weights(conn)
    codes = shadow_codes(conn)
    for c in codes:
        score_one(conn, c, d, rules, weights, table="scores_watch")
    conn.commit()
    return len(codes)


# ---- 成績追跡（1・3・6か月後の株価と TOPIX の比較） ----

def _bars_from(conn: sqlite3.Connection, code: str, start: str, n: int) -> list[sqlite3.Row]:
    return conn.execute("""SELECT date, COALESCE(adj_close, close) AS c FROM daily_quotes
                           WHERE code=? AND date>=? AND close IS NOT NULL ORDER BY date LIMIT ?""",
                        (code, start, n)).fetchall()


def _topix(conn: sqlite3.Connection, d: str) -> float | None:
    r = conn.execute("SELECT close FROM benchmark_quotes WHERE name='TOPIX' AND date=?", (d,)).fetchone()
    return r[0] if r else None


def track_one(conn: sqlite3.Connection, code: str, start: str, as_of: date) -> dict[str, Any]:
    """start 以降の最初の終値を起点に、各期間後の騰落率と TOPIX との差。as_of より後の株価は使わない。"""
    bars = [b for b in _bars_from(conn, code, start, max(HORIZONS.values()) + 1) if b["date"] <= as_of.isoformat()]
    out: dict[str, Any] = {"entry_date": bars[0]["date"] if bars else None, "entry_price": bars[0]["c"] if bars else None}
    for label, h in HORIZONS.items():
        if len(bars) <= h:
            out[label] = None  # 期間未経過または株価未取得
            continue
        e, x = bars[0], bars[h]
        r = x["c"] / e["c"] - 1
        te, tx = _topix(conn, e["date"]), _topix(conn, x["date"])
        tr = tx / te - 1 if te and tx else None
        out[label] = {"exit_date": x["date"], "return": round(r, 4),
                      "topix": round(tr, 4) if tr is not None else None,
                      "excess": round(r - tr, 4) if tr is not None else None}
    return out


def track(conn: sqlite3.Connection, list_name: str, as_of: date) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM watchlists WHERE list_name=? ORDER BY tier, code, added_at", (list_name,)).fetchall()
    return [{"tier": r["tier"], "code": r["code"], "name": r["name"], "added_at": r["added_at"], "removed_at": r["removed_at"],
             **track_one(conn, r["code"], r["added_at"], as_of)} for r in rows]


# ---- 比較（同じ評価日・同じデータ条件） ----

def _score_row(conn: sqlite3.Connection, code: str, d: str) -> sqlite3.Row | None:
    return (conn.execute("SELECT * FROM scores WHERE run_date=? AND code=?", (d, code)).fetchone()
            or conn.execute("SELECT * FROM scores_watch WHERE run_date=? AND code=?", (d, code)).fetchone())


def _buy_kind(bd: dict[str, Any]) -> str:
    if bd.get("buy_score") is None:
        return "未算定"
    if bd.get("assumption_origin") == "research":
        return "正式" if bd.get("assumption_status") == "verified" else "暫定"
    if bd.get("assumption_origin") == "manual":
        return "正式"
    return "参考（仮のEPS・PER）"


def _fin(conn: sqlite3.Connection, code: str, d: str) -> dict[str, Any]:
    cut = f"{d}T23:59:59+09:00"
    fy = conn.execute("""SELECT * FROM financials WHERE code=? AND period_type='FY' AND disclosed_at<=?
                         ORDER BY fiscal_period_end DESC, disclosed_at DESC LIMIT 1""", (code, cut)).fetchone()
    if not fy:
        return {"status": "取得不可"}
    eq_ratio = fy["equity"] / fy["total_assets"] if fy["equity"] and fy["total_assets"] else None
    margin = fy["operating_profit"] / fy["revenue"] if fy["operating_profit"] is not None and fy["revenue"] else None
    debt = conn.execute("SELECT interest_bearing_debt FROM edinet_balance WHERE code=? AND period_end<=? ORDER BY period_end DESC",
                        (code, d)).fetchone()
    return {"period": fy["fiscal_period_end"], "equity_ratio": eq_ratio, "op_margin": margin, "operating_cf": fy["operating_cf"],
            "debt": debt[0] if debt else None}


def compare(conn: sqlite3.Connection, lists: list[str], d: date) -> dict[str, Any]:
    ds = d.isoformat()
    out: dict[str, Any] = {"date": ds, "lists": {}}
    for ln in lists:
        rows = []
        for m in members(conn, ln, "main"):
            s = _score_row(conn, m["code"], ds)
            sec = conn.execute("SELECT sector FROM securities WHERE code=?", (m["code"],)).fetchone()
            v5 = conn.execute("SELECT confidence, severe_risks FROM scores_v5 WHERE run_date=? AND code=?", (ds, m["code"])).fetchone()
            item: dict[str, Any] = {"code": m["code"], "name": m["name"], "sector": sec[0] if sec else None,
                                    "fin": _fin(conn, m["code"], ds),
                                    "confidence": v5["confidence"] if v5 else None,
                                    "severe_risks": [x["type"] if isinstance(x, dict) else str(x)
                                                     for x in json.loads(v5["severe_risks"] or "[]")] if v5 else None}
            if s:
                g, bd = json.loads(s["growth_detail"] or "{}"), json.loads(s["buy_detail"] or "{}")
                item.update({
                    "growth": s["growth_score"], "growth_coverage": g.get("coverage"),
                    "growth_kind": "正式" if (g.get("coverage") or 0) >= 0.8 else "暫定（参考）",
                    "growth_2029": (g.get("factors", {}).get("growth_2029") or {}).get("score"),
                    "buy": s["buy_score"], "buy_kind": _buy_kind({**bd, "buy_score": s["buy_score"]}),
                    "v_over_p": bd.get("v_over_p"), "buy_reason": bd.get("reason"), "sell": s["sell_score"]})
            else:
                item["missing"] = "この日の採点なし"
            rows.append(item)
        sectors: dict[str, int] = {}
        for r in rows:
            sectors[r["sector"] or "不明"] = sectors.get(r["sector"] or "不明", 0) + 1
        out["lists"][ln] = {"rows": rows, "sectors": dict(sorted(sectors.items(), key=lambda x: -x[1]))}
    return out


def track_markdown(conn: sqlite3.Connection, lists: list[str], as_of: date) -> str:
    lines: list[str] = []
    for ln in lists:
        lines += ["", f"## {ln} の成績（入れた日から、TOPIX との差）", "",
                  "| 枠 | コード | 入れた日 | 外した日 | 1か月 | 3か月 | 6か月 |", "|---|---|---|---|---|---|---|"]
        for t in track(conn, ln, as_of):
            cells = ["期間未経過" if t[k] is None else
                     (f"{t[k]['excess'] * 100:+.1f}%" if t[k]["excess"] is not None else "TOPIX取得不可") for k in HORIZONS]
            lines.append(f"| {TIERS[t['tier']]} | {t['code']} | {t['added_at']} | {t['removed_at'] or '-'} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _avg(xs: list[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return round(sum(v) / len(v), 1) if v else None


def to_markdown(rep: dict[str, Any]) -> str:
    def pct(x: float | None) -> str:
        return "取得不可" if x is None else f"{x * 100:.0f}%"
    lines = [f"# 監視枠の比較（評価日 {rep['date']}、同じ計算方式・同じデータ条件）", "",
             "- 成長期待点は充足率80%未満なら「暫定（参考）」。買い点数は 正式／暫定（承認前）／参考（仮のEPS・PER）／未算定 を分ける。",
             "- 有利子負債が未取得の銘柄は財務健全と断定しない（「取得不可」）。", ""]
    for ln, block in rep["lists"].items():
        rows = block["rows"]
        lines += [f"## {ln}（{len(rows)}銘柄）", "",
                  f"- 平均：成長期待 {_avg([r.get('growth') for r in rows])} / 買い {_avg([r.get('buy') for r in rows])}"
                  f" / 売り {_avg([r.get('sell') for r in rows])}",
                  "- 業種：" + "、".join(f"{k} {v}" for k, v in block["sectors"].items()), "",
                  "| コード | 銘柄 | 成長期待 | 買い | 売り | 信頼度 | 2029成長 | 自己資本比率 | 営業利益率 | 有利子負債 | 重大リスク |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            f = r["fin"]
            lines.append(
                f"| {r['code']} | {r['name'] or ''} | {r.get('growth', '未算定')}（{r.get('growth_kind', '-')}） "
                f"| {r.get('buy') if r.get('buy') is not None else '未算定'}（{r.get('buy_kind', '-')}） | {r.get('sell', '未算定')} "
                f"| {r['confidence'] or '未算定'} | {r.get('growth_2029') if r.get('growth_2029') is not None else '未算定'} "
                f"| {pct(f.get('equity_ratio'))} | {pct(f.get('op_margin'))} "
                f"| {'取得不可' if f.get('debt') is None else f'{f['debt'] / 1e8:.0f}億円'} "
                f"| {'、'.join(r['severe_risks']) if r['severe_risks'] else ('なし' if r['severe_risks'] == [] else '未算定')} |")
        lines.append("")
    return "\n".join(lines)
