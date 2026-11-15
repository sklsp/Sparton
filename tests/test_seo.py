"""The site address in public pages: %APP_URL% is filled in at serve time.

Public pages are static files and cannot know the site's own address, so they
carry a %APP_URL% placeholder that app/main.py replaces with settings.app_url
when serving. These tests pin the server side of that contract: replaced where
present, byte for byte where absent, error pages included.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

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


class TestCanonicalMatchesSitemap:
    """The sitemap and the canonical links can never drift apart again."""

    def test_every_sitemap_page_has_exactly_one_canonical_and_og_url(self, client):
        base = settings.app_url.rstrip("/")
        locs = re.findall(r"<loc>(.*?)</loc>", client.get("/sitemap.xml").text)
        assert len(locs) == 7, f"the sitemap lists {len(locs)} pages, expected 7: {locs}"
        for url in locs:
            path = url[len(base):]
            body = client.get(path).text
            canonicals = re.findall(r'<link rel="canonical" href="(.*?)"', body)
            assert canonicals == [url], f"{path}: canonical {canonicals} != sitemap {url}"
            og_urls = re.findall(r'<meta property="og:url" content="(.*?)"', body)
            assert og_urls == [url], f"{path}: og:url {og_urls} != sitemap {url}"

    def test_the_404_page_stays_noindex_without_a_canonical(self, client):
        response = client.get("/no-such-page", headers={"Accept": "text/html"})
        assert response.status_code == 404
        assert '<meta name="robots" content="noindex">' in response.text
        assert 'rel="canonical"' not in response.text


class TestSocialCardImage:
    """Link previews can only load an image from the site's full address."""

    PAGES = ["/", "/pricing", "/faq", "/vs/prisync",
             "/nl/prisync-alternatief", "/nl/concurrentieprijzen-shopify",
             "/nl/concurrentieprijzen-woocommerce-lightspeed"]

    def test_every_page_with_an_og_image_has_an_absolute_one(self, client):
        base = settings.app_url.rstrip("/")
        for path in self.PAGES:
            body = client.get(path).text
            images = re.findall(r'<meta property="og:image" content="(.*?)"', body)
            assert len(images) == 1, f"{path}: og:image tags {images}"
            image = images[0]
            assert image.startswith(base), f"{path}: og:image is not absolute: {image}"

    def test_the_error_pages_do_the_same(self, client):
        base = settings.app_url.rstrip("/")
        body = client.get("/no-such-page", headers={"Accept": "text/html"}).text
        images = re.findall(r'<meta property="og:image" content="(.*?)"', body)
        assert len(images) == 1, f"404 page og:image tags {images}"
        assert images[0].startswith(base), f"404 page og:image is not absolute: {images[0]}"

    def test_the_og_image_path_is_served(self, client):
        base = settings.app_url.rstrip("/")
        body = client.get("/").text
        image = re.findall(r'<meta property="og:image" content="(.*?)"', body)[0]
        path = image[len(base):]
        response = client.get(path)
        assert response.status_code == 200, f"{path} does not serve the social card image"


class TestPageStructure:
    """Every public page: one h1, a title, a description, lang and alt on every img."""

    PAGES = ["/", "/pricing", "/faq", "/vs/prisync",
             "/nl/prisync-alternatief", "/nl/concurrentieprijzen-shopify",
             "/nl/concurrentieprijzen-woocommerce-lightspeed",
             "/legal/privacy", "/legal/terms", "/legal/dpa"]

    class _Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.html_lang: str | None = None
            self.title_parts: list[str] = []
            self.in_title = False
            self.description: str | None = None
            self.h1_count = 0
            self.imgs_without_alt: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            d = dict(attrs)
            if tag == "html":
                self.html_lang = d.get("lang")
            elif tag == "title":
                self.in_title = True
            elif tag == "meta" and (d.get("name") or "").lower() == "description":
                self.description = d.get("content")
            elif tag == "h1":
                self.h1_count += 1
            elif tag == "img" and not (d.get("alt") or "").strip():
                self.imgs_without_alt.append(d.get("src") or "?")

        def handle_endtag(self, tag: str) -> None:
            if tag == "title":
                self.in_title = False

        def handle_data(self, data: str) -> None:
            if self.in_title:
                self.title_parts.append(data)

    def test_every_public_page_passes_the_structure_check(self, client):
        failures: list[str] = []
        for path in self.PAGES:
            parser = TestPageStructure._Parser()
            parser.feed(client.get(path).text)
            parser.close()
            if parser.h1_count != 1:
                failures.append(f"{path}: {parser.h1_count} h1 tags")
            if not "".join(parser.title_parts).strip():
                failures.append(f"{path}: missing or empty <title>")
            if not (parser.description or "").strip():
                failures.append(f"{path}: missing or empty meta description")
            if not parser.html_lang:
                failures.append(f"{path}: <html> has no lang attribute")
            for src in parser.imgs_without_alt:
                failures.append(f"{path}: img {src} has no alt text")
        assert not failures, "pages failing the structure check:\n" + "\n".join(failures)
