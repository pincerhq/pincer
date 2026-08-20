# Voice Latency Log (Sprint 5 measurement protocol)

Running record of voice-to-voice latency measurements. **Append one row per
measurement** (a call or a `pincer voice latency-report` snapshot) — never
rewrite history; the point is seeing the trend across changes.

## How the measurement works

Every streamed turn writes one JSON line to `data_dir/logs/voice_latency.jsonl`
(and a `TURN_LATENCY` log line). `pincer voice latency-report [-n N]`
aggregates the last N calls into p50/p95 per stage.
`pincer voice latency-model [-n N] [--sort p50|name] [--json]` groups the same
turns by the LLM model that served them (the `turn_model` stamp) — one row per
model with turns/calls/errors and p50/p95 of `total`, `llm_first_token`,
`first_dispatch`, `llm_done` — so a Sonnet-vs-Haiku-vs-GPT A/B is read off
directly instead of averaged together (see snapshot S4 below for why).
Turns recorded before the model stamp existed land in an `unknown` row.

The clock starts when the final STT transcript reaches the channel (the
caller has stopped speaking and Twilio finished transcribing — endpointing
time *before* that is Twilio-side and not in these numbers):

| Stage | Meaning | What moves it |
|---|---|---|
| `prep_ms` | session load, memory search, prompt assembly — everything before the LLM request | our code (currently ~6ms, solved) |
| `llm_first_token_ms` | provider time-to-first-token, network included | model tier, prompt cache, API RTT |
| `first_sentence_ms` | first complete sentence assembled from the token stream | sentence length; usually TTFT + ~300ms |
| `first_dispatch_ms` | first audio handed to Twilio — **what the caller experiences** | everything above |
| `llm_done_ms` | full generation finished (off the critical path thanks to streaming) | answer length |
| `total_ms` | turn total (≈ llm_done; audio started earlier at first_dispatch) | — |

Per-turn records also carry `turn_model` (the model that actually served the
turn), `cache_read_tokens` (Anthropic tools-prefix cache hit), and
`input_tokens` — so every row below is attributable.

Targets (Sprint 5): **p50 ≤ 1200ms / p95 ≤ 2000ms** on `total_ms`;
stretch p50 ≤ 900ms. Note the caller-perceived number is `first_dispatch`.

## Results

All 2026-08-19 calls: ConversationRelay engine, ngrok free tunnel (dev
machine, no EU deploy yet), Sonnet = claude-sonnet-4-5, Haiku =
claude-haiku-4-5-20251001. Times local (Europe/Berlin).

| # | When | Call | Model | Tools cache | Turns | total p50 | TTFT p50 | Notes |
|---|------|------|-------|-------------|-------|-----------|----------|-------|
| 1 | 08-19 17:39 | …9925d5 | Sonnet 4.5 | — | 5 | 2412 | 2056 | first streamed call; baseline for the streaming pipeline |
| 2 | 08-19 18:08 | …97ab95 | Sonnet 4.5 | — | 5 | 3587 | 2167 | the 2-min-cutoff + German-switch-silence call; includes a drift-regen turn (5084ms) — both bugs fixed after |
| 3 | 08-19 18:36 | …8597bb | Sonnet 4.5 | 25 952 tok | 11 | 2878 | 1757 | tools-prefix prompt cache live (write turn 1, hits after) |
| 4 | 08-19 18:40 | …e23f3d | Sonnet 4.5 | 25 952 tok | 2 | 2046 | 1805 | cache hit across calls (5-min TTL) |
| 5 | 08-19 19:20 | …1094d3 | Haiku 4.5 | 25 952 tok | 6 | 1797 | 1208 | Ukrainian switch worked instantly; summarizer off critical path |
| 6 | 08-19 19:37 | …b8b0de | Haiku 4.5 | 25 952 tok | 7 | 2107 | 1341 | |
| 7 | 08-19 19:39 | …9f6745 | Haiku 4.5 | 25 952 tok | 11 | 1490 | 1180 | best call so far; first_dispatch p50 well under 1.5s |
| 8 | 08-19 19:45 | …7f1c53 | GPT-4o mini | — | 2 | — | — | FAILED: 160 tools > OpenAI's 128 limit (400 every turn) → voice tool cap + provider backstop shipped |

### Aggregate snapshots (`latency-report` screenshots)

| # | When | Scope | total p50 | total p95 | TTFT p50 | Notes |
|---|------|-------|-----------|-----------|----------|-------|
| S1 | 08-19 17:42 | 5 turns / 1 call | 2412 | 4950 | 2056 | Sonnet, pre-cache |
| S2 | 08-19 18:12 | 10 turns / 2 calls | 2563 | 6694 | 2112 | includes bug-call #2 |
| S3 | 08-19 18:44 | 23 turns / 4 calls | 2658 | 5463 | 1863 | first prep_ms split (6ms) |
| S4 | 08-19 19:53 | 49 turns / 8 calls | 2300 | 4963 | 1648 | mixed models — averages Sonnet history with Haiku; see per-call rows for the real trend |

### Trend reading (as of S4)

Sonnet no-cache → Sonnet cached → Haiku: total p50 **2412 → ~2400 → ~1500–1800**,
TTFT **~2050 → ~1780 → ~1200**. Pipeline overhead after the first token is a
constant ~300ms. Remaining gap to the 1.2s target sits in provider TTFT +
tunnel RTT — next levers: GPT-4o mini A/B (retest after the tool-cap fix) and
the Sprint 7 EU deployment (removes the ngrok hop, EU locality).
