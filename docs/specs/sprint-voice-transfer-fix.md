# Sprint spec — Voice: make receptionist transfers survive and finish cleanly

| | |
|---|---|
| **Issue** | "Voice: receptionist transfer to a human is hung up ~30 s after it starts" (P0) |
| **Baseline** | `pincerhq/pincer@dev` `223bff1` (2026-10-19) |
| **Area** | `voice/state_machine.py`, `voice/engine.py`, `voice/twiml_server.py`, `voice/receptionist/session.py`, `channels/phone_calls.py`, `observability/golden_signals.py`, `config/channels.py` |
| **Estimate** | 3 dev-days + 0.5 day live verification |
| **Proposed location in repo** | `docs/specs/sprint-voice-transfer-fix.md` |

---

## 1. Problem

The receptionist hands a caller to a human by redirecting the live call into `<Dial>`. From that moment no one on our side owns the call correctly:

| # | Defect | Where | Effect |
|---|--------|-------|--------|
| D1 | `TRANSFERRING` has a 30 s phase timeout, and the watchdog enforces it on a call that left us for `<Dial>` | `state_machine.py:110`, `phone_calls.py:414-420, 493-512` | Parent call is hung up (`end_call` → `update(status="completed")`). The bridge drops ~30 s after the transfer starts, so the caller gets ~5–10 s with the human. Outcome is `FAILED/timeout_transferring`. **Reproduced.** |
| D2 | A successful transfer is never finalized | `twiml_server.py:274-287` (`/transfer-result` completed → `<Hangup/>` only). Inbound calls have no status callback. | Once D1 is fixed, the call state stays in `engine._active_calls` until the reaper. The post-call report never runs. |
| D3 | The stuck-call reaper treats a long bridged call as stuck | `golden_signals.py:307-337`, `phone_calls.py:430-460` | A conversation with staff that runs over 660 s is hung up, and on-call gets a false `PAGE` alert. |
| D4 | Receptionist capacity counts bridged calls | `twiml_server.py:203-208` | With `inbound_max_concurrent=3`, three staff calls block every new caller. |
| D5 | `/status completed` uses `end_call` (REST hangup) on a call Twilio already ended | `twiml_server.py` `/status` completed branch | Logs a spurious "Failed to end call" exception on every normal end, and adds a pointless blocking REST round-trip. |

D1 is the customer-facing bug. D2–D4 appear as soon as D1 is fixed, so all of them go into one sprint. D5 is a one-line consequence of the D2 design.

## 2. Goals and non-goals

**Goals**

- A transferred call is never ended by Pincer while the caller and the human are talking, up to an explicit ceiling.
- Each transferred call is finalized exactly once, when Twilio tells us it ended, with no REST hangup. The post-call pipeline runs and the report says the call was transferred.
- The busy / no-answer / failed fallback to message-taking keeps working unchanged.
- Bridged calls don't page. They keep their receptionist capacity slot (see D-1).
- Regression tests cover each defect.

**Non-goals**

- Warm or attended transfer, transfer to multiple targets, or routing by department. The dead `voice/transfer.py` (`WarmTransfer`) is out of scope and gets removed in the cleanup sprint.
- Moving Twilio REST calls off the event loop. That is a separate P0 issue; this sprint only avoids adding new blocking calls. (*Revised in review:* the transfer redirect in `transfer_call` now runs in `asyncio.to_thread`, since both engines share it; the other `Client(...)` call sites stay for the P0.)
- Recording the bridged leg.

## 3. Design

### 3.1 Phases that the watchdog doesn't time (fixes D1)

`src/pincer/voice/state_machine.py`

```python
# Phases in which the call is not ours to time: the media session is gone
# (e.g. the call is bridged to a human via <Dial>). Their end is signalled by
# Twilio (/transfer-result, /status), and the ceiling is the Dial timeLimit.
UNTIMED_PHASES: frozenset[CallPhase] = frozenset({CallPhase.TRANSFERRING})
```

- Remove `CallPhase.TRANSFERRING` from `PHASE_TIMEOUTS`.
- `check_timeout()` returns `False` for `UNTIMED_PHASES`. `get_timeout_remaining()` returns `math.inf` for them.
- Remove the `"transferring"` entry from `PHASE_TIMEOUT_MESSAGES` in `prompts/en.py`, `de.py` and `uk.py`. It only existed for the faulty timeout ("Entschuldigung, die Weiterleitung hat nicht geklappt…") and no caller ever heard it, because the socket is closed by then. The prompt-pack key-parity test must stay green.
- Export `UNTIMED_PHASES` in `__all__`.

Why here and not in the watchdog loop: `check_timeout()` is also used by the test harness runner (`tests/voice_harness/runner.py:145`) and `test_voice_shutdown.py`. Putting the rule in the state machine keeps the live loop and the harness in agreement.

### 3.2 A reliable end signal for the bridged call (fixes D2)

Twilio sends us nothing on its own when an inbound call ends unless a status callback is configured in the console. The Twilio SDK's `CallInstance.update(...)` accepts `status_callback` / `status_callback_method`, which by default sends `completed` (verified against the installed `twilio` package). We attach it when we redirect into `<Dial>`.

`src/pincer/voice/engine.py`

1. Move the duplicated body of `transfer_call` (ConversationRelay `:636-666` and Media Streams `:1157-1185`) into one helper on the `VoiceEngine` base class. Both engines then call it, so the fix is made in one place.
2. The helper's call update becomes:

   ```python
   client.calls(call_sid).update(
       twiml=build_dial_twiml(target, timeout_s=..., action_url=..., announce=..., language=...,
                              time_limit_s=self._settings.voice_transfer_max_duration_s),
       status_callback=f"{base}/api/apps/twilio/status",   # only when base is http(s)
       status_callback_method="POST",
   )
   ```

   `base` is `voice_webhook_base_url`, the same value the receptionist already uses to build `action_url`.
3. Record the transfer on the state: `metadata["transferring"] = True` (already set), plus `metadata["transfer"] = {"target": ..., "started_mono": time.monotonic()}`.

`build_dial_twiml` (`engine.py:496`): add `time_limit_s: int | None = None`, rendered as `timeLimit="…"` on `<Dial>`.

New setting in `src/pincer/config/channels.py`:

```python
voice_transfer_max_duration_s: int = Field(
    default=3600, ge=60, le=14400,
    description="Ceiling for a call bridged to a human (<Dial timeLimit>); also the stuck-call allowance for transferred calls",
)
```

Twilio's own default `timeLimit` is 14400 s (4 h). 1 h is a sensible SMB default and stays within the account maximum.

### 3.3 Finalize without hanging up (fixes D2 and D5)

`src/pincer/voice/engine.py`, on the base `VoiceEngine`:

```python
async def finish_remote_ended(self, call_sid: str, reason: str = "") -> bool:
    """Twilio says the call is over: close media, unregister, run the
    end-of-call callback. Never issues a REST hangup. Idempotent — returns
    False if the call was already finalized."""
    state = self._active_calls.get(call_sid)
    if state is None:
        return False
    if reason:
        state.metadata.setdefault("end_reason", reason)
    with contextlib.suppress(Exception):
        await self.close_media_stream(call_sid)
    ended = await self._unregister_call(call_sid)
    if ended and self._on_call_end_callback:
        await self._on_call_end_callback(call_sid, ended)
    return ended is not None
```

`_unregister_call` pops from `_active_calls`, so the second caller always gets `None`. The two webhooks below can therefore arrive in either order, or be retried, and the pipeline still runs exactly once. `on_media_closed` should be refactored to call this helper after its `transferring` guard, so there is one finalization path.

`src/pincer/voice/twiml_server.py`

- **`/status`, `CallStatus=completed`, state exists:** replace `await _engine.end_call(call_sid)` with `await _engine.finish_remote_ended(call_sid, reason="provider_completed")`. This applies to every call, not only transfers: Twilio has already ended the call, and a REST hangup can only fail (D5).
- **`/transfer-result`:**

  | Input | Action |
  |---|---|
  | `DialCallStatus` `completed` or `answered`, or `DialBridged=true` (bridged conversation happened; *`answered`/`DialBridged` added in review*) | Store `DialCallSid`, `DialCallDuration` and `DialBridged` in `state.metadata["transfer"]`. Call `session.on_transfer_completed(duration_s)`. Call `finish_remote_ended(call_sid, "transferred")`. Return `<Hangup/>`. |
  | `busy` / `no-answer` / `failed` / `canceled`, parent still live | `engine.end_transfer()` clears `transferring` right away (*revised in review*: it used to stay set until the relay `setup`, or forever if the reconnect never connected), then `session.on_transfer_failed()` → reconnect TwiML with the apology greeting. The phase becomes `TAKE_MESSAGE`, and the watchdog times it again. |
  | Any other `DialCallStatus` (empty, unknown) and not bridged | Don't guess: no `on_transfer_*` call, `finish_remote_ended(call_sid, "transfer_result_unknown")`, return `<Hangup/>`. The report says the transfer started. (*Added in review.*) |
  | Any status, and the request's `CallStatus` is `completed` (caller hung up during ringing) | Don't build reconnect TwiML. Set `session.transfer_failed = True` with `end_reason="caller_hangup_during_transfer"`, call `finish_remote_ended`, and return `<Hangup/>`. *Open question Q1: confirm on a live call that Twilio requests the action URL and sends `CallStatus` in this case. If it doesn't, the status callback from §3.2 covers the end anyway.* |
  | No state for the CallSid, and the dial bridged or `CallStatus` is `completed` (already finalized) | Return `<Hangup/>`. Log at INFO and do nothing else. |
  | No state for the CallSid, caller still on the line (state lost to a restart while ringing) | Reconnect to the relay with the generic `transfer_failed` line as the greeting (*added in review*; the call was previously hung up). |

`src/pincer/voice/receptionist/session.py`

- New `on_transfer_completed(duration_s: int | None)`: sets `self.transfer_completed = True` and `self.transfer_duration_s`, records the outcome in the `receptionist_transfers` metric via `_transfer_result("completed")` (the separate `transfer_completed` series was removed in review), records `_log_action("transfer_completed", target, duration)`, and calls `_sync()`. Phase stays `TRANSFERRING`. `TRANSFERRING` is in `_BENIGN_HANGUP_PHASES`, so `_handle_call_end` maps it to `COMPLETED`.
- Add `transfer_completed` and `transfer_duration_s` to the snapshot dict next to `transfer_attempted` / `transfer_failed` (`session.py:311-312`), so the receptionist report (`receptionist/report.py`) can print "Weitergeleitet an … (3 Min 12 s)". Add one line per language pack: `RECEPTIONIST_REPORT_TRANSFERRED`.

### 3.4 Reaper and capacity (fixes D3; D4 kept by design)

`src/pincer/observability/golden_signals.py` → `stuck_calls()`:

```python
transfer = metadata.get("transfer") if transferring else None
if transfer has started_mono:
    elapsed = monotonic() - transfer["started_mono"]
    limit = announce_margin (30) + transfer["dial_timeout_s"] + voice_transfer_max_duration_s + grace
else:
    elapsed = duration
    limit = threshold + (voice_transfer_max_duration_s if transferring else 0)
if elapsed > limit: ...
```

A transferred call is only "stuck" if it outlives the announcement, the ringing and the Dial `timeLimit`. At that point Twilio should already have ended it, so the page is real. The transferring part is measured from `transfer["started_mono"]` (set in `transfer_call`), not from call start: `timeLimit` counts from the bridge, and a long AI conversation before the transfer would otherwise eat into the allowance (*revised in review*: ~640 s of AI talk + a full 3600 s bridge would have been reaped).

`src/pincer/voice/twiml_server.py` → `_active_inbound_count()`: unchanged — transferring calls still count. *Decision D-1 (revised in review): a failed transfer (busy / no-answer / failed) reconnects the call to ConversationRelay, where it uses agent and LLM capacity again. Excluding bridged calls would let a new caller take the slot, and the reconnect would then exceed `inbound_max_concurrent`. D4 is accepted: a bridged call is still a live line.*

### 3.5 Observability

- Telemetry: emit `transfer.started` (in the helper from §3.2) and `transfer.result` (in `/transfer-result`, with `dial_status` and `dial_duration_s`) through the existing `voice/telemetry/hooks.py` lifecycle events, so the call detail page shows the bridge on the timeline. If `hooks` has no generic lifecycle event, add one following the `provider_status` pattern.
- Counters: `pincer.voice.receptionist.transfers{result=completed|busy|no_answer|failed|caller_hangup}`.
- `docs/operations/runbook.md`: add a "Transferred call dropped" entry covering what to check (`transfer.result`, the Twilio Dial log, `timeLimit`).

## 4. Task breakdown

| ID | Task | Files | Est. |
|----|------|-------|------|
| T1 | `UNTIMED_PHASES`, remove the `TRANSFERRING` timeout and the three prompt entries | `state_machine.py`, `prompts/{en,de,uk}.py` | 0.25 d |
| T2 | Shared `transfer_call` helper with `status_callback` and `timeLimit`; new setting | `engine.py`, `config/channels.py` | 0.5 d |
| T3 | `finish_remote_ended`, used by `on_media_closed`, `/status` completed and `/transfer-result` | `engine.py`, `twiml_server.py` | 0.5 d |
| T4 | `on_transfer_completed`, snapshot fields, report line ×3 languages | `receptionist/session.py`, `receptionist/report.py`, prompts | 0.5 d |
| T5 | Reaper allowance and capacity exclusion | `golden_signals.py`, `twiml_server.py` | 0.25 d |
| T6 | Telemetry events, counter, runbook entry, config docs (`voice-calling.md` table, `.env.example`, `receptionist-setup.md`) | telemetry hooks, docs | 0.5 d |
| T7 | Tests (§5) | `tests/…` | 0.5 d (alongside T1–T5) |
| T8 | Live verification (§6) | — | 0.5 d |

Suggested PR split: **PR 1** = T1 + its test, a minimal hotfix that can ship on its own and stops the drops immediately. **PR 2** = T2–T7. PR 1 alone leaves D2–D4 open, which are minor until a transferred call runs over 11 minutes, so PR 2 must follow in the same sprint.

## 5. Test plan

All new tests go next to the existing ones. Async tests need no decorator.

| # | Test | File | Asserts |
|---|------|------|---------|
| 1 | `test_transfer_not_cut_by_watchdog` | `tests/voice_harness/test_receptionist.py` | The repro from the issue: after a transfer, set the phase age to 31 s and then 3600 s, run one watchdog pass → `CALL not in engine.ended`, phase still `TRANSFERRING`. |
| 2 | `test_untimed_phase_never_times_out` | `tests/test_voice_state_machine.py` | `check_timeout()` is `False` and `get_timeout_remaining()` is `inf` for `TRANSFERRING`. Every other phase is unchanged (parametrized over `PHASE_TIMEOUTS`). |
| 3 | `test_dial_twiml_time_limit` | `tests/test_voice_engine.py` | `build_dial_twiml(..., time_limit_s=3600)` contains `timeLimit="3600"`, and the attribute is absent when `None`. |
| 4 | `test_transfer_call_sets_status_callback` (both engines) | `tests/test_voice_engine.py` | With a mocked `twilio.rest.Client`, `update()` is called with `twiml`, `status_callback=…/api/apps/twilio/status` and `status_callback_method="POST"`, and `metadata["transferring"]` is true. No `status_callback` when the base URL isn't http(s). |
| 5 | `test_transfer_result_completed_finalizes_once` | `tests/voice_harness/test_receptionist.py` | POST `/transfer-result` completed, then `/status` completed → post-call callback runs once, outcome `completed`, report has `transfer_completed=True` and the duration. Repeat with the order reversed. Retried callbacks are no-ops. |
| 6 | `test_status_completed_does_not_rest_hangup` | `tests/test_voice_webhook_auth.py` or `tests/test_voice_call_end.py` | For a live state, `/status` completed doesn't call `calls(...).update(status="completed")`. |
| 7 | `test_transfer_result_busy_still_reconnects` | existing `test_transfer_result_webhook_reconnects_with_apology` | Stays green. Add `no-answer` and `failed` cases. |
| 8 | `test_transfer_result_parent_gone_no_reconnect` | `tests/voice_harness/test_receptionist.py` | `DialCallStatus=no-answer` with `CallStatus=completed` → `<Hangup/>`, no `<Connect>`, finalized once, `end_reason=caller_hangup_during_transfer`. |
| 9 | `test_back_from_failed_transfer_is_timed_again` | `tests/voice_harness/test_receptionist.py` | Busy → reconnect → `setup` → phase `TAKE_MESSAGE` → the watchdog times it again (so the silence/timeout exits still work). |
| 10 | `test_stuck_calls_allows_transfer` | `tests/test_observability_signals.py` | A transferring state at 700 s isn't stuck. At `600 + 60 + 3600 + 1` s it is. A non-transferring state at 700 s still is. |
| 10b | `test_stuck_calls_measures_transfer_from_its_start` | `tests/test_observability_signals.py` | 640 s of AI talk + announce + ringing + a full 3600 s bridge isn't stuck; past `30 + 30 + 3600 + 60` s since the transfer start it is. |
| 11 | `test_capacity_counts_transferred_calls` | `tests/voice_harness/test_receptionist.py` | With `inbound_max_concurrent=1` and one transferring call, a new inbound call is declined (the bridged call may reconnect to the relay). |
| 12 | `test_media_closed_during_transfer_is_noop` | existing `tests/test_voice_media_closed.py` | Stays green after the refactor onto `finish_remote_ended`. |

The gates from `AGENTS.md` must stay green: `uv run pytest`, `ruff check .`, `ruff format --check .`, `mypy src/` and `bandit -r src/pincer -ll -s B101,B104,B310,B608`.

## 6. Live verification (definition of done)

Use a real Twilio DE number, a receptionist profile with `transfer.enabled: true`, a mobile phone as the target, and `PINCER_VOICE_TRANSFER_MAX_DURATION_S=3600`. Run each scenario on ConversationRelay. Media Streams is a secondary target: run scenarios 1 and 2 if it is deployed anywhere.

| # | Scenario | Pass if |
|---|----------|---------|
| 1 | Ask for a human, the target answers, talk **≥ 3 min**, the caller hangs up | No drop. One post-call report: "transferred", duration ≈ the conversation. `telephony_calls` outcome is `completed`. No "Failed to end call" in the logs. |
| 2 | Same, but the **target** hangs up first | Same as 1. |
| 3 | The target rejects or is busy | Apology + message-taking, as today. The message is delivered to the owner. |
| 4 | The target doesn't answer (20 s) | Same as 3. |
| 5 | The caller hangs up while the target is ringing | Finalized once. Report shows the transfer as not completed. No reconnect attempt. **Answers Q1.** |
| 6 | Set `PINCER_VOICE_TRANSFER_MAX_DURATION_S=60`, talk 90 s | Twilio ends the bridge at ~60 s. Finalized once. No `PAGE` alert. |
| 7 | Set `inbound_max_concurrent=1` and keep one transferred call active, then call again | The second caller gets the receptionist, not the decline. |

Record the CallSids and the outcome of each run in the PR description.

## 7. Risks and open questions

- **Q1. Does Twilio request the `<Dial action>` URL when the parent hangs up during ringing, and does it include `CallStatus`?** The Twilio `<Dial>` docs don't say for this case. The design doesn't depend on the answer: the `status_callback` added in §3.2 finalizes the call either way. Scenario 5 settles it.
- **Q2. Does `status_callback` on `calls.update` replace a callback configured on the number in the Twilio console?** Both point at our `/status`, and `/status` is idempotent through `finish_remote_ended`, so a duplicate does no harm. Check the Twilio call log during scenario 1.
- **D-1 (product): should bridged calls count toward `inbound_max_concurrent`?** The spec assumes no. Confirm with whoever owns the receptionist offering.
- **Risk: changing `/status` completed for every call** (not only transfers) touches outbound end handling. The safeguard is test 6 plus the existing `test_voice_call_end.py`, `test_voice_outbound_errors.py` and `test_postcall_e2e.py` suites. Outcome classification for unanswered outbound calls is a separate P0 issue. This change must not alter it, only remove the REST hangup.
- **Risk: Media Streams `transfer_call`** has the same body today but has had less pilot exposure. The shared helper means it gets the same fix. Scenario 1 on Media Streams is recommended.

## 8. Rollout

1. Merge PR 1 (watchdog exemption) as a hotfix as soon as it is green. It removes the customer-visible drop.
2. Merge PR 2. Deploy to the pilot instance and run §6.
3. Add a CHANGELOG `### Fixed` entry: "Receptionist transfers to a human were hung up after ~30 s by the phase watchdog; transferred calls are now finalized by Twilio's end signal and reported as transferred." Under `### Added`: `PINCER_VOICE_TRANSFER_MAX_DURATION_S`.
4. Update `docs/reference/voice-supported-features.md`: transfer moves from "⚠️ not exercised by a pilot" to ✅ once §6 passes.

## 9. Implementation notes

Where the implementation differs from the design above, and why:

- **`finish_remote_ended` writes `metadata["remote_end_reason"]`, not `end_reason`.** `end_reason` is localized, user-facing text: the post-call report appends it as `ℹ️ <reason>` (voicemail / not-connected endings). Writing `provider_completed` there would have added that code to every normal outbound report. The technical reason is used as the telemetry `termination_reason` when no `end_reason` is set.
- **`UNTIMED_PHASES` is not added to an `__all__`.** `state_machine.py` has none, and adding one would have to list the whole module.
- **The report line is rendered in `receptionist/report.py`**, like every other line of the owner report, not as a `RECEPTIONIST_REPORT_TRANSFERRED` prompt key. It has three states: transferred (with duration), did not connect (`transfer_failed`), and started (the call ended before Twilio reported the dial result, e.g. `/status` arrived first; the report does not guess).
- **`should_report` is true for any attempted transfer.** A transfer-only call has intent `human`, which never produced an owner report, so "the report says the call was transferred" needed it.
- **Caller hang-up during ringing** is `session.on_transfer_abandoned()`: `transfer_failed`, `end_reason="caller_hangup_during_transfer"` on the reception snapshot, metric `result=caller_hangup`.
- **`transfer_call` clears `transferring` when the REST redirect raises.** The call never left the relay; leaving the flag set would give it the reaper allowance and make `on_media_closed` a no-op.
- **Media Streams `start` clears `transferring`**, as the ConversationRelay `setup` does, so a Media Streams call back from a failed transfer is ours again. Both go through `VoiceEngine.end_transfer()`, the only place the flag is cleared after a successful `transfer_call` (*revised in review*).
- **Test 5, reversed order:** when `/status completed` arrives before `/transfer-result`, the call is finalized as `COMPLETED` once and the late dial result is a no-op, but `transfer_completed` cannot be known, so the report says the transfer started.
