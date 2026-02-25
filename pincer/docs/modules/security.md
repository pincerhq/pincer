# Security

> **Source**: `src/pincer/security/`

Pincer includes several security layers to protect the agent and its host system. Some are fully implemented; others are scaffolded for future development.

## Security Overview

| Component | Status | Description |
|-----------|--------|-------------|
| **Shell safety** | Implemented | Blocked command patterns in shell_exec |
| **File sandboxing** | Implemented | Files restricted to workspace directory |
| **User allowlist** | Implemented | Telegram user ID restriction |
| **Budget enforcement** | Implemented | Daily cost limits with auto-cutoff |
| **Python isolation** | Implemented | Subprocess execution with stripped credentials |
| **Rate limiter** | Placeholder | `src/pincer/security/rate_limiter.py` |
| **Firewall** | Placeholder | `src/pincer/security/firewall.py` |
| **Audit logging** | Placeholder | `src/pincer/security/audit.py` |
| **Health checks** | Placeholder | `src/pincer/security/doctor.py` |
| **Tool approval** | Placeholder | `src/pincer/tools/approval.py` |
| **Tool sandboxing** | Placeholder | `src/pincer/tools/sandbox.py` |

## Implemented Security Controls

### Shell Command Safety

The `shell_exec` tool blocks dangerous command patterns before execution:

```python
BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+/",          # Recursive root delete
    r"\bdd\s+if=",            # Disk duplication
    r":\(\)\s*\{",            # Fork bomb
    r"\bmkfs\b",              # Filesystem creation
    r"\bformat\b",            # Disk formatting
    r">\s*/dev/sd",           # Direct disk write
    r"chmod\s+777\s+/",      # Insecure root permissions
    r"\b(shutdown|reboot)\b", # System power control
    r"curl.*\|\s*sh",         # Remote code execution
    r"wget.*\|\s*sh",         # Remote code execution
]
```

The tool also enforces:
- **Timeout**: Configurable (default 30s, max 300s)
- **Output truncation**: Max 4000 characters
- **Optional approval**: `PINCER_SHELL_REQUIRE_APPROVAL` flag

### File Sandbox

All file operations are restricted to `~/.pincer/workspace/`:

```python
target = (workspace / path_str).resolve()
if not str(target).startswith(str(workspace.resolve())):
    raise ValueError("Access denied: path is outside workspace")
```

This prevents directory traversal attacks (e.g., `../../etc/passwd`).

### Telegram Allowlist

```bash
PINCER_TELEGRAM_ALLOWED_USERS=12345,67890
```

Only listed user IDs can interact with the bot. Empty list = allow all.

### Budget Enforcement

The `CostTracker` monitors daily spending and raises `BudgetExceededError` when the limit is hit, preventing runaway costs from tool loops or expensive model usage.

### Python Execution Isolation

The `python_exec` tool runs code in a subprocess with:
- Separate process (not in-process eval)
- Timeout enforcement (max 120s)
- AWS credentials stripped from environment
- Separate working directory

## Planned Security Components

### Rate Limiter

> **Source**: `src/pincer/security/rate_limiter.py`

Scaffolded for per-user or per-channel rate limiting. Planned features:
- Configurable request rate per time window
- Token bucket or sliding window algorithm
- Per-user and per-channel limits

### Firewall

> **Source**: `src/pincer/security/firewall.py`

Scaffolded for network-level security. Planned features:
- URL allowlisting for browser and web_search
- Domain blocking
- Outbound connection monitoring

### Audit Logging

> **Source**: `src/pincer/security/audit.py`

Scaffolded for comprehensive event logging. Planned features:
- Log all tool executions with parameters and results
- Log all user interactions
- Log cost events
- Structured JSON log format for analysis

### Health Checks (Doctor)

> **Source**: `src/pincer/security/doctor.py`

Scaffolded for system health monitoring. Planned features:
- API key validity checks
- Database integrity verification
- Memory usage monitoring
- Provider connectivity tests

## Security Best Practices

When deploying Pincer:

1. **Always set `PINCER_TELEGRAM_ALLOWED_USERS`** — restrict to known user IDs
2. **Set a daily budget** — `PINCER_DAILY_BUDGET=5.0` prevents cost overruns
3. **Disable shell if not needed** — `PINCER_SHELL_ENABLED=false`
4. **Require shell approval** — `PINCER_SHELL_REQUIRE_APPROVAL=true`
5. **Use Docker** — Containers add an extra isolation layer
6. **Keep API keys in `.env`** — Never commit secrets to version control
7. **Review workspace regularly** — Check `~/.pincer/workspace/` for unexpected files
