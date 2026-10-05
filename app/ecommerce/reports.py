from __future__ import annotations

import logging
from collections import Counter, defaultdict
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.billing.plans import get_plan
from app.core import email as mailer
from app.core.config import settings
from app.core.database.ecommerce_models import (
    ChangeEvent,
    ChangeKind,
    ChangeSeverity,
    Competitor,
    Report,
    ReportStatus,
    Shop,
)
from app.core.database.identity import Organization, User
from app.core.database.models import as_utc, utcnow
from app.core.observability.metrics import inc
from app.core.report_email import render_report_email
from app.ecommerce.changes import money

logger = logging.getLogger(__name__)

#: A weekly report by default. The headline cadence of the product.
WEEK_DAYS = 7

#: The most changes we put in front of the model at once. Beyond this the
#: prompt gets long and the tail is noise; the counts still cover everything.
MAX_CHANGES_IN_PROMPT = 60

#: Low-severity rows are dropped from the *narrative* by default. The first
#: crawl of a new competitor generates one "everything is new" row per product,
#: which is baseline noise, not news.
NARRATIVE_SEVERITIES = (ChangeSeverity.MEDIUM, ChangeSeverity.HIGH)

SYSTEM_PROMPT = """\
You are the analyst behind a competitor-intelligence product for small \
e-commerce sellers. You write the weekly brief.

You will be given FACTS: changes already detected by a diff engine, with exact \
prices, deltas, and a source URL for each.

Rules, in order of importance:
1. Use ONLY numbers present in the FACTS. Never calculate, estimate, or invent \
a figure. If a number is not in the FACTS, do not state it.
2. Do not speculate about why a competitor did something. You observe; you do \
not know their margin, their stock, or their intentions.
3. Name competitors and products exactly as the FACTS spell them.
4. Be brief and concrete. This is read on a phone between packing orders.
5. If the FACTS show no meaningful change, say so in one sentence. Do not \
manufacture urgency.

Write in Markdown with this exact structure:

## What changed
Two to four sentences. The single most important thing that happened.

## Price moves
A Markdown table: Competitor | Product | Was | Now | Change. Omit if none.

## Assortment
A Markdown table: Competitor | Product | What happened. Omit if none.

## What to do next
Two or three specific, actionable suggestions a shop owner could take this \
week. These must follow from the FACTS, not from general e-commerce advice.
"""

#: The report is written in the account's language (Organization.language). The
#: rules above stay in English: they are instructions, not copy. Only the prose
#: changes language; names and numbers are copied from the FACTS untouched.
LANGUAGES = ("en", "nl")
LANGUAGE_RULES = {
    "en": "\nLanguage: write the whole brief in English.\n",
    "nl": (
        "\nLanguage: write the whole brief in Dutch (Nederlands), in the plain words a "
        "Dutch or Flemish shop owner uses. Use these headings exactly: "
        "'## Wat er veranderde', '## Prijsbewegingen', '## Assortiment', "
        "'## Wat je nu kunt doen'. Write the table headers in Dutch. Copy competitor "
        "names, product names and every number exactly as the FACTS spell them; do "
        "not translate product names.\n"
    ),
}


def report_language(language: str | None) -> str:
    return language if language in LANGUAGES else "en"


def system_prompt(language: str = "en") -> str:
    return SYSTEM_PROMPT + LANGUAGE_RULES[report_language(language)]


def period_bounds(days: int = WEEK_DAYS, end: datetime | None = None) -> tuple[datetime, datetime]:
    finish = end or utcnow()
    return finish - timedelta(days=days), finish


def collect_changes(
    db: Session,
    organization_id: int | None,
    start: datetime,
    end: datetime,
    shop_id: int | None = None,
    *,
    severities: Sequence[str] | None = None,
) -> list[ChangeEvent]:
    """Every change in the period, newest first."""
    query = (
        select(ChangeEvent)
        .where(
            ChangeEvent.organization_id == organization_id,
            ChangeEvent.detected_at >= start,
            ChangeEvent.detected_at <= end,
        )
        .order_by(ChangeEvent.detected_at.desc())
    )
    if shop_id is not None:
        query = query.where(ChangeEvent.shop_id == shop_id)
    if severities is not None:
        query = query.where(ChangeEvent.severity.in_(list(severities)))
    return list(db.execute(query).scalars().all())


def build_facts(
    changes: list[ChangeEvent],
    shop: Shop | None = None,
    competitors: list[Competitor] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    """The structured payload. This is the source of truth for the report.

    Built once, stored on the Report row, and given verbatim to the model. The
    UI renders its table from the same dict, so the prose and the table can
    never disagree.
    """
    by_kind: Counter[str] = Counter(c.kind for c in changes)
    by_severity: Counter[str] = Counter(c.severity for c in changes)
    by_competitor: dict[str, Counter[str]] = defaultdict(Counter)
    for change in changes:
        by_competitor[change.competitor_domain or "unknown"][change.kind] += 1

    price_moves = [
        {
            "competitor": c.competitor_name or c.competitor_domain,
            "product": c.product_name,
            "previous_price": c.previous_price,
            "new_price": c.new_price,
            "delta": c.delta,
            "delta_pct": c.delta_pct,
            "currency": c.currency,
            "evidence_url": c.evidence_url,
        }
        for c in changes
        if c.kind in (ChangeKind.PRICE_INCREASE, ChangeKind.PRICE_DECREASE)
    ]
    # Biggest absolute move first: that is the one worth the seller's attention.
    # Sorted on the Decimal, not on the rendered string, and the key is dropped
    # before the blob is serialised.
    price_moves.sort(key=lambda m: abs(m.get("delta") or Decimal(0)), reverse=True)

    assortment = [
        {
            "competitor": c.competitor_name or c.competitor_domain,
            "product": c.product_name,
            "kind": c.kind,
            "price": c.new_price or c.previous_price,
            "currency": c.currency,
            "evidence_url": c.evidence_url,
        }
        for c in changes
        if c.kind in (ChangeKind.NEW_PRODUCT, ChangeKind.REMOVED_PRODUCT)
    ]

    return {
        "shop": (
            {"id": shop.id, "name": shop.name, "url": shop.url, "category": shop.category}
            if shop
            else None
        ),
        "competitors": [
            {"id": c.id, "name": c.name, "domain": c.domain, "products": c.product_count}
            for c in (competitors or [])
        ],
        "period": {
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
        },
        "total_changes": len(changes),
        "by_kind": dict(by_kind),
        "by_severity": dict(by_severity),
        "by_competitor": {k: dict(v) for k, v in by_competitor.items()},
        "price_moves": price_moves,
        "assortment": assortment,
        "largest_price_move": price_moves[0] if price_moves else None,
    }


# ---------------------------------------------------------------------------
# Deterministic rendering: the fallback that needs no model at all
# ---------------------------------------------------------------------------
_COPY: dict[str, dict[str, Any]] = {
    "en": {
        "title": "# Competitor report: {name}",
        "your_shop": "your shop",
        "what": "## What changed",
        "nothing": (
            "Nothing moved this week. No competitor changed a price, added a "
            "product, or went out of stock on anything we track."
        ),
        "next": "## What to do next",
        "no_action": (
            "No action needed. If you have just added competitors, the first "
            "crawl builds a baseline and the next one is when changes appear."
        ),
        "largest": "**{who}** moved **{what}** from {was} to {now} ({pct}).",
        "total": "{total} change(s) detected across {n} competitor(s): {counts}.",
        "moves": "## Price moves",
        "moves_head": "| Competitor | Product | Was | Now | Change | Evidence |",
        "view": "view",
        "more_moves": "_...and {n} more price moves._",
        "assortment": "## Assortment",
        "assortment_head": "| Competitor | Product | What happened | Evidence |",
        "more_assortment": "_...and {n} more assortment changes._",
        "kinds": {
            ChangeKind.PRICE_DECREASE: "price cut",
            ChangeKind.PRICE_INCREASE: "price rise",
            ChangeKind.NEW_PRODUCT: "new listing",
            ChangeKind.REMOVED_PRODUCT: "delisted",
            ChangeKind.OUT_OF_STOCK: "out of stock",
            ChangeKind.BACK_IN_STOCK: "back in stock",
        },
    },
    "nl": {
        "title": "# Concurrentierapport: {name}",
        "your_shop": "je winkel",
        "what": "## Wat er veranderde",
        "nothing": (
            "Deze week bewoog er niets. Geen concurrent veranderde een prijs, voegde "
            "een product toe of raakte iets uitverkocht van wat we volgen."
        ),
        "next": "## Wat je nu kunt doen",
        "no_action": (
            "Niets te doen. Heb je net concurrenten toegevoegd, dan legt de eerste "
            "meting een basis en zie je veranderingen vanaf de volgende."
        ),
        "largest": "**{who}** bracht **{what}** van {was} naar {now} ({pct}).",
        "total": "{total} verandering(en) bij {n} concurrent(en): {counts}.",
        "moves": "## Prijsbewegingen",
        "moves_head": "| Concurrent | Product | Was | Nu | Verschil | Bewijs |",
        "view": "bekijk",
        "more_moves": "_...en nog {n} prijsbewegingen._",
        "assortment": "## Assortiment",
        "assortment_head": "| Concurrent | Product | Wat er gebeurde | Bewijs |",
        "more_assortment": "_...en nog {n} assortimentswijzigingen._",
        "kinds": {
            ChangeKind.PRICE_DECREASE: "prijsverlaging",
            ChangeKind.PRICE_INCREASE: "prijsverhoging",
            ChangeKind.NEW_PRODUCT: "nieuw product",
            ChangeKind.REMOVED_PRODUCT: "uit het assortiment",
            ChangeKind.OUT_OF_STOCK: "uitverkocht",
            ChangeKind.BACK_IN_STOCK: "weer op voorraad",
        },
    },
}


def render_facts(facts: dict[str, Any], language: str = "en") -> str:
    """A complete, correct report built from the facts with no LLM involved.

    This is not a degraded path bolted on for when the API is down: it is the
    guarantee that a customer always gets a usable report, in their language.
    The AI only ever *improves the prose* on top of this.
    """
    c = _COPY[report_language(language)]
    kinds = c["kinds"]
    shop = facts.get("shop") or {}
    name = shop.get("name") or c["your_shop"]
    total = facts.get("total_changes", 0)
    by_kind: dict[str, int] = facts.get("by_kind") or {}
    moves = facts.get("price_moves") or []
    assortment = facts.get("assortment") or []

    lines = [c["title"].format(name=name), ""]

    if total == 0:
        lines += [c["what"], "", c["nothing"], "", c["next"], "", c["no_action"]]
        return "\n".join(lines)

    # --- narrative ------------------------------------------------------
    largest = facts.get("largest_price_move")
    counts = ", ".join(
        f"{kinds.get(k, k)}: {v}" for k, v in sorted(by_kind.items()) if v
    )
    lines += [c["what"], ""]
    if largest:
        currency = largest.get("currency", "EUR")
        lines.append(c["largest"].format(
            who=largest["competitor"],
            what=largest["product"],
            was=money(largest.get("previous_price"), currency),
            now=money(largest.get("new_price"), currency),
            pct=f"{largest.get('delta_pct') or 0:+.0f}%",
        ))
    lines.append("")
    lines.append(c["total"].format(
        total=total, n=len(facts.get("by_competitor") or {}), counts=counts
    ))

    # --- price table ----------------------------------------------------
    if moves:
        lines += ["", c["moves"], "", c["moves_head"], "|---|---|---|---|---|---|"]
        for move in moves[:30]:
            currency = move.get("currency", "EUR")
            was = money(move.get("previous_price"), currency)
            now = money(move.get("new_price"), currency)
            pct = move.get("delta_pct")
            change = f"{pct:+.0f}%" if pct is not None else "-"
            link = move.get("evidence_url") or ""
            evidence = f"[{c['view']}]({link})" if link else "-"
            lines.append(
                f"| {move.get('competitor', '-')} | {move.get('product', '-')} "
                f"| {was} | {now} | {change} | {evidence} |"
            )
        if len(moves) > 30:
            lines.append("\n" + c["more_moves"].format(n=len(moves) - 30))

    # --- assortment table ------------------------------------------------
    if assortment:
        lines += ["", c["assortment"], "", c["assortment_head"], "|---|---|---|---|"]
        for item in assortment[:30]:
            link = item.get("evidence_url") or ""
            evidence = f"[{c['view']}]({link})" if link else "-"
            lines.append(
                f"| {item.get('competitor', '-')} | {item.get('product', '-')} "
                f"| {kinds.get(item.get('kind'), item.get('kind'))} | {evidence} |"
            )
        if len(assortment) > 30:
            lines.append("\n" + c["more_assortment"].format(n=len(assortment) - 30))

    return "\n".join(lines)


def build_prompt(facts: dict[str, Any], language: str = "en") -> str:
    """The user message. Facts are embedded verbatim; the model may not add."""
    import json

    trimmed = dict(facts)
    trimmed["price_moves"] = facts.get("price_moves", [])[:MAX_CHANGES_IN_PROMPT]
    trimmed["assortment"] = facts.get("assortment", [])[:MAX_CHANGES_IN_PROMPT]
    return (
        "FACTS for the week. These were computed by the diff engine; every "
        "number you use must come from here.\n\n"
        f"```json\n{json.dumps(trimmed, indent=2, default=str)}\n```\n\n"
        "Write the weekly brief in Markdown, in "
        f"{'Dutch' if report_language(language) == 'nl' else 'English'}, following "
        "the structure you were given. If `price_moves` and `assortment` are both "
        "empty, say so in one sentence under the first heading and stop."
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def generate_report(
    db: Session,
    *,
    organization_id: int | None,
    shop: Shop | None = None,
    competitors: list[Competitor] | None = None,
    days: int = WEEK_DAYS,
    kind: str = "weekly",
    use_llm: bool = True,
    language: str | None = None,
) -> Report:
    """Build (and persist) a report for the last `days`.

    The report row is written and committed *before* the LLM is called, so a
    slow or failed model call still leaves the customer with the deterministic
    rendering. On model failure the report completes with the fallback text and
    an `error` explaining why: it is never left in GENERATING forever.

    `language` defaults to the account's (Organization.language). Only the prose
    follows it; the facts, and so every number, are the same in both languages.
    """
    if language is None:
        from app.core.database.identity import Organization

        org = db.get(Organization, organization_id) if organization_id else None
        language = org.language if org else "en"
    language = report_language(language)
    start, end = period_bounds(days)
    all_changes = collect_changes(db, organization_id, start, end, shop.id if shop else None)
    narrative_changes = [
        c for c in all_changes if c.severity in NARRATIVE_SEVERITIES
    ] or all_changes

    facts = build_facts(
        narrative_changes, shop=shop, competitors=competitors, start=start, end=end
    )
    report = Report(
        organization_id=organization_id,
        shop_id=shop.id if shop else None,
        kind=kind,
        status=ReportStatus.GENERATING,
        period_start=start,
        period_end=end,
        title=_report_title(shop, kind, start, end),
        # Start with the deterministic version. If the model works, we replace
        # it; if not, the customer still has a correct report.
        markdown=render_facts(facts, language),
        facts=facts,
        language=language,
        change_ids=[c.id for c in narrative_changes[:200]],
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    if not use_llm:
        report.status = ReportStatus.COMPLETED
        report.completed_at = utcnow()
        db.commit()
        return report

    try:
        from app.llm import LLMError, get_llm_provider, set_usage_context

        provider = get_llm_provider()
        # Attribute the tokens to the customer, not to NULL: this is a billable
        # call and the plan limits depend on it.
        tokens = set_usage_context(organization_id)
        try:
            markdown = provider.complete(
                [
                    {"role": "system", "content": system_prompt(language)},
                    {"role": "user", "content": build_prompt(facts, language)},
                ],
                task="report",
                temperature=0.3,
            )
        finally:
            from app.llm import clear_usage_context

            clear_usage_context(tokens)

        if markdown and markdown.strip():
            report.markdown = markdown.strip()
        report.model = getattr(provider, "model", "") or ""
        report.status = ReportStatus.COMPLETED
        report.completed_at = utcnow()
        report.error = None
        inc("reports_total", outcome="completed")
    except Exception as exc:  # noqa: BLE001 — the deterministic report stands on its own
        logger.warning("Report generation fell back to deterministic text: %s", exc)
        report.status = ReportStatus.COMPLETED
        report.error = f"AI narrative unavailable: {exc}"[:500]
        report.completed_at = utcnow()
        inc("reports_total", outcome="fallback")

    db.commit()
    db.refresh(report)
    return report


def _digest_changes(db: Session, report: Report) -> list[dict]:
    """The report's change rows in the shape ``render_report_email`` expects."""
    ids = [int(i) for i in (report.change_ids or [])]
    if not ids:
        return []
    events = db.execute(
        select(ChangeEvent).where(ChangeEvent.id.in_(ids))
    ).scalars().all()
    changes = []
    for event in events:
        change = {
            "kind": event.kind,
            "competitor": event.competitor_name or event.competitor_domain,
            "product": event.product_name,
            "evidence_url": event.source_url,
        }
        if event.previous_price is not None:
            change["previous_price"] = float(event.previous_price)
        if event.new_price is not None:
            change["new_price"] = float(event.new_price)
        if event.delta_pct is not None:
            change["delta_pct"] = float(event.delta_pct)
        changes.append(change)
    return changes


def send_weekly_digest(db: Session, report: Report) -> bool:
    """Send the weekly email digest for one finished report (D-032).

    Returns ``True`` when a mail was sent. Every other outcome -- ineligible
    plan, no verified admin, preference off, already sent -- returns ``False``
    without raising, and any transport error is swallowed: this runs inside the
    job that produced the report, and a mail failure must never fail that job.

    Idempotency is an atomic claim on ``Report.email_digest_sent``: two workers
    racing on the same report both pass the read check, but only one UPDATE can
    flip the flag from false to true, so the mail goes out exactly once.
    """
    try:
        if report.status != ReportStatus.COMPLETED or not (report.markdown or "").strip():
            return False

        org = db.get(Organization, report.organization_id) if report.organization_id else None
        plan = get_plan(org.plan if org is not None else "free")
        if not bool(plan.features.get("email_digest")):
            logger.info("Weekly digest skipped for shop %s: plan %r has no email digest",
                        report.shop_id, org.plan if org else "?")
            return False

        # The roles are admin | manager | analyst | viewer; the oldest verified
        # admin is the account that owns the subscription.
        recipient = db.execute(
            select(User).where(
                User.organization_id == report.organization_id,
                User.role == "admin",
                User.is_active.is_(True),
                User.email_verified.is_(True),
            ).order_by(User.created_at.asc(), User.id.asc()).limit(1)
        ).scalars().first()
        if recipient is None:
            logger.info("Weekly digest skipped for shop %s: no verified admin", report.shop_id)
            return False
        if not bool(getattr(recipient, "weekly_digest_enabled", True)):
            logger.info("Weekly digest skipped for %s: preference off", recipient.email)
            return False

        claimed = db.execute(
            update(Report)
            .where(Report.id == report.id, Report.email_digest_sent.is_(False))
            .values(email_digest_sent=True)
        ).rowcount
        if not claimed:
            logger.info("Weekly digest skipped for shop %s: already sent", report.shop_id)
            return False
        report.email_digest_sent = True

        lang = report_language(report.language)  # the email matches the report it carries
        subject, text, html = render_report_email(
            {
                "period_start": report.period_start.isoformat(),
                "period_end": report.period_end.isoformat(),
                "facts": report.facts if isinstance(report.facts, dict) else {},
            },
            _digest_changes(db, report),
            lang=lang,
            report_url=f"{settings.app_url.rstrip('/')}/app/#/reports" if settings.app_url else "",
        )
        mailer.send_rich(recipient.email, subject, text, html)
        db.commit()
        logger.info("Weekly digest sent for shop %s report %s to %s (%s)",
                    report.shop_id, report.id, recipient.email, lang)
        return True
    except Exception:  # noqa: BLE001 -- best-effort by contract (D-032)
        db.rollback()
        logger.exception("Weekly digest failed for shop %s report %s",
                         report.shop_id, getattr(report, "id", "?"))
        return False


def _report_title(shop: Shop | None, kind: str, start: datetime, end: datetime) -> str:
    label = (shop.name if shop else "All shops") or "All shops"
    return f"{label}: {kind} report, {start:%d %b} to {end:%d %b %Y}"


def report_to_dict(report: Report, include_markdown: bool = True) -> dict[str, Any]:
    """API shape. The UI renders `facts` as the table and `markdown` as prose."""
    payload: dict[str, Any] = {
        "id": report.id,
        "shop_id": report.shop_id,
        "kind": report.kind,
        "status": report.status,
        "title": report.title,
        "period_start": report.period_start.isoformat() if report.period_start else None,
        "period_end": report.period_end.isoformat() if report.period_end else None,
        "facts": report.facts or {},
        "change_ids": report.change_ids or [],
        "model": report.model,
        "language": report.language or "en",
        "error": report.error,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "completed_at": report.completed_at.isoformat() if report.completed_at else None,
    }
    if include_markdown:
        payload["markdown"] = report.markdown or ""
    return payload


__all__ = [
    "NARRATIVE_SEVERITIES",
    "SYSTEM_PROMPT",
    "WEEK_DAYS",
    "build_facts",
    "build_prompt",
    "report_language",
    "system_prompt",
    "collect_changes",
    "generate_report",
    "period_bounds",
    "render_facts",
    "report_to_dict",
    "send_weekly_digest",
]
