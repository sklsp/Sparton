"""Robots-aware, bounded HTTP crawler for public pages.

Ported from Ares `app/intelligence/crawler.py`. Security boundary: the
crawler is fed URLs discovered from untrusted search results. It refuses
non-HTTP(S) schemes and network-resolved private/loopback/link-local/
reserved addresses unless explicitly allowed for local development,
preventing SSRF through crafted discovery results.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.core.observability.metrics import inc, observe
from app.research.extraction import ExtractedProduct, extract_page

#: A robots.txt is a few kB. 64 kB is generous; anything larger is not a
#: robots file, and is not worth buffering on a hostile origin.
ROBOTS_MAX_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class CrawlPolicy:
    user_agent: str = "SpartonResearch/1.0 (+responsible-crawler)"
    timeout_seconds: float = 12.0
    max_retries: int = 2
    delay_seconds: float = 1.0
    max_pages: int = 25
    max_depth: int = 2
    max_body_bytes: int = 2_000_000
    allow_private_addresses: bool = False
    #: Redirect hops followed manually, re-checking the destination each time.
    max_redirects: int = 5


class SSRFBlockedError(RuntimeError):
    """A URL targeted a disallowed (private or non-HTTP) destination."""


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for a globally routable unicast address.

    `not address.is_global` is a single negation, where the previous explicit
    list was a denylist. That is the safer shape: a denylist has to enumerate
    every non-public range, and the ones it forgets (100.64.0.0/10 carrier NAT,
    192.0.0.0/24 protocol assignments, site-local IPv6, 6to4 and Teredo
    encodings of v4) are exactly the interesting ones. `is_global` is defined
    by Python as "not private", and covers the v4 and v6 special registries
    together.

    Multicast is checked separately because `is_global` does not exclude it.
    """
    if address.is_multicast:
        return False
    return bool(address.is_global)


def _resolve_public_addresses(hostname: str) -> list[str]:
    """Every address `hostname` currently resolves to, as strings.

    Raises OSError when the name does not resolve, so callers can treat
    "unresolvable" and "resolves to something private" as one decision.
    """
    infos = socket.getaddrinfo(hostname, None)
    return [info[4][0] for info in infos]


def _is_public_http_url(url: str) -> bool:
    """Scheme, host and every resolved address must be public.

    Checking *all* addresses matters: a name with one public and one private
    record is load-balanced onto the private one about half the time.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        addresses = _resolve_public_addresses(hostname)
    except OSError:
        return False
    if not addresses:
        return False
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if not _is_public_address(address):
            return False
    return True


@dataclass(slots=True)
class CrawlResult:
    url: str
    status_code: int | None
    products: list[ExtractedProduct]
    links: list[str]
    metadata: dict[str, str]
    error: str | None = None
    robots_allowed: bool = True
    content_hash: str | None = None


class ResponsibleCrawler:
    def __init__(self, policy: CrawlPolicy | None = None, client: httpx.Client | None = None) -> None:
        self.policy = policy or CrawlPolicy()
        self.client = client or httpx.Client(
            timeout=self.policy.timeout_seconds,
            # Redirects are followed by hand, one hop at a time, so that the
            # public-address check runs on every destination. Letting httpx
            # follow them would validate the first URL and then hand the
            # response to whatever the redirect pointed at — the cheapest SSRF
            # bypass there is, and `http://evil.test` -> `169.254.169.254` is
            # one header away.
            follow_redirects=False,
            headers={"User-Agent": self.policy.user_agent},
        )
        self._robots: dict[str, RobotFileParser] = {}
        self._cache: dict[str, CrawlResult] = {}
        self._last_request: dict[str, float] = {}
        self._lock = Lock()

    @staticmethod
    def canonical(url: str) -> str:
        clean, _ = urldefrag(url.strip())
        parsed = urlparse(clean)
        scheme = parsed.scheme.lower() or "https"
        host = parsed.netloc.lower()
        path = parsed.path.rstrip("/") or "/"
        return f"{scheme}://{host}{path}" + (f"?{parsed.query}" if parsed.query else "")

    def _throttle(self, url: str) -> None:
        """Politeness floor: never hit one host faster than `delay_seconds`."""
        host = urlparse(url).netloc.lower()
        with self._lock:
            wait = self.policy.delay_seconds - (time.monotonic() - self._last_request.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last_request[host] = time.monotonic()

    def _address_allowed(self, url: str) -> bool:
        return self.policy.allow_private_addresses or _is_public_http_url(url)

    def _assert_peer_is_public(self, response: httpx.Response) -> None:
        """Reject a response that actually came from a non-public peer.

        This is what closes DNS rebinding. A name can resolve to a public
        address when we validate it and to 127.0.0.1 microseconds later when
        httpx opens the socket; checking the name twice does not close that
        window, because the name is the thing that changed. No-op when the
        transport exposes no real socket (MockTransport in tests).
        """
        try:
            stream = response.extensions.get("network_stream")
            peer = stream.get_extra_info("server_addr") if stream is not None else None
        except Exception:  # noqa: BLE001 - transport-specific, never fatal
            peer = None
        if not peer:
            return
        host = peer[0] if isinstance(peer, (tuple, list)) else str(peer)
        try:
            address = ipaddress.ip_address(str(host).split("%")[0])
        except ValueError:
            return
        if self.policy.allow_private_addresses:
            return
        if not _is_public_address(address):
            raise SSRFBlockedError(f"Blocked: connected to non-public peer {host}")

    def _get(self, url: str) -> httpx.Response:
        """One GET with no redirect following, so the caller keeps control.

        The address check runs immediately before the connection is made, and
        the connected peer is checked again on the response.
        """
        if not self._address_allowed(url):
            raise SSRFBlockedError(f"Blocked: non-public destination {url}")
        request = self.client.build_request("GET", url)
        response = self.client.send(request, follow_redirects=False, stream=True)
        self._assert_peer_is_public(response)
        return response

    def _stream_with_redirects(
        self, url: str, limit: int
    ) -> "list[tuple[str, httpx.Response]]":
        """Follow redirects by hand, re-checking the address on every hop.

        httpx's own redirect following is what made the original check
        worthless: it validated the first URL, then followed `Location` to
        anything at all. `http://evil.test` -> `169.254.169.254` is one header
        away, and it is how a public competitor URL reaches cloud metadata.
        """
        chain: list[tuple[str, httpx.Response]] = []
        current = url
        for _ in range(self.policy.max_redirects + 1):
            self._throttle(current)
            response = self._get(current)
            chain.append((current, response))
            if response.status_code not in (301, 302, 303, 307, 308):
                return chain
            location = response.headers.get("location")
            # Release the socket before the next hop, so a redirect chain does
            # not hold one connection open per hop.
            response.close()
            if not location:
                return chain
            current = urljoin(current, location)
            if not self._address_allowed(current):
                for _, earlier in chain:
                    earlier.close()
                raise SSRFBlockedError(
                    f"Blocked: redirect to non-public destination {current}"
                )
        raise SSRFBlockedError(f"Blocked: more than {self.policy.max_redirects} redirects")

    def _allowed(self, url: str) -> bool:
        """robots.txt for this origin, fetched under the same SSRF rules.

        The robots file is a second network fetch to a URL we constructed, so
        it gets the same treatment: manual redirects, address checked per hop.
        A blocked or unreachable robots.txt reads as "no rules", which is what
        `RobotFileParser` does with an empty rule set.
        """
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            robots_url = urljoin(origin, "/robots.txt")
            parser = RobotFileParser(robots_url)
            lines: list[str] = []
            chain: list[tuple[str, httpx.Response]] = []
            try:
                chain = self._stream_with_redirects(robots_url, ROBOTS_MAX_BYTES)
                final = chain[-1][1]
                if final.status_code < 400:
                    body = bytearray()
                    for chunk in final.iter_bytes():
                        room = ROBOTS_MAX_BYTES - len(body)
                        if room <= 0:
                            break
                        body.extend(chunk[:room])
                    lines = bytes(body).decode(
                        final.encoding or "utf-8", errors="replace"
                    ).splitlines()
            except (httpx.HTTPError, SSRFBlockedError, UnicodeError):
                lines = []
            finally:
                for _, response in chain:
                    response.close()
            parser.parse(lines)
            self._robots[origin] = parser
        return self._robots[origin].can_fetch(self.policy.user_agent, url)

    def fetch(self, url: str) -> CrawlResult:
        url = self.canonical(url)
        if url in self._cache:
            return self._cache[url]
        if not self._address_allowed(url):
            result = CrawlResult(url, None, [], [], {}, "Blocked: non-public or non-HTTP destination", False)
            self._cache[url] = result
            return result
        if not self._allowed(url):
            inc("crawler_robots_denied_total")
            result = CrawlResult(url, None, [], [], {}, "Blocked by robots.txt", False)
            self._cache[url] = result
            return result
        last_error = "request failed"
        for attempt in range(self.policy.max_retries + 1):
            started = time.monotonic()
            chain: list[tuple[str, httpx.Response]] = []
            try:
                chain = self._stream_with_redirects(url, self.policy.max_body_bytes)
                final_url, response = chain[-1]
                response.raise_for_status()

                # Read up to the cap and stop. `response.content` buffers the
                # whole body first, so `[:max_body_bytes]` truncated *after*
                # downloading: a 2 GB response still cost 2 GB of memory and
                # bandwidth before the slice discarded it.
                body = bytearray()
                for chunk in response.iter_bytes():
                    room = self.policy.max_body_bytes - len(body)
                    if room <= 0:
                        break
                    body.extend(chunk[:room])
                inc("crawler_pages_total", outcome="success")
                observe("crawler_request_duration_seconds", time.monotonic() - started)
                raw = bytes(body)
                html = raw.decode(response.encoding or "utf-8", errors="replace")
                products, links, metadata = extract_page(html, final_url)
                result = CrawlResult(
                    final_url, response.status_code, products, links, metadata,
                    content_hash=hashlib.sha256(raw).hexdigest(),
                )
                self._cache[url] = result
                return result
            except (httpx.HTTPError, UnicodeError, SSRFBlockedError) as exc:
                inc("crawler_pages_total", outcome="failed")
                last_error = str(exc)
                if attempt < self.policy.max_retries:
                    time.sleep(min(2**attempt, 8))
            finally:
                for _, response in chain:
                    response.close()
        result = CrawlResult(url, None, [], [], {}, last_error)
        self._cache[url] = result
        return result

    def crawl(self, start_urls: list[str]) -> list[CrawlResult]:
        queue = [(self.canonical(url), 0) for url in start_urls]
        visited: set[str] = set()
        results: list[CrawlResult] = []
        while queue and len(results) < self.policy.max_pages:
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            result = self.fetch(url)
            results.append(result)
            if result.error or depth >= self.policy.max_depth:
                continue
            origin = urlparse(url).netloc
            for link in result.links:
                if urlparse(link).netloc == origin and self.canonical(link) not in visited:
                    queue.append((self.canonical(link), depth + 1))
        return results

    def close(self) -> None:
        self.client.close()
