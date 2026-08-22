# SPARTON Architecture

> One unified AI platform with multiple domains, built on two proven
> foundations: **Apollo** (AI Document Agent) and **Ares** (AI E-Commerce Agent).

## 1. Conceptual architecture

```
                        SPARTON
                           │
                     ATHENA ─ Core AI Agent (app/agent)
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
     HECTOR              ARES              APOLLO
   Documents/RAG      E-Commerce         Generation
   (app/documents)   (app/ecommerce)    (app/generation)
        │                  │                  │
        │              ODYSSEUS              │
        │           Research                 │
        │          (app/research)            │
        └──────────────────┼──────────────────┘
                           │
                         ARGO ─ Datasets (app/datasets)
                           │
                      LEONIDAS ─ LoRA Training (app/training)
                           │
                       APOLLO ─ ComfyUI execution
```

These are **logical modules inside one application**, not separate services.

## 2. Shared platform core (`app/core`)

One implementation of every cross-cutting concern. Where Apollo and Ares
duplicated functionality, the stronger implementation was chosen and the
weaker one's useful behavior merged into it.

| Concern | Source | Design |
|---|---|---|
| Configuration | Ares | Single pydantic-settings `Settings`, all env-driven |
| Authentication | Ares | Opaque session tokens (SHA-256 at rest), scrypt password hashing, revocable sessions |
| Tenancy | Ares | `TenantContext` from authenticated user; per-query org scoping; `scoped_or_404` (404, not 403 — no existence leaks) |
| RBAC | Ares | `admin > manager > analyst > viewer`; `require_role` dependency |
| Database | Ares | SQLAlchemy 2.0 typed ORM + Alembic; PostgreSQL in prod, SQLite for dev/tests; JSON columns with JSONB variant |
| Jobs | Ares | Durable DB-backed state machine: idempotency keys, retries w/ backoff, stale reclaim, dead-letter, cancellation |
| Queue transport | Ares | Redis lists (`BRPOPLPUSH`) + delayed zset + dead-letter list; inline fallback when Redis absent |
| Workers | Ares | Standalone worker process (`workers/`), horizontally scalable replicas |
| LLM providers | Ares + Apollo | One provider abstraction: Ollama, OpenAI-compatible, deterministic test provider |
| Storage | Apollo paths + new | Asset abstraction with provenance chains; path-safety helpers (`safe_join`, sanitization) |
| Observability | Ares | Structured logging, zero-dependency Prometheus `/metrics`, correlation-ID middleware, optional OTel tracing |
| Audit | Ares | Append-only audit log (actor, org, action, resource, outcome, correlation ID) |
| Projects | New | Central cross-domain workspace entity |

## 3. Domain boundaries

Each domain owns its models, services, and routes. Domains never import each
other's internals directly — cross-domain behavior flows through:

1. **Projects** — the central workspace entity linking documents, research,
   stores, datasets, training runs, LoRAs, workflows, generated assets.
2. **The Athena agent** — namespaced tools exposed across domains.
3. **Asset provenance** — every generated artifact records its lineage.

### documents (Hector)
Ingestion (PDF/DOCX/TXT), sentence-aware chunking, FAISS flat IP index over
L2-normalized embeddings (Ollama `nomic-embed-text` first,
`sentence-transformers/all-MiniLM-L6-v2` fallback), incremental indexing keyed
by content hash + embedding model, corruption quarantine, RAG answers with
citations.

### ecommerce (Ares)
Store connections (provider protocol), catalog/inventory/orders, competitor
discovery (DuckDuckGo HTML), robots-respecting SSRF-guarded crawler,
intelligence pipeline → explainable opportunity scoring → evidence rows →
approval workflow for WRITE actions.

### research (Odysseus)
Web search, domain discovery, crawling, source provenance, long-running
investigations on the shared job system.

### generation (Apollo/ComfyUI)
Workflow library as file pairs (`<id>.json` API-format graph +
`<id>.map.json` logical-input mapping), input injection, submit/poll/download,
LoRA injection into workflows.

### datasets (Argo)
Project-scoped image datasets (image + same-stem `.txt` caption pairing),
validation (decodability, resolution bounds, exact SHA-256 duplicates,
multi-signal near-duplicates via aHash Hamming bands, caption checks, weighted
quality score), Ollama-vision auto-captioning with human editing.

### training (Leonidas)
Training projects, presets (character/style/product/concept × arch defaults),
vendor-agnostic hardware detection, VRAM-aware preflight verdicts
(ok/heavy/risky/unsupported), AI Toolkit subprocess orchestration, run history,
trained-LoRA discovery.

## 4. Athena agent architecture (`app/agent`)

One runtime for all domains:

```
User request
  → auth (session/API key) → RBAC → tenant context → project access check
  → agent loop (bounded iterations):
      understand → decide (LLM proposes tool call)
      → validate args against tool schema      ── reject → structured error
      → permission/approval gate               ── WRITE tools pause for approval
      → execute tool server-side               ── bounded retry on invalid args
      → strict grounding (no fabricated facts; final answer must cite tool results)
  → persisted AgentRun + ToolCall steps (audit + resume by any worker)
```

Principles:
- **The backend is authoritative.** The LLM is not trusted.
- Invalid tool arguments → validate → reject → structured error → bounded
  retry → successful execution. Never *tool failure → hallucinated answer*.
- Tools are namespaced: `documents.*`, `ecommerce.*`, `research.*`,
  `generation.*`, `datasets.*`, `training.*`, `models.*`, `projects.*`.
- READ/WRITE classification is the approval boundary.

## 5. Job architecture

All long-running operations use one system:

```
DocumentIndexJob · ResearchJob · CrawlJob · DatasetValidationJob · CaptionJob
· TrainingJob · ComfyGenerationJob · ReportJob
```

- **Durable state**: DB rows (QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED),
  atomic claim via guarded UPDATE (duplicate delivery = no-op).
- **Transport**: Redis `BRPOPLPUSH` processing lists (at-least-once,
  exactly-one-consumer), delayed zset for retries, dead-letter list,
  stale reclaim after 300 s. Inline backend for dev/tests.
- **Retries**: worker-level exponential backoff (5 s × 3ⁿ capped 600 s),
  max 3 attempts → dead-letter.
- **Idempotency**: SHA-256 signature keys, org-prefixed.
- **Observability**: progress, correlation IDs, metrics.

## 6. Database strategy

One coherent schema (Alembic-managed). Common entities:
`organizations ← users ← sessions`, `memberships`, `projects`, `assets`,
`jobs`, `agent_runs ← agent_steps / tool_calls / approval_requests`,
`audit_logs`, `integrations`.

Domain entities:
- documents: `documents ← document_versions ← document_chunks`, `rag_indexes`
- datasets: `datasets ← dataset_images`
- training: `training_projects ← training_runs`, `loras`
- ecommerce: `stores ← products ← inventory/orders`, `external_stores ←
  external_products ← product_snapshots`, `competitors`
- research/intelligence: `research_jobs ← opportunities ← opportunity_evidence`

Naming conflicts resolved deliberately (e.g., Apollo's filesystem "projects"
become `training_projects`; Ares' `research_jobs` generalize to the shared
`jobs` table plus domain payload). Every tenant-owned object carries
`organization_id`; cross-tenant access is impossible through ID manipulation
(scoped queries + generic 404s).

## 7. Frontend strategy

One Next.js application (Ares foundation: Next 16 / React 19 / Tailwind v4 /
shadcn-style components). Navigation:

```
SPARTON
Home · Projects
Intelligence (E-Commerce · Research · Opportunities)
Knowledge (Documents · Collections · Reports)
Create (Images · ComfyUI · Datasets · LoRA Training)
Models (Library · LoRAs · Workflows)
Activity (Jobs · Agent Runs)
Approvals · Admin · Settings
```

Shared shell: auth context (bearer token, 401 handling), project selector,
job status components, toasts, tables/forms, permission-aware UI.

## 8. Integration strategy

One integration layer (`app/integrations`) with connection status, config,
credentials (org-scoped), health, capabilities for: Ollama, ComfyUI,
e-commerce store APIs (provider protocol — mock today, real connectors later),
web search.

## 9. Apollo → SPARTON mapping

| Apollo | SPARTON | Strategy |
|---|---|---|
| `app/core/config.py` | `app/core/config` | MERGE into pydantic-settings |
| `app/core/jobs.py` (threads + jobs.json) | `app/core/jobs` + queue | REWRITE onto shared durable queue |
| `app/core/paths.py`, `persistence.py` | `app/core/storage`, `security` | ADAPT |
| `rag/*` (ingestion/chunking/embeddings/vector_store/service) | `app/documents/rag` | KEEP (proven logic) |
| `services/document_service.py` | `app/documents` | ADAPT to DB + tenancy |
| `services/memory_service.py` | conversations under projects | ADAPT |
| `services/dataset_validation.py` | `app/datasets/validation` | KEEP |
| `services/lora_dataset_service.py` | `app/datasets`, `app/training` | SPLIT along domain line |
| `services/lora_training_service.py`, `run_history.py`, `hardware.py`, `training_preflight.py` | `app/training` | KEEP, re-home onto shared jobs |
| `clients/comfyui_client.py`, `services/comfyui_service.py`, `workflows/*.json` | `app/generation/comfyui` | KEEP |
| `clients/ollama_client.py` | `app/integrations/ollama` | MERGE with Ares LLM abstraction |
| `ai_agent/*` (legacy CLI) | superseded by Athena | REMOVE (behavior absorbed) |
| `launcher.py` (cloudflared) | not carried forward | REMOVE (incompatible with auth model) |
| tests (~224 real) | `tests/` | MIGRATE meaningful ones; drop vacuous print-scripts |

## 10. Ares → SPARTON mapping

| Ares | SPARTON | Strategy |
|---|---|---|
| `db/identity.py`, `api/auth.py` | `app/core/auth`, `database` | KEEP (stronger than Apollo's none) |
| `api/tenancy.py` | `app/core/tenancy` | KEEP |
| `jobs/queue.py`, `transport.py`, `worker.py` | `app/core/jobs`, `queue`, `workers/` | KEEP |
| `observability/*` | `app/core/observability` | KEEP |
| `services/rate_limit.py` | `app/core/security` | KEEP (declare redis dep explicitly) |
| `agent/engine.py`, `runner.py`, `grounding.py` | `app/agent/runtime` | EXTEND to multi-domain tools |
| `tools/*` registry | `app/agent/tools` | EXTEND with domain namespaces |
| `intelligence/*` (crawler/discovery/extraction) | `app/ecommerce/crawling`, `app/research` | SPLIT shared crawler into research |
| `services/intelligence*.py`, approvals | `app/ecommerce/intelligence`, `approvals` | KEEP |
| `integrations/base.py` (mock provider) | `app/integrations/ecommerce` | KEEP protocol |
| frontend (`src/`) | `frontend/` | ADAPT into unified shell |
| compose.yaml, Dockerfiles, scripts | root | ADAPT |
| tests (~178) + Playwright specs | `tests/`, `frontend/tests` | MIGRATE |

## 11. Known issues resolved during unification

- Apollo had **zero auth** while being tunneled public-facing → now behind the
  Ares auth/session/RBAC core.
- Apollo CORS `allow_origins=["*"]` + credentials → explicit origin allowlist.
- Ares' undeclared optional deps (`redis`, `opentelemetry`) → declared in
  requirements so deployments don't silently lose features.
- Ares registration allowed self-selecting `admin` role → restricted in SPARTON.
- Apollo's process-local thread job store → durable shared queue.
- Apollo's in-memory prompt templates → persisted.
