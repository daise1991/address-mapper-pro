"""J-Quants / EDINET の接続チェック（APIキー設定後に最初に実行する）。

  python -m stock_analysis check

- 各エンドポイントに1回ずつ問い合わせ、件数・項目名・想定した項目名が揃っているかを表示する
- 最新の日足日付から、当日終値が取れるプランか（無料プランの12週間遅延でないか）を判定する
- 60銘柄が上場銘柄一覧に存在するかを確認する
データは保存しない（読み取りのみ）。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from .config import JST, get_settings
from .market_calendar import previous_business_day
from .providers.base import DataUnavailable
from .universe import SEED, to_jquants_code

# 実装が読みにいく項目名（V2短縮名, V1名）。どちらかがあれば OK
EXPECTED = {
    "daily": [("Date",), ("C", "Close"), ("Vo", "Volume"), ("AdjC", "AdjustmentClose"), ("AdjFactor", "AdjustmentFactor")],
    "summary": [("DiscDate", "DisclosedDate"), ("CurPerType", "TypeOfCurrentPeriod"), ("DocType", "TypeOfDocument"),
                ("Sales", "NetSales"), ("OP", "OperatingProfit"), ("NP", "Profit"), ("EPS", "EarningsPerShare"),
                ("Eq", "Equity"), ("TA", "TotalAssets"), ("CurFYEn", "CurrentFiscalYearEndDate")],
    "master": [("Code",), ("CoName", "CompanyName")],
    "calendar": [("Date",), ("HolDiv", "HolidayDivision")],
    "topix": [("Date",), ("C", "Close")],
}


def _keys_report(rows: list[dict[str, Any]], expected: list[tuple[str, ...]]) -> dict[str, Any]:
    keys = set().union(*(r.keys() for r in rows[:50])) if rows else set()
    missing = [" / ".join(names) for names in expected if not any(n in keys for n in names)]
    return {"rows": len(rows), "keys": sorted(keys), "missing": missing}


def check_jquants(provider: Any, today: date | None = None, probe_code: str = "6857") -> list[dict[str, Any]]:
    from .providers.jquants import PATHS
    today = today or date.today()
    last_bd = previous_business_day(today + timedelta(days=1))
    out: list[dict[str, Any]] = []

    def run(name: str, path: str, params: dict[str, Any], kind: str, extra: Callable[[list[dict[str, Any]]], str] | None = None) -> list[dict[str, Any]]:
        try:
            rows = provider._get(path, params)
        except DataUnavailable as e:
            out.append({"name": name, "status": "NG", "detail": str(e)})
            return []
        rep = _keys_report(rows, EXPECTED[kind])
        status = "OK" if rows and not rep["missing"] else ("要確認" if rows else "0件")
        detail = f"{rep['rows']}件"
        if rep["missing"]:
            detail += f" ／ 想定した項目が見つからない: {', '.join(rep['missing'])} ／ 実際の項目: {', '.join(rep['keys'][:40])}"
        if extra and rows:
            detail += " ／ " + extra(rows)
        out.append({"name": name, "status": status, "detail": detail})
        return rows

    run("取引カレンダー", PATHS["calendar"], {"from": (today - timedelta(days=10)).isoformat(), "to": today.isoformat()}, "calendar")
    master = run("上場銘柄一覧", PATHS["master"], {}, "master")
    daily = run(f"日足（{probe_code}・過去2週間）", PATHS["daily"],
                {"code": to_jquants_code(probe_code), "from": (today - timedelta(days=14)).isoformat(), "to": today.isoformat()}, "daily")
    if daily:
        latest = max(str(r.get("Date"))[:10].replace("-", "") for r in daily)
        latest_d = date(int(latest[:4]), int(latest[4:6]), int(latest[6:8]))
        ok = latest_d >= last_bd - timedelta(days=3)
        out.append({"name": "当日終値の取得可否", "status": "OK" if ok else "NG",
                    "detail": f"最新の日足: {latest_d}（直近営業日 {last_bd}）" + ("" if ok else " → 遅延あり。無料プランの可能性。Light 以上が必要")})
    else:
        old = run("日足（12週間前・無料プラン判定用）", PATHS["daily"],
                  {"code": to_jquants_code(probe_code), "from": (today - timedelta(days=100)).isoformat(),
                   "to": (today - timedelta(days=85)).isoformat()}, "daily")
        out.append({"name": "当日終値の取得可否", "status": "NG",
                    "detail": "直近2週間の日足が0件" + ("（12週間前は取得可＝無料プランの遅延とみられる。Light 以上が必要）" if old else "")})
    run(f"日足（日付指定・{last_bd}・全銘柄）", PATHS["daily"], {"date": last_bd.isoformat()}, "daily")
    run("TOPIX 日足", PATHS["topix"], {"from": (today - timedelta(days=14)).isoformat(), "to": today.isoformat()}, "topix")
    run(f"決算サマリー（{probe_code}）", PATHS["summary"], {"code": to_jquants_code(probe_code)}, "summary",
        lambda rows: "書類種別: " + ", ".join(sorted({str(r.get('DocType') or r.get('TypeOfDocument')) for r in rows})[:8]))

    if master:
        listed = {str(r.get("Code"))[:4] if str(r.get("Code")).endswith("0") and len(str(r.get("Code"))) == 5 else str(r.get("Code"))
                  for r in master}
        absent = [f"{c} {n}" for c, n, *_ in SEED if c not in listed]
        out.append({"name": "60銘柄の上場確認", "status": "OK" if not absent else "要確認",
                    "detail": "全銘柄が一覧に存在" if not absent else "一覧に無い: " + "、".join(absent)})
    return out


def check_edinet(client: Any, today: date | None = None) -> list[dict[str, Any]]:
    today = today or date.today()
    d = previous_business_day(today)
    try:
        docs = client.documents(d)
    except DataUnavailable as e:
        return [{"name": "EDINET 書類一覧", "status": "NG", "detail": str(e)}]
    return [{"name": "EDINET 書類一覧", "status": "OK", "detail": f"{d} の提出書類 {len(docs)}件"}]


def run_all() -> list[dict[str, Any]]:
    from datetime import datetime
    s = get_settings()
    today = datetime.now(JST).date()
    results: list[dict[str, Any]] = []
    if s.jquants_api_key:
        from .providers.jquants import JQuantsProvider
        results += check_jquants(JQuantsProvider(s), today)
    else:
        results.append({"name": "J-Quants", "status": "未設定", "detail": "backend/.env の JQUANTS_API_KEY が空です"})
    if s.edinet_api_key:
        from .providers.edinet import EdinetClient
        results += check_edinet(EdinetClient(s), today)
    else:
        results.append({"name": "EDINET", "status": "未設定", "detail": "EDINET_API_KEY が空です（任意。有利子負債・増資の取得に使用）"})
    return results


ORIGIN_LABELS = {"research": "調査前提", "manual": "登録済みの前提", "auto": "自動初期推計"}
STATUS_LABELS = {"provisional": "暫定・承認前", "review_required": "要再承認", "verified": "承認済み", "unconfirmed": "前提未確認"}


def _n(v: Any, unit: str = "") -> str:
    """未算定（None）を 0 と書かない。"""
    return "未算定" if v is None else f"{v}{unit}"


def _factors(det: dict[str, Any]) -> str:
    return "、".join(f"{x['label']}={_n(x.get('score'))}" for x in (det.get("factors") or {}).values()) or "内訳なし"


def explain(conn: Any, code: str, d: date | None = None) -> list[str]:
    """ランキングと同じ保存済みの評価結果（v4 正式・v5 参考）と、その計算根拠を文字で返す。
    v4 は採点と同じ前提（analysis.scoring_assumption）で計算し直し、保存値との一致も表示する。"""
    import json

    from .analysis import scoring_assumption
    from .assumptions import auto_baseline
    from .scoring.features import latest_forecast
    from .scoring.valuation import evaluate

    if d is None:
        r = conn.execute("SELECT MAX(run_date) FROM scores WHERE code=?", (code,)).fetchone()
        d = date.fromisoformat(r[0]) if r and r[0] else date.today()
    sec = conn.execute("SELECT name, category, asset_type FROM securities WHERE code=?", (code,)).fetchone()
    if not sec:
        return [f"{code} は登録されていません"]
    out = [f"■ {code} {sec['name']}（分類: {sec['category']} / {sec['asset_type']}）  評価日 {d}"]
    sc = conn.execute("SELECT * FROM scores WHERE code=? AND run_date=?", (code, d.isoformat())).fetchone()

    out.append("【v4 正式（ランキングと同じ保存値）】")
    origin = None
    if not sc:
        out.append("  この評価日の v4 採点はありません")
    else:
        bd = json.loads(sc["buy_detail"] or "{}")
        rated = sc["buy_status"] == "rated"
        inp = bd.get("inputs") or {}
        origin = bd.get("assumption_origin")
        out.append(f"  方式 {sc['method_version']} ／ 採点 {sc['created_at']} ／ 株価 {_n(sc['price'], '円')}（{sc['price_date'] or '株価なし'}）")
        out.append(f"  成長期待 {_n(sc['growth_score'])} ／ 買い {sc['buy_score'] if rated else '未評価（' + str(sc['buy_status']) + '）'}"
                   f" ／ 売り {_n(sc['sell_score'])} ／ 理論価値 {_n(sc['theoretical_value'], '円')} ／ 狙い目 {_n(sc['target_price'], '円')}")
        out.append(f"  使った前提: {ORIGIN_LABELS.get(origin, origin or '不明')}（{STATUS_LABELS.get(bd.get('assumption_status'), bd.get('assumption_status'))}）"
                   f" ／ {inp.get('target_year')}年EPS {_n(inp.get('eps_forecast') or inp.get('normalized_eps'), '円')}"
                   f" ／ 想定PER {_n(inp.get('assumed_per'), '倍')} ／ 必要利回り {_n(inp.get('required_return'))}"
                   f" ／ 安全余裕 {_n(inp.get('safety_margin'))} ／ 年数 {_n(bd.get('years'))}")
        out.append(f"  前提の理由: {inp.get('reason') or '-'}")
        if bd.get("data_issue"):
            out.append(f"  データの注意: {bd['data_issue']}")
        # 採点と同じ前提で計算し直し、保存値と照合する
        a, o2, _, _, _ = scoring_assumption(conn, code, d)
        v = evaluate(a, sc["price"], d)
        same = o2 == origin and v.theoretical_value == sc["theoretical_value"] and v.buy_score == sc["buy_score"]
        out.append(f"  再計算: 将来株価 {_n(v.future_price)} → 理論価値 {_n(v.theoretical_value)} V/P {_n(v.v_over_p)} 買い {_n(v.buy_score)}"
                   f" → 保存値と{'一致' if same else '不一致（前提か入力データが採点後に変わった可能性）'}")
        out.append(f"  成長期待の内訳: {_factors(json.loads(sc['growth_detail'] or '{}'))}")
        out.append(f"  売りの内訳: {_factors(json.loads(sc['sell_detail'] or '{}'))}")
        out.append(f"  採点に使った情報: {json.loads(sc['indicators'] or '{}').get('sources')}")

    v5 = conn.execute("SELECT * FROM scores_v5 WHERE code=? AND run_date=?", (code, d.isoformat())).fetchone()
    out.append("【v5 参考（正式採用前。ランキングの点数とは別）】")
    if not v5:
        out.append("  この評価日の v5 参考計算はありません")
    else:
        b5 = json.loads(v5["buy_detail"] or "{}")
        pb = b5.get("per_basis") or {}
        out.append(f"  方式 {v5['method_version']} ／ 採点 {v5['created_at']} ／ 成長 正式 {_n(v5['growth_formal'])}・参考 {_n(v5['growth_reference'])}"
                   f"（そろい具合 {_n(v5['growth_coverage'])}） ／ 買い {_n(v5['buy_score'])} ／ 理論価値 {_n(v5['theoretical_value'], '円')}"
                   f" ／ 売り 正式 {_n(v5['sell_formal'])}・参考 {_n(v5['sell_reference'])} ／ 信頼度 {_n(v5['confidence'])}")
        out.append(f"  PER: {pb.get('policy') or '-'} 中央値 {_n(pb.get('median'), '倍')}（適用 {pb.get('applied')}） ／ 乖離 {_n(b5.get('gap_pct'), '%')}"
                   f" ／ 重大リスク {', '.join(x['type'] for x in json.loads(v5['severe_risks'] or '[]')) or 'なし'}")

    if sc and origin != "auto":
        a0 = auto_baseline(conn, code, d)
        v0 = evaluate(a0, sc["price"], d)
        out.append(f"【参考：自動初期推計（採点には使っていない）】理論価値 {_n(v0.theoretical_value)} 買い {_n(v0.buy_score)}"
                   f" ／ EPS {_n(a0 and (a0.eps_forecast or a0.normalized_eps))} PER {_n(a0 and a0.assumed_per)} 利回り {_n(a0 and a0.required_return)}")

    q = conn.execute("SELECT date, close, adj_close, adjustment_factor, source FROM daily_quotes WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
                     (code, d.isoformat())).fetchone()
    out.append(f"株価データ: {dict(q) if q else '無し'}")
    splits = conn.execute("SELECT date, adjustment_factor FROM daily_quotes WHERE code=? AND date<=? AND adjustment_factor IS NOT NULL"
                          " AND adjustment_factor<>1 ORDER BY date", (code, d.isoformat())).fetchall()
    out.append("株式分割・併合（調整係数≠1）: " + ("、".join(f"{r['date']} 係数{r['adjustment_factor']}" for r in splits) or "なし"))
    cutoff = f"{d.isoformat()}T23:59:59+09:00"
    fys = conn.execute(
        """SELECT fiscal_period_end, period_type, disclosed_at, document_type, eps, forecast_eps, forecast_fy_end,
                  net_income, shares_outstanding, source FROM financials
           WHERE code=? AND disclosed_at<=? ORDER BY disclosed_at DESC LIMIT 6""", (code, cutoff)).fetchall()
    out.append("直近の決算データ（新しい順。分割前の決算は分割前の株数・EPS のまま）:")
    out += [f"  {dict(r)}" for r in fys] or ["  無し"]
    fy = next((r for r in fys if r["period_type"] == "FY"), None)
    if fy:
        out.append(f"採用した最新の会社予想: {latest_forecast(conn, code, fy['fiscal_period_end'], cutoff)}")
        if fy["net_income"] and fy["shares_outstanding"] and fy["eps"]:
            implied = fy["net_income"] / fy["shares_outstanding"]
            ratio = fy["eps"] / implied if implied else None
            out.append(f"検算: 純利益÷発行済株式数 = {implied:.1f}円（EPS {fy['eps']}円との比 {ratio:.2f}）"
                       + ("  ← 株式分割・単位の不一致の可能性" if ratio and not 0.8 < ratio < 1.25 else ""))
    disc = conn.execute("SELECT disclosed_at, category, title FROM disclosures WHERE code=? ORDER BY disclosed_at DESC LIMIT 5",
                        (code,)).fetchall()
    out.append("直近の開示: " + (" ／ ".join(f"{r['disclosed_at'][:10]} {r['category']} {r['title']}" for r in disc) or "なし"))
    return out
