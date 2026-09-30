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

- The working virtualenv is **`W:\Sparton\Apollo\.venv`** (Python 3.14.5, has
  fastapi/sqlalchemy/httpx/requests/faiss/pillow/pypdf/python-docx/redis/psycopg/
  pytest). There is no `.venv` in this repo.
- Run commands as
  `& 'W:\Sparton\Apollo\.venv\Scripts\python.exe' -m pytest`
  from the repo root.
- **Shell commands time out at 30 s.** Run anything long in the background:
  ```powershell
  Start-Process -FilePath 'W:\Sparton\Apollo\.venv\Scripts\python.exe' `
    -ArgumentList '-m','pytest' -WorkingDirectory (Get-Location).Path `
    -RedirectStandardOutput 'logs_pytest.txt' -NoNewWindow
  ```
  then poll `logs_pytest.txt`.
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
