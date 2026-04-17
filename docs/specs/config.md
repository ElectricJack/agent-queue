---
tags: [spec, config]
---

# Config Module Specification

## 1. Overview

The config module is responsible for loading, parsing, and structuring the application's runtime configuration. It reads a single YAML file from a caller-supplied path, loads an optional `.env` file from the same directory, performs environment variable substitution on all string values, and returns a fully populated `AppConfig` dataclass instance.

The module never writes configuration, never searches for the config file itself, and never falls back to alternative paths. All of that is the caller's responsibility. The module's only public entry point is the `load_config(path: str) -> AppConfig` function.

## Source Files
- `src/config.py`

---

## 2. Config File Location and Format

### File Format

The config file must be valid YAML. It is parsed with `yaml.safe_load`, which means only standard YAML scalar types, mappings, and sequences are accepted. An empty file or a file that produces `None` after parsing is treated as an empty mapping and does not raise an error.

### File Location

The config file path is passed directly to `load_config`. There is no automatic discovery. If the file does not exist at the given path, `load_config` raises `FileNotFoundError` immediately, before any other processing.

### .env Sidecar File

Before reading the YAML, `load_config` attempts to load a `.env` file located in the **same directory** as the config file. The `.env` path is constructed as `os.path.join(os.path.dirname(config_path), ".env")`.

If no `.env` file exists at that path, the step is silently skipped.

---

## 3. Environment Variable Substitution

### .env File Loading

The `.env` file is a line-oriented key-value file. Processing rules:

- Lines are stripped of leading and trailing whitespace before evaluation.
- Empty lines (after stripping) are skipped.
- Lines beginning with `#` (after stripping) are treated as comments and skipped.
- Lines that do not contain `=` are skipped.
- Each qualifying line is split into a key and a value using `partition("=")`: the key is everything before the first `=`, the value is everything after. Both key and value are stripped of leading and trailing whitespace after splitting.
- If the resulting key is an empty string, the entry is skipped.
- A key is only written into `os.environ` if it does **not** already exist there. Existing environment variables are never overwritten by the `.env` file.

### YAML Value Substitution

After the YAML is parsed into a Python object (dict/list/scalar), every string value in the entire structure is scanned for `${VAR_NAME}` placeholders using the pattern `\$\{(\w+)\}`.

- The substitution is recursive: it descends into nested dicts and lists.
- Non-string values (integers, booleans, `None`, etc.) are passed through unchanged.
- For each placeholder found in a string, the variable name is looked up with `os.environ.get(var_name)`.
- If the environment variable is not set (returns `None`), a `ValueError` is raised with the message `"Environment variable {var_name} not set"`. This is a hard error; loading stops.
- If the environment variable is set, its value replaces the placeholder text in the string. A single string may contain multiple placeholders; all are substituted.

Substitution occurs on the fully parsed YAML structure before any config section is mapped to dataclasses.

---

## 4. Config Sections

### 4.1 Top-Level Fields

These fields appear at the root of the YAML document and map directly to scalar fields on `AppConfig`.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `workspace_dir` | `str` | `~/agent-queue-workspaces` (home-expanded at class instantiation time) | Filesystem path to the directory where agent workspaces are stored. |
| `database_path` | `str` | `~/.agent-queue/agent-queue.db` (home-expanded at class instantiation time) | Filesystem path to the SQLite database file. Legacy field — prefer using the `database` section for new setups. |
| `global_token_budget_daily` | `int` or `None` | `None` | Daily token budget across all agents. `None` means no global cap is enforced. |

### 4.1.1 `database` Section

Maps to `DatabaseConfig`. The YAML key is `database`. This section configures the database backend. The backend (SQLite or PostgreSQL) is inferred automatically from the URL scheme.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | `""` | Database connection URL. A `postgresql://` or `postgres://` prefix selects PostgreSQL (asyncpg). Anything else is treated as a SQLite file path. |
| `pool_min_size` | `int` | `2` | Minimum connection pool size (PostgreSQL only). |
| `pool_max_size` | `int` | `10` | Maximum connection pool size (PostgreSQL only). |

**Backward compatibility:** If no `database` section is present, the top-level `database_path` field is used as the `url` value (SQLite). New installations using the setup wizard will write the appropriate section automatically.

**Examples:**

```yaml
# SQLite (default):
database_path: ~/.agent-queue/agent-queue.db

# PostgreSQL:
database:
  url: postgresql://agent_queue:mypassword@localhost:5432/agent_queue
  pool_min_size: 2
  pool_max_size: 10
```

### 4.2 `discord` Section

Maps to `DiscordConfig`. The YAML key is `discord`.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `bot_token` | `str` | `""` | Discord bot token for authentication. |
| `guild_id` | `str` | `""` | Discord server (guild) ID the bot operates in. |
| `channels` | `dict[str, str]` | `{"channel": "agent-queue", "agent_questions": "agent-questions"}` | Mapping of logical channel role names to Discord channel names. See backward-compatibility note below. |
| `authorized_users` | `list[str]` | `[]` | List of Discord usernames or IDs permitted to issue commands. |
| `per_project_channels` | nested object | See section 4.2.1 | Settings for automatic per-project Discord channel management. |

#### Backward-Compatibility Channel Merging

When the `channels` dict is loaded, a check is performed for old-style configs that used `control` and/or `notifications` as channel keys instead of the unified `channel` key.

The merging rule:

1. If the parsed `channels` dict already contains a `channel` key, no merging occurs and the dict is used as-is.
2. If `channel` is absent but either `control` or `notifications` (or both) is present:
   - The unified `channel` value is set to the value of `control` if that value is truthy (non-empty); otherwise it falls back to `notifications` (with `"agent-queue"` as the final default if `notifications` is also absent or empty).
   - The `agent_questions` key is preserved from the raw dict if present, otherwise defaults to `"agent-questions"`.
   - All other keys from the old config are discarded.

This merging only activates when `channel` is absent and at least one of `control` or `notifications` is present. Any other combination of keys (including completely custom keys) is passed through untouched.

#### 4.2.1 `per_project_channels` Sub-Section

Maps to `PerProjectChannelsConfig`. The YAML key within `discord` is `per_project_channels`.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `auto_create` | `bool` | `False` | When `True`, the bot automatically creates a Discord channel for each project. |
| `naming_convention` | `str` | `"{project_id}"` | Template string for the generated channel name. The placeholder `{project_id}` is substituted with the project's ID at runtime. |
| `category_name` | `str` | `""` | Name of the Discord category to place project channels under. Empty string means no category grouping. |
| `private` | `bool` | `True` | When `True`, auto-created channels and categories are private — the `@everyone` role is denied `view_channel`, and the bot is explicitly granted `view_channel` and `send_messages`. |

### 4.3 `agents` Section

Maps to `AgentsDefaultConfig`. The YAML key is `agents`.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `heartbeat_interval_seconds` | `int` | `30` | How often (in seconds) a running agent must emit a heartbeat to be considered alive. |
| `stuck_timeout_seconds` | `int` | `0` | Seconds without a heartbeat before an agent is declared stuck. `0` disables the timeout entirely (no stuck detection). |
| `graceful_shutdown_timeout_seconds` | `int` | `30` | Maximum seconds to wait for an agent to finish cleanly during shutdown before forcibly terminating it. |

### 4.4 `scheduling` Section

Maps to `SchedulingConfig`. The YAML key is `scheduling`.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `rolling_window_hours` | `int` | `24` | Length of the rolling window (in hours) used for credit-weight scheduling calculations. |
| `min_task_guarantee` | `bool` | `True` | When `True`, the scheduler guarantees every project receives at least one task slot even if its credit weight is proportionally very small. |

### 4.5 `pause_retry` Section

Maps to `PauseRetryConfig`. The YAML key is `pause_retry`.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `rate_limit_backoff_seconds` | `int` | `60` | Seconds to pause a task after hitting a rate limit, before the task is eligible to resume. |
| `token_exhaustion_retry_seconds` | `int` | `300` | Seconds to wait before retrying after daily token budget is exhausted. |
| `rate_limit_max_retries` | `int` | `3` | Maximum number of in-process retries for rate limit errors before the task is paused at the orchestrator level. |
| `rate_limit_max_backoff_seconds` | `int` | `300` | Maximum backoff duration (in seconds) for exponential in-process retry, capping the growth of the delay. |

### 4.6 `chat_provider` Section

Maps to `ChatProviderConfig`. The YAML key is `chat_provider`.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `provider` | `str` | `"anthropic"` | LLM provider to use for the chat agent. Valid values are `"anthropic"`, `"ollama"`, and `"gemini"`. |
| `model` | `str` | `""` | Model name to use. An empty string means the provider's own default model is used. |
| `base_url` | `str` | `""` | Base URL for the provider's API endpoint. Primarily used for Ollama installations. Empty string means the provider SDK's default URL is used. |
| `api_key` | `str` | `""` | API key for the provider. Currently used by Gemini (falls back to `GEMINI_API_KEY` or `GOOGLE_API_KEY` env vars). |
| `keep_alive` | `str` | `"1h"` | Ollama only: how long to keep the model loaded in memory after the last request. Accepts Go-style durations (`"1h"`, `"30m"`, `"-1"` for infinite, `"0"` for immediate unload). |
| `num_ctx` | `int` | `0` | Ollama only: context window size override. `0` means use the model's default. |

**Note:** The `__post_init__` method coerces `model` to a string, since YAML may parse numeric model names (e.g., `model: 4`) as integers.

### 4.7 `hook_engine` Section

Maps to `HookEngineConfig`. The YAML key is `hook_engine`.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `True` | Whether the hook engine runs at all. When `False`, no hooks are triggered or executed. |
| `max_concurrent_hooks` | `int` | `2` | Maximum number of hooks that may execute simultaneously. |

### 4.8 `monitoring` Section

Maps to `MonitoringConfig`. The YAML key is `monitoring`.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `stuck_task_threshold_seconds` | `int` | `3600` | A task that has been in `IN_PROGRESS` state without any status change for longer than this threshold (in seconds) is considered stuck. Defaults to 1 hour. |

### 4.9 `auto_task` Section

Maps to `AutoTaskConfig`. The YAML key is `auto_task`.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `True` | Whether automatic task generation from plan files is active. |
| `plan_file_patterns` | `list[str]` | `[".claude/plan.md", "plan.md", "docs/plans/*.md", "plans/*.md", "docs/plan.md"]` | Ordered list of file path patterns (relative to the repo root) to search for implementation plans. Glob patterns are supported. |
| `inherit_repo` | `bool` | `True` | When `True`, generated subtasks inherit the `repo_id` of the parent task. |
| `inherit_approval` | `bool` | `True` | When `True`, generated subtasks inherit the `requires_approval` flag of the parent task. |
| `base_priority` | `int` | `100` | Base priority value assigned to generated tasks. |
| `chain_dependencies` | `bool` | `True` | When `True`, each generated step depends on the previous step completing before it can start. |
| `max_plan_depth` | `int` | `1` | Maximum nesting depth for plan-generated tasks. A value of `1` means only one level of plan generation is allowed; a plan-generated task cannot itself trigger further plan generation. |
| `max_steps_per_plan` | `int` | `20` | Maximum number of steps extracted from a single plan file. Steps beyond this limit are ignored. |
| `use_llm_parser` | `bool` | `False` | When `True`, an LLM (Claude) is invoked to parse plan files instead of the deterministic parser. |
| `llm_parser_model` | `str` | `""` | Model name to use when `use_llm_parser` is `True`. An empty string means the system uses its default model. |

### 4.10 `memory` Section

Maps to `MemoryConfig`. The YAML key is `memory`.

This section configures the optional semantic memory subsystem powered by [memsearch](https://github.com/zilliztech/memsearch). When enabled, agents automatically receive relevant context from past task results and project notes at task start, and completed task results are saved as searchable memories.

The subsystem is **disabled by default** — set `enabled: true` to activate it. When disabled (or when the `memsearch` package is not installed), all memory operations are no-ops with no impact on normal operation.

| YAML key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Whether the memory subsystem is active. Must be explicitly set to `true` to enable. |
| `embedding_provider` | `str` | `"openai"` | Embedding provider for vector generation. Valid values: `"openai"`, `"google"`, `"voyage"`, `"ollama"`, `"local"`. |
| `embedding_model` | `str` | `""` | Embedding model name. Empty string uses the provider's default model. |
| `embedding_base_url` | `str` | `""` | Base URL for the embedding API. Primarily used for Ollama or custom embedding endpoints. |
| `embedding_api_key` | `str` | `""` | API key for the embedding provider. Supports `${ENV_VAR}` substitution (e.g., `${OPENAI_API_KEY}`). |
| `milvus_uri` | `str` | `"~/.agent-queue/memsearch/milvus.db"` | Milvus connection URI. A file path activates Milvus Lite (embedded); an `http://` or `https://` URI connects to a Milvus Server instance. |
| `milvus_token` | `str` | `""` | Authentication token for Milvus Server or Zilliz Cloud. Not needed for Milvus Lite. |
| `max_chunk_size` | `int` | `1500` | Maximum size (in characters) for each indexed text chunk. |
| `overlap_lines` | `int` | `2` | Number of overlapping lines between consecutive chunks to preserve context across chunk boundaries. |
| `auto_remember` | `bool` | `True` | When `true`, completed and failed task results are automatically saved as markdown memory files and indexed. |
| `auto_recall` | `bool` | `True` | When `true`, relevant memories are automatically injected into task context at task start. |
| `recall_top_k` | `int` | `5` | Number of memory chunks to inject during automatic recall. |
| `compact_enabled` | `bool` | `False` | Whether periodic LLM-based memory compaction is active. |
| `compact_interval_hours` | `int` | `24` | Hours between compaction runs when `compact_enabled` is `true`. |
| `compact_llm_provider` | `str` | `""` | LLM provider for compaction (defaults to `revision_provider` or `chat_provider`). |
| `compact_llm_model` | `str` | `""` | Model override for compaction. |
| `compact_recent_days` | `int` | `7` | Task memories younger than this many days are kept as-is during compaction. |
| `compact_archive_days` | `int` | `30` | Task memories older than this many days are deleted after digesting during compaction. |
| `index_notes` | `bool` | `True` | When `true`, markdown files in the project's `notes/` directory are included in the memory index. |
| `index_specs` | `bool` | `True` | When `true`, the workspace `specs/` directory is included in the memory index. |
| `index_docs` | `bool` | `True` | When `true`, the workspace `docs/` directory (published documentation) is included in the memory index. |
| `index_project_docs` | `bool` | `True` | When `true`, individual project doc files are indexed separately for targeted retrieval. |
| `project_docs_files` | `tuple[str, ...]` | `("CLAUDE.md", "README.md")` | Files to index individually when `index_project_docs` is enabled. |
| `index_sessions` | `bool` | `False` | When `true`, session transcripts are included in the memory index. |
| `profile_enabled` | `bool` | `True` | Toggle project profiles — auto-generated summaries of each project. |
| `profile_max_size` | `int` | `5000` | Maximum characters for project profile content. |
| `revision_enabled` | `bool` | `True` | Toggle post-task profile revision — updates the project profile after each task completes. |
| `revision_provider` | `str` | `""` | LLM provider for profile revision (defaults to `chat_provider`). |
| `revision_model` | `str` | `""` | Model override for profile revision. |
| `auto_generate_notes` | `bool` | `False` | Auto-note generation after tasks. Off by default as it can be noisy. |
| `notes_inform_profile` | `bool` | `True` | Include notes in profile revision context for richer profiles. |
| `fact_extraction_enabled` | `bool` | `True` | Extract structured facts after task completion into staging files for consolidation. |
| `index_knowledge` | `bool` | `True` | When `true`, the `knowledge/` directory is included in the vector index. |
| `knowledge_topics` | `tuple[str, ...]` | `("architecture", "api-and-endpoints", "deployment", "dependencies", "gotchas", "conventions", "decisions")` | Default topic files in the knowledge base. |
| `consolidation_enabled` | `bool` | `False` | Master switch for knowledge consolidation (daily + weekly). |
| `consolidation_schedule` | `str` | `"0 3 * * *"` | Cron expression for daily consolidation runs. |
| `deep_consolidation_schedule` | `str` | `"0 4 * * 0"` | Cron expression for weekly deep consolidation. |
| `consolidation_provider` | `str` | `""` | LLM provider for consolidation (defaults to `revision_provider`). |
| `consolidation_model` | `str` | `""` | Model override for consolidation LLM calls. |
| `factsheet_in_context` | `bool` | `True` | Include the project factsheet in agent context as Tier 0 (always included). |
| `context_max_tokens` | `int` | `4000` | Soft budget for total memory context injected into agent prompts. |
| `context_include_recent` | `int` | `3` | Number of recent same-project task results to include in context. |
| `consolidation_auto_trigger` | `bool` | `True` | Auto-run consolidation when growth thresholds are met (independent of cron schedule). |
| `consolidation_growth_threshold` | `int` | `10` | Number of staging files that triggers auto-consolidation. |
| `consolidation_min_age_hours` | `float` | `1.0` | Minimum age (hours) of staging facts before they are eligible for consolidation. |
| `consolidation_max_batch_size` | `int` | `50` | Maximum staging files processed per consolidation run. |
| `consolidation_similarity_threshold` | `float` | `0.7` | Similarity threshold for clustering related facts during consolidation. |
| `consolidation_cooldown_minutes` | `int` | `30` | Minimum minutes between auto-triggered consolidation runs. |

#### Storage Layout

Each project's memories are stored in its workspace under `memory/tasks/`. The memory index uses a per-project Milvus collection named `aq_{project_id}_memory` (with hyphens and spaces replaced by underscores).

#### Example Configurations

**Minimal (local, no API key needed):**
```yaml
memory:
  enabled: true
  embedding_provider: local
```

**OpenAI embeddings:**
```yaml
memory:
  enabled: true
  embedding_provider: openai
  embedding_api_key: ${OPENAI_API_KEY}
```

**Production (Milvus Server):**
```yaml
memory:
  enabled: true
  embedding_provider: openai
  embedding_api_key: ${OPENAI_API_KEY}
  milvus_uri: http://milvus-server:19530
  milvus_token: ${MILVUS_TOKEN}
  recall_top_k: 10
```

### 4.11 `rate_limits` Section

Maps directly to `AppConfig.rate_limits` as a raw dict. The YAML key is `rate_limits`.

The value must be a dict whose values are themselves dicts mapping string keys to integer values. There is no fixed schema for the inner dicts; the structure is passed through to the rate-limit subsystem without transformation.

Default value is an empty dict `{}` if the key is absent.

---

## 5. Loading Behavior

### 5.1 Call Sequence

`load_config(path)` executes the following steps in order:

1. Check that the file at `path` exists. Raise `FileNotFoundError` if it does not.
2. Call `_load_env_file(path)` to load the `.env` sidecar (see section 3).
3. Open and parse the YAML file with `yaml.safe_load`. A `None` result (empty file) is normalized to `{}`.
4. Call `_process_values(raw)` to recursively substitute all `${VAR}` placeholders in string values. This raises `ValueError` on any unset variable reference.
5. Instantiate a default `AppConfig()` object. All fields receive their default values at this point, including home-directory expansion for path fields.
6. For each recognized top-level key present in `raw`, construct the corresponding dataclass and assign it to the appropriate field on `config`. Keys absent from `raw` leave the corresponding `AppConfig` field at its default value.
7. Return the populated `AppConfig`.

### 5.2 Section Presence vs. Absence

Each config section is optional. If a section key is absent from the YAML, the corresponding `AppConfig` field retains its default-constructed value. There is no merging of partial YAML sections with defaults at the YAML level; instead, each field within a section uses `dict.get(key, default)` to supply defaults field-by-field, so a partial section (e.g., `scheduling` with only `rolling_window_hours` specified) correctly defaults the omitted fields.

### 5.3 Unrecognized Keys

Unrecognized top-level YAML keys are silently ignored. Unrecognized keys within a section are also silently ignored, because section dicts are accessed field-by-field with `.get()` rather than unpacked wholesale.

### 5.4 Type Coercion

No explicit type coercion is performed. Values are assigned directly from the YAML-parsed Python object. YAML's own type inference handles basic scalar types: unquoted integers become Python `int`, unquoted `true`/`false` become Python `bool`, and quoted values become `str`. If a YAML author provides a string where an integer is expected, the dataclass field will hold a string value; there is no schema validation.

### 5.5 Error Conditions

| Condition | Behavior |
|---|---|
| Config file does not exist | `FileNotFoundError` raised immediately. |
| YAML is syntactically invalid | `yaml.YAMLError` (or a subclass) propagates from `yaml.safe_load`. |
| A `${VAR}` placeholder references an unset environment variable | `ValueError` raised with the message `"Environment variable {VAR} not set"`. |
| `.env` file does not exist | Silently skipped; not an error. |

### 5.6 Environment Variable Precedence

Environment variables set in the process environment before `load_config` is called take precedence over values in the `.env` file. The `.env` loader explicitly skips any key already present in `os.environ`. Therefore, the precedence order (highest to lowest) is:

1. Process environment (set externally before launch, or by the OS)
2. `.env` file in the same directory as the config file
3. Hardcoded defaults in the dataclasses

---

## 6. Logging Configuration

### LoggingConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `level` | str | `"INFO"` | Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `format` | str | `"dev"` | Output mode: `"dev"` (colored), `"json"` (JSONL), `"plain"` (no ANSI). `"text"` is accepted as an alias for `"dev"`. |
| `include_source` | bool | `false` | Include filename and line number in log output |
| `log_file` | str | `""` | Path for JSONL log file. Empty = auto (`~/.agent-queue/logs/agent-queue.log`) |
| `log_file_max_bytes` | int | `50000000` | Max bytes per log file before rotation (50 MB) |
| `log_file_backup_count` | int | `5` | Number of rotated log files to keep |
| `console_format` | str | `""` | Custom format template (see below). Empty = default structlog layout. |

### Environment Variables

| Variable | Effect |
|---|---|
| `AGENT_QUEUE_LOG_LEVEL` | Override `logging.level` (used before config loads) |
| `AGENT_QUEUE_LOG_FORMAT` | Override `logging.format` (`dev`, `json`, `plain`) |

### Output Modes

**dev** (default) — Rich-colored console output with aligned columns. Best for local terminal use. Timestamps are shortened to `HH:MM:SS`, logger names strip the `src.` prefix.

**json** — Single-line JSON objects (JSONL). Every log line is a valid JSON object with `timestamp`, `level`, `logger`, `event`, and any bound context fields. Ideal for piping to `jq`, or shipping to log aggregation systems.

**plain** — Same layout as `dev` but without ANSI escape codes. Use when piping to a file or a tool that doesn't support colors.

### Custom Format Templates (`console_format`)

When set, replaces the default structlog ConsoleRenderer with a user-defined template. Uses `{field}` placeholders that expand to values from the log event.

**Available fields:**
- `{timestamp}` — Time (HH:MM:SS in dev/plain, full ISO in json)
- `{level}` — Log level (info, warning, error, ...)
- `{logger}` — Logger name (with `src.` prefix stripped)
- `{event}` or `{message}` — The log message
- `{lineno}` — Source line number (requires `include_source: true`)
- `{filename}` — Source filename (requires `include_source: true`)
- Any context field: `{task_id}`, `{project_id}`, `{component}`, `{command}`, `{plugin}`, `{platform}`, `{hook_id}`, `{agent_id}`, `{route}`, `{request_id}`, `{cycle_id}`, etc.
- `{*}` — All remaining context fields as colored `key=value` pairs

**Bracket group collapsing:** Groups like `[{component}:{project_id}]` are removed entirely when all fields inside are empty, so output stays clean.

**Coloring** is automatic in `dev` mode:
- Level → green (info), yellow (warning), red (error)
- Timestamp, logger, lineno → dim
- Error/critical messages → bold
- `{*}` key=value → cyan keys, magenta values
- Colors disabled automatically in `plain` mode

**Examples:**

```yaml
# Compact with source location
logging:
  console_format: "{timestamp} [{level}] {event} [{logger}:{lineno}] [{component}:{project_id}]"

# Minimal with catch-all
logging:
  console_format: "[{level}] {event} [{component}] {*}"

# Task-focused
logging:
  console_format: "{timestamp} {level} {event} [{task_id}] [{plugin}] {*}"
```

### Log File

The daemon always writes a JSONL log file alongside console output. The file uses JSON format regardless of the console `format` setting, so `jq` and `aq logs` always work.

Default path: `~/.agent-queue/logs/agent-queue.log`

Rotation: `RotatingFileHandler` with configurable size and backup count. Default keeps up to ~300 MB total (50 MB × 6 files).

### CLI: `aq logs`

Reads the JSONL log file directly (no running daemon required).

```
aq logs                          # tail + follow with colors
aq logs -F -n 100                # last 100 lines, no follow
aq logs --level ERROR            # filter by minimum level
aq logs --task-id swift-dawn     # filter by task_id
aq logs --project-id acme        # filter by project_id
aq logs --component hooks        # filter by component
aq logs --plugin github-issues   # filter by plugin name
aq logs --command create_task    # filter by command
aq logs --since 5m               # last 5 minutes
aq logs --json                   # raw JSONL (pipe to jq)
aq logs --no-color               # disable colors
```

### Context Propagation

Context fields are bound at system boundaries and automatically appear on all log lines within that scope:

| Boundary | Fields bound |
|---|---|
| CommandHandler.execute() | `command`, `component="command_handler"` |
| API middleware | `request_id`, `route`, `method`, `component="api"` |
| Orchestrator task execution | `task_id`, `project_id`, `component="orchestrator"` |
| Plugin command | `plugin` (nested inside command handler context) |
| Plugin cron | `plugin`, `component="plugin_cron"`, `cron_method` |
| Discord bot | `platform="discord"`, `discord_user`, `channel_id` |
| Telegram bot | `platform="telegram"`, `telegram_user`, `chat_id` |
| Supervisor | `component="supervisor"` |
| Hook engine | `hook_id`, `project_id`, `component="hooks"` |

Context propagates through async call chains via `contextvars`. A log call deep in `database/queries/tasks.py` automatically includes the `task_id` and `command` from the outer scope.

### YAML Example

```yaml
logging:
  level: INFO
  format: dev
  include_source: false
  console_format: "{timestamp} [{level}] {event} [{component}:{task_id}] {*}"
  log_file: ""  # auto
  log_file_max_bytes: 50000000
  log_file_backup_count: 5
```
