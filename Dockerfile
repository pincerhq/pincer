# ============================================
# Stage 0: Dashboard build
# ============================================
FROM node:20-slim AS dashboard-builder

WORKDIR /app

RUN corepack enable pnpm

COPY dashboard/ ./

RUN pnpm install
RUN pnpm build

# ============================================
# Stage 1: Build
# ============================================
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
COPY skills/ skills/
COPY README.md ./
RUN uv sync --frozen --no-dev

# ============================================
# Stage 2: Runtime
# ============================================
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r pincer && useradd -r -g pincer -m pincer

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/skills /app/skills
COPY --from=builder /app/pyproject.toml /app/
COPY --from=dashboard-builder /app/dist /app/dashboard/dist

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /app/data && chown -R pincer:pincer /app

USER pincer

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

EXPOSE 8080

CMD ["pincer", "run"]
