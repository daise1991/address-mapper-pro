import json

from stock_analysis.db import connect
from stock_analysis.notify import generate_vapid_keys, notify, write_vapid_to_env


def test_vapid_generation_and_env_write(tmp_path):
    pub, priv = generate_vapid_keys()
    assert len(pub) == 87 and len(priv) == 43
    env = tmp_path / ".env"
    env.write_text("# c\nVAPID_PUBLIC_KEY=\nVAPID_PRIVATE_KEY=\nOTHER=1\n", encoding="utf-8")
    assert "生成" in write_vapid_to_env(env)
    text = env.read_text(encoding="utf-8")
    assert "OTHER=1" in text and "VAPID_PUBLIC_KEY=\n" not in text
    assert "変更しません" in write_vapid_to_env(env)


def test_web_push_sent_and_expired_subscription_removed(tmp_path, monkeypatch):
    pub, priv = generate_vapid_keys()
    monkeypatch.setenv("VAPID_PUBLIC_KEY", pub)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", priv)
    import pywebpush

    sent = []

    class Resp:
        status_code = 410

    def fake(subscription_info, data, **kw):
        if "gone" in subscription_info["endpoint"]:
            raise pywebpush.WebPushException("gone", response=Resp())
        sent.append(json.loads(data))

    monkeypatch.setattr(pywebpush, "webpush", fake)
    conn = connect(tmp_path / "n.db")
    for ep in ("https://push.example/ok", "https://push.example/gone"):
        conn.execute("INSERT INTO push_subscriptions VALUES (?, 'p', 'a', 'x')", (ep,))
    conn.commit()
    assert notify(conn, "test", "題", "本文") == 1
    assert sent[0]["title"] == "題"
    assert [r[0] for r in conn.execute("SELECT endpoint FROM push_subscriptions")] == ["https://push.example/ok"]
    assert conn.execute("SELECT delivered FROM notifications").fetchone()[0] == 1
