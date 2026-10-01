"""Transactional email: verification, password reset, alerts.

Sends over SMTP using the stdlib (``smtplib`` + ``email.mime``) — no
dependency for the handful of messages this product sends (D-002).

**When SMTP is not configured, messages are written to ``data/outbox/`` as
``.eml`` files instead of being sent.** That is not a stub: it means local
development, the Playwright smoke test and CI can exercise the real
verification and password-reset flows end to end, with no mail server and no
network, and a developer can open the file to see exactly what a customer
would have received.

Sending is best-effort by design. A mail server outage must never turn a
successful signup into a 500 — the customer can always request a resend.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from app.core.config import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

OUTBOX_DIR = PROJECT_ROOT / "data" / "outbox"


class EmailError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(settings.smtp_host)


def outbox_path(recipient: str, subject: str) -> Path:
    """A deterministic, greppable filename: `2026-09-29-verify-ada@example.com.eml`."""
    safe_subject = "".join(c if c.isalnum() else "-" for c in subject.lower())[:40]
    safe_recipient = "".join(c if c.isalnum() or c in "@.-" else "-" for c in recipient)
    from app.core.database.models import utcnow

    return OUTBOX_DIR / f"{utcnow():%Y-%m-%d-%H%M%S}-{safe_subject}-{safe_recipient}.eml"


def build_message(to: str, subject: str, text: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    # Plain text only, on purpose: these are transactional notices and a
    # customer must be able to read them in any client.
    message.set_content(text)
    return message


def send(to: str, subject: str, text: str) -> bool:
    """Send, or write to the outbox. Returns True if actually sent.

    Never raises: a failed send is logged, and the caller moves on. A
    verification mail that failed to send must not roll back a signup.
    """
    message = build_message(to, subject, text)
    if not is_configured():
        _write_to_outbox(message, to, subject)
        return False
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            if settings.smtp_starttls:
                server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
        logger.info("Sent %r to %s", subject, to)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("Could not send %r to %s: %s", subject, to, exc)
        _write_to_outbox(message, to, subject)
        return False


def _write_to_outbox(message: EmailMessage, to: str, subject: str) -> None:
    try:
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        path = outbox_path(to, subject)
        path.write_text(message.as_string(), encoding="utf-8")
        logger.info("SMTP not configured; wrote %r to %s", subject, path)
    except OSError as exc:  # noqa: BLE001 - a failed outbox write must not raise
        logger.warning("Could not write to the outbox: %s", exc)


# ---------------------------------------------------------------------------
# Message bodies
# ---------------------------------------------------------------------------
def verification_email(to: str, token: str) -> tuple[str, str]:
    link = f"{settings.app_url.rstrip('/')}/verify-email?token={token}"
    subject = "Confirm your Sparton email address"
    body = f"""Hi,

Confirm your email address to finish setting up your Sparton account:

    {link}

This link is valid for 24 hours. If you did not create a Sparton account you
can safely ignore this message.

— Sparton Intelligence
"""
    return subject, body


def reset_email(to: str, token: str) -> tuple[str, str]:
    link = f"{settings.app_url.rstrip('/')}/reset-password?token={token}"
    subject = "Reset your Sparton password"
    body = f"""Hi,

Someone asked to reset the password for this address. If that was you:

    {link}

This link is valid for 24 hours and can only be used once. If it was not you,
no action is needed — your password has not changed.

— Sparton Intelligence
"""
    return subject, body


def send_verification(to: str, token: str) -> bool:
    subject, body = verification_email(to, token)
    return send(to, subject, body)


def send_password_reset(to: str, token: str) -> bool:
    subject, body = reset_email(to, token)
    return send(to, subject, body)


__all__ = [
    "OUTBOX_DIR",
    "EmailError",
    "build_message",
    "is_configured",
    "outbox_path",
    "reset_email",
    "send",
    "send_password_reset",
    "send_verification",
    "verification_email",
]
