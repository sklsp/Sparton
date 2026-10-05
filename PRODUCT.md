# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Owners of small webshops in the Netherlands and Belgium (1 to 10 people), mostly on Shopify
or WooCommerce, who set their own prices next to everything else they do: buying, content,
customer service. They check competitors by hand today, irregularly, and miss price moves and
sales until customers mention them. A representative shop: The Modern Man (themodernman.nl),
a men's lifestyle store on Shopify. (Confirmed by the owner, 2026-09-30.)

## Product Purpose

Sparton watches a shop's competitors so the owner doesn't have to. The owner adds their shop
URL; Sparton suggests competitors, crawls them on a schedule, detects price changes, sales,
stock-outs and new or removed products, and writes a weekly report with an evidence link for
every claim. Success: the owner reacts to a competitor's price move within days, not weeks,
and trusts the numbers enough to act on them without double-checking.

## Positioning

Exact data, not guesses. Where a competitor publishes a Shopify or WooCommerce feed or
schema.org product data, Sparton reads the shop's own numbers (to the cent, including sale
"was" prices and stock) and says so; only where neither exists does it extract from the page,
and it labels that as extracted. The diff engine computes every number; the AI only writes the
summary and never invents a price. Every change links to the page it came from.

## Operating Context

- Owners check the dashboard in short sessions between other work, often on a phone; the weekly
  report is the main ritual.
- Plans: Free (1 shop, 3 competitors, weekly crawl), Pro €29/month (3 shops, 15 competitors,
  daily), Business €79/month (10 shops, 50 competitors, twice daily). Prices come from the API
  (`app/billing/plans.py`), never hardcoded in the page.
- Signup, email verification, password reset, Stripe Checkout and Customer Portal exist.

## Capabilities and Constraints

- Frontend: static ES-module SPA in `app/web/` served by FastAPI. No bundler, no node_modules,
  no build step; this constraint is deliberate and stays. Third-party libraries must load as
  plain ES modules (vendored or from an allowed CDN).
- Surfaces: public landing page (`landing.html`) with pricing and FAQ; legal pages; the customer
  dashboard (`index.html` + `views/`): shops, competitors, changes/alerts, reports, billing,
  settings. Experimental domains (documents, generation, datasets, training, agent) are
  feature-flagged off and out of scope.
- Data source labels per competitor: feed / jsonld = exact; html = extracted from the page.
- Languages: the product and the landing page must work in **Dutch and English** with a
  language switch (confirmed 2026-09-30). All user-facing copy must be translatable.
- Must pass the existing browser smoke tests (landing shows prices, "Start free" reaches
  signup, product loop, tenant isolation); update them with the redesign, don't delete them.

## Brand Commitments

- The name **Sparton** stays. Everything visual is replaceable, including the Greek-myth module
  names (Athena, Hector, Ares…), which should not appear in the customer-facing UI.
  (Confirmed 2026-09-30.)

## Evidence on Hand

- No customers, testimonials, logos, case studies or usage numbers exist yet. Do not invent any.
- Real, demonstrable facts: exact-feed reading for Shopify/WooCommerce, evidence URL per change,
  the plan table, the security properties in README.md.
- Demo data: `scripts/seed_demo.py`. A real test setup: themodernman.nl with competitors
  themodernman.co.uk (Shopify), modernmankit.com, modernman.com.

## Product Principles

1. **Proof over claims.** Every number shows where it came from; unverifiable claims don't ship.
2. **Calm over alarm.** The owner is busy; surface what changed and why it matters, not noise.
3. **Honest about certainty.** "Exact" only when it is; extracted data is labelled as such.
4. **Small-shop scale.** Built for one person with ten minutes, not an analyst team.

## Accessibility & Inclusion

Dutch and English. Usable on a phone. Standard WCAG 2.2 AA; reduced-motion respected.
