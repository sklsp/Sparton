"""Tests for app.research.discovery: DuckDuckGo result parsing and provider behavior."""

from urllib.parse import parse_qs

import httpx
import pytest

from app.research.discovery import (
    DuckDuckGoProvider,
    SearchProvider,
    _demo,
    _resolve_result,
    domains_from_urls,
)


def _page(*hrefs: str) -> str:
    """A minimal fake results page with one result__a anchor per href."""
    anchors = "".join(f'<a class="result__a" href="{h}">title</a>' for h in hrefs)
    return f"<html><body>{anchors}</body></html>"


def _provider(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DuckDuckGoProvider(client=client), client


# --- search -----------------------------------------------------------------------


def test_search_resolves_redirects_and_drops_unusable_links():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=_page(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.etsy.com%2Fmarket%2Fmugs&rut=abc",
            "https://shop.example/collections/all",
            "//duckduckgo.com/l/?rut=abc",  # redirect without a target
            "javascript:alert(1)",
            "/relative/only",
        ))

    provider, client = _provider(handler)
    try:
        urls = provider.search('mugs <10 "handmade"')
    finally:
        client.close()

    assert urls == [
        "https://www.etsy.com/market/mugs",
        "https://shop.example/collections/all",
    ]
    # The query is percent-encoded on the wire and decodes back to the original.
    request = seen[0]
    assert request.url.host == "html.duckduckgo.com"
    assert request.url.path == "/html/"
    assert parse_qs(request.url.query.decode())["q"] == ['mugs <10 "handmade"']


def test_search_stops_at_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_page(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2F1",
            "https://b.example/2",
            "https://c.example/3",
            "https://d.example/4",
        ))

    provider, client = _provider(handler)
    try:
        urls = provider.search("anything", limit=2)
    finally:
        client.close()

    assert urls == ["https://a.example/1", "https://b.example/2"]


def test_search_dedupes_repeated_urls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_page(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fx",
            "https://b.example/y",
            "https://a.example/x",  # same target twice
            "https://c.example/z",
        ))

    provider, client = _provider(handler)
    try:
        urls = provider.search("anything")
    finally:
        client.close()

    assert urls == ["https://a.example/x", "https://b.example/y", "https://c.example/z"]


def test_search_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    provider, client = _provider(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            provider.search("anything")
    finally:
        client.close()


def test_close_closes_the_client_it_created():
    provider = DuckDuckGoProvider()  # no injected client: it builds its own
    assert not provider.client.is_closed
    assert provider.client.headers["User-Agent"] == "SpartonResearch/1.0"

    provider.close()
    assert provider.client.is_closed


def test_search_provider_is_abstract():
    with pytest.raises(TypeError):
        SearchProvider()  # abstract: cannot be instantiated directly


# --- _resolve_result ---------------------------------------------------------------


def test_resolve_result_unwraps_duckduckgo_redirects():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.etsy.com%2Fmarket%2Fmugs&rut=abc"
    assert _resolve_result(wrapped) == "https://www.etsy.com/market/mugs"

    # Subdomain redirect hosts are unwrapped too.
    sub = "//lite.duckduckgo.com/l/?uddg=https%3A%2F%2Fsub.example%2Fa&rut=z"
    assert _resolve_result(sub) == "https://sub.example/a"


def test_resolve_result_passes_through_direct_and_protocol_relative_links():
    assert (
        _resolve_result("https://shop.example/collections/all")
        == "https://shop.example/collections/all"
    )
    assert _resolve_result("//shop.example/x") == "https://shop.example/x"

    # A plain duckduckgo.com page is not a /l/ redirect, so it passes through.
    assert _resolve_result("https://duckduckgo.com/home") == "https://duckduckgo.com/home"


def test_resolve_result_rejects_unusable_candidates():
    assert _resolve_result("//duckduckgo.com/l/?rut=abc") is None  # no uddg target
    assert _resolve_result("//duckduckgo.com/l/?uddg=ftp%3A%2F%2Fx") is None  # non-http target
    assert _resolve_result("javascript:alert(1)") is None
    assert _resolve_result("/relative/only") is None


# --- domains_from_urls ---------------------------------------------------------------


def test_domains_from_urls_lowercases_and_dedupes():
    assert domains_from_urls(["https://a.example/x", "https://A.example/y"]) == ["a.example"]


def test_domains_from_urls_drops_entries_without_a_netloc():
    assert domains_from_urls(["not a url", "mailto:x@y", "https://b.example/"]) == ["b.example"]
    assert domains_from_urls([]) == []


# --- module self-check ---------------------------------------------------------------


def test_demo_self_check_passes():
    # _demo asserts the parsing contract end to end; any regression raises here.
    assert _demo() is None
