# Built-in Tools

> **Source**: `src/pincer/tools/builtin/`

Pincer ships with a set of built-in tools that give the agent real-world capabilities.

---

## Web Search

> **Source**: `src/pincer/tools/builtin/web_search.py`

Search the web for current information.

**Parameters:**
- `query` (string, required) — The search query
- `num_results` (integer, default 5) — Number of results (1-10)

**Provider hierarchy:**
1. **Tavily** (if `PINCER_TAVILY_API_KEY` is set) — Rich results with answer summaries
2. **DuckDuckGo** (fallback) — No API key needed, uses `duckduckgo-search` library

**Output format:**
```
**Summary:** [Tavily answer]

1. **Title**
   Snippet text...
   URL: https://...

2. **Title**
   Snippet text...
   URL: https://...
```

---

## Shell Execution

> **Source**: `src/pincer/tools/builtin/shell.py`

Execute shell commands on the host machine.

**Parameters:**
- `command` (string, required) — The shell command to run
- `workdir` (string, default "~") — Working directory

**Safety controls:**

| Control | Description |
|---------|-------------|
| Enable/disable | `PINCER_SHELL_ENABLED` (default: true) |
| Timeout | `PINCER_SHELL_TIMEOUT` (default: 30s, max 300s) |
| Approval | `PINCER_SHELL_REQUIRE_APPROVAL` (default: true) |
| Blocked patterns | Regex-based dangerous command detection |

**Blocked command patterns:**

| Pattern | Description |
|---------|-------------|
| `rm -rf /` | Recursive root delete |
| `dd if=` | Disk duplication |
| Fork bomb | `:(){ :\|:& };:` |
| `mkfs` | Filesystem creation |
| `format` | Disk formatting |
| `> /dev/sd*` | Direct disk write |
| `chmod 777 /` | Insecure root permissions |
| `shutdown` / `reboot` | System power control |
| `curl \| sh` / `wget \| sh` | Remote code execution |

**Output:** Includes STDOUT, STDERR, and exit code. Truncated at 4000 characters.

---

## File Operations

> **Source**: `src/pincer/tools/builtin/files.py`

All file operations are **sandboxed** to `~/.pincer/workspace/`. Any attempt to escape the sandbox raises a `ValueError`.

### `file_read`

Read a file's content (max 100KB).

**Parameters:**
- `path` (string, required) — File path relative to workspace or absolute within sandbox

### `file_write`

Write content to a file (creates or overwrites).

**Parameters:**
- `path` (string, required) — File path within workspace
- `content` (string, required) — Text content to write

### `file_list`

List files in a directory (hidden files excluded).

**Parameters:**
- `directory` (string, default ".") — Directory path within workspace

**Sandbox enforcement:**
```python
def _sandbox_path(path_str: str) -> Path:
    workspace = settings.data_dir / "workspace"
    target = (workspace / path_str).resolve()
    if not str(target).startswith(str(workspace.resolve())):
        raise ValueError(f"Access denied: path is outside workspace")
    return target
```

---

## Browser Tools

> **Source**: `src/pincer/tools/builtin/browser.py`

Requires `pip install 'pincer-agent[browser]'` (Playwright).

### `browse`

Navigate to a URL and return the page's readable text content.

**Parameters:**
- `url` (string, required) — Full URL to navigate to

**Behavior:**
- Launches headless Chromium (auto-installs on first use)
- Waits for DOM content loaded
- Strips HTML tags, scripts, and styles
- Returns title + URL + extracted text (max 6000 chars)

### `screenshot`

Take a screenshot of a web page.

**Parameters:**
- `url` (string, required) — Full URL to screenshot

**Behavior:**
- Viewport: 1280x900
- Saves PNG to `~/.pincer/workspace/screenshot_{timestamp}.png`
- Returns file path and base64 preview

**Browser lifecycle:** A single shared browser instance is reused across calls and closed on agent shutdown.

---

## Python Execution

> **Source**: `src/pincer/tools/builtin/python_exec.py`

Execute Python code in an isolated subprocess.

**Parameters:**
- `code` (string, required) — Python code to execute
- `timeout` (integer, default 30, max 120) — Execution timeout in seconds

**Isolation:**
- Runs in a separate subprocess (`asyncio.create_subprocess_exec`)
- Working directory: `~/.pincer/workspace/exec_output/`
- AWS credentials are stripped from the environment
- Output capped at 8000 characters

**Matplotlib integration:**
- Automatically patches `plt.show()` to save figures as PNG files
- Auto-saves any unsaved figures after code execution
- Figures saved to `~/.pincer/workspace/exec_output/plot_N.png`

**Output includes:**
- STDOUT
- STDERR (prefixed with `[stderr]`)
- List of newly created files (`[File saved: /path/to/file]`)

---

## Voice Transcription

> **Source**: `src/pincer/tools/builtin/transcribe.py`

Transcribe voice notes using OpenAI's Whisper API.

**Not a tool** — this is called internally when a voice message is received. It requires `PINCER_OPENAI_API_KEY`.

**Supported formats:**

| MIME Type | Extension |
|-----------|-----------|
| `audio/ogg`, `audio/oga`, `audio/opus` | `.ogg` |
| `audio/mp3`, `audio/mpeg` | `.mp3` |
| `audio/wav`, `audio/x-wav` | `.wav` |
| `audio/mp4` | `.mp4` |
| `audio/m4a` | `.m4a` |
| `audio/webm` | `.webm` |
| `audio/flac` | `.flac` |

**Process:**
1. Receive audio bytes from channel
2. Wrap in `BytesIO` with appropriate extension
3. Call `client.audio.transcriptions.create(model="whisper-1")`
4. Return transcribed text (language auto-detected)

---

## `send_file`

Send a file to the user as a document attachment. Defined inline in `cli.py`.

**Parameters:**
- `path` (string, required) — Absolute path to the file to send
- `caption` (string, optional) — Caption for the file

Uses the `context` parameter to determine the user's channel and send via the appropriate channel's `send_file()` method.

---

## `send_image`

Send an image or GIF inline in the chat. Defined inline in `cli.py`.

**Parameters:**
- `url` (string, required) — Direct URL to the image or GIF
- `caption` (string, optional) — Caption for the image

Detects GIF URLs (by extension or domain like Giphy/Tenor) and uses `send_animation()` instead of `send_photo()`.
