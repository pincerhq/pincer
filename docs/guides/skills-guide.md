# Skills Guide — Build Custom Pincer Skills

Skills are how you extend Pincer's agent with domain-specific knowledge and, optionally, scripts. A skill is a directory containing a single `SKILL.md` file — this is Anthropic's open [Agent Skills](https://www.anthropic.com/) format, so skills you write for Pincer are portable to other agents that support the same convention.

Skills and MCP servers coexist — connecting an MCP server no longer disables skills.

---

## Your First Skill in 2 Minutes

Create `skills/hello-world/SKILL.md`:

```markdown
---
name: hello-world
description: Greets the user by name. Load this when asked to say hello.
---

# Hello World

When asked to greet someone, respond with a warm, personalized greeting
that includes their name.
```

Restart Pincer. The skill's name and description now appear in the "Available Skills" block of the system prompt. Ask the agent something that matches the description and it will call `load_skill("hello-world")` to read the full body before acting on it.

That's it — no manifest, no Python entry point, no registration step. A directory with `SKILL.md` present *is* a skill.

---

## Skill Structure

```
skills/
└── my-skill/
    ├── SKILL.md          # required: frontmatter + instructions
    ├── reference.md        # optional: extra docs, loaded on demand
    └── scripts/
        └── do_thing.py     # optional: executable helper
```

### SKILL.md frontmatter

Two fields are required:

```yaml
---
name: my-skill
description: One dense sentence describing what this skill does and when to use it.
---
```

- `name` is the identifier the agent uses — it's what appears in the system prompt's Available Skills block, and what `load_skill(name)` / `load_skill_reference(name, path)` / `run_skill_script(name, script, args)` take as their `name` argument. It does **not** need to match the skill's directory name; the directory is just where the files live on disk. (The dashboard's `GET /api/skills/{name}` route is the exception — it addresses skills by directory name specifically, so it keeps working even if a skill's frontmatter `name` changes.)
- `description` is the *only* part of the skill shown to the model before it decides to load it — write it as a specific, dense sentence covering both what the skill does and when to reach for it. A vague description means the model will never load the skill.

Everything after the closing `---` is free-form markdown: the instructions returned by `load_skill(name)`.

---

## Progressive Disclosure

Skills are loaded in three levels, so a large skill library doesn't blow the context budget:

| Level | Mechanism | What's loaded |
|-------|-----------|----------------|
| 1 | System prompt | Every skill's `name` + `description`, in the "Available Skills" block |
| 2 | `load_skill(name)` | The full markdown body, plus a manifest of other files in the skill's directory |
| 3 | `load_skill_reference(name, path)` | One specific file from the skill's directory, by relative path |

Use level 3 for large reference docs, data files, or examples you don't want included in every `load_skill` call.

---

## Adding a Script

If a skill needs to *do* something deterministic rather than just explain an approach, add an executable file anywhere under the skill's directory (Python or any script with a shebang) and call it with:

```
run_skill_script(name, script, args)
```

Scripts run in a sandboxed subprocess — resource limits (memory, CPU) and network domain allowlisting for Python scripts — unless `skill_sandbox_disabled` is set. **`run_skill_script` always requires user approval** before executing, unlike `load_skill`/`load_skill_reference` which are read-only and ungated.

Prefer pure-instruction skills (no scripts) when the model can already accomplish the task with its existing tools just by being told the right approach. Reach for a script when you need repeatable, deterministic logic (parsing a fixed file format, calling an API with a specific auth scheme) that's error-prone to improvise from instructions alone.

---

## Where Skills Live

- **Bundled skills:** shipped inside the installed package at `src/pincer/skills/` (fixed path, not configurable). Ships with `pip install pincer-agent` — no project checkout required.
- **User skills:** `~/.pincer/skills/` (`skills_dir` config) — a local directory, or an S3 / Azure Blob / GCS prefix (see [Remote Skill Roots](#remote-skill-roots)).

A user skill with the same `name` as a bundled skill overrides it.

### Config

| Setting | Default | Purpose |
|---|---|---|
| `skills_dir` | `~/.pincer/skills` | User skills root: a local path, or an `s3://`, `az://` or `gs://` URI |
| `skills_max_loaded_per_root` | `100` | Hard cap on skills loaded per root — applied at discovery, before prompt construction |
| `skills_max_prompt_tokens` | `None` (no limit) | Soft budget for the Available Skills prompt block; once exceeded, descriptions are truncated and then trailing entries are dropped with an "X more skills" note. Tune this down for small-context local models. |
| `skill_sandbox_disabled` | `False` | Bypass sandboxing for `run_skill_script` (trusted/dev workflows only) |
| `skills_remote_cache_dir` | `<data_dir>/cache/skills` | Local cache for remote skill roots |
| `skills_index_cache_ttl_seconds` | `60` | How long `GET /api/skills` reuses a scanned index; `0` rescans on every request |

### Remote Skill Roots

Teams can host shared skills in object storage instead of mounting a directory — useful in containers. Point `skills_dir` at a bucket prefix whose subdirectories each hold a `SKILL.md`, exactly like a local root:

```bash
pip install "pincer-agent[remote-skills]"   # cloudpathlib + the S3, Azure and GCS SDKs

PINCER_SKILLS_DIR=s3://my-bucket/team-skills      # Amazon S3, MinIO, Cloudflare R2, …
PINCER_SKILLS_DIR=az://my-container/team-skills   # Azure Blob Storage
PINCER_SKILLS_DIR=gs://my-bucket/team-skills      # Google Cloud Storage
```

There is still exactly one user root; bundled skills always stay local. Without the extra, a remote `skills_dir` logs an error naming the install command and Pincer runs with bundled skills only. An unreachable bucket does the same (with a warning) instead of aborting startup.

**Credentials.** Every setting below is optional. Anything left unset falls through to the provider SDK's own default chain (`AWS_*` env vars / `~/.aws` / instance roles, `DefaultAzureCredential`, Application Default Credentials). Secrets are never logged — log lines show only the URI.

| Provider | Settings (`PINCER_` + upper-case name) |
|---|---|
| S3 | `skills_s3_endpoint_url` (MinIO/R2), `skills_s3_region`, `skills_s3_profile`, `skills_s3_access_key_id`, `skills_s3_secret_access_key`, `skills_s3_session_token` |
| Azure | `skills_azure_connection_string`, **or** `skills_azure_account_url` + `skills_azure_credential` (account key or SAS token). With only the account URL, `DefaultAzureCredential` is used. |
| GCS | `skills_gcs_credentials_file` (service-account JSON), `skills_gcs_project` |

**Caching.** Files read from a remote root are cached under `skills_remote_cache_dir/objects/` and re-downloaded only when the object in the bucket is newer. Skills are discovered once at startup, so restart Pincer to pick up new or removed remote skills. The dashboard's `/api/skills` rescans at most every `skills_index_cache_ttl_seconds`.

**Scripts.** `run_skill_script` on a remote skill first downloads the whole skill directory into `skills_remote_cache_dir/materialized/<uri-hash>/<SKILL.md-hash>/`, then runs it through the same approval gate and sandbox as a local script. Object storage keeps no file modes, so non-Python scripts get the owner execute bit restored after download. The copy is keyed on the SKILL.md hash: **if you change only a script, also change SKILL.md** (or delete that skill's `materialized/` folder), otherwise the previous copy keeps running.

---

## Bundled Starter Skills

Pincer ships five pure-instruction starter skills documenting its own capabilities: `skill-authoring` (this guide, in agent-readable form), `memory-recall`, `mcp-server-setup`, `scheduler-briefings`, and `doctor-troubleshooting`. Read any of them under `src/pincer/skills/` for a second example of the format.

---

## Migrating from the Legacy Format

Prior to this migration, skills used a `manifest.json` + `skill.py` pair with Python-decorated tool functions, a security scanner that gated loading, and mutual exclusion with MCP. That format is removed — there is no automatic converter. To port an old skill:

1. Create `skills/<name>/SKILL.md`. Move `manifest.json`'s `description` into the frontmatter `description` field; write a markdown body explaining what the skill does (this replaces the old per-tool `tools[]` schema entries — the model now reads instructions instead of calling a fixed function signature).
2. If `skill.py` did real work (an API call, a computation), move it into `skills/<name>/scripts/` as a standalone script invoked via `run_skill_script`, rather than a function called directly by the tool registry.
3. Drop `permissions`/`env_required` from the old manifest — the new sandbox model uses `SandboxConfig.allowed_domains`/`allowed_env_vars` passed by the caller, not a self-declared manifest field.
4. Remove any dependency on the old `pincer skills` CLI (`list`/`install`/`create`/`scan`/`remove`/`info` are all gone) — skills are now filesystem-discovered only, no install step.
