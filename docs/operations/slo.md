# Pilot SLOs & Error Budget

Written down so they can be argued with. Every target below is **measured** by
code in this repository — none is aspirational, and each names the SLI that
produces it. Where a number cannot honestly be measured yet, it says so.

Status any time: `pincer voice ops slo` or `GET /api/ops/slo`.

---

## The four SLOs

| # | SLO | Target | Window | SLI source | Confidence |
|---|---|---|---|---|---|
| 1 | Call attempt success | **99%** | calendar month | `voice_calls.failure_code` | measured |
| 2 | Voice-to-voice latency p95 | **≤ 2.0 s** | calendar month | `voice_latency.jsonl` turn records | measured |
| 3 | Report delivery | **≤ 30 s** p95 after hangup | calendar month | `voice_calls.ended_at` → `report_delivered_at` | measured |
| 4 | Voice availability | **99.5%** | calendar month | canary run history | **inferred** |

### 1. Call attempt success — 99%

**Definition.** Of the call attempts whose outcome we can influence, 99% end
`COMPLETED`.

**What is excluded from the denominator, and why:**

* *Callee unreachable* — `no_answer`, `busy`, `voicemail`, `wrong_number`. No
  deploy makes someone answer their phone. Including these would mean a week of
  holidays reads as an outage and burns budget nobody can defend.
* *Policy blocks* — `do_not_call`, `quiet_hours`, `abuse_gate`,
  `injection_blocked`, `budget`. The system worked exactly as designed; counting
  a correctly-refused robocall as a failure would create pressure to weaken the
  Sprint 8 guardrails.

Everything else is ours: transport, media, model, tools, our own bugs.

**Error budget.** 1% of eligible attempts. Over a month with 1,000 eligible
attempts, 10 may fail before the SLO is missed.

### 2. Latency p95 — ≤ 2.0 s

**Definition.** 95% of conversational turns complete within 2.0 s from the
caller finishing their utterance to our audio being dispatched.

Note the two thresholds: **2.0 s is the SLO**, **2.5 s is the alert**. The alert
fires before the promise is broken, which is the point of having both.

**Error budget.** 5% of turns may exceed 2.0 s — that is what a p95 target
means. `budget_spent` is the count of turns over target.

### 3. Report delivery — ≤ 30 s p95

**Definition.** After a call ends, the initiating user receives their post-call
report within 30 s at p95. Measured from `ended_at` to the moment delivery was
confirmed; **only successful deliveries are stamped**, so an undelivered report
is never recorded as a fast one.

This SLO exists because the post-call report is the product for outbound calls.
A call that succeeded but whose result reached nobody has not delivered value.

### 4. Availability — 99.5%, and honestly labelled

**Definition.** The fraction of canary runs that succeeded.

**This is `inferred`, not `measured`, and the API says so in a `confidence`
field.** We only know the service was up at the moments the canary ran (every
6 h). A 30-minute outage between canary runs is invisible to this number. It is
reported anyway because a rough availability figure that admits its own
uncertainty beats none — but do not quote it to a customer as a measured SLA.

**To make it measurable** (out of scope for the pilot): an external prober
hitting `/api/health` on a short interval from outside the host.

---

## Error budget and the freeze rule

**The rule (agreed, T9.5):** burning more than **50%** of any SLO's monthly error
budget stops feature work on the voice surface until the budget recovers.
Reliability work continues; new features wait.

**Guard rails on the rule itself:**

* A freeze needs at least **100 observations** for that SLO
  (`slo.MIN_BUDGET_SAMPLE`). Without it, three slow turns on a quiet Tuesday
  declare a company-wide freeze, and the rule gets ignored the first time it
  fires — which is worse than not having it.
* Burn is computed **month-to-date**, not over a rolling window: the budget
  resets on the 1st, deliberately, so the rule has a natural end.
* `burn_pct > 100%` means the SLO is already missed for the month. The freeze
  stays until the calendar month turns; the point of the remaining time is
  fixing, not shipping.

**Where it shows up:** `pincer voice ops slo`, `GET /api/ops/slo`
(`feature_freeze`, `freeze_reason`), and the weekly digest, which calls out a
freeze in its own line so it cannot be missed.

Configure with `PINCER_SLO_ERROR_BUDGET_FREEZE_PCT`. Changing it during a freeze
to escape the freeze is exactly the failure mode the rule exists to prevent —
if the target is wrong, change it at the start of a month, in writing, with a
reason.

---

## Reading the numbers without fooling yourself

**A green SLO with `sample_size: 0` means nothing.** The API and CLI both render
"no data" rather than 100%, because a system with no traffic is not a reliable
system — it is an unobserved one.

**The SLO windows and the alert windows are different on purpose.** Alerts use
short windows (1–24 h) to catch a problem while it is happening. SLOs use the
month, because a promise measured over an hour is not a promise. Do not "fix" the
discrepancy by aligning them.

**Latency is measured from our side of the call.** It does not include PSTN
audio propagation or the callee's own handset delay. The number a caller
*experiences* is somewhat worse than the number we report; the SLO is an internal
target, not a customer-facing one.

---

## Changing these targets

The targets are configuration (`PINCER_SLO_*`), but they are not knobs to be
tuned when they are inconvenient. Change one when:

* it turns out to be measuring the wrong thing, or
* the product's requirements genuinely changed.

Never because it is currently being missed. Record the reason in the changelog
alongside the change, and make it effective from the start of the next month so
the error budget arithmetic stays coherent.

---

## Out of scope for the pilot

Recorded so absence does not read as oversight:

* A public status page.
* Customer-facing SLAs with financial remedies. Pilots use these internal SLOs.
* Multi-window, multi-burn-rate alerting (the Google SRE approach). The pilot's
  call volume is too low for burn-rate alerting to be stable — single-threshold
  alerts plus the monthly budget are the right complexity for this scale.
