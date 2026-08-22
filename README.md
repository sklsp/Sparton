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
uvicorn app.main:app --reload   # API at http://localhost:8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev                     # http://localhost:3000
```

Full stack (PostgreSQL + Redis + API + workers):

```bash
docker compose up --build
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [DEPLOYMENT.md](DEPLOYMENT.md).

## Status

🚧 Under active migration from Apollo + Ares. See [docs/MIGRATION.md](docs/MIGRATION.md).
