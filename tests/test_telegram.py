"""Tests for Telegram channel utilities."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Router

from pincer.channels.telegram import TelegramChannel, split_message


def test_split_short_message() -> None:
    text = "Hello, world!"
    chunks = split_message(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_long_message() -> None:
    # Create a message longer than 4096 chars
    paragraph = "This is a test paragraph. " * 50  # ~1300 chars
    text = "\n\n".join([paragraph] * 5)  # ~6500 chars
    chunks = split_message(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 4096


def test_split_preserves_content() -> None:
    text = "Part 1\n\nPart 2\n\nPart 3"
    chunks = split_message(text)
    assert len(chunks) == 1
    assert "Part 1" in chunks[0]
    assert "Part 2" in chunks[0]
    assert "Part 3" in chunks[0]


def _make_channel() -> TelegramChannel:
    settings = MagicMock()
    channel = TelegramChannel(settings)
    channel._bot = AsyncMock()
    return channel


async def test_send_with_non_numeric_user_id_raises_once() -> None:
    """A canonical id (not a real Telegram chat id) must fail once, clearly — not twice (issue #162)."""
    channel = _make_channel()

    with pytest.raises(ValueError, match="invalid chat_id 'r_lutsiv'"):
        await channel.send("r_lutsiv", "hello")

    # The retry-on-failure path (send_message called a second time with
    # parse_mode=None) must never be reached — chat_id is invalid, not the markdown.
    channel._bot.send_message.assert_not_called()


async def test_send_file_with_non_numeric_user_id_raises_once(tmp_path) -> None:
    channel = _make_channel()
    file_path = tmp_path / "report.csv"
    file_path.write_text("a,b,c\n")

    with pytest.raises(ValueError, match="invalid chat_id 'r_lutsiv'"):
        await channel.send_file("r_lutsiv", str(file_path))

    channel._bot.send_document.assert_not_called()
    # send_file's except-fallback (self.send(...)) must not even be attempted —
    # it would fail on the exact same invalid id, doubling the exception.
    channel._bot.send_message.assert_not_called()


async def test_send_with_numeric_user_id_still_works() -> None:
    channel = _make_channel()

    await channel.send("123456789", "hello")

    channel._bot.send_message.assert_called_once_with(chat_id=123456789, text="hello")


async def test_send_converts_markdown_to_html() -> None:
    channel = _make_channel()

    await channel.send("123456789", "**bold** and `code`")

    channel._bot.send_message.assert_called_once_with(chat_id=123456789, text="<b>bold</b> and <code>code</code>")


async def test_send_falls_back_to_plain_unconverted_text_on_error() -> None:
    channel = _make_channel()
    channel._bot.send_message = AsyncMock(side_effect=[Exception("bad markup"), None])

    await channel.send("123456789", "**bold**")

    assert channel._bot.send_message.call_count == 2
    first_call, second_call = channel._bot.send_message.call_args_list
    assert first_call.kwargs["text"] == "<b>bold</b>"
    assert second_call.kwargs == {"chat_id": 123456789, "text": "**bold**", "parse_mode": None}


async def test_request_approval_falls_back_to_plain_text_on_markdown_error() -> None:
    """Regression: a malformed-markdown args_preview must not silently drop the approval prompt."""
    channel = _make_channel()
    channel._bot.send_message = AsyncMock(side_effect=[Exception("bad markup"), None])

    task = asyncio.ensure_future(channel.request_approval("123456789", "some_tool", {"arg": "value"}))
    await asyncio.sleep(0)  # let request_approval register the pending future before we resolve it
    channel._pending_approvals["123456789"].set_result(True)
    result = await task

    assert result is True
    assert channel._bot.send_message.call_count == 2
    first_call, second_call = channel._bot.send_message.call_args_list
    assert first_call.kwargs["chat_id"] == 123456789
    assert "reply_markup" in first_call.kwargs
    assert second_call.kwargs["chat_id"] == 123456789
    assert second_call.kwargs["parse_mode"] is None
    assert "reply_markup" in second_call.kwargs


async def test_send_photo_fast_path_uses_chat_id() -> None:
    channel = _make_channel()

    await channel.send_photo("123456789", "https://example.com/cat.png", caption="cat")

    channel._bot.send_photo.assert_called_once()
    kwargs = channel._bot.send_photo.call_args.kwargs
    assert kwargs["chat_id"] == 123456789
    assert kwargs["photo"] == "https://example.com/cat.png"
    assert kwargs["caption"] == "cat"


async def test_send_photo_with_non_numeric_user_id_raises() -> None:
    channel = _make_channel()

    with pytest.raises(ValueError, match="invalid chat_id 'r_lutsiv'"):
        await channel.send_photo("r_lutsiv", "https://example.com/cat.png")

    channel._bot.send_photo.assert_not_called()


async def test_send_photo_from_bytes_uses_chat_id() -> None:
    channel = _make_channel()

    await channel.send_photo_from_bytes("123456789", b"PNGDATA", "image/png", "cat")

    channel._bot.send_photo.assert_called_once()
    kwargs = channel._bot.send_photo.call_args.kwargs
    assert kwargs["chat_id"] == 123456789
    assert kwargs["photo"].data == b"PNGDATA"
    assert kwargs["caption"] == "cat"


async def test_send_animation_fast_path_uses_chat_id() -> None:
    channel = _make_channel()

    await channel.send_animation("123456789", "https://example.com/cat.gif", caption="cat")

    channel._bot.send_animation.assert_called_once()
    kwargs = channel._bot.send_animation.call_args.kwargs
    assert kwargs["chat_id"] == 123456789
    assert kwargs["animation"] == "https://example.com/cat.gif"


async def test_send_animation_with_non_numeric_user_id_raises() -> None:
    channel = _make_channel()

    with pytest.raises(ValueError, match="invalid chat_id 'r_lutsiv'"):
        await channel.send_animation("r_lutsiv", "https://example.com/cat.gif")

    channel._bot.send_animation.assert_not_called()


async def test_send_streaming_uses_chat_id() -> None:
    channel = _make_channel()
    sent_msg = AsyncMock()
    channel._bot.send_message.return_value = sent_msg

    async def one_chunk():
        yield "hello"

    await channel.send_streaming("123456789", one_chunk())

    channel._bot.send_message.assert_called_once_with(chat_id=123456789, text="...", parse_mode=None)
    sent_msg.edit_text.assert_called_once_with("hello")


async def test_send_streaming_with_non_numeric_user_id_raises() -> None:
    channel = _make_channel()

    async def one_chunk():
        yield "hello"

    with pytest.raises(ValueError, match="invalid chat_id 'r_lutsiv'"):
        await channel.send_streaming("r_lutsiv", one_chunk())

    channel._bot.send_message.assert_not_called()


async def test_send_streaming_sends_overflow_parts_as_new_messages() -> None:
    """When the streamed text is long enough to split, parts after the first are sent (not edited)."""
    channel = _make_channel()
    sent_msg = AsyncMock()
    channel._bot.send_message.return_value = sent_msg

    paragraph = "This is a test paragraph. " * 50
    long_text = "\n\n".join([paragraph] * 5)

    async def one_chunk():
        yield long_text

    await channel.send_streaming("123456789", one_chunk())

    # First send_message call is the streaming placeholder "..."; subsequent
    # calls are the overflow parts sent via markdown_to_telegram_html.
    assert channel._bot.send_message.call_count >= 2
    sent_msg.edit_text.assert_called_once()


def _registered_handlers() -> tuple[TelegramChannel, Router]:
    channel = _make_channel()
    router = Router()
    channel._register_handlers(router)
    return channel, router


def _mock_message(text: str | None = None, user_id: int = 123456789) -> MagicMock:
    message = MagicMock()
    message.from_user.id = user_id
    message.text = text
    message.answer = AsyncMock()
    message.chat.id = user_id
    return message


async def test_cmd_cost_handler_sends_response_via_send() -> None:
    channel, router = _registered_handlers()
    channel._handler = AsyncMock(return_value="Today's spend: $1.23")
    channel.send = AsyncMock()

    cmd_cost = router.message.handlers[2].callback
    await cmd_cost(_mock_message())

    channel.send.assert_awaited_once_with("123456789", "Today's spend: $1.23")


async def test_handle_voice_sends_response_via_send() -> None:
    channel, router = _registered_handlers()
    channel._handler = AsyncMock(return_value="transcribed reply")
    channel.send = AsyncMock()

    message = _mock_message()
    message.caption = None
    message.voice.file_id = "voice1"
    message.voice.mime_type = "audio/ogg"
    fake_file = MagicMock()
    fake_file.file_path = "path/to/voice.ogg"
    channel._bot.get_file = AsyncMock(return_value=fake_file)

    async def _fake_download(_path, buf):
        buf.write(b"voicedata")

    channel._bot.download_file = AsyncMock(side_effect=_fake_download)

    handle_voice = router.message.handlers[4].callback
    await handle_voice(message)

    channel.send.assert_awaited_once_with("123456789", "transcribed reply")


async def test_handle_photo_sends_response_via_send() -> None:
    channel, router = _registered_handlers()
    channel._handler = AsyncMock(return_value="nice photo")
    channel.send = AsyncMock()

    message = _mock_message()
    message.caption = None
    photo = MagicMock()
    photo.file_id = "photo1"
    message.photo = [photo]
    fake_file = MagicMock()
    fake_file.file_path = "path/to/photo.jpg"
    channel._bot.get_file = AsyncMock(return_value=fake_file)

    async def _fake_download(_path, buf):
        buf.write(b"imgdata")

    channel._bot.download_file = AsyncMock(side_effect=_fake_download)

    handle_photo = router.message.handlers[5].callback
    await handle_photo(message)

    channel.send.assert_awaited_once_with("123456789", "nice photo")


async def test_handle_document_sends_response_via_send() -> None:
    channel, router = _registered_handlers()
    channel._handler = AsyncMock(return_value="got your file")
    channel.send = AsyncMock()

    message = _mock_message()
    message.caption = None
    doc = MagicMock()
    doc.file_id = "doc1"
    doc.file_name = "report.csv"
    doc.mime_type = "text/csv"
    message.document = doc
    fake_file = MagicMock()
    fake_file.file_path = "path/to/report.csv"
    channel._bot.get_file = AsyncMock(return_value=fake_file)

    async def _fake_download(_path, buf):
        buf.write(b"a,b,c\n")

    channel._bot.download_file = AsyncMock(side_effect=_fake_download)

    handle_document = router.message.handlers[6].callback
    await handle_document(message)

    channel.send.assert_awaited_once_with("123456789", "got your file")


async def test_handle_text_sends_response_via_send() -> None:
    channel, router = _registered_handlers()
    channel._handler = AsyncMock(return_value="hi there")
    channel.send = AsyncMock()

    handle_text = router.message.handlers[7].callback
    await handle_text(_mock_message(text="hello bot"))

    channel.send.assert_awaited_once_with("123456789", "hi there")


async def test_is_guest_blocked_false_when_no_identity_configured() -> None:
    channel = _make_channel()
    assert channel._identity is None
    assert await channel._is_guest_blocked(123456789) is False


async def test_is_guest_blocked_true_when_guest_and_not_allowed() -> None:
    channel = _make_channel()
    channel._settings.telegram_guests_allowed = False
    channel._identity = AsyncMock()
    channel._identity.is_guest = AsyncMock(return_value=True)

    assert await channel._is_guest_blocked(123456789) is True


async def test_is_guest_blocked_false_when_guests_allowed() -> None:
    channel = _make_channel()
    channel._settings.telegram_guests_allowed = True
    channel._identity = AsyncMock()
    channel._identity.is_guest = AsyncMock(return_value=True)

    assert await channel._is_guest_blocked(123456789) is False
    channel._identity.is_guest.assert_not_called()


async def test_handle_text_blocked_for_unmapped_sender() -> None:
    channel, router = _registered_handlers()
    channel._handler = AsyncMock(return_value="hi there")
    channel.send = AsyncMock()
    channel._settings.telegram_guests_allowed = False
    channel._identity = AsyncMock()
    channel._identity.is_guest = AsyncMock(return_value=True)

    handle_text = router.message.handlers[7].callback
    await handle_text(_mock_message(text="hello bot"))

    channel._handler.assert_not_called()
    channel.send.assert_not_called()


def _mock_group_message(
    text: str | None = None,
    user_id: int = 987654321,
    entities: list | None = None,
    reply_to_bot_id: int | None = None,
) -> MagicMock:
    message = MagicMock()
    message.from_user.id = user_id
    message.text = text
    message.caption = None
    message.caption_entities = None
    message.entities = entities
    message.answer = AsyncMock()
    message.chat.id = -100123456789
    message.chat.type = "group"
    if reply_to_bot_id is not None:
        message.reply_to_message = MagicMock()
        message.reply_to_message.from_user.id = reply_to_bot_id
    else:
        message.reply_to_message = None
    return message


def test_is_triggered_in_group_by_trigger_word() -> None:
    channel = _make_channel()
    channel._settings.telegram_group_trigger = "pincer"
    message = _mock_group_message(text="hey pincer, what's up?")
    assert channel._is_triggered_in_group(message) is True


def test_is_triggered_in_group_by_mention_entity() -> None:
    channel = _make_channel()
    channel._settings.telegram_group_trigger = ""
    channel._own_username = "PincerBot"
    text = "hey @PincerBot help me"
    entity = MagicMock()
    entity.type = "mention"
    entity.offset = text.index("@PincerBot")
    entity.length = len("@PincerBot")
    message = _mock_group_message(text=text, entities=[entity])
    assert channel._is_triggered_in_group(message) is True


def test_is_triggered_in_group_by_reply_to_bot() -> None:
    channel = _make_channel()
    channel._settings.telegram_group_trigger = ""
    channel._bot.id = 111
    message = _mock_group_message(text="what do you think?", reply_to_bot_id=111)
    assert channel._is_triggered_in_group(message) is True


def test_is_triggered_in_group_false_when_no_match() -> None:
    channel = _make_channel()
    channel._settings.telegram_group_trigger = "pincer"
    message = _mock_group_message(text="just chatting here")
    assert channel._is_triggered_in_group(message) is False


async def test_handle_text_group_without_trigger_ignored() -> None:
    """Group chatter that doesn't mention/trigger Pincer is dropped before the guest check."""
    channel, router = _registered_handlers()
    channel._handler = AsyncMock(return_value="hi there")
    channel.send = AsyncMock()
    channel._settings.telegram_group_trigger = "pincer"

    handle_text = router.message.handlers[7].callback
    await handle_text(_mock_group_message(text="just chatting here"))

    channel._handler.assert_not_called()
    channel.send.assert_not_called()


async def test_handle_text_group_with_trigger_responds() -> None:
    channel, router = _registered_handlers()
    channel._handler = AsyncMock(return_value="hi there")
    channel.send = AsyncMock()
    channel._settings.telegram_group_trigger = "pincer"
    channel._settings.telegram_guests_allowed = True

    handle_text = router.message.handlers[7].callback
    await handle_text(_mock_group_message(text="hey pincer, help me"))

    channel._handler.assert_called_once()
    channel.send.assert_awaited_once()


async def test_handle_photo_group_without_trigger_ignored() -> None:
    """The group-trigger gate applies uniformly across message types, not just text."""
    channel, router = _registered_handlers()
    channel._handler = AsyncMock(return_value="nice photo")
    channel.send = AsyncMock()
    channel._settings.telegram_group_trigger = "pincer"

    message = _mock_group_message(text=None)
    photo = MagicMock()
    photo.file_id = "photo1"
    message.photo = [photo]

    handle_photo = router.message.handlers[5].callback
    await handle_photo(message)

    channel._handler.assert_not_called()
    channel.send.assert_not_called()
    channel._bot.get_file.assert_not_called()


async def test_cmd_help_blocked_for_unmapped_sender() -> None:
    """cmd_help must respect the guest gate like every other command/handler."""
    channel, router = _registered_handlers()
    channel._settings.telegram_guests_allowed = False
    channel._identity = AsyncMock()
    channel._identity.is_guest = AsyncMock(return_value=True)

    cmd_help = router.message.handlers[3].callback
    message = _mock_message()
    await cmd_help(message)

    message.answer.assert_not_called()


async def test_cmd_help_responds_when_allowed() -> None:
    channel, router = _registered_handlers()
    assert channel._identity is None

    cmd_help = router.message.handlers[3].callback
    message = _mock_message()
    await cmd_help(message)

    message.answer.assert_called_once()


async def test_tool_approval_callback_edits_message_with_escaped_label() -> None:
    channel, router = _registered_handlers()

    callback = MagicMock()
    callback.data = "tool_approve:req-1"
    callback.answer = AsyncMock()
    callback.message.text = "Approval required <danger>"
    callback.message.edit_text = AsyncMock()

    handle_tool_approval = router.callback_query.handlers[0].callback
    await handle_tool_approval(callback)

    callback.answer.assert_awaited_once_with("Approved")
    callback.message.edit_text.assert_awaited_once()
    (edited_text,), _ = callback.message.edit_text.call_args
    assert "&lt;danger&gt;" in edited_text
    assert "<b>Approved</b>" in edited_text
