# Post-GA Backlog

> **Ranking status: provisional.** The acceptance criterion for this document is
> "ranked by pilot evidence, not opinion" — and the pilot has not run yet. What
> follows is ordered by *reasoning*, with the evidence that would confirm or
> overturn each position written next to it. Re-rank this document at the GA
> gate review, using the spot-check sheets and the failure-code digest.
>
> Any item still ordered by reasoning after the pilot is a gap in the review, not
> a finished ranking.

---

## How to re-rank this with evidence

At the GA gate, three sources decide the order:

```bash
pincer voice ops failures --hours 336     # what actually broke, ranked
pincer pilot spot-check --days 14 -n 30   # what reviewers flagged, verbatim
pincer voice ops ga-gate --markdown       # which criteria were missed, and why
```

Plus the pilot feedback template: a feature three of five customers asked for
outranks a feature one asked for loudly.

---

## 1. Rescheduling and cancellation calls

**Why first:** the agent can create an appointment but cannot move one. Every
booked appointment is a future rescheduling request, so this gap grows with
usage — it is the only backlog item whose cost increases the longer it is left.
The negotiation machinery, slot validation, and calendar write already exist;
this is largely a new task type plus prompt work.

**Evidence that would confirm:** pilot customers asking "can it call back to
move the Tuesday appointment?" in feedback calls; `declined` outcomes whose
transcripts are actually reschedule requests.

**Evidence that would demote it:** pilots handling reschedules by text without
friction.

## 2. Inbound self-booking

**Why second:** inbound calls currently converse but do not run the scheduling
workflow, so a customer's own client calling in gets a chat, not a booking. It
doubles the product's surface for one workflow's worth of work — the free/busy
and negotiation paths are shared.

**Evidence that would confirm:** inbound call volume in the pilot; transcripts
where the caller clearly wanted to book.

**Risk to weigh:** inbound means an unknown caller reaching the agent. The
Sprint 8 tool denylist covers it, but the red-team persona set should grow
before this ships.

## 3. Automatic language detection at call start

**Why third:** language is currently a parameter chosen when the call is placed.
For outbound that is correct — we know who we are calling. For inbound it is a
real gap, which is why it ranks just behind inbound booking rather than ahead of
it.

**Evidence that would confirm:** German-first pilots receiving calls in another
language; `language_drift` codes on inbound calls.

## 4. Live listen-in

**Why fourth:** repeatedly requested in the abstract ("can I hear it working?"),
but it is a trust feature, not a capability one — and the same trust need may be
served far more cheaply by the existing live transcript view plus the post-call
report. Audio streaming to a browser is a meaningful amount of work.

**Evidence that would confirm:** pilots saying the post-call report is not
enough. **Evidence that would demote it:** pilots stopping asking after week one,
which is the common pattern for reassurance features.

## 5. STT provider A/B for German

**Why fifth, conditionally — this may jump to #1.** If the spot-checks show a
German ASR problem on dates and names, this becomes the highest-value item in
the list, because it degrades every call rather than blocking one workflow. The
`STTProvider` abstraction already exists, so an A/B is cheap to run.

**Evidence that would confirm:** `asr_accuracy` misses in the spot-check sheets,
with the misheard strings recorded verbatim.

## 6. Onboarding automation

**Why sixth:** onboarding is currently ~170 minutes against a 120-minute target
(`pincer pilot automation-candidates`). It does not affect a live customer, so it
ranks below product gaps — but it is the item that limits how many pilots can
become customers per week, so it rises as the sales pipeline does.

Top three candidates by minutes saved, from the step data:

1. Twilio number purchase + webhook wiring (25 min) — pure API work
2. ElevenLabs voice selection (20 min) — removable entirely by offering curated stock German voices
3. Webhook URL configuration (10 min) — two API calls, identical every time

## 7. Multi-tenant architecture

**Why last, and why it is here at all:** the current one-instance-per-customer
model is operable to roughly 50 customers ([capacity](capacity.md)). Below that,
fleet automation is strictly cheaper than a data-model change. Above it, this is
unavoidable — and it is a large change: identity, memory, the do-not-call list,
and every observability query assume one customer per database.

**Evidence that would promote it:** customer count trajectory, not customer
requests. No customer will ever ask for multi-tenancy.

---

## Deliberately not on this list

| Item | Why not |
|---|---|
| More languages | The mechanism exists (a prompt pack + a voice). Add one when a customer needs it, not speculatively |
| Encrypted SQLite at rest | Real, but a compliance-driven decision rather than a product one. Tracked in the security checklist as F-7 |
| Public status page | Sprint 9 out-of-scope, and internal SLOs serve the pilot |
| Call recording playback | Recording is off by default for good DACH reasons; enabling it is a customer decision with compliance weight, not a backlog item |

---

## Also feeding this backlog

Anything the pilot files as **won't-fix-for-GA** lands here with the issue link
and the reason it was deferred. A deferral without a backlog entry is how a known
gap becomes a surprise six months later.
