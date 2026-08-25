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


class SSRFBlockedError(RuntimeError):
    """A URL targeted a disallowed (private or non-HTTP) destination."""


def _is_public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
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
            follow_redirects=True,
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

    def _allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            robots_url = urljoin(origin, "/robots.txt")
            parser = RobotFileParser(robots_url)
            try:
                response = self.client.get(robots_url)
                if response.status_code < 400:
                    parser.parse(response.text.splitlines())
                else:
                    parser.parse([])
            except httpx.HTTPError:
                parser.parse([])
            self._robots[origin] = parser
        return self._robots[origin].can_fetch(self.policy.user_agent, url)

    def fetch(self, url: str) -> CrawlResult:
        url = self.canonical(url)
        if url in self._cache:
            return self._cache[url]
        if not self.policy.allow_private_addresses and not _is_public_http_url(url):
            result = CrawlResult(url, None, [], [], {}, "Blocked: non-public or non-HTTP destination", False)
            self._cache[url] = result
            return result
        if not self._allowed(url):
            inc("crawler_robots_denied_total")
            result = CrawlResult(url, None, [], [], {}, "Blocked by robots.txt", False)
            self._cache[url] = result
            return result
        host = urlparse(url).netloc.lower()
        with self._lock:
            wait = self.policy.delay_seconds - (time.monotonic() - self._last_request.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last_request[host] = time.monotonic()
        last_error = "request failed"
        for attempt in range(self.policy.max_retries + 1):
            started = time.monotonic()
            try:
                response = self.client.get(url)
                response.raise_for_status()
                inc("crawler_pages_total", outcome="success")
                observe("crawler_request_duration_seconds", time.monotonic() - started)
                body = response.content[: self.policy.max_body_bytes]
                html = body.decode(response.encoding or "utf-8", errors="replace")
                products, links, metadata = extract_page(html, str(response.url))
                result = CrawlResult(
                    str(response.url), response.status_code, products, links, metadata,
                    content_hash=hashlib.sha256(body).hexdigest(),
                )
                self._cache[url] = result
                return result
            except (httpx.HTTPError, UnicodeError) as exc:
                inc("crawler_pages_total", outcome="failed")
                last_error = str(exc)
                if attempt < self.policy.max_retries:
                    time.sleep(min(2**attempt, 8))
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
