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
| `skills_cache_dir` | `<data_dir>/cache/skills` | Local directory (never a URI) for cloudpathlib's copies of remote skill files (`objects/`), the index generation stamp and the disk ID. Scripts from remote skills run from here. |
| `skills_index_cache_ttl_seconds` | `60` | Max age of the index the dashboard API serves before it rescans (0–60; `0` rescans on every request). Any index change invalidates it immediately. |
| `skills_index_watch_interval_seconds` | `300` | How often `pincer run` checks in the background for index changes made by other processes (e.g. `pincer skills rebuild`); `0` disables the watcher, and requests then check themselves |

### Rebuilding the Index

Skills are discovered at startup. To pick up added, removed or edited skills without restarting, run:

```bash
pincer skills rebuild            # clear cached remote files, rescan, notify running processes
pincer skills rebuild --keep-cache
```

The command rescans both roots and writes a new generation stamp to `skills_cache_dir/index-generation`. A running `pincer run` picks that up in two ways:

- **Agent and skill tools:** a background watcher checks the stamp every `skills_index_watch_interval_seconds` (5 minutes by default) and rescans off the request path. The agent therefore sees a rebuild within at most the interval plus the scan time, and no chat turn ever waits on a rescan.
- **Dashboard (`/api/skills`):** checks the stamp on every request, so it shows the rebuild immediately (and rescans whenever its index is older than `skills_index_cache_ttl_seconds`). A dashboard request in `pincer run` reloads the shared index, so the agent sees that reload too.

Processes without a watcher (the standalone MCP server, or `pincer run` with the interval set to `0`) check the stamp on every read instead.

**Over the API:** `POST /api/skills/rebuild` (optional `?keep_cache=true`) does the same from the dashboard or a script. It rebuilds the serving process's own index first, so the agent and dashboard see the result straight away. It then queues a `rebuild_skill_index` background job for the task workers. In a Redis setup that job reaches **one** consumer, which may be the main process (where it does nothing). Workers sharing the data volume pick the change up from the stamp regardless. See the [REST API reference](../reference/rest-api.md#post-apiskillsrebuild). The command and the API share one implementation, `pincer.services.skills.SkillService` — its `rebuild()` and `invalidate()` are the hooks for anything else that changes skills (such as a future upload endpoint).

### Remote Skill Roots

Teams can host shared skills in object storage instead of mounting a directory — useful in containers. Point `skills_dir` at a bucket prefix whose subdirectories each hold a `SKILL.md`, exactly like a local root:

```bash
pip install "pincer-agent[remote-skills]"   # cloudpathlib + the S3, Azure and GCS SDKs

PINCER_SKILLS_DIR=s3://my-bucket/team-skills      # Amazon S3, MinIO, Cloudflare R2, …
PINCER_SKILLS_DIR=az://my-container/team-skills   # Azure Blob Storage
PINCER_SKILLS_DIR=gs://my-bucket/team-skills      # Google Cloud Storage
```

There is still exactly one user root; bundled skills always stay local.

**A remote root must be usable at startup.** `pincer run`, `pincer run tasks`, `pincer mcp serve` and `pincer skills rebuild` refuse to start, exiting with code 1, when a remote `skills_dir` can't be used. The error says what to fix:

- **the `remote-skills` extra isn't installed:** the error names the install command;
- **the storage client can't be built** (for example, an unknown AWS profile or a malformed connection string): the error lists the `PINCER_STORAGE_*` settings for that provider;
- **listing the root fails** (missing or wrong credentials, a bucket that doesn't exist, an unreachable endpoint): same as above. Startup makes this one listing, the same call discovery makes.

An empty prefix is fine (no skills yet). SDK default credentials count as configured: an IAM role, `AWS_*` env vars or `~/.aws` for S3; Application Default Credentials for GCS; `DefaultAzureCredential` for Azure once `storage_azure_account_url` is set (see the table below).

**After startup, an outage never empties the index.** If the bucket becomes unreachable later (an expired token, a 5xx), Pincer keeps running:
- A background or dashboard rescan logs a warning and keeps that root's previously discovered skills. It announces nothing, and retries a pending change at most once a minute.
- `pincer skills rebuild` and `POST /api/skills/rebuild` fail instead (exit 1, or HTTP 503). The index and the generation stamp stay as they were.

This applies to the root's *listing*. A single skill whose SKILL.md can't be read is never fatal, and the rest of the root loads normally. On a local root the skill is skipped (e.g. a `chmod 000` file). On a cloud root it keeps its previous version, because a one-object failure is usually transient. Either way, a warning is logged.

**Credentials.** For S3 and GCS every setting below is optional; anything left unset falls through to the SDK's default chain (`AWS_*` env vars / `~/.aws` / instance roles, Application Default Credentials). Azure needs at least a connection string or an account URL; there's no default chain without one. Secrets are never logged — log lines show only the URI.

| Provider | Settings (`PINCER_` + upper-case name) |
|---|---|
| S3 | `storage_s3_endpoint_url` (MinIO/R2), `storage_s3_region`, `storage_s3_profile`, `storage_s3_access_key_id`, `storage_s3_secret_access_key`, `storage_s3_session_token` |
| Azure | `storage_azure_connection_string`, **or** `storage_azure_account_url` + `storage_azure_credential` (account key or SAS token). With only the account URL, Pincer uses `DefaultAzureCredential` (managed / workload identity, `az login`, `AZURE_*` env vars), from the `azure-identity` package the extra installs. |
| GCS | `storage_gcs_credentials_file` (service-account JSON), `storage_gcs_project` |

**Caching.** There are two layers, no more:

1. **cloudpathlib's file cache** (`skills_cache_dir/objects/<bucket>/<key>`). Every read of a remote file goes through it and checks the bucket first; a file is downloaded again only when the object is newer. Reference files are therefore always current.
2. **The dashboard API's rescan interval.** `/api/skills` serves the process's index for at most `skills_index_cache_ttl_seconds` (≤ 60) before rescanning, and immediately after any index change in any process.

The skill index itself is state, not a cache: one per process, shared by the agent, its tools and the dashboard API. It changes on startup, on `pincer skills rebuild` (applied by the watcher), and when the dashboard's rescan finds different skills (which it announces to other processes in turn). The watcher only reacts to announced changes; it doesn't rescan the bucket on its own. So outside the dashboard, run `pincer skills rebuild` after changing SKILL.md files.

**Scripts.** `run_skill_script` on a remote skill first brings the skill's folder in cloudpathlib's cache up to date: changed files are downloaded again, and files deleted from the bucket are removed. It then runs the script from there, through the same approval gate and sandbox as a local script. There is no separate copy, so editing only a script takes effect on the next run. Object storage keeps no file modes, so non-Python scripts get the owner execute bit restored. If a script changed its own cached files, they are replaced from the bucket on the next run. Avoid `pincer skills rebuild` (or `POST /api/skills/rebuild`) while a remote skill script is running: clearing the cache removes the files that script runs from. Use `--keep-cache` / `?keep_cache=true` if you only need a rescan.

---

## Bundled Starter Skills

Pincer ships five pure-instruction starter skills documenting its own capabilities: `skill-authoring` (this guide, in agent-readable form), `memory-recall`, `mcp-server-setup`, `scheduler-briefings`, and `doctor-troubleshooting`. Read any of them under `src/pincer/skills/` for a second example of the format.

---

## Migrating from the Legacy Format

Prior to this migration, skills used a `manifest.json` + `skill.py` pair with Python-decorated tool functions, a security scanner that gated loading, and mutual exclusion with MCP. That format is removed — there is no automatic converter. To port an old skill:

1. Create `skills/<name>/SKILL.md`. Move `manifest.json`'s `description` into the frontmatter `description` field; write a markdown body explaining what the skill does (this replaces the old per-tool `tools[]` schema entries — the model now reads instructions instead of calling a fixed function signature).
2. If `skill.py` did real work (an API call, a computation), move it into `skills/<name>/scripts/` as a standalone script invoked via `run_skill_script`, rather than a function called directly by the tool registry.
3. Drop `permissions`/`env_required` from the old manifest — the new sandbox model uses `SandboxConfig.allowed_domains`/`allowed_env_vars` passed by the caller, not a self-declared manifest field.
4. Remove any dependency on the old `pincer skills` CLI (`list`/`install`/`create`/`scan`/`remove`/`info` are all gone) — skills are discovered from their root, no install step. The only `pincer skills` subcommand now is `rebuild`, which rescans.
