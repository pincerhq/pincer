# 🦀 The Pincer Community

> *"A personal AI agent is only as good as the community that builds it."*

---

## What We're Doing Here

We're building something that didn't exist two years ago: a personal AI agent that lives in your messaging apps and actually does things for you. Reads your email. Manages your calendar. Searches the web. Remembers what you told it last month. Makes phone calls on your behalf.

This isn't a toy and it isn't a research project. It's a tool that people use every day to manage their real lives. The doctor who checks their schedule between patients. The freelancer who manages invoices via WhatsApp. The parent who asks their agent to find a plumber while wrangling kids. The student who has their agent summarize papers while they're on the bus.

Every line of code, every skill, every documentation fix, every answered question in Discord — it ripples out into the daily lives of real people who trust us to handle their digital world. That's a big deal. We don't take it lightly, and we hope you don't either.

---

## Our Values

### Ship Things That Work

We value working software over perfect plans. A merged PR that handles 90% of cases today is more valuable than a theoretical design that handles 100% of cases someday. Ship, learn, iterate.

This doesn't mean we ship garbage. It means we have a bias toward action and a tolerance for imperfection, because we know that real-world feedback teaches us things that no amount of design discussion can.

### Security Is Not a Feature

Security is a property of everything we build. It's not a checkbox, it's not a sprint, it's not something you add later. Every feature gets a threat model. Every tool gets a risk assessment. Every skill gets scanned.

People trust Pincer with their email, their calendar, their files. That trust is earned slowly and lost instantly. We will always choose safety over convenience, and we'll be transparent about the tradeoffs.

### Readable Over Clever

Pincer's codebase should be understandable by someone reading it for the first time. We prefer:

- Explicit over implicit
- Boring over clever
- Comments that explain *why* over comments that explain *what*
- Small functions with clear names over large functions with unclear names
- Flat over nested

If you need to write a comment explaining what your code does, your code probably needs to be rewritten. If you need to write a comment explaining *why* your code does what it does, that's a great comment.

### Everyone Teaches, Everyone Learns

The maintainer learns from the first-time contributor who notices a confusing API. The experienced engineer learns from the newcomer who asks "why don't we just...?" The user who reports a bug teaches us where our assumptions were wrong.

Knowledge in this community flows in every direction. No one is above learning, and no one is below teaching.

### Build for the Person, Not the Benchmark

When we make decisions about features, performance, cost, and complexity, we think about the actual person using Pincer. Not benchmarks, not GitHub stars, not venture capital narratives. A real person, on their phone, in their messaging app, trying to get through their day a little more easily.

---

## Community Spaces

### Discord — [discord.gg/pincer](https://discord.gg/pincer)

This is where the daily life of the community happens. It's informal, it's fast, and it's where you'll get the quickest help. Think of it as the team chat for an open-source project.

**#general** — The front door. Say hi, ask questions, share what you're working on. If you're not sure where something goes, put it here.

**#showcase** — Built something with Pincer? Show it off. Screenshots, recordings, stories about how your agent saved you time. This is one of the most important channels because it reminds us all *why* we're building this.

**#skills-dev** — The workshop. Building a custom skill? Stuck on the tool decorator? Want feedback on your approach? This is the place. People here tend to pair-program in threads.

**#channels-dev** — Working on a new channel integration (Signal, LINE, Matrix, etc.)? Coordinate here. Channel integrations are complex and we want to make sure efforts don't overlap.

**#ideas** — Brainstorming without commitment. "What if Pincer could...?" conversations happen here. Some of the best features started as wild ideas in this channel.

**#bugs** — Quick bug reports that don't need a full GitHub issue yet. "Is this a bug or am I doing something wrong?" — ask here first.

**#off-topic** — We're humans, not just code-shipping machines. Talk about your day, share interesting things you've read, bond over shared interests. Community is built in the spaces between work.

### GitHub Discussions — [github.com/pincerhq/pincer/discussions](https://github.com/pincerhq/pincer/discussions)

For longer-form conversations that need to be searchable and persistent. Architecture decisions, RFCs, roadmap planning, and "how should we approach X?" discussions belong here.

### GitHub Issues — [github.com/pincerhq/pincer/issues](https://github.com/pincerhq/pincer/issues)

For well-defined bugs and feature requests with clear scope. If you're not sure yet, start in Discord or Discussions.

---

## How to Get Involved

### If You Have 5 Minutes

- ⭐ Star the repo (yes, this actually matters)
- Join Discord and say hi in #general
- Share Pincer with someone who might find it useful

### If You Have an Hour

- Go through the [Quickstart](quickstart.md) and report anything confusing
- Pick a `good-first-issue` from the [issue tracker](https://github.com/pincerhq/pincer/labels/good-first-issue)
- Answer someone's question in Discord
- Read a doc and fix a typo (PRs for typos are PRs for quality)

### If You Have a Weekend

- Build a skill — check the [Skills Guide](skills-guide.md) and the ideas list in [CONTRIBUTING.md](../CONTRIBUTING.md)
- Improve test coverage for a module you've been reading
- Write a tutorial or blog post about your experience with Pincer
- Translate documentation into your language

### If You Want to Go Deep

- Pick an open Discussion about architecture and contribute your perspective
- Prototype a new channel integration
- Work on the memory system (entity extraction, relationship mapping, smarter retrieval)
- Help design and implement MCP support

---

## Skill Bounties

We occasionally post bounties for community-built skills — small cash rewards ($50-$200) for skills we think would be valuable. Check the [`bounty` label](https://github.com/pincerhq/pincer/labels/bounty) on GitHub Issues for current opportunities.

Even without bounties, every merged skill gets its author credited in the skill registry with a link to their GitHub profile and a permanent place in the CHANGELOG.

---

## Community Calls

We host monthly community calls on Discord (voice channel). These are informal, unrecorded, and open to everyone:

- **What we discuss:** What shipped last month, what's planned next, open questions, live demos of community-built skills
- **When:** First Thursday of each month, 18:00 UTC
- **How to join:** Just show up in the Discord voice channel

No agenda. No slides. Just people talking about what they're building and what they need. If you want to demo something, ping a maintainer in Discord beforehand so we can make sure you get time.

---

## For Companies Using Pincer

If your team or company uses Pincer, we'd love to know:

- **Share your use case** — it helps us prioritize features that matter
- **Report bugs** — enterprise-grade reliability comes from real-world usage
- **Sponsor the project** — GitHub Sponsors helps us dedicate more time to maintenance and security
- **Contribute back** — if your team builds internal improvements, consider upstreaming them

We're building toward enterprise features (SSO, audit compliance, managed hosting), and your input shapes the roadmap.

---

## Recognition & Gratitude

Open source runs on volunteer energy. We try to be thoughtful about recognizing that:

- **Every contributor** is listed on the GitHub Contributors page
- **Every PR** gets a genuine thank-you, not a bot response
- **Significant contributions** get called out in release notes by name
- **Skill authors** get permanent credit in the registry
- **Community helpers** (people who answer questions, triage issues, review PRs) get the `@contributor` Discord role

If you've contributed and feel underrecognized, tell us. We'll fix it.

---

## The Future

Pincer started as one person's side project. It's growing into something bigger. Where it goes depends on the people who show up.

We're not trying to build a company. We're not trying to raise money. We're not trying to "disrupt" anything. We're building a useful tool and a good community, and we're sharing it with anyone who wants to use it.

If that resonates with you — welcome. Pull up a chair. There's work to do and we're glad you're here.

🦀