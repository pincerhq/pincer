"""Inbound AI receptionist (Sprint 12).

Answers inbound calls for a business: discloses being an AI, answers ONLY from
the business profile, takes structured messages, books appointments inside
real free slots, transfers to a human on request — and discloses nothing
about stored data to the (always untrusted) caller.
"""

from pincer.voice.receptionist.profile import (
    BusinessProfile,
    ProfileError,
    get_profile,
    load_business_profile,
    load_from_settings,
    receptionist_active,
    set_profile,
)
from pincer.voice.receptionist.session import ReceptionSession, TurnPlan, opening_text

__all__ = [
    "BusinessProfile",
    "ProfileError",
    "ReceptionSession",
    "TurnPlan",
    "get_profile",
    "load_business_profile",
    "load_from_settings",
    "opening_text",
    "receptionist_active",
    "set_profile",
]
