# ============================================
# Stage 0: Dashboard build
# ============================================
FROM node:22-slim AS dashboard-builder

WORKDIR /app

RUN corepack enable pnpm

COPY dashboard/ ./

ENV CI=true
RUN pnpm install --frozen-lockfile
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
RUN uv sync --frozen --no-dev --no-install-project --extra mcp

COPY src/ src/
COPY skills/ skills/
COPY README.md ./
RUN uv sync --frozen --no-dev --extra mcp

# ============================================
# Stage 2: Runtime
# ============================================
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Copy Node.js from the dashboard builder so npx-based MCP stdio servers work
COPY --from=dashboard-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=dashboard-builder /usr/local/bin/npm /usr/local/bin/npm
COPY --from=dashboard-builder /usr/local/bin/npx /usr/local/bin/npx
COPY --from=dashboard-builder /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN groupadd -r pincer && useradd -r -g pincer -m pincer

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/skills /app/skills
COPY --from=builder /app/pyproject.toml /app/
COPY --from=dashboard-builder /app/dist /app/dashboard/dist

# npm cache goes inside the data volume so it persists and is writable by pincer user
ENV PATH="/app/.venv/bin:/usr/local/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    npm_config_cache=/app/data/.npm

RUN mkdir -p /app/data && chown -R pincer:pincer /app

USER pincer

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

# Dashboard API
EXPOSE 8080
# MCP server endpoint (Pincer as an MCP server for Claude Desktop, Cursor, etc.)
EXPOSE 18800

CMD ["pincer", "run"]
