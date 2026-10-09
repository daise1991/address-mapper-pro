"""EDINET API v2（金融庁・無料・APIキー必須）。

用途: 有価証券報告書・四半期/半期報告書・臨時報告書・有価証券届出書の検出。
増資・希薄化（有価証券届出書／発行登録追補／臨時報告書）の検知と、
有利子負債など決算短信サマリーに無い項目の補完（XBRL/CSV 取得）に使う。
金融庁の推奨に従い、リクエスト間隔を空ける。
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
import zipfile
from datetime import date, timedelta
from typing import Any

import requests

from ..config import Settings, get_settings
from .base import DataUnavailable

BASE = "https://api.edinet-fsa.go.jp/api/v2"
REQUEST_INTERVAL_SEC = 3.0

# 書類種別コード → disclosures.category
DOC_TYPE_CATEGORY = {
    "030": "offering",   # 有価証券届出書
    "040": "offering",   # 訂正有価証券届出書
    "080": "offering",   # 発行登録書
    "100": "offering",   # 発行登録追補書類
    "120": "other",      # 有価証券報告書
    "140": "other",      # 四半期報告書
    "160": "other",      # 半期報告書
    "180": "other",      # 臨時報告書（内容で再分類）
    "220": "buyback",    # 自己株券買付状況報告書
}


class EdinetClient:
    name = "edinet"

    def __init__(self, settings: Settings | None = None, session: requests.Session | None = None):
        self.s = settings or get_settings()
        self.http = session or requests.Session()
        self._last = 0.0

    def _get(self, path: str, params: dict[str, Any]) -> requests.Response:
        if not self.s.edinet_api_key:
            raise DataUnavailable("EDINET_API_KEY 未設定")
        wait = REQUEST_INTERVAL_SEC - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        try:
            r = self.http.get(f"{BASE}{path}", params={**params, "Subscription-Key": self.s.edinet_api_key}, timeout=60)
        except requests.RequestException as e:
            raise DataUnavailable(f"EDINET 通信失敗: {e}") from e
        if r.status_code != 200:
            raise DataUnavailable(f"EDINET HTTP {r.status_code}")
        return r

    def documents(self, d: date) -> list[dict[str, Any]]:
        body = self._get("/documents.json", {"date": d.isoformat(), "type": 2}).json()
        status = str(body.get("metadata", {}).get("status"))
        if status != "200":
            raise DataUnavailable(f"EDINET status {status}: {body.get('metadata', {}).get('message')}")
        return body.get("results") or []

    def disclosures_for(self, codes: set[str], start: date, end: date) -> list[dict[str, Any]]:
        """期間内の対象銘柄の提出書類を disclosures 形式で返す（secCode は5桁）。"""
        out = []
        d = start
        while d <= end:
            for doc in self.documents(d):
                sec = (doc.get("secCode") or "")[:4]
                if sec not in codes:
                    continue
                cat = DOC_TYPE_CATEGORY.get(doc.get("docTypeCode") or "", "other")
                desc = doc.get("docDescription") or ""
                if doc.get("docTypeCode") == "180" and any(k in desc for k in ("新株", "募集", "第三者割当")):
                    cat = "offering"
                out.append({
                    "code": sec,
                    "disclosed_at": (doc.get("submitDateTime") or d.isoformat()).replace(" ", "T") + "+09:00",
                    "title": desc,
                    "category": cat,
                    "url": f"edinet:{doc.get('docID')}",  # APIキーをURLに残さないため docID のみ保存
                    "doc_id": doc.get("docID"),
                    "doc_type": doc.get("docTypeCode"),
                    "period_end": doc.get("periodEnd"),
                    "has_csv": str(doc.get("csvFlag")) == "1",
                })
            d += timedelta(days=1)
        return out


# --- 有価証券報告書 XBRL(CSV) から有利子負債を集計 -----------------------------------
ANNUAL_REPORT_TYPES = {"120", "130"}  # 有価証券報告書・訂正有価証券報告書

# 有利子負債として合算する要素名（名前空間を除いたローカル名の部分一致）。リース債務・利息・CF項目は除外
DEBT_KEYWORDS = ("LoansPayable", "Borrowings", "BondsPayable", "Bonds", "CommercialPapers")
DEBT_EXCLUDE = ("Lease", "Interest", "Proceeds", "Repayment", "Redemption", "Increase", "Decrease",
                "Issuance", "Payments", "Allowance", "Discount", "Premium", "Receivable", "Investment",
                "Securities", "TextBlock", "Abstract")


def _read_rows(data: bytes) -> list[list[str]]:
    for enc in ("utf-16", "utf-8-sig", "cp932"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    return list(csv.reader(io.StringIO(text), delimiter="\t"))


def parse_interest_bearing_debt(zip_bytes: bytes) -> tuple[float | None, dict[str, float]]:
    """EDINET の CSV(type=5) ZIP から当期末の有利子負債を合計する。

    列: 要素ID, 項目名, コンテキストID, 相対年度, 連結・個別, 期間・時点, ユニットID, 単位, 値
    連結（CurrentYearInstant）を優先し、連結が無い会社は個別（..._NonConsolidatedMember）を使う。
    """
    found: dict[str, dict[str, float]] = {"consolidated": {}, "non_consolidated": {}}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.lower().endswith(".csv") or "jpcrp" not in name.lower():
                continue  # 本表（jpcrp030000-asr...）のみ。監査報告書等は除く
            rows = _read_rows(z.read(name))
            for r in rows[1:]:
                if len(r) < 9:
                    continue
                elem, ctx, value = r[0], r[2], r[8]
                local = elem.split(":")[-1]
                if not any(k in local for k in DEBT_KEYWORDS) or any(x in local for x in DEBT_EXCLUDE):
                    continue
                if ctx == "CurrentYearInstant":
                    bucket = "consolidated"
                elif ctx == "CurrentYearInstant_NonConsolidatedMember":
                    bucket = "non_consolidated"
                else:
                    continue
                try:
                    found[bucket][local] = float(value)
                except ValueError:
                    continue
    parts = found["consolidated"] or found["non_consolidated"]
    if not parts:
        return None, {}
    # 「社債及び借入金」等の合計行と内訳が両方ある場合は合計行だけを使い二重計上を防ぐ
    totals = {k: v for k, v in parts.items() if "And" in k}
    use = totals or parts
    return sum(use.values()), use


def store_debt_from_report(conn: sqlite3.Connection, client: "EdinetClient", code: str, doc_id: str,
                           period_end: str) -> float | None:
    from ..db import now_iso, upsert
    r = client._get(f"/documents/{doc_id}", {"type": 5})
    debt, parts = parse_interest_bearing_debt(r.content)
    upsert(conn, "edinet_balance", {
        "code": code, "period_end": period_end, "interest_bearing_debt": debt,
        "components": json.dumps(parts, ensure_ascii=False), "doc_id": doc_id,
        "source": "edinet", "fetched_at": now_iso(),
    }, ["code", "period_end"])
    conn.commit()
    return debt


def sync_annual_reports(conn: sqlite3.Connection, client: "EdinetClient", docs: list[dict[str, Any]]) -> int:
    n = 0
    for d in docs:
        if d.get("doc_type") in ANNUAL_REPORT_TYPES and d.get("has_csv") and d.get("period_end"):
            try:
                store_debt_from_report(conn, client, d["code"], d["doc_id"], d["period_end"])
                n += 1
            except (DataUnavailable, zipfile.BadZipFile):
                continue
    return n
