FROM python:3.12-slim

# opencv-python (non-headless) needs libgl1/libglib2.0-0/libsm6/libxext6 on a slim
# base; ffmpeg covers pypdfium2/opencv's optional codec paths; curl is for the
# healthcheck. Mirrors doctr's own reference api/Dockerfile's system deps, minus
# git/make (not needed once installing python-doctr from a released PyPI wheel).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libsm6 libxext6 ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install package and deps. This Dockerfile only ever sees this subdirectory as
# its build context (see docker-compose.yml's `build: .`), so `uv sync` here
# resolves src/doctr-mcp/pyproject.toml as a standalone project, not as a
# workspace member — this is why that file carries its own duplicate
# `[[tool.uv.index]]` block (see the comment there for why).
COPY pyproject.toml ./
COPY doctr_mcp ./doctr_mcp
RUN uv sync --no-cache --no-dev

# Pre-bake a curated set of lightweight architectures into the image so that
# switching DOCTR_DEFAULT_DET_ARCH/DOCTR_DEFAULT_RECO_ARCH via env var at runtime
# doesn't trigger a cold Hugging Face download on first use. Override with
# --build-arg PREBAKE_DET_ARCHS="..." / PREBAKE_RECO_ARCHS="..." (space-separated)
# to bake a different set.
ARG PREBAKE_DET_ARCHS="db_resnet50 db_mobilenet_v3_large fast_base"
ARG PREBAKE_RECO_ARCHS="crnn_vgg16_bn crnn_mobilenet_v3_small"
ENV DOCTR_CACHE_DIR=/app/models
# Note: python-doctr==1.0.1 (pinned in pyproject.toml) has no layout-detection or
# table-structure models — only detection/recognition architectures are baked here.
RUN mkdir -p /app/models && \
    for det in $PREBAKE_DET_ARCHS; do \
        for reco in $PREBAKE_RECO_ARCHS; do \
            echo "Pre-baking det_arch=$det reco_arch=$reco"; \
            uv run python -c "from doctr.models import ocr_predictor; ocr_predictor(det_arch='$det', reco_arch='$reco', pretrained=True)"; \
        done; \
    done

VOLUME ["/app/models"]

ENV DOCTR_DEVICE=cpu \
    DOCTR_MAX_WORKERS=1 \
    DOCTR_DEFAULT_DET_ARCH=db_resnet50 \
    DOCTR_DEFAULT_RECO_ARCH=crnn_vgg16_bn \
    TRANSPORT=http \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# Cold start includes torch import + baked-model load from disk (slower than
# sqlite-vec-memory-mcp's ONNX/fastembed equivalent) — hence the longer start_period.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD sh -c 'curl -f http://localhost:${PORT:-8000}/health || exit 1'

CMD ["uv", "run", "doctr-mcp-run"]
