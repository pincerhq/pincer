"""Configuration for doctr-mcp via environment variables.

Note: docTR itself already reads `DOCTR_CACHE_DIR` directly from the environment
(see doctr.utils.data) to control where model weights are cached — that variable is
intentionally *not* duplicated here as a settings field, it's just passed straight
through to the library.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class DoctrSettings(BaseSettings):  # type: ignore[misc]
    model_config = SettingsConfigDict(
        env_prefix="DOCTR_",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    device: str = "cpu"
    """"cpu" (default) or "cuda". Never auto-detected — this repo's bundled servers
    are deployed without GPU passthrough by default, so silently preferring CUDA when
    available would make behavior depend on host hardware."""

    max_workers: int = 1
    """Max concurrent doctr inference calls process-wide. torch already parallelizes
    a single call across cores via intra-op threads, so serializing calls tends to
    beat oversubscribing CPU with N concurrent Python threads."""

    torch_threads: int = 0
    """Passed to torch.set_num_threads() at startup if > 0. 0 (default) means "let
    torch_threads_default() pick it, seeded from os.cpu_count()" — set explicitly
    inside containers with a CPU quota, where torch's own heuristics commonly guess
    wrong relative to the cgroup limit."""

    max_cached_predictors: int = 4
    """Bounded LRU size for the predictor cache. Each cached CPU predictor is
    hundreds of MB resident, so this is not allowed to grow unbounded."""

    max_input_mb: int = 20
    """Reject decoded (post-base64) image/PDF payloads larger than this many MB."""

    default_det_arch: str = "db_resnet50"
    default_reco_arch: str = "crnn_vgg16_bn"

    log_tool_requests: bool = False
    """If True, log every tools/call request and response (tool name, truncated
    payload, duration) via FastMCP's LoggingMiddleware. Off by default — this is
    a debugging aid; payloads can contain OCR'd document contents."""

    log_max_payload_length: int = 2000
    """Max characters of the (JSON-serialized) request/response payload to
    include per log line when log_tool_requests is on. Prevents a single
    DOCTR_MAX_INPUT_MB-sized call from writing a multi-megabyte log line."""


@lru_cache(maxsize=1)
def get_settings() -> DoctrSettings:
    return DoctrSettings()


def torch_threads_default() -> int:
    return os.cpu_count() or 1


# Full architecture zoo as of python-doctr 1.0.1, verified against the installed
# package's real doctr.models.{detection,recognition}.zoo.ARCHS lists (not doctr's
# GitHub main branch, which has since added layout/table-structure models not
# present in this release). Tool inputs accept any string here (not a restrictive
# enum, per product decision) — this list is surfaced only for discovery via the
# list_architectures tool.
KNOWN_DET_ARCHS: tuple[str, ...] = (
    "db_resnet34",
    "db_resnet50",
    "db_mobilenet_v3_large",
    "linknet_resnet18",
    "linknet_resnet34",
    "linknet_resnet50",
    "fast_tiny",
    "fast_small",
    "fast_base",
)
KNOWN_RECO_ARCHS: tuple[str, ...] = (
    "crnn_vgg16_bn",
    "crnn_mobilenet_v3_small",
    "crnn_mobilenet_v3_large",
    "sar_resnet31",
    "master",
    "vitstr_small",
    "vitstr_base",
    "parseq",
    "viptr_tiny",
)

# Curated lightweight architectures pre-baked into the Docker image at build time (see
# Dockerfile's PREBAKE_DET_ARCHS/PREBAKE_RECO_ARCHS build args) so that switching
# DOCTR_DEFAULT_DET_ARCH/DOCTR_DEFAULT_RECO_ARCH via env var doesn't trigger a cold
# download on first use.
PREBAKED_DET_ARCHS: tuple[str, ...] = ("db_resnet50", "db_mobilenet_v3_large", "fast_base")
PREBAKED_RECO_ARCHS: tuple[str, ...] = ("crnn_vgg16_bn", "crnn_mobilenet_v3_small")
