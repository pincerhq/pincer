"""recognize_text — text recognition only, on pre-cropped word/line images."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from doctr_mcp import io_utils
from doctr_mcp.predictors import PredictorParams, get_predictor, run_predictor
from doctr_mcp.schemas import RecognitionInput, RecognitionItem, RecognitionOutput

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register_recognition_tools(mcp: FastMCP) -> None:
    @mcp.tool
    async def recognize_text(params: RecognitionInput) -> RecognitionOutput:
        """Recognize text in a batch of pre-cropped word/line images.

        Each image should already be tightly cropped to a single line or word — use
        detect_text or ocr_document first to locate text regions in a
        full page. Returns one {text, confidence} result per input image, in order.
        """
        logger.info("tool: recognize_text (n=%d)", len(params.images))
        crops = []
        for source in params.images:
            data, decoded_filename = io_utils.decode_input(source.image_b64, source.file_path)
            filename = source.filename or decoded_filename
            pages, _ = io_utils.documents_from_bytes(data, filename, page_range=None)
            crops.extend(pages)

        predictor_params = PredictorParams(task="recognition", reco_arch=params.reco_arch, reco_bs=params.reco_bs)
        predictor = await get_predictor(predictor_params)
        raw_results = await run_predictor(predictor, crops)

        results = [
            RecognitionItem(text=text, confidence=io_utils.round_confidence(confidence))
            for text, confidence in raw_results
        ]
        return RecognitionOutput(results=results)
