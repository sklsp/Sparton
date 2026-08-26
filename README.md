# SPARTON

**One unified AI platform with multiple domains** — built on two proven foundations:

| Foundation | Was | Becomes |
|---|---|---|
| **Apollo** | AI Document Agent (RAG, datasets, LoRA training, ComfyUI) | Knowledge + Create domains |
| **Ares** | AI E-Commerce Agent (auth, tenancy, jobs, intelligence) | Platform core + Intelligence domain |

## Domains

```
                        SPARTON
                           │
                     ATHENA ─ Core AI Agent
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
     HECTOR              ARES              APOLLO
   Documents/RAG      E-Commerce         Generation
        │                  │                  │
        │              ODYSSEUS              │
        │              Research               │
        └──────────────────┼──────────────────┘
                           │
                         ARGO ─ Datasets
                           │
                      LEONIDAS ─ LoRA Training
                           │
                       APOLLO ─ ComfyUI
```

## Quick start (development)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # then edit as needed
python start_sparton.py         # API + dashboard on http://localhost:8000
```

| URL | What |
|---|---|
| `/dashboard/` | The SPARTON dashboard (`/` redirects here) |
| `/docs` | Interactive OpenAPI reference |
| `/health`, `/metrics` | Health probe and Prometheus counters |

Populate a demo workspace to click through with real data:

```bash
python scripts/seed_demo.py
```

Full stack (PostgreSQL + Redis + API + workers):

```bash
docker compose up --build
```

## Dashboard

`app/web/` is a static ES-module SPA served by the API process itself — **no
build step, no bundler, no `node_modules`**. Edit a file, reload the browser;
that is the whole development loop.

```
app/web/
  index.html      shell + first paint
  styles.css      design tokens and every component
  api.js          the only module that speaks HTTP
  ui.js           h(), formatters, toasts, dialogs, loading/empty/error states
  app.js          session, routing, navigation chrome
  routes.js       route table (views are lazy imports)
  views/          one module per route
```

Node is optional, and used only to parse-check those modules:

```bash
sh scripts/check_web.sh
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [DEPLOYMENT.md](DEPLOYMENT.md).

## Status

🚧 Under active migration from Apollo + Ares. See [docs/MIGRATION.md](docs/MIGRATION.md).
