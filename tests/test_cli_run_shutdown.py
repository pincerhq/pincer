"""A real `pincer run`, stopped by a real signal, must finish its whole graceful shutdown.

The agent runs in a subprocess, isolated from the developer's own config: a scratch HOME,
cwd and data dir, a dummy API key, and a Signal channel in poll mode aimed at a local HTTP
stub (an agent with no channel exits before it starts its API server, and the API server
is part of what is being tested here).
"""

from __future__ import annotations

import http.server
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

STARTUP_TIMEOUT = 90.0
SHUTDOWN_TIMEOUT = 60.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def signal_stub():  # type: ignore[no-untyped-def]
    """Answers the Signal channel's health check and polls (with 404s, which it only warns about)."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_signal_runs_the_whole_graceful_shutdown(tmp_path: Path, signal_stub: str, sig: signal.Signals) -> None:
    home, cwd, data = tmp_path / "home", tmp_path / "cwd", tmp_path / "data"
    for path in (home, cwd, data):
        path.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
        "HF_HUB_OFFLINE": "1",
        "PINCER_DATA_DIR": str(data),
        "PINCER_ANTHROPIC_API_KEY": "sk-ant-test-dummy",
        "PINCER_DASHBOARD_HOST": "127.0.0.1",
        "PINCER_DASHBOARD_PORT": str(_free_port()),
        "PINCER_LOG_LEVEL": "INFO",
        "PINCER_SIGNAL_ENABLED": "true",
        "PINCER_SIGNAL_API_URL": signal_stub,
        "PINCER_SIGNAL_PHONE_NUMBER": "+15550000000",
        "PINCER_SIGNAL_RECEIVE_MODE": "poll",
    }
    out_path = tmp_path / "pincer.out"
    with out_path.open("wb") as out:
        proc = subprocess.Popen(
            [sys.executable, "-P", "-m", "pincer", "run"], cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT
        )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while "is running!" not in out_path.read_text(errors="replace"):
            assert proc.poll() is None, out_path.read_text(errors="replace")
            assert time.monotonic() < deadline, "pincer never started:\n" + out_path.read_text(errors="replace")
            time.sleep(0.2)

        proc.send_signal(sig)
        exit_code = proc.wait(timeout=SHUTDOWN_TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    output = out_path.read_text(errors="replace")
    assert "Shutting down..." in output
    # The channels are stopped near the end of the `finally`, the banner is its last line.
    assert "SignalChannel stopped" in output, output
    assert "Shutdown complete" in output, output
    assert "Traceback" not in output, output
    assert exit_code == 0
