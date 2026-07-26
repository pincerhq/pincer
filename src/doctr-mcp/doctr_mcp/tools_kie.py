"""extract_key_info — key-information extraction (KIE)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from doctr_mcp import io_utils
from doctr_mcp.predictors import PredictorParams, get_predictor, run_predictor
from doctr_mcp.schemas import (
    CropOrientation,
    KieClassPredictions,
    KieInput,
    KieItem,
    KieOutput,
    KiePage,
    OcrLanguage,
    OcrPageValue,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register_kie_tools(mcp: FastMCP) -> None:
    @mcp.tool
    async def extract_key_info(params: KieInput) -> KieOutput:
        """Run key-information extraction (KIE) on an image or PDF.

        Returns, per page, predictions grouped by detected class name — each
        prediction has value, geometry, objectness/confidence scores, and crop
        orientation. Unlike ocr_document, output is organized by class, not
        as a block/line tree.
        """
        logger.info("tool: extract_key_info")
        data, decoded_filename = io_utils.decode_input(params.source.image_b64, params.source.file_path)
        filename = params.source.filename or decoded_filename
        pages, names = io_utils.documents_from_bytes(data, filename, params.page_range)

        predictor_params = PredictorParams(
            task="kie",
            det_arch=params.det_arch,
            reco_arch=params.reco_arch,
            assume_straight_pages=params.assume_straight_pages,
            preserve_aspect_ratio=params.preserve_aspect_ratio,
            symmetric_pad=params.symmetric_pad,
            detect_language=params.detect_language,
            det_bs=params.det_bs,
            reco_bs=params.reco_bs,
            bin_thresh=params.bin_thresh,
            box_thresh=params.box_thresh,
        )
        predictor = await get_predictor(predictor_params)
        document = await run_predictor(predictor, pages)

        kie_pages = [
            KiePage(
                name=name,
                orientation=OcrPageValue(**page.orientation),
                language=OcrLanguage(**page.language),
                dimensions=tuple(page.dimensions),
                predictions=[
                    KieClassPredictions(
                        class_name=class_name,
                        items=[
                            KieItem(
                                value=prediction.value,
                                geometry=io_utils.resolve_geometry(prediction.geometry),
                                objectness_score=io_utils.round_confidence(prediction.objectness_score),
                                confidence=io_utils.round_confidence(prediction.confidence),
                                crop_orientation=CropOrientation(**prediction.crop_orientation),
                            )
                            for prediction in predictions
                        ],
                    )
                    for class_name, predictions in page.predictions.items()
                ],
            )
            for page, name in zip(document.pages, names, strict=True)
        ]

        return KieOutput(pages=kie_pages)
