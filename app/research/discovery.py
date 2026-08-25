"""Swappable public search/discovery providers.

Ported from Ares `app/intelligence/discovery.py` (unchanged behavior).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from urllib.parse import quote, urlparse

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
            parsed = urlparse(candidate)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                urls.append(candidate)
            if len(urls) >= limit:
                break
        return list(dict.fromkeys(urls))

    def close(self) -> None:
        self.client.close()


def domains_from_urls(urls: list[str]) -> list[str]:
    return list(dict.fromkeys(urlparse(url).netloc.lower() for url in urls if urlparse(url).netloc))
