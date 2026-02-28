# 🧩 Skills Guide — Build Custom Pincer Skills

Skills are how you extend Pincer with new capabilities. A skill is a Python module that registers one or more tools the agent can use.

---

## Your First Skill in 2 Minutes

Create a file `skills/hello_world/main.py`:

```python
from pincer.tools import tool

@tool(
    name="hello_world",
    description="Says hello to someone by name",
)
async def hello_world(name: str) -> str:
    return f"Hello, {name}! 🦀"
```

Create `skills/hello_world/skill.yaml`:

```yaml
name: hello_world
version: 0.1.0
description: A simple greeting skill
author: your-name
```

Restart Pincer. The agent now has access to `hello_world`. Ask it: "Say hello to Alice" and it'll call your skill.

---

## Skill Structure

```
skills/
└── my_skill/
    ├── skill.yaml          # Required: metadata
    ├── main.py             # Required: tool definitions
    ├── requirements.txt    # Optional: pip dependencies
    └── README.md           # Optional: documentation
```

### skill.yaml

```yaml
name: stock_tracker
version: 1.0.0
description: Track stock prices and portfolio performance
author: your-github-username
license: MIT

# Permissions this skill needs
permissions:
  - network          # Make HTTP requests
  - file_read        # Read files from data directory

# Dependencies (installed automatically)
dependencies:
  - yfinance>=0.2.0

# Optional: tags for discoverability
tags:
  - finance
  - stocks
  - portfolio
```

### main.py

```python
from pincer.tools import tool
import yfinance as yf

@tool(
    name="get_stock_price",
    description="Get the current price of a stock by its ticker symbol (e.g., AAPL, MSFT, GOOGL)",
    requires_approval=False,
)
async def get_stock_price(ticker: str) -> str:
    """Fetch current stock price."""
    stock = yf.Ticker(ticker.upper())
    info = stock.info
    price = info.get("regularMarketPrice", "N/A")
    change = info.get("regularMarketChangePercent", 0)
    name = info.get("shortName", ticker)
    
    direction = "📈" if change >= 0 else "📉"
    return f"{direction} {name} ({ticker.upper()}): ${price:.2f} ({change:+.2f}%)"


@tool(
    name="get_stock_history",
    description="Get price history for a stock over a time period (1d, 5d, 1mo, 3mo, 6mo, 1y)",
    requires_approval=False,
)
async def get_stock_history(ticker: str, period: str = "1mo") -> str:
    """Fetch stock price history."""
    stock = yf.Ticker(ticker.upper())
    hist = stock.history(period=period)
    
    if hist.empty:
        return f"No data found for {ticker.upper()}"
    
    lines = [f"📊 {ticker.upper()} — Last {period}:"]
    lines.append(f"  Open:  ${hist['Open'].iloc[0]:.2f}")
    lines.append(f"  Close: ${hist['Close'].iloc[-1]:.2f}")
    lines.append(f"  High:  ${hist['High'].max():.2f}")
    lines.append(f"  Low:   ${hist['Low'].min():.2f}")
    
    pct = ((hist['Close'].iloc[-1] - hist['Open'].iloc[0]) / hist['Open'].iloc[0]) * 100
    lines.append(f"  Change: {pct:+.2f}%")
    
    return "\n".join(lines)
```

---

## The `@tool` Decorator

```python
@tool(
    name="tool_name",                 # Unique identifier (snake_case)
    description="What this tool does", # Shown to the LLM — be specific!
    requires_approval=False,          # Ask user before executing?
    timeout=30,                       # Max execution time in seconds
    cost_category="low",              # low / medium / high — affects budget tracking
)
async def my_tool(
    param1: str,                      # Required parameter
    param2: int = 10,                 # Optional with default
    param3: list[str] | None = None,  # Optional nullable
) -> str:                             # Must return a string
    """Docstring is used as extended description."""
    ...
```

### Parameter Types

The decorator auto-generates a JSON schema from your type hints. Supported types:

| Python Type | JSON Schema | Notes |
|-------------|------------|-------|
| `str` | `string` | |
| `int` | `integer` | |
| `float` | `number` | |
| `bool` | `boolean` | |
| `list[str]` | `array` of strings | |
| `dict[str, Any]` | `object` | |
| `Literal["a", "b"]` | `enum` | Restricts values |
| `Optional[str]` or `str \| None` | nullable string | |

### Return Value

Tools must return a `str`. The returned string is injected into the LLM's context as the tool result. Format it for readability — the LLM will summarize it for the user.

---

## Permissions

Skills run in a sandbox by default. To access system resources, declare permissions in `skill.yaml`:

| Permission | What It Allows |
|-----------|---------------|
| `network` | Make outbound HTTP requests |
| `file_read` | Read files in `data/` directory |
| `file_write` | Write files in `data/` directory |
| `shell` | Execute shell commands (requires user approval) |
| `system` | Access system info (CPU, memory, disk) |

Skills without declared permissions can only do pure computation — no I/O, no network, no filesystem.

---

## Skill Lifecycle

1. **Discovery** — Pincer scans the `skills/` directory on startup
2. **Validation** — `skill.yaml` is parsed and validated
3. **Security scan** — code is checked for known dangerous patterns
4. **Dependency install** — `requirements.txt` deps are installed in an isolated environment
5. **Registration** — tools are registered in the tool registry
6. **Execution** — when the LLM calls a tool, it runs in the skill's sandbox

---

## Managing Skills via CLI

```bash
# List installed skills
pincer skills list

# Install a skill from GitHub
pincer skills install github:username/pincer-skill-weather

# Install from a local directory
pincer skills install ./my-skill

# Remove a skill
pincer skills remove weather

# Scan a skill for security issues
pincer skills scan ./untrusted-skill

# Verify a signed skill
pincer skills verify ./skill-directory
```

---

## Skill Security

### Security Scanner

Before installing third-party skills, run:

```bash
pincer skills scan ./downloaded-skill
```

The scanner checks for:
- Import of dangerous modules (`os.system`, `subprocess`, `eval`, `exec`)
- Network access without declared `network` permission
- File access outside the sandbox
- Obfuscated code
- Known malicious patterns

### Skill Signing

Trusted skills can be cryptographically signed:

```bash
# Generate a signing key
pincer skills keygen

# Sign your skill
pincer skills sign ./my-skill --key ~/.pincer/signing_key.pem

# Verify a signed skill
pincer skills verify ./my-skill
```

When `PINCER_REQUIRE_SIGNED_SKILLS=true`, only signed skills are loaded.

---

## Examples

### Weather Skill

```python
from pincer.tools import tool
import httpx

@tool(
    name="get_weather",
    description="Get current weather for a city",
)
async def get_weather(city: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://wttr.in/{city}",
            params={"format": "j1"},
        )
        data = resp.json()
        current = data["current_condition"][0]
        
        return (
            f"🌤️ Weather in {city}:\n"
            f"  Temperature: {current['temp_C']}°C / {current['temp_F']}°F\n"
            f"  Condition: {current['weatherDesc'][0]['value']}\n"
            f"  Humidity: {current['humidity']}%\n"
            f"  Wind: {current['windspeedKmph']} km/h"
        )
```

### Habit Tracker Skill

```python
from pincer.tools import tool
from datetime import date
import json
from pathlib import Path

DATA_FILE = Path("data/habits.json")

def _load() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"habits": {}}

def _save(data: dict):
    DATA_FILE.write_text(json.dumps(data, indent=2))

@tool(
    name="log_habit",
    description="Log completion of a daily habit (e.g., exercise, meditation, reading)",
)
async def log_habit(habit_name: str) -> str:
    data = _load()
    today = date.today().isoformat()
    
    if habit_name not in data["habits"]:
        data["habits"][habit_name] = []
    
    if today not in data["habits"][habit_name]:
        data["habits"][habit_name].append(today)
    
    streak = _calculate_streak(data["habits"][habit_name])
    _save(data)
    
    return f"✅ Logged '{habit_name}' for {today}. Current streak: {streak} days 🔥"

@tool(
    name="habit_stats",
    description="Show statistics for all tracked habits",
)
async def habit_stats() -> str:
    data = _load()
    if not data["habits"]:
        return "No habits tracked yet. Tell me to log a habit!"
    
    lines = ["📊 Habit Stats:"]
    for habit, dates in data["habits"].items():
        streak = _calculate_streak(dates)
        total = len(dates)
        lines.append(f"  {habit}: {streak}-day streak, {total} total")
    
    return "\n".join(lines)

def _calculate_streak(dates: list[str]) -> int:
    if not dates:
        return 0
    sorted_dates = sorted(dates, reverse=True)
    streak = 1
    for i in range(len(sorted_dates) - 1):
        curr = date.fromisoformat(sorted_dates[i])
        prev = date.fromisoformat(sorted_dates[i + 1])
        if (curr - prev).days == 1:
            streak += 1
        else:
            break
    return streak
```

---

## Publishing Skills

Share your skill with the community:

1. Create a GitHub repo named `pincer-skill-{name}`
2. Include `skill.yaml`, `main.py`, `requirements.txt`, and `README.md`
3. Tag a release (e.g., `v1.0.0`)
4. Users can install with: `pincer skills install github:you/pincer-skill-{name}`

### Community Skills Registry

Submit a PR to [pincerhq/skill-registry](https://github.com/pincerhq/skill-registry) to list your skill in the official registry. We review for quality and security before listing.