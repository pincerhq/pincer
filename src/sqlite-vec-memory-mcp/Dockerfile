FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install package and deps
COPY pyproject.toml ./
COPY memory_server.py ./server.py
RUN uv sync --no-cache --no-dev --all-extras

# Pre-download the ONNX model into the image so first request isn't slow.
# Override with --build-arg DEFAULT_MODEL=<other-model> if needed.
ARG DEFAULT_MODEL=BAAI/bge-small-en-v1.5
# Multilingual model for the tool index (tool_index_sync/tool_index_search).
ARG DEFAULT_TOOL_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
ENV FASTEMBED_CACHE_PATH=/app/models
RUN uv run python -c "from fastembed import TextEmbedding; TextEmbedding('${DEFAULT_MODEL}')"
RUN uv run python -c "from fastembed import TextEmbedding; TextEmbedding('${DEFAULT_TOOL_MODEL}')"

VOLUME ["/app/data"]

ENV MEMORY_DB_PATH=/app/data/memory.db
ENV EMBED_MODEL=${DEFAULT_MODEL}
ENV EMBED_DIM=384
ENV TOOL_EMBED_MODEL=${DEFAULT_TOOL_MODEL}
ENV TOOL_EMBED_DIM=384
ENV TRANSPORT=http
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

# Healthcheck: poll the health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'curl -f http://localhost:${PORT:-8000}/health || exit 1'

CMD ["uv", "run", "server.py"]
