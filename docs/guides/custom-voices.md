# Custom Voices (ElevenLabs Cloning)

Give your agent a specific voice — a brand voice, or a clone of a real person's voice (with their consent). Pincer speaks with any voice in your ElevenLabs account on the `media_streams` engine, and with public-library ElevenLabs voices on `conversation_relay`.

> **Custom clones require `media_streams`.** Twilio's ConversationRelay integration only exposes ElevenLabs' public voice library; it does not currently document a way to link your own ElevenLabs API key. Voices you cloned yourself are only visible to your account, so set `PINCER_VOICE_ENGINE=media_streams` to use them.

---

## 1. Create the clone in ElevenLabs

Voice creation happens in the [ElevenLabs UI](https://elevenlabs.io/app/voice-lab) — Pincer doesn't generate voices, it uses them.

- **Instant Voice Clone** (Starter plan+): ~1–3 minutes of clean audio. Good enough for many use cases; accents and emotional range are approximate.
- **Professional Voice Clone** (Creator plan+): 30 minutes to 3 hours of audio, several hours of training. Markedly better on telephony, where 8kHz mu-law strips much of what makes a voice recognizable.

Source-audio quality matters more than quantity:

- One speaker only, no music/background noise, no room echo.
- Consistent mic, distance, and tone across recordings — the clone reproduces the *average* of what you feed it.
- Speak the way the agent should speak: conversational pace, phone-call register. A clone from audiobook narration sounds like audiobook narration.
- For German deployments, include German source audio — a clone from English-only audio speaks German with an English accent.

## 2. Wire it into Pincer

```bash
pincer voice list          # find the clone's voice ID (category: cloned/professional)
```

```env
PINCER_VOICE_ENGINE=media_streams
PINCER_ELEVENLABS_API_KEY=your-key
PINCER_ELEVENLABS_VOICE_ID=<your-clone-id>
# or per-language:
PINCER_ELEVENLABS_VOICE_ID_DE=<german-tuned-clone>
PINCER_ELEVENLABS_VOICE_ID_EN=<english-tuned-clone>
```

Then judge it **at telephony quality** before any live call:

```bash
pincer voice test --language de
# → ~/.pincer/voice_test.wav (16 kHz) and ~/.pincer/voice_test_ulaw.wav (8 kHz mu-law)
```

Listen to the mu-law file — that's what callers hear. If it sounds off, try `PINCER_ELEVENLABS_STABILITY=0.6`–`0.7` (steadier, less expressive) or a Professional clone.

`pincer doctor` verifies the configured voice IDs exist in your account; a bad ID fails at startup, never mid-call.

## 3. Consent and compliance — read this before cloning anyone

Cloning a voice without the speaker's permission is not a gray area:

- **ElevenLabs Terms of Service require verifiable consent** from the voice's owner. Professional clones include a spoken verification step; circumventing it violates the ToS and can get the account terminated.
- **Germany/Austria/Switzerland:** a person's voice is protected as part of their personality rights (allgemeines Persönlichkeitsrecht, §§ 823, 1004 BGB analog) and voice recordings of an identifiable person are personal — biometric — data under GDPR Art. 4/9. You need documented, informed consent from the speaker covering *this specific use* (an AI agent making phone calls with their voice), and you must be able to produce that documentation.
- Keep the consent record with your DPA/AVV paperwork — see [DACH Compliance](dach-compliance.md).

**Keep the AI disclosure ON for cloned voices.** The Sprint 0 announcement ("this is an AI assistant calling…") becomes *more* important, not less, when the agent sounds convincingly human. A callee who realizes mid-call that the "person" they've been talking to is a clone of a real voice will reasonably feel deceived — and in DACH that perception carries legal weight (UWG unfair-practices exposure, plus the GDPR transparency principle). Recommended settings for cloned-voice deployments:

```env
PINCER_VOICE_ASSISTANT_NAME=Pincer          # never empty for cloned voices
PINCER_VOICE_ASSISTANT_ORG=your-company     # who is calling
PINCER_VOICE_CONSENT_MODE=two_party         # DACH: all-party consent for recording
```

Do **not** clone the voice of a public figure, a colleague "as a joke", or anyone who hasn't signed off. Pincer will speak with whatever voice ID you configure; the responsibility for having the right to use it is yours.

## 4. Release checklist (manual, before going live with a custom voice)

- [ ] `pincer doctor` — `voice_elevenlabs_voices` green
- [ ] `pincer voice test` — mu-law sample judged acceptable by a human
- [ ] 2 live calls on `conversation_relay` with the configured voice, one in German
- [ ] 2 live calls on `media_streams` with the custom voice, one in German
- [ ] Honest answer to: "Would I notice this is a bot from voice quality alone?" — if no, double-check the AI disclosure is on

---

## See also

- [Voice Calling Setup](voice-calling-setup.md) — ElevenLabs configuration for both engines
- [Voice Calling](../core-components/voice-calling.md) — architecture and full configuration reference
- [DACH Compliance](dach-compliance.md) — GDPR/consent documentation for voice deployments
