"""The app factory's deployment branches.

create_app() returns a different surface per deployment: which domain routers
are mounted is pinned elsewhere (test_route_coverage.py), but the startup path
and the public-page routes have their own branches no API test reaches --
whether the durable job queue is drained in-process, and what happens when
files under app/web are missing entirely. The experimental documents router
(app.api.knowledge) needs numpy from requirements-experimental.txt and is not
exercised here; it stays off by default anyway (docs/DECISIONS.md D-020).
"""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings


class _FakeEmbeddedWorker:
    """Records the calls the lifespan makes on the in-process worker."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    def start(self) -> None:
        self.log.append("start")

    def stop(self) -> None:
        self.log.append("stop")


def _install_fake_worker(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    """Point `from workers.embedded import EmbeddedWorker` at a recorder."""
    fake = types.ModuleType("workers.embedded")

    def make() -> _FakeEmbeddedWorker:
        return _FakeEmbeddedWorker(log)

    # raising=False: the fake module is fresh, so the attribute does not exist yet.
    monkeypatch.setattr(fake, "EmbeddedWorker", make, raising=False)
    monkeypatch.setitem(sys.modules, "workers.embedded", fake)


class TestEmbeddedWorkerLifespan:
    """A single-process deployment drains the durable job queue in-process."""

    def test_the_worker_starts_and_stops_with_the_app(self, monkeypatch):
        log: list[str] = []
        _install_fake_worker(monkeypatch, log)
        monkeypatch.setattr(settings, "embedded_worker", True)

        from app.main import create_app

        with TestClient(create_app()):
            assert log == ["start"]  # startup has finished by the time we are in here
        assert log == ["start", "stop"]

    def test_no_worker_starts_when_the_flag_is_off(self, monkeypatch):
        log: list[str] = []
        _install_fake_worker(monkeypatch, log)
        monkeypatch.setattr(settings, "embedded_worker", False)

        from app.main import create_app

        with TestClient(create_app()):
            pass
        assert log == []


class TestMissingWebDir:
    """A deployment whose app/web lost files still serves a working API.

    The public routes must degrade to 404s and redirects, not crash the app;
    test_seo.py pins the same routes with the real pages in place.
    """

    @pytest.fixture()
    def bare_client(self, tmp_path, monkeypatch):
        import app.main as main

        # An existing but empty directory: the mount still happens, every file is gone.
        monkeypatch.setattr(main, "WEB_DIR", tmp_path)
        from app.main import create_app

        with TestClient(create_app()) as client:
            yield client

    def test_root_redirects_to_the_dashboard_without_a_landing_page(self, bare_client):
        response = bare_client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/app/"

    @pytest.mark.parametrize("asset", ["landing.css", "landing.js", "styles.css", "ui.js"])
    def test_missing_landing_assets_answer_404(self, bare_client, asset):
        assert bare_client.get(f"/{asset}").status_code == 404

    @pytest.mark.parametrize("page", ["privacy", "terms", "dpa"])
    def test_missing_legal_pages_answer_404(self, bare_client, page):
        assert bare_client.get(f"/legal/{page}").status_code == 404
    @pytest.mark.parametrize(
        "path",
        [
            "/vs/prisync",
            "/pricing",
            "/faq",
            "/nl/prisync-alternatief",
            "/nl/concurrentieprijzen-shopify",
            "/nl/concurrentieprijzen-woocommerce-lightspeed",
        ],
    )
    def test_missing_public_pages_answer_404(self, bare_client, path):
        assert bare_client.get(path).status_code == 404

    def test_a_browser_not_found_without_a_404_page_stays_json(self, bare_client):
        response = bare_client.get("/no-such-page", headers={"Accept": "text/html"})
        assert response.status_code == 404
        assert "application/json" in response.headers["content-type"]

    def test_sitemap_and_robots_are_built_from_the_configured_app_url(
        self, bare_client, monkeypatch
    ):
        monkeypatch.setattr(settings, "app_url", "https://shop.example")
        sitemap = bare_client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert "<loc>https://shop.example/</loc>" in sitemap.text
        robots = bare_client.get("/robots.txt")
        assert robots.status_code == 200
        assert "Sitemap: https://shop.example/sitemap.xml" in robots.text

    def test_email_links_hand_the_token_to_the_dashboard(self, bare_client):
        response = bare_client.get(
            "/verify-email?token=abc%20def", follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/app/#/verify?token=abc%20def"

    def test_favicon_is_a_no_content(self, bare_client):
        assert bare_client.get("/favicon.ico").status_code == 204


class TestLandingAssets:
    """The landing page's assets live in the dashboard directory and are served from it."""

    @pytest.mark.parametrize(
        ("asset", "media"),
        [
            ("landing.css", "text/css"),
            ("landing.js", "text/javascript"),
            ("styles.css", "text/css"),
            ("ui.js", "text/javascript"),
        ],
    )
    def test_landing_assets_are_served(self, client, asset, media):
        response = client.get(f"/{asset}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(media)

    def test_the_dashboard_mount_serves_the_app(self, client):
        response = client.get("/app/")
        assert response.status_code == 200


class TestErrorPages:
    """Browsers get designed error pages; API clients keep their JSON.

    The handlers are registered on every app (app/main.py), so the routes that
    raise live here, in a throwaway app instance per test.
    """

    @pytest.fixture()
    def web_dir(self, tmp_path, monkeypatch):
        import app.main as main

        (tmp_path / "500.html").write_text(
            "<html><body>server on fire</body></html>", encoding="utf-8"
        )
        monkeypatch.setattr(main, "WEB_DIR", tmp_path)

    def _app_with_raising_routes(self):
        from app.main import create_app

        app = create_app()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("kaboom")

        @app.get("/bad-value")
        async def bad_value():
            raise ValueError("bad input")

        return app

    def test_a_server_error_for_a_browser_gets_the_500_page(self, web_dir):
        with TestClient(
            self._app_with_raising_routes(), raise_server_exceptions=False
        ) as client:
            response = client.get("/boom", headers={"Accept": "text/html"})
        assert response.status_code == 500
        assert "server on fire" in response.text

    def test_a_server_error_for_an_api_client_stays_json(self, web_dir):
        with TestClient(
            self._app_with_raising_routes(), raise_server_exceptions=False
        ) as client:
            response = client.get("/boom")
        assert response.status_code == 500
        assert response.json() == {"detail": "Internal Server Error"}

    def test_a_value_error_becomes_a_400(self, web_dir):
        with TestClient(
            self._app_with_raising_routes(), raise_server_exceptions=False
        ) as client:
            response = client.get("/bad-value")
        assert response.status_code == 400
        assert response.json() == {"detail": "bad input"}

