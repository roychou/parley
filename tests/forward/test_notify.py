"""Offline tests for the operational notify + heartbeat layer (no real SMTP/IBKR)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import src.forward.notify as notify
from src.forward.ibkr import GatewayNotReadyError, assert_paper_ready

# ---- email ----------------------------------------------------------------

_SMTP_ENV = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "ALERT_EMAIL_TO"]


def _clear_smtp(monkeypatch):
    for k in _SMTP_ENV:
        monkeypatch.delenv(k, raising=False)


def test_send_email_noop_when_unconfigured(monkeypatch):
    _clear_smtp(monkeypatch)
    # No SMTP env -> logged no-op, returns False, never raises.
    assert notify.send_email("subject", "body") is False


def test_send_email_builds_and_sends_via_starttls(monkeypatch):
    _clear_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("ALERT_EMAIL_TO", "me@example.com")  # port defaults to 587 -> STARTTLS

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent["host"], sent["port"] = host, port
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent["starttls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg):
            sent["from"], sent["to"] = msg["From"], msg["To"]
            sent["subject"] = msg["Subject"]

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    assert notify.send_email("hello", "world") is True
    assert sent["host"] == "smtp.example.com" and sent["port"] == 587
    assert sent["starttls"] is True
    assert sent["login"] == ("bot@example.com", "secret")
    assert sent["from"] == "bot@example.com" and sent["to"] == "me@example.com"
    assert sent["subject"] == "hello"


def test_send_email_uses_ssl_on_465(monkeypatch):
    _clear_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("ALERT_EMAIL_TO", "me@example.com")

    used = {"ssl": False}

    class FakeSSL:
        def __init__(self, host, port, timeout=0):
            used["port"] = port
        def __enter__(self):
            used["ssl"] = True
            return self
        def __exit__(self, *a):
            return False
        def login(self, u, p):
            pass
        def send_message(self, msg):
            pass

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSSL)
    assert notify.send_email("s", "b") is True
    assert used["ssl"] is True and used["port"] == 465


def test_send_email_swallows_smtp_errors(monkeypatch):
    _clear_smtp(monkeypatch)
    for k, v in {"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
                 "ALERT_EMAIL_TO": "t"}.items():
        monkeypatch.setenv(k, v)

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(notify.smtplib, "SMTP", boom)
    # A broken SMTP must never raise out of the notifier.
    assert notify.send_email("s", "b") is False


# ---- telegram + dispatch --------------------------------------------------

_TG_ENV = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


def _clear_all(monkeypatch):
    for k in _SMTP_ENV + _TG_ENV:
        monkeypatch.delenv(k, raising=False)


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_send_telegram_noop_when_unconfigured(monkeypatch):
    _clear_all(monkeypatch)
    assert notify.send_telegram("hi") is False


def test_send_telegram_posts_to_bot_api(monkeypatch):
    _clear_all(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    captured = {}

    def fake_post(url, json=None, timeout=0):
        captured["url"] = url
        captured["json"] = json
        return _Resp(200)

    monkeypatch.setattr(notify.requests, "post", fake_post)
    assert notify.send_telegram("hello") is True
    assert captured["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert captured["json"]["chat_id"] == "42" and captured["json"]["text"] == "hello"


def test_send_telegram_handles_api_error(monkeypatch):
    _clear_all(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: _Resp(401, "unauthorized"))
    assert notify.send_telegram("hello") is False  # non-200, no raise


def test_notify_dispatches_to_telegram_only(monkeypatch):
    _clear_all(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    seen = {}
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, json=None, timeout=0: seen.update(json) or _Resp(200))
    assert notify.notify("subj", "body") is True
    assert seen["text"] == "subj\n\nbody"          # subject + body combined for Telegram


def test_notify_noop_when_nothing_configured(monkeypatch):
    _clear_all(monkeypatch)
    assert notify.notify("subj", "body") is False


# ---- heartbeat ------------------------------------------------------------


def test_heartbeat_roundtrip_and_freshness(tmp_path):
    p = tmp_path / "last_run.json"
    notify.write_heartbeat("ok", "2026-06-07", "decided 3/5", path=p)
    hb = notify.read_heartbeat(p)
    assert hb is not None and hb.status == "ok" and hb.as_of == "2026-06-07"
    assert hb.note == "decided 3/5"
    assert notify.heartbeat_stale(hb, max_age_hours=48) is False


def test_heartbeat_missing_is_stale(tmp_path):
    assert notify.read_heartbeat(tmp_path / "nope.json") is None
    assert notify.heartbeat_stale(None, max_age_hours=48) is True


def test_heartbeat_error_status_is_stale(tmp_path):
    p = tmp_path / "last_run.json"
    notify.write_heartbeat("error", "2026-06-07", "boom", path=p)
    assert notify.heartbeat_stale(notify.read_heartbeat(p), max_age_hours=10_000) is True


def test_heartbeat_old_is_stale():
    old_ts = (datetime.now(UTC) - timedelta(hours=200)).isoformat()
    hb = notify.Heartbeat(status="ok", as_of="2026-05-01", ts=old_ts, note="stale")
    assert notify.heartbeat_stale(hb, max_age_hours=48) is True       # 200h > 48h
    assert notify.heartbeat_stale(hb, max_age_hours=300) is False      # within window


def test_heartbeat_bad_timestamp_is_stale():
    hb = notify.Heartbeat(status="ok", as_of="2026-05-01", ts="not-a-date", note="")
    assert notify.heartbeat_stale(hb, max_age_hours=48) is True


# ---- preflight ------------------------------------------------------------


class _FakeIB:
    def __init__(self, accounts):
        self._accts = accounts

    def managedAccounts(self):  # noqa: N802 — mirrors ib_async's IB.managedAccounts()
        return self._accts


def test_assert_paper_ready_accepts_paper_account():
    assert assert_paper_ready(_FakeIB(["DUQ576452"])) == "DUQ576452"


def test_assert_paper_ready_rejects_empty():
    with pytest.raises(GatewayNotReadyError):
        assert_paper_ready(_FakeIB([]))


def test_assert_paper_ready_rejects_live_account():
    with pytest.raises(GatewayNotReadyError):
        assert_paper_ready(_FakeIB(["U1234567"]))
