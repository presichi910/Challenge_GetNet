# Two stages: the dependencies are installed in a virtualenv in `build`, and only that folder is copied into the final image.
# pip, its caches and everything it needed to install stay behind in `build`.
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# `constraints.txt` pins EVERY dependency, transitive ones included (it was generated inside python:3.12-slim on Linux, see
# README, Production notes): a rebuild installs the same versions, it cannot pick up a newer release of a sub-dependency.
# Deps first, before the code, so this layer is cached.
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt


FROM python:3.12-slim

# What this image is and which code it holds, readable with `docker inspect`. CI passes the commit; a local build says "unknown".
ARG REVISION=unknown
ARG VERSION=dev
LABEL org.opencontainers.image.title="getnet-agents" \
      org.opencontainers.image.description="Getnet multi-agent support API (LangGraph + FastAPI)" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# The base image is a snapshot: security fixes of its Debian packages that came out after it was built are applied here (trivy found two HIGH
# ones in zlib1g with a fix available, and the CI blocks on that). The cost is that two builds of the same commit can differ by a patched system
# package; the Python dependencies are exact (constraints.txt).
RUN apt-get update \
 && apt-get -y --no-install-recommends upgrade \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

# Unprivileged user with a fixed uid/gid, so volume ownership is predictable.
# Created after the dependencies so their layer stays cached.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

# Code stays owned by root and read-only for the app user: the process cannot rewrite its own code.
# `scripts/` (the demo token helper) and `infra/` (the Keycloak realms) are NOT copied: nobody who can open a shell in
# this container should be able to mint a token.
COPY app ./app
COPY eval ./eval

# Only two places are writable: /app/data (Chroma, crawl cache, tickets, handoffs) and
# /app/eval/reports. /app/data is a named volume in docker-compose.yml: Docker initialises a new
# volume with this directory's owner, so no root entrypoint and no chown at start are needed.
RUN mkdir -p /app/data /app/eval/reports \
 && chown -R app:app /app/data /app/eval/reports
ENV CHROMA_DIR=/app/data/chroma \
    RAW_DIR=/app/data/raw \
    TICKETS_FILE=/app/data/tickets.jsonl \
    HANDOFFS_FILE=/app/data/handoffs.jsonl \
    HANDOFFS_DB_PATH=/app/data/handoffs.sqlite \
    CHECKPOINT_DB_PATH=/app/data/checkpoints.sqlite \
    REVOCATIONS_DB_PATH=/app/data/revocations.sqlite \
    RAG_CACHE_PATH=/app/data/rag_cache.sqlite \
    APPROVALS_DB_PATH=/app/data/approvals.sqlite

USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# ONE worker, on purpose: the rate limit and the key cache live in the process and the conversation memory is one
# SQLite file. The API also refuses to start if WEB_CONCURRENCY asks for more (see README, Production notes).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
