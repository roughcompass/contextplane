# syntax=docker/dockerfile:1
# ── build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Build-time deps only — not copied to the final image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY registry ./registry
COPY scripts ./scripts
COPY alembic.ini ./

# NOT --editable: the editable install records absolute paths from the builder
# (/build/registry), which don't exist in the runtime stage at /app/registry.
# A regular install lays the package under site-packages where the path is
# self-contained, and `docker exec ... python` resolves `registry` correctly.
# Hot-reload via the docker-compose volume mount still works because uvicorn's
# `--reload` watches the source files at /app/registry/.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install .

# Stage the embedding model into the image.
#
# The running container must never need to reach a model host. Deployments the
# image targets are network-isolated: there is no egress, the root filesystem is
# read-only, and the only writable mount is an emptyDir at /tmp — so there is
# nowhere to download to and no way to get there. The model is a layer instead.
#
# Build hosts behind a proxy override the origin; the layout must match the
# manifest paths, and checksums are enforced either way:
#   docker build --build-arg EMBEDDING_MODEL_SOURCE=https://artifacts.corp/minilm .
#
# A pre-staged directory works too — CI pre-fetches into .model-cache/ on the
# runner (the public hub rate-limits shared runner IPs, so fetching inside the
# build fails there while succeeding everywhere else) and passes
#   --build-arg EMBEDDING_MODEL_SOURCE=/build/.model-cache/all-MiniLM-L6-v2
# The glob COPY below stages that directory when it exists and is a no-op on
# builds that fetch live; the checksum manifest is the trust anchor either way.
COPY .model-cach[e] ./.model-cache
ARG EMBEDDING_MODEL_SOURCE=""
RUN python scripts/fetch_embedding_model.py \
      --out /opt/models/all-MiniLM-L6-v2 \
      ${EMBEDDING_MODEL_SOURCE:+--source "$EMBEDDING_MODEL_SOURCE"}

# ── runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# No curl, wget, or shell utilities in the final image.
# libpq-dev runtime is the only external dep needed for asyncpg at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && apt-get purge -y --auto-remove

# Non-root user.
RUN useradd -m -u 999 -g 0 registry

WORKDIR /app

# Copy installed packages from builder.
COPY --from=builder --chown=registry:root /install /usr/local
# Copy application source with correct ownership.
COPY --from=builder --chown=registry:root /build/registry ./registry
COPY --from=builder --chown=registry:root /build/scripts ./scripts
COPY --from=builder --chown=registry:root /build/alembic.ini ./
COPY --from=builder --chown=registry:root /build/pyproject.toml ./

# Model artifact: root-owned and read-only. Nothing writes here at runtime, so
# it needs no volume and is compatible with readOnlyRootFilesystem.
COPY --from=builder --chown=root:root /opt/models /opt/models

# Prove the staged artifact computes real vectors before the image is tagged.
# Checksums already proved the bytes are intact; this proves they are the right
# export — right width, unit-length output, meaning actually encoded.
RUN python scripts/verify_embedding_model.py --model-path /opt/models/all-MiniLM-L6-v2

# Belt and braces on top of the artifact being local: if some future code path
# does reach for the Hugging Face Hub, these turn a silent network call into an
# immediate error rather than a hang against an unreachable host.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/opt/models/hf \
    EMBEDDING_MODEL_PATH=/opt/models/all-MiniLM-L6-v2

# Drop to non-root.
USER registry

EXPOSE 8000

# Default: run API server. Override command to run sync-worker:
#   command: ["python", "-m", "registry.sync_worker"]
#
# --timeout-graceful-shutdown bounds how long shutdown waits for open
# connections. Unbounded is the default and is wrong here: the streaming
# endpoint holds a response open for the life of a client session, so it is
# never idle and never closed, and the wait happens *before* the app's own
# teardown runs. On a rolling deploy that means the container sits until the
# orchestrator's grace period expires and then dies by signal, having flushed
# neither queued spans nor in-flight delivery work. Five seconds is well past
# p95 for every request that does terminate on its own.
CMD ["uvicorn", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "5", "--factory", "registry.main:create_app"]
