"""list_architectures — discovery tool for valid det_arch/reco_arch values.
Documentation only, not enforced: tool inputs accept any string here, per product
decision, so a caller isn't limited to a hard-coded Literal[...] enum."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from doctr_mcp.config import KNOWN_DET_ARCHS, KNOWN_RECO_ARCHS, get_settings
from doctr_mcp.schemas import ListArchitecturesInput, ListArchitecturesOutput

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register_meta_tools(mcp: FastMCP) -> None:
    @mcp.tool
    async def list_architectures(params: ListArchitecturesInput) -> ListArchitecturesOutput:
        """List doctr's supported detection/recognition architecture names.

        det_arch/reco_arch parameters on other tools accept any of these strings
        (or, in principle, any name doctr itself supports — this list is not
        enforced). Use this tool to discover valid values instead of guessing.
        """
        logger.info("tool: list_architectures")
        settings = get_settings()
        return ListArchitecturesOutput(
            detection=list(KNOWN_DET_ARCHS),
            recognition=list(KNOWN_RECO_ARCHS),
            defaults={
                "det_arch": settings.default_det_arch,
                "reco_arch": settings.default_reco_arch,
            },
        )
