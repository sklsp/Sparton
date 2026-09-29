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
| 4 | SaaS layer — signup, verification, reset, Stripe, plans, feature flags | 🔄 in progress |
| 5 | Frontend — public landing page + product dashboard | ⬜ not started |
| 6 | Production — Docker, compose, real migrations, health, CI, DEPLOYMENT.md | ⬜ not started |
| 7 | Hardening — full route coverage, tenant isolation, Playwright, security review | ⬜ not started |
| 8 | Launch checklist — `docs/LAUNCH.md` | ⬜ not started |

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
