"""Shared fixtures for doctr-mcp tests.

No real torch/doctr model is ever built or downloaded in these tests: every test
that would trigger predictor construction monkeypatches
`doctr_mcp.predictors.predictor_builder` with a fake, duck-typed to match the shape
of doctr's own Page/Document/Prediction objects.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from io import BytesIO
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Fresh predictor cache/locks and settings for every test."""
    from doctr_mcp import predictors
    from doctr_mcp.config import get_settings

    predictors.reset_state()
    get_settings.cache_clear()
    yield
    predictors.reset_state()
    get_settings.cache_clear()


@pytest.fixture
def tiny_png_b64() -> str:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (16, 16), color="white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# Fake doctr-shaped objects (duck-typed, no doctr/torch import)
# ---------------------------------------------------------------------------


class FakeWord:
    def __init__(
        self,
        value: str = "HELLO",
        confidence: float = 0.951,
        geometry: Any = ((0.1, 0.1), (0.2, 0.2)),
        objectness_score: float = 0.901,
        crop_orientation: dict[str, Any] | None = None,
    ) -> None:
        self.value = value
        self.confidence = confidence
        self.geometry = geometry
        self.objectness_score = objectness_score
        self.crop_orientation = crop_orientation or {"value": 0, "confidence": None}

    def render(self) -> str:
        return self.value


class FakeLine:
    def __init__(self, words: list[FakeWord]) -> None:
        self.words = words
        self.geometry = ((0.1, 0.1), (0.9, 0.2))
        self.objectness_score = 0.9

    def render(self) -> str:
        return " ".join(w.render() for w in self.words)


class FakeBlock:
    def __init__(self, lines: list[FakeLine]) -> None:
        self.lines = lines
        self.geometry = ((0.1, 0.1), (0.9, 0.9))
        self.objectness_score = 0.9

    def render(self) -> str:
        return "\n".join(line.render() for line in self.lines)


class FakePage:
    def __init__(
        self,
        blocks: list[FakeBlock] | None = None,
        orientation: dict[str, Any] | None = None,
        language: dict[str, Any] | None = None,
        dimensions: tuple[int, int] = (100, 200),
    ) -> None:
        self.blocks = blocks if blocks is not None else [FakeBlock([FakeLine([FakeWord()])])]
        self.orientation = orientation or {"value": 0, "confidence": None}
        self.language = language or {"value": None, "confidence": None}
        self.dimensions = dimensions

    def render(self) -> str:
        return "\n\n".join(b.render() for b in self.blocks)


class FakeDocument:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages

    def render(self) -> str:
        return "\n\n\n\n".join(p.render() for p in self.pages)


class FakePrediction:
    def __init__(
        self,
        value: str = "42.00",
        confidence: float = 0.9,
        geometry: Any = ((0.1, 0.1), (0.2, 0.2)),
        objectness_score: float = 0.9,
        crop_orientation: dict[str, Any] | None = None,
    ) -> None:
        self.value = value
        self.confidence = confidence
        self.geometry = geometry
        self.objectness_score = objectness_score
        self.crop_orientation = crop_orientation or {"value": 0, "confidence": None}


class FakeKiePage:
    def __init__(
        self,
        predictions: dict[str, list[FakePrediction]] | None = None,
        orientation: dict[str, Any] | None = None,
        language: dict[str, Any] | None = None,
        dimensions: tuple[int, int] = (100, 200),
    ) -> None:
        self.predictions = predictions if predictions is not None else {"total_price": [FakePrediction()]}
        self.orientation = orientation or {"value": 0, "confidence": None}
        self.language = language or {"value": None, "confidence": None}
        self.dimensions = dimensions


class FakeKieDocument:
    def __init__(self, pages: list[FakeKiePage]) -> None:
        self.pages = pages


class FakeDetPredictor:
    def __call__(self, pages: list[Any]) -> list[dict[str, Any]]:
        import numpy as np

        return [{"words": np.array([[0.1, 0.1, 0.2, 0.2, 0.93]])} for _ in pages]


class FakeRecoPredictor:
    def __call__(self, crops: list[Any]) -> list[tuple[str, float]]:
        return [("HELLO", 0.951) for _ in crops]


class FakeOcrPredictor:
    def __call__(self, pages: list[Any]) -> FakeDocument:
        return FakeDocument(pages=[FakePage() for _ in pages])


class FakeKiePredictor:
    def __call__(self, pages: list[Any]) -> FakeKieDocument:
        return FakeKieDocument(pages=[FakeKiePage() for _ in pages])


@pytest.fixture
def fake_predictor_builder(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Patch doctr_mcp.predictors.predictor_builder so no real torch/doctr model is
    ever built. Returns the list of PredictorParams each call was made with, so
    tests can assert on cache-key behavior."""
    from doctr_mcp import predictors

    build_calls: list[Any] = []

    def _fake_builder(params: Any) -> Any:
        build_calls.append(params)
        if params.task == "detection":
            return FakeDetPredictor()
        if params.task == "recognition":
            return FakeRecoPredictor()
        if params.task == "kie":
            return FakeKiePredictor()
        return FakeOcrPredictor()

    monkeypatch.setattr(predictors, "predictor_builder", _fake_builder)
    return build_calls
