---
tags: [spec, setup, cli]
---

# Setup Wizard Specification

## 1. Overview

The setup wizard is an interactive CLI tool that guides a first-time user through configuring all required services (Discord, Claude) and writing the config files needed to run agent-queue. It is idempotent — running it again pre-fills all prompts from existing config, skipping steps that are already satisfied.

## Source Files

- `src/setup_wizard.py`

---

## 2. Entry Point

The wizard runs via `./setup.sh` (which handles venv and dependencies) or directly via `python src/setup_wizard.py`. The `main()` function orchestrates seven sequential steps that produce a [[specs/config]] file:

1. Load existing configuration (pre-fill defaults)
2. Step 1: Workspace & Database directories
3. Step 2: Discord bot setup
4. Step 3: Agent configuration (Claude Code)
5. Step 4: Scheduling & budget
6. Step 5: Write config files
7. Step 6: Connectivity summary
8. Step 7: Launch daemon

There is no chat-provider step. The wizard writes a fixed `llm:` block
(`provider: anthropic`, `default_class: fast-medium`) to `config.yaml` — see
§9 below — and leaves any further tuning of the direct LLM path (`docs/specs/config.md` §4.6) to manual config editing.

If an existing `~/.agent-queue/config.yaml` or `~/.agent-queue/.env` is found, values are loaded and used as defaults throughout.

---

## 3. UI Helpers

The wizard uses ANSI color codes for terminal formatting:

- `banner()` — prints a bordered title box
- `step_header(num, title)` — prints a numbered step heading
- `success(msg)` — green checkmark prefix
- `warn(msg)` — yellow exclamation prefix
- `error(msg)` — red X prefix
- `info(msg)` — dim text
- `prompt(label, default)` — text input with optional default shown in brackets; returns default if input is empty
- `prompt_yes_no(label, default)` — Y/n or y/N prompt; returns bool
- `prompt_secret(label, existing)` — uses `getpass` for hidden input; if an existing value is provided, shows a masked preview (first 4 + last 4 chars if length > 12, otherwise `****`)

---

## 4. Incremental .env Saving

`_save_env_value(key, value)` writes a single key=value pair to `~/.agent-queue/.env`:

- Skips if value is empty
- Creates the directory and file if they don't exist
- If the key already exists in the file, updates it in place
- If the key is new, appends it
- Sets file permissions to `0o600` after every write
- Values are saved incrementally during each step, not batched at the end — this means partial progress is preserved if the wizard is interrupted

---

## 5. Loading Existing Config

`_load_existing_config()` returns a dict with pre-existing values:

- Reads `~/.agent-queue/.env`: parses `KEY=VALUE` lines (skips comments and lines without `=`)
- Reads `~/.agent-queue/config.yaml`:
  - Tries `yaml.safe_load` first (if PyYAML is available)
  - Falls back to a simple line parser that handles 0-indent top-level keys, 1-indent section keys, and 2-indent subsection keys
- Stores the parsed YAML under the `_yaml` key in the returned dict
- Environment variables from `.env` are stored as top-level keys (e.g., `DISCORD_BOT_TOKEN`)

---

## 6. Step 1: Workspace & Database

`step_directories(existing)` configures two paths:

- **Workspace directory** — where agent working copies live (default: `~/agent-queue-workspaces`)
- **Database path** — SQLite file location (default: `~/.agent-queue/agent-queue.db`)

**Skip logic:** The step is skipped entirely (no prompts) if either:
- Both `WORKSPACE_DIR` and `DATABASE_PATH` are already saved in config/env, OR
- The default directories already exist on disk

When skipped, directories are created silently with `os.makedirs(exist_ok=True)`.

When prompted, values are saved to `.env` immediately via `_save_env_value`.

---

## 7. Step 2: Discord Bot

`step_discord(existing)` collects Discord credentials and verifies connectivity.

### Bot Token
- If already saved in `.env`, uses the existing value without prompting
- Otherwise, prints step-by-step instructions for creating a Discord bot (developer portal URL, intents, OAuth scopes, permissions)
- Collects token via `prompt_secret` (hidden input)
- Saves to `.env` immediately
- Required — exits with `sys.exit(1)` if empty

### Guild ID
- If already saved in config, uses existing value
- Otherwise, prints instructions for finding the guild ID (Developer Mode → Copy Server ID)
- Saves to `.env` immediately
- Required — exits with `sys.exit(1)` if empty

### Channel Names
Pre-filled from existing config with defaults:
- `control` → `"control"`
- `notifications` → `"notifications"`
- `agent_questions` → `"agent-questions"`

### Connectivity Test
`_test_discord(token, guild_id, channels)` verifies the bot can connect:

- Creates a temporary `discord.Client` with `message_content` intent
- Connects with a 15-second timeout
- Checks the guild is visible to the bot
- Verifies all configured channel names exist as text channels in the guild
- Returns `(success, missing_channel_names)`
- Handles specific errors: `LoginFailure` (bad token), `PrivilegedIntentsRequired` (missing Message Content Intent), `TimeoutError`
- If `discord.py` is not installed, returns `(False, [])` with a warning

**Retry loop:** On failure, the wizard offers different recovery paths:
- If the bot connected but channels are missing: offers to update channel names, then retests
- If connection-level failure: shows debugging tips, offers to retry

### Authorized Users
Optional — prompts for Discord user IDs one per line (empty line to finish).

### Per-Project Channels
`_step_per_project_channels(existing, discord_ok)` configures automatic channel creation:

- Asks whether to enable auto-creation (default from existing config, or False)
- If declined: shows manual channel management commands (`/create-channel`, `/set-channel`, `/channel-map`) and returns disabled config
- If enabled:
  - Collects naming convention (must contain `{project_id}`, resets to default if missing)
  - Collects optional Discord category name for organizing project channels
  - Asks whether channels should be private (default: yes) — private channels are only visible to the bot and users granted access
  - Shows summary of configured settings

Returns a dict with `auto_create`, `naming_convention`, `category_name`, and `private`.

---

## 8. Step 3: Agent Configuration

`step_agents(existing)` configures Claude Code agent credentials.

### Claude CLI Detection
`_check_claude_cli()` checks:
1. Whether `claude` is in PATH (via `shutil.which`)
2. Whether OAuth credentials exist at `~/.claude/.credentials.json` or `~/.claude/credentials.json`

Returns `(is_installed, is_authenticated)`.

### Authentication Flow
Priority order:
1. If already logged in via `claude login` (credentials file exists) — use that, skip prompts
2. Otherwise, test SDK connectivity with existing API key (if any) via `_test_claude_sdk`
3. If no credentials found, offer three choices:
   - **[1] Log in** — runs `claude login` as a subprocess, rechecks credentials afterward
   - **[2] API key** — collects via `prompt_secret`, saves to `.env`, tests connectivity with retry loop
   - **[3] Skip** — defer to later manual configuration

### SDK Connectivity Test
`_test_claude_sdk(api_key)` tries multiple backends in order:
1. **Direct API** — if an API key is provided, creates `anthropic.Anthropic(api_key=...)` and sends a minimal test message (`"Say ok"`, `max_tokens=16`, model `claude-sonnet-4-20250514`)
2. **Vertex AI** — checks for `GOOGLE_CLOUD_PROJECT` or `ANTHROPIC_VERTEX_PROJECT_ID` env vars; uses `AnthropicVertex` client with model `claude-sonnet-4@20250514`
3. **AWS Bedrock** — checks for `AWS_REGION` or `AWS_DEFAULT_REGION`; uses `AnthropicBedrock` client

Returns `(success, backend_name)`. If all fail, shows per-backend error details.

### Agent SDK Test
`_test_claude_agent_sdk()` verifies the `claude_agent_sdk` package:
- Imports `query` and `ClaudeAgentOptions`
- Constructs a minimal `ClaudeAgentOptions(allowed_tools=["Read"])` — no network call
- Returns True on success, False with install instructions on `ImportError`

### Model Selection
Prompts for model name with default from existing config or `claude-sonnet-4-20250514`.

---

## 9. Step 4: Scheduling & Budget

> **Removed: the Chat Provider step.** The wizard no longer has a
> `step_chat_provider` step or a Discord-chat-specific LLM selection —
> there is no in-process chat agent to configure an LLM for (the Supervisor
> was deleted; see `docs/superpowers/specs/2026-08-30-llm-direct-path-design.md`).
> `step_write_config` (§11 below) writes a fixed `llm:` block
> (`provider: anthropic`, `default_class: fast-medium`); provider/model/`base_url`
> tuning — including pointing it at Ollama via `openai` + `base_url` — is a
> manual edit to `config.yaml`, documented in [[specs/config]] §4.6.

`step_scheduling(existing)` configures token budgets and retry behavior.

### Global Token Budget
Optional — asks whether to set a daily limit. If yes, prompts for a number.

### Scheduling/Retry Defaults
Asks whether to customize (default: no). If yes, prompts for:
- **Rolling window hours** (default: 24)
- **Rate-limit backoff seconds** (default: 60)
- **Token-exhaustion retry seconds** (default: 300)

If customization is declined, shows the default values being used.

---

## 10. Step 5: Write Config Files

`step_write_config(workspace, db_config, discord_cfg, agents_cfg, sched_cfg)` generates both config files.

### .env File
Written to `~/.agent-queue/.env` with mode `0o600`:
- Always includes `DISCORD_BOT_TOKEN`
- Includes `ANTHROPIC_API_KEY` only if the key was entered during setup (not if it came from the environment)

### config.yaml File
Written to `~/.agent-queue/config.yaml`. Includes:
- `workspace_dir` and `database_path`
- `discord` section: bot token as `${DISCORD_BOT_TOKEN}`, guild ID (quoted), channel names, optional authorized users, optional per-project channel config (includes `private` flag)
- `llm` section: fixed `provider: anthropic`, `default_class: fast-medium` (see note in §9 above)
- `global_token_budget_daily`: only written if set
- `scheduling` section: rolling window hours, min_task_guarantee always true
- `pause_retry` section: rate limit backoff, token exhaustion retry

### Shell Profile Integration
`_offer_shell_env(env_path)` offers to add `.env` sourcing to the user's shell profile:
- Detects shell from `$SHELL` env var: zsh → `.zshrc`, otherwise → `.bashrc`
- Source line: `set -a; source "<path>"; set +a`
- Skips if the line is already present in the profile
- If accepted, appends with a comment header

---

## 11. Step 6: Connectivity Summary

`step_test_connectivity(discord_cfg, agents_cfg)` prints a status summary:
- Discord: connected or not verified
- Claude API: connected with backend name, or not verified
- claude-agent-sdk: installed or not

No retries or recovery at this step — purely informational.

---

## 12. Step 7: Launch Daemon

`step_launch(config_path)` offers to start the daemon:
- Default: no (the user must opt in)
- If yes: runs `agent-queue <config_path>` as a background process (`start_new_session=True`), logging stdout/stderr to `~/.agent-queue/daemon.log`
- Shows the PID and instructions for stopping/viewing logs
- If no: shows the command to run later

---

## 13. Idempotency and Interruption Safety

The wizard is designed to be safely re-run:
- Existing values pre-fill all prompts
- Steps with satisfied preconditions are skipped (e.g., directories step)
- Secrets are saved incrementally via `_save_env_value`, so partial progress survives interruption
- The final config write overwrites previous config files entirely
- No destructive operations — existing workspaces, databases, and repos are never deleted
