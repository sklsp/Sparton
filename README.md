# SPARTON

**A unified, self-hosted AI platform for knowledge, research, e-commerce intelligence, and image generation.**

SPARTON merges two proven codebases — **Apollo** (AI document agent: RAG, datasets, LoRA training, ComfyUI generation) and **Ares** (AI e-commerce agent: auth, multi-tenancy, durable jobs, competitive intelligence) — into a single FastAPI application with a built-in dashboard. No build step, no microservice sprawl: one process serves the API, the UI, and (by default) the background workers.

---

## Highlights

- **Athena agent** — a single LLM-driven agent runtime with namespaced tools across every domain (`documents.*`, `ecommerce.*`, `research.*`, `generation.*`, `datasets.*`, `training.*`). The backend is authoritative: tool arguments are schema-validated, WRITE actions require human approval, and answers must be grounded in real tool results.
- **Hector (Documents & RAG)** — PDF/DOCX/TXT ingestion, FAISS vector search over Ollama or sentence-transformer embeddings, incremental indexing, RAG answers with citations.
- **Ares (E-commerce)** — store connections, catalog/inventory/orders, competitor discovery and crawling, explainable opportunity scoring with an approval workflow.
- **Odysseus (Research)** — web search, domain discovery, and long-running investigations on a durable job system.
- **Apollo (Generation)** — ComfyUI workflow library with logical input mapping and LoRA injection.
- **Argo (Datasets)** — project-scoped image datasets with validation, deduplication, and AI auto-captioning.
- **Leonidas (Training)** — VRAM-aware LoRA training preflight and AI Toolkit orchestration.
- **Platform core** — session auth + API keys, scrypt hashing, multi-tenancy, RBAC, audit log, Prometheus metrics, structured logging, correlation IDs.

## Architecture

```
                        SPARTON
                           |
                     ATHENA - Core AI Agent
                           |
        +------------------+------------------+
        |                  |                  |
     HECTOR              ARES              APOLLO
   Documents/RAG      E-Commerce         Generation
        |                  |                  |
        |              ODYSSEUS              |
        |              Research               |
        +------------------+------------------+
                           |
                         ARGO - Datasets
                           |
                      LEONIDAS - LoRA Training
                           |
                       APOLLO - ComfyUI execution
```

These are logical modules inside one application, not separate services. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## Requirements

| Component | Notes |
|---|---|
| Python 3.11+ | 3.12 recommended |
| [Ollama](https://ollama.com) | Default LLM/embedding provider; optional but recommended |
| ComfyUI | Optional - image generation only |
| PostgreSQL + Redis | Production; SQLite + inline queue fallback used locally |

## Quick Start

### Windows (one click)

Double-click **`start_sparton.bat`**, or run it from a terminal:

```bat
start_sparton.bat
```

It prefers the Apollo virtualenv if present and falls back to system Python.

### Any platform

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure (optional - sensible defaults are used without a .env)
copy .env.example .env             # then edit as needed

# 4. Launch
python start_sparton.py            # http://localhost:8000
```

### Launcher options

```text
python start_sparton.py [--host HOST] [--port PORT] [--reload]
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address (`0.0.0.0` to expose on the network) |
| `--port` | `8000` | Listen port (the launcher detects and reports port conflicts) |
| `--reload` | off | Auto-reload on code changes (development) |

Without extra configuration the launcher uses SQLite (`sparton.db`), Ollama at `http://localhost:11434`, and an embedded worker thread.

### Docker (full stack)

PostgreSQL + Redis + API + dedicated workers:

```bash
docker compose up --build
```

## What to open first

| URL | Purpose |
|---|---|
| [`/dashboard/`](http://localhost:8000/dashboard/) | SPARTON dashboard (`/` redirects here) |
| [`/docs`](http://localhost:8000/docs) | Interactive OpenAPI reference |
| `/health`, `/metrics` | Health probe and Prometheus counters |

To explore with realistic data:

```bash
python scripts/seed_demo.py
```

## Background jobs

Research crawls and ComfyUI generations are durable `Job` rows executed by a worker. Locally, the API drains its own queue on a background thread (`EMBEDDED_WORKER=true`, the default) so nothing extra needs to run.

In production, set `EMBEDDED_WORKER=false` and scale workers independently against Redis:

```bash
python -m workers.worker
```

## Frontend

The dashboard in `app/web/` is a static ES-module SPA served directly by the API process - **no bundler, no `node_modules`, no build step**. Edit a file, reload the browser; that is the whole development loop.

Optionally syntax-check the modules with Node:

```bash
sh scripts/check_web.sh
```

## Testing

```bash
pytest
```

The test suite runs against SQLite with a deterministic LLM provider - no Ollama, GPU, or network required.

## Project layout

```
app/
  main.py            FastAPI app factory
  agent/             Athena agent runtime + tools
  api/               HTTP routes
  core/              config, auth, tenancy, RBAC, jobs, observability
  documents/         Hector - ingestion + RAG
  ecommerce/         Ares - stores + intelligence
  research/          Odysseus - web investigation
  generation/        Apollo - ComfyUI workflows
  datasets/          Argo - dataset management
  training/          Leonidas - LoRA training
  web/               static SPA dashboard
workers/             standalone job workers
workflows/           ComfyUI workflow JSON + input maps
scripts/             seed data, diagnostics
migrations/          Alembic migrations
tests/               pytest suite
docs/                architecture & migration notes
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) - domain boundaries, platform core, agent design
- [Migration](docs/MIGRATION.md) - how Apollo + Ares became SPARTON
- [Deployment](Ares/DEPLOYMENT.md) - production deployment notes

## Status

Under active development. See [docs/MIGRATION.md](docs/MIGRATION.md) for progress.
