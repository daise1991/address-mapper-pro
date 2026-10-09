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
