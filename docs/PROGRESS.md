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
| 1 | Audit & baseline — audit doc, app boots, `pytest` green, docs corrected | 🔄 in progress |
| 2 | OpenRouter — production provider: retries, timeouts, headers, token usage | ⬜ not started |
| 3 | Product core — `app/ecommerce/` shop→competitors→crawl→changes→report | ⬜ not started |
| 4 | SaaS layer — signup, verification, reset, Stripe, plans, feature flags | ⬜ not started |
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
