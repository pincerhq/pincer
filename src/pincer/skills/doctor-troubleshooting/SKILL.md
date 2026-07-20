---
name: Doctor troubleshooting
description: How to run and interpret Pincer's security doctor checks (pincer doctor) to diagnose configuration, permission, and security issues. Load this when the user reports something misconfigured, insecure, or asks you to check their setup's health.
---

# Doctor troubleshooting

`pincer doctor` runs 25+ security and configuration checks and prints a
traffic-light report with an overall score out of 100.

```bash
pincer doctor          # human-readable table
pincer doctor --json    # machine-readable, for scripting
```

## Reading the report

Each check has:

- A status icon: ✅ pass, ⚠️ warning, ❌ critical, ➖ skipped (not applicable
  to this deployment).
- A category (grouped in the table, e.g. filesystem permissions, secrets,
  network exposure).
- A message describing what was found.
- A fix hint — a concrete, actionable next step, not just a description of
  the problem.

When helping a user interpret a report, prioritize **critical** findings
first, then **warnings**. Skipped checks are not failures — they mean the
check doesn't apply (e.g. a check for a feature the user has disabled).

## Common categories

- **Filesystem permissions**: directories like the skills root or data dir
  having overly permissive bits (e.g. not `755`).
- **Secrets**: API keys or tokens present in config that should be in
  environment variables, or vice versa.
- **Network exposure**: services bound to `0.0.0.0` when they should be
  local-only, missing auth on exposed endpoints.

## Workflow

1. Run `pincer doctor` (or `--json` if you need to parse it programmatically).
2. Walk through critical findings first — each has a fix hint; apply it or
   walk the user through applying it.
3. Re-run `pincer doctor` after changes to confirm the score improved and
   the specific check now passes.
4. Don't just report the score — explain what a low score on a *specific*
   check actually means for the user's setup, since a 100/100 score isn't
   itself the goal, an appropriately secured deployment is.
