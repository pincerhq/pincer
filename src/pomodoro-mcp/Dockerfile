FROM python:3.12-slim

LABEL org.opencontainers.image.title="Pomodoro MCP Server"
LABEL org.opencontainers.image.description="A Model Context Protocol server for Pomodoro session management"
LABEL org.opencontainers.image.source="https://github.com/your-org/pomodoro-mcp"

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── App user (non-root) ───────────────────────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN useradd --create-home --shell /bin/bash pomodoro
WORKDIR /app

# ── Python deps ───────────────────────────────────────────────────────────────
COPY pyproject.toml ./
RUN uv sync --no-dev

# ── Application code ──────────────────────────────────────────────────────────
COPY --chown=pomodoro:pomodoro server.py state.py ./

USER pomodoro

# Healthcheck: poll the health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'curl -f http://localhost:${PORT:-8000}/health || exit 1'

# ── Runtime ───────────────────────────────────────────────────────────────────
ENV TRANSPORT=http
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

CMD ["uv", "run", "server.py"]
