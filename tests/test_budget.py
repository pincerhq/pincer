"""Tests for the budget enforcer."""

import pytest

from pincer.costs.budget import BudgetEnforcer, BudgetExhausted


@pytest.mark.asyncio
async def test_check_budget_allows_within_limit():
    enforcer = BudgetEnforcer(daily_limit_usd=5.0)
    await enforcer.check_budget("u1", estimated_cost=1.0)


@pytest.mark.asyncio
async def test_check_budget_blocks_over_limit():
    enforcer = BudgetEnforcer(daily_limit_usd=1.0)
    await enforcer.record_cost("u1", 0.90)
    with pytest.raises(BudgetExhausted):
        await enforcer.check_budget("u1", estimated_cost=0.20)


@pytest.mark.asyncio
async def test_conversation_budget():
    enforcer = BudgetEnforcer(
        daily_limit_usd=100.0,
        conversation_limit_usd=0.50,
    )
    await enforcer.record_cost("u1", 0.45, conversation_id="conv1")
    with pytest.raises(BudgetExhausted):
        await enforcer.check_budget("u1", estimated_cost=0.10, conversation_id="conv1")


@pytest.mark.asyncio
async def test_auto_downgrade():
    enforcer = BudgetEnforcer(
        daily_limit_usd=10.0,
        auto_downgrade_threshold_pct=0.70,
    )
    status = await enforcer.record_cost("u1", 7.50)
    assert status.is_downgraded is True


@pytest.mark.asyncio
async def test_model_downgrade():
    enforcer = BudgetEnforcer(
        daily_limit_usd=10.0,
        auto_downgrade_threshold_pct=0.70,
    )
    await enforcer.record_cost("u1", 8.0)
    model = await enforcer.get_model_for_budget(
        "u1", "claude-sonnet-4-5-20250929"
    )
    assert model == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_no_downgrade_under_threshold():
    enforcer = BudgetEnforcer(
        daily_limit_usd=10.0,
        auto_downgrade_threshold_pct=0.70,
    )
    await enforcer.record_cost("u1", 3.0)
    model = await enforcer.get_model_for_budget(
        "u1", "claude-sonnet-4-5-20250929"
    )
    assert model == "claude-sonnet-4-5-20250929"


@pytest.mark.asyncio
async def test_warning_notification():
    notifications = []

    async def on_notify(user_id: str, message: str) -> None:
        notifications.append((user_id, message))

    enforcer = BudgetEnforcer(
        daily_limit_usd=10.0,
        warning_threshold_pct=0.80,
        notify_callback=on_notify,
    )
    await enforcer.record_cost("u1", 8.50)
    assert len(notifications) == 1
    assert notifications[0][0] == "u1"
    assert "85%" in notifications[0][1]


@pytest.mark.asyncio
async def test_warning_sent_only_once():
    notifications = []

    async def on_notify(user_id: str, message: str) -> None:
        notifications.append(message)

    enforcer = BudgetEnforcer(
        daily_limit_usd=10.0,
        warning_threshold_pct=0.80,
        notify_callback=on_notify,
    )
    await enforcer.record_cost("u1", 8.50)
    await enforcer.record_cost("u1", 0.50)
    assert len(notifications) == 1  # Not sent again


@pytest.mark.asyncio
async def test_increase_budget():
    enforcer = BudgetEnforcer(daily_limit_usd=5.0)
    await enforcer.record_cost("u1", 4.0)
    result = await enforcer.increase_budget("u1", 20.0)
    assert "$20.00" in result
    # Budget check should now pass
    await enforcer.check_budget("u1", estimated_cost=10.0)


@pytest.mark.asyncio
async def test_get_status():
    enforcer = BudgetEnforcer(daily_limit_usd=10.0)
    await enforcer.record_cost("u1", 3.0)
    status = await enforcer.get_status("u1")
    assert status["daily_spent_usd"] == 3.0
    assert status["daily_limit_usd"] == 10.0
    assert status["daily_pct"] == 30.0
