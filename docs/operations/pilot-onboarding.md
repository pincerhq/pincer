# Pilot Onboarding Runbook

**Target: a customer live in ≤ 2 hours.** The current step list totals ~170
minutes, so the target is not met yet — `pincer pilot automation-candidates`
names the gap and what closes it. That honesty is the point: a target everyone
knows is missed drives work; a target quietly declared met does not.

---

## Before the session (do NOT do these live)

Two things have lead times measured in days, and discovering that during the
onboarding call is how a 2-hour session becomes a 2-week one:

1. **Twilio regulatory bundle** for the +49 number. Approval can take days.
   Start it as soon as the customer signs.
2. **AVV/DPA** — send [the annex](../guides/avv-annex.md) for signature ahead of
   time. Counsel should have pre-approved the template once, so each pilot is a
   countersign rather than a review.

Then, the morning of:

```bash
pincer pilot preflight        # every automatable prerequisite, checked
```

Exit code 1 means blocking items. Fix them **before** the customer joins.

## The session

Generate the customer's checklist and track actual times against it:

```bash
pincer pilot checklist "Zahnarztpraxis Weber" --out pilots/weber-onboarding.md
```

| Step | Est. | Automated | Notes |
|---|---|---|---|
| Host provisioned + deployed | 20 | ✅ | `scripts/deploy.sh`; the gate blocks a bad config |
| Twilio number + webhooks | 35 | ❌ | Bundle done beforehand; wiring is manual today |
| API tokens | 2 | ✅ | Two distinct 32+ char tokens |
| Google Calendar OAuth | 10 | ❌ | Customer signs in with their business account |
| ElevenLabs voice | 20 | ❌ | Stock voice, or clone per [custom-voices](../guides/custom-voices.md) — cloning needs the speaker's consent |
| DACH compliance settings | 3 | ✅ | two-party consent, retention, Europe/Berlin |
| Abuse limits reviewed | 5 | ✅ | Confirm the defaults suit their business hours |
| Ops alerting + canary | 5 | ✅ | Alerts must have a recipient — doctor is CRITICAL otherwise |
| Customer channel access | 10 | ❌ | Telegram allowlist or web chat handover |
| Verification | 15 | ✅ | Below |
| Quickstart + expectations walkthrough | 15 | ❌ | The conversation where they learn the limits |

### Verification, in order

```bash
pincer doctor --production      # must be zero CRITICAL
pincer voice ops canary         # a real call goes out and comes back healthy
pincer pilot preflight          # everything green or explicitly manual
```

Then **one supervised live call** with the customer watching — to their own
mobile, not a customer of theirs. Nothing replaces hearing it work.

### The handover conversation

Walk through [pilot-expectations.md](pilot-expectations.md) out loud. Do not
email it. The out-of-scope list is the part that prevents a bad week three:

- it books but does not reschedule
- inbound converses but does not book
- live listen-in only if enabled for them (`PINCER_LISTEN_IN_ENABLED`), and then the call says so
- language is chosen when the call is placed

Hand over the quickstart in their language
([DE](../guides/pilot-quickstart-de.md) / [EN](../guides/pilot-quickstart-en.md)).

## After the session

Fill in the **actual** times in the checklist. Anything materially over its
estimate is either a wrong estimate or a new automation candidate — update
`src/pincer/onboarding.py` rather than remembering it. That file is where the
2-hour target is actually tracked.

## Weekly cadence during the pilot

| Cadence | Action |
|---|---|
| Daily | `pincer voice ops status` — the five signals, per customer instance |
| Weekly | `pincer pilot spot-check --days 7 -n 10 --week "week 2" --out reviews/w2.md` |
| Weekly | 30-minute feedback call |
| On any bug | Triage within 24h: hotfix / sprint-backlog / won't-fix-for-GA |

### Bug triage discipline

From the Hotfix 1–3 pattern that already exists in this project — every hotfix
gets all three, or it is not done:

1. A short write-up of what broke and why.
2. **A regression test** — a harness fixture if it was a call-behaviour bug
   (`pincer pilot export-fixture <call_sid>`).
3. A [runbook](runbook.md) entry if an operator could hit it again.

A fix without a regression test is a fix that comes back.

## Decommissioning

See [runbook §decommissioning](runbook.md#decommissioning). Release the Twilio
number properly — a number you merely stop paying for gets reassigned to someone
else, who then receives your customer's callbacks.
