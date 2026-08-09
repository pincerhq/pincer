"""Tests for pincer.tasks.context module-level task runtime context."""

import pytest

from pincer.tasks import context


@pytest.fixture(autouse=True)
def _reset_context():
    """Isolate each test from context set by other tests/actors."""
    prev = (context._deliverer, context._proactive, context._triggers)
    context._deliverer = None
    context._proactive = None
    context._triggers = None
    yield
    context._deliverer, context._proactive, context._triggers = prev


def test_get_deliverer_before_init_raises():
    with pytest.raises(RuntimeError, match="Task context not initialized"):
        context.get_deliverer()


def test_get_proactive_before_init_raises():
    with pytest.raises(RuntimeError, match="Task context not initialized"):
        context.get_proactive()


def test_get_triggers_before_init_raises():
    with pytest.raises(RuntimeError, match="Task context not initialized"):
        context.get_triggers()


def test_set_context_makes_getters_return_values():
    deliverer, proactive, triggers = object(), object(), object()

    context.set_context(deliverer, proactive, triggers)

    assert context.get_deliverer() is deliverer
    assert context.get_proactive() is proactive
    assert context.get_triggers() is triggers
