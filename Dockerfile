# syntax=docker/dockerfile:1
# SPARTON production image.
#
# Build stages (deps, then shopfeed) so the runtime layer carries no compiler toolchain: a smaller image
# is a smaller patch surface, and a breach of the running container has nothing
# useful to build a exploit with.
#
# The app is a single ASGI process behind `workers/worker.py` for the crawl and
# report queue. Both run from the same image; compose distinguishes them by
# command. See docs/DEPLOYMENT.md.

# ---------------------------------------------------------------- builder
FROM python:3.12-slim AS deps

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

# ---------------------------------------------------------------- shopfeed
# Its own stage so `--no-cache-filter shopfeed` re-runs this step alone. BuildKit
# does not put secrets in the cache key, so without that flag a build WITH the
# secret silently reuses a cached build WITHOUT it (and a cached shopfeed never
# picks up new commits). Re-running it costs one small clone.
FROM deps AS shopfeed

# shopfeed reads a webshop's own product feed instead of parsing HTML: exact
# prices and no LLM tokens. It is a PRIVATE repository, so it is installed from
# GitHub with a token passed as a BuildKit secret:
#
#     GH_TOKEN=... docker build --secret id=gh_token,env=GH_TOKEN \
#         --no-cache-filter shopfeed -t sparton:latest .
#
# The secret is mounted at /run/secrets/gh_token for this one RUN step only. It
# is never an ARG or ENV (both are recorded in `docker history`), it never
# appears in the git URL (pip records that URL in the installed package's
# direct_url.json), and it lives only in git's environment for the clone.
# git and the clone stay in this stage; only /install reaches the runtime image.
#
# No secret, no shopfeed: the build still succeeds and says so, and the app
# falls back to the HTML crawl (app/ecommerce/feeds.py). See docs/DEPLOYMENT.md.
ARG SHOPFEED_REF=master
RUN --mount=type=secret,id=gh_token,required=false \
    if [ -s /run/secrets/gh_token ]; then \
      apt-get update -qq \
      && apt-get install -y -qq --no-install-recommends git >/dev/null \
      && rm -rf /var/lib/apt/lists/* \
      && GIT_TERMINAL_PROMPT=0 \
         GIT_CONFIG_COUNT=1 \
         GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
         GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$(cat /run/secrets/gh_token)" | base64 -w0)" \
         pip install --prefix=/install --no-deps \
           "git+https://github.com/sklsp/shopfeed@${SHOPFEED_REF}"; \
    else \
      echo "WARNING: no gh_token build secret, so shopfeed is NOT installed." >&2; \
      echo "WARNING: competitor prices will come from the HTML crawl (extracted, not exact)." >&2; \
      echo "WARNING: build with --secret id=gh_token,env=GH_TOKEN to include it." >&2; \
    fi

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

COPY --from=shopfeed /install /usr/local

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
