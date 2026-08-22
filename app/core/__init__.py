"""Shared platform core.

One implementation of each cross-cutting concern, sourced from the
stronger of Apollo/Ares (primarily Ares for enterprise infrastructure):

- config:        single pydantic-settings configuration system
- auth:          opaque session tokens (SHA-256 stored), scrypt passwords
- tenancy:       TenantContext + per-query organization scoping
- rbac:          admin > manager > analyst > viewer capability levels
- database:      SQLAlchemy 2.0 engine/session, JSONB-variant columns
- jobs:          durable DB-backed job state machine (idempotency, retries)
- queue:         Redis transport (BRPOPLPUSH + delayed zset + dead-letter),
                 inline fallback for dev/tests
- storage:       asset/file abstraction with provenance
- observability: structured logging, Prometheus metrics, correlation IDs,
                 optional OpenTelemetry tracing
- security:      SSRF guards, path safety, secure subprocess handling
- audit:         append-only AuditEvent log
"""
