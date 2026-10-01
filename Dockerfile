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

# The product's dependencies, and only those. `requirements-experimental.txt`
# holds sentence-transformers, faiss and the PyTorch stack for the feature-
# flagged documents/generation/datasets/training domains; installing them by
# default is what made this image 9.8 GB, for code a default deployment cannot
# reach. Those routers are imported inside their feature-flag branches, so
# nothing the product imports touches them.
#
# Pass --build-arg INSTALL_EXPERIMENTAL=1 to get them anyway (a ~9 GB image).
ARG INSTALL_EXPERIMENTAL=0
WORKDIR /build
COPY requirements.txt requirements-experimental.txt ./
RUN pip install --prefix=/install -r requirements.txt \
 && if [ "$INSTALL_EXPERIMENTAL" = "1" ]; then \
      pip install --prefix=/install -r requirements-experimental.txt; \
    fi

# shopfeed reads a webshop's own product feed instead of parsing HTML: exact
# prices and no LLM tokens. It is a PRIVATE repository, so there is no git URL to
# put in requirements.txt -- it is installed from a local path, and the
# application degrades to the HTML crawl if it is missing. See docs/LAUNCH.md.
# RUN pip install --prefix=/install -e /opt/shopfeed

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
COPY requirements.txt requirements-experimental.txt ./

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
#
# --forwarded-allow-ips is NOT '*'. With '*', uvicorn rewrites request.client
# from whatever X-Forwarded-For the caller sent, so any client can claim to be
# any address and per-client rate limiting becomes decorative. The list must
# name the real proxy; see FORWARDED_ALLOW_IPS in docs/DEPLOYMENT.md.
#
# Running this in more than one replica? `alembic upgrade head` on every
# replica will race with itself. See docs/DEPLOYMENT.md.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host ${SPARTON_HOST} --port ${SPARTON_PORT} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
