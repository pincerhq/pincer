FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl git \
    && rm -rf /var/lib/apt/lists/*


COPY pyproject.toml ./
RUN uv sync --no-cache --no-dev --all-extras

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'curl -f http://localhost:${PORT:-8000}/health || exit 1'

ENV TRANSPORT=http
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

CMD ["uv", "run", "server.py"]


# NewsAPI mcp server
FROM builder AS newsapi-mcp

COPY newsapi_server.py ./server.py


# Stock price mcp server
FROM builder AS stock-price-mcp

COPY stock_price_server.py ./server.py


# Summarize mcp server
FROM builder AS summarize-mcp

COPY summarize_server.py ./server.py


# Translate mcp server
FROM builder AS translate-mcp

COPY translate_server.py ./server.py
ENV LIBRETRANSLATE_URL=https://libretranslate.com


# OpenWeatherMap mcp server
FROM builder AS openweathermap-mcp

COPY openweathermap_server.py ./server.py


# Web search mcp server
FROM builder AS websearch-mcp

COPY websearch_server.py ./server.py
