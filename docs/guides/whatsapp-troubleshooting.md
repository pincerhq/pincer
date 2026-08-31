# WhatsApp troubleshooting

Pincer's WhatsApp channel runs on [`neonize`](https://pypi.org/project/neonize/),
a Python wrapper around the Go `whatsmeow` library. WhatsApp periodically
invalidates old client protocol versions, so any fixed build eventually stops
being accepted by the server. This page covers the failures you're most likely
to hit and how to recover from them.

## `err-client-outdated`

**Symptom**

```
[pincer.channels.whatsapp INFO] Connecting to WhatsApp...
[pincer.channels.whatsapp ERROR] WhatsApp login failed reason=client_outdated
    neonize=<version> action=<remediation>
```

(If you see a bare `Login event: err-client-outdated` with nothing else, you're
on an older Pincer without the actionable handler — upgrade Pincer first.)

**Cause**

WhatsApp's servers rejected the client protocol version advertised by
`whatsmeow`. There is no recovery without upgrading the underlying library or
(temporarily) spoofing the version string.

**Fix — try these in order, stop at the first that works:**

### 1. Upgrade neonize

```bash
uv pip install -U "neonize>=0.3.16"
# or
pip install -U neonize
```

Re-pair is usually required:

```bash
# Neonize stores its session as an sqlite file in the process CWD
# (or wherever you launched pincer from). Typical names include
# pincer-wa.sqlite3 and neonize.db — remove whichever you find.
find . -maxdepth 3 -name "pincer-wa.sqlite3" -o -name "neonize.db"
rm -f ./pincer-wa.sqlite3   # or the path printed above
pincer run --channel whatsapp
```

NOTE: neonize strictly require libmagick library. You should install it system wide if you run app without docker.

MacOS:

```bash
brew install imagemagick
```

Linux:

```bash
# Debian/Ubuntu
sudo apt-get install libmagick++-dev
# Fedora/CentOS/RHEL
sudo yum install ImageMagick-devel
```

Windows:

```
WIP
```

Scan the QR code that appears in the terminal.

### 2. Verify the shipped backend

`neonize` bundles a prebuilt `whatsmeow` binary. If you're on the latest
`neonize` and still failing, the maintainer hasn't published a rebuild yet:

```bash
python - <<'PY'
import os, neonize
print("neonize:", neonize.__version__)
for f in os.listdir(os.path.dirname(neonize.__file__)):
    if f.endswith((".so", ".dylib", ".dll")):
        print("  shipped lib:", f)
PY
```

Cross-check the [neonize PyPI release history](https://pypi.org/project/neonize/#history)
and [the `whatsmeow` repo](https://github.com/tulir/whatsmeow) for a recent
version bump.

### 3. Spoof the version string (only if you know what you're doing)

If no upstream fix is available yet and WhatsApp is business-critical, you can
temporarily override the advertised version. In Pincer's WhatsApp adapter
(`src/pincer/channels/whatsapp.py`), after the `NewAClient(...)` line and
before `.connect()`:

```python
# Match the version reported by web.whatsapp.com right now:
# DevTools → Network → any request → search for "wa-version",
# or look at the inline manifest.json for "version".
self._client.set_version(2, 3000, 1030000000)  # major, minor, patch — example
```

This is a workaround, not a long-term posture — WhatsApp will outpace any
hardcoded number within weeks.

### 4. Fall back to Telegram while you fix it

Pincer supports seven channels. Start the agent on Telegram alone and keep
working until the `neonize` upgrade lands:

```bash
pincer run --channel telegram
```

Don't leave an agent running while its primary channel silently fails —
the new error handler logs loudly, but the agent is still offline for
WhatsApp users until you restart.

## `logged_out`

**Symptom**

```
[pincer.channels.whatsapp ERROR] WhatsApp login failed reason=logged_out ...
```

**Cause**

The phone that owns this WhatsApp account removed Pincer's linked device
session (Settings → Linked Devices → Log out).

**Fix**

Delete the neonize session file (see step 1 above) and re-pair.

## `temp_banned`

**Symptom**

```
[pincer.channels.whatsapp ERROR] WhatsApp login failed reason=temp_banned ...
```

**Cause**

WhatsApp flagged the number for abuse (too many outbound messages, broadcast
list usage, reports from recipients, etc.).

**Fix**

Wait out the ban. The Linked Devices screen in WhatsApp shows the remaining
duration. While waiting, run Pincer on Telegram. When you come back, review
rate-limit settings in `pincer.toml` and your agent's outbound cadence.

## `main_device_gone`

The primary phone is offline or logged out of WhatsApp. Re-verify the primary
device, then delete the neonize session and re-pair.

## Messages processed before identity map is ready

**Symptom**

A message sent to the bot immediately after it starts (or replayed from history-sync on reconnect) is answered correctly, but the conversation is stored under the raw channel ID (`whatsapp:<lid>`) instead of the configured name from `PINCER_IDENTITY_MAP` (e.g. `user:john`). Subsequent messages in the same session resolve correctly.

**Cause**

`neonize` registers the `MessageEv` handler and begins delivering events as soon as the WhatsApp socket handshake completes. Pincer's startup sequence seeds the identity map (`seed_from_config`) only after **all** channels have finished connecting. The gap between WhatsApp connecting and the identity map being ready is typically 1–3 seconds (longer if multiple channels are starting concurrently), but `neonize` may replay a burst of recent history-sync events during exactly that window.

When a message arrives in this gap, `IdentityMiddleware` cannot find the user in the (not-yet-seeded) database. If `PINCER_IDENTITY_MAP` is configured, no new row is created (by design, to prevent phantom identities), and the message falls back to a channel-scoped identity: `whatsapp:<lid>`. Memory and context from that message are stored under the wrong key and will not be merged when the correct identity is seeded moments later.

**Impact**

- Only the first message(s) received in the startup window are affected.
- WhatsApp sends a history-sync burst of recent messages on every reconnect. The existing 120-second age filter (`_MAX_MESSAGE_AGE`) drops most of these before identity resolution runs, so real conversations are rarely affected in practice.
- Any context written under the fallback identity is not automatically migrated; it remains as an orphaned entry in the database.

**Workaround**

If you consistently see this (e.g. a user messages the bot the instant it restarts):

1. Wait a few seconds after seeing `WhatsApp connected` in the console before sending a message.
2. If a message was misidentified, delete the orphaned row from the identity database and re-send: `sqlite3 data/pincer.db "DELETE FROM channel_identities WHERE channel='whatsapp' AND channel_user_id='<lid>';"`.

This is a known limitation; a readiness gate that queues messages until the identity map is seeded is tracked as a future improvement.

**Interaction with `PINCER_WHATSAPP_GUESTS_ALLOWED`**

`PINCER_WHATSAPP_GUESTS_ALLOWED=false` (the default) rejects unmapped senders once an identity map is configured — see [Guest access control](../reference/cli.md#guest-access-control). The guest check blocks (rather than failing open) until the same startup seeding step described above (`seed_from_config`) has completed: a message arriving during the startup gap holds until seeding finishes (typically 1–3 seconds) before the sender is evaluated against the map, rather than being waved through unchecked. If seeding somehow never completes, the check gives up and fails open after 30 seconds so a stuck seed doesn't hang the channel forever — this is logged as an error and should not happen in normal operation. Note this only affects whether a message is *accepted or rejected*; it does not change the fallback-identity behavior described above — a message that arrives before seeding finishes can still be stored under the wrong (channel-scoped) identity even once it's accepted.

## Keeping an eye on this

`pincer doctor` includes a `whatsapp_neonize_version` check. It reports PASS
when the installed `neonize` is at or above the minimum known-good version and
WARNING otherwise, with the exact upgrade command in the fix hint.

## Tracking upstream

- neonize releases: <https://pypi.org/project/neonize/#history>
- `whatsmeow` repo: <https://github.com/tulir/whatsmeow>
- Similar issues in adjacent projects confirm the signature:
  `lharries/whatsapp-mcp` issues #94, #136, #153.
