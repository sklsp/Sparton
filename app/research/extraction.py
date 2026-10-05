"""Structured-first extraction for public e-commerce pages.

Ported from Ares `app/intelligence/extraction.py` (unchanged behavior).
Page text is treated as untrusted data — returned as data only, never fed
into system instructions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin


@dataclass(slots=True)
class ExtractedProduct:
    name: str = ""
    brand: str = ""
    category: str = ""
    price: float | None = None
    currency: str = ""
    availability: str = "unknown"
    description: str = ""
    image_url: str = ""
    url: str = ""
    review_count: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    method: str = "html"
    confidence: float = 0.0


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self.text_parts: list[str] = []
        self._tag = ""
        self._href = ""
        self._capture_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        self._tag = tag.lower()
        if self._tag == "title":
            self._capture_title = True
        if self._tag == "meta":
            key = values.get("property") or values.get("name")
            if key and values.get("content"):
                self.meta[key.lower()] = values["content"]
        if self._tag == "a" and values.get("href"):
            self._href = values["href"]
        if self._tag in {"p", "h1", "h2", "h3", "li"}:
            self.text_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._capture_title = False
        if tag.lower() == "a":
            self._href = ""
        self._tag = ""

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value:
            return
        if self._capture_title:
            self.title += f" {value}"
        if self._href:
            self.links.append((self._href, value))
        if self._tag not in {"script", "style", "noscript"}:
            self.text_parts.append(value)


def _walk_json(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("@type") in {"Product", "http://schema.org/Product"}:
            found.append(value)
        for child in value.values():
            found.extend(_walk_json(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_json(child))
    return found


def _parse_json_ld(html: str) -> list[dict[str, Any]]:
    scripts = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html, re.I | re.S,
    )
    products: list[dict[str, Any]] = []
    for raw in scripts:
        try:
            products.extend(_walk_json(json.loads(raw.strip())))
        except (json.JSONDecodeError, TypeError):
            continue
    return products


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"\d+(?:[,.]\d+)?", value.replace(" ", ""))
        if match:
            return float(match.group(0).replace(",", "."))
    return None


def _first(value: Any) -> str:
    if isinstance(value, list):
        return _first(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("url") or "")
    return str(value or "")


def _from_structured(data: dict[str, Any], base_url: str) -> ExtractedProduct:
    offers = data.get("offers") or {}
    brand = data.get("brand") or {}
    availability = str(offers.get("availability") or "unknown").rsplit("/", 1)[-1].lower()
    image = urljoin(base_url, _first(data.get("image")))
    return ExtractedProduct(
        name=_first(data.get("name")), brand=_first(brand), category=_first(data.get("category")),
        price=_number(offers.get("price")), currency=str(offers.get("priceCurrency") or ""),
        availability=availability, description=_first(data.get("description")), image_url=image,
        url=urljoin(base_url, _first(data.get("url"))),
        review_count=int(_number((data.get("aggregateRating") or {}).get("reviewCount")) or 0) or None,
        attributes={"sku": data.get("sku"), "mpn": data.get("mpn")},
        method="json-ld", confidence=0.95,
    )


def extract_page(html: str, url: str) -> tuple[list[ExtractedProduct], list[str], dict[str, str]]:
    """Return products, relevant links, and safe page metadata."""
    parser = _PageParser()
    parser.feed(html)
    products = [_from_structured(item, url) for item in _parse_json_ld(html)]
    if not products:
        # OpenGraph fallback, for sites with no JSON-LD.
        #
        # The signal must be a PRICE, not the word "product": every listing
        # page contains the substring "product" in its product URLs, so
        # matching on that turned each competitor's category page into a
        # phantom product named after the site — which then alerted as "new
        # product" on every single crawl.
        title = parser.meta.get("og:title") or parser.title.strip()
        price = _number(parser.meta.get("product:price:amount"))
        looks_like_a_product = (
            parser.meta.get("og:type") == "product"
            or parser.meta.get("product:price:amount")
            or parser.meta.get("product:price:currency")
        )
        if title and price is not None and looks_like_a_product:
            products = [ExtractedProduct(
                name=title,
                description=parser.meta.get("og:description", ""),
                price=price,
                currency=parser.meta.get("product:price:currency", ""),
                image_url=urljoin(url, parser.meta.get("og:image", "")),
                url=url,
                method="opengraph",
                confidence=0.65,
            )]
    links = [
        urljoin(url, href)
        for href, _ in parser.links
        if href and not href.startswith(("javascript:", "mailto:", "#"))
    ]
    return products, list(dict.fromkeys(links)), {
        "title": parser.title.strip(),
        "description": parser.meta.get("description", ""),
    }
