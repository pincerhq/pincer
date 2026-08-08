"""Lazy singleton doctr predictor cache with bounded LRU eviction and per-key locking.

docTR's own reference implementation (mindee/doctr `api/app/vision.py`) mutates
`predictor.det_predictor.model.postprocessor.bin_thresh`/`box_thresh` as instance
attributes *after* construction. That means the cache key here must include those
thresholds (and every other parameter that affects predictor behavior) — keying only
on architecture names would let two concurrent calls with different thresholds race
on a shared predictor instance.

Note: the pinned `python-doctr==1.0.1` release does not support layout detection,
table structure, or `disable_page_orientation`/`disable_crop_orientation` — those
exist only on doctr's unreleased GitHub `main` branch (verified by introspecting the
installed package's real signatures). Do not reintroduce them without also bumping
the pinned version and re-verifying against the actually-installed API surface.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from doctr_mcp.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

Task = str  # "detection" | "recognition" | "ocr" | "kie"


@dataclass(frozen=True)
class PredictorParams:
    task: Task
    det_arch: str = "db_resnet50"
    reco_arch: str = "crnn_vgg16_bn"
    assume_straight_pages: bool = True
    preserve_aspect_ratio: bool = True
    symmetric_pad: bool = True
    detect_orientation: bool = False
    straighten_pages: bool = False
    detect_language: bool = False
    det_bs: int = 2
    reco_bs: int = 128
    bin_thresh: float = 0.1
    box_thresh: float = 0.1
    # OCR-only (ignored for task == "kie"/"detection"/"recognition" — kie_predictor's
    # signature doesn't accept them, and detection/recognition slice a sub-predictor
    # out of a full ocr_predictor build anyway, same trick doctr's own
    # api/app/vision.py uses). Defaults match doctr.models.builder.DocumentBuilder's
    # own defaults in the pinned 1.0.1 release.
    resolve_lines: bool = True
    resolve_blocks: bool = False
    paragraph_break: float = 0.035

    def cache_key(self) -> tuple[tuple[str, Any], ...]:
        return tuple(sorted(self.__dict__.items()))


class _BoundedPredictorCache:
    """A plain-dict-backed LRU cache guarded by a thread lock (get/set are cheap and
    synchronous — the expensive part, building a predictor, happens outside this
    lock, guarded instead by the per-key asyncio.Lock in `get_predictor`)."""

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._data: OrderedDict[tuple[tuple[str, Any], ...], Any] = OrderedDict()
        self._guard = threading.Lock()

    def get(self, key: tuple[tuple[str, Any], ...]) -> Any | None:
        with self._guard:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def set(self, key: tuple[tuple[str, Any], ...], value: Any) -> None:
        with self._guard:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                evicted_key, _ = self._data.popitem(last=False)
                logger.info("Evicted cached predictor for key=%s", evicted_key)

    def __len__(self) -> int:
        with self._guard:
            return len(self._data)


_cache: _BoundedPredictorCache | None = None
_locks: dict[tuple[tuple[str, Any], ...], asyncio.Lock] = {}
_locks_guard = threading.Lock()


def _get_cache() -> _BoundedPredictorCache:
    global _cache
    if _cache is None:
        _cache = _BoundedPredictorCache(get_settings().max_cached_predictors)
    return _cache


def _get_lock(key: tuple[tuple[str, Any], ...]) -> asyncio.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        return lock


def reset_state() -> None:
    """Test-only: clear the module-level cache/lock singletons between test cases."""
    global _cache
    _cache = None
    _locks.clear()


def _build_predictor(params: PredictorParams) -> Any:
    import torch
    from doctr.models import kie_predictor, ocr_predictor

    kwargs: dict[str, Any] = {
        "det_arch": params.det_arch,
        "reco_arch": params.reco_arch,
        "pretrained": True,
        "assume_straight_pages": params.assume_straight_pages,
        "preserve_aspect_ratio": params.preserve_aspect_ratio,
        "symmetric_pad": params.symmetric_pad,
        "detect_orientation": params.detect_orientation,
        "straighten_pages": params.straighten_pages,
        "detect_language": params.detect_language,
        "det_bs": params.det_bs,
        "reco_bs": params.reco_bs,
    }

    predictor: Any
    if params.task == "kie":
        # kie_predictor's signature has no resolve_lines/resolve_blocks/
        # paragraph_break — KIE output is per-class predictions, not a block/line
        # tree, and passing these would not be a recognized kwarg.
        predictor = kie_predictor(**kwargs)
    else:
        predictor = ocr_predictor(
            **kwargs,
            resolve_lines=params.resolve_lines,
            resolve_blocks=params.resolve_blocks,
            paragraph_break=params.paragraph_break,
        )

    det_model: Any = predictor.det_predictor.model
    det_model.postprocessor.bin_thresh = params.bin_thresh
    det_model.postprocessor.box_thresh = params.box_thresh

    device = torch.device("cuda" if get_settings().device == "cuda" else "cpu")
    predictor = predictor.to(device)

    if params.task == "detection":
        return predictor.det_predictor
    if params.task == "recognition":
        return predictor.reco_predictor
    return predictor


# Swappable in tests so no real torch/doctr import is ever exercised.
predictor_builder: Callable[[PredictorParams], Any] = _build_predictor

_inference_semaphore: asyncio.Semaphore | None = None


def _get_inference_semaphore() -> asyncio.Semaphore:
    global _inference_semaphore
    if _inference_semaphore is None:
        _inference_semaphore = asyncio.Semaphore(get_settings().max_workers)
    return _inference_semaphore


async def get_predictor(params: PredictorParams) -> Any:
    """Return a cached predictor for these params, building it if necessary.

    Concurrent calls sharing the same params share one asyncio.Lock, so the predictor
    is built (and its thresholds mutated) exactly once, and no caller can observe a
    partially-configured predictor mid-mutation.
    """
    key = params.cache_key()
    cache = _get_cache()
    lock = _get_lock(key)
    async with lock:
        predictor = cache.get(key)
        if predictor is None:
            predictor = await asyncio.to_thread(predictor_builder, params)
            cache.set(key, predictor)
        return predictor


async def run_predictor(predictor: Any, documents: list[Any]) -> Any:
    """Run predictor inference off the event loop, capped by DOCTR_MAX_WORKERS
    concurrent inferences process-wide (torch already parallelizes a single call
    across cores via intra-op threads — serializing calls tends to beat letting N
    Python threads oversubscribe CPU)."""
    semaphore = _get_inference_semaphore()
    async with semaphore:
        return await asyncio.to_thread(predictor, documents)
