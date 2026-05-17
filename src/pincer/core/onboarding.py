"""
First-session onboarding constants.

A single warm question is appended to the assistant's very first reply in a
fresh session. The user's next message (whatever it is) closes the gate; an
LLM extraction call pulls profile fields out of it.
"""

from __future__ import annotations

ONBOARDING_QUESTION_EN = (
    "By the way — what's your name, and what will you mainly use me for? "
    "Feel free to also mention your preferred language."
)

# Injected into the system prompt only for the turn that handles the user's
# reply to the onboarding question.
ONBOARDING_FOLLOWUP_INSTRUCTION = """\
[Onboarding context]
You just asked the user a single warm question about their name, use-case, and
preferred language. The user's next message is their reply.

Rules for handling the reply:
- Acknowledge what they shared briefly and warmly.
- If they mentioned a preferred language explicitly, use it from now on.
- If they did NOT mention a language, infer it from the language they're writing in.
  Do not ask about language unless they used a language different from their first message.
- It is OK to ask ONE gentle follow-up if something obvious is missing (e.g. their name).
  Never ask twice. Never treat this like a form.
- Then continue normally with whatever they want to do.
"""

# Always-on guidance about how to use a user's profile (name / use_case /
# language) when it's available via memory or session metadata.
PROFILE_USAGE_INSTRUCTION = (
    "The user's profile (name, use-case, preferred language) may be available in your context. "
    "Use it naturally — address them by name occasionally, tune defaults to their use-case, "
    "respond in their preferred language. Do not quiz the user about their profile or ask them "
    "to confirm fields. If a field is missing and naturally relevant in conversation, you may "
    "ask ONCE in a light, conversational way — never as part of a form-feeling sequence."
)

EXTRACTION_PROMPT_TEMPLATE = """\
Extract profile fields from the user's reply. Return STRICT JSON only.

User reply:
\"\"\"{reply}\"\"\"

Return a JSON object with these keys (use null when not mentioned or not inferable):
- "name":     the user's name, as a short string. null if not stated.
- "use_case": what they want to use the assistant for, as a short phrase. null if not stated.
- "language": ISO 639-1 code of their PREFERRED language.
              Prefer an EXPLICIT statement ("I prefer German") over inference.
              If only inferable from the language they wrote in, return that code.
              null only if truly unclear.

Output ONLY the JSON object. No prose, no markdown fences.
"""
