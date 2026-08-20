# Voice Security Checklist (Sprint 8)

The signed-off record of the Sprint 8 hardening review. **Required artifact for
the Sprint 10 GA gate** — GA is not granted while any control below is
unverified or any finding is untriaged.

**Scope.** Everything that accepts traffic from the public internet or can spend
money and speak on a customer's behalf: the Twilio webhooks, the WSS relay, the
dashboard/REST API, the outbound dialer, and the in-call tool surface.

**Threat model in one line.** Two untrusted inputs: *the internet* (anyone can
POST to a webhook URL or open a socket) and *the callee* (whoever picks up the
phone is not the operator and their words are not instructions).

| | |
|---|---|
| Review date | 2026-08-20 |
| Reviewed by | `<name>` — engineering |
| Counter-signed by | `<name>` — `<role>` |
| Next review | Sprint 10 GA gate, or on any change to an item below |

---

## How to verify

Every automated control names the test that proves it. Run them together:

```bash
uv run pytest tests/test_voice_webhook_auth.py tests/test_voice_relay_ws.py \
              tests/test_voice_outbound_gate.py tests/test_api_auth_guard.py \
              tests/test_voice_pii_egress.py tests/voice_harness/test_red_team.py
uv run pincer doctor --production
scripts/audit-deps.sh
```

Manual items are marked **[manual]** and must be re-verified on the actual
production host, not on a laptop.

---

## T8.1 — Webhook & WebSocket authentication

| # | Control | Status | Evidence |
|---|---|---|---|
| 1.1 | `X-Twilio-Signature` validated on **every** `/api/apps/twilio/*` route | ✅ | `test_voice_webhook_auth.py::test_unsigned_request_is_rejected` |
| 1.2 | …and on the deprecated `/voice/*` aliases, which are equally public | ✅ | same test, parametrized over both path sets |
| 1.3 | Forged signatures rejected | ✅ | `test_forged_signature_is_rejected` |
| 1.4 | Body tampering invalidates a captured signature | ✅ | `test_tampered_body_invalidates_the_signature` |
| 1.5 | Digest matches Twilio's own `RequestValidator` byte-for-byte | ✅ | `test_compute_signature_matches_the_twilio_sdk` |
| 1.6 | Works behind a TLS-terminating proxy (http→https, internal→public host) | ✅ | `test_candidate_urls_include_the_proxy_corrected_form` |
| 1.7 | JSON webhooks: `bodySHA256` verified | ✅ | `test_body_sha256_guard` |
| 1.8 | Unsigned WSS `/relay` connect refused **before** `accept()` | ✅ | `test_voice_relay_ws.py::TestRelayWebSocketAuth::test_unsigned_connect_is_refused` |
| 1.9 | Unsigned `/stream/{sid}` connect refused | ✅ | `test_unsigned_media_stream_is_refused` |
| 1.10 | A token signed for one surface cannot open the other | ✅ | `test_token_signed_for_another_path_is_refused` |
| 1.11 | Replay guard: requests older than 5 min rejected | ✅ | `test_replay_guard_rejects_stale_request`, `test_stale_token_is_refused` |
| 1.12 | Validation cannot be silently disabled | ✅ | `pincer doctor --production` → `prod_voice_signatures` CRITICAL when `PINCER_VOICE_WEBHOOK_VALIDATE=false`, `PINCER_VOICE_WS_AUTH_REQUIRED=false`, or the Twilio auth token is empty |
| 1.13 | Deploy verification asserts the *refusal*, not a successful connect | ✅ | `scripts/deploy.sh` → `verify_relay_upgrade` dies on HTTP 101/200 |

**Note on 1.13.** The Hotfix-2 debug step was `wscat -c wss://…/relay` and a
successful handshake was the pass condition. That is now the failure condition:
a 403 proves both that the proxy forwards the upgrade and that the app refuses
an unsigned socket.

## T8.2 — Dashboard / API auth

| # | Control | Status | Evidence |
|---|---|---|---|
| 2.1 | `dashboard_token` and `web_chat_token` both required in production | ✅ | `doctor` → `prod_auth_tokens` CRITICAL if either is empty or <16 chars |
| 2.2 | The two tokens must differ | ✅ | `test_prod_auth_tokens_critical_when_tokens_identical` |
| 2.3 | Empty token = allow-all is dev-only and cannot ship | ✅ | 2.1 blocks the deploy; behaviour documented in `api/server.py` |
| 2.4 | Token comparison is constant-time | ✅ | `secrets.compare_digest` in `auth_middleware` |
| 2.5 | Per-IP exponential lockout after repeated failures | ✅ | `test_api_auth_guard.py::test_repeated_failures_lock_the_ip_out` |
| 2.6 | A locked-out IP is refused even with the correct token | ✅ | same test — otherwise the lockout is a slow oracle |
| 2.7 | Lockout is capped so a shared proxy IP cannot DoS the dashboard | ✅ | `test_lockout_is_capped` (1 h ceiling) |
| 2.8 | Every 401 audit-logged with its IP | ✅ | `test_failed_auth_is_audit_logged` |
| 2.9 | `/api/health` never rate-limited (uptime probes) | ✅ | `test_health_endpoint_is_never_rate_limited` |
| 2.10 | Production CORS carries no localhost origin | ✅ | `test_production_drops_localhost`; `doctor` → `prod_cors_origins` |
| 2.11 | `POST /api/voice/calls` passes the same budget/abuse gate as chat | ✅ | `test_voice_outbound_gate.py::test_dashboard_api_hits_the_same_gate` |
| 2.12 | `POST /api/voice/schedule` likewise | ✅ | routes through `make_phone_call`; `doctor` → `voice_do_not_call` asserts the path |
| 2.13 | **[manual]** Tokens rotated at each pilot handover | ⬜ | Procedure: `docs/guides/production-deployment.md` §Secrets rotation |

## T8.3 — Outbound abuse prevention

Single enforcement point: `voice/safety_gates.check_outbound_allowed`, called by
`voice/outbound.make_phone_call`, which is the only path to Twilio.

| # | Control | Status | Evidence |
|---|---|---|---|
| 3.1 | Global daily cap across all users and channels | ✅ | `test_daily_cap_counts_every_user_and_channel` |
| 3.2 | Per-target cooldown (default 60 min) | ✅ | `test_target_cooldown_bounds_calls_to_one_number` |
| 3.3 | Quiet hours (default 20:00–08:00 local, §7 UWG) | ✅ | `test_quiet_hours_block_the_dial`, window wraps midnight |
| 3.4 | Quiet-hours override is an explicit per-user allowlist | ✅ | `test_quiet_hours_override_user_may_still_call` |
| 3.5 | `do_not_call` table consulted on every dial | ✅ | `test_do_not_call_blocks_the_dial`; `doctor` → `voice_do_not_call` reads the source of `make_phone_call` so a refactor around the gate fails the check |
| 3.6 | Opt-out auto-detected from the callee's own words, EN + DE + UK | ✅ | `test_opt_out_intent_detected_in_both_languages` |
| 3.7 | Ordinary speech does not trigger a false opt-out | ✅ | `test_opt_out_not_triggered_by_ordinary_speech` — a false positive silently blocks a number the user needs |
| 3.8 | The list is honoured across **all** initiating users | ✅ | `test_do_not_call_is_honored_across_all_initiating_users` |
| 3.9 | Retries share the daily cap and the cooldown | ✅ | `test_retries_share_the_daily_cap` drives the real scheduler retry path |
| 3.10 | Blocked dials are audit-logged (`voice_call_blocked`) with a masked number | ✅ | `outbound._audit_block` |
| 3.11 | Limits present in production config | ✅ | `doctor` → `voice_abuse_limits` |
| 3.12 | Removal from the list is a deliberate, logged human action | ✅ | `pincer voice dnc remove` (confirmation prompt) / `DELETE /api/voice/do-not-call/{n}` |

## T8.4 — Prompt injection & call-content hardening

| # | Control | Status | Evidence |
|---|---|---|---|
| 4.1 | In-call tool set is a minimal allowlist | ✅ | `voice_tools.filter_voice_tools` |
| 4.2 | Hard denylist beats every allowlist | ✅ | `test_red_team.py::test_forbidden_tools_denied_in_voice` |
| 4.3 | MCP tools are covered by the denylist | ✅ | `test_dangerous_mcp_tools_denied_in_voice` — **this was a real hole**: `is_voice_compatible` previously admitted any name containing `__`, so an MCP `memory__export` or `filesystem__read_file` was reachable from a live call |
| 4.4 | No memory, file, config, credential, or database tool reachable in-call | ✅ | `VOICE_DENIED_KEYWORDS` + `test_filter_voice_tools_drops_dangerous_schemas` |
| 4.5 | The denylist does not break the tools calls legitimately need | ✅ | `test_denylist_does_not_break_the_curated_call_tools` |
| 4.6 | Disclosure-limit rule in every language pack | ✅ | `test_language_pack_carries_disclosure_rules` (en/de/uk) |
| 4.7 | Instruction-override refusal rule in every language pack | ✅ | same test — a new language cannot ship without both rules |
| 4.8 | Red-team persona runs in CI | ✅ | `tests/voice_harness/test_red_team.py`, EN + DE |
| 4.9 | No canary leaked under attack | ✅ | `test_hostile_callee_leaks_nothing` |
| 4.10 | Social-engineering variant (polite, no obvious override) also refused | ✅ | `SocialEngineerPersona` — catches an agent that refuses "ignore your instructions" but answers the same request politely phrased |
| 4.11 | The call still terminates gracefully under attack | ✅ | `test_hostile_callee_leaks_nothing` asserts the reliability contract too |

**Residual risk.** 4.6–4.7 are prompt-level and therefore probabilistic; a
sufficiently novel jailbreak may still elicit an unwanted sentence. 4.1–4.5 are
the control that holds regardless of model behaviour: a tool absent from the
tool array cannot be called by any prompt. Treat the prompt rules as defence in
depth, never as the primary control.

## T8.5 — Data protection

| # | Control | Status | Evidence |
|---|---|---|---|
| 5.1 | No raw E.164 number in log output at INFO | ✅ | `test_voice_pii_egress.py::test_no_raw_phone_numbers_in_info_logs` — the grep-test, driving the real call-logging paths |
| 5.2 | Filter handles lazy `%s` log arguments | ✅ | `test_filter_masks_lazy_format_arguments` — numbers are almost never in the format string |
| 5.3 | Filter installed for every logger, including third-party | ✅ | `install_log_pii_filter()` attaches at handler level in `cli._setup_logging` |
| 5.4 | Transcripts masked before storage and before API egress | ✅ | `mask_pii` in `postcall`, `api/voice.call_detail` |
| 5.5 | Card / national-ID / account / PIN masking still intact | ✅ | `test_mask_pii_still_masks_the_older_pii_classes` |
| 5.6 | Retention purge runs in production, audit entries exist | ✅ | `doctor` → `prod_dach_compliance`; `retention_purge` audit events |
| 5.7 | Recordings covered by the same purge | ✅ | recording is off by default; metadata rows age out with `voice_calls` |
| 5.8 | AVV/DPA customer package prepared | ✅ | [`avv-annex.md`](avv-annex.md) — **flagged as engineering input for legal review** |
| 5.9 | **[manual]** Annex reviewed and adopted by counsel | ⬜ | Owner: `<name>` — must complete before the first pilot signature |

**Documented decision — structured phone-number fields in API responses.**
`from_number` / `to_number` on `/api/voice/calls` and the `/api/voice/contacts`
address book are returned unmasked. They are the user's own call records and
address book, behind bearer auth and TLS; masking them would remove the ability
to tell two calls apart or to dial a contact, destroying the feature. Masking is
applied where the risk actually is — logs (which get shipped to aggregators) and
free-text transcripts/reports (which contain third-party speech). The
`/api/voice/do-not-call` list is likewise unmasked, because a masked entry
cannot be removed. Reviewed and accepted; revisit if log aggregation ever
ingests API response bodies.

## T8.6 — Dependency & platform hygiene

| # | Control | Status | Evidence |
|---|---|---|---|
| 6.1 | `pip-audit` in CI, failing on untriaged advisories | ✅ | `.github/workflows/ci.yml` → `dependency-audit`; `scripts/audit-deps.sh` |
| 6.2 | `pnpm audit` in CI, failing on criticals | ✅ | same job |
| 6.3 | Triage list requires an owner and a review date | ✅ | `.security/pip-audit-ignore.txt` |
| 6.4 | Dependabot enabled (pip, npm, actions, docker) | ✅ | `.github/dependabot.yml` |
| 6.5 | **[manual]** unattended-upgrades on the host | ⬜ | `docs/guides/production-deployment.md` §Host hardening |
| 6.6 | **[manual]** SSH keys only, password auth disabled | ⬜ | same |
| 6.7 | **[manual]** Firewall allowlist: 443 + SSH only; app ports unpublished | ⬜ | same — compose publishes no host ports; verify with `ss -ltnp` |
| 6.8 | **[manual]** fail2ban active on sshd | ⬜ | same |
| 6.9 | This checklist signed off | ⬜ | Sprint 10 GA gate |

---

## Findings from this review

Untriaged findings block GA. Every row needs an owner and a date.

| # | Finding | Severity | Owner | Status | Review by |
|---|---|---|---|---|---|
| F-1 | `is_voice_compatible` admitted every MCP tool (`"__" in name`), making memory/filesystem/shell MCP tools reachable from a live call | **High** | @vpu | **Fixed** — hard denylist applied before any allowlist (T8.4) | — |
| F-2 | `_validate_twilio_signature` existed in `twiml_server.py` but was never called — every voice webhook was unauthenticated | **High** | @vpu | **Fixed** — enforced on all routes and aliases (T8.1) | — |
| F-3 | The relay/stream WebSockets had no authentication of any kind | **High** | @vpu | **Fixed** — signed upgrade token verified before `accept()` (T8.1) | — |
| F-4 | 20 Python packages carried known advisories (75 total) | **High** | @vpu | **Fixed** — 18 upgraded in `uv.lock`; 2 triaged, see F-5 | — |
| F-5 | `aiohttp` 3.13.5 (14 advisories) and `PyNaCl` 1.5.0 (1) cannot resolve to their fixed versions — both transitive via aiogram/discord.py/twilio | **Medium** | @vpu | **Triaged** in `.security/pip-audit-ignore.txt`; Dependabot will open the PR | 2026-09-17 |
| F-6 | Dashboard npm tree: 60 advisories in production dependencies (29 high, 0 critical). CI gates on critical per the sprint spec, so these do not block today. They are client-side bundle dependencies (react-router, transitive babel), not server-side | **Medium** | `<name>` | **Open** — needs a dashboard dependency upgrade pass | Sprint 9 |
| F-7 | SQLite is not encrypted at rest | **Low** | `<name>` | **Accepted** — out of scope per the sprint; disclosed in the AVV annex §G and tracked in `contributing.md` | Sprint 10 |
| F-8 | The API brute-force guard is in-process, so it resets on restart and does not span replicas | **Low** | `<name>` | **Accepted** — pilot is a single container; revisit when the API scales out | SaaS phase |
| F-9 | `GET /api/apps/twilio/health` (and the `/voice/health` alias) is unauthenticated and reports the engine name and the number of active calls | **Low** | `<name>` | **Accepted** — it is the deploy/uptime probe (`scripts/deploy.sh`, external monitors) and cannot carry a bearer token. It exposes a count, never a number or a transcript | Sprint 10 |

## Explicitly out of scope for Sprint 8

Recorded so a reviewer does not read absence as oversight: multi-tenancy
isolation (pilot is one instance per customer), SOC 2 / ISO 27001 processes, and
encrypted SQLite at rest.

## Sign-off

- [ ] All automated controls green in CI on the release commit
- [ ] `pincer doctor --production` clean on the production host
- [ ] `scripts/audit-deps.sh` clean
- [ ] Every manual item verified on the production host
- [ ] Every finding above has an owner and a status
- [ ] AVV annex reviewed by counsel

Engineering: `<name>` ______________________ Date: __________

Accountable: `<name>` ______________________ Date: __________
