# MACH catalog — combined REST + MCP server for Render (baked-DB variant).
#
# Unlike the boot-build Dockerfile, this copies a pre-built data/mach.duckdb into
# the image, so a cold start is just "wake and serve" — no harvest on boot. The
# catalogue is refreshed by rebuilding the DB locally and redeploying the image.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    MACH_DB_PATH=/app/data/mach.duckdb \
    FASTEMBED_CACHE_PATH=/app/.fastembed_cache \
    PORT=10000

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY core ./core
COPY adapters ./adapters
COPY edges ./edges

RUN uv sync --frozen --no-dev

# Pre-download the embedding model at build time (needed for /search at runtime).
RUN uv run python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='sentence-transformers/all-MiniLM-L6-v2')"

# Bake the pre-built catalogue into the image — no boot-time harvest.
COPY data/mach.duckdb ./data/mach.duckdb

EXPOSE 10000
CMD uv run uvicorn edges.server:app --host 0.0.0.0 --port ${PORT:-10000}
