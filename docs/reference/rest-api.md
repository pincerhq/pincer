# 📚 API Reference

Pincer exposes a REST API via its dashboard server for monitoring and management.

---

## Base URL

```
http://localhost:8080/api
```

All endpoints require the dashboard auth token:

```
Authorization: Bearer <PINCER_DASHBOARD_TOKEN>
```

---

## Health

### `GET /health`

No auth required. Returns agent status.

```json
{
  "status": "ok",
  "version": "0.7.0",
  "uptime_seconds": 86400,
  "channels": {
    "telegram": "connected",
    "whatsapp": "connected",
    "discord": "disconnected"
  },
  "budget": {
    "daily_limit": 5.0,
    "spent_today": 1.58,
    "remaining": 3.42
  }
}
```

---

## Costs

### `GET /api/costs/today`

Today's spending breakdown.

```json
{
  "date": "2026-02-26",
  "total_usd": 1.58,
  "by_model": {
    "claude-sonnet-4-20250514": 1.20,
    "claude-haiku-4-5-20251001": 0.38
  },
  "by_tool": {
    "web_search": 0.45,
    "gmail_read": 0.32,
    "python_exec": 0.81
  },
  "by_channel": {
    "telegram": 1.10,
    "whatsapp": 0.48
  },
  "total_tokens": {
    "input": 45200,
    "output": 12800
  }
}
```

### `GET /api/costs/history?days=30`

Daily spending for the last N days.

```json
{
  "days": [
    {"date": "2026-02-26", "total_usd": 1.58},
    {"date": "2026-02-25", "total_usd": 3.21},
    ...
  ]
}
```

### `GET /api/costs/by-tool?period=7d`

Spending breakdown by tool.

### `GET /api/costs/by-model?period=7d`

Spending breakdown by model.

---

## Conversations

### `GET /api/conversations?limit=20&offset=0`

List recent conversations.

```json
{
  "conversations": [
    {
      "id": "conv_abc123",
      "user_id": "123456789",
      "channel": "telegram",
      "message_count": 15,
      "last_message_at": "2026-02-26T10:30:00Z",
      "cost_usd": 0.12,
      "summary": "Discussed rescheduling dentist appointment"
    }
  ],
  "total": 342,
  "limit": 20,
  "offset": 0
}
```

### `GET /api/conversations/:id`

Full conversation with messages.

### `DELETE /api/conversations/:id`

Delete a conversation and its memory.

---

## Memory

### `GET /api/memory/stats`

```json
{
  "total_conversations": 342,
  "total_messages": 8451,
  "total_entities": 156,
  "total_summaries": 89,
  "database_size_mb": 24.5
}
```

### `GET /api/memory/search?q=dentist&limit=5`

Search memory by keyword (uses FTS5).

### `GET /api/memory/entities?type=person`

List extracted entities. Types: `person`, `place`, `project`, `organization`.

---

## Skills

### `GET /api/skills`

List installed skills.

```json
{
  "skills": [
    {
      "name": "weather",
      "version": "1.0.0",
      "author": "pincerhq",
      "tools": ["get_weather", "get_forecast"],
      "signed": true,
      "status": "active"
    }
  ]
}
```

### `POST /api/skills/scan`

Scan a skill for security issues. Body: `{"path": "/path/to/skill"}`.

### `DELETE /api/skills/:name`

Remove a skill.

---

## Audit

### `GET /api/audit?limit=50&tool=shell_exec&after=2026-02-25`

Query audit log with filters.

```json
{
  "events": [
    {
      "timestamp": "2026-02-26T10:30:15Z",
      "event": "tool_call",
      "user_id": "123456789",
      "channel": "telegram",
      "tool": "shell_exec",
      "approved": true,
      "duration_ms": 342
    }
  ]
}
```

---

## Voice (Sprint 7)

### `GET /api/voice/calls?limit=20`

List recent voice calls.

### `GET /api/voice/calls/:id`

Get call details including transcript.

### `GET /api/voice/calls/:id/transcript`

Get just the transcript.

---

## WebSocket

### `WS /ws`

Real-time event stream. Connect for live updates:

```javascript
const ws = new WebSocket("ws://localhost:8080/ws?token=YOUR_TOKEN");
ws.onmessage = (e) => {
  const event = JSON.parse(e.data);
  // event.type: "message", "tool_call", "cost_update", "channel_status"
};
```