# GA Gate

The formal promotion of voice from 🧪 peripheral to 🟡 stable. Every criterion is
evaluated against real pilot data by `pincer voice ops ga-gate`, so the sign-off
meeting argues about evidence rather than assembling it.

```bash
pincer voice ops ga-gate --days 14                       # table
pincer voice ops ga-gate --days 14 --out ga-review.md    # sign-off document
```

Exit code is 0 only when **every** criterion passes, so this is safe to wire into
a release gate.

---

## The four verdicts

| | Meaning |
|---|---|
| ✅ `pass` | Met, with the numbers attached |
| ❌ `fail` | Measured and missed |
| ⏳ `insufficient_data` | **Not a pass.** The criterion says how much more it needs |
| 🧑 `manual` | Genuinely needs a human. The tool says what to bring |

The third one is the whole design. A gate that reads "100% success" off three
calls is how a product ships on a lie, so `ready` requires every criterion to be
`pass` — never merely "nothing failed".

## What is measured, and from where

| Criterion | Threshold | Source |
|---|---|---|
| Call volume | ≥ 200 | `voice_calls` |
| Call success | ≥ 95% excl. callee-unreachable | `voice_calls.failure_code` |
| Stuck calls | 0 | `failure_code = 'stuck'` |
| Booking success | ≥ 80% cooperative / ≥ 70% overall | `appointment_outcomes` |
| Latency | p50 ≤ 1.2s, p95 ≤ 2.0s, **per language** | turn records |
| Security | zero CRITICAL | `doctor --production` |
| Compliance | consent + quiet hours active, zero opt-out violations | settings + `outbound_call_log` ⋈ `do_not_call` |
| Cost per call | within $0.12–$0.25 | `call_costs` |
| Alert quality | 🧑 | canary + stuck history, judged by on-call |
| Pilot feedback | 🧑 | [pilot-feedback.md](pilot-feedback.md) per customer |

### Two details worth knowing before the meeting

**Latency is checked per language, not in aggregate.** A DACH product whose
German latency is twice its English latency has not met the bar, and an
aggregate would hide exactly that.

**Cost only fails on overshoot.** Cheaper than modelled is reported, not failed —
it is good news that still needs to reach whoever sets the price. And the figure
is only as accurate as `PINCER_PRICE_*`: check them against a real Twilio invoice
before the review, or the number is precise and wrong.

## Running the review

1. **A week before:** run the gate. Whatever is `insufficient_data` needs pilot
   volume — that is a scheduling problem, and a week is the last moment to fix it.
2. **Two days before:** collect the two manual criteria. The feedback template
   takes a conversation per customer, not an afternoon.
3. **At the meeting:** generate the document fresh, walk it top to bottom.
4. **For each fail/undecided:** fix before GA, or grant a **dated exception with
   a named owner**. Both are legitimate. "We'll look at it" is not — an undecided
   criterion is a measurement that was not taken, not an exception.

The generated document ends with a decision table for exactly this.

## After a pass

```bash
pincer voice ops ga-gate --days 14 --out docs/operations/ga-review-<date>.md
```

Commit the report — it is the evidence that the promotion was earned — then:

1. Flip the voice tier in `README.md` from 🧪 to 🟡.
2. Version bump + CHANGELOG entry referencing the committed report.
3. Confirm the voice harness gate is green in CI (it is a release blocker at
   stable tier).
4. Re-rank [post-ga-backlog.md](post-ga-backlog.md) using pilot evidence.

**The tier badge stays 🧪 until this report says `ready`.** Flipping it early is
the one thing that makes the whole gate decorative.
