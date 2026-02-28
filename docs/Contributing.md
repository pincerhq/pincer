# Contributing to Pincer 🦀

> *"We don't need your resume. We need your pull request."*

---

## You Belong Here

Pincer was started by a solo developer — a radiologist who writes Python between reading scans. If that sounds unusual, good. This project was built by someone who doesn't fit the typical open-source founder mold, and we want contributors who don't either.

You don't need to be a senior engineer. You don't need a CS degree. You don't need to have contributed to open source before. If you've ever texted an AI and thought *"this should be able to actually do things for me"* — you understand the mission, and you're qualified to help build it.

**We especially welcome:**

- 🩺 Doctors, lawyers, teachers, and other professionals who code on the side
- 🌍 Non-English speakers (we need translations and internationalization)
- 🎨 Designers who can make the dashboard and docs beautiful
- 🧪 Tinkerers who break things and file good bug reports
- 🤖 People who vibe-code with AI — we do too, and we're not ashamed of it
- 🐣 First-time open source contributors — we'll help you through your first PR

---

## The 5-Minute Contribution

Not sure where to start? Here are things you can do right now that genuinely help:

1. **Star the repo** — seriously, this matters for discoverability
2. **Try the quickstart** and report where you got stuck — docs bugs are real bugs
3. **Fix a typo** — open a PR, no issue needed
4. **Answer a question in Discord** — helping others is contributing
5. **Share what you built** — post in #showcase, write a tweet, tell a friend

You don't have to write code to be a contributor.

---

## Where Things Happen

| Place | What Goes On There |
|-------|--------------------|
| [**Discord**](https://discord.gg/pincer) | Daily conversations, questions, showing off builds, real-time help |
| [**GitHub Issues**](https://github.com/pincerhq/pincer/issues) | Bug reports and well-defined feature requests |
| [**GitHub Discussions**](https://github.com/pincerhq/pincer/discussions) | Architecture ideas, RFCs, roadmap conversations |
| [**Twitter/X**](https://twitter.com/pincerhq) | Updates, demos, community highlights |

### Discord Channels

| Channel | Purpose |
|---------|---------|
| `#general` | Hang out, ask questions, share ideas |
| `#showcase` | Show what you've built with Pincer |
| `#skills-dev` | Building custom skills — get help, share patterns |
| `#channels-dev` | Working on new channel integrations |
| `#bugs` | Quick bug reports and troubleshooting |
| `#ideas` | Wild ideas, feature brainstorms, "what if..." |
| `#off-topic` | Life, memes, other projects, whatever |

**The rule in Discord is simple:** be helpful, be curious, and remember that the person asking the "dumb question" is the brave one. Every expert was once a beginner who felt stupid.

---

## Development Setup

### Prerequisites

- Python 3.11+ (3.12 recommended)
- [uv](https://github.com/astral-sh/uv) (strongly recommended) or pip
- Git
- A Telegram bot token (fastest channel to test against)
- At least one LLM API key (Anthropic, OpenAI, or free with Ollama)

### Getting Running

```bash
# Fork on GitHub, then:
git clone https://github.com/YOUR_USERNAME/pincer.git
cd pincer

# Install everything
uv sync
# or: pip install -e ".[dev,all]"

# Set up config
cp .env.example .env
nano .env  # Add your API keys

# Verify it works
pytest                        # Tests pass?
ruff check src/ tests/        # Linting clean?
pincer run --verbose          # Agent starts?
```

If you get stuck at any step, that's a documentation bug. [Open an issue](https://github.com/pincerhq/pincer/issues/new?template=bug_report.md) or ask in Discord.

### Project Structure

```
pincer/
├── src/pincer/
│   ├── core/           # Agent loop, sessions, events — the brain
│   ├── llm/            # LLM providers (Anthropic, OpenAI, Ollama, etc.)
│   ├── channels/       # Telegram, WhatsApp, Discord — the ears and mouth
│   ├── memory/         # SQLite + FTS5, entities — the memory
│   ├── tools/          # Tool registry, sandbox — the hands
│   ├── skills/         # Skill loader, scanner — the extensibility
│   ├── voice/          # Twilio voice calling — Sprint 7
│   ├── security/       # Audit log, firewall, rate limiter — the immune system
│   ├── scheduler/      # Cron jobs, proactive briefings
│   └── dashboard/      # FastAPI + HTMX web UI
├── skills/             # Bundled and community skills
├── tests/              # pytest test suite
├── docs/               # You are here
└── pyproject.toml
```

The entire codebase is under 8,000 lines. You can read the whole thing in an afternoon. That's intentional.

---

## Ways to Contribute

### 🟢 Easy — Great First Contributions

**Build a skill.** Skills are self-contained Python modules, typically 50-150 lines. If you can write a Python function, you can write a skill. See the [Skills Guide](docs/skills-guide.md).

Skill ideas we'd love to have:

- Home automation (Home Assistant, Philips Hue)
- Notion sync (read/write pages and databases)
- Spotify/Apple Music control
- Fitness tracking (Apple Health, Garmin, Strava)
- Package tracking (DHL, FedEx, UPS)
- Language flashcards (Anki integration)
- Recipe lookup and meal planning
- News digest from specific RSS feeds
- Local transit (DB, TfL, MTA) schedules
- Pomodoro timer with stats

**Improve documentation.** Found something confusing? Fix it. Write a tutorial. Record a video walkthrough. Translate a page. Documentation that prevents one confused user from giving up is more valuable than most code.

**Write tests.** We aim for comprehensive coverage. Pick a module, read the code, write tests for edge cases. Tests are the safety net that lets everyone else move fast.

### 🟡 Medium — Some Context Needed

**Add a new channel integration.** Every messaging platform is a potential channel. Please open a discussion first so we can coordinate, but we'd love:

- Signal (via signal-cli or libsignal)
- LINE
- Matrix / Element
- Microsoft Teams
- iMessage (macOS only)
- Zalo (Vietnamese market)
- Slack (workspace bot)

**Improve the dashboard.** The web UI is functional but basic. If you have frontend skills and an eye for design, there's a lot of room to make it beautiful. The stack is FastAPI + HTMX + Jinja2 — no heavy frontend framework needed.

**Work on memory.** The memory system is the most intellectually interesting part of Pincer. Better entity extraction, smarter summarization, relationship mapping between entities — this is where the agent starts to feel truly personal.

### 🔴 Hard — Deep Architecture Work

- **MCP (Model Context Protocol) support** — integrate with the broader MCP ecosystem
- **Multi-agent routing** — let users define specialized sub-agents
- **Voice-to-voice** — direct speech-to-speech without text intermediary
- **Encrypted memory** — at-rest encryption for the SQLite database
- **Plugin marketplace** — discovery, ratings, verified publishers

For any of these, please open a GitHub Discussion first. We want to design together before you invest days of work.

---

## The Pull Request Process

### 1. Branch Naming

```
feat/signal-channel         # New feature
fix/whatsapp-reconnect      # Bug fix
docs/skill-tutorial         # Documentation
skill/notion-sync           # New skill
test/memory-edge-cases      # More tests
refactor/memory-store       # Refactoring
```

### 2. Code Style

We keep it simple:

- **ruff** handles formatting and linting (runs in CI)
- **100-character** line length
- **Type hints** on all public functions
- **Docstrings** on all public classes and functions (Google style)
- **async/await** preferred — the whole codebase is async
- **Tests** for new functionality (pytest + pytest-asyncio)

```python
async def send_notification(
    self,
    user_id: str,
    message: str,
    *,
    urgent: bool = False,
) -> bool:
    """Send a notification to a user across their preferred channel.

    Args:
        user_id: The user's identifier.
        message: Notification content.
        urgent: If True, send immediately regardless of quiet hours.

    Returns:
        True if the notification was delivered successfully.

    Raises:
        ChannelError: If no active channel is available for the user.
    """
```

### 3. Commit Messages

[Conventional Commits](https://www.conventionalcommits.org/) format:

```
feat(channels): add Signal channel integration
fix(whatsapp): handle reconnection on session timeout
docs(skills): add tutorial for weather skill
test(agent): add edge cases for tool approval flow
chore(deps): bump anthropic SDK to 0.50.0
```

### 4. Opening the PR

- Use a clear title: `feat(skills): add Notion sync skill`
- Explain what and why (not just how)
- Link to the issue if there is one
- Include screenshots/recordings for UI changes
- Fill in the PR template checklist

### 5. Review

We aim to review PRs within 48 hours. Often faster. If it's been 3 days and you haven't heard from us, ping in Discord — we might have missed it.

We review for:
- Correctness and security (does it work? does it stay safe?)
- Tests (is it tested? will we know if it breaks?)
- Code clarity (can someone else read this in 6 months?)
- Architecture fit (does it follow existing patterns?)

We don't review for:
- Perfection (good enough, shipped, and iterated on > perfect and never merged)
- Credential gatekeeping (we don't care about your background, we care about the code)

### PR Checklist

```
- [ ] Tests pass (pytest)
- [ ] Linting passes (ruff check src/ tests/)
- [ ] Type checking passes (mypy src/)
- [ ] Documentation updated (if behavior changed)
- [ ] CHANGELOG.md updated (if user-facing)
```

---

## AI-Assisted Contributions

Let's be honest: Pincer itself was largely vibe-coded with AI assistance. We would be massive hypocrites to reject AI-assisted contributions.

**AI-assisted and vibe-coded PRs are explicitly welcome.** Use Claude, Copilot, Cursor, whatever helps you ship. The only requirements:

1. You understand what the code does (you can explain it in review)
2. Tests pass
3. It follows our style
4. You can respond to review comments

We don't check. We don't care. We care that the code works and you can maintain it.

---

## Recognition

Every contributor gets recognized:

- **All contributors** appear on the [GitHub Contributors page](https://github.com/pincerhq/pincer/graphs/contributors)
- **Significant contributions** get a shoutout in [release notes](CHANGELOG.md)
- **Skill authors** are credited in the skill registry with a link to their profile
- **Repeat contributors** get the `@contributor` role in Discord

We're also planning a contributors page on the website. If you've helped make Pincer better, people should know.

---

## Decision Making

Pincer is currently maintained by a solo developer. As the community grows, so will governance. Here's how decisions work today:

- **Small changes** (bug fixes, docs, simple skills) — merge fast, iterate later
- **Medium changes** (new features, new skills with external deps) — PR review + brief discussion
- **Large changes** (new channels, architecture changes, security-critical) — GitHub Discussion first, design together, then implement
- **Direction and roadmap** — driven by what users actually need, discussed openly in GitHub Discussions

The goal is to be responsive, not bureaucratic. We'd rather merge something good-enough today than debate something perfect for weeks.

---

## Philosophy

A few principles that shape how we build Pincer:

**Simplicity over features.** We'd rather have 10 things that work perfectly than 100 things that kind of work. The codebase should stay under 10K lines for as long as possible.

**Security is not optional.** Every feature gets a threat model. Every tool gets a safety classification. Every skill gets scanned. This is non-negotiable because people trust Pincer with their email, calendar, and files.

**Readability over cleverness.** Write code that a tired developer at 11pm can understand. If you need a comment to explain what the code does, the code should probably be rewritten.

**Ship, then iterate.** A merged PR that works > a perfect PR in draft. We can always improve things in the next release.

**Respect people's time.** Quick reviews. Clear feedback. No PR left hanging for weeks. If we can't merge something, we explain why promptly.

---

## Getting Help

Stuck? Lost? Confused by the codebase? That's normal and it's not your fault.

- **Discord #general** — fastest response, usually within hours
- **GitHub Discussions** — for longer questions
- **Direct message the maintainer** on Discord — for sensitive topics

We will never make you feel bad for asking a question. If someone does, that's a Code of Conduct violation and we'll deal with it.

---

Thank you for being here. Every person who contributes — code, docs, bug reports, ideas, encouragement — makes Pincer better for everyone. 🦀