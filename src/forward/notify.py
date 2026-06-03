"""
Operational notifications + heartbeat for the unattended forward clock.

Two concerns, both host-agnostic (env-configured, no laptop/launchd/macOS coupling) so
they behave identically on a dev machine and inside the deployment container:

- **Alert** (`notify`): a one-line success/failure ping per weekly run, so a failed Sunday
  session is *loud* instead of a silent hole in the GATE-0 evidence record. Dispatches to
  every configured channel — **Telegram** (TELEGRAM_BOT_TOKEN/CHAT_ID; instant phone push,
  no sender account) and/or **email** (provider-agnostic SMTP env). A no-op (logged, never
  raised) when none is configured, so local runs aren't burdened.
- **Heartbeat** (`write_heartbeat`/`read_heartbeat`/`heartbeat_stale`): a tiny JSON record
  of the last run's wall-clock time + status, so "has the clock gone quiet?" is a cheap,
  pollable check (a separate healthcheck can alert when it goes stale).

Notifications must never themselves break a run: every path here swallows its own errors.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_PATH = Path("data/forward/last_run.json")


# ==========================================
# EMAIL
# ==========================================


def _smtp_config() -> dict | None:
    """SMTP settings from env, or None if not configured (then email is a no-op).

    Required: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO.
    Optional: SMTP_PORT (default 587), SMTP_FROM (default SMTP_USER).
    Port 465 → implicit TLS (SMTP_SSL); any other port → STARTTLS.
    """
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    to = os.getenv("ALERT_EMAIL_TO")
    if not (host and user and password and to):
        return None
    port = int(os.getenv("SMTP_PORT", "587"))
    return {
        "host": host, "port": port, "user": user, "password": password,
        "from": os.getenv("SMTP_FROM", user), "to": to,
    }


def send_email(subject: str, body: str) -> bool:
    """Send a plaintext alert email. Returns True if sent, False if skipped/failed.

    Never raises — a notification problem must not abort or fail the run that triggered
    it. When SMTP env is absent the call is a logged no-op (returns False)."""
    cfg = _smtp_config()
    if cfg is None:
        logger.info("email alert skipped: SMTP not configured "
                    "(set SMTP_HOST/USER/PASSWORD/ALERT_EMAIL_TO)")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content(body)

    try:
        if cfg["port"] == 465:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30) as s:
                s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
                s.starttls()
                s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        logger.info(f"alert email sent to {cfg['to']}: {subject}")
        return True
    except Exception as e:  # noqa: BLE001 — notifications never break the run
        logger.warning(f"alert email failed ({type(e).__name__}: {e})")
        return False


# ==========================================
# TELEGRAM
# ==========================================


def send_telegram(text: str) -> bool:
    """Send a Telegram message via the Bot API. Returns True if sent, else False.

    Needs TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (a private chat between you and your bot).
    No-op (logged) when unset; never raises — a notification problem can't break a run."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        logger.info("telegram alert skipped: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code == 200:
            logger.info("telegram alert sent")
            return True
        logger.warning(f"telegram alert failed: HTTP {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:  # noqa: BLE001 — notifications never break the run
        logger.warning(f"telegram alert failed ({type(e).__name__}: {e})")
        return False


# ==========================================
# DISPATCH
# ==========================================


def notify(subject: str, body: str = "") -> bool:
    """Send an alert over every configured channel (Telegram and/or email). Returns True
    if at least one channel delivered. Logged no-op when none is configured. This is what
    the run wraps its success/failure in — channel choice is pure env/config."""
    text = f"{subject}\n\n{body}" if body else subject
    sent = send_telegram(text)
    sent = send_email(subject, body) or sent
    if not sent:
        logger.info("no alert channel configured (set TELEGRAM_* and/or SMTP_*)")
    return sent


# ==========================================
# HEARTBEAT
# ==========================================


@dataclass(frozen=True)
class Heartbeat:
    status: str           # "ok" | "error"
    as_of: str            # the session's decision date
    ts: str               # wall-clock UTC ISO timestamp of the run
    note: str = ""        # short digest line or error message


def write_heartbeat(
    status: str, as_of: str, note: str = "", path: Path = DEFAULT_HEARTBEAT_PATH
) -> None:
    """Record the last run's outcome. Best-effort — never raises."""
    hb = Heartbeat(status=status, as_of=as_of,
                   ts=datetime.now(UTC).isoformat(), note=note[:500])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(asdict(hb), f, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"heartbeat write failed: {e}")


def read_heartbeat(path: Path = DEFAULT_HEARTBEAT_PATH) -> Heartbeat | None:
    """Load the last heartbeat, or None if absent/unreadable."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return Heartbeat(**json.load(f))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"heartbeat read failed: {e}")
        return None


def heartbeat_stale(hb: Heartbeat | None, max_age_hours: float) -> bool:
    """True if there's no heartbeat, the last run errored, or it's older than the
    allowed age — i.e. the clock has gone quiet and a human should look."""
    if hb is None or hb.status != "ok":
        return True
    try:
        age = datetime.now(UTC) - datetime.fromisoformat(hb.ts)
    except ValueError:
        return True
    return age.total_seconds() > max_age_hours * 3600
