"""Tests for app/core/auth/service.py: passwords, sessions, RBAC helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, timedelta

from sqlalchemy import func, select

from app.core.auth.service import (
    SESSION_TTL,
    audit,
    create_session,
    ensure_default_organization,
    hash_password,
    resolve_session,
    revoke_session,
    role_at_least,
    slugify,
    unique_slug,
    verify_password,
)
from app.core.database.identity import AuditLog, Organization, User
from app.core.database.models import utcnow


def _make_org(db_session, name="Acme", slug=None):
    org = Organization(name=name, slug=slug if slug is not None else "acme")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org


def _make_user(db_session, org, email="user@example.com", role="viewer"):
    user = User(
        organization_id=org.id,
        email=email,
        password_hash=hash_password("correct-horse-battery"),
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_role_at_least_orders_roles():
    assert role_at_least("admin", "manager") is True
    assert role_at_least("manager", "analyst") is True
    assert role_at_least("viewer", "viewer") is True
    assert role_at_least("viewer", "manager") is False
    assert role_at_least("analyst", "admin") is False


def test_role_at_least_rejects_unknown_roles():
    # Unknown roles score -1, unknown requirements score 99: neither side can pass.
    assert role_at_least("ghost", "viewer") is False
    assert role_at_least("admin", "ghost") is False


def test_hash_password_uses_scrypt_with_a_random_salt():
    first = hash_password("hunter2")
    second = hash_password("hunter2")
    scheme, salt_hex, digest_hex = first.split("$")
    assert scheme == "scrypt"
    assert len(bytes.fromhex(salt_hex)) == 16
    # The default dklen varies with the OpenSSL build (32 or 64 bytes).
    assert len(bytes.fromhex(digest_hex)) >= 32
    # A fresh salt per call means the same password hashes differently.
    assert first != second


def test_verify_password_accepts_the_correct_password():
    stored = hash_password("correct-horse-battery")
    assert verify_password("correct-horse-battery", stored) is True


def test_verify_password_rejects_a_wrong_password():
    stored = hash_password("correct-horse-battery")
    assert verify_password("wrong-password", stored) is False


def test_verify_password_rejects_a_malformed_stored_value():
    # No "$" separators, and too many of them: both fail the three-part split.
    assert verify_password("anything", "not-a-valid-hash") is False
    assert verify_password("anything", "a$b$c$d") is False


def test_verify_password_rejects_an_unknown_scheme():
    bogus = f"bcrypt${'ab' * 16}${'cd' * 32}"
    assert verify_password("correct-horse-battery", bogus) is False


def test_create_session_stores_only_the_token_hash(db_session):
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    token, row = create_session(db_session, user)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is not None
    # The raw token never touches the database; only its SHA-256 does.
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert row.user_id == user.id


def test_create_session_expires_after_the_ttl(db_session):
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    _, row = create_session(db_session, user)
    # SQLite hands back naive datetimes; normalize like Session.is_valid does.
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    delta = expires - utcnow()
    assert abs(delta - SESSION_TTL) < timedelta(seconds=5)


def test_resolve_session_returns_the_user_for_a_valid_token(db_session):
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    token, _ = create_session(db_session, user)
    resolved = resolve_session(db_session, token)
    assert resolved is not None and resolved.id == user.id


def test_resolve_session_rejects_an_unknown_token(db_session):
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    create_session(db_session, user)
    assert resolve_session(db_session, "no-such-token") is None


def test_resolve_session_rejects_a_tampered_token(db_session):
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    token, _ = create_session(db_session, user)
    assert resolve_session(db_session, token + "x") is None


def test_resolve_session_rejects_an_expired_session(db_session):
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    token, row = create_session(db_session, user)
    row.expires_at = utcnow() - timedelta(hours=1)
    db_session.commit()
    assert resolve_session(db_session, token) is None


def test_resolve_session_rejects_a_revoked_session(db_session):
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    token, _ = create_session(db_session, user)
    assert revoke_session(db_session, token) is True
    assert resolve_session(db_session, token) is None


def test_resolve_session_rejects_an_inactive_user(db_session):
    # A session row can outlive the user's active status; resolution must not.
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    token, _ = create_session(db_session, user)
    user.is_active = False
    db_session.commit()
    assert resolve_session(db_session, token) is None


def test_resolve_session_rejects_a_deleted_user(db_session):
    # Deleting the user removes their sessions with it, so a live token must stop resolving.
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    token, _ = create_session(db_session, user)
    db_session.delete(user)
    db_session.commit()
    assert resolve_session(db_session, token) is None


def test_revoke_session_sets_revoked_at_and_returns_true(db_session):
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    token, row = create_session(db_session, user)
    assert revoke_session(db_session, token) is True
    db_session.refresh(row)
    assert row.revoked_at is not None


def test_revoke_unknown_token_returns_false(db_session):
    assert revoke_session(db_session, "no-such-token") is False


def test_audit_records_the_event(db_session):
    org = _make_org(db_session)
    user = _make_user(db_session, org)
    audit(
        db_session,
        action="login",
        actor_user_id=user.id,
        organization_id=org.id,
        resource="users/1",
        outcome="success",
        correlation_id="corr-1",
        detail={"ip": "127.0.0.1"},
    )
    row = db_session.execute(select(AuditLog)).scalar_one()
    assert row.actor_user_id == user.id
    assert row.organization_id == org.id
    assert row.action == "login"
    assert row.resource == "users/1"
    assert row.outcome == "success"
    assert row.correlation_id == "corr-1"
    assert row.detail == {"ip": "127.0.0.1"}


def test_audit_uses_defaults_for_optional_fields(db_session):
    audit(db_session, action="logout")
    row = db_session.execute(select(AuditLog)).scalar_one()
    assert row.actor_user_id is None
    assert row.organization_id is None
    assert row.resource == ""
    assert row.outcome == "success"
    assert row.correlation_id is None
    assert row.detail is None


def test_audit_truncates_the_resource_to_160_chars(db_session):
    audit(db_session, action="login", resource="r" * 200)
    row = db_session.execute(select(AuditLog)).scalar_one()
    assert len(row.resource) == 160
    assert row.resource == "r" * 160


def test_ensure_default_organization_creates_one_when_none_exists(db_session):
    org = ensure_default_organization(db_session)
    assert org.name == "Default"
    assert org.slug == "default"
    count = db_session.execute(select(func.count()).select_from(Organization)).scalar_one()
    assert count == 1


def test_ensure_default_organization_returns_the_existing_org(db_session):
    first = ensure_default_organization(db_session)
    second = ensure_default_organization(db_session)
    assert second.id == first.id
    count = db_session.execute(select(func.count()).select_from(Organization)).scalar_one()
    assert count == 1


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Acme Corp.") == "acme-corp"
    assert slugify("  Hello   World  ") == "hello-world"


def test_slugify_never_returns_empty():
    assert slugify("") == "org"
    assert slugify("!!!") == "org"


def test_slugify_truncates_to_max_length_and_strips_the_hyphen():
    # 47 letters plus a hyphen at position 47: the cut lands on the hyphen.
    assert slugify("x" * 47 + "-tail") == "x" * 47
    assert slugify("a" * 60) == "a" * 48


def test_unique_slug_returns_the_base_when_free(db_session):
    assert unique_slug(db_session, "Acme") == "acme"


def test_unique_slug_appends_a_numeric_suffix_on_collision(db_session):
    _make_org(db_session, slug="acme")
    assert unique_slug(db_session, "Acme") == "acme-2"
    _make_org(db_session, name="Other", slug="acme-2")
    assert unique_slug(db_session, "Acme") == "acme-3"


def test_unique_slug_excludes_the_given_id(db_session):
    # Renaming an org to its own current name must not collide with itself.
    org = _make_org(db_session, slug="acme")
    assert unique_slug(db_session, "Acme", exclude_id=org.id) == "acme"


