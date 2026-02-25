# Installation & Setup

## Prerequisites

- **Python 3.12+**
- At least one LLM API key (Anthropic or OpenAI)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) for the Telegram channel

## Install from Source

```bash
# Clone the repository
git clone https://github.com/vpu2301/pincer.git
cd pincer/pincer

# Install with uv (recommended)
uv sync --all-extras --extra dev

# Or install with pip
pip install -e ".[all,dev]"
```

## Optional Extras

Install only what you need:

```bash
pip install -e ".[search]"    # DuckDuckGo search
pip install -e ".[tavily]"    # Tavily search (richer results)
pip install -e ".[browser]"   # Playwright browser automation
pip install -e ".[memory]"    # sqlite-vec for vector search
pip install -e ".[pdf]"       # PDF text extraction (pymupdf)
pip install -e ".[pdfgen]"    # PDF generation (fpdf2)
pip install -e ".[all]"       # Everything above
```

## Configuration

```bash
# Copy the example .env file
cp .env.example .env

# Edit with your API keys
nano .env
```

**Minimum required configuration:**

```env
PINCER_ANTHROPIC_API_KEY=sk-ant-...   # or PINCER_OPENAI_API_KEY
PINCER_TELEGRAM_BOT_TOKEN=123456:ABC...
```

See [Configuration Reference](configuration.md) for all available options.

## Run the Agent

```bash
# Using the CLI
pincer run

# Or directly with Python
python -m pincer
```

You should see output like:

```
Pincer starting...
   Provider: anthropic
   Model: claude-sonnet-4-5-20250929
   Budget: $5.00/day
   Data: /home/you/.pincer

Memory system enabled
Telegram connected (streaming enabled)

Pincer is running! Press Ctrl+C to stop.
```

## Docker

```bash
# Build and run with Docker Compose
docker-compose up --build
```

The `Dockerfile` installs the package and runs `pincer` as the entry point. The `docker-compose.yml` reads environment variables from `.env`.

## Verify Installation

```bash
# Check configuration
pincer config

# Check today's API costs
pincer cost
```

## Talk to Your Agent

1. Open Telegram
2. Find your bot (the one you created with @BotFather)
3. Send `/start`
4. Start chatting!

### Available Commands

| Command | Description |
|---------|-------------|
| `/start` | Initialize the bot and show welcome message |
| `/clear` | Reset conversation history |
| `/cost` | Show today's API spend |
| `/help` | Show help message |
