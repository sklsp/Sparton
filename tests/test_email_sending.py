"""Tests for app.core.email: SMTP delivery, outbox fallback and message bodies."""

from __future__ import annotations

import re
import smtplib
from email import message_from_string
from email.policy import default as email_default_policy
from pathlib import Path

import pytest  # noqa: F401 - kept for future skip/xfail use in this file

import app.core.email as em
from app.core.config import settings


def _outbox_files(tmp_path) -> list[Path]:
    outbox = tmp_path / "outbox"
    return sorted(outbox.glob("*.eml")) if outbox.is_dir() else []


def install_fake_smtp(monkeypatch, *, ctor_error=None, send_error=None):
    """Replace smtplib.SMTP with a recorder and return the recorded calls."""
    calls = {"ctor": [], "starttls": 0, "login": [], "sent": []}

    class FakeSMTP:
        def __init__(self, host, port=None, timeout=None):
            if ctor_error is not None:
                raise ctor_error("smtp server unreachable")
            calls["ctor"].append({"host": host, "port": port, "timeout": timeout})

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def starttls(self):
            calls["starttls"] += 1

        def login(self, user, password):
            calls["login"].append((user, password))

        def send_message(self, message):
            if send_error is not None:
                raise send_error("smtp server rejected the message")
            calls["sent"].append(message)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return calls


def _configure_smtp(monkeypatch, *, starttls=True, user=""):
    monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
    monkeypatch.setattr(settings, "smtp_port", 2525)
    monkeypatch.setattr(settings, "smtp_from", "no-reply@sparton.example")
    monkeypatch.setattr(settings, "smtp_starttls", starttls)
    monkeypatch.setattr(settings, "smtp_user", user)
    monkeypatch.setattr(settings, "smtp_password", "secret-pw")


def test_is_configured_is_false_without_smtp_host(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    assert em.is_configured() is False


def test_is_configured_is_true_with_smtp_host(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
    assert em.is_configured() is True


def test_build_message_sets_sender_recipient_subject_and_plain_text(monkeypatch):
    _configure_smtp(monkeypatch)
    message = em.build_message("ada@example.com", "Hello there", "Line one\nLine two")
    assert message["From"] == "no-reply@sparton.example"
    assert message["To"] == "ada@example.com"
    assert message["Subject"] == "Hello there"
    assert message.get_content_type() == "text/plain"
    # set_content terminates the body with a newline; that is part of the contract.
    assert message.get_content() == "Line one\nLine two\n"


def test_outbox_path_sanitizes_subject_and_recipient(tmp_path):
    path = em.outbox_path("Ada Lovelace <ada@example.com>", "Verify Your Email!")
    name = path.name
    assert str(path.parent) == str(em.OUTBOX_DIR)
    assert name.endswith(".eml")
    assert "verify-your-email" in name
    assert "@example.com" in name
    for bad in (" ", "!", "<", ">"):
        assert bad not in name


def test_outbox_path_truncates_long_subjects_to_40_characters(tmp_path):
    subject = "a" * 60
    path = em.outbox_path("ada@example.com", subject)
    assert f"-{'a' * 40}-" in path.name
    assert f"-{'a' * 41}-" not in path.name


def test_outbox_path_name_starts_with_a_date_prefix(tmp_path):
    path = em.outbox_path("ada@example.com", "Hello")
    assert re.match(r"\d{4}-\d{2}-\d{2}-\d{6}-", path.name) is not None


def test_send_writes_an_eml_file_when_smtp_is_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "smtp_host", "")
    assert em.send("ada@example.com", "Hello there", "Body text") is False
    files = _outbox_files(tmp_path)
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "To: ada@example.com" in content
    assert "Subject: Hello there" in content
    assert "Body text" in content


def test_send_rich_writes_a_multipart_eml_with_html_and_text_alternatives(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "smtp_host", "")
    html = "<html><body><b>Weekly digest</b></body></html>"
    assert em.send_rich("ada@example.com", "Digest", "Plain version", html) is False
    files = _outbox_files(tmp_path)
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "multipart/alternative" in content
    assert "Plain version" in content
    assert html in content


def test_send_connects_to_the_configured_server_and_returns_true(monkeypatch, tmp_path):
    calls = install_fake_smtp(monkeypatch)
    _configure_smtp(monkeypatch)
    assert em.send("ada@example.com", "Hello there", "Body text") is True
    assert calls["ctor"] == [{"host": "mail.example.com", "port": 2525, "timeout": 20}]
    message = calls["sent"][0]
    assert message["From"] == "no-reply@sparton.example"
    assert message["To"] == "ada@example.com"
    assert message["Subject"] == "Hello there"
    assert not (tmp_path / "outbox").exists()


def test_send_starts_tls_and_logs_in_when_they_are_configured(monkeypatch):
    calls = install_fake_smtp(monkeypatch)
    _configure_smtp(monkeypatch, starttls=True, user="postmaster")
    em.send("ada@example.com", "Hello there", "Body text")
    assert calls["starttls"] == 1
    assert calls["login"] == [("postmaster", "secret-pw")]


def test_send_skips_starttls_and_login_when_they_are_not_configured(monkeypatch):
    calls = install_fake_smtp(monkeypatch)
    _configure_smtp(monkeypatch, starttls=False, user="")
    em.send("ada@example.com", "Hello there", "Body text")
    assert calls["starttls"] == 0
    assert calls["login"] == []


def test_send_rich_delivers_a_multipart_message_over_smtp(monkeypatch):
    calls = install_fake_smtp(monkeypatch)
    _configure_smtp(monkeypatch)
    html = "<html><body><b>Weekly digest</b></body></html>"
    assert em.send_rich("ada@example.com", "Digest", "Plain version", html) is True
    message = calls["sent"][0]
    parts = {part.get_content_type(): part.get_content() for part in message.iter_parts()}
    assert parts == {"text/plain": "Plain version\n", "text/html": html + "\n"}


def test_rejected_message_falls_back_to_the_outbox_and_returns_false(monkeypatch, tmp_path):
    install_fake_smtp(monkeypatch, send_error=smtplib.SMTPException)
    _configure_smtp(monkeypatch)
    assert em.send("ada@example.com", "Hello there", "Body text") is False
    files = _outbox_files(tmp_path)
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "Subject: Hello there" in content
    assert "Body text" in content


def test_unreachable_server_is_handled_the_same_way(monkeypatch, tmp_path):
    calls = install_fake_smtp(monkeypatch, ctor_error=ConnectionRefusedError)
    _configure_smtp(monkeypatch)
    assert em.send("ada@example.com", "Hello there", "Body text") is False
    assert calls["sent"] == []
    files = _outbox_files(tmp_path)
    assert len(files) == 1


def test_send_never_raises_when_the_outbox_cannot_be_written(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "smtp_host", "")
    # A regular file where the outbox directory should be makes mkdir raise.
    (tmp_path / "outbox").write_text("not a directory", encoding="utf-8")
    assert em.send("ada@example.com", "Hello there", "Body text") is False


def test_verification_email_contains_the_token_link(monkeypatch):
    monkeypatch.setattr(settings, "app_url", "https://app.example.com")
    subject, body = em.verification_email("ada@example.com", "tok-123")
    assert subject == "Confirm your Sparton email address"
    assert "https://app.example.com/verify-email?token=tok-123" in body


def test_app_url_trailing_slash_does_not_double_in_links(monkeypatch):
    monkeypatch.setattr(settings, "app_url", "https://app.example.com/")
    _, body = em.verification_email("ada@example.com", "tok-123")
    assert "https://app.example.com/verify-email?token=tok-123" in body
    assert "//verify-email" not in body


def test_reset_email_contains_the_single_use_token_link(monkeypatch):
    monkeypatch.setattr(settings, "app_url", "https://app.example.com")
    subject, body = em.reset_email("ada@example.com", "tok-456")
    assert subject == "Reset your Sparton password"
    assert "https://app.example.com/reset-password?token=tok-456" in body


def test_send_verification_writes_the_token_mail_to_the_outbox(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "smtp_host", "")
    assert em.send_verification("ada@example.com", "tok-123") is False
    files = _outbox_files(tmp_path)
    assert len(files) == 1
    assert "confirm-your-sparton-email-address" in files[0].name
    # The body carries a non-ASCII signature, so the part is base64 encoded on disk.
    body = message_from_string(
        files[0].read_text(encoding="utf-8"), policy=email_default_policy
    ).get_content()
    assert "/verify-email?token=tok-123" in body


def test_send_password_reset_writes_the_token_mail_to_the_outbox(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "smtp_host", "")
    assert em.send_password_reset("ada@example.com", "tok-456") is False
    files = _outbox_files(tmp_path)
    assert len(files) == 1
    assert "reset-your-sparton-password" in files[0].name
    body = message_from_string(
        files[0].read_text(encoding="utf-8"), policy=email_default_policy
    ).get_content()
    assert "/reset-password?token=tok-456" in body


def test_email_error_is_a_runtime_error():
    assert issubclass(em.EmailError, RuntimeError)
    try:
        raise em.EmailError("smtp down")
    except RuntimeError as exc:
        assert str(exc) == "smtp down"
    else:  # pragma: no cover - unreachable if the raise above works
        pytest.fail("EmailError was not raised")


