"""Repid actors — durable, retryable execution for scheduled and on-request work.

Repid has no built-in retry policy, so each actor wraps its handler call in
a `tenacity` retry (bounded by `settings.task_max_retries`). `on_error="nack"`
below is the second layer: once tenacity's retries are exhausted and the actor
re-raises, repid nacks the message — this acks and discards it (the Redis
broker additionally writes it once to a `repid:{channel}:dlq` stream by
default), it does *not* redeliver. repid's `reject()` is the requeue action;
it's unused here.
"""

from __future__ import annotations

import logging
from typing import Any

from tenacity import before_sleep_log, retry, stop_after_attempt, wait_exponential

from pincer.channels.base import ChannelType
from pincer.config import get_settings
from pincer.scheduler.cron import CronScheduler
from pincer.tasks.app import router
from pincer.tasks.context import get_deliverer, get_proactive, get_triggers

logger = logging.getLogger(__name__)


# Retries wrap the entire handler call below, not just a narrow I/O call — a
# transient failure after a handler's side effect (e.g. sending a briefing)
# but before it returns will re-run that side effect on the next attempt.
# Narrowing retry scope to the specific flaky call would mean threading it
# through each handler in `proactive.py`; left as a follow-up, not done here.
def _retrying() -> Any:
    settings = get_settings()
    return retry(
        stop=stop_after_attempt(max(1, settings.task_max_retries)),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


@router.actor(confirmation_mode="auto", on_error="nack")
async def run_scheduled_action(schedule_id: int) -> None:
    """Execute a due cron schedule (briefing, custom action, ...)."""
    logger.info("Scheduled action starting: schedule_id=%s", schedule_id)
    settings = get_settings()
    store = CronScheduler(settings.db_path)
    schedule = await store.get(schedule_id)
    if schedule is None:
        logger.warning("Scheduled action skipped: schedule %s no longer exists", schedule_id)
        return

    proactive = get_proactive()
    action_type = schedule.action.get("type", "custom")
    handlers = {
        "briefing": proactive.generate_briefing,
        "custom": proactive.run_custom_action,
    }
    handler = handlers.get(action_type)
    if handler is None:
        logger.warning("No handler for action type: %s (schedule_id=%s)", action_type, schedule_id)
        return

    try:
        result = await _retrying()(handler)(
            pincer_user_id=schedule.pincer_user_id,
            action=schedule.action,
            channel=schedule.channel,
        )
    except Exception:
        logger.exception(
            "Scheduled action failed after retries: schedule_id=%s name=%s type=%s",
            schedule_id,
            schedule.name,
            action_type,
        )
        raise

    if result and isinstance(result, str):
        delivered = await get_deliverer().send_to_user(
            schedule.pincer_user_id,
            result,
            prefer=ChannelType(schedule.channel),
        )
        if delivered:
            logger.info("Scheduled action delivered: schedule_id=%s user=%s", schedule_id, schedule.pincer_user_id)
        else:
            logger.error(
                "Scheduled action NOT delivered (no reachable channel for user): schedule_id=%s user=%s",
                schedule_id,
                schedule.pincer_user_id,
            )
    else:
        logger.info("Scheduled action completed with no message to send: schedule_id=%s", schedule_id)


@router.actor(confirmation_mode="auto", on_error="nack")
async def process_webhook(webhook_id: str, payload: dict[str, Any], pincer_user_id: str) -> None:
    """Deliver a webhook-triggered notification (on-request background execution).

    Not yet wired to a production trigger — no HTTP route or dispatcher
    enqueues this actor today; it's exercised only by tests. It exists as the
    target for a future webhook-intake endpoint.
    """
    logger.info("Webhook processing starting: webhook_id=%s user=%s", webhook_id, pincer_user_id)
    try:
        await _retrying()(get_triggers().handle_webhook)(webhook_id, payload, pincer_user_id)
    except Exception:
        logger.exception("Webhook processing failed after retries: webhook_id=%s", webhook_id)
        raise
    logger.info("Webhook processed: webhook_id=%s user=%s", webhook_id, pincer_user_id)
