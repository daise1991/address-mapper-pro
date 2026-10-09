"""実行設定。秘密情報は環境変数（.env）からのみ読み込み、リポジトリに保存しない。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def _db_path() -> Path:
    # 相対パスは backend ディレクトリ基準（タスクスケジューラ等で作業フォルダが変わっても同じDBを使う）
    p = Path(os.environ.get("STOCK_DB_PATH", BASE_DIR / "data" / "stock.db"))
    return p if p.is_absolute() or str(p) == ":memory:" else (BASE_DIR / p).resolve()


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=_db_path)
    # "jquants" | "demo"。demo は合成データで、本番DBと混在させない。
    data_mode: str = field(default_factory=lambda: os.environ.get("STOCK_DATA_MODE", "demo"))
    jquants_api_key: str | None = field(default_factory=lambda: os.environ.get("JQUANTS_API_KEY"))
    jquants_base_url: str = field(default_factory=lambda: os.environ.get("JQUANTS_BASE_URL", "https://api.jquants.com/v2"))
    edinet_api_key: str | None = field(default_factory=lambda: os.environ.get("EDINET_API_KEY"))
    app_token: str | None = field(default_factory=lambda: os.environ.get("STOCK_APP_TOKEN"))
    vapid_private_key: str | None = field(default_factory=lambda: os.environ.get("VAPID_PRIVATE_KEY"))
    vapid_public_key: str | None = field(default_factory=lambda: os.environ.get("VAPID_PUBLIC_KEY"))
    vapid_subject: str = field(default_factory=lambda: os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com"))
    # 終値が揃うのを待つ最終時刻（JST, HH:MM）。これを過ぎたら「遅延」として通知し、分析は確定させない。
    data_deadline: str = field(default_factory=lambda: os.environ.get("STOCK_DATA_DEADLINE", "20:00"))
    retry_interval_min: int = field(default_factory=lambda: int(os.environ.get("STOCK_RETRY_INTERVAL_MIN", "15")))


def get_settings() -> Settings:
    return Settings()
