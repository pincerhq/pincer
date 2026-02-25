# Cost Tracking & Budget

> **Source**: `src/pincer/llm/cost_tracker.py`

The cost tracking system records every LLM API call, calculates costs based on a built-in pricing table, and enforces configurable daily budgets.

## Class: `CostTracker`

### Constructor

```python
CostTracker(db_path: Path, daily_budget: float = 0.0)
```

- `daily_budget=0.0` means unlimited spending
- `daily_budget=5.0` means $5.00/day limit (default)

### Methods

| Method | Description |
|--------|-------------|
| `initialize()` | Open SQLite, create `cost_log` table |
| `close()` | Close database connection |
| `record(provider, model, input_tokens, output_tokens, session_id)` | Log a cost entry, check budget, return cost |
| `get_today_spend()` | Sum of costs since midnight UTC |
| `get_summary(since_timestamp)` | Aggregated stats (total USD, calls, tokens) |

### Budget Enforcement

On every `record()` call:

```python
if self._daily_budget > 0:
    today_spent = await self.get_today_spend()
    if today_spent + cost > self._daily_budget:
        raise BudgetExceededError(spent=today_spent + cost, limit=self._daily_budget)
```

The `BudgetExceededError` is caught by the agent's ReAct loop, which returns a friendly message to the user:

> "Warning: Daily budget limit reached. Limit: $5.00. I'll be back tomorrow, or you can increase the limit."

## Pricing Table

Costs are calculated per 1M tokens (input, output):

| Model | Input $/1M | Output $/1M |
|-------|-----------|------------|
| `claude-opus-4-6` | $15.00 | $75.00 |
| `claude-opus-4-20250514` | $15.00 | $75.00 |
| `claude-sonnet-4-5-20250929` | $3.00 | $15.00 |
| `claude-sonnet-4-20250514` | $3.00 | $15.00 |
| `claude-haiku-4-5-20251001` | $0.80 | $4.00 |
| `gpt-4o` | $2.50 | $10.00 |
| `gpt-4o-mini` | $0.15 | $0.60 |
| `gpt-4-turbo` | $10.00 | $30.00 |
| `o1` | $15.00 | $60.00 |
| `o1-mini` | $1.10 | $4.40 |
| `o3-mini` | $1.10 | $4.40 |

**Default pricing** (for unrecognized models): $3.00 input / $15.00 output per 1M tokens.

### Cost Calculation

```python
def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    input_rate, output_rate = PRICING.get(model, DEFAULT_PRICING)
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
```

## Database Schema

```sql
CREATE TABLE cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    session_id TEXT
);

CREATE INDEX idx_cost_timestamp ON cost_log(timestamp);
```

## CLI Access

```bash
# View today's costs
pincer cost

# Output:
# Cost Summary
#   Today: $0.1234
#   Total: $2.5678
#   Calls: 42
#   Tokens: 125,000 in / 15,000 out
#   Budget: $5.00/day
```

The `/cost` command is also available in Telegram chat.

## `CostSummary`

```python
@dataclass
class CostSummary:
    total_usd: float
    total_calls: int
    total_input_tokens: int
    total_output_tokens: int
```
