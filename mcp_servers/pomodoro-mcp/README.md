# 🍅 Pomodoro MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for managing Pomodoro sessions, built with Python and [FastMCP](https://github.com/jlowin/fastmcp).

## Tools

| Tool | Description |
|------|-------------|
| `pomodoro_start` | Start a focus session (description, duration, tags) |
| `pomodoro_status` | Check time remaining and current phase |
| `pomodoro_pause` | Pause the active session |
| `pomodoro_resume` | Resume a paused session |
| `pomodoro_finish` | Mark session complete, get break recommendation |
| `pomodoro_cancel` | Cancel and discard the active session |
| `pomodoro_break` | Start a short (5 min) or long (15 min) break |
| `pomodoro_amend` | Update description or tags mid-session |
| `pomodoro_repeat` | Repeat the last completed Pomodoro |
| `pomodoro_history` | List past sessions with optional filters |
| `pomodoro_settings` | Show defaults and today's progress |

## Quick Start

### With Docker Compose (recommended)

```bash
# Production
docker compose up -d

# With MCP Inspector for testing
docker compose --profile dev up
```

The server is available at `http://localhost:8000/mcp`.

### Without Docker

```bash
pip install -r requirements.txt
python server.py
```

### Custom port

```bash
MCP_PORT=9000 docker compose up -d
```

## Connect to Claude Code

```bash
claude mcp add pomodoro --transport http http://localhost:8000/mcp
```

## Connect to Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pomodoro": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

## Example Conversation

```
> Start a 25-minute pomodoro for "writing unit tests"
🍅 Pomodoro started! Focus for 25 minutes.

> What's the status?
Active: writing unit tests — 18:42 remaining

> I'm done, finish it
✅ Session complete! Take a short break (5 min) ☕

> Show my history
3 sessions today · 75 min focus time · goal: 3/8
```

## Notes

- Sessions are stored **in-memory** — they reset when the container restarts.
- Only one session can be active at a time.
- Long breaks are automatically suggested every 4 completed Pomodoros.
