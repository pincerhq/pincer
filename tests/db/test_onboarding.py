"""Onboarding state on the repository/service layer, on both dialects."""

from __future__ import annotations

import pytest

from pincer.db.engine import get_engine
from pincer.models.identity import AuthSession, ChannelIdentity, IdentityCredential, IdentityProfile
from pincer.models.onboarding import Onboarding
from pincer.models.sessions import ChatSession
from pincer.services import onboarding as onboarding_module
from pincer.services.identity import IdentityService
from pincer.services.onboarding import OnboardingService
from pincer.services.sessions import SessionService

_TABLES = [
    IdentityProfile.__table__,
    ChannelIdentity.__table__,
    IdentityCredential.__table__,
    AuthSession.__table__,
    ChatSession.__table__,
    Onboarding.__table__,
]

_CONVERSATION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Onboarding.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: Onboarding.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Onboarding.metadata.drop_all(c, tables=_TABLES))


async def _store_session(
    url: str, session_id: str, *, user_id: str, channel: str = "web", metadata: str = "{}"
) -> None:
    await SessionService(url).save(
        session_id=session_id,
        user_id=user_id,
        channel=channel,
        messages="[]",
        metadata=metadata,
        created_at=1.0,
        updated_at=1.0,
    )


async def test_an_unknown_user_has_no_record(url):
    assert await OnboardingService(url).get("u", "web") is None


async def test_claiming_the_prompt_is_first_writer_wins(url):
    service = OnboardingService(url)

    assert await service.claim_prompt("u", "web", "web:u:a", now=10.0) is True
    assert await service.claim_prompt("u", "web", "web:u:b", now=11.0) is False

    record = await service.get("u", "web")
    assert record is not None
    assert (record.asked_session_id, record.asked_at, record.completed_at) == ("web:u:a", 10.0, None)


async def test_completing_records_when_and_keeps_the_asking_session(url):
    service = OnboardingService(url)
    await service.claim_prompt("u", "web", "web:u", now=10.0)

    await service.complete("u", "web", now=20.0)

    record = await service.get("u", "web")
    assert record is not None
    assert (record.asked_session_id, record.asked_at, record.completed_at) == ("web:u", 10.0, 20.0)


async def test_only_the_first_completion_wins(url):
    """Two turns completing at once: one is told it won, and the time is its."""
    service = OnboardingService(url)
    await service.claim_prompt("u", "web", "web:u", now=10.0)
    assert await service.complete("u", "web", now=20.0) is True

    assert await service.complete("u", "web", now=30.0) is False

    record = await service.get("u", "web")
    assert record is not None
    assert record.completed_at == 20.0


async def test_an_empty_reply_counts_and_the_second_closes(url):
    service = OnboardingService(url)
    await service.claim_prompt("u", "web", "web:u", now=10.0)

    assert await service.record_empty_reply("u", "web", max_empty=2, now=20.0) is False
    record = await service.get("u", "web")
    assert record is not None
    assert (record.attempts, record.completed_at) == (1, None)

    assert await service.record_empty_reply("u", "web", max_empty=2, now=30.0) is True
    record = await service.get("u", "web")
    assert record is not None
    assert (record.attempts, record.completed_at) == (2, 30.0)


async def test_an_empty_reply_without_a_row_inserts_one(url):
    """A user asked before the table existed: an empty reply is counted too."""
    service = OnboardingService(url)

    assert await service.record_empty_reply("u", "web", max_empty=2, now=20.0) is False

    record = await service.get("u", "web")
    assert record is not None
    assert (record.asked_session_id, record.attempts, record.completed_at) == (None, 1, None)


async def test_completing_without_a_row_inserts_one(url):
    """A user asked before the table existed: the reply still closes the gate."""
    service = OnboardingService(url)

    assert await service.complete("u", "web", now=20.0) is True

    record = await service.get("u", "web")
    assert record is not None
    assert (record.asked_session_id, record.completed_at) == (None, 20.0)


async def test_the_record_is_per_user_and_channel(url):
    service = OnboardingService(url)
    await service.claim_prompt("u", "slack-thread-1", "slack-thread-1:u", now=10.0)

    assert await service.get("u", "slack-thread-2") is None
    assert await service.get("v", "slack-thread-1") is None


async def test_stored_sessions_count_conversations_for_that_user_and_channel_only(url):
    service = SessionService(url)
    assert await service.has_any("u", "web") is False

    await _store_session(url, f"web:u:{_CONVERSATION}", user_id="u")
    await _store_session(url, "telegram:u", user_id="u", channel="telegram")
    await _store_session(url, "web:v", user_id="v")

    assert await service.has_any("u", "web") is True
    assert await service.has_any("u", "slack") is False
    assert await service.has_any("w", "web") is False


async def test_stored_sessions_leave_out_memory_free_conversations(url):
    service = SessionService(url)
    await _store_session(url, f"web:u:{_CONVERSATION}", user_id="u", metadata='{"memory_free": "true"}')
    await _store_session(url, "web:v", user_id="v", metadata='{"onboarding_complete": "true"}')
    await _store_session(url, "web:w", user_id="w", metadata="")

    assert await service.has_any("u", "web") is False
    assert await service.has_any("v", "web") is True
    assert await service.has_any("w", "web") is True


async def test_renaming_moves_onboarding_and_its_asking_session(url):
    identities = IdentityService(url)
    await identities.create_identity("usr_hash", channel="web", channel_user_id="123")
    onboarding = OnboardingService(url)
    await onboarding.claim_prompt("usr_hash", "web", f"web:usr_hash:{_CONVERSATION}", now=10.0)

    await identities.rename_identity("usr_hash", "alice")

    assert await onboarding.get("usr_hash", "web") is None
    record = await onboarding.get("alice", "web")
    assert record is not None
    assert record.asked_session_id == f"web:alice:{_CONVERSATION}"


@pytest.mark.parametrize(
    ("asked", "moved"),
    [
        (f"web:e:{_CONVERSATION}", f"web:alice:{_CONVERSATION}"),
        ("web:e", "web:alice"),
        ("slack-thread-e:e", "slack-thread-e:alice"),
    ],
)
async def test_renaming_rekeys_the_asking_session_like_the_session_itself(url, asked, moved):
    """The id is replaced as a whole segment, as the session's own key is."""
    identities = IdentityService(url)
    await identities.create_identity("e", channel="web", channel_user_id="123")
    onboarding = OnboardingService(url)
    await onboarding.claim_prompt("e", "web", asked, now=10.0)

    await identities.rename_identity("e", "alice")

    record = await onboarding.get("alice", "web")
    assert record is not None
    assert record.asked_session_id == moved


async def test_merging_keeps_the_source_s_completed_onboarding(url):
    """The source answered, the target was only asked: the answer survives."""
    identities = IdentityService(url)
    await identities.create_identity("usr_hash", channel="web", channel_user_id="123")
    await identities.create_identity("alice", channel="telegram", channel_user_id="456")
    onboarding = OnboardingService(url)
    await onboarding.claim_prompt("usr_hash", "web", "web:usr_hash", now=10.0)
    await onboarding.complete("usr_hash", "web", now=11.0)
    await onboarding.claim_prompt("alice", "web", "web:alice", now=5.0)

    await identities.rename_identity("usr_hash", "alice")

    record = await onboarding.get("alice", "web")
    assert record is not None
    assert (record.asked_session_id, record.completed_at) == ("web:alice", 11.0)
    assert await onboarding.get("usr_hash", "web") is None


async def test_a_completed_pair_is_cached_and_forgotten_on_rename(url, monkeypatch):
    identities = IdentityService(url)
    await identities.create_identity("usr_hash", channel="web", channel_user_id="123")
    onboarding = OnboardingService(url)
    await onboarding.claim_prompt("usr_hash", "web", "web:usr_hash", now=10.0)
    assert onboarding.is_completed_cached("usr_hash", "web") is False

    await onboarding.complete("usr_hash", "web", now=11.0)

    assert onboarding.is_completed_cached("usr_hash", "web") is True
    await identities.rename_identity("usr_hash", "alice")
    assert onboarding.is_completed_cached("usr_hash", "web") is False
    assert onboarding.is_completed_cached("alice", "web") is False
    assert (await onboarding.get("alice", "web")).completed_at == 11.0  # type: ignore[union-attr]
    assert onboarding.is_completed_cached("alice", "web") is True


async def test_the_completed_cache_is_bounded(url, monkeypatch):
    monkeypatch.setattr(onboarding_module, "_COMPLETED_CACHE_SIZE", 2)
    onboarding = OnboardingService(url)
    for user in ("a", "b", "c"):
        await onboarding.complete(user, "web", now=1.0)

    assert [onboarding.is_completed_cached(user, "web") for user in ("a", "b", "c")] == [False, True, True]


async def test_merging_keeps_the_target_s_onboarding(url):
    identities = IdentityService(url)
    await identities.create_identity("usr_hash", channel="web", channel_user_id="123")
    await identities.create_identity("alice", channel="telegram", channel_user_id="456")
    onboarding = OnboardingService(url)
    await onboarding.claim_prompt("usr_hash", "web", "web:usr_hash", now=10.0)
    await onboarding.claim_prompt("alice", "web", "web:alice", now=5.0)
    await onboarding.complete("alice", "web", now=6.0)

    await identities.rename_identity("usr_hash", "alice")

    record = await onboarding.get("alice", "web")
    assert record is not None
    assert (record.asked_session_id, record.completed_at) == ("web:alice", 6.0)
    assert await onboarding.get("usr_hash", "web") is None
