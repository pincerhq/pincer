# Telegram Channel

> **Source**: `src/pincer/channels/telegram.py`

The Telegram channel is Pincer's primary messaging interface, built on [aiogram 3.x](https://docs.aiogram.dev/). It supports text, voice, photos, documents, streaming responses, and inline media delivery.

## Class: `TelegramChannel`

### Constructor

```python
TelegramChannel(settings: Settings)
```

Reads from settings:
- `telegram_bot_token` — Bot token from @BotFather
- `telegram_allowed_users` — Set of allowed user IDs (empty = all allowed)

### Startup

```python
async def start(self, handler: MessageHandler) -> None:
    # Create Bot with Markdown parse mode
    # Create Dispatcher + Router
    # Register all message handlers
    # Start polling as background task
```

The channel uses **long polling** via `Dispatcher.start_polling()`, running as an `asyncio.Task` so it doesn't block the main loop.

### Shutdown

```python
async def stop(self) -> None:
    # Stop polling
    # Close bot session
    # Cancel polling task
```

## Message Handlers

The channel registers handlers for different Telegram message types:

| Handler | Trigger | Description |
|---------|---------|-------------|
| `cmd_start` | `/start` | Welcome message with capabilities |
| `cmd_clear` | `/clear` | Reset conversation via agent |
| `cmd_cost` | `/cost` | Show today's API spend |
| `cmd_help` | `/help` | Show help with available commands |
| `handle_voice` | Voice note | Download + transcribe via Whisper |
| `handle_photo` | Photo | Download + send to LLM as image |
| `handle_document` | Document | Download + classify (image/text/binary) |
| `handle_text` | Text message | Main handler — uses streaming by default |

### User Allowlist

```python
def _is_allowed(self, user_id: int) -> bool:
    if not self._allowed_users:
        return True  # Empty set = allow everyone
    return user_id in self._allowed_users
```

Every handler checks the allowlist before processing. Set `PINCER_TELEGRAM_ALLOWED_USERS=12345,67890` to restrict access.

## Streaming Responses

When the stream agent is available (set via `set_stream_agent()`), text messages use streaming:

1. Send an initial `"..."` message
2. As tokens arrive from the LLM, edit the message every 1.5 seconds
3. Tool events show as `[Using web_search...]` inline
4. When complete, finalize with Markdown formatting
5. If the response exceeds 4096 characters, split into multiple messages

```python
async def send_streaming(self, user_id, chunks):
    msg = await bot.send_message(chat_id, "...", parse_mode=None)
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        if time_elapsed >= 1.5:
            await msg.edit_text(buffer[:safe_limit])
    # Finalize with Markdown
    parts = split_message(buffer)
    await msg.edit_text(parts[0])
    for part in parts[1:]:
        await bot.send_message(chat_id, part)
```

## Message Splitting

Telegram has a 4096-character message limit. The `split_message()` function splits at paragraph boundaries:

```python
def split_message(text: str, max_len: int = 4096) -> list[str]:
    # Split at paragraph boundaries (\n\n)
    # If a paragraph exceeds max, split at line boundaries (\n)
    # Never return empty chunks
```

## Media Sending

### `send_file`

Sends documents using `FSInputFile`:

```python
async def send_file(self, user_id, file_path, caption):
    doc = FSInputFile(file_path)
    await bot.send_document(chat_id, document=doc, caption=caption)
```

### `send_photo`

Two-strategy approach:

1. **Fast path**: Pass URL directly to Telegram (works for public images)
2. **Slow path**: Download with browser User-Agent headers, send as `BufferedInputFile`

```python
async def send_photo(self, user_id, url, caption):
    try:
        await bot.send_photo(chat_id, photo=url)  # Fast path
    except Exception:
        data = await self._download_image(url)     # Slow path
        photo = BufferedInputFile(data, filename="image.jpg")
        await bot.send_photo(chat_id, photo=photo)
```

### `send_animation`

Same two-strategy approach as `send_photo`, but uses `bot.send_animation()` for GIFs.

### Image Download Helper

```python
async def _download_image(self, url: str) -> bytes:
    # Uses aiohttp with browser-like User-Agent
    # Validates content-type is image/*
    # 15-second timeout
```

## Voice Note Handling

```python
async def handle_voice(message):
    # 1. Show typing indicator
    # 2. Download voice file from Telegram servers
    # 3. Pass audio bytes as IncomingMessage.voice_data
    # 4. CLI on_message() handles transcription via Whisper
```

## Photo Handling

```python
async def handle_photo(message):
    # 1. Show typing indicator
    # 2. Download highest-resolution photo
    # 3. Pass as IncomingMessage.images with media_type "image/jpeg"
    # 4. Default caption: "What's in this image?"
```

## Document Handling

```python
async def handle_document(message):
    # 1. Show typing indicator
    # 2. Download document
    # 3. If image MIME type → treat as photo
    # 4. Otherwise → treat as file attachment
    # 5. CLI on_message() handles text extraction / PDF parsing
```

## Markdown Formatting

The bot sends messages with Markdown parse mode by default. If Markdown parsing fails (e.g., unbalanced formatting), it retries with `parse_mode=None` (plain text).
