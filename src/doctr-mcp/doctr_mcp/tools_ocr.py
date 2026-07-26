"""ocr_document (full block/line/word tree) and ocr_document_text
(plain-text convenience, sharing the same predictor/params so identical calls to
either tool hit the same cached predictor)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from doctr_mcp import io_utils
from doctr_mcp.predictors import PredictorParams, get_predictor, run_predictor
from doctr_mcp.schemas import (
    CropOrientation,
    OcrBlock,
    OcrDocumentInput,
    OcrDocumentOutput,
    OcrDocumentTextInput,
    OcrDocumentTextOutput,
    OcrLanguage,
    OcrLine,
    OcrPage,
    OcrPageValue,
    OcrWord,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _predictor_params_from_ocr_input(params: OcrDocumentInput) -> PredictorParams:
    return PredictorParams(
        task="ocr",
        det_arch=params.det_arch,
        reco_arch=params.reco_arch,
        assume_straight_pages=params.assume_straight_pages,
        preserve_aspect_ratio=params.preserve_aspect_ratio,
        symmetric_pad=params.symmetric_pad,
        detect_orientation=params.detect_orientation,
        straighten_pages=params.straighten_pages,
        detect_language=params.detect_language,
        det_bs=params.det_bs,
        reco_bs=params.reco_bs,
        bin_thresh=params.bin_thresh,
        box_thresh=params.box_thresh,
        resolve_lines=params.resolve_lines,
        resolve_blocks=params.resolve_blocks,
        paragraph_break=params.paragraph_break,
    )


async def _run_ocr(params: OcrDocumentInput) -> tuple[Any, list[str]]:
    data, decoded_filename = io_utils.decode_input(params.source.image_b64, params.source.file_path)
    filename = params.source.filename or decoded_filename
    pages, names = io_utils.documents_from_bytes(data, filename, params.page_range)

    predictor_params = _predictor_params_from_ocr_input(params)
    predictor = await get_predictor(predictor_params)
    document = await run_predictor(predictor, pages)
    return document, names


def _build_ocr_page(page: Any, name: str) -> OcrPage:
    return OcrPage(
        name=name,
        orientation=OcrPageValue(**page.orientation),
        language=OcrLanguage(**page.language),
        dimensions=tuple(page.dimensions),
        blocks=[
            OcrBlock(
                geometry=io_utils.resolve_geometry(block.geometry),
                objectness_score=io_utils.round_confidence(block.objectness_score),
                lines=[
                    OcrLine(
                        geometry=io_utils.resolve_geometry(line.geometry),
                        objectness_score=io_utils.round_confidence(line.objectness_score),
                        words=[
                            OcrWord(
                                value=word.value,
                                geometry=io_utils.resolve_geometry(word.geometry),
                                objectness_score=io_utils.round_confidence(word.objectness_score),
                                confidence=io_utils.round_confidence(word.confidence),
                                crop_orientation=CropOrientation(**word.crop_orientation),
                            )
                            for word in line.words
                        ],
                    )
                    for line in block.lines
                ],
            )
            for block in page.blocks
        ],
    )


def register_ocr_tools(mcp: FastMCP) -> None:
    @mcp.tool
    async def ocr_document(params: OcrDocumentInput) -> OcrDocumentOutput:
        """Run full end-to-end OCR on an image or PDF.

        Returns the complete page -> block -> line -> word tree with geometry,
        confidence/objectness scores, orientation, and language. For a cheaper
        plain-text read, use ocr_document_text instead.
        """
        logger.info("tool: ocr_document")
        document, names = await _run_ocr(params)
        pages = [_build_ocr_page(page, name) for page, name in zip(document.pages, names, strict=True)]
        return OcrDocumentOutput(pages=pages)

    @mcp.tool
    async def ocr_document_text(params: OcrDocumentTextInput) -> OcrDocumentTextOutput:
        """Run OCR and return plain text only — no geometry or confidence scores.

        Uses the same predictor and parameters as ocr_document (identical
        calls to either tool share one cached predictor); this is the cheap "just
        read this document" path most callers want. Use ocr_document when
        you need bounding boxes or confidence scores.
        """
        logger.info("tool: ocr_document_text")
        document, _names = await _run_ocr(params)
        page_texts = [page.render() for page in document.pages]
        return OcrDocumentTextOutput(pages=page_texts, text=document.render())
