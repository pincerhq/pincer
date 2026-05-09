"""
Outbound call prompt policy — specialized prompting for agent-initiated calls.

Enforces fact injection (only user-provided facts), prevents hallucination,
and structures the agent's behavior during outbound conversations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OutboundCallContext:
    """Context for an outbound call on behalf of the user."""

    user_name: str
    target_name: str
    target_number: str
    task_description: str
    known_facts: dict[str, Any] = field(default_factory=dict)

    def build_system_prompt(self) -> str:
        facts_str = json.dumps(self.known_facts, indent=2) if self.known_facts else "None provided"
        return OUTBOUND_CALL_PROMPT.format(
            user_name=self.user_name,
            target_name=self.target_name,
            task_description=self.task_description,
            facts_json=facts_str,
        )

    def build_greeting(self) -> str:
        return f"Hi, I'm calling on behalf of {self.user_name} regarding {self.task_description}."


OUTBOUND_CALL_PROMPT = """\
You are calling {target_name} on behalf of {user_name}.

YOUR TASK: {task_description}
KNOWN FACTS: {facts_json}

CRITICAL RULES:
1. Introduce yourself: "Hi, I'm calling on behalf of {user_name} regarding..."
2. ONLY state facts from KNOWN FACTS above. NEVER invent any information.
3. If asked something you don't know, say: "Let me check with {user_name} and call back."
4. Confirm what was agreed: "So to confirm, [summary]. Is that correct?"
5. Be polite, professional, and concise.
6. If the call isn't going well, gracefully end: "Thank you for your time."
7. Never share personal information beyond what's in KNOWN FACTS.
8. If you reach voicemail, leave a brief message and hang up.\
"""


def build_outbound_context(
    user_name: str,
    target_name: str,
    target_number: str,
    task_description: str,
    facts: dict[str, Any] | None = None,
) -> OutboundCallContext:
    """Create an outbound call context with the provided details."""
    return OutboundCallContext(
        user_name=user_name,
        target_name=target_name,
        target_number=target_number,
        task_description=task_description,
        known_facts=facts or {},
    )
