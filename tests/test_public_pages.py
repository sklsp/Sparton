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


class TestEmailLinks:
    """The verification and reset emails link to /verify-email and /reset-password. Those paths
    must reach the dashboard's flows, not 404, and must not drop the token on the way."""

    @pytest.mark.parametrize("path, view", [("/verify-email", "verify"), ("/reset-password", "reset")])
    def test_the_email_link_lands_in_the_dashboard_flow(self, client, path, view):
        response = client.get(f"{path}?token=abc-123_XYZ", follow_redirects=False)
        assert response.status_code in (302, 303, 307)
        assert response.headers["location"] == f"/app/#/{view}?token=abc-123_XYZ"

    def test_a_hostile_token_cannot_break_out_of_the_fragment(self, client):
        response = client.get("/reset-password?token=a%26next%3Dhttps%3A%2F%2Fevil.example", follow_redirects=False)
        location = response.headers["location"]
        assert location.startswith("/app/#/reset?token=")
        assert "&" not in location and "://" not in location


class TestComparisonPage:
    def test_vs_prisync_is_served(self, client):
        response = client.get("/vs/prisync")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_every_prisync_claim_links_to_its_source(self, client):
        """A comparison page is only as honest as its sources: each Prisync cell cites one."""
        import re

        body = client.get("/vs/prisync").text
        rows = re.findall(r"<tr>(.*?)</tr>", body, flags=re.S)[1:]
        assert rows, "the comparison table is empty"
        for row in rows:
            prisync_cell = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)[-1]
            assert "prisync.com" in prisync_cell or "vs-unknown" in row, prisync_cell


class TestPublicTextPages:
    @pytest.mark.parametrize("path", ["/pricing", "/faq"])
    def test_page_is_served(self, client, path):
        response = client.get(path)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    @pytest.mark.parametrize("page", ["privacy", "terms", "dpa"])
    def test_legal_drafts_say_they_are_drafts(self, client, page):
        body = client.get(f"/legal/{page}").text
        assert "DRAFT — needs legal review" in body
        assert "CONCEPT — moet juridisch worden nagekeken" in body


class TestErrorPages:
    def test_a_browser_gets_the_designed_404(self, client):
        response = client.get("/no-such-page", headers={"Accept": "text/html,application/xhtml+xml"})
        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]
        assert "isn't on the board" in response.text

    def test_an_api_client_still_gets_json(self, client):
        response = client.get("/no-such-page", headers={"Accept": "application/json"})
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}

    def test_fetch_from_the_dashboard_keeps_json(self, client):
        response = client.get("/shops/999999", headers={"Accept": "*/*"})
        assert response.headers["content-type"].startswith("application/json")


class TestSearchPages:
    PAGES = ["/nl/prisync-alternatief", "/nl/concurrentieprijzen-shopify",
             "/nl/concurrentieprijzen-woocommerce-lightspeed"]

    @pytest.mark.parametrize("path", PAGES)
    def test_dutch_page_is_served_with_its_meta(self, client, path):
        body = client.get(path).text
        assert 'lang="nl"' in body
        assert '<meta name="description"' in body and 'property="og:title"' in body
        assert "<h1>" in body and "/app/#signup" in body

    def test_the_sitemap_lists_every_public_page(self, client):
        response = client.get("/sitemap.xml")
        assert response.status_code == 200
        assert "application/xml" in response.headers["content-type"]
        for path in ["/pricing", "/faq", "/vs/prisync", *self.PAGES]:
            assert f"{path}</loc>" in response.text

    def test_robots_points_at_the_sitemap_and_keeps_the_app_out(self, client):
        body = client.get("/robots.txt").text
        assert "Sitemap:" in body and "Disallow: /app/" in body
