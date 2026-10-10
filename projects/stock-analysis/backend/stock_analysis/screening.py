"""新興株の定期選別（月1回）。

グロース市場の全銘柄から条件に合う銘柄を「追加候補」、今の新興株のうち条件を外れた銘柄を「外す候補」として
報告書（Markdown）にまとめる。銘柄一覧（securities）・採点（v4/v5）は変更しない。追加・削除は経営者の承認後に行う。
条件は settings 表の emerging_screen で上書きできる。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from .config import get_settings
from .db import get_setting, set_setting
from .providers.base import DataUnavailable

log = logging.getLogger(__name__)

DEFAULT_CRITERIA: dict[str, Any] = {
    "market_keyword": "グロース",
    "min_revenue_growth": 0.20,      # 直近の本決算の売上 前年比
    "min_market_cap": 10_000_000_000,  # 時価総額 100億円
    "min_turnover": 100_000_000,     # 1日の売買代金（直近の平均）1億円
    "turnover_days": 20,             # 売買代金の平均を取る営業日数
    "min_listed_days": 365,          # 上場からの日数（この日数前に株価があること）
    "max_dilution": 0.10,            # 発行済株数（自己株除く）の前年比増加
}


def criteria(conn: sqlite3.Connection) -> dict[str, Any]:
    return {**DEFAULT_CRITERIA, **(get_setting(conn, "emerging_screen", {}) or {})}


def _quotes_by_dates(provider: Any, d: date, n: int, max_back: int = 45) -> list[dict[str, dict[str, Any]]]:
    """d から遡って、株価のある日を n 日分集める（休日は空なので飛ばす）。"""
    out: list[dict[str, dict[str, Any]]] = []
    day = d
    while len(out) < n and (d - day).days <= max_back:
        q = provider.daily_quotes_by_date(day)
        if q:
            out.append(q)
        day -= timedelta(days=1)
    return out


def _shares(r: dict[str, Any]) -> float | None:
    s = r.get("shares_outstanding")
    return s - (r.get("treasury_shares") or 0) if s else None


def _fy_pair(fins: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """直近とその1年前の本決算（同じ決算期末が複数あれば最新の開示）。"""
    fy: dict[str, dict[str, Any]] = {}
    for r in sorted((r for r in fins if r.get("period_type") == "FY"), key=lambda r: r["disclosed_at"]):
        fy[r["fiscal_period_end"]] = r
    ends = sorted(fy)
    if not ends:
        return None, None
    last = fy[ends[-1]]
    prev = None
    for e in reversed(ends[:-1]):
        gap = (date.fromisoformat(ends[-1]) - date.fromisoformat(e)).days
        if 330 <= gap <= 400:
            prev = fy[e]
            break
    return last, prev


def _split_factor(provider: Any, code: str, prev_end: str, last_end: str) -> float | None:
    """前年の期末の翌日から直近の期末までの分割倍率（調整係数 0.5 → 2倍）。取得できなければ None。"""
    try:
        bars = provider.daily_quotes(code, date.fromisoformat(prev_end) + timedelta(days=1), date.fromisoformat(last_end))
    except DataUnavailable:
        return None
    mult = 1.0
    for b in bars:
        f = b.get("adjustment_factor") or 1.0
        if f and f != 1.0:
            mult /= f
    return round(mult, 4)


def evaluate(fins: list[dict[str, Any]], close: float | None, turnover: float | None, listed_long: bool,
             c: dict[str, Any], split: float | None = 1.0) -> dict[str, Any]:
    """1銘柄の指標と、条件を満たさない理由（取得できない値は「取得不可」として理由に入れる）。

    split: 前年の本決算期末から直近の期末までの株式分割の倍率（株価の調整係数から算出。None は確認不可）。
    """
    last, prev = _fy_pair(fins)
    growth = None
    if last and prev and last.get("revenue") is not None and (prev.get("revenue") or 0) > 0:
        growth = last["revenue"] / prev["revenue"] - 1
    sh_now = _shares(last) if last else None
    sh_prev = _shares(prev) if prev else None
    raw = sh_now / sh_prev - 1 if sh_now and sh_prev else None
    dilution = sh_now / (sh_prev * split) - 1 if sh_now and sh_prev and split else None
    mcap = close * sh_now if close and sh_now else None
    op = last.get("operating_profit") if last else None
    fails: list[str] = []
    if growth is None:
        fails.append("売上の伸び 取得不可")
    elif growth < c["min_revenue_growth"]:
        fails.append(f"売上の伸び {growth:+.0%}")
    if mcap is None:
        fails.append("時価総額 取得不可")
    elif mcap < c["min_market_cap"]:
        fails.append(f"時価総額 {mcap / 1e8:,.0f}億円")
    if turnover is None:
        fails.append("売買代金 取得不可")
    elif turnover < c["min_turnover"]:
        fails.append(f"売買代金 {turnover / 1e8:.2f}億円/日")
    if not listed_long:
        fails.append(f"上場{c['min_listed_days']}日未満")
    notes: list[str] = []
    if split is None and raw is not None and raw > c["max_dilution"]:
        fails.append(f"株数 {raw:+.0%}（株式分割の確認不可）")
    elif dilution is not None and dilution > c["max_dilution"]:
        fails.append(f"株数 {dilution:+.0%}" + (f"（分割 {split:g}倍を補正後）" if split and split != 1 else ""))
    if split and split != 1:
        notes.append(f"株式分割 {split:g}倍を補正（補正前の株数増加 {raw:+.0%}、補正後 {dilution:+.0%}）")
    return {"revenue_growth": growth, "market_cap": mcap, "turnover": turnover, "dilution": dilution,
            "operating_profit": op, "fiscal_period_end": last["fiscal_period_end"] if last else None,
            "prev_period_end": prev["fiscal_period_end"] if prev else None, "raw_dilution": raw,
            "fails": fails, "notes": notes, "passed": not fails}


def screen(conn: sqlite3.Connection, provider: Any, d: date) -> dict[str, Any]:
    c = criteria(conn)
    universe = [r for r in provider.listed_all() if c["market_keyword"] in str(r.get("market") or "")]
    days = _quotes_by_dates(provider, d, c["turnover_days"])
    if not days:
        raise DataUnavailable(f"{d} 以前 45日の株価を取得できない")
    old = _quotes_by_dates(provider, d - timedelta(days=c["min_listed_days"]), 1)
    old_codes = set(old[0]) if old else set()
    cur = {r["code"]: r for r in conn.execute(
        "SELECT code, name, group_name, active FROM securities WHERE group_name='emerging'").fetchall()}
    tracked = {r[0] for r in conn.execute("SELECT code FROM securities WHERE active=1")}
    names = {r["code"]: r for r in universe}
    targets = sorted(set(names) | set(cur))
    items, errors = [], []
    for code in targets:
        closes = [q[code]["close"] for q in days if code in q and q[code].get("close")]
        tv = [q[code]["close"] * q[code]["volume"] for q in days
              if code in q and q[code].get("close") and q[code].get("volume") is not None]
        try:
            fins = provider.financial_statements(code)
        except DataUnavailable as e:
            errors.append(f"{code}: 決算 取得不可（{e}）")
            fins = []
        args = (fins, closes[0] if closes else None, sum(tv) / len(tv) if tv else None, code in old_codes, c)
        ev = evaluate(*args)
        if ev["raw_dilution"] is not None and ev["raw_dilution"] > c["max_dilution"]:
            ev = evaluate(*args, split=_split_factor(provider, code, ev["prev_period_end"], ev["fiscal_period_end"]))
        info = names.get(code) or {}
        items.append({"code": code, "name": info.get("name") or (cur[code]["name"] if code in cur else ""),
                      "market": info.get("market"), "sector": info.get("sector"),
                      "current": code in cur, "tracked": code in tracked, **ev})
    add = [i for i in items if i["passed"] and not i["tracked"]]
    drop = [i for i in items if i["current"] and not i["passed"]]
    add.sort(key=lambda i: -(i["revenue_growth"] or 0))
    return {"date": d.isoformat(), "criteria": c, "universe": len(universe), "quote_days": len(days),
            "passed": sum(i["passed"] for i in items), "add": add, "drop": drop, "items": items, "errors": errors}


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:+.0%}"


def _oku(x: float | None, nd: int = 0) -> str:
    return "—" if x is None else f"{x / 1e8:,.{nd}f}"


def to_markdown(rep: dict[str, Any]) -> str:
    c = rep["criteria"]
    out = [f"# 新興株の定期選別（{rep['date']}）", "",
           "追加・削除はまだしていません。経営者の承認後に銘柄一覧へ反映します（v4/v5 の採点は変わりません）。", "",
           f"- 対象: {c['market_keyword']}市場 {rep['universe']}社（売買代金は直近 {rep['quote_days']}営業日の平均）",
           f"- 条件: 売上の伸び {c['min_revenue_growth']:.0%}以上／時価総額 {c['min_market_cap'] / 1e8:,.0f}億円以上"
           f"／売買代金 {c['min_turnover'] / 1e8:,.1f}億円/日以上／上場 {c['min_listed_days']}日以上／株数の増加 {c['max_dilution']:.0%}以下",
           f"- 条件を満たした銘柄: {rep['passed']}社（うち未登録の追加候補 {len(rep['add'])}社）／外す候補 {len(rep['drop'])}社", "",
           "## 追加候補（売上の伸びが大きい順）", "",
           "| 銘柄 | 業種 | 売上の伸び | 時価総額(億円) | 売買代金(億円/日) | 株数の増加 | 営業利益(億円) | 決算期末 |",
           "|---|---|---|---|---|---|---|---|"]
    for i in rep["add"]:
        out.append(f"| {i['code']} {i['name']} | {i['sector'] or '—'} | {_pct(i['revenue_growth'])} | {_oku(i['market_cap'])}"
                   f" | {_oku(i['turnover'], 2)} | {_pct(i['dilution'])} | {_oku(i['operating_profit'], 1)} | {i['fiscal_period_end'] or '—'} |")
    if not rep["add"]:
        out.append("| なし | | | | | | | |")
    out += ["", "## 外す候補（今の新興株で条件を外れた銘柄）", "", "| 銘柄 | 外れた条件 |", "|---|---|"]
    for i in rep["drop"]:
        out.append(f"| {i['code']} {i['name']} | {'／'.join(i['fails'])} |")
    if not rep["drop"]:
        out.append("| なし | |")
    noted = [i for i in rep["items"] if i["notes"]]
    if noted:
        out += ["", "## 要確認", ""] + [f"- {i['code']} {i['name']}: {'／'.join(i['notes'])}" for i in noted]
    if rep["errors"]:
        out += ["", "## 取得できなかったデータ", ""] + [f"- {e}" for e in rep["errors"]]
    return "\n".join(out) + "\n"


def run_and_save(conn: sqlite3.Connection, provider: Any, d: date, out: Any = None) -> tuple[dict[str, Any], Any]:
    from pathlib import Path
    rep = screen(conn, provider, d)
    path = Path(out) if out else get_settings().db_path.parent / f"emerging_screen_{d}.md"
    path.write_text(to_markdown(rep), encoding="utf-8")
    return rep, path


def run_monthly(conn: sqlite3.Connection, provider: Any, now: datetime) -> str | None:
    """月に1回（その月の初回、9時以降）選別して報告書を保存し、通知する。失敗したら1時間後に再試行。"""
    if not hasattr(provider, "listed_all"):  # デモなど全銘柄一覧が無い取得元では行わない
        return None
    month = now.strftime("%Y-%m")
    st = get_setting(conn, "emerging_screen_state", {}) or {}
    if st.get("done") == month or now.hour < 9:
        return None
    if st.get("tried_at") and (now - datetime.fromisoformat(st["tried_at"])).total_seconds() < 3600:
        return None
    set_setting(conn, "emerging_screen_state", {**st, "tried_at": now.isoformat()})
    conn.commit()
    try:
        rep, path = run_and_save(conn, provider, now.date())
    except Exception as e:  # noqa: BLE001 — 次の機会に再試行する
        log.exception("新興株の定期選別に失敗")
        set_setting(conn, "emerging_screen_state", {**st, "tried_at": now.isoformat(), "error": str(e)[:300]})
        conn.commit()
        return f"失敗: {e}"
    set_setting(conn, "emerging_screen_state", {"done": month, "tried_at": now.isoformat(), "report": str(path)})
    conn.commit()
    from .notify import notify
    notify(conn, "screen", "新興株の定期選別",
           f"追加候補 {len(rep['add'])}社・外す候補 {len(rep['drop'])}社。報告書: {path.name}（追加・削除は承認後）")
    return str(path)


__all__ = ["DEFAULT_CRITERIA", "criteria", "evaluate", "run_and_save", "run_monthly", "screen", "to_markdown"]
