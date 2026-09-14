# Harness fixtures from real calls

Each `*.json` here is one real pilot call, PII-masked, replayable as a persona
by the reliability suite (`tests/voice_harness/test_real_fixtures.py`).

Export one:

```bash
pincer pilot export-fixture CAxxxxxxxx --name wrong_number_dialect --out tests/voice_harness/fixtures/
```

**Before committing**, clear `review_required.possible_names`: `mask_pii` removes
numbers, cards, and emails, but a callee saying "hier ist Frau Schneider" is not
a pattern. Replace names with placeholders and empty the list — `load_fixture`
refuses a fixture that still carries flagged names, so an unreviewed one fails
CI rather than shipping quietly.

Add a fixture whenever a pilot call surfaces behaviour the imagination-written
Sprint 1 personas do not cover. A prompt fix without a fixture regresses
silently.
