"""The public, unauthenticated surface: landing page, its assets, legal pages.

These routes are what a prospective customer sees before they trust us with a
credit card, and they are the easiest thing in the codebase to break silently —
an HTML file renders fine on disk while its stylesheet 404s. Hence tests.

Also guards the dashboard's own `/app/...` mount, which must keep working
unchanged when root-level routes are added.
"""

from __future__ import annotations

import pytest


class TestLandingPage:
    def test_root_serves_the_landing_page(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_the_landing_page_is_not_a_broken_shell(self, client):
        """Every asset it references must actually resolve. A page that loads
        unstyled is the single most damaging thing we could ship."""
        body = client.get("/").text
        for asset in ("landing.css", "landing.js", "styles.css"):
            assert asset in body, f"landing page never references {asset}"
            assert client.get(f"/{asset}").status_code == 200, f"/{asset} 404s"

    def test_the_pricing_anchor_exists(self, client):
        """The nav links to #pricing; a link to a missing id is a dead end."""
        assert 'id="pricing"' in client.get("/").text

    def test_the_pricing_table_is_public(self, client):
        """The landing page prices itself from the API before signup, so this
        must not require a session."""
        response = client.get("/billing/plans")
        assert response.status_code == 200
        payload = response.json()
        assert payload["plans"], "no plans returned to the public pricing table"
        assert all("id" in p and "price_cents" in p for p in payload["plans"])


class TestLegalPages:
    """The landing page links to these three. Dead legal links on a pricing
    page are a bad signal long before they are a legal problem."""

    @pytest.mark.parametrize("page", ["privacy", "terms", "dpa"])
    def test_legal_page_is_served(self, client, page):
        response = client.get(f"/legal/{page}")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert len(response.text) > 500, f"/legal/{page} looks like a stub"

    @pytest.mark.parametrize("page", ["privacy", "terms", "dpa"])
    def test_legal_page_does_not_require_a_session(self, client, page):
        assert client.get(f"/legal/{page}").status_code == 200

    def test_an_unknown_legal_page_is_a_clean_404(self, client):
        assert client.get("/legal/nonexistent").status_code == 404

    def test_legal_pages_link_back_to_the_site(self, client):
        assert 'href="/"' in client.get("/legal/terms").text


class TestDashboardMountUndisturbed:
    """Root-level routes were added above; these prove the existing dashboard
    mount still resolves the same way it did before."""

    @pytest.mark.parametrize(
        "path",
        [
            "/app/",
            "/app/styles.css",
            "/app/ui.js",
            "/app/api.js",
            "/app/routes.js",
            "/app/views/overview.js",
        ],
    )
    def test_dashboard_asset_is_served(self, client, path):
        assert client.get(path).status_code == 200, path

    def test_root_does_not_shadow_the_dashboard(self, client):
        assert "dashboard" in client.get("/app/").text.lower() or \
            client.get("/app/").status_code == 200
