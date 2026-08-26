"""Swappable public search/discovery providers.

Ported from Ares `app/intelligence/discovery.py` (unchanged behavior).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from urllib.parse import parse_qs, quote, urlparse

import httpx


class SearchProvider(ABC):
    @abstractmethod
    def search(self, query: str, *, limit: int = 10) -> list[str]:
        """Return public result URLs for a research query."""


class DuckDuckGoProvider(SearchProvider):
    """Credential-free provider for local development and small research jobs."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=15, headers={"User-Agent": "SpartonResearch/1.0"}
        )

    def search(self, query: str, *, limit: int = 10) -> list[str]:
        response = self.client.get(f"https://html.duckduckgo.com/html/?q={quote(query)}")
        response.raise_for_status()
        candidates = re.findall(r"result__a[^>]+href=[\"']([^\"']+)", response.text, re.I)
        urls: list[str] = []
        for candidate in candidates:
            target = _resolve_result(candidate)
            if target is not None:
                urls.append(target)
            if len(urls) >= limit:
                break
        return list(dict.fromkeys(urls))

    def close(self) -> None:
        self.client.close()


def _resolve_result(candidate: str) -> str | None:
    """Turn one DuckDuckGo result href into a usable http(s) URL.

    Results are not direct links: they arrive as protocol-relative redirects
    of the form ``//duckduckgo.com/l/?uddg=<url-encoded target>&rut=...``.
    Taking them at face value yields no scheme (so every hit gets discarded)
    and, once a scheme is added, points the crawler at duckduckgo.com rather
    than the store being researched.
    """
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"

    parsed = urlparse(candidate)
    host = parsed.netloc.lower()
    if (host == "duckduckgo.com" or host.endswith(".duckduckgo.com")) and parsed.path.startswith("/l/"):
        # parse_qs already percent-decodes the value.
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if not target:
            return None
        candidate = target
        parsed = urlparse(candidate)

    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate
    return None


def domains_from_urls(urls: list[str]) -> list[str]:
    return list(dict.fromkeys(urlparse(url).netloc.lower() for url in urls if urlparse(url).netloc))


def _demo() -> None:
    """Self-check for the result-href parsing. Run: python -m app.research.discovery"""
    wrapped = (
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.etsy.com%2Fmarket%2Fmugs&rut=abc"
    )
    assert _resolve_result(wrapped) == "https://www.etsy.com/market/mugs"
    assert _resolve_result("https://shop.example/collections/all") == "https://shop.example/collections/all"
    assert _resolve_result("//shop.example/x") == "https://shop.example/x"
    # Unusable: no target in the redirect, and non-http schemes.
    assert _resolve_result("//duckduckgo.com/l/?rut=abc") is None
    assert _resolve_result("javascript:alert(1)") is None
    assert _resolve_result("/relative/only") is None
    assert domains_from_urls(["https://a.example/x", "https://A.example/y"]) == ["a.example"]
    print("discovery: all checks passed")


if __name__ == "__main__":
    _demo()
