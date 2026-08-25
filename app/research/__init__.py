"""Odysseus — research & market intelligence domain."""

from app.research.crawler import CrawlPolicy, ResponsibleCrawler, SSRFBlockedError
from app.research.discovery import DuckDuckGoProvider, SearchProvider
from app.research.extraction import ExtractedProduct, extract_page
from app.research.intelligence import (
    get_opportunity,
    list_opportunities,
    normalize_name,
    run_investigation,
)

__all__ = [
    "CrawlPolicy",
    "DuckDuckGoProvider",
    "ExtractedProduct",
    "ResponsibleCrawler",
    "SSRFBlockedError",
    "SearchProvider",
    "extract_page",
    "get_opportunity",
    "list_opportunities",
    "normalize_name",
    "run_investigation",
]
