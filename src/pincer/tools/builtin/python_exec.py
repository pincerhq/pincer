"""
Sandboxed Python code execution tool.

Runs user-provided Python code in an isolated subprocess with:
- Configurable timeout (default 30s, max 120s)
- Capped output (8000 chars)
- The workspace's exec_output/ folder as working directory (new files are uploaded to it)
- Matplotlib plot capture (auto-saves figures instead of showing)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from typing import TYPE_CHECKING

from pincer.tools.sandbox import scrubbed_env

if TYPE_CHECKING:
    from pathlib import Path

    from pincer.storage.workspace import Workspace

logger = logging.getLogger(__name__)

MAX_OUTPUT_LENGTH = 8000
MAX_TIMEOUT = 120

# Wrapper injected before user code to capture matplotlib figures
_MATPLOTLIB_WRAPPER = """
import sys as _sys
import os as _os

# Redirect matplotlib to save figures instead of displaying
_plot_dir = _os.environ.get("_PINCER_PLOT_DIR", ".")
_plot_files = []

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as _plt

    _original_show = _plt.show
    def _patched_show(*args, **kwargs):
        for i, fig_num in enumerate(_plt.get_fignums()):
            fig = _plt.figure(fig_num)
            path = _os.path.join(_plot_dir, f"plot_{i}.png")
            fig.savefig(path, dpi=150, bbox_inches="tight")
            _plot_files.append(path)
            print(f"[Plot saved: {path}]")
        _plt.close("all")
    _plt.show = _patched_show
except ImportError:
    pass

"""

_MATPLOTLIB_FOOTER = """

# Auto-save any unsaved matplotlib figures
try:
    import matplotlib.pyplot as _plt2
    if _plt2.get_fignums():
        _patched_show()
except (ImportError, NameError):
    pass
"""


def _workspace() -> Workspace:
    from pincer.config import get_settings
    from pincer.storage.workspace import get_workspace

    return get_workspace(get_settings())


def _prepare_output_dir() -> tuple[Path, dict[Path, float]]:
    """The workspace's `exec_output/` folder (its local side), synced from the bucket
    first so the script sees files uploaded there, and a snapshot of it. Blocking.

    A sync that fails (bucket unreachable) is logged; the script then runs on the
    cached copy.
    """
    ws = _workspace()
    try:
        ws.sync("exec_output")
    except Exception:
        logger.warning("Could not sync exec_output/ from the workspace; using the cached copy", exc_info=True)
    output_dir = ws.local / "exec_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir, _snapshot(output_dir)


def _snapshot(output_dir: Path) -> dict[Path, float]:
    """Every file under `output_dir` with its mtime."""
    files: dict[Path, float] = {}
    for dirpath, _dirnames, filenames in output_dir.walk():
        for name in filenames:
            path = dirpath / name
            try:
                if path.is_file() and not path.is_symlink():
                    files[path] = path.stat().st_mtime
            except OSError:
                continue
    return files


def _publish_changes(output_dir: Path, before: dict[Path, float]) -> list[Path]:
    """Upload the files the run created or changed under `output_dir`; return the new ones.

    On a local storage root publishing is a no-op. A file that fails to upload
    is logged and still reported: it exists locally.
    """
    after = _snapshot(output_dir)
    changed = [path for path, mtime in after.items() if before.get(path) != mtime]
    ws = _workspace()
    for path in changed:
        try:
            ws.publish(path)
        except Exception:
            logger.warning("Could not upload %s to the workspace", path, exc_info=True)
    return sorted(path for path in changed if path not in before)


async def python_exec(code: str = "", timeout: int = 30) -> str:
    """
    Execute Python code in an isolated subprocess and return the output.

    code: The Python code to execute
    timeout: Execution timeout in seconds (default 30, max 120)
    """
    if not code or not code.strip():
        return "Error: No code provided."

    timeout = min(max(timeout, 1), MAX_TIMEOUT)

    output_dir, existing_files = await asyncio.to_thread(_prepare_output_dir)

    try:
        outcome = await _run(code, timeout, output_dir)
    finally:
        # Upload what the run created or changed, even when it timed out or failed:
        # files it wrote are in the workspace's local copy either way.
        new_files = await asyncio.to_thread(_publish_changes, output_dir, existing_files)
    if isinstance(outcome, str):
        return outcome
    stdout, stderr, returncode = outcome

    output_parts: list[str] = []
    if stdout.strip():
        output_parts.append(stdout.strip())
    if stderr.strip():
        output_parts.append(f"[stderr]\n{stderr.strip()}")
    for nf in new_files:
        output_parts.append(f"[File saved: {nf}]")

    if returncode != 0 and not stderr.strip():
        output_parts.append(f"[Process exited with code {returncode}]")

    result = "\n".join(output_parts) if output_parts else "(no output)"

    if len(result) > MAX_OUTPUT_LENGTH:
        result = result[:MAX_OUTPUT_LENGTH] + "\n...[output truncated]"

    return result


async def _run(code: str, timeout: int, output_dir: Path) -> tuple[str, str, int | None] | str:
    """Run `code` with `output_dir` as working directory: (stdout, stderr, exit code),
    or an error message when it timed out or could not start."""
    with tempfile.TemporaryDirectory(prefix="pincer_exec_") as tmpdir:
        full_code = _MATPLOTLIB_WRAPPER + code + _MATPLOTLIB_FOOTER
        script_path = os.path.join(tmpdir, "script.py")
        with open(script_path, "w") as f:
            f.write(full_code)

        env = scrubbed_env()
        env["_PINCER_PLOT_DIR"] = str(output_dir)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(output_dir),
                env=env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Error: Code execution timed out after {timeout} seconds."

        except Exception as e:
            return f"Error starting subprocess: {type(e).__name__}: {e}"

        return stdout_bytes.decode(errors="replace"), stderr_bytes.decode(errors="replace"), proc.returncode
