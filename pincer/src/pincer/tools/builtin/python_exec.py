"""
Sandboxed Python code execution tool.

Runs user-provided Python code in an isolated subprocess with:
- Configurable timeout (default 30s, max 120s)
- Capped output (8000 chars)
- Temporary working directory
- Matplotlib plot capture (auto-saves figures instead of showing)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_OUTPUT_LENGTH = 8000
MAX_TIMEOUT = 120

# Wrapper injected before user code to capture matplotlib figures
_MATPLOTLIB_WRAPPER = '''
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

'''

_MATPLOTLIB_FOOTER = '''

# Auto-save any unsaved matplotlib figures
try:
    import matplotlib.pyplot as _plt2
    if _plt2.get_fignums():
        _patched_show()
except (ImportError, NameError):
    pass
'''


async def python_exec(code: str, timeout: int = 30) -> str:
    """
    Execute Python code in an isolated subprocess and return the output.

    code: The Python code to execute
    timeout: Execution timeout in seconds (default 30, max 120)
    """
    timeout = min(max(timeout, 1), MAX_TIMEOUT)

    with tempfile.TemporaryDirectory(prefix="pincer_exec_") as tmpdir:
        plot_dir = os.path.join(tmpdir, "plots")
        os.makedirs(plot_dir, exist_ok=True)

        full_code = _MATPLOTLIB_WRAPPER + code + _MATPLOTLIB_FOOTER
        script_path = os.path.join(tmpdir, "script.py")
        with open(script_path, "w") as f:
            f.write(full_code)

        env = os.environ.copy()
        env["_PINCER_PLOT_DIR"] = plot_dir
        # Minimal isolation: restrict some dangerous env vars
        env.pop("AWS_ACCESS_KEY_ID", None)
        env.pop("AWS_SECRET_ACCESS_KEY", None)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env=env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Error: Code execution timed out after {timeout} seconds."

        except Exception as e:
            return f"Error starting subprocess: {type(e).__name__}: {e}"

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")

        output_parts: list[str] = []
        if stdout.strip():
            output_parts.append(stdout.strip())
        if stderr.strip():
            output_parts.append(f"[stderr]\n{stderr.strip()}")

        # Check for generated plot files
        plot_files = list(Path(plot_dir).glob("*.png"))
        if plot_files:
            # Copy plots to workspace for persistence
            workspace = Path.home() / ".pincer" / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            for pf in plot_files:
                dest = workspace / pf.name
                dest.write_bytes(pf.read_bytes())
                output_parts.append(f"[Plot saved to: {dest}]")

        if proc.returncode != 0 and not stderr.strip():
            output_parts.append(f"[Process exited with code {proc.returncode}]")

        result = "\n".join(output_parts) if output_parts else "(no output)"

        if len(result) > MAX_OUTPUT_LENGTH:
            result = result[:MAX_OUTPUT_LENGTH] + "\n...[output truncated]"

        return result
