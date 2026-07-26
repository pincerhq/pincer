"""Pydantic input/output models for doctr-mcp's tools.

Convention (matching src/pincer-mcps/openweathermap_server.py): one input model per
tool, `extra="forbid"`, `Field(description=...)` on every field, passed to the tool
as a single `params` argument.

Architecture names (`det_arch`/`reco_arch`) are plain strings with sensible
defaults, not a restrictive `Literal[...]` enum — full customization is allowed;
call `list_architectures` to discover valid names.

Note: layout detection, table structure, and `disable_page_orientation`/
`disable_crop_orientation` are not supported — those exist only on doctr's
unreleased GitHub `main` branch, not in the pinned `python-doctr==1.0.1` release
(confirmed by introspecting the installed package's real signatures).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from doctr_mcp.config import get_settings

# ---------------------------------------------------------------------------
# Shared input pieces
# ---------------------------------------------------------------------------


class ImageSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_b64: str | None = Field(
        default=None,
        description=(
            "Base64-encoded image or PDF bytes. The portable default — required when "
            "doctr-mcp is deployed over HTTP/Docker, since there is no shared "
            "filesystem with the caller in that case."
        ),
    )
    file_path: str | None = Field(
        default=None,
        description=(
            "Absolute path to a file on disk. Only usable when doctr-mcp runs via "
            "stdio with a filesystem shared with the caller (e.g. Pincer's "
            "PINCER_DATA_DIR) — not usable against the default HTTP/Docker "
            "deployment unless a volume is explicitly shared."
        ),
    )
    filename: str | None = Field(
        default=None,
        description="Optional filename hint (e.g. 'scan.pdf') to disambiguate format when image_b64 is given.",
    )

    @model_validator(mode="after")
    def _check_exclusive(self) -> ImageSource:
        if bool(self.image_b64) == bool(self.file_path):
            raise ValueError("Provide exactly one of image_b64 or file_path.")
        return self


def _page_range_field() -> tuple[int, int] | None:
    return Field(
        default=None,
        description=(
            "Optional [start, end) page slice for multi-page PDFs, e.g. [0, 3] for "
            "the first three pages. Ignored for single images. Applied before "
            "running any model, to bound response size for large PDFs."
        ),
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class DetectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ImageSource
    page_range: tuple[int, int] | None = _page_range_field()
    det_arch: str = Field(
        default_factory=lambda: get_settings().default_det_arch,
        description="Detection architecture name, e.g. 'db_resnet50'. See list_architectures.",
    )
    assume_straight_pages: bool = Field(default=True, description="If True, fit straight boxes to the page.")
    preserve_aspect_ratio: bool = Field(
        default=True, description="Pad the input to preserve aspect ratio before detection."
    )
    symmetric_pad: bool = Field(default=True, description="Pad the image symmetrically instead of bottom-right only.")
    det_bs: int = Field(default=2, ge=1, description="Detection batch size.")
    bin_thresh: float = Field(
        default=0.1, ge=0.0, le=1.0, description="Binarization threshold for the detection postprocessor."
    )
    box_thresh: float = Field(
        default=0.1, ge=0.0, le=1.0, description="Box confidence threshold for the detection postprocessor."
    )


class DetectionBox(BaseModel):
    geometry: tuple[float, ...] = Field(description="Flattened box/polygon coordinates, normalized to [0, 1].")
    confidence: float | None = Field(default=None, description="Confidence score, if the architecture reports one.")


class DetectionResult(BaseModel):
    name: str
    boxes: list[DetectionBox]


class DetectionOutput(BaseModel):
    results: list[DetectionResult]


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


class RecognitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[ImageSource] = Field(description="Pre-cropped word/line images to recognize (one result per image).")
    reco_arch: str = Field(
        default_factory=lambda: get_settings().default_reco_arch,
        description="Recognition architecture name, e.g. 'crnn_vgg16_bn'. See list_architectures.",
    )
    reco_bs: int = Field(default=128, ge=1, description="Recognition batch size.")


class RecognitionItem(BaseModel):
    text: str
    confidence: float


class RecognitionOutput(BaseModel):
    results: list[RecognitionItem]


# ---------------------------------------------------------------------------
# OCR (full tree + text convenience)
# ---------------------------------------------------------------------------


class OcrDocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ImageSource
    page_range: tuple[int, int] | None = _page_range_field()
    det_arch: str = Field(default_factory=lambda: get_settings().default_det_arch)
    reco_arch: str = Field(default_factory=lambda: get_settings().default_reco_arch)
    assume_straight_pages: bool = Field(default=True)
    preserve_aspect_ratio: bool = Field(default=True)
    symmetric_pad: bool = Field(default=True)
    detect_orientation: bool = Field(default=False, description="Detect page orientation (0/90/180/270).")
    straighten_pages: bool = Field(default=False, description="Rotate pages upright before running detection.")
    detect_language: bool = Field(default=False, description="Detect the dominant page language.")
    det_bs: int = Field(default=2, ge=1)
    reco_bs: int = Field(default=128, ge=1)
    bin_thresh: float = Field(default=0.1, ge=0.0, le=1.0)
    box_thresh: float = Field(default=0.1, ge=0.0, le=1.0)
    resolve_lines: bool = Field(default=True, description="Group words into lines.")
    resolve_blocks: bool = Field(default=False, description="Group lines into blocks.")
    paragraph_break: float = Field(
        default=0.035,
        description="Vertical-gap threshold (as a fraction of page height) used to split paragraphs/blocks.",
    )


class OcrDocumentTextInput(OcrDocumentInput):
    """Same predictor/params as ocr_document (so both tools share the same
    cached predictor for identical params) — only the output shape differs."""


class CropOrientation(BaseModel):
    value: int
    confidence: float | None = None


class OcrWord(BaseModel):
    value: str
    geometry: tuple[float, ...]
    objectness_score: float
    confidence: float
    crop_orientation: CropOrientation


class OcrLine(BaseModel):
    geometry: tuple[float, ...]
    objectness_score: float
    words: list[OcrWord]


class OcrBlock(BaseModel):
    geometry: tuple[float, ...]
    objectness_score: float
    lines: list[OcrLine]


class OcrPageValue(BaseModel):
    value: float | None = None
    confidence: float | None = None


class OcrLanguage(BaseModel):
    value: str | None = None
    confidence: float | None = None


class OcrPage(BaseModel):
    name: str
    orientation: OcrPageValue
    language: OcrLanguage
    dimensions: tuple[int, int]
    blocks: list[OcrBlock]


class OcrDocumentOutput(BaseModel):
    pages: list[OcrPage]


class OcrDocumentTextOutput(BaseModel):
    pages: list[str] = Field(description="Concatenated text per page, in reading order.")
    text: str = Field(description="All pages concatenated, separated by blank lines.")


# ---------------------------------------------------------------------------
# KIE
# ---------------------------------------------------------------------------


class KieInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ImageSource
    page_range: tuple[int, int] | None = _page_range_field()
    det_arch: str = Field(default_factory=lambda: get_settings().default_det_arch)
    reco_arch: str = Field(default_factory=lambda: get_settings().default_reco_arch)
    assume_straight_pages: bool = Field(default=True)
    preserve_aspect_ratio: bool = Field(default=True)
    symmetric_pad: bool = Field(default=True)
    detect_language: bool = Field(default=False)
    det_bs: int = Field(default=2, ge=1)
    reco_bs: int = Field(default=128, ge=1)
    bin_thresh: float = Field(default=0.1, ge=0.0, le=1.0)
    box_thresh: float = Field(default=0.1, ge=0.0, le=1.0)


class KieItem(BaseModel):
    value: str
    geometry: tuple[float, ...]
    objectness_score: float
    confidence: float
    crop_orientation: CropOrientation


class KieClassPredictions(BaseModel):
    class_name: str
    items: list[KieItem]


class KiePage(BaseModel):
    name: str
    orientation: OcrPageValue
    language: OcrLanguage
    dimensions: tuple[int, int]
    predictions: list[KieClassPredictions]


class KieOutput(BaseModel):
    pages: list[KiePage]


# ---------------------------------------------------------------------------
# Meta / discovery
# ---------------------------------------------------------------------------


class ListArchitecturesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListArchitecturesOutput(BaseModel):
    detection: list[str]
    recognition: list[str]
    defaults: dict[str, str]
