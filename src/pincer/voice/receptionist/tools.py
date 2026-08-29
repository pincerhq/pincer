"""
Receptionist tools (Sprint 12 §9) — ``business_profile_lookup`` (Tier R).

The only knowledge tool an inbound caller can reach. It returns profile
facts (hours, address, services, FAQ answers) and nothing else: no calendar
contents, no owner data, no other callers. The free/busy tool used for
booking is the Sprint 11 ``google__check_freebusy`` — it returns busy/free
windows only, never event contents.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from pincer.voice.receptionist.profile import get_profile

if TYPE_CHECKING:
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

TOPICS = ("hours", "address", "services", "faq", "all")


def make_business_profile_lookup() -> Any:
    async def business_profile_lookup(topic: str = "all", question: str = "") -> str:
        """Look up the business profile: opening hours, address, services, and the
        answers to frequently asked questions. This is the ONLY source for answers
        to caller questions; anything not in it must be passed on as a message.
        """
        profile = get_profile()
        if profile is None:
            return "Error: no business profile is loaded."
        topic = str(topic or "all").strip().lower()
        if topic not in TOPICS:
            topic = "all"
        lang = profile.default_language
        data: dict[str, Any] = {}
        if topic in ("hours", "all"):
            data["hours"] = profile.speakable_hours(lang)
        if topic in ("address", "all"):
            data["address"] = profile.address
        if topic in ("services", "all"):
            data["services"] = list(profile.services)
        if topic in ("faq", "all"):
            if question:
                item = profile.faq_lookup(question)
                data["faq"] = [{"q": item.q, "a": item.a}] if item else []
            else:
                data["faq"] = [{"q": i.q, "a": i.a} for i in profile.faq]
        data["business_name"] = profile.business.name
        return json.dumps(data, ensure_ascii=False)

    return business_profile_lookup


def register_receptionist_tools(registry: ToolRegistry) -> list[str]:
    registry.register(
        name="business_profile_lookup",
        description=(
            "Business profile of this phone line: opening hours, address, services, and FAQ answers. "
            "The ONLY source for answering caller questions."
        ),
        handler=make_business_profile_lookup(),
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "enum": list(TOPICS), "default": "all"},
                "question": {
                    "type": "string",
                    "description": "Caller question to match against the FAQ",
                    "default": "",
                },
            },
            "required": [],
        },
        require_approval=False,
    )
    return ["business_profile_lookup"]


__all__ = ["TOPICS", "make_business_profile_lookup", "register_receptionist_tools"]
