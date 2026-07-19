---
name: memory-recall
description: How Pincer's long-term memory works and how to search or reference past conversations and stored facts about a user. Load this when the user asks what you remember about them, references something from a previous conversation, or asks you to recall/forget information.
---

# Memory recall

Pincer stores per-user memories (facts, preferences, past conversation
context) in a persistent backend, separate from the current chat session.
Relevant memories are already retrieved automatically and injected into
your system prompt as a "Relevant memories about this user" block on every
turn — you usually don't need to do anything extra to use them.

## When to explicitly search memory

Reach for an explicit memory search tool (if one is registered — look for
`memory_search` or an MCP-exposed `pincer_memory_search` tool) when:

- The user references something specific from the past ("what did I tell
  you about my allergy last month?") that may not have matched the
  automatic top-3 relevance injection.
- You need more than the ~3 memories already injected into context.
- The user asks a direct "what do you remember about me" question — search
  broadly rather than relying only on what's already in context.

## What gets remembered

Memories are tied to a `pincer_user_id` (the canonical cross-channel
identity) and are searched by text relevance against the current message.
Each memory is a short fact or note, not a full transcript — don't expect
verbatim conversation recall, expect distilled facts ("prefers metric
units", "allergic to shellfish", "working on a Rust project called Foo").

## If the user asks you to forget something

There is no automatic redaction — if a memory-delete capability exists in
your current tool set, use it explicitly and confirm back to the user what
was removed. If no such tool is available, tell the user honestly that you
can't delete stored memories from this session rather than pretending to.
