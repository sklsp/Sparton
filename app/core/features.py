"""Feature flags — which domains exist at all.

A flag checked at **router registration**, not at request time. A disabled
domain has no routes whatsoever, rather than routes that return 403. That is
strictly less attack surface: `/comfyui/workflows` POST writes files to the
server and `/rag/debug-query` returns document text, and with the flags off
neither endpoint exists to be probed (docs/DECISIONS.md D-020).

The product domains (`intelligence`, `commerce`, `agent`) are on by default.
Everything else stays in the codebase and is reachable by naming it in
`ENABLED_DOMAINS`.
"""

from __future__ import annotations

from enum import StrEnum

from app.core.config import settings


class Domain(StrEnum):
    """The domains the platform can expose."""

    #: Competitor & pricing intelligence. The product. Always on.
    INTELLIGENCE = "intelligence"
    #: Shops, competitors, changes, reports. The product. Always on.
    COMMERCE = "commerce"
    #: The Athena agent.
    AGENT = "agent"
    #: Ares legacy research pipeline.
    RESEARCH = "research"
    #: Hector: document upload, RAG, chat.
    DOCUMENTS = "documents"
    #: Apollo: ComfyUI generation.
    GENERATION = "generation"
    #: Argo: LoRA datasets.
    DATASETS = "datasets"
    #: Leonidas: LoRA training.
    TRAINING = "training"
    #: Cross-cutting platform surfaces that are always present.
    PLATFORM = "platform"


#: What a production deployment runs by default. Just the product.
DEFAULT_ENABLED = "intelligence,commerce,agent"

#: Domains that can never be turned off, because the product needs them.
ALWAYS_ON = frozenset({Domain.INTELLIGENCE, Domain.COMMERCE, Domain.AGENT, Domain.PLATFORM})


def enabled_domains() -> frozenset[str]:
    """The active set, as plain strings.

    Unknown names are ignored rather than raising: a typo in an env var should
    not take the API down at import time.
    """
    raw = settings.enabled_domains or DEFAULT_ENABLED
    names = {part.strip().lower() for part in raw.split(",") if part.strip()}
    known = {d.value for d in Domain}
    return frozenset((names & known) | {d.value for d in ALWAYS_ON})


def is_enabled(domain: Domain | str) -> bool:
    name = domain.value if isinstance(domain, Domain) else str(domain).lower()
    return name in enabled_domains()


def describe() -> dict[str, object]:
    """For /health and the dashboard's system view."""
    active = enabled_domains()
    return {
        "enabled": sorted(active),
        "disabled": sorted({d.value for d in Domain} - active),
        "configurable": sorted({d.value for d in Domain} - {d.value for d in ALWAYS_ON}),
    }


__all__ = ["ALWAYS_ON", "DEFAULT_ENABLED", "Domain", "describe", "enabled_domains", "is_enabled"]
