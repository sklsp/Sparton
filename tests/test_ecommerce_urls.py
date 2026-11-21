"""Unit tests for shop URL handling in app/ecommerce/urls.py.

Covers normalising (scheme, www, trailing slash, query strings, uppercase
hosts), refused URLs (private and loopback addresses, non-http schemes,
missing host) and product URL detection. DNS is stubbed so no test touches
the network: the SSRF guard resolves names before it decides. IP literals
need no stub because getaddrinfo parses them locally without a lookup.
"""

from __future__ import annotations

import socket

import pytest

from app.core.config import settings
from app.core.database.ecommerce_models import Platform
from app.ecommerce.urls import (
    MAX_URL_LENGTH,
    InvalidShopUrl,
    base_url,
    detect_platform,
    normalize_url,
    product_feed_urls,
    robots_url_for,
)


def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every hostname at one public address so the SSRF guard passes.

    The guard resolves DNS and refuses private targets; without this stub the
    fake hosts used here would either hit a real resolver or be refused before
    the behaviour under test ever ran.
    """
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )


def resolves_to(monkeypatch: pytest.MonkeyPatch, address: str | None) -> None:
    """Point every hostname at one fixed address, or nowhere when address is None."""

    if address is None:
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    else:
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))],
        )


class TestNormalising:
    def test_a_bare_domain_gets_an_https_scheme(self, monkeypatch: pytest.MonkeyPatch):
        public_dns(monkeypatch)
        result = normalize_url("acme.example.com")
        assert result.url == "https://acme.example.com/"
        assert result.domain == "acme.example.com"
        assert result.platform == Platform.UNKNOWN

    def test_an_existing_http_scheme_is_kept(self, monkeypatch: pytest.MonkeyPatch):
        public_dns(monkeypatch)
        assert normalize_url("http://acme.example.com/").url == "http://acme.example.com/"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("HTTP://ACME.Example.COM/", "http://acme.example.com/"),
            ("HTTPS://Acme.Example.Com/shop", "https://acme.example.com/shop"),
        ],
    )
    def test_uppercase_scheme_and_host_are_lowercased(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
    ):
        public_dns(monkeypatch)
        result = normalize_url(raw)
        assert result.url == expected
        assert result.domain == "acme.example.com"

    def test_trailing_slashes_on_the_path_are_stripped(self, monkeypatch: pytest.MonkeyPatch):
        public_dns(monkeypatch)
        result = normalize_url("https://acme.example.com/collections/all/")
        assert result.url == "https://acme.example.com/collections/all"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://acme.example.com", "https://acme.example.com/"),
            ("https://acme.example.com///", "https://acme.example.com/"),
        ],
    )
    def test_a_missing_or_empty_path_becomes_the_root(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
    ):
        public_dns(monkeypatch)
        assert normalize_url(raw).url == expected

    def test_the_query_string_is_kept(self, monkeypatch: pytest.MonkeyPatch):
        public_dns(monkeypatch)
        result = normalize_url("https://acme.example.com/collections/all?page=2&sort=price-asc")
        assert result.url == "https://acme.example.com/collections/all?page=2&sort=price-asc"

    def test_the_fragment_is_dropped(self, monkeypatch: pytest.MonkeyPatch):
        public_dns(monkeypatch)
        assert normalize_url("https://acme.example.com/page#section").url == "https://acme.example.com/page"

    def test_surrounding_whitespace_is_stripped(self, monkeypatch: pytest.MonkeyPatch):
        public_dns(monkeypatch)
        assert normalize_url("  https://acme.example.com/shop  ").url == "https://acme.example.com/shop"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http://acme.example.com:80/x", "http://acme.example.com/x"),
            ("https://acme.example.com:443/", "https://acme.example.com/"),
        ],
    )
    def test_default_ports_are_dropped(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str):
        public_dns(monkeypatch)
        assert normalize_url(raw).url == expected

    def test_a_non_default_port_stays_in_the_url_but_not_the_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        public_dns(monkeypatch)
        result = normalize_url("http://acme.example.com:8080/shop")
        assert result.url == "http://acme.example.com:8080/shop"
        assert result.domain == "acme.example.com"

    def test_a_www_host_is_kept_as_given(self, monkeypatch: pytest.MonkeyPatch):
        """The module does not strip www; the host as typed is the identity key."""
        public_dns(monkeypatch)
        result = normalize_url("https://www.acme.example.com/")
        assert result.url == "https://www.acme.example.com/"
        assert result.domain == "www.acme.example.com"

    def test_the_platform_is_detected_from_the_canonical_url(self, monkeypatch: pytest.MonkeyPatch):
        public_dns(monkeypatch)
        assert normalize_url("acme.myshopify.com").platform == Platform.SHOPIFY
        assert normalize_url("https://plain.example.org/collections/all").platform == Platform.SHOPIFY

    def test_to_dict_returns_the_three_stored_parts(self, monkeypatch: pytest.MonkeyPatch):
        public_dns(monkeypatch)
        result = normalize_url("acme.myshopify.com")
        assert result.to_dict() == {
            "url": "https://acme.myshopify.com/",
            "domain": "acme.myshopify.com",
            "platform": Platform.SHOPIFY,
        }


class TestRefusedUrls:
    def test_none_is_refused(self):
        with pytest.raises(InvalidShopUrl, match="A shop URL is required"):
            normalize_url(None)  # type: ignore[arg-type]  # intentional: verifying None is refused

    @pytest.mark.parametrize("raw", ["", "   ", chr(9)])
    def test_empty_input_is_refused(self, raw: str):
        with pytest.raises(InvalidShopUrl, match="A shop URL is required"):
            normalize_url(raw)

    def test_a_url_longer_than_the_limit_is_refused(self):
        raw = f"https://acme.example.com/{'x' * 476}"  # 501 characters
        with pytest.raises(InvalidShopUrl, match="URL is too long"):
            normalize_url(raw)

    def test_a_url_of_exactly_the_limit_is_accepted(self, monkeypatch: pytest.MonkeyPatch):
        public_dns(monkeypatch)
        raw = f"https://acme.example.com/{'x' * 475}"  # exactly MAX_URL_LENGTH characters
        assert len(raw) == MAX_URL_LENGTH
        result = normalize_url(raw)
        assert result.url.endswith("x" * 475)

    @pytest.mark.parametrize(
        "raw",
        ["ftp://acme.example.com/", "gopher://acme.example.com/", "wss://acme.example.com/"],
    )
    def test_non_http_schemes_are_refused(self, raw: str):
        with pytest.raises(InvalidShopUrl, match="Only http and https URLs are supported"):
            normalize_url(raw)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # No "://" here, so an https scheme is prepended first; the host that
            # remains is not a domain name.
            ("javascript:alert(1)", "'javascript' is not a valid domain name"),
            ("https://under_score.com/", "'under_score.com' is not a valid domain name"),
            ("localhost", "'localhost' is not a valid domain name"),
            ("http://[::1]/", "'::1' is not a valid domain name"),
            ("https://-bad-.com/", "'-bad-.com' is not a valid domain name"),
            ("https://example.com./", "'example.com.' is not a valid domain name"),
        ],
    )
    def test_hosts_that_are_not_valid_domain_names_are_refused(self, raw: str, expected: str):
        with pytest.raises(InvalidShopUrl, match=expected):
            normalize_url(raw)

    @pytest.mark.parametrize("raw", ["http://", "https:///path"])
    def test_a_url_without_a_hostname_is_refused(self, raw: str):
        with pytest.raises(InvalidShopUrl, match="That URL has no hostname"):
            normalize_url(raw)

    def test_input_that_urlparse_rejects_is_refused(self):
        # An unterminated IPv6 literal makes urlparse itself raise ValueError.
        with pytest.raises(InvalidShopUrl, match="That does not look like a valid URL"):
            normalize_url("https://[::1")

    @pytest.mark.xfail(strict=True, reason="real bug, see sparton-quality-19/BUGS.md #1")
    def test_an_invalid_port_is_refused_like_other_bad_urls(self):
        """A port that is not a number should be refused with InvalidShopUrl;
        today the raw ValueError from urllib leaks out of normalize_url."""
        with pytest.raises(InvalidShopUrl):
            normalize_url("http://acme.example.com:abc/")


class TestTheSsrfGuard:
    @pytest.mark.parametrize(
        "raw",
        [
            "http://127.0.0.1/admin",  # loopback
            "http://169.254.169.254/latest/meta-data/",  # link-local, cloud metadata
            "http://10.0.0.1/",  # RFC 1918 private
            "http://192.168.1.1/",  # RFC 1918 private
        ],
    )
    def test_private_and_loopback_ip_literals_are_refused(self, monkeypatch: pytest.MonkeyPatch, raw: str):
        """IP literals need no DNS stub: getaddrinfo parses them locally."""
        monkeypatch.setattr(settings, "crawler_allow_private_addresses", False)
        with pytest.raises(InvalidShopUrl, match="That address is not a public website we can access"):
            normalize_url(raw)

    def test_a_name_that_resolves_to_a_private_address_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "crawler_allow_private_addresses", False)
        resolves_to(monkeypatch, "10.0.0.5")
        with pytest.raises(InvalidShopUrl, match="That address is not a public website we can access"):
            normalize_url("http://internal.example.com/")

    def test_a_name_that_does_not_resolve_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "crawler_allow_private_addresses", False)
        resolves_to(monkeypatch, None)
        with pytest.raises(InvalidShopUrl, match="That address is not a public website we can access"):
            normalize_url("https://gone.example.com/")

    def test_private_addresses_pass_only_when_the_config_allows_them(self, monkeypatch: pytest.MonkeyPatch):
        """The escape hatch exists for local fixtures and is off by default."""
        monkeypatch.setattr(settings, "crawler_allow_private_addresses", True)
        result = normalize_url("http://127.0.0.1:8000/shop")
        assert result.url == "http://127.0.0.1:8000/shop"
        assert result.domain == "127.0.0.1"


class TestDetectPlatform:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://acme.myshopify.com/", Platform.SHOPIFY),
            ("https://x.acme.myshopify.com/products", Platform.SHOPIFY),
            ("https://www.bol.com/nl/nl/c/home", Platform.BOL),
            ("https://shop.bol.nl/", Platform.BOL),
            ("https://store.bigcommerce.com/", Platform.BIGCOMMERCE),
            ("https://my-prestashop.fr/", Platform.PRESTASHOP),
            ("https://shop.magento.com/", Platform.MAGENTO),
        ],
    )
    def test_host_fragments_identify_the_platform(self, url: str, expected: str):
        assert detect_platform(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://acme.example.com/wp-json/wc/store/v1/products", Platform.WOOCOMMERCE),
            (
                "https://acme.example.com/wp-content/plugins/woocommerce/assets/js/x.js",
                Platform.WOOCOMMERCE,
            ),
            ("https://plain.example.org/collections/all", Platform.SHOPIFY),
            ("https://plain.example.org/products.json?limit=250", Platform.SHOPIFY),
        ],
    )
    def test_path_fingerprints_identify_the_platform(self, url: str, expected: str):
        assert detect_platform(url) == expected

    def test_host_hints_win_over_path_hints(self):
        assert detect_platform("https://www.bol.com/collections/all") == Platform.BOL

    def test_a_url_with_no_recognised_hint_is_unknown(self):
        assert detect_platform("https://plain.example.org/products/1") == Platform.UNKNOWN

    def test_an_uppercase_path_still_matches(self):
        assert detect_platform("https://ACME.EXAMPLE.COM/Collections/All") == Platform.SHOPIFY

    def test_the_host_argument_overrides_the_url_and_is_lowercased(self):
        assert detect_platform("https://whatever.example/", host="ACME.MYSHOPIFY.COM") == Platform.SHOPIFY

    def test_a_url_without_a_hostname_still_checks_path_hints(self):
        assert detect_platform("https:///collections/all") == Platform.SHOPIFY


class TestBaseUrlAndFeeds:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://shop.example/collections/all?x=1", "https://shop.example"),
            ("http://acme.example.com:8080/deep/path?q=1#frag", "http://acme.example.com:8080"),
            ("HTTPS://SHOP.Example/Path", "https://shop.example"),
        ],
    )
    def test_base_url_keeps_only_scheme_host_and_port(self, url: str, expected: str):
        assert base_url(url) == expected

    def test_shopify_gets_the_products_json_feed(self):
        urls = product_feed_urls("https://acme.myshopify.com/collections/all", Platform.SHOPIFY)
        assert urls == ["https://acme.myshopify.com/products.json?limit=250&page=1"]

    def test_woocommerce_gets_the_store_api_feed(self):
        urls = product_feed_urls("https://acme.example.com/wp-json/x", Platform.WOOCOMMERCE)
        assert urls == ["https://acme.example.com/wp-json/wc/store/v1/products?per_page=100&page=1"]

    @pytest.mark.parametrize("platform", [Platform.UNKNOWN, Platform.BOL, Platform.MAGENTO])
    def test_other_platforms_have_no_structured_feed(self, platform: str):
        assert product_feed_urls("https://plain.example.org/", platform) == []

    def test_robots_url_for_appends_robots_txt_to_the_base(self):
        assert robots_url_for("https://shop.example/collections/all?x=1") == "https://shop.example/robots.txt"
