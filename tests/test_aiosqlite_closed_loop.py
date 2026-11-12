"""The aiosqlite worker thread patched in conftest.py, when a test's loop closes under it."""

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import aiosqlite
import aiosqlite.core as aiosqlite_core
import conftest


def _run_worker(*items: tuple[Any, Any]) -> threading.Thread:
    """Feed `items` to a worker thread, then stop it, and wait for it to exit."""
    tx = conftest._CountingTxQueue()
    for item in items:
        tx.put_nowait(item)
    tx.put_nowait((None, lambda: aiosqlite_core._STOP_RUNNING_SENTINEL))
    thread = threading.Thread(target=conftest._counting_worker_thread, args=(tx,))
    thread.start()
    thread.join(timeout=5)
    return thread


def _closed_loop_future(*, cancelled: bool) -> asyncio.Future[Any]:
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    if cancelled:
        future.cancel()
    loop.close()
    return future


def test_result_for_a_cancelled_query_is_dropped() -> None:
    future = _closed_loop_future(cancelled=True)

    thread = _run_worker((future, lambda: 1))

    assert not thread.is_alive()  # it went on to the stop item
    assert not conftest._aiosqlite_late
    assert not conftest._aiosqlite_in_flight.get(future.get_loop())


def test_exception_for_a_cancelled_query_is_dropped() -> None:
    def fails() -> None:
        raise ValueError("boom")

    future = _closed_loop_future(cancelled=True)

    thread = _run_worker((future, fails))

    assert not thread.is_alive()
    assert not conftest._aiosqlite_late


def test_stop_nobody_awaits_is_dropped() -> None:
    """What SQLAlchemy's terminate() queues when it closes a cancelled checkout."""
    future = _closed_loop_future(cancelled=False)
    tx = conftest._CountingTxQueue()
    tx.put_nowait((future, lambda: aiosqlite_core._STOP_RUNNING_SENTINEL))
    thread = threading.Thread(target=conftest._counting_worker_thread, args=(tx,))
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert not conftest._aiosqlite_late


def test_result_a_test_still_awaits_is_reported_with_its_test(request: Any) -> None:
    future = _closed_loop_future(cancelled=False)

    thread = _run_worker((future, lambda: 1))

    assert not thread.is_alive()  # reported, but the thread survives
    late = conftest._aiosqlite_late[:]
    conftest._aiosqlite_late.clear()  # or this test's teardown would raise it
    assert len(late) == 1
    assert late[0].startswith(f"{request.node.nodeid}: ")


def test_query_cancelled_as_the_loop_closes(tmp_path: Path) -> None:
    """The CI flake: a task cancelled mid-query, then its loop closed while the
    worker thread is still running the query."""
    started = threading.Event()

    def slow(x: int) -> int:
        started.set()
        time.sleep(0.3)
        return x

    runner = asyncio.Runner()
    conn = runner.run(aiosqlite.connect(tmp_path / "t.db"))

    async def query() -> None:
        await conn.create_function("slow", 1, slow)
        await conn.execute("SELECT slow(1)")

    async def cancel_mid_query() -> None:
        task = asyncio.create_task(query())
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()

    runner.run(cancel_mid_query())
    runner.close()  # the query is still sleeping on the worker thread

    conn.stop()  # no loop now: a stop nobody awaits
    conn._thread.join(timeout=5)

    assert not conn._thread.is_alive()
    assert not conftest._aiosqlite_late
