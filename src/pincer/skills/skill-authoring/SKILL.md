---
name: Authoring a Pincer skill
description: How to write a new Pincer skill as a SKILL.md directory — frontmatter fields, progressive disclosure, and when to add a script versus pure instructions. Load this when the user asks you to create, package, or explain a Pincer skill.
---

# Authoring a Pincer skill

A skill is a directory containing a single `SKILL.md` file. Nothing else is
required — a directory with `SKILL.md` present *is* a skill.

```text
skills/<skill-name>/
  SKILL.md          # required: frontmatter + instructions
  reference.md       # optional: extra docs, loaded on demand
  scripts/
    do_thing.py       # optional: executable helper
```

## SKILL.md format

```markdown
---
name: my-skill
description: One dense sentence describing what this skill does and when to use it.
---

# My Skill

Free-form markdown instructions for the agent. This is the body that
`load_skill(name)` returns.
```

Two frontmatter fields are required: `name` and `description`. The
`description` is the *only* part shown to the model before it decides to
load the skill (progressive disclosure level 1 — it's injected into the
system prompt's "Available Skills" block), so write it as a dense,
specific sentence that makes clear both what the skill does and when to
reach for it. Vague descriptions ("Helps with stuff") mean the model will
never load the skill.

## Progressive disclosure — the three levels

1. **Level 1 (always in context):** name + description only, via the
   system prompt's Available Skills block.
2. **Level 2 (`load_skill(name)`):** returns the full markdown body plus a
   manifest of any other files in the skill's directory.
3. **Level 3 (`load_skill_reference(name, path)`):** reads one specific
   file from the skill's directory by relative path — use this for large
   reference docs, data files, or examples you don't want in every
   `load_skill` call.

## Adding a script

If the skill needs to *do* something rather than just explain, add an
executable file (Python or any script with a shebang) anywhere under the
skill's directory and call it with `run_skill_script(name, script, args)`.
Scripts run in a sandboxed subprocess (resource limits + network domain
allowlisting) unless `skill_sandbox_disabled` is set, and always require
user approval before executing.

Prefer pure-instruction skills (no scripts) when the task is something the
model can already do with its existing tools (shell, files, etc.) just by
being told the right approach — a script is for cases where you need
deterministic, repeatable logic (parsing a specific file format, calling
an API with a fixed auth scheme) that's error-prone to improvise every time.

## Where skills live

- Bundled skills: shipped inside the installed package at `src/pincer/skills/` (fixed path, not configurable).
- User skills: `~/.pincer/skills/` (configurable via `skills_dir`).

A user skill with the same `name` as a bundled skill overrides it. Both
roots are capped at `skills_max_loaded_per_root` (default 100) skills.
