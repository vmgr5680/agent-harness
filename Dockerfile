# Multi-stage build. The builder has compilers and caches; the runtime has
# neither, which is both smaller and a smaller attack surface.
#
# Pin the digest in your own registry. A floating tag means "whatever was
# published this morning", which is not a reproducible build and is a supply
# chain decision you did not make deliberately.

# ---------- builder ----------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependencies first, so a source change does not invalidate the layer that
# takes the time.
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install ".[api]"

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=random \
    AH_SESSION_DB_URL=sqlite:////data/sessions.db \
    AH_LOG_FORMAT=json \
    AH_ENV=prod

# Non-root. An agent runtime executes tools decided by a language model; it is
# the last process on the machine that should be able to write outside /data.
RUN groupadd --system --gid 1001 harness \
 && useradd --system --uid 1001 --gid harness --no-create-home harness \
 && mkdir -p /data && chown harness:harness /data

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=harness:harness src ./src
COPY --chown=harness:harness data ./data
COPY --chown=harness:harness evals ./evals
COPY --chown=harness:harness adk_agents ./adk_agents

USER harness
EXPOSE 8000
VOLUME ["/data"]

# Liveness only — it must not touch the model provider. See api/app.py.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

# One worker per container. The governance plugin holds the cost ledger, the
# metrics registry and the per-tenant rate limiter in process; forking workers
# would silently give each one its own copy of all three, so a "60 per minute"
# limit would quietly become 60 per worker. Scale with replicas and move those
# three behind Redis when you need to.
CMD ["uvicorn", "agent_harness.api.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--log-config", "/dev/null"]
