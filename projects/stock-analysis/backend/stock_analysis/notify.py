"""プッシュ通知（Web Push / VAPID）。

iPhone は iOS 16.4 以降で「ホーム画面に追加」した PWA のみ Web Push を受信できる。
pywebpush と VAPID 鍵が未設定の場合は notifications テーブルに記録するだけ（アプリ内で表示）。
"""
from __future__ import annotations

import json
import logging
import sqlite3

from .config import get_settings
from .db import now_iso

log = logging.getLogger(__name__)


def notify(conn: sqlite3.Connection, kind: str, title: str, body: str, url: str = "/") -> int:
    conn.execute("INSERT INTO notifications (created_at, kind, title, body) VALUES (?,?,?,?)",
                 (now_iso(), kind, title, body))
    nid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    sent = _send_web_push(conn, {"title": title, "body": body, "url": url, "kind": kind})
    if sent:
        conn.execute("UPDATE notifications SET delivered=1 WHERE id=?", (nid,))
        conn.commit()
    return sent


def _send_web_push(conn: sqlite3.Connection, payload: dict[str, str]) -> int:
    s = get_settings()
    if not s.vapid_private_key:
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.warning("pywebpush 未インストールのため Web Push を送信しません")
        return 0
    sent = 0
    for sub in conn.execute("SELECT * FROM push_subscriptions").fetchall():
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=s.vapid_private_key,
                vapid_claims={"sub": s.vapid_subject},
                ttl=6 * 3600,
            )
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):  # 失効した購読は削除
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (sub["endpoint"],))
                conn.commit()
            else:
                log.warning("Web Push 失敗: %s", e)
    return sent


def generate_vapid_keys() -> tuple[str, str]:
    """Web Push 用の VAPID 鍵（公開鍵, 秘密鍵）を base64url で生成する。"""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64(public), b64(key.private_numbers().private_value.to_bytes(32, "big"))


def write_vapid_to_env(env_path) -> str:  # type: ignore[no-untyped-def]
    """backend/.env の VAPID 鍵が空なら生成して書き込む。既に設定済みなら変更しない。"""
    from pathlib import Path
    path = Path(env_path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    current = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in lines if "=" in ln and not ln.startswith("#")}
    if current.get("VAPID_PUBLIC_KEY") and current.get("VAPID_PRIVATE_KEY"):
        return "VAPID 鍵は設定済みのため変更しません"
    public, private = generate_vapid_keys()
    new = {"VAPID_PUBLIC_KEY": public, "VAPID_PRIVATE_KEY": private}
    out, seen = [], set()
    for ln in lines:
        k = ln.split("=", 1)[0] if "=" in ln and not ln.startswith("#") else None
        if k in new:
            out.append(f"{k}={new[k]}")
            seen.add(k)
        else:
            out.append(ln)
    out += [f"{k}={v}" for k, v in new.items() if k not in seen]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return "VAPID 鍵を生成して .env に保存しました"
