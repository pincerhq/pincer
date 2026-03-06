"""
Confirmation gates — mandatory verbal confirmation before consequential actions.

Every action that spends money, modifies schedules, sends messages, or shares
personal data MUST pass through a confirmation gate during voice calls.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


class ActionCategory(StrEnum):
    SPENDING = "spending"
    SCHEDULING = "scheduling"
    MESSAGING = "messaging"
    DATA_SHARING = "data_sharing"
    CANCELLATION = "cancellation"
    CALLING = "calling"
    OTHER = "other"


class ConfirmationStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNCLEAR = "unclear"


@dataclass
class ConfirmationGate:
    category: ActionCategory
    description: str
    prompt: str
    status: ConfirmationStatus = ConfirmationStatus.PENDING


CONFIRMATION_PATTERNS: dict[ActionCategory, str] = {
    ActionCategory.SPENDING: "This will cost {details}. Should I go ahead?",
    ActionCategory.SCHEDULING: "I'll book {details}. Confirm?",
    ActionCategory.MESSAGING: "I'll send {details}. OK?",
    ActionCategory.DATA_SHARING: "They're asking for your {details}. Should I share it?",
    ActionCategory.CANCELLATION: "This will cancel {details}. Are you sure?",
    ActionCategory.CALLING: "I'll call {details}. Proceed?",
    ActionCategory.OTHER: "I'm going to {details}. Is that correct?",
}

AFFIRMATIVE_PATTERNS = re.compile(
    r"\b(yes|yeah|yep|yup|sure|go ahead|do it|correct|confirmed|absolutely|"
    r"that's right|proceed|affirmative|ok|okay|sounds good|perfect|right|"
    r"go for it|please do|of course)\b",
    re.IGNORECASE,
)

NEGATIVE_PATTERNS = re.compile(
    r"\b(no|nah|nope|don't|stop|wait|hold on|cancel|never mind|"
    r"not yet|hold off|scratch that|forget it|negative|wrong|"
    r"that's wrong|incorrect|actually no)\b",
    re.IGNORECASE,
)


def classify_action(tool_name: str, arguments: dict) -> ActionCategory:
    """Classify a tool call into an action category for confirmation."""
    spending_tools = {"make_payment", "purchase", "order", "buy"}
    scheduling_tools = {"calendar_create", "schedule", "book", "reschedule"}
    messaging_tools = {"email_send", "send_message", "sms_send"}
    calling_tools = {"make_phone_call"}
    cancel_tools = {"cancel", "delete", "remove", "unsubscribe"}

    name_lower = tool_name.lower()

    if name_lower in calling_tools or "call" in name_lower:
        return ActionCategory.CALLING
    if name_lower in spending_tools or "pay" in name_lower or "cost" in str(arguments):
        return ActionCategory.SPENDING
    if name_lower in scheduling_tools or "calendar" in name_lower:
        return ActionCategory.SCHEDULING
    if name_lower in messaging_tools or "email_send" in name_lower:
        return ActionCategory.MESSAGING
    if name_lower in cancel_tools or "cancel" in name_lower:
        return ActionCategory.CANCELLATION

    return ActionCategory.OTHER


def build_confirmation_prompt(category: ActionCategory, details: str) -> str:
    """Build a natural-language confirmation prompt for the given action."""
    template = CONFIRMATION_PATTERNS.get(category, CONFIRMATION_PATTERNS[ActionCategory.OTHER])
    return template.format(details=details)


def parse_confirmation(utterance: str) -> ConfirmationStatus:
    """Parse a user's verbal response as confirmation, rejection, or unclear."""
    text = utterance.strip()
    if not text:
        return ConfirmationStatus.UNCLEAR

    has_affirmative = bool(AFFIRMATIVE_PATTERNS.search(text))
    has_negative = bool(NEGATIVE_PATTERNS.search(text))

    if has_affirmative and not has_negative:
        return ConfirmationStatus.CONFIRMED
    if has_negative and not has_affirmative:
        return ConfirmationStatus.REJECTED
    if has_affirmative and has_negative:
        return ConfirmationStatus.UNCLEAR

    return ConfirmationStatus.UNCLEAR


def create_gate(tool_name: str, arguments: dict, description: str = "") -> ConfirmationGate:
    """Create a confirmation gate for a tool call."""
    category = classify_action(tool_name, arguments)
    if not description:
        description = f"{tool_name} with {arguments}"

    prompt = build_confirmation_prompt(category, description)

    return ConfirmationGate(
        category=category,
        description=description,
        prompt=prompt,
    )


def requires_confirmation(tool_name: str) -> bool:
    """Check if a tool call requires verbal confirmation during a voice call."""
    no_confirm_tools = {
        "web_search", "calendar_today", "calendar_week",
        "email_check", "email_read", "email_search",
        "email_list_folders", "file_read", "file_list",
    }
    if tool_name in no_confirm_tools:
        return False
    return True
