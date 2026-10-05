# SPARTON: Decision Log

Decisions taken autonomously while turning the merged codebase into a launched
product. Each entry records **what**, **why**, and **what it costs us**.

Format: `D-nnn`, monotonically increasing, never renumbered.

---

## D-001: Product scope is *Sparton Intelligence* only

**Decision.** The launched product is competitor & pricing intelligence for
small e-commerce sellers (Shopify / WooCommerce / Bol.com), built on the Ares
domain. Documents, generation, datasets and training remain in the codebase but
are gated behind feature flags (D-020) and are not part of the sale.

**Why.** Every hour spent on LoRA training is an hour not spent on the crawl →
diff → report loop that is the only thing a customer pays for. The other domains
also have the worst test coverage and the widest attack surface
(`/comfyui/workflows` POST writes server files; `/training/hardware` leaks the
host GPU list).

**Cost.** `app/documents`, `app/generation`, `app/datasets`, `app/training`,
`app/api/create.py` and `app/api/knowledge.py` are retained but disabled by
default in production. Not deleted, reachable by enabling the flag, and their
routers disappear entirely when the flag is off.

---

## D-002: No new runtime dependencies

**Decision.** No new pip package is added to `requirements.txt` for product
features. Stripe Checkout, the Customer Portal and webhook verification are
implemented directly against Stripe's documented HTTPS + `stripe-signature` HMAC
scheme using `requests` (already a dependency). Email is sent over SMTP using
the stdlib `smtplib`/`email` modules.

**Why.** The brief requires stdlib/existing-dependency solutions, and every new
dependency is a CVE surface on a product about to handle customer data. The
Stripe surface we need is a handful of HTTP calls and one HMAC.

**Cost.** We own ~150 lines of Stripe protocol code. In exchange the Docker image
gets no new packages. Swapping to the official library later is trivial because
the calls sit behind one module.

---

## D-003: SQLite in tests, PostgreSQL in production, one schema source

**Decision.** `Base.metadata` is the single source of schema truth. Alembic
autogenerates real migrations from it and they are committed. `create_all` is
used only in tests and local SQLite dev; production runs `alembic upgrade head`
on deploy.

**Why.** The current `f4d75c8de498` revision is `pass`, so a PostgreSQL deploy
today yields an empty database. The models are correct; only the migration is
missing.

**Cost.** One-time migration authoring, then the usual add → autogenerate →
review → commit loop.

---

## D-004: Anonymous access is removed; machine principals stay but are explicit

**Decision.** The `if not settings.api_key: return anonymous admin` branch in
`current_user` is **deleted**. Unauthenticated requests to protected routes
return 401. A machine principal is created only when a correct `X-API-Key` is
presented, and it is constructed per request (not a mutated class singleton).

**Why.** It is the single worst bug in the codebase: a missing env var silently
converts a private API into a public cross-tenant read/write surface. "Open dev
mode" is not a product requirement, and local dev has a perfectly good login.

**Cost.** Local dev friction: you must register a user. Acceptable.

---

## D-005: Every route declares its own auth

**Decision.** No route may be registered without an explicit principal
dependency. Routes that were open (`/rag/*`, `/comfyui/*`, `/training/*`,
`/metrics`, `/tools`) get `current_user` and a role gate, or move behind a
feature flag that removes the router entirely.

**Why.** The open routes are the ones that leak: `/rag/debug-query` returned
other tenants' document text; `/comfyui/workflows` POST wrote server files.

**Cost.** Slightly more boilerplate per route.
`tests/test_routes_are_guarded.py` enforces it automatically from here on.

---

## D-006: Model routing: cheap for extraction, strong for the agent and report

**Decision.** `settings.llm_model_strong` and `settings.llm_model_cheap` are
separate settings. The weekly report and the agent use *strong*; structured
extraction/classification tasks use *cheap*. Both are OpenRouter model ids,
configurable per deployment.

**Why.** Cost per customer per month is a launch-critical number
(`docs/LAUNCH.md`). Extraction runs on every crawled page; the report runs once
a week. One expensive model for both is the difference between ~€0.60 and ~€4
per customer per month.

**Cost.** Two more settings and a per-task `model=` argument, which the LLM
layer already supports.

---

## D-007: Token usage is a first-class table, not a log line

**Decision.** Every LLM call records an `LLMUsage` row (org, model, provider,
prompt/completion tokens, latency, task label, created_at). Billing limits and
the cost estimate in `docs/LAUNCH.md` read from this table.

**Why.** Plan enforcement ("usage limits tied to the token accounting") is
impossible without per-organization numbers, and retro-fitting them later
requires a backfill nobody can do.
---

## D-009: Organization names are not globally unique

**Decision.** The unique constraint on `organizations.name` is dropped. Signup
derives a unique internal `slug` instead.

**Why.** On a public signup form, a globally unique name means "Acme" can be
squatted by one user to deny it to every other. Self-serve SaaS cannot tell
people to "ask an admin for an invite" because a shop is called Acme and someone
else got there first.

**Cost.** Display names may repeat; the internal id/slug is the real key.

---

## D-010: Email verification is enforced in production only

**Decision.** `EMAIL_VERIFICATION_REQUIRED` defaults to `true` when
`SPARTON_ENV=production`. When on, an unverified user gets 403 from product
routes. Dev and tests are unaffected.

**Why.** GDPR/AVG requires a verified contact address for transactional mail, and
unverified signups are the cheapest spam vector available. Making it
unconditional would break the existing tests and the local dev loop.

**Cost.** A second code path in `current_user`. Covered by tests both ways.

---

## D-011: Password reset and email verification share one token table

**Decision.** A single `auth_tokens` table with a `purpose` discriminator
(`verify_email` | `reset_password`), a SHA-256 hash of the token, and an expiry.
One code path, one expiry sweep, one audit action per purpose.

**Why.** Two near-identical tables is two places to get the hashing wrong.

**Cost.** A discriminator column.

---

## D-012: Plans are enforced server-side at the service boundary

**Decision.** `app/billing/limits.py` exposes `Plan` definitions and
`enforce(db, org, resource, n)` raising 402/409. Every mutating product route
calls it. The frontend reads the same values from `GET /billing/plan` to render
the upgrade prompt, the UI never decides what is allowed.

**Why.** The brief is explicit, and client-side-only limits are the most common
SaaS breach. Also: the existing `TenantContext.scoped` returns *unscoped* queries
for machine principals, so a limit check must be org-explicit.

**Cost.** A check per route. Cheap.

---

## D-013: Free / Pro / Business limits

**Decision.** Free: 1 shop, 3 competitors, 200 AI tokens/day, weekly report.
Pro (€29/mo): 3 shops, 15 competitors, 2000 tokens/day. Business (€79/mo):
10 shops, 50 competitors, 10000 tokens/day. Enforced in `app/billing/limits.py`.

**Why.** Given in the brief. Chosen so Free is a genuine trial for one shop but
cannot be resold as a monitoring service.

**Cost.** A hardcoded table; changing it is a deploy.

---

## D-014: Stripe webhooks are the only source of truth for subscription state

**Decision.** `checkout.session.completed`, `customer.subscription.updated` and
`customer.subscription.deleted` update the org's plan. Verified with a constant
time HMAC over the **raw** request body. Handled idempotently by event id.
`POST /stripe/webhook` is exempt from session auth but **not** from signature
---

## D-016: The crawler keeps its SSRF guard; it is extended, never relaxed

**Decision.** `ResponsibleCrawler` is reused as-is, DNS-resolution SSRF check,
robots.txt gate, per-host delay, body cap. A user-supplied shop URL goes
through the *same* guard before a crawl is enqueued, so nobody can point the
crawler at `169.254.169.254`.

**Why.** The brief says the SSRF guard stays intact, and it is the best code in
the repository.

**Cost.** None. The guard runs on the user-supplied URL as a cheap pre-check.

---

## D-017: Reports are stored Markdown plus the structured facts they were written from

**Decision.** A `reports` table (org, shop, period, markdown, model, tokens,
status). The API returns the markdown *and* the structured change list, so the
UI can render a table as well as the prose.

**Why.** A report a customer cannot re-read is not a product. Storing the
structured changes makes the AI a *writer*, not the source of truth, the
numbers are computed by the diff engine.

**Cost.** One table.

---

## D-018: Alerts are rows, not email, in v1

**Decision.** A `change_events` table. `GET /changes` powers an in-app inbox with
evidence links. Email digest is post-launch.

**Why.** In-app is shippable without a verified SMTP path, and the brief lists
"in-app alerts" explicitly.

**Cost.** Users must visit the dashboard. Accepted for v1.

---

## D-019: The no-build ES-module SPA is kept

**Decision.** `app/web/` stays a zero-dependency ES-module app. Phase 5 adds a
public landing page and rebuilds the dashboard around the product loop. No
bundler, no `node_modules`, no build step in CI or Docker.

**Why.** The brief requires it, and it means the Docker image is just Python
plus static files with no frontend build failure mode. The existing `ui.js`
primitives are good; they need product views, not a framework.

**Cost.** Hand-written DOM. Already the established pattern here.

---

## D-020: Feature flags are env-driven, default off for non-product domains

**Decision.** `ENABLED_DOMAINS` (default `intelligence,agent`). A router is
included in `create_app()` only if its domain is enabled; the corresponding nav
entries disappear with it. `/health` reports the active set.

---

## D-024: CI runs compile-check plus pytest; no linter dependency

**Decision.** GitHub Actions: `python -m compileall` (catches syntax errors with
no new dependency) plus `pytest`. Heavy optional extras are not installed; the
embedding client degrades to a deterministic hash embedding in tests.

**Why.** A linter is a new dependency (D-002) and its findings would be a large
diff unrelated to launching. `compileall` + `pytest` catches what breaks users.

**Cost.** Style is not machine-enforced. Acceptable for launch.

---

## D-025: The test embedding backend is deterministic and offline

**Decision.** `EmbeddingClient` gains a `hash` backend: a seeded,
L2-normalised bag-of-words projection built from `hashlib` + numpy. It is the
final fallback when neither Ollama nor `sentence-transformers` is reachable,
and it is what tests use.

**Why.** `pytest` currently hangs forever on a HuggingFace download. A test
suite that needs the network is a suite that stops being run. numpy is already a
dependency and `hashlib` is stdlib.

**Cost.** Retrieval quality is poor. Irrelevant, documents are not part of the
launched product and no product code path uses embeddings.

---

## D-026: Slug, not name, is the unique organization key

**Decision.** `organizations` gets a `slug` column with a unique index,
generated from the display name with a numeric suffix on collision.

**Why.** Follows from D-009. Also gives a stable, URL-safe identifier.

**Cost.** One column.

---

## D-027: Rate limits on every sensitive auth route

**Decision.** `/auth/login`, `/auth/register`, `/auth/forgot-password`,
`/auth/reset-password` and `/stripe/webhook` all get `rate_limit(...)`, per-IP,
keyed per route via the existing limiter.

**Why.** `/auth/register` was unlimited. With public signup that is a free
database-fill and email-bomb vector.

**Cost.** The shared Redis limiter in production is already the design.

---

## D-028: The Playwright smoke test targets the real product loop

**Decision.** One spec: landing page → signup → onboarding → add shop URL →
"crawl now" → report appears. Runs against `LLM_PROVIDER=test` so it needs no
API key in CI.

**Why.** The brief asks for exactly this. It is the one test that proves the
definition-of-done sentence: "a new user can sign up, add a shop and receive a
real AI report."

**Cost.** Playwright is a dev dependency, not a runtime one; it does not enter
the Docker image.

---

## D-029: Cost control: the diff engine needs no LLM at all

**Decision.** Crawl + diff + change detection are pure code. The LLM is called
once per shop per report period to *write* the narrative from pre-computed
structured facts. Optional LLM extraction (`EXTRACT_WITH_LLM`, default off) uses
the cheap model.

**Why.** This is the biggest lever on "cost per customer per month", and it also
makes the product's numbers non-hallucinatable.

**Cost.** Report prose is less rich than a fully-LLM pipeline. The numbers,
which is what a seller acts on, are exact.

**Why.** Hiding is not deleting, and the brief says the other domains stay but
are hidden. A flag checked at *router registration* means a disabled domain has
no route at all, not a route that 403s, strictly less attack surface.

**Cost.** The dashboard must tolerate a route list it does not recognise; the
route table is already data-driven, so this is cheap.

---

## D-021: Landing page and dashboard share one origin

**Decision.** `/` serves the marketing landing page; `/app/` serves the
dashboard. Both are static files from `app/web/`.

**Why.** Avoids CORS entirely for the UI the customer actually uses, and keeps
`CORS_ORIGINS` meaningful only for third-party API clients.

**Cost.** None. The SPA is already served this way.

---

## D-022: `CORS_ORIGINS` no longer ships a domain we do not own

**Decision.** The default becomes `http://localhost:8000,http://127.0.0.1:8000`.
`https://sparton.vercel.app` is removed.

**Why.** We do not control that domain; allowing credentials from it is a
liability.

**Cost.** None, nothing is deployed there.

---

## D-023: `/metrics` and `/tools` require a principal

**Decision.** `/tools` requires a user; `/metrics` requires either a user or a
correct `X-API-Key`. A Prometheus scrape sends the header.

**Why.** Tool schemas are internal surface; metric names leak infrastructure
shape. Prometheus can send a header, so this costs nothing operationally.

**Cost.** Documented in DEPLOYMENT.md.

verification, body-size limits, or rate limiting.

**Why.** The post-checkout redirect is not a reliable signal (the customer can
close the tab). Stripe explicitly requires webhook handling for this.

**Cost.** Needs a public URL in dev, `stripe listen` handles it; documented.

---

## D-015: Crawl cadence is per-shop and enforced by the job system

**Decision.** A `shops.crawl_frequency_hours` column (min 6, default 168 =
weekly). `POST /shops/{id}/crawl` enqueues immediately; a scheduler in the worker
enqueues due shops. The job id is `crawl:{shop_id}:{date}` so a manual
re-crawl and the scheduler cannot both run on the same day.

**Why.** "Crawl them on a schedule" is a headline feature. The queue already has
idempotency keys; using them is free.

**Cost.** A small periodic scan in the worker loop.


**Cost.** One table, one insert in the provider.

---

## D-008: Competitor data is a time series, not a "current value"

**Decision.** A `competitor_products` row is one capture: `(competitor, product,
crawl)` with price, availability, capture time and evidence URL. Diffs are
computed between the two most recent captures.

**Why.** The existing `ProductSnapshot` has no `organization_id` and no
`competitor_id`, so two tenants watching the same shop would collide and a
tenant could not see its own history. A time series makes "what changed this
week" a window query, and makes each evidence link reproducible (*this* price,
*at* this URL, *on* this date).

**Cost.** More rows. Bounded by crawl cadence × product count.

---

## D-030: Frontend visual world: The Price Board

**Context.** The v1.0 frontend redesign. Jay delegated the pick between the three directions from the
impeccable direction round (`.impeccable/decision-direction.json`): The Price Board, Shelf Label &
Weekly Folder, Sliding Planes. Scored 1-5 (5 = best):

| Direction | Fit with the shop owner | Difference from Prisync / Bigshopper | Prices + certainty shown natively | Feasible in code, no generated imagery | Total |
|---|---|---|---|---|---|
| **The Price Board** | 4 | 5 | 5 | 4 | **18** |
| Shelf Label & Weekly Folder | 5 | 3 | 4 | 5 | 17 |
| Sliding Planes | 3 | 4 | 2 | 3 | 12 |

- *Price Board*: a board that shows what moved since you last looked is exactly the product. Solid
  tiles for exact data, hatched tiles for extracted data and struck-through rows for sold-out make
  certainty a mark, not a colour. Split-flap boards are pure CSS/JS. Risk: reading as a stock
  ticker, so rows always speak in products and shops, never in tickers.
- *Shelf Label*: the most familiar to a Dutch shop owner and the cheapest to build, but it looks like
  the discount folders the owner competes with, and was/now labels say nothing about certainty.
- *Sliding Planes*: strong Dutch identity, but prices and proof are not native to the form.

**Decision.** The Price Board, with the raises from the round: certainty as a mark (solid / hatched /
struck), an attract loop on the landing board before signup, whole-step flap motion only, one weekly
time axis across views, and "your price solid, competitors dashed" in every price history.

**Consequences.** Light "concourse wall" ground for the dashboard (owners use it on a phone during
the day and on a laptop on Sunday evening, indoors, lit); graphite board panels carry the data.
Station yellow is the single action colour. One self-hosted variable face (Archivo, wdth + wght):
condensed for the flaps and headings, normal width for reading. The previous dark theme is dropped
for v1.0 (one light world, the board itself is the dark element). Build path is code-led: no image
generation (€0 rule), so every scene is CSS/SVG/canvas.

## D-031: The site claims no product matching and no own-price tracking (yet)

**Context.** The brief positions Sparton on "automatic product matching" and asks for price
histories with "your price a solid line". The backend (2026-10-01) reads competitor catalogues
only: there is no capture of the owner's own catalogue and no own-product ↔ competitor-product
matching (`app/ecommerce/` matches a competitor's product to *its own* earlier captures).

**Decision.** Public copy and the dashboard claim only what the code does: competitor discovery
(suggest + confirm), full competitor catalogue reading with exact/extracted labels, change
detection, the weekly report with evidence links. The price-history chart keeps the "your price
solid, competitors dashed" grammar and shows the own-price line as "not tracked yet" until the
backend records it.

**Jay decides.** Whether to build own-catalogue reading + matching (then restore the claim), or
drop it from the positioning.

## D-032: "Email digest" is not shown as a plan feature

**Context.** `app/billing/plans.py` sets `email_digest: true` on Pro and Business, but no code
sends a digest or a report email (`app/core/email.py` only sends verification and reset mails).

**Decision.** The landing page and the billing view do not list "Email digest" until it exists.
The plan data is unchanged (API contract kept).

**Jay decides.** Build the weekly report email (the M10 template is the design for it), or drop the
flag from the plans.

## D-033: Comparison and email links are served by small static routes

`/vs/prisync` serves `app/web/vs-prisync.html`; `/verify-email` and `/reset-password` (the links in
the existing emails, which previously 404'd) redirect into the dashboard's `#/verify` and
`#/reset` screens, with the token in the fragment so it never reaches an access log.

## D-034: NumberFlow was tried and removed; anime.js stays for entrances only

The brief asked for anime.js v4 and NumberFlow. NumberFlow rolls digits smoothly, which breaks
the direction's whole-step motion rule (flaps turn, they do not tween), and the finish review
flagged the counter strip it drove as a KPI pattern. Counters are now flap tiles in the board's
title bar; NumberFlow is no longer vendored. anime.js (vendored, MIT) drives the plan and row
entrances on the landing page, loads after first paint and never under reduced motion.

## D-035: Critique fixes: legends in the app, honest quiet weeks, a confirmed mark-all

The M13 critique (24/40) found the app relied on the landing page to explain its marks. Boards in
the app now carry a legend of the marks they show; unread is spoken to screen readers; a quiet
week tells an established account when it last checked instead of showing first-run copy;
"mark all as read" asks first (there is no un-acknowledge endpoint, so no undo) and reports
partial failure; plan check frequencies are words, not "1×/W". Plan names (Free, Pro, Business)
stay untranslated: they are names, shared with the API and Stripe.

## D-036: shopfeed goes into the image through a BuildKit secret

**Context.** v1.0 shipped without `shopfeed` in the image, so production read every competitor
through the HTML crawl ("extracted" instead of "exact"). Options were a build secret, vendoring the
code, or making the repository public.

**Decision.** A BuildKit secret (`gh_token`) used by one `RUN pip install git+https://github.com/sklsp/shopfeed`
step in its own build stage. The repository stays private and nothing is vendored. Without the
secret the build succeeds with a warning and the app keeps its HTML fallback. The build command
and the verification are in DEPLOYMENT.md.

**Cost.** Whoever builds the image needs a read token for `sklsp/shopfeed`, and the build must use
`--no-cache-filter shopfeed` (BuildKit does not key its cache on secrets).
