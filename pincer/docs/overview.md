# Overview & Purpose

## What is Pincer?

Pincer is a **personal AI agent** that lives in your messaging apps. You text it on Telegram (with WhatsApp, Discord, and web planned), and it can:

- **Chat** using Claude or GPT models with full conversation context
- **Search the web** for current information (Tavily or DuckDuckGo)
- **Execute shell commands** on your machine (with safety controls)
- **Read and write files** in a sandboxed workspace
- **Browse web pages** and take screenshots (via Playwright)
- **Run Python code** in isolated subprocesses
- **Process voice notes** via OpenAI Whisper transcription
- **Analyze images** using multimodal LLM vision capabilities
- **Handle file uploads** (text, PDF, binary) with inline content extraction
- **Remember context** across conversations using FTS5 full-text search
- **Auto-summarize** long conversations to stay within token limits
- **Track costs** per API call with configurable daily budgets
- **Stream responses** token-by-token for real-time feedback

## Design Philosophy

1. **Personal-first**: Pincer is designed for a single user or small group, not enterprise scale. It runs on your machine or a VPS, talking to LLM APIs.

2. **Provider-agnostic**: The core agent never imports Anthropic or OpenAI directly. A `BaseLLMProvider` abstraction means switching providers is a config change.

3. **Tool-augmented (ReAct)**: The agent follows a [ReAct](https://arxiv.org/abs/2210.03629) loop — it reasons about the user's request, picks a tool, observes the result, and repeats until it has a final answer.

4. **Safety-conscious**: Shell commands are blocked by dangerous-pattern regex. File operations are sandboxed. Cost budgets prevent runaway spend. User allowlists restrict access.

5. **Async-native**: Everything is `async/await` from the ground up — LLM calls, database access, tool execution, and channel I/O.

## Current Status

| Component | Status |
|-----------|--------|
| Core Agent (ReAct loop) | Implemented |
| Anthropic Provider | Implemented |
| OpenAI Provider | Implemented |
| Ollama Provider | Placeholder |
| Telegram Channel | Implemented |
| WhatsApp Channel | Placeholder |
| Discord Channel | Placeholder |
| Web Channel | Placeholder |
| Memory (FTS5) | Implemented |
| Memory (Vector / Embeddings) | Schema ready, search implemented |
| Conversation Summarizer | Implemented |
| Tool Registry | Implemented |
| Built-in Tools (7) | Implemented |
| Security (Firewall, Rate Limit, Audit) | Placeholders |
| Scheduler | Placeholder |
| Dashboard | Placeholder |
| CLI | Implemented |
| Docker Support | Implemented |
| CI (GitHub Actions) | Implemented |
