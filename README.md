# SPARTON: Intelligence for small e-commerce sellers

**Sparton Intelligence** watches your competitors so you do not have to.

Add your shop URL. SPARTON discovers the competitors you are actually competing
with, crawls them on a schedule, diffs their catalogues week over week, and
writes a plain-English report of what changed, with a link to the evidence for
every claim.

- **Price changes**, who moved, by how much, in which direction.
- **New and removed products**, assortment growth and shrinkage.
- **Stock and availability**, who is quietly out of their best sellers.
- **A weekly AI-written report**, the numbers are computed by our diff engine;
  the AI writes the summary. It never invents a price.

**Status: feature-complete, pre-launch.** 581 tests pass, including 29 that drive
a real browser through signup, the product loop and tenant isolation. What is left
needs real accounts and money (a live Stripe charge, SMTP delivery, a production
deploy) and is listed step by step in [docs/LAUNCH.md](docs/LAUNCH.md).

## Plans

| | Free | Pro | Business |
|---|---|---|---|
| Price | €0 | €29 / month | €79 / month |
| Your shops | 1 | 3 | 10 |
| Competitors tracked | 3 | 15 | 50 |
| Crawl frequency | weekly | daily | twice a day |
| Report history | 1 month | 12 months | 36 months |

Plans are enforced server-side on every mutating route, and granted only by a
signature-verified Stripe webhook, never by a browser redirect. Prices live in
one place (`app/billing/plans.py`) and the landing page reads them from the API.

## Security, briefly

- **Tenant isolation** on every query; another account's data answers 404, not 403.
- **The crawler can't be pointed inward:** public addresses only, re-checked on every
  redirect hop and against the address the connection actually reached (DNS
  rebinding), bodies streamed and capped, `robots.txt` honoured.
- **Stripe webhooks:** HMAC over the raw body, replay window, idempotent events.
- **Auth:** scrypt password hashes, hashed session tokens, rate-limited login, signup
  and reset, with client IPs only taken from trusted proxies (`FORWARDED_ALLOW_IPS`).
- Details and the reasoning behind each choice: [docs/DECISIONS.md](docs/DECISIONS.md).

---

## Requirements

| Component | Notes |
|---|---|
| Python 3.11+ | 3.12+ recommended |
| PostgreSQL | Production. SQLite for local dev and tests. |
| Redis | Production: queue transport + shared rate limits. Optional locally. |
| OpenRouter API key | The only external service the product calls. |

ComfyUI, Ollama and a GPU are **not** required. They power experimental domains
that sit behind feature flags and are not part of the product.

---

## Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env               # then set OPENAI_API_KEY (see below)

# 4. Create the database schema
alembic upgrade head

# 5. Launch
python start_sparton.py            # http://localhost:8000
```

| URL | Purpose |
|---|---|
| `/` | Public landing page |
| `/app/` | Customer dashboard |
| `/docs` | Interactive OpenAPI reference |
| `/live`, `/ready` | Liveness and readiness probes |
| `/health`, `/metrics` | Diagnostics (require authentication) |

### Launcher options

```text
python start_sparton.py [--host HOST] [--port PORT] [--reload]
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address (`0.0.0.0` to expose on the network) |
| `--port` | `8000` | Listen port (conflicts are detected and reported) |
| `--reload` | off | Auto-reload on code changes (development) |

### Windows (one click)

Double-click **`start_sparton.bat`**, or run `start_sparton.bat` from a terminal.

### Docker

```bash
cp .env.example .env      # set POSTGRES_PASSWORD and APP_URL at minimum
GH_TOKEN=<read token for sklsp/shopfeed> docker build --secret id=gh_token,env=GH_TOKEN --no-cache-filter shopfeed -t sparton:latest .
docker compose up -d
```

The token is a BuildKit secret: it installs the private `shopfeed` feed reader
and never lands in an image layer. Without it the build still succeeds, warns,
and competitor prices come from the HTML crawl instead of the exact feed.
Details: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#building-the-image-the-private-shopfeed-library).

Postgres 16, Redis 7, the API and the queue worker. Migrations run on start, so
there is no separate step to forget. Full runbook, the settings that actually
matter, and a security checklist: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Configuration

Everything is environment driven. Never commit a `.env`; `.env.example` and
`.env.production.example` are the templates.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://…` in production, `sqlite:///./sparton.db` locally |
| `REDIS_URL` | Enables the Redis queue transport and cross-replica rate limits |
| `LLM_PROVIDER` | `openai_compatible` (OpenRouter), `ollama`, or `test` |
| `OPENAI_BASE_URL` | `https://openrouter.ai/api/v1` |
| `OPENAI_API_KEY` | **Your OpenRouter key.** Never hardcoded, never committed. |
| `LLM_MODEL_STRONG` | Model for the agent and weekly reports |
| `LLM_MODEL_CHEAP` | Model for structured extraction |
| `APP_URL` | Public base URL, used for Stripe redirects and email links |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PRO`, `STRIPE_PRICE_BUSINESS` | Billing |
| `FORWARDED_ALLOW_IPS` | The proxy/load balancer allowed to set the client IP (default `127.0.0.1`) |
| `ENABLED_DOMAINS` | Feature flags; defaults to the product domains only |
| `API_KEY` | Machine principal for server-to-server calls and Prometheus |

`LLM_PROVIDER=test` runs the whole app on a deterministic stub provider, which
is how the test suite runs with no network and no API key.

---

## Architecture

```
                      SPARTON, Intelligence
                             |
                    shop URL ─┴─► competitor discovery
                             |            |
                             |            v
                             |     scheduled crawls
                             |     (robots.txt + SSRF guarded)
                             |            |
                             |            v
                             |      change detection
                             |     (price / assortment / stock)
                             |            |
                             +------------+--► weekly AI report + alerts
```

One FastAPI process serves the API and the static frontend. Long-running work
goes through a durable job queue backed by the database, with Redis as the
transport. Crawling respects `robots.txt` and refuses to fetch private network
addresses.

Full design notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Background jobs

Crawls and report generation are durable `Job` rows executed by a worker.

Locally the API drains its own queue on a background thread
(`EMBEDDED_WORKER=true`, the default), so nothing extra needs to run. In
production set `EMBEDDED_WORKER=false` and run worker replicas:

```bash
python -m workers.worker
```

---

## Frontend

`app/web/` is a static ES-module SPA served directly by the API process,
**no bundler, no `node_modules`, no build step**. Edit a file, reload the
browser.

---

## Testing

```bash
pytest
```

The suite (581 tests) runs against SQLite with the deterministic LLM provider and
the offline hash embedding backend: no network, no API key, no GPU, no Ollama.

`tests/test_browser_smoke.py` drives real Chrome against a real server (landing
page and prices, signup, the product loop, two accounts that must not see each
other). It needs Playwright and skips loudly without it:

```bash
pip install playwright && playwright install chromium
pytest tests/test_browser_smoke.py
```

---

## Project layout

```
app/
  main.py            FastAPI app factory
  llm.py             LLM provider abstraction (OpenRouter / Ollama / test)
  api/               HTTP routes
  core/              config, auth, tenancy, RBAC, jobs, observability
  agent/             Athena agent runtime + tools
  ecommerce/         The product: shops, competitors, changes, reports
  research/          Crawler, extraction, discovery
  documents/         Experimental, feature-flagged
  generation/        Experimental, feature-flagged
  datasets/          Experimental, feature-flagged
  training/          Experimental, feature-flagged
  web/               static SPA (landing page + dashboard)
workers/             standalone job workers
scripts/             seed data, diagnostics
migrations/          Alembic migrations
tests/               pytest suite
docs/                audit, decisions, progress, architecture
```

---

## Documentation

| Document | What it is |
|---|---|
| [docs/AUDIT.md](docs/AUDIT.md) | What actually works, what is broken, what the docs got wrong |
| [docs/PROGRESS.md](docs/PROGRESS.md) | Phase-by-phase launch status and next steps |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every decision, its rationale, and its cost |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design of the platform core and each domain |
| [docs/MIGRATION.md](docs/MIGRATION.md) | How Apollo + Ares became SPARTON |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production deployment (Phase 6) |
| [docs/LAUNCH.md](docs/LAUNCH.md) | Launch checklist and go-to-market plan (Phase 8) |
