# 🔒 Security Model

Pincer is designed with security as a first-class concern — not an afterthought. This document explains the threat model, security layers, and best practices.

---

## Why Security Matters

AI agents with tool access can read your emails, execute code, browse the web, and manage your calendar. A single vulnerability can expose your entire digital life. Pincer addresses this with defense in depth.

---

## Threat Model

### What We Protect Against

| Threat | Mitigation |
|--------|-----------|
| Unauthorized access | User allowlist — only approved IDs can interact |
| Prompt injection via tools | Tool outputs are sanitized, system prompt is hardened |
| Malicious skills | Sandbox isolation, security scanner, optional skill signing |
| Runaway costs | Hard budget limits, per-session and per-tool caps, auto-downgrade |
| Data exfiltration | No telemetry, all data stays on your machine, audit logging |
| Credential exposure | `SecretStr` for API keys, masked in logs, `.env` not committed |
| Lateral movement | Skills can't access other skills' data, minimal permissions |
| Replay attacks | Session tokens are time-limited, nonces for critical operations |

### What We Don't Protect Against

- A compromised host machine (if your server is rooted, all bets are off)
- Social engineering of the human user (Pincer can't stop you from approving a bad action)
- Vulnerabilities in upstream LLM providers

---

## Security Layers

### Layer 1: User Allowlist

Only users whose IDs are in `PINCER_ALLOWED_USERS` can interact with the agent. All other messages are silently dropped and logged.

```env
# Comma-separated list of authorized user IDs
PINCER_ALLOWED_USERS=123456789,987654321
```

For Telegram, this is your numeric user ID. For WhatsApp, your phone number. For Discord, your user snowflake ID.

The agent will not respond to, acknowledge, or process messages from unauthorized users. There is no "public mode."

### Layer 2: Tool Approval

Dangerous tools require explicit user approval before execution:

```
🔧 Tool: shell_exec
📝 Command: rm -rf /tmp/old_logs
⚠️ This tool can modify your system.

Approve? Reply with ✅ or ❌
```

Tools are classified at registration time:

| Risk Level | Example Tools | Approval Required |
|------------|--------------|-------------------|
| **Safe** | web_search, memory_search, calendar_read | No |
| **Moderate** | gmail_send, calendar_create | Configurable |
| **Dangerous** | shell_exec, file_write, python_exec | Always |

You can override the approval policy per tool in your config:

```env
# Force approval for all tools (paranoid mode)
PINCER_APPROVE_ALL_TOOLS=true

# Skip approval for specific tools
PINCER_SKIP_APPROVAL=gmail_send,calendar_create
```

### Layer 3: Skill Sandboxing

Skills run in isolated subprocess environments with:

- **Resource limits** — CPU time (30s default), memory (256MB default), no disk writes outside `data/`
- **Permission system** — skills must declare what they need (`network`, `file_read`, etc.)
- **Import restrictions** — dangerous modules blocked unless explicitly permitted
- **No cross-skill access** — skills can't read other skills' data or state

### Layer 4: Security Scanner

Before loading any skill, Pincer scans the code for:

- Calls to `os.system()`, `subprocess.run()`, `eval()`, `exec()`
- Import of `socket`, `ctypes`, `importlib` without declared permissions
- Obfuscated or encoded code (base64-encoded strings, char manipulation)
- Network access patterns without `network` permission
- File access outside the sandbox boundary
- Known malicious code signatures

Run it manually:

```bash
pincer skills scan ./suspicious-skill
```

Output:

```
🔍 Scanning: suspicious-skill
  ⚠️  WARNING: Uses subprocess.run() without shell permission
  🔴 CRITICAL: Encoded payload detected in line 47
  ⚠️  WARNING: Accesses /etc/passwd (outside sandbox)
  
  Result: 1 critical, 2 warnings — NOT SAFE TO INSTALL
```

### Layer 5: Skill Signing

For maximum assurance, enable skill signing:

```env
PINCER_REQUIRE_SIGNED_SKILLS=true
```

When enabled, only skills with a valid cryptographic signature from a trusted key are loaded. This prevents tampering and supply-chain attacks.

### Layer 6: Audit Log

Every action is logged to `data/audit.log` in structured JSON format:

```json
{
  "timestamp": "2026-02-26T10:30:15Z",
  "event": "tool_call",
  "user_id": "123456789",
  "channel": "telegram",
  "tool": "shell_exec",
  "input": {"command": "ls -la /home"},
  "output_hash": "sha256:a1b2c3...",
  "approved": true,
  "duration_ms": 342,
  "tokens_used": 0,
  "cost_usd": 0.0
}
```

View audit logs:

```bash
# Recent events
pincer audit tail

# Filter by user
pincer audit search --user 123456789

# Filter by tool
pincer audit search --tool shell_exec

# Filter by time range
pincer audit search --after 2026-02-25 --before 2026-02-26

# Export for analysis
pincer audit export --format csv > audit.csv
```

### Layer 7: Rate Limiting

Protects against abuse and runaway loops:

```env
# Max messages per user per minute
PINCER_RATE_LIMIT_USER=20

# Max messages globally per minute
PINCER_RATE_LIMIT_GLOBAL=100

# Max tool calls per conversation turn
PINCER_MAX_TOOL_CALLS=10
```

### Layer 8: Cost Controls

Prevents surprise bills:

```env
PINCER_BUDGET_DAILY=5.00          # Hard daily limit
PINCER_BUDGET_PER_SESSION=1.00    # Per conversation
PINCER_BUDGET_PER_TOOL=0.50       # Per tool call
PINCER_BUDGET_AUTO_DOWNGRADE=true # Switch to cheaper model when tight
```

At 80% of daily budget, the agent warns you. At 100%, it stops processing until the next day.

---

## Security Doctor

Run a comprehensive security audit:

```bash
pincer doctor
```

This checks 25+ items:

```
🩺 Pincer Security Doctor

Configuration:
  🟢 API keys not in config files (using .env)
  🟢 .env file has restrictive permissions (600)
  🟢 .env is in .gitignore
  🟢 Allowed users list configured (2 users)
  🟡 Budget limit is high ($50/day) — consider lowering

Channels:
  🟢 Telegram bot token is valid
  🟢 WhatsApp session is active
  🟡 Discord bot has admin permissions — consider restricting

Skills:
  🟢 3 skills installed, all pass security scan
  🟡 Skill signing not enforced — consider enabling
  🟢 No unsigned skills found

System:
  🟢 SQLite database encrypted at rest
  🟢 Audit logging enabled
  🟢 Rate limiting configured
  🟢 No world-readable sensitive files
  🟢 Python dependencies up to date

Summary: 22 passed, 3 warnings, 0 critical
```

---

## Best Practices

1. **Keep your allowlist tight** — only add your own user IDs
2. **Enable skill signing** if you install third-party skills
3. **Set a reasonable daily budget** — $5 is plenty for personal use
4. **Run `pincer doctor` weekly** — catch configuration drift early
5. **Review the audit log** — especially after installing new skills
6. **Don't run Pincer as root** — use a dedicated unprivileged user
7. **Keep dependencies updated** — `uv pip install --upgrade pincer-agent`
8. **Use Docker** — provides an additional isolation layer
9. **Back up `data/pincer.db`** — it contains your memories and config
10. **Rotate API keys** periodically — especially if you suspect exposure

---

## Reporting Security Issues

If you find a security vulnerability, please report it responsibly:

- **Email:** security@pincer.dev
- **Do NOT** open a public GitHub issue for security vulnerabilities
- We aim to acknowledge reports within 24 hours and patch critical issues within 72 hours
- We follow coordinated disclosure — we'll credit you in the advisory unless you prefer anonymity
