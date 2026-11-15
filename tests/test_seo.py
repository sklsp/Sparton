"""The site address in public pages: %APP_URL% is filled in at serve time.

Public pages are static files and cannot know the site's own address, so they
carry a %APP_URL% placeholder that app/main.py replaces with settings.app_url
when serving. These tests pin the server side of that contract: replaced where
present, byte for byte where absent, error pages included.
"""

from __future__ import annotations

import pytest

from app.core.config import settings


class TestAppUrlAtServeTime:
    @pytest.fixture()
    def public_dir(self, tmp_path, monkeypatch):
        """Point the public page routes at temp files we control."""
        (tmp_path / "landing.html").write_text(
            "<html><head><link rel=\"canonical\" href=\"%APP_URL%/\"></head>"
            "<body>hello</body></html>",
            encoding="utf-8",
        )
        (tmp_path / "faq.html").write_bytes(b"<html><body>no placeholder here</body></html>")
        (tmp_path / "404.html").write_text(
            "<html><head><link rel=\"canonical\" href=\"%APP_URL%/missing\"></head>"
            "<body>not found</body></html>",
            encoding="utf-8",
        )
        monkeypatch.setattr("app.main.WEB_DIR", tmp_path)
        monkeypatch.setattr(settings, "app_url", "https://shop.example")
        return tmp_path

    def test_a_page_with_the_placeholder_is_served_replaced(self, client, public_dir):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert 'href="https://shop.example/"' in response.text
        assert "%APP_URL%" not in response.text

    def test_a_file_without_the_placeholder_is_served_byte_for_byte(self, client, public_dir):
        response = client.get("/faq")
        assert response.status_code == 200
        assert response.content == b"<html><body>no placeholder here</body></html>"

    def test_the_404_page_still_answers_404(self, client, public_dir):
        response = client.get("/no-such-page", headers={"Accept": "text/html"})
        assert response.status_code == 404
        assert "%APP_URL%" not in response.text
        assert 'href="https://shop.example/missing"' in response.text
