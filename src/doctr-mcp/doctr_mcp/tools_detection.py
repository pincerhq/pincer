"""detect_text — text detection only, no recognition."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from doctr_mcp import io_utils
from doctr_mcp.predictors import PredictorParams, get_predictor, run_predictor
from doctr_mcp.schemas import DetectionBox, DetectionInput, DetectionOutput, DetectionResult

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register_detection_tools(mcp: FastMCP) -> None:
    @mcp.tool
    async def detect_text(params: DetectionInput) -> DetectionOutput:
        """Detect text regions in an image or PDF (no text recognition).

        Returns one result per page, each with a list of boxes/polygons — flat
        [xmin, ymin, xmax, ymax] for straight pages, flat 8-float 4-point polygons
        for rotated pages — and a confidence score where the architecture reports one.
        """
        logger.info("tool: detect_text")
        data, decoded_filename = io_utils.decode_input(params.source.image_b64, params.source.file_path)
        filename = params.source.filename or decoded_filename
        pages, names = io_utils.documents_from_bytes(data, filename, params.page_range)

        predictor_params = PredictorParams(
            task="detection",
            det_arch=params.det_arch,
            assume_straight_pages=params.assume_straight_pages,
            preserve_aspect_ratio=params.preserve_aspect_ratio,
            symmetric_pad=params.symmetric_pad,
            det_bs=params.det_bs,
            bin_thresh=params.bin_thresh,
            box_thresh=params.box_thresh,
        )
        predictor = await get_predictor(predictor_params)
        raw_results = await run_predictor(predictor, pages)

        from doctr.file_utils import CLASS_NAME

        results = []
        for doc, name in zip(raw_results, names, strict=True):
            boxes = []
            for geom in doc[CLASS_NAME]:
                if geom.shape == (5,):
                    geom_list = geom.tolist()
                    boxes.append(
                        DetectionBox(
                            geometry=tuple(geom_list[:-1]),
                            confidence=io_utils.round_confidence(geom_list[-1]),
                        )
                    )
                else:
                    boxes.append(DetectionBox(geometry=io_utils.resolve_geometry(geom[:4].tolist()), confidence=None))
            results.append(DetectionResult(name=name, boxes=boxes))

        return DetectionOutput(results=results)
