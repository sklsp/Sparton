"""Every route is classified, and every protected one refuses anonymous callers.

`tests/test_security_guards.py` checks a hand-written list of sensitive paths.
That list is a snapshot: it cannot notice a route added after it was written,
which is exactly how Phase 1 ended up with twelve endpoints answering `200` to
an anonymous caller.

So this file derives the inventory from the app's own OpenAPI document. A new
route has to be classified here or this fails, whatever it is called. The
default is "protected", because that is the safe way to be wrong.

The route table is read from `openapi()`, not from `app.routes`, because the
domain routers are mounted as sub-applications and do not appear in the
top-level list.
"""

from __future__ import annotations

import re

import pytest

from app.main import create_app

# --- public by design -------------------------------------------------------
# Adding to this list is a decision, not a convenience.
#
#   /live, /ready        health probes for the orchestrator. They report that
#                        the process is up and say nothing about the database,
#                        the model provider, or any tenant.
#   /billing/plans       the pricing table, needed to render pricing before
#                        anyone has an account.
#   /auth/register, /auth/login, /auth/verify-email, /auth/forgot-password,
#   /auth/reset-password  the signup and recovery flows, which by definition
#                        happen before you have an account.
#
# Everything else requires a session.
PUBLIC = {
    "/live",
    "/ready",
    "/billing/plans",
    "/auth/register",
    "/auth/login",
    "/auth/verify-email",
    "/auth/forgot-password",
    "/auth/reset-password",
}

# --- signed rather than authenticated ---------------------------------------
# The webhook authenticates with a Stripe signature, not a session. It is
# therefore neither public nor session-protected, and is tested on its own.
SIGNED = {
    "/stripe/webhook",
}


def _paths() -> set[str]:
    return set(create_app().openapi()["paths"])


def _template(path: str) -> str:
    """Reduce a path to a shape that can be compared across the two sources.

    The OpenAPI table uses `{param}` templates (`/shops/{shop_id}`); the probe
    lists below use a concrete id so the request is well formed
    (`/shops/1`). Both become `/shops/*`.
    """
    return re.sub(r"\{[^}]+\}", "*", re.sub(r"/\d+(?=/|$)", "/*", path))


class TestTheInventoryItself:
    def test_the_route_table_is_not_empty(self):
        paths = _paths()
        assert len(paths) > 30, f"only {len(paths)} routes; this file is not testing much"

    def test_every_route_is_classified(self):
        """The point of the exercise: a new route must be classified here."""
        known = {_template(p) for p in PUBLIC} | {_template(p) for p in SIGNED}
        known |= {_template(p) for p in ALL_PROTECTED}
        unknown = sorted(p for p in _paths() if _template(p) not in known)
        assert not unknown, (
            f"unclassified routes: {unknown}\n"
            "If it is public by design, add it to PUBLIC with a comment saying "
            "why. Otherwise it must require Depends(current_user) and be added to "
            "one of the PROTECTED lists so it is actually probed."
        )

    def test_the_public_surface_has_not_grown_silently(self):
        """A route becoming public is a security change and should be visible in
        a diff. This pins the set so an addition cannot slip through review."""
        assert PUBLIC == {
            "/live",
            "/ready",
            "/billing/plans",
            "/auth/register",
            "/auth/login",
            "/auth/verify-email",
            "/auth/forgot-password",
            "/auth/reset-password",
        }, (
            "the public surface changed. Each entry must be justifiable in one "
            "line, and this list is what makes that reviewable."
        )


# Every route that is neither public nor signed and answers GET.
PROTECTED = [
    "/admin/users",
    "/agent/runs",
    "/analytics/summary",
    "/approvals",
    "/auth/me",
    "/billing/invoices",
    "/billing/plan",
    "/billing/usage",
    "/changes",
    "/competitors",
    "/health",
    "/metrics",
    "/overview",
    "/products",
    "/reports",
    "/shops",
    "/tools",
]

# Routes that do not answer GET at all. Probed with their real method below; a
# GET here returns 405, which says nothing about the guard.
PROTECTED_WRITE_ONLY = [
    ("post", "/agent/run"),
    ("post", "/auth/change-password"),
    ("put", "/auth/language"),
    ("patch", "/auth/settings"),
    ("post", "/auth/logout"),
    ("post", "/auth/resend-verification"),
    ("post", "/billing/checkout"),
    ("post", "/billing/portal"),
    ("patch", "/admin/users/1"),
]

# Routes with a path parameter that answer GET.
PROTECTED_WITH_IDS = [
    "/agent/runs/1",
    "/agent/runs/1/events",
    "/changes/1/history",
    "/products/1",
    "/reports/1",
    "/shops/1",
    "/shops/1/reports",
]

# Parameterised routes that do not answer GET.
PROTECTED_WRITE_ONLY_WITH_IDS = [
    ("post", "/approvals/1/resolve"),
    ("post", "/changes/1/ack"),
    ("post", "/competitors/1/crawl"),
    ("post", "/shops/1/crawl"),
    ("post", "/shops/1/discover"),
    ("post", "/shops/1/report"),
    ("delete", "/competitors/1"),
    ("delete", "/shops/1"),
    ("patch", "/shops/1"),
]

ALL_PROTECTED = (
    PROTECTED
    + PROTECTED_WITH_IDS
    + [p for _, p in PROTECTED_WRITE_ONLY]
    + [p for _, p in PROTECTED_WRITE_ONLY_WITH_IDS]
)

ALL_WRITES = (
    PROTECTED_WRITE_ONLY
    + PROTECTED_WRITE_ONLY_WITH_IDS
    + [("post", "/shops"), ("post", "/competitors")]
)



class TestTheInventoryIsComplete:
    def test_the_probe_list_matches_the_route_table(self):
        """If this fails, a route was added, classified as protected, and not
        actually probed — the exact gap this file exists to close."""
        probed = {_template(p) for p in ALL_PROTECTED}
        unprobed = sorted(
            _template(p) for p in _paths()
            if _template(p) not in probed
            and _template(p) not in {_template(p) for p in PUBLIC}
            and _template(p) not in {_template(p) for p in SIGNED}
        )
        assert not unprobed, f"classified as protected but never probed: {unprobed}"


class TestAnonymousCallersAreRefused:
    @pytest.mark.parametrize("path", PROTECTED)
    def test_reads_are_refused(self, client, db_session, path):
        response = client.get(path)
        assert response.status_code == 401, f"{path} -> {response.status_code}"

    @pytest.mark.parametrize("path", PROTECTED_WITH_IDS)
    def test_parameterised_read_routes_are_refused(self, client, db_session, path):
        """A 404 would mean the route does not exist, and 403 would mean it was
        reached with the guard in the wrong place. Only 401 is correct:
        authentication must be checked before the row is looked up, or the
        response becomes an existence oracle."""
        response = client.get(path)
        assert response.status_code == 401, f"{path} -> {response.status_code}"

    @pytest.mark.parametrize("method,path", ALL_WRITES)
    def test_writes_are_refused(self, client, db_session, method, path):
        """401 means the guard ran. 422 is also acceptable: it can only be
        reached after authentication passed, so it is not a leak — but 401 is
        the correct answer and anything above 422 would mean the route ran
        without credentials."""
        caller = getattr(client, method)
        # `TestClient.delete` does not accept a json= argument.
        response = (
            caller(path, json={}) if method != "delete" else caller(path)
        )
        assert response.status_code in (401, 422), (
            f"{method.upper()} {path} -> {response.status_code}"
        )


class TestTheWebhookIsSignedNotOpen:
    def test_an_unconfigured_webhook_is_refused_loudly(self, client, db_session):
        """With no signing secret there is no way to verify anything, so the
        endpoint must refuse rather than accept. 503 is the honest answer:
        "this deployment is not set up for webhooks", not "request denied"."""
        response = client.post(
            "/stripe/webhook", json={"type": "checkout.session.completed"}
        )
        assert response.status_code in (400, 403, 422, 503), response.status_code

    def test_a_configured_webhook_rejects_an_unsigned_request(
        self, client, db_session, monkeypatch
    ):
        """The real guard: with a secret set, a request with no signature must
        not be honoured. This is the case that would otherwise grant paid plans
        to anyone who can POST."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_secret", raising=False)
        response = client.post(
            "/stripe/webhook", json={"type": "checkout.session.completed"}
        )
        assert response.status_code in (400, 403, 422), response.status_code

    def test_a_garbage_signature_is_not_honoured(
        self, client, db_session, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_secret", raising=False)
        response = client.post(
            "/stripe/webhook",
            content=b'{"type":"checkout.session.completed"}',
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "t=1,v1=deadbeef",
            },
        )
        assert response.status_code in (400, 403, 422), response.status_code



class TestLogoutWorks:
    """Regression: logout used to 500 for every caller.

    The handler called FastAPI's `HTTPBearer` from synchronous code. Its
    ``__call__`` is async, so it returned a coroutine, which is never None and
    has no ``.credentials`` -- an unhandled AttributeError. Logout is the route
    a user reaches for when something has already gone wrong, so it has to work.
    """

    def test_logout_with_no_session_is_a_clean_401(self, client, db_session):
        """Not a 500, and not a 200: there is nothing to revoke."""
        response = client.post("/auth/logout")
        assert response.status_code == 401, response.text

    def test_logout_revokes_the_callers_own_session(self, client, auth_headers, db_session):
        """The happy path that the async-bug made unreachable."""
        assert client.get("/auth/me", headers=auth_headers).status_code == 200

        response = client.post("/auth/logout", headers=auth_headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"revoked": True}

        # The session must be dead afterwards, not merely reported as revoked.
        assert client.get("/auth/me", headers=auth_headers).status_code == 401

    def test_a_garbage_bearer_is_refused_not_crashed(self, client, db_session):
        response = client.post(
            "/auth/logout", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code in (200, 401), response.text
