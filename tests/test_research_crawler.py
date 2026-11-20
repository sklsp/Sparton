"""Behavioural tests for the responsible crawler itself.

``test_ssrf_and_proxy.py`` proves the security boundary (SSRF, page body caps,
proxy trust); this file covers the rest of ``app/research/crawler.py`` --
robots.txt handling and its size cap, retries and backoff, redirect edge cases,
politeness pacing and caching -- through the public surface (``fetch``,
``crawl``) with a MockTransport, so no test ever touches the network or really
sleeps.

Pacing is asserted through the crawler's existing sleep seam: the module-level
``time.sleep`` calls in ``_throttle`` and the retry loop. Tests patch it with a
recording stand-in, which both keeps the suite fast and proves the waits are
actually requested (a test that mocked the clock entirely would not notice the
throttle being removed).
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

import httpx
import pytest

from app.research import crawler as crawler_mod
from app.research.crawler import CrawlPolicy, ResponsibleCrawler, SSRFBlockedError


class RecordingSleep:
    """A fake ``sleep`` that records what it was asked to wait for.

    The point of the exercise: a test that really slept would take seconds per
    request, and a test that mocked the clock entirely would not notice the
    throttle being removed. Recording the requested waits tests the real
    behaviour in milliseconds.
    """

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def resolved_to(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    """Point the crawler's resolver at a fixed name-to-address table."""

    def resolve(host: str) -> list[str]:
        return [mapping[host]] if host in mapping else []

    monkeypatch.setattr(crawler_mod, "_resolve_public_addresses", resolve)


def make_crawler(
    handler,
    *,
    delay_seconds: float = 0.0,
    max_retries: int = 0,
    allow_private_addresses: bool = False,
    max_redirects: int = 5,
    max_depth: int = 2,
) -> ResponsibleCrawler:
    """A crawler pointed at a MockTransport; nothing here can reach the network."""

    def transport(request: httpx.Request) -> httpx.Response:
        return handler(str(request.url), request)

    return ResponsibleCrawler(
        policy=CrawlPolicy(
            user_agent="sparton-test",
            delay_seconds=delay_seconds,
            max_retries=max_retries,
            allow_private_addresses=allow_private_addresses,
            max_redirects=max_redirects,
            max_depth=max_depth,
        ),
        client=httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False),
    )


def handler_200(url: str, request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html></html>", request=request)


class TestTheUrlGate:
    """``_is_public_http_url`` refuses anything that cannot be proven public."""

    def test_a_url_with_no_host_is_refused(self):
        assert crawler_mod._is_public_http_url("https:///no-host") is False

    def test_a_name_that_resolves_to_nothing_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {})
        assert crawler_mod._is_public_http_url("https://empty.test/x") is False

    def test_a_resolution_result_that_is_not_an_address_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(crawler_mod, "_resolve_public_addresses", lambda host: ["not-an-ip"])
        assert crawler_mod._is_public_http_url("https://junk.test/x") is False

    def test_a_public_ip_literal_host_uses_the_real_resolver(self):
        """No resolver patch here on purpose: getaddrinfo parses an IP literal
        locally, so the real ``_resolve_public_addresses`` runs without a network."""
        assert crawler_mod._is_public_http_url("https://93.184.216.34/x") is True

    def test_a_private_ip_literal_is_refused_by_the_real_resolver(self):
        """An IP literal needs no DNS at all, so it cannot hide behind a name."""
        assert crawler_mod._is_public_http_url("http://10.0.0.5/latest/meta-data/") is False


class TestThePeerCheck:
    """``_assert_peer_is_public`` re-checks the address a connection actually made."""

    class Stream:
        def __init__(self, peer) -> None:
            self.peer = peer

        def get_extra_info(self, name: str):
            # The crawler asks for "server_addr" (crawler._assert_peer_is_public);
            # a fake that answers any other key makes the whole check a no-op.
            return self.peer if name == "server_addr" else None

    class BrokenStream:
        def get_extra_info(self, name: str):
            raise RuntimeError("no sockets in this test")

    @staticmethod
    def _response_with_stream(stream) -> httpx.Response:
        response = httpx.Response(200, request=httpx.Request("GET", "https://peer.test/"))
        response.extensions = {"network_stream": stream}
        return response

    def test_a_stream_that_raises_on_probe_is_ignored(self):
        crawler = make_crawler(handler_200)
        try:
            crawler._assert_peer_is_public(self._response_with_stream(self.BrokenStream()))
        finally:
            crawler.close()

    def test_a_peer_address_that_is_not_an_ip_is_left_alone(self):
        crawler = make_crawler(handler_200)
        try:
            crawler._assert_peer_is_public(self._response_with_stream(self.Stream(("not-an-ip", 443))))
        finally:
            crawler.close()

    def test_private_peers_pass_when_local_development_allows_them(self):
        crawler = make_crawler(handler_200, allow_private_addresses=True)
        try:
            crawler._assert_peer_is_public(self._response_with_stream(self.Stream(("127.0.0.1", 443))))
        finally:
            crawler.close()

    def test_a_private_peer_is_rejected_when_local_development_does_not_allow_it(self):
        """The DNS rebinding case: the name validated public, the socket is private."""
        crawler = make_crawler(handler_200)
        try:
            with pytest.raises(SSRFBlockedError):
                crawler._assert_peer_is_public(self._response_with_stream(self.Stream(("192.168.1.10", 443))))
        finally:
            crawler.close()

    def test_a_public_peer_passes_the_check_silently(self):
        """A genuinely public peer must still pass, or the guard is useless."""
        crawler = make_crawler(handler_200)
        try:
            crawler._assert_peer_is_public(self._response_with_stream(self.Stream(("93.184.216.34", 443))))
        finally:
            crawler.close()

    def test_get_refuses_a_non_public_destination_before_connecting(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"169.254.169.254": "169.254.169.254"})
        calls: list[str] = []

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            calls.append(url)
            return httpx.Response(200, text="nope", request=request)

        crawler = make_crawler(handler)
        with pytest.raises(SSRFBlockedError):
            crawler._get("http://169.254.169.254/latest/meta-data/")
        assert calls == []


class TestThePolitenessDelay:
    """Requests to one host are spaced by ``delay_seconds``; hosts keep their own clock."""

    def test_the_second_request_to_a_host_waits_for_the_interval(self, monkeypatch: pytest.MonkeyPatch):
        sleep = RecordingSleep()
        monkeypatch.setattr(crawler_mod.time, "sleep", sleep)
        crawler = make_crawler(handler_200, delay_seconds=5.0)
        try:
            crawler._throttle("https://a.test/one")  # first hit on the host: no wait
            assert sleep.waits == []
            crawler._throttle("https://a.test/two")  # same host: the full interval
            assert len(sleep.waits) == 1
            assert 4.9 <= sleep.waits[0] < 5.0
            crawler._throttle("https://b.test/one")  # another host is independent
            assert len(sleep.waits) == 1
        finally:
            crawler.close()

    def test_a_fetch_waits_between_robots_and_the_page(self, monkeypatch: pytest.MonkeyPatch):
        """Robots.txt and the page share a host, so the page request is the second hit."""
        resolved_to(monkeypatch, {"polite.test": "93.184.216.34"})
        sleep = RecordingSleep()
        monkeypatch.setattr(crawler_mod.time, "sleep", sleep)

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            return httpx.Response(200, text="<html></html>", request=request)

        crawler = make_crawler(handler, delay_seconds=5.0)
        result = crawler.fetch("https://polite.test/page")
        assert result.error is None
        assert len(sleep.waits) == 1
        assert 4.9 <= sleep.waits[0] < 5.0


class TestRetriesAndBackoff:
    """Failures are retried with exponential backoff through the same sleep seam."""

    def test_connection_failures_back_off_exponentially(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"flaky.test": "93.184.216.34"})
        page_calls = 0
        sleep = RecordingSleep()
        monkeypatch.setattr(crawler_mod.time, "sleep", sleep)

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            nonlocal page_calls
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            page_calls += 1
            raise httpx.ConnectError("boom")

        crawler = make_crawler(handler, max_retries=2)
        result = crawler.fetch("https://flaky.test/page")
        assert result.status_code is None
        assert result.error == "boom"
        assert page_calls == 3
        assert sleep.waits == [1, 2]

    def test_a_timeout_is_retried_the_same_way(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"slow.test": "93.184.216.34"})
        sleep = RecordingSleep()
        monkeypatch.setattr(crawler_mod.time, "sleep", sleep)

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            raise httpx.ReadTimeout("timed out")

        crawler = make_crawler(handler, max_retries=1)
        result = crawler.fetch("https://slow.test/page")
        assert result.status_code is None
        assert result.error == "timed out"
        assert sleep.waits == [1]

    def test_an_error_status_is_retried(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"down.test": "93.184.216.34"})
        sleep = RecordingSleep()
        monkeypatch.setattr(crawler_mod.time, "sleep", sleep)

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            return httpx.Response(500, text="oops", request=request)

        crawler = make_crawler(handler, max_retries=1)
        result = crawler.fetch("https://down.test/page")
        assert result.status_code is None
        assert "500" in (result.error or "")
        assert sleep.waits == [1]

    def test_a_retry_that_recovers_returns_the_page(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"flaky.test": "93.184.216.34"})
        attempts = 0
        sleep = RecordingSleep()
        monkeypatch.setattr(crawler_mod.time, "sleep", sleep)

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("first attempt drops")
            return httpx.Response(200, text="<html></html>", request=request)

        crawler = make_crawler(handler, max_retries=1)
        result = crawler.fetch("https://flaky.test/page")
        assert result.status_code == 200
        assert result.error is None
        assert sleep.waits == [1]


class TestRobotsTxtResponses:
    """Every way robots.txt can answer, and what each one means for a fetch."""

    @staticmethod
    def _handler(robots_body: str | None, robots_status: int = 200):
        calls: list[str] = []

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            calls.append(url)
            if url.endswith("/robots.txt"):
                return httpx.Response(robots_status, text=robots_body or "", request=request)
            return httpx.Response(200, text="<html></html>", request=request)

        return handler, calls

    def test_an_allow_all_robots_file_permits_the_page(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"shop.test": "93.184.216.34"})
        handler, _ = self._handler("User-agent: *\nAllow: /")
        crawler = make_crawler(handler)
        result = crawler.fetch("https://shop.test/products/mug")
        assert result.error is None
        assert result.status_code == 200
        assert result.robots_allowed is True

    def test_a_disallow_rule_blocks_the_page_before_it_is_requested(self, monkeypatch: pytest.MonkeyPatch):
        from app.core.observability.metrics import snapshot

        resolved_to(monkeypatch, {"shop.test": "93.184.216.34"})
        handler, calls = self._handler("User-agent: *\nDisallow: /private")
        before = snapshot()["counters"].get("crawler_robots_denied_total", 0.0)

        crawler = make_crawler(handler)
        result = crawler.fetch("https://shop.test/private/page")
        after = snapshot()["counters"].get("crawler_robots_denied_total", 0.0)

        assert result.error == "Blocked by robots.txt"
        assert result.robots_allowed is False
        assert result.status_code is None
        assert calls.count("https://shop.test/private/page") == 0, "the page was fetched despite Disallow"
        assert after - before == 1.0

    @pytest.mark.parametrize("status", [404, 500])
    def test_an_unreachable_robots_file_reads_as_no_rules(self, monkeypatch: pytest.MonkeyPatch, status: int):
        resolved_to(monkeypatch, {"shop.test": "93.184.216.34"})
        handler, _ = self._handler(None, robots_status=status)
        crawler = make_crawler(handler)
        result = crawler.fetch("https://shop.test/products/mug")
        assert result.error is None
        assert result.status_code == 200

    def test_an_overlong_robots_file_is_capped_while_streaming(self, monkeypatch: pytest.MonkeyPatch):
        """The cap exists so a hostile robots.txt cannot pin memory; the stream must stop early."""
        resolved_to(monkeypatch, {"big.test": "93.184.216.34"})
        sent = 0

        def robots_body():
            nonlocal sent
            for _ in range(20):  # 200 kB of "rules"
                sent += 10_000
                yield b"x" * 10_000

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, content=robots_body(), request=request)
            return httpx.Response(200, text="<html></html>", request=request)

        crawler = make_crawler(handler)
        result = crawler.fetch("https://big.test/page")
        assert result.error is None
        assert sent < 100_000, f"the robots stream was not stopped at the cap: {sent} bytes read"


class TestRedirectEdgeCases:
    def test_a_redirect_without_a_location_header_ends_the_chain(self, monkeypatch: pytest.MonkeyPatch):
        """No Location means no next hop; the 3xx itself is the final response.

        httpx's ``raise_for_status`` treats a non-2xx final status as an error, so
        the fetch reports it -- but the chain must stop after exactly one request.
        """
        resolved_to(monkeypatch, {"loop.test": "93.184.216.34"})
        calls: list[str] = []

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            calls.append(url)
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            return httpx.Response(302, request=request)  # no location header

        crawler = make_crawler(handler)
        result = crawler.fetch("https://loop.test/start")
        assert calls.count("https://loop.test/start") == 1, "the chain kept hopping without a Location"
        assert result.status_code is None
        assert "302" in (result.error or "")

    def test_a_relative_location_is_resolved_against_the_current_url(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"shop.test": "93.184.216.34"})

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            if url == "https://shop.test/start":
                return httpx.Response(301, headers={"location": "/catalog"}, request=request)
            return httpx.Response(200, text="<html></html>", request=request)

        crawler = make_crawler(handler)
        result = crawler.fetch("https://shop.test/start")
        assert result.url == "https://shop.test/catalog"
        assert result.status_code == 200

    def test_a_redirect_loop_stops_at_the_limit(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"loop.test": "93.184.216.34"})

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            return httpx.Response(302, headers={"location": "https://loop.test/start"}, request=request)

        crawler = make_crawler(handler, max_redirects=1)
        result = crawler.fetch("https://loop.test/start")
        assert result.error == "Blocked: more than 1 redirects"


class TestFetchGuardsAndCache:
    def test_fetching_a_non_public_destination_is_blocked_before_any_request(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"private.test": "192.168.1.10"})
        calls: list[str] = []

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            calls.append(url)
            return httpx.Response(200, text="nope", request=request)

        crawler = make_crawler(handler)
        result = crawler.fetch("https://private.test/x")
        assert result.error == "Blocked: non-public or non-HTTP destination"
        assert result.robots_allowed is False
        assert result.status_code is None
        assert calls == [], "a request went out to a private address"

    def test_a_second_fetch_of_the_same_url_comes_from_the_cache(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"cached.test": "93.184.216.34"})
        calls: list[str] = []

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            calls.append(url)
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            return httpx.Response(200, text="<html></html>", request=request)

        crawler = make_crawler(handler)
        first = crawler.fetch("https://cached.test/page")
        second = crawler.fetch("https://cached.test/page")
        assert calls.count("https://cached.test/robots.txt") == 1
        assert calls.count("https://cached.test/page") == 1, "the cached page was fetched again"
        assert second is first


class TestStructuredDataFirst:
    """A page with schema.org JSON-LD yields the shop's own values, not parsed ones."""

    def test_json_ld_products_win_over_html_parsing(self, monkeypatch: pytest.MonkeyPatch):
        from app.ecommerce.feeds import shopfeed_available

        if not shopfeed_available():  # the JSON-LD tier needs the optional shopfeed package (not in CI)
            pytest.skip("shopfeed is not installed, so the crawler falls back to HTML parsing")
        resolved_to(monkeypatch, {"shop.test": "93.184.216.34"})
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@context": "https://schema.org", "@type": "Product", '
            '"name": "Ceramic Mug", "sku": "MUG-1", '
            '"offers": {"@type": "Offer", "price": "9.99", "priceCurrency": "EUR"}}'
            "</script></head>"
            '<body><a href="/next">Next</a></body></html>'
        )

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            return httpx.Response(200, text=html, request=request)

        crawler = make_crawler(handler)
        result = crawler.fetch("https://shop.test/products/mug")
        assert len(result.products) == 1
        product = result.products[0]
        assert product.name == "Ceramic Mug"
        assert product.price == Decimal("9.99")
        assert product.currency == "EUR"
        assert product.attributes["data_source"] == "jsonld"
        # Links still come from the page itself, not the structured data.
        assert result.links == ["https://shop.test/next"]


class TestCrawlVisits:
    def test_crawl_skips_a_url_it_has_already_visited(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"a.test": "93.184.216.34"})
        calls: list[str] = []

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            calls.append(url)
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            return httpx.Response(200, text="<html></html>", request=request)

        crawler = make_crawler(handler)
        results = crawler.crawl(["https://a.test/x", "https://a.test/x"])
        assert len(results) == 1
        assert calls.count("https://a.test/x") == 1

    def test_crawl_follows_same_origin_links(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"site.test": "93.184.216.34"})
        pages = {
            "/start": '<html><body><a href="/one">One</a></body></html>',
            "/one": "<html><body>the end</body></html>",
        }

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            return httpx.Response(200, text=pages.get(urlparse(url).path, ""), request=request)

        crawler = make_crawler(handler)
        results = crawler.crawl(["https://site.test/start"])
        assert [r.url for r in results] == ["https://site.test/start", "https://site.test/one"]

    def test_crawl_stops_expanding_a_page_that_failed(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"site.test": "93.184.216.34"})

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            return httpx.Response(404, text="gone", request=request)

        crawler = make_crawler(handler)
        results = crawler.crawl(["https://site.test/missing"])
        assert len(results) == 1
        assert "404" in (results[0].error or "")

    def test_crawl_respects_the_depth_limit(self, monkeypatch: pytest.MonkeyPatch):
        resolved_to(monkeypatch, {"site.test": "93.184.216.34"})

        def handler(url: str, request: httpx.Request) -> httpx.Response:
            if url.endswith("/robots.txt"):
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            # Every page links one level deeper, so only the depth limit can stop it.
            path = urlparse(url).path
            return httpx.Response(
                200, text=f'<html><body><a href="{path}/more">More</a></body></html>', request=request
            )

        crawler = make_crawler(handler, max_depth=1)
        results = crawler.crawl(["https://site.test/start"])
        assert [r.url for r in results] == ["https://site.test/start", "https://site.test/start/more"]
