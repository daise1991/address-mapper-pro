"""EDINET API v2（金融庁・無料・APIキー必須）。

用途: 有価証券報告書・四半期/半期報告書・臨時報告書・有価証券届出書の検出。
増資・希薄化（有価証券届出書／発行登録追補／臨時報告書）の検知と、
有利子負債など決算短信サマリーに無い項目の補完（XBRL/CSV 取得）に使う。
金融庁の推奨に従い、リクエスト間隔を空ける。
"""
from __future__ import annotations

import time
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
                })
            d += timedelta(days=1)
        return out
