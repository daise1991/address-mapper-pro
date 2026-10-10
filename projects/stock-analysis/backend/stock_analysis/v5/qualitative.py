"""市場拡大・競争優位（定性2項目）の承認済み評価ルールと検査（入力形式 v2）。

仕様: docs/qualitative-v2-spec.md
調査結果の「構成要素」からアプリ側で点数を再計算する（調査担当の合計値をそのまま使わない）。
市場拡大: 各セグメントの市場年平均成長率→基礎点（<0%:20, 0-3%:40, 3-7%:60, 7-12%:75, 12-20%:88, 20%以上:95）
          を売上構成比で加重平均し、調整（±5、根拠付きのときだけ）を加える
競争優位: シェア(30/22/12/5)+同業比利益率(25/15/5)+技術(20/12/5)+受注・顧客(15/9/4)+中計の実績(10/6/2)

点数は2種類に分ける。
  参考点数: 検証前でも計算する（構成比のずれは正規化、規則外の点数は最も近い値に寄せて警告）
  正式点数: 検証済み（company_verified / market_verified）かつ計算検査（calculation_verified）に合格した項目だけ。
            満たさない項目は None（未評価）。0点にはしない。
検査の「根拠」エラーは検証済みを取り消し、「計算」エラーは calculation_verified を取り消す。どちらも正式点数を未評価にする。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from statistics import median
from typing import Any

from ..db import now_iso

CAGR_BANDS = [(0.0, 20), (0.03, 40), (0.07, 60), (0.12, 75), (0.20, 88)]
ALLOWED = {"share": {30, 22, 12, 5}, "margin_vs_peers": {25, 15, 5}, "technology": {20, 12, 5},
           "orders_customers": {15, 9, 4}, "midterm_plan": {10, 6, 2}}
LABEL = {"share": "シェア", "margin_vs_peers": "同業比利益率", "technology": "技術",
         "orders_customers": "受注・顧客", "midterm_plan": "中計の実績"}
MARGIN_TYPES = {"operating", "net", "ebit"}
SHARE_SUM_RANGE = (0.95, 1.05)   # 事業別売上構成比の合計の許容範囲
SOURCE_MAX_AGE_YEARS = 3         # 市場成長率の出典の発表日の鮮度
MIN_CAGR_YEARS = 3               # 予測期間の最短年数
HORIZON_YEAR = 2029              # 評価の対象年（予測期間はこの年以前に始まること）
PEER_MAX_GAP_MONTHS = 12         # 同業の決算期末のずれの上限
MIN_PEERS = 2
MARGIN_BAND_PT = 5.0             # 同業中央値との差（pt）: +5超→25、±5以内→15、-5未満→5
ADJ_LIMIT = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS qualitative_v5 (
    code TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL,      -- 取込ファイルの1銘柄分（そのまま）
    source TEXT NOT NULL,       -- 取込ファイル名
    imported_at TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    from .. import research
    research.ensure_schema(conn)
    conn.executescript(SCHEMA)


def cagr_points(cagr: float) -> int:
    for upper, pts in CAGR_BANDS:
        if cagr < upper:
            return pts
    return 95


def margin_points(company: float, peers: list[float]) -> tuple[int, float]:
    diff = (company - median(peers)) * 100
    pts = 25 if diff > MARGIN_BAND_PT else (15 if diff >= -MARGIN_BAND_PT else 5)
    return pts, round(diff, 2)


def _parse_date(s: Any) -> date | None:
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", str(s or "").strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3] or 1))
    except ValueError:
        return None


def _months(s: Any) -> int | None:
    d = _parse_date(s)
    return d.year * 12 + d.month if d else None


def _norm_market(s: str) -> str:
    return re.sub(r"[\s　]", "", s or "").lower()


def _source_ok(src: Any) -> bool:
    return isinstance(src, dict) and bool(src.get("url")) and _parse_date(src.get("published")) is not None


class _Log:
    def __init__(self) -> None:
        self.evidence: list[str] = []   # 根拠の不足（検証済みを取り消す）
        self.calc: list[str] = []       # 計算の不一致・規則違反（calculation_verified を取り消す）
        self.warnings: list[str] = []   # 表示のみ


def _market(me: dict[str, Any], as_of: date, log: _Log) -> int | None:
    segs = [s for s in me.get("segments") or [] if s.get("cagr") is not None and s.get("sales_share")]
    if not segs:
        log.evidence.append("市場拡大: 市場成長率の根拠なし（未評価）")
        return None
    total = sum(s["sales_share"] for s in segs)
    if not SHARE_SUM_RANGE[0] <= total <= SHARE_SUM_RANGE[1]:
        log.calc.append(f"市場拡大: 売上構成比の合計 {total:.3f} が {SHARE_SUM_RANGE[0]}〜{SHARE_SUM_RANGE[1]} の範囲外（参考点数は正規化して計算）")
    seen: dict[str, str] = {}
    for s in segs:
        name = s.get("segment") or "?"
        pts = cagr_points(s["cagr"])
        if s.get("base_points") != pts:
            log.calc.append(f"市場拡大: {name} の基礎点 {s.get('base_points')} は規則では {pts}")
        src = s.get("source") if isinstance(s.get("source"), dict) else {}
        missing = [k for k, v in (("market", s.get("market")), ("cagr_period", s.get("cagr_period")), ("fit", s.get("fit")),
                                  ("source.publisher", src.get("publisher")), ("source.url", src.get("url")),
                                  ("source.published", src.get("published"))) if not v]
        if missing:
            log.evidence.append(f"市場拡大: {name} の必須項目なし（{', '.join(missing)}）")
        pub = _parse_date(src.get("published"))
        if src.get("published") and pub is None:
            log.evidence.append(f"市場拡大: {name} の発表日 {src.get('published')!r} が日付でない")
        if pub and (as_of - pub).days > SOURCE_MAX_AGE_YEARS * 366:
            log.evidence.append(f"市場拡大: {name} の出典の発表日 {src.get('published')} が{SOURCE_MAX_AGE_YEARS}年より古い")
        per = re.fullmatch(r"(\d{4})\s*[-–~〜]\s*(\d{4})", str(s.get("cagr_period") or "").strip())
        if s.get("cagr_period") and not per:
            log.evidence.append(f"市場拡大: {name} の予測期間 {s.get('cagr_period')!r} が YYYY-YYYY でない")
        elif per:
            y0, y1 = int(per[1]), int(per[2])
            if y1 - y0 < MIN_CAGR_YEARS or y1 < as_of.year or y0 > HORIZON_YEAR:
                log.evidence.append(f"市場拡大: {name} の予測期間 {y0}-{y1} が評価期間に合わない"
                                    f"（{MIN_CAGR_YEARS}年以上・終わりが{as_of.year}年以降・始まりが{HORIZON_YEAR}年以前）")
        key = _norm_market(s.get("market") or "")
        if key and key in seen:
            log.calc.append(f"市場拡大: 市場「{s.get('market')}」が {seen[key]} と {name} で重複")
        seen.setdefault(key, name)
    adj = me.get("adjustment") or 0
    if not isinstance(adj, int) or abs(adj) > ADJ_LIMIT:
        log.calc.append(f"市場拡大: 調整 {adj} は -{ADJ_LIMIT}〜+{ADJ_LIMIT} の整数でない（参考点数は ±{ADJ_LIMIT} に丸める）")
    if adj:
        b = me.get("adjustment_basis") if isinstance(me.get("adjustment_basis"), dict) else {}
        if not (b.get("reason") and b.get("url")):
            log.evidence.append(f"市場拡大: 調整 {adj:+} の根拠（理由とURL）なし")
    base = sum(s["sales_share"] * cagr_points(s["cagr"]) for s in segs) / total
    score = max(0, min(100, round(base + max(-ADJ_LIMIT, min(ADJ_LIMIT, adj)))))
    if me.get("score") is None:
        log.calc.append("市場拡大: 調査の合計点の記載なし（再計算と照合できない）")
    elif abs(me["score"] - score) > 1:
        log.calc.append(f"市場拡大: 調査の合計 {me['score']} → 規則で再計算 {score}")
    return score


def _margin(c: dict[str, Any], log: _Log) -> None:
    mt = c.get("margin_type")
    if mt not in MARGIN_TYPES:
        log.evidence.append(f"競争優位: 同業比利益率の種類 {mt!r} が未記載または不明（{sorted(MARGIN_TYPES)}）")
    co = c.get("company") if isinstance(c.get("company"), dict) else {}
    peers = [p for p in c.get("peers") or [] if isinstance(p, dict)]
    usable = [p for p in peers if isinstance(p.get("margin"), (int, float))]
    if len(usable) < MIN_PEERS:
        log.evidence.append(f"競争優位: 利益率の比較相手が {len(usable)} 社（{MIN_PEERS}社以上必要）")
    if not isinstance(co.get("margin"), (int, float)):
        log.evidence.append("競争優位: 自社の利益率の記載なし")
        return
    co_m = _months(co.get("fiscal_period_end"))
    if co_m is None:
        log.evidence.append("競争優位: 自社の利益率の対象年度（決算期末）なし")
    if not co.get("accounting_standard"):
        log.evidence.append("競争優位: 自社の会計基準なし")
    if not _source_ok(co.get("source")):
        log.evidence.append("競争優位: 自社の利益率の出典（URL・発表日）なし")
    for p in usable:
        n = p.get("name") or "?"
        if p.get("margin_type") and p["margin_type"] != mt:
            log.calc.append(f"競争優位: {n} の利益率の種類 {p['margin_type']} が自社（{mt}）と異なる")
        pm = _months(p.get("fiscal_period_end"))
        if pm is None:
            log.evidence.append(f"競争優位: {n} の対象年度（決算期末）なし")
        elif co_m is not None and abs(pm - co_m) > PEER_MAX_GAP_MONTHS:
            log.calc.append(f"競争優位: {n} の決算期末 {p['fiscal_period_end']} が自社と{PEER_MAX_GAP_MONTHS}か月超ずれている")
        if not p.get("accounting_standard"):
            log.evidence.append(f"競争優位: {n} の会計基準なし")
        elif co.get("accounting_standard") and p["accounting_standard"] != co["accounting_standard"] and not c.get("standard_note"):
            log.evidence.append(f"競争優位: {n} の会計基準 {p['accounting_standard']} が自社（{co['accounting_standard']}）と異なるのに説明（standard_note）なし")
        if not _source_ok(p.get("source")):
            log.evidence.append(f"競争優位: {n} の利益率の出典（URL・発表日）なし")
    if len(usable) >= MIN_PEERS:
        pts, diff = margin_points(co["margin"], [p["margin"] for p in usable])
        if c.get("points") != pts:
            log.calc.append(f"競争優位: 同業比利益率は中央値との差 {diff:+.1f}pt で規則では {pts} 点（調査 {c.get('points')}）")


def _competitive(ca_all: dict[str, Any], company_declared: bool, log: _Log) -> int | None:
    ca = ca_all.get("components") or {}
    missing = [LABEL[k] for k in ALLOWED if not isinstance(ca.get(k), dict) or ca[k].get("points") is None]
    if missing:
        log.evidence.append(f"競争優位: 構成要素が不足（{', '.join(missing)}、未評価）")
    pts_total = 0
    for k, allowed in ALLOWED.items():
        c = ca.get(k) if isinstance(ca.get(k), dict) else {}
        p = c.get("points")
        if p is not None and p not in allowed:
            # 正式点数では補正しない（計算エラー）。参考点数だけ最も近い許可値に寄せる
            log.calc.append(f"競争優位: {LABEL[k]} の点数 {p} は規則外（{sorted(allowed)}）")
            log.warnings.append(f"競争優位: {LABEL[k]} の参考点数は {min(allowed, key=lambda a: abs(a - p))} として計算")
            p = min(allowed, key=lambda a: abs(a - p))
        if p is not None:
            pts_total += p
        srcs = [s for s in c.get("sources") or [] if isinstance(s, dict)]
        if not any(_source_ok(s) for s in srcs):
            log.evidence.append(f"競争優位: {LABEL[k]} の出典（URL・発表日）なし")
        if company_declared and k != "share" and not any(s.get("type") == "primary" and _source_ok(s) for s in srcs):
            log.evidence.append(f"競争優位: {LABEL[k]} に一次情報の出典なし（検証済みの条件を満たさない）")
    if isinstance(ca.get("share"), dict) and not ca["share"].get("as_of"):
        log.evidence.append("競争優位: シェアの時点（as_of）なし")
    mp = ca.get("midterm_plan") if isinstance(ca.get("midterm_plan"), dict) else {}
    lack = [k for k in ("plan_name", "target_fiscal_year", "actual_fiscal_year") if not mp.get(k)]
    if mp and lack:
        log.evidence.append(f"競争優位: 中計の {', '.join(lack)} なし")
    if isinstance(ca.get("margin_vs_peers"), dict):
        _margin(ca["margin_vs_peers"], log)
    if missing:
        return None
    stated = ca_all.get("score")
    if stated is None:
        log.calc.append("競争優位: 調査の合計点の記載なし（再計算と照合できない）")
    elif stated != pts_total:
        log.calc.append(f"競争優位: 調査の合計 {stated} → 規則で再計算 {pts_total}")
    return pts_total


def _evaluate_v1(item: dict[str, Any]) -> dict[str, Any]:
    """旧形式（v1）。参考点数だけ計算し、正式点数は未評価とする。"""
    out = _blank()
    me = item.get("market_expansion") or {}
    segs = [s for s in me.get("segments") or [] if s.get("cagr") is not None and s.get("sales_share")]
    if segs:
        total = sum(s["sales_share"] for s in segs)
        base = sum(s["sales_share"] * cagr_points(s["cagr"]) for s in segs) / total
        out["reference"]["market_expansion"] = max(0, min(100, round(base + max(-5, min(5, me.get("adjustment") or 0)))))
    ca = (item.get("competitive_advantage") or {}).get("components") or {}
    if all(isinstance(ca.get(k), dict) and ca[k].get("points") is not None for k in ALLOWED):
        out["reference"]["competitive_advantage"] = sum(
            min(a, key=lambda x: abs(x - ca[k]["points"])) for k, a in ALLOWED.items())
    reason = "旧形式（v1）の調査ファイルのため正式点数の対象外"
    out["unevaluated"] = {"market_expansion": reason, "competitive_advantage": reason}
    out["warnings"].append(reason)
    return out


def _blank() -> dict[str, Any]:
    return {"schema_version": 1,
            "reference": {"market_expansion": None, "competitive_advantage": None},
            "formal": {"market_expansion": None, "competitive_advantage": None},
            "verification": {"company_verified": False, "market_verified": False,
                             "calculation_verified": {"market_expansion": False, "competitive_advantage": False},
                             "declared": {"company_verified": False, "market_verified": False}},
            "errors": {"market_expansion": [], "competitive_advantage": []},
            "warnings": [], "unevaluated": {}}


def evaluate(item: dict[str, Any], as_of: date | None = None) -> dict[str, Any]:
    """1銘柄を検査し、参考点数・正式点数・検証フラグ・エラーを返す。"""
    as_of = as_of or date.today()
    if item.get("schema_version") != 2:
        out = _evaluate_v1(item)
    else:
        out = _blank()
        out["schema_version"] = 2
        ver = item.get("verification") or {}
        declared = {"company_verified": ver.get("company_verified") is True, "market_verified": ver.get("market_verified") is True}
        out["verification"]["declared"] = declared
        lm, lc = _Log(), _Log()
        me_ref = _market(item.get("market_expansion") or {}, as_of, lm)
        ca_ref = _competitive(item.get("competitive_advantage") or {}, declared["company_verified"], lc)
        out["reference"] = {"market_expansion": me_ref, "competitive_advantage": ca_ref}
        v = out["verification"]
        v["market_verified"] = declared["market_verified"] and not lm.evidence and me_ref is not None
        v["company_verified"] = declared["company_verified"] and not lc.evidence and ca_ref is not None
        v["calculation_verified"] = {"market_expansion": me_ref is not None and not lm.calc,
                                     "competitive_advantage": ca_ref is not None and not lc.calc}
        out["errors"] = {"market_expansion": lm.evidence + lm.calc, "competitive_advantage": lc.evidence + lc.calc}
        out["warnings"] = lm.warnings + lc.warnings
        for key, flag, ref, decl, label in (
                ("market_expansion", "market_verified", me_ref, declared["market_verified"], "市場の検証"),
                ("competitive_advantage", "company_verified", ca_ref, declared["company_verified"], "会社情報の検証")):
            if v[flag] and v["calculation_verified"][key]:
                out["formal"][key] = ref
            elif ref is None:
                out["unevaluated"][key] = "構成要素・根拠が不足（参考点数も未算定）"
            elif not decl:
                out["unevaluated"][key] = f"{label}が未了"
            else:
                out["unevaluated"][key] = "検査エラーあり（" + "／".join(out["errors"][key][:3]) + "）"
    # 互換: 参考点数と全指摘
    out["market_expansion"] = out["reference"]["market_expansion"]
    out["competitive_advantage"] = out["reference"]["competitive_advantage"]
    out["issues"] = out["errors"]["market_expansion"] + out["errors"]["competitive_advantage"] + out["warnings"]
    return out


def import_items(conn: sqlite3.Connection, items: list[dict[str, Any]], source: str,
                 as_of: date | None = None) -> list[dict[str, Any]]:
    """定性評価を v5 専用の表に保存する（v4 と調査前提には触れない）。点数は採点のたびに再検査する。"""
    ensure_schema(conn)
    results = []
    for it in items:
        ev = evaluate(it, as_of)
        conn.execute("""INSERT INTO qualitative_v5 (code, schema_version, payload, source, imported_at) VALUES (?,?,?,?,?)
                        ON CONFLICT(code) DO UPDATE SET schema_version=excluded.schema_version, payload=excluded.payload,
                        source=excluded.source, imported_at=excluded.imported_at""",
                     (it["code"], ev["schema_version"], json.dumps(it, ensure_ascii=False), source, now_iso()))
        conn.execute("INSERT INTO assumption_research_history (code, event, payload, created_at) VALUES (?,?,?,?)",
                     (it["code"], f"qualitative_v5:{source}",
                      json.dumps({"raw": it, "evaluation": ev}, ensure_ascii=False, default=str), now_iso()))
        results.append({"code": it["code"], **ev, "saved": True})
    conn.commit()
    return results


def scores_for(conn: sqlite3.Connection, code: str, as_of: date | None = None) -> dict[str, Any]:
    """v5 専用の定性評価 → 無ければ旧形式（調査前提に保存したもの）→ 手入力（qualitative_inputs）。
    戻り値の market_expansion / competitive_advantage は参考点数、formal が正式点数（未評価は None）。"""
    ensure_schema(conn)
    out: dict[str, Any] = {"market_expansion": None, "competitive_advantage": None, "growth_outlook": None,
                           "formal": {"market_expansion": None, "competitive_advantage": None},
                           "verification": None, "unevaluated": {}, "errors": {}, "origin": None}
    r = conn.execute("SELECT payload FROM qualitative_v5 WHERE code=?", (code,)).fetchone()
    raw = json.loads(r["payload"]) if r else None
    origin = "qualitative_v5" if r else None
    if raw is None:
        old = conn.execute("SELECT qualitative FROM assumption_research WHERE code=?", (code,)).fetchone()
        if old and old["qualitative"]:
            raw, origin = json.loads(old["qualitative"]).get("raw"), "assumption_research(v1)"
    if raw:
        ev = evaluate(raw, as_of)
        out.update({"market_expansion": ev["reference"]["market_expansion"],
                    "competitive_advantage": ev["reference"]["competitive_advantage"],
                    "formal": ev["formal"], "verification": ev["verification"],
                    "unevaluated": ev["unevaluated"], "errors": ev["errors"], "origin": origin})
    for row in conn.execute("SELECT factor, score FROM qualitative_inputs WHERE code=?", (code,)):
        if out.get(row["factor"]) is None:
            out[row["factor"]] = row["score"]  # 手入力は根拠の検証がないため参考点数だけ
            if row["factor"] in out["formal"]:
                out["unevaluated"][row["factor"]] = "手入力（根拠の検証なし）のため参考のみ"
    for k in ("market_expansion", "competitive_advantage"):
        if out[k] is None and k not in out["unevaluated"]:
            out["unevaluated"][k] = "定性調査なし"
    return out
