"""初期監視60銘柄。

category は必要利回り・安全余裕の初期値を決める分類で、検証用の初期仮定。
上場状況は起動後に J-Quants 上場銘柄一覧で自動検証し、securities.listing_status を更新する。
"""
from __future__ import annotations

import sqlite3

from .db import now_iso, upsert

# (code, name, group, category, asset_type)
SEED: list[tuple[str, str, str, str, str]] = [
    # 既存16銘柄
    ("2644", "グローバルX 半導体関連-日本株式 ETF", "existing", "cyclical", "etf"),
    ("5801", "古河電気工業", "existing", "cyclical", "stock"),
    ("5802", "住友電気工業", "existing", "cyclical", "stock"),
    ("6479", "ミネベアミツミ", "existing", "cyclical", "stock"),
    ("6526", "ソシオネクスト", "existing", "growth", "stock"),
    ("6590", "芝浦メカトロニクス", "existing", "cyclical", "stock"),
    ("6613", "QDレーザ", "existing", "emerging_loss", "stock"),
    ("6855", "日本電子材料", "existing", "cyclical", "stock"),
    ("6981", "村田製作所", "existing", "cyclical", "stock"),
    ("6976", "太陽誘電", "existing", "cyclical", "stock"),
    ("3176", "三洋貿易", "existing", "stable", "stock"),
    ("6269", "三井海洋開発", "existing", "cyclical", "stock"),
    ("7012", "川崎重工業", "existing", "cyclical", "stock"),
    ("7011", "三菱重工業", "existing", "growth", "stock"),
    ("1605", "INPEX", "existing", "cyclical", "stock"),
    ("6701", "NEC", "existing", "stable", "stock"),
    # 追加24銘柄
    ("5803", "フジクラ", "added", "growth", "stock"),
    ("6857", "アドバンテスト", "added", "growth", "stock"),
    ("8035", "東京エレクトロン", "added", "cyclical", "stock"),
    ("6146", "ディスコ", "added", "growth", "stock"),
    ("7735", "SCREENホールディングス", "added", "cyclical", "stock"),
    ("6723", "ルネサスエレクトロニクス", "added", "cyclical", "stock"),
    ("6323", "ローツェ", "added", "cyclical", "stock"),
    ("6525", "KOKUSAI ELECTRIC", "added", "cyclical", "stock"),
    ("6227", "AIメカテック", "added", "cyclical", "stock"),
    ("4063", "信越化学工業", "added", "cyclical", "stock"),
    ("6762", "TDK", "added", "cyclical", "stock"),
    ("6963", "ローム", "added", "cyclical", "stock"),
    ("6501", "日立製作所", "added", "stable", "stock"),
    ("6503", "三菱電機", "added", "stable", "stock"),
    ("1944", "きんでん", "added", "stable", "stock"),
    ("6361", "荏原製作所", "added", "cyclical", "stock"),
    ("6330", "東洋エンジニアリング", "added", "cyclical", "stock"),
    ("5713", "住友金属鉱山", "added", "cyclical", "stock"),
    ("5711", "三菱マテリアル", "added", "cyclical", "stock"),
    ("7003", "三井E&S", "added", "cyclical", "stock"),
    ("1662", "石油資源開発", "added", "cyclical", "stock"),
    ("7729", "東京精密", "added", "cyclical", "stock"),
    ("6988", "日東電工", "added", "cyclical", "stock"),
    ("6965", "浜松ホトニクス", "added", "cyclical", "stock"),
    # 新興・成長株20銘柄
    ("186A", "アストロスケールホールディングス", "emerging", "emerging_loss", "stock"),
    ("464A", "QPSホールディングス", "emerging", "emerging_loss", "stock"),
    ("290A", "Synspective", "emerging", "emerging_loss", "stock"),
    ("9348", "ispace", "emerging", "emerging_loss", "stock"),
    ("5574", "ABEJA", "emerging", "growth", "stock"),
    ("5582", "グリッド", "emerging", "growth", "stock"),
    ("5591", "AVILEN", "emerging", "growth", "stock"),
    ("4259", "エクサウィザーズ", "emerging", "growth", "stock"),
    ("5246", "ELEMENTS", "emerging", "emerging_loss", "stock"),
    ("6521", "オキサイド", "emerging", "emerging_loss", "stock"),
    ("285A", "キオクシアホールディングス", "emerging", "cyclical", "stock"),
    ("4478", "freee", "emerging", "growth", "stock"),
    ("3994", "マネーフォワード", "emerging", "growth", "stock"),
    ("4443", "Sansan", "emerging", "growth", "stock"),
    ("5032", "ANYCOLOR", "emerging", "growth", "stock"),
    ("5253", "カバー", "emerging", "growth", "stock"),
    ("4385", "メルカリ", "emerging", "growth", "stock"),
    ("4889", "レナサイエンス", "emerging", "emerging_loss", "stock"),
    ("4894", "クオリプス", "emerging", "emerging_loss", "stock"),
    ("2160", "ジーエヌアイグループ", "emerging", "growth", "stock"),
]

# コード変更（持株会社化など）。過去株価の連結に使う。新コード -> (旧コード, 切替日)
CODE_PREDECESSORS: dict[str, tuple[str, str]] = {
    "464A": ("5595", "2025-12-01"),  # QPS研究所 → QPSホールディングス（単独株式移転、1:1）
}

CATEGORIES = {"stable", "growth", "cyclical", "emerging_loss"}


def _seed_note(code: str, asset_type: str) -> str | None:
    if asset_type == "etf":
        return "ETF: EPS基準の買い点数は未算定（NAV乖離で別評価）"
    if code in CODE_PREDECESSORS:
        old, since = CODE_PREDECESSORS[code]
        return f"{since} に旧コード {old} から移行。過去株価は旧コードと連結"
    return None


def seed_universe(conn: sqlite3.Connection) -> int:
    """未登録の銘柄だけを追加する（利用者が削除・変更した内容は上書きしない）。"""
    existing = {r["code"] for r in conn.execute("SELECT code FROM securities")}
    added = 0
    for code, name, group, category, asset_type in SEED:
        if code in existing:
            continue
        upsert(
            conn,
            "securities",
            {
                "code": code, "name": name, "group_name": group, "category": category,
                "asset_type": asset_type, "active": 1, "listing_status": "unverified",
                "note": _seed_note(code, asset_type),
                "added_at": now_iso(),
            },
            ["code"],
        )
        added += 1
    conn.commit()
    return added


def active_codes(conn: sqlite3.Connection) -> list[str]:
    return [r["code"] for r in conn.execute("SELECT code FROM securities WHERE active=1 ORDER BY code")]


def to_jquants_code(code: str) -> str:
    """J-Quants は5桁コード（末尾0）を使う。英字入りコード（例 186A）も同様。"""
    return code + "0" if len(code) == 4 else code


def from_jquants_code(code: str) -> str:
    return code[:4] if len(code) == 5 and code.endswith("0") else code
