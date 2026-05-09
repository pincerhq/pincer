"""
Voice transcription using OpenAI Whisper API.

Accepts audio bytes (OGG, MP3, WAV, etc.) and returns transcribed text.
Language is auto-detected by Whisper.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# Whisper-supported MIME types mapped to file extensions
_MIME_TO_EXT: dict[str, str] = {
    "audio/ogg": "ogg",
    "audio/oga": "ogg",
    "audio/opus": "ogg",
    "audio/mp3": "mp3",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mp4": "mp4",
    "audio/m4a": "m4a",
    "audio/webm": "webm",
    "audio/flac": "flac",
    "audio/aac": "aac",  # Signal voice notes
}


async def transcribe_voice(
    audio_data: bytes,
    mime_type: str,
    api_key: str,
) -> str:
    """
    Transcribe audio using the OpenAI Whisper API.

    Returns the transcribed text, or an error message string.
    """
    if not api_key:
        return "[Voice transcription requires PINCER_OPENAI_API_KEY to be set]"

    ext = _MIME_TO_EXT.get(mime_type, "ogg")

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key)
        audio_file = io.BytesIO(audio_data)
        audio_file.name = f"voice.{ext}"

        transcription = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )
        await client.close()

        text = transcription.text.strip()
        logger.info("Transcribed %d bytes of audio -> %d chars", len(audio_data), len(text))
        return text

    except ImportError:
        return "[Voice transcription requires the openai package]"
    except Exception as e:
        logger.exception("Voice transcription failed")
        return f"[Voice transcription failed: {type(e).__name__}: {e}]"
