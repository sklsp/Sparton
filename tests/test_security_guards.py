"""Security regression tests for the Phase 1 audit findings.

Every test corresponds to a numbered item in docs/AUDIT.md sections 3 and 5.
These are behavioural (call the API, assert the status) rather than
structural, so they keep holding if a route is later moved.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.database.domain_models import Product
from app.core.database.identity import User

#: Every one of these answered 200 with no credentials before the fix. The
#: list is the audit's "endpoints with no auth dependency" finding, verbatim.
UNGUARDED_AT_AUDIT_TIME = [
    "/rag/status",
    "/rag/debug-query?q=secret",
    "/comfyui/status",
    "/comfyui/workflows",
    "/training/status",
    "/training/presets",
    "/training/hardware",
    "/metrics",
    "/tools",
    "/products",
    "/intelligence/stores",
    "/intelligence/opportunities",
    "/admin/users",
    "/agent/runs",
    "/documents",
    "/analytics/summary",
    "/approvals",
    "/prompts",
    "/conversations/1/history",
    "/health",
]


@pytest.mark.parametrize("path", UNGUARDED_AT_AUDIT_TIME)
def test_route_requires_authentication(client, path):
    """Audit P0-B: these routes had no `current_user` dependency at all."""
    response = client.get(path)
    assert response.status_code == 401, (
        f"{path} answered {response.status_code} anonymously; expected 401. "
        f"Body: {response.text[:200]}"
    )


def test_no_anonymous_cross_tenant_fallback(client, db_session):
    """Audit P0-A: an unset API_KEY used to grant a cross-tenant admin.

    `MachineUser.organization_id is None`, and every query path reads that as
    "sees all tenants" — so the old `if not settings.api_key: return anon`
    branch turned a forgotten env var into a public read/write window.
    """
    from app.core.config import settings

    assert not settings.api_key, (
        "This test proves the no-API_KEY path is closed; if API_KEY is set in "
        "the test environment the assertion is meaningless."
    )
    assert client.get("/products").status_code == 401
    assert client.get("/intelligence/stores").status_code == 401
    assert client.get("/admin/users").status_code == 401


def test_api_key_principal_still_works_when_configured(client, monkeypatch):
    """The machine principal is still available — it is just explicit now."""
    from app.core.config import settings
    from app.core.security import rate_limit

    monkeypatch.setattr(settings, "api_key", "unit-test-machine-key", raising=False)
    rate_limit.reset_limits()
    try:
        ok = client.get("/products", headers={"X-API-Key": "unit-test-machine-key"})
        assert ok.status_code == 200
        bad = client.get("/products", headers={"X-API-Key": "wrong"})
        assert bad.status_code == 401
    finally:
        rate_limit.reset_limits()
        monkeypatch.undo()


def test_two_tenants_cannot_see_each_others_products(client, db_session):
    """Cross-tenant read isolation on the one endpoint that had a real check."""
    a = client.post(
        "/auth/register",
        json={"email": "a@example.com", "password": "long-password-1",
              "organization_name": "Shop A"},
    ).json()["token"]
    b = client.post(
        "/auth/register",
        json={"email": "b@example.com", "password": "long-password-1",
              "organization_name": "Shop B"},
    ).json()["token"]

    org_a = db_session.execute(
        select(User).where(User.email == "a@example.com")
    ).scalars().first().organization_id
    db_session.add(
        Product(organization_id=org_a, sku="SECRET-1", title="A's secret product",
                description="", price=1.0, category="x")
    )
    db_session.commit()

    mine = client.get("/products", headers={"Authorization": f"Bearer {a}"}).json()
    theirs = client.get("/products", headers={"Authorization": f"Bearer {b}"}).json()
    assert mine["count"] == 1
    assert theirs["count"] == 0


def test_duplicate_organization_name_is_allowed(client):
    """Audit P2-8 / D-009: names must not be globally unique.

    Two unrelated shops may both be called "Acme"; squatting the name must not
    be a denial-of-service against everyone else.
    """
    first = client.post(
        "/auth/register",
        json={"email": "one@example.com", "password": "long-password-1",
              "organization_name": "Acme"},
    )
    second = client.post(
        "/auth/register",
        json={"email": "two@example.com", "password": "long-password-1",
              "organization_name": "Acme"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["user"]["organization_id"] != second.json()["user"]["organization_id"]


def test_duplicate_email_still_rejected(client):
    client.post(
        "/auth/register",
        json={"email": "dupe@example.com", "password": "long-password-1",
              "organization_name": "One"},
    )
    again = client.post(
        "/auth/register",
        json={"email": "dupe@example.com", "password": "long-password-1",
              "organization_name": "Two"},
    )
    assert again.status_code == 409


def test_register_is_rate_limited(client):
    """Audit P2-7 / D-027: public signup must not be unlimited."""
    from app.core.security import rate_limit

    rate_limit.reset_limits()
    try:
        created = 0
        for i in range(25):
            response = client.post(
                "/auth/register",
                json={"email": f"rl{i}@example.com", "password": "long-password-1",
                      "organization_name": f"Org {i}"},
            )
            if response.status_code == 201:
                created += 1
            elif response.status_code == 429:
                break
        assert created < 25, "register was never rate limited"
    finally:
        rate_limit.reset_limits()


def test_liveness_probes_stay_open(client):
    """Orchestrators probe these without credentials; they must not 401."""
    assert client.get("/live").status_code == 200
    assert client.get("/ready").status_code == 200


def test_global_prompt_templates_are_visible_to_tenants(client, db_session):
    """Audit P2-3: `organization_id IN (org, NULL)` never matches NULL."""
    from app.core.database.creation_models import PromptTemplate

    db_session.add(
        PromptTemplate(organization_id=None, key="global_tpl", name="Global",
                       template="Hi {input}", description="shipped with the app")
    )
    db_session.commit()

    token = client.post(
        "/auth/register",
        json={"email": "tpl@example.com", "password": "long-password-1",
              "organization_name": "Tpl Org"},
    ).json()["token"]
    listed = client.get("/prompts", headers={"Authorization": f"Bearer {token}"})
    assert any(p["key"] == "global_tpl" for p in listed.json()["prompts"])



def test_machine_user_email_is_not_shared_class_state():
    """Audit P2-6: `anon.email = ...` mutated a class-level singleton.

    One request's identity leaked into every other request in the process.
    """
    from app.core.auth.api import MachineUser

    first = MachineUser("a@example.test")
    second = MachineUser("b@example.test")
    assert first.email == "a@example.test"
    assert second.email == "b@example.test"
    assert MachineUser.email == "machine@sparton.local", (
        "the class default was mutated by an instance assignment"
    )
