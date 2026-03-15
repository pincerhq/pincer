"""
Skill sandboxing — execute untrusted skill functions in isolated subprocesses.

Security layers:
  1. Resource limits (RLIMIT_AS, RLIMIT_CPU) via the resource module
  2. Network domain whitelisting via socket.getaddrinfo monkey-patch
  3. Environment variable isolation (only declared vars passed through)
  4. Timeout enforcement via asyncio.wait_for + SIGKILL
  5. Filesystem isolation (HOME set to temp directory)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SandboxConfig:
    """Configuration for sandboxed execution."""

    timeout: int = 30
    max_memory_bytes: int = 256 * 1024 * 1024  # 256 MB
    max_cpu_seconds: int = 10
    allowed_domains: list[str] = field(default_factory=list)
    allowed_env_vars: list[str] = field(default_factory=list)
    skill_dir: str = ""
    enable_network: bool = True


@dataclass
class SandboxResult:
    """Result of a sandboxed execution."""

    success: bool
    result: Any = None
    error: str | None = None
    stderr: str | None = None
    exit_code: int = 0
    timed_out: bool = False
    execution_time: float = 0.0


def _build_runner_script(
    skill_path: str,
    function_name: str,
    arguments: dict[str, Any],
    config: SandboxConfig,
) -> str:
    """Build a self-contained Python script that executes the skill function."""
    args_json = json.dumps(arguments)
    domains_json = json.dumps(config.allowed_domains)

    return textwrap.dedent(f"""\
        import json
        import sys
        import os

        # Resource limits
        try:
            import resource
            mem = {config.max_memory_bytes}
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        except (ImportError, ValueError, OSError):
            pass
        try:
            import resource
            cpu = {config.max_cpu_seconds}
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
        except (ImportError, ValueError, OSError):
            pass

        # Network domain whitelisting
        allowed_domains = {domains_json}
        if allowed_domains:
            import socket
            _original_getaddrinfo = socket.getaddrinfo
            def _filtered_getaddrinfo(host, *args, **kwargs):
                if host in ("localhost", "127.0.0.1", "::1"):
                    return _original_getaddrinfo(host, *args, **kwargs)
                ok = False
                for d in allowed_domains:
                    if host == d or host.endswith("." + d):
                        ok = True
                        break
                if not ok:
                    raise PermissionError(f"Network access to '{{host}}' blocked by sandbox")
                return _original_getaddrinfo(host, *args, **kwargs)
            socket.getaddrinfo = _filtered_getaddrinfo

        # Import and execute
        try:
            skill_path = {skill_path!r}
            sys.path.insert(0, str(os.path.dirname(skill_path)))

            import importlib.util
            spec = importlib.util.spec_from_file_location("_sandbox_skill", skill_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            fn = getattr(module, {function_name!r}, None)
            if fn is None:
                msg = "Function '{{}}' not found".format({function_name!r})
                print(json.dumps({{"error": msg}}))
                sys.exit(1)

            args = json.loads({args_json!r})
            import asyncio
            import inspect
            if inspect.iscoroutinefunction(fn):
                result = asyncio.run(fn(**args))
            else:
                result = fn(**args)

            print(json.dumps({{"result": result}}))
        except Exception as e:
            print(json.dumps({{"error": f"{{type(e).__name__}}: {{e}}"}}))
            sys.exit(1)
    """)


def _build_env(config: SandboxConfig, temp_dir: str) -> dict[str, str]:
    """Build a restricted environment for the subprocess."""
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": temp_dir,
        "LANG": "en_US.UTF-8",
    }
    pythonpath = os.environ.get("PYTHONPATH")
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    for var in config.allowed_env_vars:
        val = os.environ.get(var)
        if val is not None:
            env[var] = val
    return env


async def execute(
    skill_path: str,
    function_name: str,
    arguments: dict[str, Any],
    config: SandboxConfig | None = None,
) -> SandboxResult:
    """Execute a skill function in a sandboxed subprocess."""
    if config is None:
        config = SandboxConfig()

    start_time = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="pincer_sandbox_") as temp_dir:
        script = _build_runner_script(skill_path, function_name, arguments, config)
        runner_path = Path(temp_dir) / "_runner.py"
        runner_path.write_text(script, encoding="utf-8")

        env = _build_env(config, temp_dir)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(runner_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=temp_dir,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=config.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return SandboxResult(
                success=False,
                error="Execution timed out",
                exit_code=-1,
                timed_out=True,
                execution_time=time.monotonic() - start_time,
            )

        execution_time = time.monotonic() - start_time
        stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()
        exit_code = proc.returncode or 0

        if exit_code != 0:
            error = stderr_str or stdout_str or f"Process exited with code {exit_code}"
            return SandboxResult(
                success=False,
                error=error,
                stderr=stderr_str or None,
                exit_code=exit_code,
                execution_time=execution_time,
            )

        try:
            data = json.loads(stdout_str)
        except json.JSONDecodeError:
            return SandboxResult(
                success=False,
                error=f"Invalid JSON output: {stdout_str[:500]}",
                stderr=stderr_str or None,
                exit_code=exit_code,
                execution_time=execution_time,
            )

        if "error" in data:
            return SandboxResult(
                success=False,
                error=data["error"],
                stderr=stderr_str or None,
                exit_code=exit_code,
                execution_time=execution_time,
            )

        return SandboxResult(
            success=True,
            result=data.get("result"),
            stderr=stderr_str or None,
            exit_code=0,
            execution_time=execution_time,
        )
