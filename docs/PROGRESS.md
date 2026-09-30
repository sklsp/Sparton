# SPARTON — Launch Progress

Mission: turn the merged SPARTON codebase into a **launched, paid SaaS** —
"Sparton Intelligence", competitor & pricing intelligence for small e-commerce
sellers, on the Ares domain + Athena agent.

Read this file first if context was lost. It is the source of truth for
"where am I". Decisions live in `docs/DECISIONS.md`; the baseline is
`docs/AUDIT.md`.

---

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 1 | Audit & baseline — audit doc, app boots, `pytest` green, docs corrected | ✅ done |
| 2 | OpenRouter — production provider: retries, timeouts, headers, token usage | ✅ done |
| 3 | Product core — `app/ecommerce/` shop→competitors→crawl→changes→report | ✅ done |
| 4 | SaaS layer — signup, verification, reset, Stripe, plans, feature flags | ✅ done |
| 5 | Frontend — public landing page + product dashboard | ✅ done |
| 6 | Production — Docker, compose, real migrations, health, CI, DEPLOYMENT.md | ✅ done |
| 7 | Hardening — full route coverage, tenant isolation, security review | 🔄 in progress |
| 8 | Launch checklist — `docs/LAUNCH.md` | ⬜ not started |

Commits, one per phase: `77d14d6` (security), `aafb69c` (llm), `006d5ed`
(ecommerce), `a4f3b76` (saas), `2efe9bf` (web), then Phase 6 on top.

**Suite status: 279 passed, 0 failed, 0 errors** (226 at the end of Phase 4,
+31 in Phase 5, +22 in Phase 6).

---

## Environment notes (read before running anything)

- Use a virtualenv with `requirements.txt` installed (Python 3.12+); there is no
  `.venv` in the repo. Run the suite from the repo root with `python -m pytest`.
- The full suite takes a few minutes; in a tool with short command timeouts, run it
  in the background and redirect the output to a file.
- The product runs on **OpenRouter** via `OpenAICompatibleProvider`
  (`OPENAI_BASE_URL=https://openrouter.ai/api/v1`). Never hardcode a key.
  Never commit a `.env`.

---

## Phase 1 — Audit & baseline

### Done

- Read `README.md`, `docs/ARCHITECTURE.md`, `docs/MIGRATION.md` and every file
  in `app/`, `workers/`, `scripts/`, `migrations/`, `tests/`.
- **Verified claims by running code**, not by reading docs:
  - app imports, `create_app()` yields 50 OpenAPI paths
  - dumped the full route table
  - ran an unauthenticated probe: 12+ routes answer `200` with no credentials,
    including `/rag/debug-query` (leaks document text) and `POST /comfyui/workflows`
    (writes server files)
  - confirmed with no `API_KEY` set, every request becomes a cross-tenant admin
  - confirmed the only Alembic revision is `upgrade() -> pass` (0 `create_table`)
  - confirmed `pytest` hangs on test 5 (sentence-transformers model download)
  - confirmed `scripts/seed_demo.py` runs clean
  - confirmed `app/ecommerce/`, `Dockerfile`, `docker-compose.yml`, `frontend/`,
    `app/integrations/`, `docs/DEPLOYMENT.md`, `.github/` **do not exist**
- Wrote `docs/AUDIT.md` — what works, what is broken, what the docs claim that
  does not exist, with file:line evidence.
- Wrote `docs/DECISIONS.md` — 29 decisions with rationale and cost.

### In progress

- Fixing the `pytest` hang (deterministic offline embedding backend, D-025).
- Removing the anonymous cross-tenant fallback (D-004).
- Guarding the open routes (D-005).

### Next

1. Finish the Phase 1 code fixes and get `pytest` genuinely green.
2. Correct `README.md` and `docs/MIGRATION.md` to describe reality.

### Open risks

- The test suite shares a process-global SQLAlchemy engine; a test that forgets
  the `db_session` fixture corrupts the next one.
- `TenantContext.scoped` relies on `column_descriptions[0]["entity"]`, which
  breaks on aggregate selects and joins — must not be trusted for billing checks.
- `documents`/`generation`/`datasets`/`training` have near-zero test coverage and
  the widest attack surface. They are being feature-flagged off rather than
  fixed (D-001, D-020).

---

## Phase 2 — OpenRouter

### Done

- `OpenAICompatibleProvider` is production-grade:
  - `HTTP-Referer` + `X-Title` attribution headers (an OpenRouter requirement).
  - Retries 408/409/425/429/5xx/529 with exponential backoff **and full
    jitter**, so N workers hitting a 429 do not retry in lockstep. Honours
    `Retry-After`, capped.
  - Does **not** retry other 4xx — a 400 is our bug, retrying costs money.
    Stops immediately on DNS failure.
  - Separate connect (10s cap) and read timeouts.
  - `is_available()` really calls `GET /models`, cached 60s.
  - `list_models()` raises rather than returning `[]` — an empty list is
    indistinguishable from "no models available".
- **Token accounting**: new `LLMUsage` table (org, user, provider, model,
  task, prompt/completion/total tokens, latency, attempts, cost). Every call
  records a row. The tenant is carried down from `current_user` via a
  contextvar, so no call site threads a user object.
- Model routing: `llm_model_strong` (agent + report) vs `llm_model_cheap`
  (extraction).
- 45 mocked-HTTP tests in `tests/test_openrouter.py`.

### Bug the tests caught

OpenRouter's `pricing` block is USD **per token**, not per million. The first
implementation divided by 1e6 and was wrong by a factor of a million on every
row. `test_cost_is_computed_from_reported_pricing` caught it.

---

## Phase 3 — Product core

### Done

`app/ecommerce/` — the loop a customer pays for, working end to end.

| File | Responsibility |
|---|---|
| `urls.py` | URL normalisation, platform detection, **SSRF pre-check** |
| `discovery.py` | Add shops/competitors; search-based suggestions (nothing auto-added) |
| `crawl.py` | Crawl orchestration, capture writes, diff, change records |
| `changes.py` | The diff engine. **Pure arithmetic, no LLM** (D-029) |
| `reports.py` | Facts → deterministic Markdown, optionally improved by the AI |
| `jobs.py` | `crawl_shop`, `crawl_competitor`, `generate_report`, `schedule_due_crawls` |

- New tables: `shops`, `competitors`, `competitor_products` (a **time
  series**), `change_events`, `reports`.
- New API: `/shops`, `/competitors`, `/changes`, `/reports`, `/overview` —
  18 routes, all tenant-scoped.
- `competitor_products` is append-only: a re-crawl inserts rows, and a change
  is a comparison between two captures (D-008). Evidence links are therefore
  reproducible.
- The first crawl of a competitor is recorded as a **baseline**, not as
  3,000 "new product" alerts.
- A **deterministic report renderer** always exists. The AI only improves the
  prose on top; a model outage never costs a customer their report.
- Feature flags (`app/core/features.py`): a disabled domain has **no routes**,
  not routes that 403. The product is on by default; documents / generation /
  datasets / training / research are off (D-001, D-020).
- `scripts/seed_demo.py` rewritten: it writes a real capture time series and
  runs it through the **real diff engine**, so the demo is exactly what the
  product would produce. 3 weeks, 3 competitors, ~105 captures, 16 price moves,
  one report.
- 76 tests in `tests/test_ecommerce.py`, driven by a fake httpx transport that
  serves fixture storefronts — the full customer journey, no network.

### Bugs the tests caught

1. **`_registrable` collapsed every Shopify shop to `myshopify.com`**, so every
   competitor looked like the customer's own shop and was rejected. Now skips
   known platform suffixes and two-part public suffixes.
2. **Listing pages became phantom products.** `extract_page` treated a page as
   a product if the HTML merely *contained the word "product"* — which every
   category page does, in its product URLs. That would have produced a bogus
   "new product" alert on every crawl of every competitor. The signal is now
   a **price**, not a substring.
3. **Removed products were never detected.** The "previous set" was built from
   products that still exist, so a product that had gone was, by definition,
   absent from it. Now loads the previous crawl's full URL set.

---

## Phase 4 — SaaS layer

### Done

**Signup and account recovery**
- Email verification: `auth_tokens` table, tokens stored **hashed**, single-use,
  re-issuing invalidates the old one, and a verification link cannot be
  redeemed as a password reset.
- Enforced in production only, and at the *product* boundary rather than at
  signup — otherwise a customer who loses the email is locked out of their own
  account with no recovery (D-010).
- Password reset + change-password. A reset **revokes every existing session**,
  because the reset may have been prompted by a compromise.
- `forgot-password` always answers the same thing, known address or not.
- Email is sent over stdlib SMTP; with no SMTP configured it is written to
  `data/outbox/*.eml`, so local dev and CI exercise the real flow offline.

**Billing** (no new dependency — Stripe over `requests` + stdlib HMAC, D-002)
- `plans.py`: Free (1 shop / 3 competitors), Pro €29 (3 / 15), Business €79
  (10 / 50). `get_plan` **fails closed to Free** on an unknown id.
- `check_shops` / `check_competitors` / `check_tokens` raise **402** with a
  machine-readable `plan_limit_reached` detail. `check_crawl_frequency` clamps
  rather than refusing.
- Every count filters on an explicit `organization_id`. Billing code does not
  use `TenantContext.scoped`, which is deliberately unscoped for machine
  principals.
- The plan is resolved **server-side on every mutating route**; the browser
  never says what plan it is on.
- `stripe.py`: Checkout, Customer Portal, customer creation, signature
  verification with a replay window and multi-`v1` rotation support.
- Webhooks are the **only** writer of paid plans, idempotent by event id.
- A duplicate shop is reported as a 409 duplicate, not as a 402 upsell.

### Bugs the tests caught

1. `_flatten()` expanded pre-built bracket keys (`line_items[0][price]`) into
   `line_items[0][price][]`, which `urlencode` would stringify into a parameter
   Stripe silently ignores.
2. The new auth routes were registered as `/auth/auth/...` — the router already
   carries the `/auth` prefix, so all six returned 404.

---

---

## Phase 5 — Frontend

### Done

**Public surface (unauthenticated)**

- `GET /` serves `landing.html` — the pitch, how it works, pricing, FAQ, all
  wired to the real endpoints. It prices itself from `GET /billing/plans`, so
  the table can never drift from what the server actually charges.
- `/landing.css`, `/landing.js`, `/styles.css` are served explicitly. The
  dashboard is mounted at `/app`, so a root-served page referencing `./x.css`
  resolved to `/x.css` and 404'd — the page rendered unstyled.
- `/legal/privacy`, `/legal/terms`, `/legal/dpa` are real pages. They were
  linked from the footer and did not exist.

**Dashboard** — six views replacing the old platform-oriented ones:

| View | Job |
|---|---|
| `overview` | One `GET /overview` round trip. Four numbers, what needs a decision, what you are watching. |
| `alerts` | The daily screen. Filter by kind/shop/unread, acknowledge, always link to the evidence page. |
| `shops` | Onboarding. Add a shop, add a competitor, crawl, discover, delete. |
| `reports` | Filing cabinet on the left, document on the right. |
| `billing` | Current plan, allowance meters against hard limits, the plan table, Stripe hand-off. |
| `settings` | Account, password, and a plain statement of what we store. |

- `views/shared.js` holds only product-domain helpers (money, change kinds,
  plan-limit detection) and **re-exports nothing `ui.js` already provides** —
  `ago`, `when`, `empty`, `badge` stay in one place.
- `api.js` gained `plans()`, `changePassword()` and `discoverCompetitors()`,
  which had server routes but no client wrapper.
- Product-view CSS added to `styles.css` on the existing token system, using
  the existing `data-variant` / `data-size` button convention.

### Bug the tests caught

`overview.js` had an unbalanced paren in the shop grid. `node --check` did not
see it (it parsed the file as CommonJS); it only surfaced when each view was
imported as a real ES module. **All 116 classes** the six views reference are
now covered by a stylesheet rule, and every view mounts against a stub DOM.

### Bug found and fixed: the verification lockout

Phase 4 applied the verification gate inside `current_user`, so in production
**every** authenticated route 403'd for an unverified account — including
`GET /auth/me`, `POST /auth/resend-verification` and `POST /auth/change-password`.

That is a deadlock: a customer who cannot receive mail could not ask for another
link, read their own account, or change the password they were emailed about.

Fixed by splitting the gate out:

- `app/core/auth/api.py` gains `unverified_user` — authenticates and binds
  tenant context, but does not require verification. `current_user` is now
  `unverified_user` + `_require_verified`, so the default is unchanged.
- Only the three recovery routes opt out. Everything that reads tenant data
  still 403s, and all three still require a valid session (anonymous → 401).

`tests/test_recovery_paths.py` pins both halves: recovery works unverified,
product access does not, and a verified session is not rejected.

### Tests added

- `tests/test_recovery_paths.py` — 12 tests: recovery open, product shut,
  verification round trip, password reset round trip.
- `tests/test_public_pages.py` — 19 tests: landing page and its assets resolve,
  legal pages serve, dashboard `/app` mount undisturbed.

---

## Phase 6 — Production

### Done

**The migration was empty.** Phase 1 recorded that the only Alembic revision
was `upgrade() -> pass`. A fresh production database would have come up with
zero tables and the app would have failed on its first query. Now:

- Autogenerated from the models: **39 `create_table` calls, 40 tables.**
- `down_revision` is `None`, and the empty stub revision is deleted, so there is
  exactly one root and no dangling reference.
- The autogenerated file used a bare `Text` in
  `postgresql.JSONB(astext_type=Text())` — a `NameError` waiting to happen.
  Fixed to `sa.Text()` in 26 places.

`tests/test_migrations.py` proves it rather than asserting it:

- no shipped revision has an empty `upgrade()`
- `upgrade head` creates every core table (identity, the product loop, billing,
  cost accounting)
- re-autogenerating produces **zero** drift, so the models and the migration
  cannot disagree
- `downgrade base` → `upgrade head` round-trips

**Deployment**

- `Dockerfile` — two stages, so the runtime image has no compiler. Runs as
  uid 10001. Migrations run in the same command as the server, with no
  `|| true`: a failed migration stops the deploy rather than leaving a process
  serving requests against the wrong schema.
- `docker-compose.yml` — PostgreSQL 16, Redis 7, `api`, `worker`. The worker
  runs the **same image** with a different command; they must not drift.
  Postgres is not published to the host, and `POSTGRES_PASSWORD` has no
  default, so compose refuses to start rather than opening the database.
- `.dockerignore` — excludes `.env`, `*.db` and `data/`. A secret in a layer
  survives a later `RUN rm`, because the bytes are still in the earlier layer.
- `docs/DEPLOYMENT.md` — runbook, the settings that actually matter, a
  security checklist, and a symptom-to-cause table.
- `.github/workflows/ci.yml` — three jobs: `pytest`, frontend parse, and a
  real `docker build` that **boots the image and asks `/live`**. Building is
  not deploying.
- `.env.example` — gained the production switches an operator must set, and
  says why each one matters.

### Bugs the tests caught

1. The Dockerfile healthcheck probed `/health/live`. The health router is
   mounted **unprefixed**, so the real path is `/live`. A healthcheck on a 404
   marks a healthy container unhealthy forever.
2. The compose env block had a `#` comment on a continuation line of `ENV` in
   the Dockerfile — a syntax error, not a comment.
3. `OPENAI_API_KEY` was commented out in `.env.example`, so the variable that
   makes the product work was not in the list of settings to set.

### Tests added

- `tests/test_migrations.py` — 4 tests, each running Alembic for real.
- `tests/test_deployment.py` — 18 tests: Dockerfile instructions, every
  `COPY` path exists, non-root, migrations-before-server, healthcheck path,
  compose service graph, no default DB password, and every compose env key is a
  real `Settings` field.

---

## Phase 7 — Hardening

### Done

**Route coverage, derived rather than remembered.**

`tests/test_security_guards.py` probes a hand-written list of sensitive paths.
That list is a snapshot — it cannot notice a route added after it was written,
which is exactly how Phase 1 found twelve endpoints answering `200` to an
anonymous caller.

`tests/test_route_coverage.py` reads the app's own OpenAPI document instead:

- every route must be classified as `PUBLIC`, `SIGNED`, or protected, or the
  test fails and names it
- the public surface is pinned as an exact set, so a route becoming public is
  visible in a diff and has to be justified in one line
- every protected route is probed with no credentials and must answer `401`
- parameterised routes must answer `401` too, not `404`: authentication has to
  be checked *before* the row is looked up, or the response becomes an
  existence oracle
- write-only routes are probed with their real method — a `GET` returns `405`,
  which says nothing about the guard

The public surface is **8 routes**: `/live`, `/ready`, `/billing/plans`, and
the five pre-account auth routes.

### Bug the tests caught

**`POST /auth/logout` returned 500 to every caller.**

```python
creds = _bearer(request)      # HTTPBearer.__call__ is async
if creds is None: ...          # never true
revoke_session(db, creds.credentials)   # AttributeError
```

`_bearer` is a FastAPI `HTTPBearer` instance. Its `__call__` is `async`, so
calling it from a sync handler returns a coroutine, which is never `None` and
has no `.credentials`. Every logout — authenticated or not — raised an
unhandled `AttributeError` and returned a 500.

This is a genuinely bad place for a 500: logout is the route a user hits when
something has already gone wrong, and it was the one route that could not be
used to recover. Fixed by reading the `Authorization` header directly, and the
unused `HTTPBearer` import is gone.

---

### External security review: five findings, all closed

`tests/test_ssrf_and_proxy.py` — 44 tests, one per way the previous code could
be made to do something it was supposed to refuse.

**1. SSRF through redirects.** The crawler validated the first URL, then handed
the response to whatever `Location` pointed at, because the httpx client was
built with `follow_redirects=True`. A public competitor URL answering
`302 Location: 169.254.169.254` reached cloud metadata; `127.0.0.1` reached an
internal admin port. Both are one header away, and neither was visible in the
original code, which *looked* like it validated every URL it fetched.

Now: `follow_redirects=False`, redirects followed by hand with a cap of 5, and
the public-address check re-run on **every hop** — including the `robots.txt`
fetch, which is a URL we construct and therefore also attacker-controlled.
Covered: redirect to loopback, to `169.254.169.254`, to a private range, and a
redirect loop.

**2. DNS rebinding.** Checking a name twice does not close the rebinding window,
because the name is the thing that changes: it can resolve to a public address
when validated and to `127.0.0.1` when httpx opens the socket. The connected
peer's address is now read from the response and checked as well. A no-op when
the transport exposes no real socket, which is what MockTransport does in tests.

**3. The body cap bounded memory after the download.** `response.content[:cap]`
buffers the entire body and then throws most of it away, so a 2 GB response
still cost 2 GB of memory and bandwidth before the slice ran. The body is now
read with `iter_bytes()` and the read stops at `max_body_bytes`. Tested with a
5 MB chunked response and with an unbounded generator that never ends — the
shape a hostile server actually sends, and the one where a `Content-Length`
check would not help.

**4. `not address.is_global` instead of a denylist.** The old check enumerated
`is_private | is_loopback | is_link_local | is_reserved | is_multicast |
is_unspecified` — a list that has to remember every non-public range, and the
ones it forgets are the interesting ones. `is_global` is a single negation that
covers the v4 and v6 special registries together, and catches `100.64.0.0/10`
carrier NAT and `192.0.0.0/24` protocol assignments. Twelve non-public forms
and three public ones are pinned by test, along with the mixed-resolution case
(one public and one private A record, which lands on the private one about half
the time and is refused outright).

**5. `X-Forwarded-For` was trusted unconditionally.** `client_key` read the
header before falling back to the socket, so any caller could rotate the header
per request and get a fresh rate-limit bucket every time — which made every
limit in the module a suggestion, on the very endpoints that need them (login,
register, password reset). The header is now read only when the TCP peer is in
`FORWARDED_ALLOW_IPS`, which defaults to loopback.

The Dockerfile ran uvicorn with `--forwarded-allow-ips='*'`, which is worse than
the bug: with a wildcard, uvicorn rewrites `request.client` from that same
untrusted header *before the app ever sees it*, so the new check would have been
theatre. It now reads `${FORWARDED_ALLOW_IPS:-127.0.0.1}`, the setting is in
compose and `.env.example`, and `docs/DEPLOYMENT.md` explains both halves and
why `*` is not a safe value.

**6. `GET /billing/invoices` cross-tenant read.** For a user with no
`organization_id` the filter became `organization_id IS NULL`, which returns
every *unscoped* invoice in the table. No organization now means an empty list,
before any query runs.

**7. Both default model ids were dead.** `anthropic/claude-3.5-sonnet` and
`google/gemini-2.0-flash-001` are no longer served by OpenRouter — verified
against `GET /api/v1/models`, which lists 464 models and contains neither. They
would have 404'd the first time a customer triggered a paid feature, which is the
worst possible moment to discover it. Now `anthropic/claude-sonnet-4.6` (strong)
and `google/gemini-3.5-flash-lite` (cheap), both confirmed present. A test fails
if a retired id is ever assigned again; the comment recording *why* the ids
changed is allowed to name them, because that is the record of the decision.

**8. Migrations must be a one-off step above one replica.** The container CMD
runs `alembic upgrade head`, which is right for one replica and wrong for
several: three replicas booting together run three concurrent migrations
against one database and the losers crash-loop during a deploy. Documented in
`docs/DEPLOYMENT.md` with the `docker compose run --rm api` sequence, and
pinned by test so the guidance cannot silently disappear.


### Suite status

```
374 passed, 0 failed, 0 errors, 0 skipped
```

Up from 279 at the Phase 6 checkpoint: +44 for the security review
(`tests/test_ssrf_and_proxy.py`), +3 for the logout regression, and the rest
from `tests/test_route_coverage.py` replacing the hand-written probe list.

### Browser validation, and two bugs only a browser could find

`tests/test_browser_smoke.py` — 17 tests driving real Chrome against a real
uvicorn on a real port. Everything up to here tested the API; the customer-facing
half of SPARTON is static HTML and ES modules that no API test ever executes.

It immediately found two shipping bugs, both on the conversion path.

**The landing page never showed a price.** `landing.js` does
`import { h, fill } from "./ui.js"`, but the root route serves only
`landing.css`, `landing.js` and `styles.css` — `ui.js` is mounted under `/app`
with the dashboard. A module whose import 404s fails to parse, so the *entire*
script was dead and `#plans` kept its "Loading plans…" placeholder forever.

This is the worst kind of failure to ship: the page is static HTML, so it looks
finished and well-designed with JavaScript disabled, and the one thing that
*cannot* be static — the pricing table, because the server must be the only
source of prices — was silently absent. `ui.js` is now served at `/`, and a
test fails on any 404 in a landing-page response, which catches the general
case rather than this one instance.

**"Start free" landed on a sign-in form.** Every CTA on the marketing page
points at `/app/#signup`, and `boot()` ignored the hash and always rendered the
login form. A visitor with no account got a login form, with the only way
forward a small secondary link reading "Create one". `boot()` now honours
`#signup`/`#register`, and a test asserts the registration form -- not the login
form -- is what the marketing CTA produces.

Neither is reachable by an API test: the routes existed, the links were correct,
the responses were 200, and the HTML was complete. The failures were entirely in
the browser's hands.

Also covered, and all passing: the pricing table loads from the public
`/billing/plans`, the three legal pages render, an unknown path does not 500,
signup reaches the dashboard shell, the session survives a reload, the
navigation lists the product views, and the full product loop runs in-browser --
add shop, add competitor, read both back, fetch reports.

**Tenant isolation in two browsers.** Two accounts, two browser contexts, two
real sessions with separate cookies and separate `localStorage` tokens. Tenant B
cannot see tenant A's shops in the list, and `GET /shops/{id}` is refused
outright rather than merely omitted. Playwright's sync API is bound to its
creating thread, so the two tenants run sequentially with independent contexts
rather than in parallel threads.

The file skips, loudly, when Playwright or a browser is absent, so the suite
still passes on a machine with neither. A `browser` job in CI installs Chromium
and runs it, so these two bugs cannot come back.

---

## Phase 8 — Launch checklist

### Done

**`docs/LAUNCH.md`** (301 lines). DEPLOYMENT.md says how to build and run the
thing; LAUNCH.md is the go-live document — the values you must supply, the
ordering that matters, and the evidence you must produce before telling anyone
it is ready. Each item names the failure it prevents, because "set the secrets"
is not a checklist.

The parts that are not obvious from the code:

- **Register the Stripe webhook before enabling paid plans.** Webhook events are
  the only authority that grants a paid plan. Enable pricing first and a customer
  pays and receives nothing.
- **Set `STRIPE_WEBHOOK_SECRET` in the same deploy as the registration.** With no
  secret, the endpoint returns `503` rather than accepting unsigned events, which
  is correct and still means payments silently do not apply.
- **The outbox is a secret store.** With `SMTP_HOST` unset, mail is written to
  `data/outbox/*.eml` instead of being sent — right for development, and in
  production it looks like it worked while nobody receives anything. Those files
  contain live reset tokens.
- **Verify the model ids before building.** They were retired once already; the
  previous defaults 404'd the first time a customer used a paid feature. The
  check is a `curl` against OpenRouter's catalogue, and there is now a test too.
- **A backup you have never restored is a hypothesis.** Restore one into a
  scratch database and query it before you need it.
- **The LLM is not in the failure path.** Change detection is arithmetic and
  reports degrade to deterministic text when the provider is down, so a provider
  outage should not page anyone at 3am.

The **go-live smoke test** in section 9 is a person-in-a-browser checklist, and
every item on it is a bug that has actually happened here: the pricing table
that never loaded, the CTA that landed on a login form, the logout that returned
500.

### The checklist is itself tested

`TestTheLaunchChecklistIsReal` in `tests/test_ssrf_and_proxy.py` asserts that the
document has not drifted from the application: that `/live`, `/ready` and the
three legal pages it names actually exist and return 200, that it states the
anonymous `/shops` probe, that it covers secrets, SMTP, Stripe, OpenRouter,
migrations, backups, rollback, monitoring and legal, that it contains no
real-looking secret, and that **the test count it quotes is the count that ran**.

That last one is the useful one. A stale number in a definition of done is a
small lie that outlives the change that made it true, so if the suite grows and
`LAUNCH.md` is not updated, the suite fails.

### CI

A `browser` job installs Chromium and runs `tests/test_browser_smoke.py`, so the
two browser-only bugs cannot return. Four jobs now: `test`, `browser`,
`frontend`, `image`.

### Not proven here, and honestly listed in the document

- **The Docker image has never been built.** No Docker daemon is available in
  this environment. The Dockerfile is verified structurally (every instruction
  is real, every `COPY` path exists, non-root, migrations before the server,
  healthcheck points at a real route) but not by a build. This is the largest
  remaining gap and it is listed first in the definition of done.
- A real Stripe charge, a real SMTP delivery, and a real rollback have not been
  performed, because they require accounts and money. Section 9 turns each into
  an explicit human step.

---

## Final suite status

```
507 passed, 0 failed, 0 errors, 1 skipped
```

412 API tests and 20 real-browser tests, in one run. The progression across the
project: 226 (end of Phase 4) → 279 (Phase 6 checkpoint) → 374 (the security
review) → 397 (browser validation and the launch checklist).

The last 23 are the interesting ones. None of them can fail on a route, a status
code or a database row; they fail on a rendered page, a module that 404s, a
button that leads somewhere useless, or a document that has drifted from the
code it describes. Those are the failures a customer finds first and the suite
used to find last.

---

## Phase 9 — Exact competitor data from shop feeds

### What changed and why

The crawl used to read prices out of rendered HTML. That is a guess wearing a
number's clothes: which of the six prices on a product page is the real one, is
the struck-through figure a "was" price or a second variant, and how much of that
did a language model infer? The answer also cost tokens, once per crawled page.

Almost every webshop already publishes the same facts as machine-readable data.
So the crawl now tries, in order:

| Tier | Source | `data_source` | Exact? | LLM tokens |
|---|---|---|---|---|
| 1 | The shop's own feed (Shopify `/products.json`, WooCommerce `/wp-json/…`) | `feed` | Yes -- the price they charge | 0 |
| 2 | `schema.org` JSON-LD on a product page | `jsonld` | Yes, for that page | 0 |
| 3 | The rendered page, parsed | `html` | Best effort | only here |

This is simultaneously more accurate and cheaper, which is rare enough to be
worth stating plainly: the tiers we would *prefer* to use are also the tiers
that cost nothing.

### Defence in depth on SSRF

`shopfeed` has its own checks — non-public addresses refused on every redirect
hop, and the connected peer verified so DNS rebinding does not get through. But
Sparton's own crawler guard remains the **outer** gate, applied before
`shopfeed` is called at all. The inner library is a dependency we did not write;
if it is ever swapped or turns out to be less strict than advertised, the outer
check is the one whose absence would be our bug. `app/ecommerce/feeds.py` also
refuses a URL when Sparton's guard would, rather than relying on the library to
reach the same conclusion.

Per-plan limits are unchanged: the feed read is given the same `max_products`
budget the HTML crawl would have spent, so a feed cannot quietly cost more than
the thing it replaces.

### Recording the source, per capture and per competitor

Two new columns on `competitor_products` (`data_source`, `compare_at_price`,
`variant_count`) and one on `competitors` (`last_source`).

`compare_at_price` matters on its own. Without it a sale is invisible: the
effective price is identical before and after a markdown ends, and only the
was-price tells you the discount was withdrawn. That is a change a customer pays
for.

The tier is shown in the competitor view, and the wording is deliberately
asymmetric:

- **"exact prices from the shop's feed"** — green
- **"exact prices from the page's structured data"** — green
- **"extracted from the page"** — neutral

A parsed price that looks like a verified one is the failure that matters: the
customer would act on it. So the parsed tier is never dressed up, and an
uncrawled competitor shows no source at all rather than claiming one.

Migration `c4d91f2ab7e3` adds the columns, all nullable or defaulted, so it is
safe against a live table: existing rows read as `html` / `1` / `NULL`, which is
exactly what is known about them.

### Tests: 28 in `tests/test_feeds.py`

Unit level, against a fake Shopify, a fake WooCommerce store, a JSON-LD-only shop
and a shop with nothing at all (all `httpx.MockTransport`, no network):

- exact `Decimal` prices, including **0.45**, which has no exact binary
  representation — the case that would expose any accumulated float delta
- a sale appearing as `compare_at`, and a bogus `compare_at` *below* the price
  being discarded rather than reported as a markdown
- WooCommerce minor units (1299 → 12.99); storing the raw integer would be out by
  100× and would be a *confident* wrong number
- currency, stock, SKU and variant count taken from the shop
- a non-Shopify/non-Woo site falling through to the HTML crawler
- broken JSON-LD skipped rather than losing the page

Integration level, through the real `crawl_competitor`:

- **a feed crawl writes no `LLMUsage` row and never calls the provider** — the
  property the whole feature exists for, and one that would stay invisible in the
  price if it ever broke, because the number would be identical
- the same for a JSON-LD crawl
- prices survive the round trip through the database
- a feed-driven markdown is detected as a price change, still with zero tokens
- the HTML fallback is labelled as *not* exact

`tests/test_browser_smoke.py` gained three checks that the API and the view agree
on the label, and that an uncrawled competitor does not claim a source it has not
earned.

### Also fixed: the migration stub test was too literal

`test_the_only_shipped_revision_is_not_a_stub` asserted that each migration's
`upgrade()` contains one of `op.create_table`, `op.execute` or `op.add_column`
*as a literal substring*. The new migration uses
`batch.add_column(...)` -- the batch form of the same operation -- and the test
failed, correctly reporting that the stub check had not kept up with the code it
guards.

The fix is to match the call on either side of the dot and to scope the search to
the `upgrade()` body, and a new test asserts the **upgrade → downgrade →
upgrade** round trip, so a migration that cannot be reversed is caught rather
than assumed reversible. The test was renamed accordingly: it no longer implies
there is only one revision.

---

## Phase 10 — Fixes from the first real Docker build

Four problems, each found by actually building the image and running the stack
rather than by a test.

### 1. The worker was never healthy

Fixed and committed separately (`8138e01`): the image's `HEALTHCHECK` curls the
API's `/live`, and the worker inherited it while serving no HTTP. It now has its
own probe, `python -m workers.healthcheck`, which checks the database, checks
Redis, and reads a heartbeat the worker writes on every poll. The heartbeat
matters most: the first two stay green while the loop is wedged, which is the
failure a worker actually has.

### 2. The image was 9.8 GB -> 82.5 MB

Measured, not estimated:

| | Before | After |
|---|---|---|
| Image size | 9.8 GB | **82.5 MB** (86,554,591 bytes) |

Measured with `docker image inspect sparton:size-test --format {{.Size}}` after
`docker build -t sparton:size-test .`.

The cause was not only the dependency list. `requirements.txt` carried
`sentence-transformers` and `faiss-cpu` (which pull the whole PyTorch stack, CUDA
runtime included) for the feature-flagged `documents`, `generation`, `datasets`
and `training` domains -- none of which the shipped product imports. And it
could not simply be deleted, because **`app/main.py` imported those routers
unconditionally**, so `import app.main` reached faiss whether or not a single
document route was mounted. Deleting the dependency without fixing the import
would have produced an image that could not start.

Three changes:

- `app/main.py` imports `create` and `knowledge` inside their feature-flag
  branches, not at module level.
- `app/api/__init__.py` no longer re-exports them. This one is easy to miss and
  it silently undid the first: `from app.api import health` executes the package
  `__init__` first, so a re-export there loads faiss no matter how carefully
  main.py is written.
- The routers are now genuinely lazy. A disabled domain is not mounted *and*
  not imported.

`requirements.txt` is the product set; `requirements-experimental.txt` holds the
extras and `-r requirements.txt`, so it extends rather than duplicates. The
Dockerfile installs only the product set, with `--build-arg
INSTALL_EXPERIMENTAL=1` to opt back in.

**Verified in the built image**, not just locally:

```
ROUTES 46
faiss_loaded False
sentence_transformers_loaded False
shopfeed_available False          # private local dep, degrades to HTML crawl
fallback_source html found False
loopback_refused True             # the SSRF guard is still ours
private_refused True
```

18 tests in `tests/test_product_dependencies.py`. The load-bearing one runs
`import app.main` in a subprocess where faiss, torch, sentence-transformers,
PIL, pypdf and docx are *unimportable* -- simulating a machine that does not
have them rather than trusting that this one happens not to use them -- and
asserts the product's own routes still exist. There is also a test for the test:
the blocker is asserted to actually block, and asserted to use `find_spec`
rather than the `find_module`/`load_module` protocol that Python 3.12 removed.
The first version of that blocker used the legacy protocol and silently allowed
everything, which would have made every subprocess test vacuous.

Enabling `documents` without the extras now fails at import with a message
naming the missing package, rather than starting an app whose RAG routes 500 on
first use.

### 3. The feed reader was bursting a competitor's server

`app/ecommerce/feeds.py` passed `sleep=_no_sleep` to shopfeed's `Fetcher`, with
a comment claiming "politeness is enforced by our caller's throttle". That was
false. `ResponsibleCrawler._throttle` is per *crawl*, not per request, and the
feed reader is a different HTTP client the crawler's throttle never sees. So one
`read_catalog` fired robots.txt, the Shopify currency probe and every
`products.json` page back to back -- a burst, aimed at a competitor's server,
from an IP that is us.

The default is now `time.sleep` with a 1 s per-host interval, and `sleep` is
injectable purely so tests can record the waits rather than take them. A test
that really slept would take a minute per file; a test that mocked the clock
away would not have noticed the throttle being removed at all.

`crawl_competitor` gained the same seam, so the integration tests drive it too.

The recording fake also corrected a wrong assertion of mine: shopfeed sleeps the
*remaining* time to the next allowed moment, so each wait is a fraction of a
millisecond under the interval, because the request itself took that long.
`assert wait >= 1.0` fails on correct code. The test now asserts the gaps are
real rather than pretending to a precision that is not there.

### 4. Prices are Decimal, not float

`price`, `compare_at_price`, `previous_price`, `new_price` and `delta` are now
`Numeric(12, 4)` with `asdecimal=True` (migration `f1b7c4e2a9d3`), and Decimal
from end to end: the feed reader no longer converts to float, and
`capture_fields` coerces the HTML and LLM tiers' floats at the single boundary
where a capture is built.

The reason is the one the product is sold on. A float cannot hold 0.45, so a
Float column turns "exact" into "exact to about fourteen decimal places" -- in a
number a customer acts on, and in the comparison that decides whether we alert.
`delta_pct` stays a float: it is a ratio, and four decimal places of a percentage
would be a misleading claim of precision.

Nullable-safe: the columns are nullable, `existing_nullable=True`, and
PostgreSQL gets an explicit `USING ... ::numeric(12, 4)` so the stored floats are
reinterpreted as values rather than as bytes.

**A real bug this surfaced.** `DetectedChange.detail` embeds the delta in a
JSON column, and a JSON column cannot hold a Decimal -- so the first run of the
price-drop test died with `TypeError: Object of type Decimal is not JSON
serializable`. The typed columns beside it already carry the value, so the
detail blob (a human-readable copy) stores a float deliberately, and says so.

Two of my own tests were also wrong in ways worth recording. `isinstance(type,
Numeric)` passes for a *float* column, because SQLAlchemy's `Float` subclasses
`Numeric` -- so the check now looks at `asdecimal` and the type name. And the
first draft of the feed assertions compared `Decimal("0.45") == 0.45`, which is
False; comparing against float literals would have reintroduced exactly the
error the column exists to prevent.

### Suite after the build fixes

```
507 passed, 0 failed, 0 errors, 1 skipped
```

Up from 455. The additions are the worker healthcheck (23), the product
dependency guard (18), the money/Decimal tests (17) and the feed pacing
tests (8). The skip is still the launch-checklist test comparing the quoted
count against the previous run's report.xml.
