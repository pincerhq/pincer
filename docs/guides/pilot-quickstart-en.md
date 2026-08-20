# Pincer Voice — Quickstart

One page. Keep it next to you for the first week.

---

## What it does

Pincer makes phone calls for you. You ask in a chat message; it dials, has the
conversation, and reports back what happened.

**Its best-tested job:** booking an appointment. It checks your calendar, offers
only times you are actually free, negotiates with the other party, and writes the
event — with invitations — once they agree.

## How to ask

Just write it, in your own words:

> Call Dr. Weber at +49 30 1234567 and book a check-up next week, 30 minutes.

> Call the supplier on +49 89 7654321 and ask whether the delivery arrives Friday.

You get three messages back per call, at most:

| | |
|---|---|
| 📞 **Dialing…** | the call is going out |
| 📞 **Connected** | someone picked up |
| 📋 **Report** | what was agreed, what was said, what (if anything) failed |

## Reading the report

The report is deliberately blunt about failure. If the calendar write failed
after a verbal agreement, it says so and does **not** claim the appointment is
booked. If it could not verify something it said on the call, it flags that too.

**Trust the report over your memory of what you asked for.** It only claims what
a tool result confirmed.

## What it will not do

- **Move or cancel** an appointment it booked — it books; you move.
- **Answer your customers' incoming calls and book them in** — it converses, but
  does not run the booking workflow inbound.
- **Guess the language.** You pick when the call goes out.
- **Call outside quiet hours** (20:00–08:00 by default), or call anyone who has
  asked not to be called again. Both are enforced, not advisory.

## What the other party hears

Every call opens with the assistant identifying itself as an AI, in the call's
language. That is a legal requirement (EU AI Act), and it is not optional.

If they say "don't call me again", the number is blocked permanently and
automatically — across your whole account, not just that task.

## When something goes wrong

1. Read the report. It usually names the reason.
2. Tell us on your feedback channel — with the call ID from the report.
3. Anything urgent: use the kill-switch contact in your expectations document.

## Your limits

| | Default |
|---|---|
| Calls per day | 20 |
| Same number | at most 3 calls per hour |
| Quiet hours | 20:00–08:00 |

Say the word if these do not fit your business — they are configuration, not
architecture.
