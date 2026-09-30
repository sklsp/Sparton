# SPARTON — Audit (Phase 1 baseline)

**Date:** 2026-09-29
**Commit audited:** `842c8cd`
**Method:** every claim below was verified by *running* the code (app import, route
table dump, unauthenticated probe script, Alembic revision inspection) — not by
reading prose.

---

## 1. Verdict

The platform core is **real and works**: auth, multi-tenancy, RBAC, audit log,
durable job queue, a robots-respecting SSRF-guarded crawler, the Athena agent
loop, Prometheus metrics and a no-build SPA all exist and run.

The **product** does not exist. There is no shop onboarding, no competitor
tracking, no change detection, no report, no billing, no landing page, no
container, and the only Alembic revision creates *zero* tables.

This is a working engine with no car bolted to it.

| # | Class | Severity |
|---|---|---|
| A | **Unauthenticated cross-tenant access** — with no `API_KEY` set, every request becomes a synthetic cross-tenant admin | **P0** |
| B | **Unauthenticated endpoints** — 12+ routes have no auth dependency at all, incl. a file-write route | **P0** |
| C | **No schema migration** — `alembic upgrade head` is a no-op; production would boot against an empty database | **P0** |

---

## 2. What actually works (verified)

| Area | Evidence |
|---|---|
| App boots | `create_app()` → 50 OpenAPI paths, 15 route objects |
| Auth | `POST /auth/register` → 201 + session token; scrypt hashes, SHA-256-at-rest opaque session tokens, session expiry |
| Multi-tenancy | `TenantContext.scoped`, `scoped_or_404` (404 not 403) |
| RBAC | `admin > manager > analyst > viewer`, `require_role`, last-admin protection |
| Audit log | Append-only `audit_logs` rows from auth + admin routes |
| Rate limiting | Sliding-window local limiter + Redis fixed-window backend, `reset_limits()` test hook |
| Job queue | DB-backed `jobs` table, atomic `claim_next` via guarded `UPDATE`, retries, stale reclaim, dead-letter, idempotency keys |
| Queue transport | Redis `BRPOPLPUSH` backend + inline backend, handler registry |
| Crawler | `ResponsibleCrawler` — robots.txt respected, SSRF guard resolves DNS and blocks private/loopback/link-local/reserved, per-host delay, depth + page caps, body cap |
| Extraction | JSON-LD `@type: Product` walk + OpenGraph fallback, 0.95/0.65 confidence |
| Discovery | DuckDuckGo HTML provider with correct `uddg` redirect unwrapping |
| Agent | Bounded loop, schema-validated tool args, WRITE→approval gate, strict grounding, `AgentRun`+`AgentStep` trail, resume-after-approval |
| LLM providers | Ollama, OpenAI-compatible, deterministic test provider |
| Observability | Structured stderr logs, correlation IDs, zero-dep Prometheus `/metrics`, optional OTel |
| Frontend | 7-view no-build ES-module SPA, light/dark, toasts, dialogs, `asyncPanel` loading/error states |
| Seed script | `python scripts/seed_demo.py` runs clean: 12 products, 3 stores, 3 opportunities, 3 documents |

**The crawler, tenancy and job system are the genuinely good parts of this
codebase and are what the product should be built on.**

---

## 3. P0 — launch blockers

### A. Unauthenticated cross-tenant access

`app/core/auth/api.py:86-89`:

```python
if not settings.api_key:
    anon = MachineUser()
    anon.email = "anonymous@localhost"
    return anon
```

`API_KEY` has **no default**, so it is empty in any deployment that did not set
it. `MachineUser.organization_id is None`, and every query path treats
`organization_id is None` as *"sees all tenants"*. Verified: with no credentials
at all, `GET /products`, `/intelligence/stores`, `/intelligence/opportunities`,
`/admin/users`, `/agent/runs` and `/documents` all returned **200** with
cross-tenant visibility.

A production deploy that forgets `API_KEY` is not "open dev mode" — it is a
public read/write window on every customer's data.

### B. Endpoints with no auth dependency

Verified anonymous `200`s. Routes whose signature has no `current_user`:

- `/rag/status`, `/rag/debug-query` — **returns the full text of every indexed
  document chunk across every tenant.** The probe response contained literal
---

## 4. P1 — what the docs claim but does not exist

| Claim | Where | Reality |
|---|---|---|
| `app/ecommerce/` package | README:156, ARCHITECTURE:17,73,156,219 | **Does not exist.** E-commerce lives in `app/api/ecommerce.py` (72 lines) + `app/research/`. |
| `docker compose up --build` | README:99-103 | **No `Dockerfile`, no `docker-compose.yml`.** Verified absent. |
| `Ares/DEPLOYMENT.md` | README:174 | **Does not exist** (and `Ares/` is gitignored). Dead link. |
| "One Next.js application (Next 16 / React 19 / Tailwind v4 / shadcn)" | ARCHITECTURE:163-166 | **No Next.js, no React, no `frontend/`.** Reality is a 0-dependency ES-module SPA. |
| `app/integrations/` (Ollama/ComfyUI/search/store connectors) | ARCHITECTURE:184-187 | **Does not exist.** |
| `assets`, `memberships`, `rag_indexes` tables; `projects` API | ARCHITECTURE:146-150 | None exist. `Project` is a model with **zero routes**. |
| `opportunity_evidence` rows | ARCHITECTURE:155 | Model exists, **never written** — `run_investigation` inlines evidence into `Opportunity.evidence` JSON. |
| Feature flags | mission brief | **Do not exist.** |
| Stripe / plans / usage limits / password reset / email verification | mission brief | **Do not exist.** |
| Landing page | mission brief | **Does not exist.** |
| CI | mission brief | **No `.github/`.** |

`docs/MIGRATION.md` still shows phases 2-10 as "pending"/"in progress" although
the code for most of them was merged. The table is stale, not a plan.

---

## 5. P2 — correctness and hygiene bugs

| # | Location | Bug |
|---|---|---|
| 1 | `app/research/intelligence.py:201` | `select(Product.title)` for `local_names` has **no org filter** — cross-tenant titles leak into opportunity scoring. |
| 2 | `app/research/intelligence.py:47-80` | `_upsert_product` matches on `source_url` **globally**, so two tenants tracking the same competitor **share one `ExternalProduct` row**. Tenant isolation breach. |
| 3 | `app/api/knowledge.py:227` | `organization_id.in_([org, None])` — SQL `IN` never matches `NULL`, so global prompt templates are **invisible to every tenant**. |
| 4 | `scripts/seed_demo.py:206` | `f"{abs(hash(name)):064x}"[:64]` — `hash()` is salted per process, so re-seeding in a new process writes a *different* content hash. Not idempotent as documented. |
| 5 | `app/core/tenancy/context.py:42` | `column_descriptions[0]["entity"].organization_id` breaks on `select(func.count(...))` and on joins. |
| 6 | `app/core/auth/api.py:87-89` | `MachineUser()` is a **shared mutable class attribute singleton**; `anon.email = ...` mutates the class, leaking between requests. |
| 7 | `app/api/auth.py:37` | `/auth/register` has **no rate limit** (only `/auth/login` does). Signup-spam / DB-fill vector. |
| 8 | `app/api/auth.py:46` | `Organization.name` is globally unique and `/auth/register` returns **409 "ask an admin for an invite"** on collision. On a public SaaS any user can DoS others by claiming a common shop name. |
| 9 | `app/llm.py:273-367` | `OpenAICompatibleProvider` has **no retries, no backoff, no HTTP-Referer/X-Title, no token-usage capture, no `list_models()`**, and `is_available()` is just `bool(self.api_key)` — it never contacts the server. Default `OPENAI_BASE_URL` is `https://api.openai.com/v1`, not OpenRouter. |
| 10 | `app/core/config.py:70-72` | `CORS_ORIGINS` ships `https://sparton.vercel.app` — a domain we do not own. |
| 11 | `app/llm.py:258` | `OllamaProvider.is_available()` does a synchronous 3s HTTP GET; `/health` calls it inline, so every health probe can block 3s. |
| 12 | `app/main.py:55` | `Base.metadata.create_all` is gated on `settings.is_sqlite`, so the SQLite dev DB and the PostgreSQL prod DB have **different provenance**. |
| 13 | `app/api/agent_api.py:198` | `__import__("app.core.database.models", fromlist=["utcnow"]).utcnow()` — inline import gymnastics. |
| 14 | `tests/conftest.py:25-33` | `db_session` does `drop_all`/`create_all` on the **process-global engine**; any test that forgets the fixture corrupts the next. |

---

## 6. Test suite reality

- **19 tests**, 1 file, one `TestXxx` class per domain.
- Covers: auth (4), knowledge/RAG (3), catalog (1), agent (3), approvals (1),
  create/ComfyUI/datasets/training (4), intelligence (2), admin (2).
- **Zero tests** for: the crawler (its SSRF/robots claims are untested),
  `app/research/extraction.py`, `app/research/discovery.py`, `app/core/jobs/*`,
  `app/core/security/rate_limit.py`, `app/core/tenancy/*`, `app/llm.py` HTTP
  behaviour, observability, and every other API route.
- Tests pass only because they exercise the happy path with a stubbed provider.

---

## 7. Plan of record

| Phase | Scope |
|---|---|
| 1 | This audit; make the app boot and `pytest` be genuinely green; correct the docs |
| 2 | Production-grade OpenRouter provider (retries, timeouts, headers, per-org token usage) |
| 3 | `app/ecommerce/` — the real product loop: shop → competitors → scheduled crawl → change detection → report + alerts |
| 4 | Public SaaS layer: signup, email verification, password reset, Stripe, plans enforced server-side, feature flags |
| 5 | Public landing page + customer dashboard for the product loop |
| 6 | Dockerfile, compose, real migrations, health checks, CI, `docs/DEPLOYMENT.md` |
| 7 | Test expansion to every route + tenant isolation + webhooks + plan limits; Playwright smoke; security review |
| 8 | `docs/LAUNCH.md` |

See `docs/DECISIONS.md` for the decisions taken and `docs/PROGRESS.md` for status.

  document body text.
- `/comfyui/workflows` (GET, POST **and** DELETE) — POST writes JSON files into
  the server's `workflows/` directory.
- `/comfyui/status`, `/comfyui/validate-generation`, `/comfyui/generate`
- `/training/status`, `/training/presets`, `/training/hardware` — the hardware
  probe leaks the host's GPU inventory.
- `/metrics`, `/tools` — internal metric names and full tool schemas.

### C. The Alembic revision creates nothing

`migrations/versions/f4d75c8de498_sparton_unified_schema.py` is
`upgrade() -> pass` with **0** `op.create_table` calls. Every model exists *only*
via `Base.metadata.create_all()`, which `app/main.py` runs **only when the
database is SQLite**. Deploying to PostgreSQL as the docs instruct yields a
database with zero tables and an app that 500s on first query.

### D. `pytest` hangs

`tests/test_integration_pass.py` collects 19 tests. Test 5
(`test_document_upload_and_rag_query`) never returns: `EmbeddingClient` tries
Ollama, gets a 404 from `localhost:11434/api/embed`, falls back to
`sentence-transformers`, and blocks on a HuggingFace model download with no
timeout. The suite is not green — it is **incomplete**, and README.md:145
("no Ollama, GPU, or network required") is false.

### E. No product

Nothing in the codebase does any of: register a shop URL, discover competitors,
re-crawl on a schedule, diff prices, write a report, alert a user, or take money.

---

## Addendum — Phase 7 external security review

Found after the initial audit, in the code that audit had already blessed. The
crawler entry above says "SSRF guard resolves DNS and blocks
private/loopback/link-local/reserved", and that was true of the code and false
of the behaviour: the guard ran once, on the first URL.

| # | Finding | Severity | Now |
|---|---------|----------|-----|
| 1 | httpx built with `follow_redirects=True`; the address check ran only on the entry URL, so a `Location` header reached loopback or `169.254.169.254` | High | `follow_redirects=False`, redirects followed by hand (max 5), address checked on every hop including `robots.txt` |
| 2 | DNS rebinding: the name could resolve public at check time and private at connect time | High | the connected peer address is read from the response and checked as well |
| 3 | `response.content[:max_body_bytes]` buffered the whole body before truncating, so the cap bounded memory *after* the download | Medium | `iter_bytes()` with the read stopping at the cap; tested against an unbounded chunked response |
| 4 | address check was a denylist, missing `100.64.0.0/10` and `192.0.0.0/24` | Medium | `not address.is_global` (plus multicast), 12 non-public forms pinned by test |
| 5 | `client_key` read `X-Forwarded-For` unconditionally, and uvicorn ran with `--forwarded-allow-ips='*'` | High | header honoured only from a peer in `FORWARDED_ALLOW_IPS` (default loopback); uvicorn flag reads the same variable |
| 6 | `GET /billing/invoices` filtered `organization_id == None`, i.e. `IS NULL`, returning every unscoped invoice | Medium | empty list before any query when the caller has no organization |
| 7 | both default model ids (`claude-3.5-sonnet`, `gemini-2.0-flash-001`) absent from OpenRouter's catalogue | Medium | verified-current ids; a test fails if a retired id is assigned again |
| 8 | `alembic upgrade head` in the container CMD races across API replicas | Low | documented as a one-off pre-deploy step; pinned by test |

The common shape of 1, 2 and 5 is worth recording: each was a check that
existed, was correct as written, and was defeated by a boundary the author had
not considered — the second URL, the socket, the request's arrival path. A
guard is only as good as the thing it is applied to, so each fix here is a test
about the *response* rather than about the function.
