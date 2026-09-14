# Pilot Feedback Template

One per customer, completed at the end of the pilot. Feeds the GA gate's
`pilot_feedback` criterion — which stays `manual` until this is filled in,
because no tool can invent it.

**Pilot:** `<customer>`  **Segment:** medical / trades / office / other
**Period:** `<start>` – `<end>`  **Interviewer:** `<name>`

---

## 1. Usage (from the dashboard, not from memory)

```bash
pincer voice ops ga-gate --days 14 --markdown
pincer voice ops failures --hours 336
```

| | |
|---|---|
| Calls placed | |
| Call success (excl. unreachable) | |
| Appointments booked / attempted | |
| Cost per call (mean) | |
| Top failure code | |

## 2. NPS

> On a scale of 0–10, how likely are you to recommend this to another business
> like yours?

**Score:** ___ /10

**Why that number?** (verbatim — the number alone is nearly useless)

## 3. Testimonial

Ask directly. A clear "no" is a valid, recordable answer — record it rather than
leaving the field blank.

- [ ] Given (quote below, with permission to use it)
- [ ] Declined — reason:

> _quote_

## 4. Churn risk

| | |
|---|---|
| Rating | low / medium / high |
| Reason | |
| What would change it | |

Rate on behaviour, not warmth. A customer who is delighted but stopped using it
in week two is high risk.

## 5. Usage honesty check

The most valuable question in this document:

> **What did you stop using, or avoid using, and why?**

Silent non-use is the strongest churn signal there is, and it never shows up in a
satisfaction score.

## 6. Callee-side quality audit

Sample of people the agent called, contacted with the customer's introduction.
**Target: ≥ 80% "pleasant".**

| # | Language | "Did you realise it was an AI?" | "Was the call pleasant?" | Comment |
|---|---|---|---|---|
| 1 | | yes / no | yes / no | |
| 2 | | | | |
| 3 | | | | |
| 4 | | | | |
| 5 | | | | |

**Pleasant:** ___ / ___ = ___%
**Realised it was an AI:** ___ / ___

Both numbers matter, in opposite directions. Low "pleasant" is a product problem.
Low "realised it was an AI" is a **compliance** problem — the disclosure is
mandatory, so if people did not notice it, it is not doing its job regardless of
whether it played.

## 7. Feature requests, ranked by this customer

| Rank | Request | Their words |
|---|---|---|
| 1 | | |
| 2 | | |
| 3 | | |

Feeds [post-ga-backlog.md](post-ga-backlog.md). A request from three of five
pilots outranks one made loudly by one.

## 8. Interviewer's own read

What did you notice that they did not say?
