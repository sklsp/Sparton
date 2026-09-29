# SPARTON production image.
#
# Two stages so the runtime layer carries no compiler toolchain: a smaller image
# is a smaller patch surface, and a breach of the running container has nothing
# useful to build a exploit with.
#
# The app is a single ASGI process behind `workers/worker.py` for the crawl and
# report queue. Both run from the same image; compose distinguishes them by
# command. See docs/DEPLOYMENT.md.

# ---------------------------------------------------------------- builder
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build only the wheels we need, then copy them out.
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

# The app binds 0.0.0.0 inside the container; publishing the port to a host
# interface is the orchestrator's job, not the app's.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SPARTON_HOST=0.0.0.0 \
    SPARTON_PORT=8000

# curl is used only by the healthcheck below. Nothing else is installed: no
# compiler, no git, no package-manager conveniences in the running image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app
COPY app/ ./app/
COPY workers/ ./workers/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/
COPY alembic.ini .
COPY requirements.txt .

# Run as an unprivileged user. The app writes only to /app/data (the email
# outbox) and needs no other write access.
RUN useradd --system --create-home --uid 10001 sparton \
    && mkdir -p /app/data \
    && chown -R sparton:sparton /app
USER sparton

EXPOSE 8000

# The health router is mounted unprefixed, so the liveness path is `/live`.
# It stays open to unauthenticated callers by design: it reports that the
# process is up and deliberately says nothing about the database, the LLM
# provider or any tenant data. `/ready` is the stricter check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${SPARTON_PORT}/live" || exit 1

# Migrations run here, at start, not as a separate deploy step: a schema that
# lags the code is the single most common way an app fails to boot, and
# running them on boot means the two can never disagree.
#
# `|| true` would hide a failed migration behind a running process serving
# requests against the wrong schema, so it is deliberately absent.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host ${SPARTON_HOST} --port ${SPARTON_PORT} --proxy-headers --forwarded-allow-ips='*'"]
