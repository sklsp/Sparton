# SPARTON Staged Migration Plan

Sources (read-only): `Apollo/`, `Ares/` in the workspace root.
Destination: this repository. Apollo and Ares are never modified.

## Phase overview

| # | Phase | Commit prefix | Status |
|---|---|---|---|
| 1 | Inspect both repositories | — | ✅ done |
| 2 | Shared core from Ares | `core:` | in progress |
| 3 | Apollo documents/RAG | `documents:` | pending |
| 4 | Ares e-commerce + research | `ecommerce:` / `research:` | pending |
| 5 | Generation + datasets + training | `generation:` `datasets:` `training:` | pending |
| 6 | Unified Athena agent | `agent:` | pending |
| 7 | Unified frontend | `frontend:` | pending |
| 8 | Cross-domain workflows | `integration:` | pending |
| 9 | Testing/security/performance | `test:` | pending |
| 10 | Production verification | `release:` | pending |

## Subsystem migration table

### From Ares

| Source | Destination | Strategy | Dependencies | Tests to preserve |
|---|---|---|---|---|
| `backend/app/db/identity.py` | `app/core/database/models.py` | adapt | SQLAlchemy | test_identity.py (12) |
| `backend/app/api/auth.py` | `app/core/auth/`, `app/api/auth.py` | adapt | tenancy, rate limit | test_auth.py (4), test_identity.py |
| `backend/app/api/tenancy.py` | `app/core/tenancy/` | keep | db | test_tenancy.py (9) |
| `backend/app/jobs/*` | `app/core/jobs/`, `app/core/queue/` | keep | redis, db | test_job_queue.py (6), test_queue_transport.py (7) |
| `backend/app/worker.py` | `workers/worker.py` | adapt | queue | queue tests |
| `backend/app/services/rate_limit.py` | `app/core/security/rate_limit.py` | keep | redis | test_rate_limit.py (3) |
| `backend/app/observability/*` | `app/core/observability/` | keep | otel (optional) | test_observability.py (4), test_tracing.py (3) |
| `backend/app/api/health.py` | `app/api/health.py` | keep | db, llm | observability tests |
| `backend/app/api/admin.py` | `app/api/admin.py` | keep | auth, audit | test_admin.py (12) |
| `backend/app/services/approvals.py` | `app/core/approvals/` → domain use | keep | db, agent | test_approval.py (12) |
| `backend/app/agent/engine|runner|grounding` | `app/agent/runtime/` | extend | llm, tools, jobs | test_agent.py (14), test_adversarial_agent.py (10), test_grounding.py (3) |
| `backend/app/tools/*` | `app/agent/tools/` | extend w/ namespaces | registry | test_tools.py (27) |
| `backend/app/intelligence/crawler.py` | `app/research/crawling/` (+ ecommerce wrapper) | split | httpx | test_intelligence.py SSRF cases |
| `backend/app/intelligence/discovery.py` | `app/research/search/` | keep | httpx | intelligence tests |
| `backend/app/services/intelligence*.py` | `app/ecommerce/intelligence/` | keep | crawler, db, jobs | test_intelligence_api.py (4) |
| `backend/app/integrations/base.py` | `app/integrations/ecommerce/` | keep | — | provider tests |
| `src/` (Next.js frontend) | `frontend/` | adapt into unified shell | API | Playwright: smoke.spec.ts, auth.spec.ts |

### From Apollo

| Source | Destination | Strategy | Dependencies | Tests to preserve |
|---|---|---|---|---|
| `app/rag/ingestion.py` | `app/documents/parsers/` | keep | pypdf, python-docx | integration_pass tests |
| `app/rag/chunking.py` | `app/documents/rag/chunking.py` | keep | — | RAG tests |
| `app/rag/embeddings.py` | `app/documents/rag/embeddings.py` | keep | ollama, sentence-transformers | embedding tests |
| `app/rag/vector_store.py` | `app/documents/rag/vector_store.py` | keep | faiss | persistence tests |
| `app/rag/service.py` | `app/documents/rag/service.py` | adapt to projects/orgs | jobs, storage | RAG persistence tests |
| `services/document_service.py` | `app/documents/` | adapt to DB models | db, tenancy | endpoint tests |
| `services/dataset_validation.py` | `app/datasets/validation/` | keep | pillow | lora dataset tests |
| `services/lora_dataset_service.py` | `app/datasets/` + `app/training/` | split | storage | test_lora.py subset |
| `services/lora_training_service.py` | `app/training/lora/` | re-home onto shared jobs | ai-toolkit, jobs | test_lora.py subset |
| `services/hardware.py`, `training_preflight.py` | `app/training/preflight/` | keep | psutil-ish probes | preflight tests |
| `services/run_history.py` | `app/training/monitoring/` | adapt to DB | db | run-history tests |
| `clients/comfyui_client.py` | `app/generation/comfyui/client.py` | keep | requests/httpx | test_comfyui.py (36) |
| `services/comfyui_service.py` | `app/generation/comfyui/service.py` | adapt to shared jobs | jobs | generation API tests (25) |
| `workflows/*.json` + `.map.json` | `workflows/` | copy as-is | — | workflow validation tests |
| `core/paths.py` safety helpers | `app/core/security/paths.py` | keep | — | test_security.py (21), hardening tests (33) |
| `clients/ollama_client.py` | `app/integrations/ollama/` | merge with Ares LLM layer | requests | ollama client tests |
| `ai_agent/*` legacy CLI | — | remove (absorbed by Athena) | — | answer-mode heuristics only |

## Data migration utilities (planned)

- `scripts/migrate_apollo.py`: import `data/documents.json`, rebuild/reconnect
  FAISS indexes when embedding model matches (`meta.json` check; deliberate
  rebuild otherwise), import LoRA projects/runs/LoRAs, register generated
  assets with provenance.
- `scripts/migrate_ares.py`: export Ares Postgres tables → SPARTON schema
  (users/orgs/sessions preserved with hash compatibility; stores, products,
  opportunities, evidence, approvals, audit).
- Model/file paths preserved via `MODEL_DIR`, `LORA_DIR`, `COMFYUI_DIR`,
  `DATA_DIR` env configuration; helpers only relocate when required.

## Test baseline (recorded before migration)

- Apollo: ~224 real pytest functions (several `test_*.py` files are vacuous
  print-scripts and will be dropped deliberately).
- Ares: ~178 backend pytest functions across 20 files; 5 Playwright specs
  (smoke + auth).

Baseline rule: meaningful coverage survives; architecture-driven rewrites are
documented here per phase.
