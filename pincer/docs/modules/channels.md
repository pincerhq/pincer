# Channel System

> **Source**: `src/pincer/channels/`

The channel system provides a unified interface for messaging platforms. Each channel converts platform-specific messages into a common format and handles sending responses back.

## Architecture

```
BaseChannel (ABC)
  ├── TelegramChannel  — Fully implemented (aiogram 3.x)
  ├── WhatsAppChannel   — Placeholder
  ├── DiscordChannel    — Placeholder
  └── WebChannel        — Placeholder
```

## Base Interface

> **Source**: `src/pincer/channels/base.py`

### `IncomingMessage`

Every channel converts its platform-specific message into this unified format:

```python
@dataclass
class IncomingMessage:
    user_id: str                                    # Platform user ID
    channel: str                                    # "telegram", "whatsapp", etc.
    text: str = ""                                  # Message text
    images: list[tuple[bytes, str]]                 # [(raw_bytes, media_type)]
    files: list[tuple[bytes, str, str]]             # [(raw_bytes, mime_type, filename)]
    voice_data: bytes | None = None                 # Raw audio bytes
    voice_mime: str = ""                            # Audio MIME type
    reply_to_message_id: str | None = None          # For threaded replies
    raw: Any = None                                 # Original platform message
```

Properties:
- `has_voice` — Whether this message contains voice data
- `has_files` — Whether this message has file attachments

### `MessageHandler`

The callback type that channels invoke when a message arrives:

```python
MessageHandler = Callable[[IncomingMessage], Awaitable[str]]
```

### `BaseChannel`

```python
class BaseChannel(ABC):
    @property
    def name(self) -> str: ...           # "telegram", "whatsapp", etc.
    async def start(self, handler) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, user_id, text) -> None: ...
    async def send_file(self, user_id, file_path, caption) -> None: ...
    async def send_photo(self, user_id, url, caption) -> None: ...
    async def send_animation(self, user_id, url, caption) -> None: ...
    async def send_streaming(self, user_id, chunks) -> None: ...
```

Default implementations for `send_file`, `send_photo`, `send_animation` fall back to plain text messages. `send_streaming` collects all chunks and sends as one message. Channels can override these with platform-specific implementations.

## Adding a New Channel

To implement a new messaging channel:

1. Create `src/pincer/channels/my_channel.py`
2. Implement `BaseChannel`:
   - `name` property returning a unique identifier
   - `start()` to begin listening (polling, webhook, etc.)
   - `stop()` to disconnect and clean up
   - `send()` for basic text messages
   - Override `send_file`, `send_photo`, `send_animation`, `send_streaming` as needed
3. Convert incoming platform messages to `IncomingMessage`
4. Call the `handler(incoming)` callback and send the response back
5. Add initialization in `cli.py`'s `_run_agent()` function
6. Register in the `channel_map` dict for `send_file` / `send_image` tool support

## Channel Lifecycle

```
startup:
  1. Create channel instance with Settings
  2. Call channel.start(on_message)
  3. Channel begins polling / listening
  4. Register in channel_map

runtime:
  1. Channel receives platform message
  2. Convert to IncomingMessage
  3. Call handler(incoming) → get response string
  4. Send response back via platform API

shutdown:
  1. Call channel.stop()
  2. Close platform connections
```
