FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install package and deps
COPY pyproject.toml ./
COPY memory_server.py ./server.py
RUN uv sync --no-cache

# Pre-download the ONNX model into the image so first request isn't slow.
# Override with --build-arg DEFAULT_MODEL=<other-model> if needed.
ARG DEFAULT_MODEL=BAAI/bge-small-en-v1.5
ENV FASTEMBED_CACHE_DIR=/app/models
RUN uv run python -c "from fastembed import TextEmbedding; TextEmbedding('${DEFAULT_MODEL}')"

VOLUME ["/app/data"]

ENV MEMORY_DB_PATH=/app/data/memory.db
ENV EMBED_MODEL=BAAI/bge-small-en-v1.5
ENV EMBED_DIM=384
ENV TRANSPORT=http
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

# Healthcheck: poll the health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'curl -f http://localhost:${PORT:-8000}/health || exit 1'

CMD ["uv", "run", "server.py"]
