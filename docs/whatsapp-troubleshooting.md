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

## Keeping an eye on this

`pincer doctor` includes a `whatsapp_neonize_version` check. It reports PASS
when the installed `neonize` is at or above the minimum known-good version and
WARNING otherwise, with the exact upgrade command in the fix hint.

## Tracking upstream

- neonize releases: <https://pypi.org/project/neonize/#history>
- `whatsmeow` repo: <https://github.com/tulir/whatsmeow>
- Similar issues in adjacent projects confirm the signature:
  `lharries/whatsapp-mcp` issues #94, #136, #153.
