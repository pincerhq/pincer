# Pincer Tools Catalog

This is the complete, authoritative catalog of every tool Pincer can call. It is generated from the source of the tool registry — the numbers below match what `pincer mcp tools` and `/tools` will report on a fully-configured install.

## Summary

| Category | Tools | Module | How to enable |
|---|---:|---|---|
| **Core built-ins** (incl. `generate_image`, `make_phone_call`) | 23 | `src/pincer/tools/builtin/` + `src/pincer/cli.py` | Always on (voice/image need their API key) |
| **Bundled skills** | 27 | `skills/` (11 skills) | Always on (bundled) |
| **Google Workspace** | 113 | `src/pincer/integrations/google/` | `pincer setup-google` |
| **Microsoft 365** | 69 | `src/ms365-mcp/ms365/` | `ms365-mcp-setup` |
| **Slack (native)** | 71 | `src/pincer/integrations/slack/` | `PINCER_SLACK_BOT_TOKEN` |
| **MCP servers (external)** | unlimited | `src/pincer/mcp/` | `pincer.toml` `[[mcp.servers]]` |
| **Custom skills** | unlimited | `pincer skills install` | AST-scanned + sandboxed |
| **Native tools (first-party)** | **303** | — | — |
| **Available out of the box with popular MCP servers** | **600+** | — | — |

> **"600+"** refers to the practical total when Pincer is paired with the most common MCP servers (GitHub, Postgres, Filesystem, Notion, Linear, Stripe, etc.) plus the 303 native Pincer tools. Pincer itself ships 303 first-party tools; MCP multiplies that arbitrarily.

---

## Core built-ins (23)

Registered in `src/pincer/cli.py::_run_agent()`. These are always available.

| Tool | Approval | Description |
|---|:---:|---|
| `shell_exec` | **Yes** | Run shell commands in a sandboxed subprocess |
| `python_exec` | **Yes** | Execute Python code in an isolated sandbox |
| `file_read` | No | Read a file from the workspace |
| `file_write` | **Yes** | Write content to a file in the workspace |
| `file_list` | No | List files in a workspace directory |
| `browse` | No | Navigate a URL and extract text (Playwright) |
| `screenshot` | No | Screenshot a web page (Playwright) |
| `email_check` | No | Check unread emails in the inbox |
| `email_send` | **Yes** | Send an email |
| `email_search` | No | Search emails by query string |
| `email_read` | No | Read a specific email by ID |
| `email_list_folders` | No | List available mail folders |
| `email_mark` | No | Mark email read / unread / starred |
| `email_move` | No | Move email to another folder |
| `email_trash` | No | Move email to Trash |
| `email_empty_folder` | **Yes** | Empty Spam / Trash folder |
| `calendar_today` | No | Today's calendar events |
| `calendar_week` | No | This week's calendar events |
| `calendar_create` | No | Create a calendar event |
| `send_file` | No | Send a file to the current channel |
| `send_image` | No | Send an image to the current channel |
| `generate_image` | No | Generate an image via fal.ai or Gemini |
| `make_phone_call` | **Yes** | Place an outbound call via Twilio (when voice is enabled) |

---

## Bundled skills (27 tool functions across 11 skills)

Loaded from `skills/` at startup. Each runs in a subprocess sandbox with a permission-declaring `manifest.json`.

| Skill | Tools | Permissions |
|---|---|---|
| `weather` | `get_weather`, `get_forecast` | network |
| `news` | `get_headlines`, `search_news`, `read_rss` | network |
| `translate` | `translate_text`, `list_languages` | network |
| `summarize_url` | `summarize_url` | network |
| `youtube_summary` | `get_transcript` | network |
| `stock_price` | `get_stock_price`, `get_crypto_price` | network |
| `expense_tracker` | `log_expense`, `expense_report` | filesystem |
| `habit_tracker` | `add_habit`, `checkin`, `habit_status` | filesystem |
| `pomodoro` | `start_pomodoro`, `pomodoro_stats` | filesystem |
| `git_status` | `repo_status`, `recent_commits` | shell |
| `phone_contacts` | `add_contact`, `search_contacts`, `list_contacts`, `update_contact`, `delete_contact` | filesystem |

---

## Google Workspace (113 tools)

Registered when the user completes `pincer setup-google`. All tools are prefixed `google__`.

### Gmail (19)

`google__list_labels` · `google__list_messages` · `google__search_messages` · `google__get_message` · `google__get_thread` · `google__get_attachment` · `google__send_message` · `google__reply_to_message` · `google__reply_all` · `google__forward_message` · `google__create_draft` · `google__send_draft` · `google__trash_message` · `google__untrash_message` · `google__mark_as_read` · `google__mark_as_unread` · `google__add_label` · `google__remove_label` · `google__create_label`

### Google Calendar (12)

`google__list_calendars` · `google__list_events` · `google__get_event` · `google__search_events` · `google__check_freebusy` · `google__create_event` · `google__update_event` · `google__delete_event` · `google__move_event` · `google__accept_event` · `google__decline_event` · `google__add_google_meet`

### Google Drive (16)

`google__list_drive_files` · `google__search_drive_files` · `google__get_file_metadata` · `google__download_file` · `google__export_google_doc` · `google__list_shared_drives` · `google__get_file_permissions` · `google__list_recent_files` · `google__list_local_files` · `google__upload_file` · `google__create_folder` · `google__move_file` · `google__rename_file` · `google__copy_file` · `google__trash_file` · `google__share_file`

### Google Docs (8)

`google__get_doc_content` · `google__get_doc_structure` · `google__create_doc` · `google__insert_text` · `google__replace_text` · `google__insert_table` · `google__update_paragraph_style` · `google__add_comment`

### Google Sheets (10)

`google__list_sheets` · `google__get_sheet_values` · `google__get_sheet_metadata` · `google__search_sheet_values` · `google__create_spreadsheet` · `google__update_sheet_values` · `google__append_sheet_values` · `google__clear_sheet_values` · `google__add_sheet` · `google__format_cells`

### Google Slides (6)

`google__list_slides` · `google__get_slide_content` · `google__create_presentation` · `google__add_slide` · `google__update_slide_text` · `google__add_image_to_slide`

### Google Tasks (8)

`google__list_task_lists` · `google__list_tasks` · `google__get_task` · `google__create_task` · `google__update_task` · `google__complete_task` · `google__delete_task` · `google__create_task_list`

### Google Contacts (7)

`google__list_contacts` · `google__search_contacts` · `google__get_contact` · `google__create_contact` · `google__update_contact` · `google__delete_contact` · `google__list_contact_groups`

### Google Meet (27)

Full Meet REST v2 surface — spaces, conference records, participants, recordings, transcripts, smart notes, event subscriptions.

`google__create_meet_space` · `google__get_meet_space` · `google__update_meet_space` · `google__end_active_conference` · `google__configure_meet_moderation` · `google__configure_meet_artifacts` · `google__add_meet_member` · `google__remove_meet_member` · `google__list_conference_records` · `google__get_conference_record` · `google__list_conference_participants` · `google__get_participant_details` · `google__list_participant_sessions` · `google__list_meet_recordings` · `google__get_meet_recording` · `google__download_meet_recording` · `google__check_recording_status` · `google__list_meet_transcripts` · `google__get_meet_transcript` · `google__list_transcript_entries` · `google__get_transcript_entry` · `google__summarize_meet_transcript` · `google__list_smart_notes` · `google__get_smart_notes` · `google__subscribe_meet_events` · `google__list_meet_subscriptions` · `google__delete_meet_subscription`

---

## Microsoft 365 (69 tools)

Available via the standalone `ms365-mcp` server. Run `ms365-mcp-setup` once to authenticate; a token is cached at `~/.pincer/ms365_mcp/default_token_cache.json` (encrypted at rest if `MS365_TOKEN_ENCRYPTION_KEY` is set, plaintext otherwise).

### Outlook Email (17) — `outlook__`

`list_mail_folders` · `list_messages` · `search_messages` · `get_message` · `get_message_attachments` · `download_attachment` · `send_message` · `reply_to_message` · `reply_all_to_message` · `forward_message` · `create_draft` · `update_draft` · `send_draft` · `move_message` · `delete_message` · `mark_as_read` · `flag_message`

### Outlook Calendar (12) — `outlook__`

`list_calendars` · `list_events` · `get_event` · `search_events` · `check_availability` · `create_event` · `update_event` · `delete_event` · `accept_event` · `decline_event` · `tentative_event` · `create_online_meeting`

### OneDrive (14) — `onedrive__`

`list_drive_items` · `search_files` · `get_file_metadata` · `download_file` · `get_file_preview` · `list_shared_with_me` · `list_recent_files` · `upload_file` · `create_folder` · `move_file` · `rename_file` · `copy_file` · `delete_file` · `share_file`

### Microsoft To Do (8) — `ms_todo__`

`list_task_lists` · `list_tasks` · `get_task` · `create_task` · `update_task` · `complete_task` · `delete_task` · `create_task_list`

### Microsoft Teams (7) — `teams__`

`list_teams` · `list_channels` · `list_channel_messages` · `get_channel_message` · `send_channel_message` · `list_chats` · `send_chat_message`

### Outlook Contacts (6) — `outlook__`

`list_contacts` · `search_contacts` · `get_contact` · `create_contact` · `update_contact` · `delete_contact`

### OneNote (5) — `onenote__`

`list_notebooks` · `list_sections` · `list_pages` · `get_page_content` · `create_page`

---

## Slack (native, 71 tools)

Registered when `PINCER_SLACK_BOT_TOKEN` (and optionally `PINCER_SLACK_USER_TOKEN`) are set. All tools are prefixed `slack__`.

### Messages (18)

`get_channel_history` · `get_thread_replies` · `get_message_permalink` · `post_message` · `post_threaded_reply` · `post_ephemeral` · `update_message` · `delete_message` · `schedule_message` · `list_scheduled_messages` · `delete_scheduled_message` · `post_message_with_blocks` · `share_message` · `post_dm` · `post_broadcast_reply` · `get_unread_messages` · `mark_channel_read` · `summarize_channel`

### Channels & DMs (16)

`list_channels` · `get_channel_info` · `list_channel_members` · `create_channel` · `archive_channel` · `unarchive_channel` · `rename_channel` · `set_channel_topic` · `set_channel_purpose` · `join_channel` · `leave_channel` · `invite_to_channel` · `kick_from_channel` · `open_dm` · `open_group_dm` · `list_dm_conversations`

### Users & Groups (10)

`list_users` · `get_user_info` · `get_user_by_email` · `set_user_status` · `get_user_presence` · `list_user_groups` · `get_user_group_members` · `create_user_group` · `update_user_group` · `disable_user_group`

### Files (10)

`list_files` · `get_file_info` · `download_file` · `upload_file` · `share_file` · `delete_file` · `list_remote_files` · `add_remote_file` · `update_remote_file` · `remove_remote_file`

### Reactions & Emoji (5)

`add_reaction` · `remove_reaction` · `get_message_reactions` · `list_reactions` · `list_emoji`

### Pins, Bookmarks, Reminders, Search (12)

`pin_message` · `unpin_message` · `list_pins` · `add_bookmark` · `list_bookmarks` · `remove_bookmark` · `create_reminder` · `list_reminders` · `complete_reminder` · `delete_reminder` · `search_messages` (user token) · `search_files` (user token)

---

## MCP — External Tool Servers (unlimited)

Pincer is a full **MCP 1.x client** and **MCP OAuth 2.0 server**. Connect any MCP-compliant server and its tools appear in the agent with a `serverName__` prefix (configurable).

### Manage MCP tools

```bash
pincer mcp list                          # connected servers + status
pincer mcp tools                         # every MCP tool registered
pincer mcp test <server>                 # smoke-test a server
pincer mcp call <server> <tool> --arg v  # call a specific tool
```

### Popular MCP servers you can plug in

| Package | Tools (approx.) | Use case |
|---|---:|---|
| `@modelcontextprotocol/server-github` | ~35 | Issues, PRs, repos, files |
| `@modelcontextprotocol/server-filesystem` | ~12 | Local filesystem |
| `@modelcontextprotocol/server-postgres` | ~5 | Postgres queries |
| `@modelcontextprotocol/server-brave-search` | ~2 | Web search |
| `@modelcontextprotocol/server-puppeteer` | ~7 | Headless browser |
| `@modelcontextprotocol/server-slack` | ~10 | Community Slack server |
| `notion-mcp` | ~20 | Notion database ops |
| `linear-mcp` | ~15 | Linear issues, cycles, projects |
| `stripe-mcp` | ~25 | Payments, customers, subscriptions |
| `sentry-mcp` | ~10 | Error tracking |

> Install any server via `pincer.toml`:
>
> ```toml
> [[mcp.servers]]
> name = "github"
> command = "npx"
> args = ["-y", "@modelcontextprotocol/server-github"]
> env = { GITHUB_PERSONAL_ACCESS_TOKEN = "ghp_..." }
> ```

### OAuth 2.0 server

Pincer exposes its **own** tools to MCP clients via an embedded OAuth 2.0 Authorization Server — RFC 8414 metadata, PKCE, JWT, scope enforcement. See `docs/mcp-guide.md`.

---

## Custom skills (unlimited)

Any Python file + `manifest.json` in `skills/` is loaded at startup after an AST scan. Community skills can be installed via:

```bash
pincer skills install github:user/repo
```

Skills run in a subprocess sandbox with declared permissions only (`network`, `filesystem`, `shell`). See `docs/Skills guide.md`.

---

## Tool filtering & cost control

With 300+ tools registered, LLM tool selection can degrade. Pincer protects against this with:

- **Tool groups** — enable only the tools you need per channel via `pincer.toml`
- **Per-tool approval prompts** — destructive tools require ✅ in chat
- **Warning at >100 MCP tools** — `pincer run` warns if a single MCP server registers too many tools
- **Daily budget cap** — `PINCER_DAILY_BUDGET_USD` stops spend regardless of tool count
- **Audit log** — every tool call captured in JSONL at `~/.pincer/audit.log`

---

## Verifying your install

```bash
pincer doctor         # 40+ checks, including tool registry integrity
pincer mcp tools      # list every MCP tool currently registered
```

In a chat session, send `/tools` to list every tool available in the current context.
