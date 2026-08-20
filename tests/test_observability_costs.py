"""Per-call cost record and LLM attribution (Sprint 9, T9.1).

The attribution tests matter most: `cost_log.session_id` is per-user, not
per-call, so the only thing standing between "cost per call" and "cost per user's
whole afternoon" is the ContextVar binding.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from pincer.observability import call_costs
from pincer.observability.call_costs import (
    CallCost,
    add_stt_seconds,
    attribute_llm_cost,
    begin_call,
    call_context,
    current_call_sid,
    get_call_cost,
    get_call_costs,
    price_call,
    save_call_cost,
)


@pytest.fixture(autouse=True)
def _reset():
    call_costs.reset_for_tests()
    yield
    call_costs.reset_for_tests()


@pytest.fixture
def settings(tmp_path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "pincer.db")
    cfg.price_twilio_outbound_per_min = 0.10
    cfg.price_twilio_inbound_per_min = 0.01
    cfg.price_conversationrelay_per_min = 0.06
    cfg.price_deepgram_per_min = 0.006
    cfg.price_elevenlabs_per_1k_chars = 0.05
    return cfg


# ── LLM attribution ──────────────────────────────────────────────────


def test_attribution_outside_a_call_is_a_noop():
    """Ordinary chat traffic must never land on a call's bill."""
    attribute_llm_cost(1000, 500, 0.42)
    assert current_call_sid() == ""
    assert call_costs.end_call("anything").cost_usd == 0.0


def test_attribution_inside_a_call_context():
    begin_call("CA1")
    with call_context("CA1"):
        attribute_llm_cost(1000, 500, 0.02)
        attribute_llm_cost(800, 300, 0.01)
    usage = call_costs.end_call("CA1")
    assert usage.input_tokens == 1800
    assert usage.output_tokens == 800
    assert usage.cost_usd == pytest.approx(0.03)


def test_two_calls_do_not_cross_contaminate():
    begin_call("CA1")
    begin_call("CA2")
    with call_context("CA1"):
        attribute_llm_cost(100, 50, 0.01)
    with call_context("CA2"):
        attribute_llm_cost(200, 100, 0.02)
    assert call_costs.end_call("CA1").cost_usd == pytest.approx(0.01)
    assert call_costs.end_call("CA2").cost_usd == pytest.approx(0.02)


def test_context_is_restored_after_the_turn():
    with call_context("CA1"):
        assert current_call_sid() == "CA1"
    assert current_call_sid() == ""


async def test_binding_propagates_into_a_child_task():
    """The streaming turn runs as its own task created inside the context."""
    begin_call("CA1")

    async def _child() -> None:
        attribute_llm_cost(500, 250, 0.05)

    with call_context("CA1"):
        await asyncio.create_task(_child())

    assert call_costs.end_call("CA1").cost_usd == pytest.approx(0.05)


async def test_a_task_created_outside_the_context_is_not_billed():
    begin_call("CA1")

    async def _unrelated() -> None:
        attribute_llm_cost(9999, 9999, 9.99)

    await asyncio.create_task(_unrelated())
    assert call_costs.end_call("CA1").cost_usd == 0.0


def test_accumulator_map_stays_bounded():
    for i in range(call_costs._MAX_TRACKED_CALLS + 50):
        begin_call(f"CA{i}")
    assert len(call_costs._usage) <= call_costs._MAX_TRACKED_CALLS


# ── Pricing ──────────────────────────────────────────────────────────


def test_conversation_relay_bundles_stt_and_tts(settings):
    """CR's per-minute add-on covers speech; billing Deepgram/ElevenLabs on top
    would double-count every DACH call."""
    begin_call("CA1")
    with call_context("CA1"):
        attribute_llm_cost(1000, 500, 0.02)

    cost = price_call(
        settings,
        call_sid="CA1",
        direction="outbound",
        engine="conversation_relay",
        language="de",
        duration_seconds=120,
        tts_characters=5000,
    )
    assert cost.stt_usd == 0.0
    assert cost.tts_usd == 0.0
    assert cost.tts_characters == 0
    # 2 min × (0.10 PSTN + 0.06 relay)
    assert cost.twilio_usd == pytest.approx(0.32)
    assert cost.llm_usd == pytest.approx(0.02)
    assert cost.total_usd == pytest.approx(0.34)


def test_media_streams_prices_stt_and_tts_separately(settings):
    begin_call("CA2")
    add_stt_seconds("CA2", 120.0)
    cost = price_call(
        settings,
        call_sid="CA2",
        direction="outbound",
        engine="media_streams",
        language="en",
        duration_seconds=120,
        tts_characters=2000,
    )
    assert cost.twilio_usd == pytest.approx(0.20)  # no relay add-on
    assert cost.stt_usd == pytest.approx(0.012)  # 2 min × 0.006
    assert cost.tts_usd == pytest.approx(0.10)  # 2000 chars × 0.05/1k


def test_stt_seconds_fall_back_to_call_duration(settings):
    """The stream may never report seconds; duration is the honest upper bound."""
    begin_call("CA3")
    cost = price_call(
        settings, call_sid="CA3", direction="inbound", engine="media_streams", language="en", duration_seconds=60
    )
    assert cost.stt_seconds == pytest.approx(60.0)


def test_inbound_uses_the_inbound_rate(settings):
    begin_call("CA4")
    cost = price_call(
        settings, call_sid="CA4", direction="inbound", engine="conversation_relay", language="en", duration_seconds=60
    )
    assert cost.twilio_usd == pytest.approx(0.07)  # 1 min × (0.01 + 0.06)


def test_zero_duration_call_costs_nothing_but_llm(settings):
    begin_call("CA5")
    with call_context("CA5"):
        attribute_llm_cost(10, 5, 0.001)
    cost = price_call(
        settings, call_sid="CA5", direction="outbound", engine="conversation_relay", language="en", duration_seconds=0
    )
    assert cost.twilio_usd == 0.0
    assert cost.total_usd == pytest.approx(0.001)


def test_pricing_consumes_the_accumulator(settings):
    """A second price_call for the same SID must not double-bill the LLM."""
    begin_call("CA6")
    with call_context("CA6"):
        attribute_llm_cost(1000, 500, 0.05)
    first = price_call(
        settings, call_sid="CA6", direction="outbound", engine="conversation_relay", language="en", duration_seconds=60
    )
    second = price_call(
        settings, call_sid="CA6", direction="outbound", engine="conversation_relay", language="en", duration_seconds=60
    )
    assert first.llm_usd == pytest.approx(0.05)
    assert second.llm_usd == 0.0


def test_components_sum_to_the_total():
    cost = CallCost(call_sid="CA", twilio_usd=0.10, stt_usd=0.01, tts_usd=0.02, llm_usd=0.03)
    assert sum(cost.components().values()) == pytest.approx(cost.total_usd)


# ── Persistence ──────────────────────────────────────────────────────


async def test_save_and_read_back(settings):
    cost = CallCost(
        call_sid="CA_save",
        direction="outbound",
        engine="conversation_relay",
        language="de",
        duration_seconds=90,
        twilio_usd=0.24,
        llm_usd=0.03,
    )
    await save_call_cost(settings, cost)

    stored = await get_call_cost(settings, "CA_save")
    assert stored is not None
    assert stored["total_usd"] == pytest.approx(0.27)
    assert stored["language"] == "de"


async def test_save_is_idempotent(settings):
    cost = CallCost(call_sid="CA_dup", twilio_usd=0.10)
    await save_call_cost(settings, cost)
    await save_call_cost(settings, cost)
    stored = await get_call_cost(settings, "CA_dup")
    assert stored["total_usd"] == pytest.approx(0.10)


async def test_batch_lookup_is_one_query(settings):
    for i in range(3):
        await save_call_cost(settings, CallCost(call_sid=f"CA{i}", twilio_usd=0.1 * (i + 1)))
    costs = await get_call_costs(settings, ["CA0", "CA1", "CA2", "CA_missing"])
    assert costs == pytest.approx({"CA0": 0.1, "CA1": 0.2, "CA2": 0.3})


async def test_missing_cost_is_none_not_an_error(settings):
    assert await get_call_cost(settings, "CA_never_existed") is None


async def test_batch_lookup_of_nothing(settings):
    assert await get_call_costs(settings, []) == {}
