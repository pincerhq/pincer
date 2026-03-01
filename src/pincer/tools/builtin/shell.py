"""
Shell command execution tool with safety controls.

- Blocked command patterns
- Timeout enforcement
- Output truncation
- Optional approval flow
"""

from __future__ import annotations

import asyncio
import logging
import re

from pincer.config import get_settings
from pincer.exceptions import ShellBlockedError

logger = logging.getLogger(__name__)

# ── Dangerous patterns (regex) ───────────────────────────
BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+(-\S+\s+)*/"),  # rm -rf /
    re.compile(r"\bdd\s+if="),  # dd if=
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),  # fork bomb
    re.compile(r"\bmkfs\b"),  # mkfs
    re.compile(r"\bformat\b"),  # format
    re.compile(r">\s*/dev/sd[a-z]"),  # write to disk
    re.compile(r"\bchmod\s+777\s+/"),  # chmod 777 /
    re.compile(r"\bshutdown\b"),  # shutdown
    re.compile(r"\breboot\b"),  # reboot
    re.compile(r"\bcurl\b.*\|\s*(ba)?sh"),  # curl | sh
    re.compile(r"\bwget\b.*\|\s*(ba)?sh"),  # wget | sh
]


def is_blocked(command: str) -> str | None:
    """Check if command matches any blocked pattern. Returns reason or None."""
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(command):
            return f"Blocked: matches dangerous pattern '{pattern.pattern}'"
    return None


async def shell_exec(command: str, workdir: str = "~") -> str:
    """
    Execute a shell command and return its output.

    command: The shell command to run
    workdir: Working directory (default: home)
    """
    settings = get_settings()

    if not settings.shell_enabled:
        return "Error: Shell execution is disabled in configuration."

    reason = is_blocked(command)
    if reason:
        raise ShellBlockedError(reason)

    timeout = settings.shell_timeout
    expanded_workdir = workdir.replace("~", str(settings.data_dir.parent))

    logger.info("Shell exec: %r (cwd=%s, timeout=%ds)", command, expanded_workdir, timeout)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=expanded_workdir,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()  # type: ignore[union-attr]
        return f"Error: Command timed out after {timeout} seconds."
    except Exception as e:
        return f"Error: Failed to execute command: {e}"

    output_parts: list[str] = []
    if stdout:
        decoded = stdout.decode(errors="replace").strip()
        if decoded:
            output_parts.append(f"STDOUT:\n{decoded}")
    if stderr:
        decoded = stderr.decode(errors="replace").strip()
        if decoded:
            output_parts.append(f"STDERR:\n{decoded}")

    output_parts.append(f"Exit code: {proc.returncode}")

    result = "\n\n".join(output_parts)

    max_len = 4000
    if len(result) > max_len:
        result = result[:max_len] + f"\n...[output truncated at {max_len} chars]"

    return result
