# Telephony telemetry: schema, metrics, and how to diagnose a call

Companion to [`telephony-pipeline.md`](telephony-pipeline.md), which describes
what the pipeline actually does. This document is the contract: what is
recorded, what each number means, and how to use it when a call is slow or
broken.

---

## 1. Model

Three grains, because three different questions are asked of this data.

| Grain | Table | Question |
|---|---|---|
| **Call** | `telephony_calls` | which calls, how many, how did they end |
| **Event** | `telephony_events` | what happened, in what order |
| **Span** | `telephony_spans` | what ran, for how long, overlapping with what |
| **Turn** | `telephony_turns` | which turn was slow, and where the time went |

**Lifecycle states and pipeline spans are deliberately separate.** The call's
state (`active` → `connected` → `completed`/`failed`) lives on the call row and
in events; the conversation pipeline lives in spans that overlap freely. Nothing
forces a streaming pipeline into one linear state machine, because it is not
one.

### Correlation

Every record carries the same identifier set, because an incident starts from
whichever one the reporter happened to have:

| Field | What |
|---|---|
| `call_id` | ours, assigned **before** the provider names the call |
| `provider_call_id` | Twilio CallSid (empty until the dial returns) |
| `trace_id` | 32 hex, OTel-compatible, one per call |
| `span_id` / `parent_span_id` | 16 hex, OTel-compatible |
| `turn_id` | one caller-utterance → response cycle |
| `conversation_id` | the agent session the call is bound to |
| `tenant_id` | workspace/organisation, where the deployment has them |
| `direction`, `provider`, `engine`, `model`, `environment`, `app_version` | dimensions |

Propagation is by `contextvars` (`voice/telemetry/context.py`), so
`asyncio.create_task` carries it for free — which matters because the turn runs
in its own task (`voice-turn-{sid}`) and TTS runs inside that. WebSocket and
webhook handlers are separate tasks with no inherited context, so they re-attach
by CallSid through the registry.

An outbound call is registered **before** `client.calls.create()`; the temporary
key is re-bound to the real CallSid on `dial_accepted`. This is why a call Twilio
rejects outright is still in the dashboard.

### Clocks

- Durations: `time.monotonic_ns()`, both readings in the same process. Always.
- Ordering across processes: `datetime.now(UTC)`.
- Cross-service latency by subtracting unsynchronised clocks: **never**.
  `clock.cross_process_gap()` exists only to return `None` and be greppable.

Monotonic stamps are projected onto the wall clock for display via a
process-wide anchor, so a span can be placed on a timeline next to a provider
webhook — but that projection never produces a duration.

---

## 2. Events

| Name | Meaning |
|---|---|
| `call.inbound_webhook` | Twilio POSTed `/webhook` |
| `call.registered` | call state created |
| `call.dial_requested` / `dial_accepted` / `dial_rejected` | outbound REST dial |
| `call.provider_status` | a status callback (deduplicated on Twilio's `SequenceNumber`) |
| `call.amd_verdict` | answering-machine detection result |
| `call.media_stream_open` | relay `setup` / media `start` |
| `call.answered` | callee picked up — **every clock starts here** |
| `call.first_inbound_audio` | first media frame (Media Streams only) |
| `call.media_stream_closed` | socket gone |
| `call.ended` | teardown, with failure code and termination reason |
| `call.declined` | refused by policy before the engine saw it |
| `call.phase` | a lifecycle state transition (`CallStateMachine`), with its reason |
| `stt.speech_start` / `speech_end` | caller speech bounds (Media Streams only) |
| `stt.partial` / `stt.final` | recognition output |
| `stt.endpoint_decision` | the provider decided the utterance ended |
| `turn.start` / `turn.end` / `turn.cancelled` / `turn.handled_locally` | turn lifecycle |
| `agent.prep_done` | session load + memory + prompt assembly finished |
| `llm.request` / `first_token` / `done` | one generation, per tool-loop iteration |
| `tool.start` / `tool.end` | per tool **and per attempt** |
| `tts.request` / `first_audio` / `done` | synthesis (Media Streams only) |
| `audio.queued` / `audio.dispatched` | handed to, then written to, the provider |
| `audio.playback_mark` | provider playback signal — **not emitted today** |
| `bargein.detected` / `audio.buffer_cleared` | interruption handling |
| `audio.gap` | inbound frames stopped arriving for >120 ms |
| `error` / `timeout` / `reconnect` | with a stable code and a `stage` |

## 3. Spans

`call`, `call.setup`, `call.media`, `turn`, `agent.prep`, `llm.generation`,
`tool.execution`, `tool.approval`, `tts.synthesis`, `audio.outbound`,
`stt.utterance`.

Status: `ok`, `error`, `timeout`, `cancelled`, `denied`, `deferred`.
`attempt` distinguishes a retry from the original — a resumed TTS segment and a
retried tool each get their own bar.

**Spans overlap.** Their durations are never summed anywhere. The only place
stage time is added up is the per-turn critical path, which partitions the
window instead.

---

## 4. Metric definitions

Served live at `GET /api/telephony/metrics` and rendered next to every number in
the UI, so the definition travels with the value.

| Metric | Start → End | Source | Available |
|---|---|---|---|
| `call_setup_ms` | dial requested → answered | server-measured | outbound |
| `media_establish_ms` | answered → media stream open | server-measured | both |
| `first_inbound_audio_ms` | media open → first frame | server-measured | Media Streams |
| `endpointing_ms` | speech end → endpointing decision | server-measured | Media Streams |
| `stt_first_partial_ms` | speech start → first partial | server-measured | Media Streams |
| `stt_final_ms` | speech end → final transcript | server-measured | Media Streams |
| `agent_queue_ms` | final transcript → turn start | server-measured | both |
| `agent_prep_ms` | turn start → prep done | server-measured | both |
| `llm_ttft_ms` | LLM request → first token | server-measured | both |
| `llm_total_ms` | LLM request → done | server-measured | both |
| `tool_ms` | tool start → tool end, per tool per attempt | server-measured | both |
| `tts_first_audio_ms` | TTS request → first chunk | server-measured | Media Streams |
| `tts_total_ms` | TTS request → done | server-measured | Media Streams |
| `audio_queue_ms` | chunk queued → written to provider | server-measured | both |
| `response_latency_ms` | caller speech end → first response audio **sent** | server-measured | both |
| `playback_response_latency_ms` | speech end → provider playback signal | provider-reported | **none** |
| `caller_perceived_latency_ms` | acoustic → acoustic | — | **none** |
| `interruption_latency_ms` | barge-in detected → buffer clear written | server-measured | Media Streams |
| `transport_rtt_ms` / jitter / loss | — | — | **none** |
| `audio_gap_ms` | previous frame → next frame | server-measured | Media Streams |

### The four kinds of latency, kept apart

1. **Server-measured** — both boundaries are monotonic readings inside this
   process. Everything above marked as such.
2. **Provider-reported** — the provider told us; we did not observe it. Nothing
   qualifies today.
3. **Estimated** — derived from something adjacent. On ConversationRelay the
   response clock starts at *transcript arrival* rather than speech end, which
   makes the number **smaller than reality** by the endpointing wait. Every turn
   records which of the two it used in `response_latency_source`.
4. **Caller-perceived** — what the human actually experienced. **Not available.**
   It needs an audio probe on the PSTN leg. `response_latency_ms` is a lower
   bound for it, never a substitute.

> **Sent is not heard.** `response_latency_ms` ends when the first response
> audio (Media Streams) or first text token (ConversationRelay) was written to
> the provider socket. Twilio buffers outbound audio and acknowledges nothing.
> The UI never labels this as playback.

### Critical path to first response audio

Stage durations overlap, so a stacked bar of them is wrong twice: it double
counts, and it misnames the bottleneck. Instead, the window
`[caller speech end, first response audio]` is **partitioned**:

- cut it at every span boundary inside it;
- in each slice, attribute the time to the *deepest active stage* in the
  pipeline's dependency order (while TTS produces the first chunk we are waiting
  on TTS, even though the LLM span is still open);
- slices with no active span are `unattributed` (usually scheduler latency) —
  named, not folded into a neighbour.

The segments therefore sum to the response latency **exactly**, which is what
makes the bar safe to read as a budget. Work that happens *after* first audio
(the rest of the LLM stream, remaining sentences) is in `total_ms` and is
deliberately excluded from first-response latency.

---

## 5. Aggregation, retention, sampling

**Percentiles** come off fixed-bucket histograms built from raw observations
(`voice/telemetry/histogram.py`). Percentiles are never averaged; a comparison
builds a separate histogram per group. Every aggregate ships its `count` and a
`sufficient_samples` flag (`PINCER_TELEPHONY_MIN_SAMPLES`, default 20).

**Rates** ship their numerator and denominator, and their denominator's
definition:

| Rate | Denominator |
|---|---|
| connection rate | attempted calls, **excluding** those a policy declined before dialling |
| technical failure rate | all terminated calls; only `technical` failures are the numerator |
| unexpected disconnect rate | calls that connected; numerator is `ws_drop`, `no_audio`, `stuck`, `twilio_api`, `twiml_error`, `call_setup` |

A rate with an empty denominator is `null`, not `0%` — "no calls" is not "0%
success".

**Failure categories** (`voice/telemetry/outcomes.py`) reuse the existing
`FailureCode` taxonomy: `none`, `technical`, `callee_unavailable`,
`policy_declined`, `ended_by_party`, `unknown`. A busy line and a blocklisted
caller are not defects and never enter the technical failure rate.

**Sampling** is head-based and **per call**
(`PINCER_TELEPHONY_TELEMETRY_SAMPLE_RATE`, default `1.0`), because half a call's
spans is not a diagnosis. Lifecycle events — registered, answered, ended, the
failure code — are recorded for **every** call regardless, so the call table is
never missing rows and a failed call is always diagnosable at the coarse grain.
What sampling drops is turn-level detail; such a call is marked
`coverage='lifecycle_only'` and the overview reports how many calls that was.

**Retention**: `PINCER_TELEPHONY_TELEMETRY_RETENTION_DAYS` (default 30), purged
by the same `retention_purge` cron as the rest of voice. Technical telemetry
gets its own window because it carries no transcript and no audio — diagnosing a
latency regression needs weeks of history; keeping what was *said* that long
would not be justifiable.

**Duplicates and out-of-order**: provider-sourced events carry a deterministic
id (`sha1(call_id|name|SequenceNumber)`), so Twilio's retries collapse to one
row. Nothing depends on arrival order; rows are ordered at read time by
`(ts_utc, seq)`, so a callback that lands minutes after teardown appears where it
belongs.

---

## 6. Privacy and access

- Every `/api/telephony/*` endpoint is behind the dashboard bearer token and is
  **tenant-scoped, failing closed**: a caller restricted to an empty set reads
  nothing, and the `X-Pincer-Tenant` header can only narrow, never widen.
- Phone numbers are masked **at write time**. The tables never hold a full
  number.
- Raw audio, transcripts, prompts, credentials and tool arguments/results are
  rejected by `records.safe_attributes()` before anything is queued — they never
  enter these tables at all.
- Recording and transcript access stays where it was, behind
  `/api/voice/calls/{sid}` with its own permissions and its own shorter
  retention.
- `call_id`, CallSid and trace ids live in traces and logs — **never as metric
  labels**, matching the existing rule in `observability/metrics.py`.

---

## 7. Alerts

`voice/telemetry/alerts.py`, evaluated over a configurable window. Every rule
has a threshold, an evaluation window, and a minimum sample size; a rule below
its minimum reports `insufficient_data` and **cannot fire**.

| Rule | Default | Setting |
|---|---|---|
| response latency p95 | > 2000 ms / 30 min | `PINCER_ALERT_RESPONSE_LATENCY_P95_MS` |
| connection rate | < 90 % | `PINCER_ALERT_CONNECTION_RATE_MIN` |
| technical failure rate | > 5 % | `PINCER_ALERT_TECHNICAL_FAILURE_RATE_MAX` |
| unexpected disconnects | > 2 % | `PINCER_ALERT_UNEXPECTED_DISCONNECT_RATE_MAX` |
| STT/LLM/TTS/tool timeouts | > 3 in window | `PINCER_ALERT_STAGE_TIMEOUT_MAX` |
| media connection failures | > 3 in window | `PINCER_ALERT_STAGE_TIMEOUT_MAX` |
| audio queue residence p95 | > 250 ms | `PINCER_ALERT_AUDIO_QUEUE_P95_MS` |
| telemetry export coverage | < 95 % | `PINCER_ALERT_TELEMETRY_COVERAGE_MIN` |

Thresholds should be set from **observed baselines**, not from these defaults.
Run the pipeline for a week, read the p95 off the Telephony page for your own
traffic, and set the threshold above it — then tighten toward the SLO
(`PINCER_SLO_LATENCY_P95_S`, default 2.0 s) as the pipeline improves.

Every firing alert carries a `dashboard_filter` and the call ids that caused it.
An alert you cannot click through to the evidence is a pager that teaches people
to ignore pagers.

---

## 8. Measured instrumentation overhead

From `tests/test_telephony_overhead.py`, on an Apple M-series laptop:

| Measurement | Result |
|---|---|
| CPU per instrumented turn (~11 records) | **≈ 200 µs** |
| 25 concurrent calls × 8 turns | worst audio-loop scheduling delay **6 ms** (frame budget is 20 ms) |
| Wedged sink, 100 turns | emitted in **8 ms**, records dropped, **no backpressure** |

The design constraint is architectural, not numeric: `emit` is synchronous and
`put_nowait`s onto a bounded queue. When the queue is full, records are dropped
and counted. Nothing on the audio path ever awaits the sink, so a wedged
database costs telemetry, never audio.

Re-measure with:

```bash
uv run pytest tests/test_telephony_overhead.py -s --no-cov
```

---

## 9. Diagnosing a slow or failed call

### A call was slow

1. **Telephony → filter the window**, then read the **telemetry banner first**.
   If records were dropped, every chart below is incomplete and an apparent
   improvement may just be missing data. Fix that before trusting anything.
2. Look at **response latency p95** and its **sample count**. A p95 over nine
   turns is noise; the UI flags it.
3. Open **Slowest turns**. Each row already names its *measured* bottleneck
   stage — the stage that owned the largest share of the response window.
4. Click through to the call, open that turn, and read its **critical path**.
   The segments sum to the response latency exactly, so the largest segment is
   the answer, not a suspicion.
5. Expand the **waterfall** for that turn to see the supporting spans, including
   the overlap. A `tool.execution` span at attempt 2 means a retry; a
   `tool.approval` span means a human was holding the line and no amount of
   engineering will shorten it.
6. Check `response_latency_source` on the turn. On ConversationRelay it is
   `transcript_arrival`, which means **the real figure is larger** by the
   endpointing wait — you cannot compare that number to a Media Streams one.

### A call failed

1. Find it in the call table (search by CallSid) — the row exists even for calls
   that never connected and even for calls that were not sampled.
2. Read `failure_code`, `failure_category` and `termination_reason` on the call
   header. `callee_unavailable` and `policy_declined` mean nothing is broken.
3. The **telemetry gaps** banner says what is missing and why, so a sparse page
   reads as a known limit rather than a bug.
4. Open the **event timeline**. `error`, `timeout` and `reconnect` events carry a
   `stage`, which names the component. `call.provider_status` rows show what
   Twilio thought was happening, which is frequently different from what we did.
5. For "the caller heard nothing": look for agent turns with no
   `audio.dispatched`, or a `call.ended` with failure code `no_audio`. The
   existing silent-agent classification already reclassifies these.

### Something looks impossible

Check the **Not measurable here** panel. ConversationRelay hands us text, not
audio: there is no TTS latency, no endpointing delay, and no barge-in timing to
be had on that engine, and a chart claiming otherwise would be measuring
something else.

### Common shapes

| Symptom | Likely reading |
|---|---|
| High `agent_queue_ms` | previous turn still cancelling — barge-in churn |
| High `agent_prep_ms` | memory search or prompt assembly, not the model |
| High `llm_ttft_ms`, low `llm_total_ms` | provider queueing, not generation |
| `tool.execution` owns the path | a slow integration; check the tool's own attempt spans |
| `tool.approval` owns the path | a human was asked to approve; not a latency bug |
| High `audio_queue_ms` | we are producing audio faster than we can ship it |
| `unattributed` owns the path | event-loop scheduling — look for blocking work on the loop |
