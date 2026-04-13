"""YAML configuration loading with environment variable substitution.

Loads the application config from a YAML file (default: ~/.agent-queue/config.yaml),
substitutes ${ENV_VAR} references with environment variable values, and maps
the result into typed dataclass instances. Also supports loading a .env file
from the same directory as the config file for local development.

The config is loaded once at startup and passed to all major components
(orchestrator, Discord bot, scheduler, adapters). Individual sections are
represented by dedicated dataclasses so each component can accept only the
config it needs.

See specs/config.md for the full specification of all configuration fields.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
import os
import re
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ConfigError:
    """A single configuration validation error or warning.

    Used by per-section ``validate()`` methods and ``AppConfig.validate()``
    to collect ALL issues before reporting, so operators can fix everything
    in one pass.
    """

    section: str
    field: str
    message: str
    severity: str = "error"  # "error" or "warning"

    def __str__(self) -> str:
        return f"[{self.section}] {self.field}: {self.message}"


class ConfigValidationError(Exception):
    """Raised when the application configuration fails validation checks.

    Contains a list of all validation errors found, not just the first one,
    so operators can fix all issues in one pass.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        super().__init__(msg)


@dataclass
class PerProjectChannelsConfig:
    """Configuration for automatic per-project Discord channel management."""

    auto_create: bool = False
    naming_convention: str = "{project_id}"
    category_name: str = ""  # Discord category to group project channels (optional)
    private: bool = True  # Make auto-created channels private (only bot + permitted users)


@dataclass
class DiscordConfig:
    """Discord bot connection and channel routing settings."""

    bot_token: str = ""
    guild_id: str = ""
    channels: dict[str, str] = field(
        default_factory=lambda: {
            "channel": "agent-queue",
            "agent_questions": "agent-questions",
        }
    )
    authorized_users: list[str] = field(default_factory=list)
    per_project_channels: PerProjectChannelsConfig = field(default_factory=PerProjectChannelsConfig)
    # Invalid request rate guard thresholds (Discord bans IPs at 10,000
    # invalid responses per 10 minutes).
    rate_guard_warn: int = 1000
    rate_guard_critical: int = 5000
    rate_guard_halt: int = 8000

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if not self.bot_token:
            errors.append(
                ConfigError("discord", "bot_token", "bot_token is required for Discord connection")
            )
        if not self.guild_id:
            errors.append(
                ConfigError("discord", "guild_id", "guild_id is required for Discord connection")
            )
        return errors


@dataclass
class TelegramConfig:
    """Telegram bot connection and chat routing settings.

    When ``messaging_platform`` is ``"telegram"``, these settings control
    the Telegram bot integration.  ``use_topics`` requires the target chat
    to be a supergroup with forum topics enabled.
    """

    bot_token: str = ""
    chat_id: str = ""  # Main chat/group for notifications
    authorized_users: list[str] = field(default_factory=list)  # Telegram user IDs
    per_project_chats: dict[str, str] = field(default_factory=dict)  # project_id -> chat_id
    use_topics: bool = True  # Use forum topics for task threads (requires supergroup)

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if not self.bot_token:
            errors.append(
                ConfigError(
                    "telegram", "bot_token", "bot_token is required for Telegram connection"
                )
            )
        if not self.chat_id:
            errors.append(
                ConfigError("telegram", "chat_id", "chat_id is required for Telegram connection")
            )
        return errors


@dataclass
class AgentsDefaultConfig:
    """Default timeouts for agent health monitoring and graceful shutdown."""

    heartbeat_interval_seconds: int = 30
    stuck_timeout_seconds: int = 0  # 0 = no timeout (was 600)
    graceful_shutdown_timeout_seconds: int = 30

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.heartbeat_interval_seconds <= 0:
            errors.append(ConfigError("agents", "heartbeat_interval_seconds", "must be > 0"))
        if self.stuck_timeout_seconds < 0:
            errors.append(ConfigError("agents", "stuck_timeout_seconds", "must be >= 0"))
        if self.graceful_shutdown_timeout_seconds <= 0:
            errors.append(ConfigError("agents", "graceful_shutdown_timeout_seconds", "must be > 0"))
        return errors


@dataclass
class SchedulingConfig:
    """Controls how the scheduler distributes agent capacity across projects.

    rolling_window_hours defines the lookback period for proportional credit
    accounting. min_task_guarantee ensures every active project gets at least
    one task slot regardless of credit balance.
    """

    rolling_window_hours: int = 24
    min_task_guarantee: bool = True
    affinity_wait_seconds: int = 120  # max seconds to wait for a busy affinity agent

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.rolling_window_hours <= 0:
            errors.append(ConfigError("scheduling", "rolling_window_hours", "must be > 0"))
        if self.affinity_wait_seconds < 0:
            errors.append(
                ConfigError("scheduling", "affinity_wait_seconds", "must be >= 0")
            )
        return errors


@dataclass
class PauseRetryConfig:
    """Backoff and retry timing for rate-limited and token-exhausted tasks.

    Controls both the in-process exponential backoff (before a task is paused)
    and the longer pause durations (after a task enters PAUSED state and waits
    for resume_after to elapse).
    """

    rate_limit_backoff_seconds: int = 60
    token_exhaustion_retry_seconds: int = 300
    # Exponential-backoff retry knobs (in-process, before the task is paused)
    rate_limit_max_retries: int = 3
    rate_limit_max_backoff_seconds: int = 300

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.rate_limit_backoff_seconds <= 0:
            errors.append(ConfigError("pause_retry", "rate_limit_backoff_seconds", "must be > 0"))
        if self.token_exhaustion_retry_seconds <= 0:
            errors.append(
                ConfigError("pause_retry", "token_exhaustion_retry_seconds", "must be > 0")
            )
        if self.rate_limit_max_retries < 0:
            errors.append(ConfigError("pause_retry", "rate_limit_max_retries", "must be >= 0"))
        if self.rate_limit_max_backoff_seconds <= 0:
            errors.append(
                ConfigError("pause_retry", "rate_limit_max_backoff_seconds", "must be > 0")
            )
        return errors


@dataclass
class AutoTaskConfig:
    """Configuration for auto-generating tasks from implementation plans."""

    enabled: bool = True
    plan_file_patterns: list[str] = field(
        default_factory=lambda: [
            ".claude/plan.md",
            "plan.md",
            "docs/plans/*.md",
            "plans/*.md",
            "docs/plan.md",
        ]
    )
    inherit_repo: bool = True  # Subtasks inherit parent's repo_id
    inherit_approval: bool = True  # Subtasks inherit parent's requires_approval
    base_priority: int = 100  # Base priority for generated tasks
    chain_dependencies: bool = True  # Tasks depend on previous step
    rebase_between_subtasks: bool = False  # Rebase onto main between subtasks
    mid_chain_rebase: bool = True  # Rebase onto main between subtasks to reduce drift
    mid_chain_rebase_push: bool = False  # Push rebased branch to remote between subtasks
    max_plan_depth: int = 1  # Max nesting of plan-generated tasks
    max_steps_per_plan: int = 20  # Cap phases from a single plan
    use_llm_parser: bool = False  # Use LLM (Claude) for plan parsing
    llm_parser_model: str = ""  # Model override for plan parsing
    skip_if_implemented: bool = True  # Skip task generation if branch has substantial code changes
    max_verification_retries: int = 2  # Max reopen attempts for git verification failures

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.max_plan_depth < 1:
            errors.append(ConfigError("auto_task", "max_plan_depth", "must be >= 1"))
        if self.max_steps_per_plan < 1:
            errors.append(ConfigError("auto_task", "max_steps_per_plan", "must be >= 1"))
        if self.base_priority < 0:
            errors.append(ConfigError("auto_task", "base_priority", "must be >= 0"))
        return errors


@dataclass
class ArchiveConfig:
    """Configuration for automatic archiving of terminal tasks.

    When enabled, the orchestrator automatically archives tasks that have
    been in a terminal status (COMPLETED, FAILED, BLOCKED) for longer than
    ``after_hours``.  This keeps the active task list clean without
    requiring manual ``/archive-tasks`` commands.
    """

    enabled: bool = True
    after_hours: float = 24.0  # Archive terminal tasks older than N hours
    statuses: list[str] = field(default_factory=lambda: ["COMPLETED", "FAILED", "BLOCKED"])

    def validate(self) -> list[ConfigError]:
        from src.models import TaskStatus

        errors: list[ConfigError] = []
        if self.after_hours <= 0:
            errors.append(ConfigError("archive", "after_hours", "must be > 0"))
        valid_statuses = {s.name for s in TaskStatus}
        for status in self.statuses:
            if status not in valid_statuses:
                errors.append(
                    ConfigError(
                        "archive",
                        "statuses",
                        f"'{status}' is not a valid TaskStatus (valid: {', '.join(sorted(valid_statuses))})",
                    )
                )
        return errors


@dataclass
class MonitoringConfig:
    """Configuration for monitoring stuck or stalled tasks."""

    stuck_task_threshold_seconds: int = 3600  # 1 hour default
    failed_blocked_report_interval_seconds: int = 3600  # 1 hour default


@dataclass
class MemoryConfig:
    """Configuration for the semantic memory subsystem (memsearch).

    All fields have safe defaults — the subsystem is disabled unless
    ``enabled`` is explicitly set to ``True`` in the YAML config.
    See notes/memsearch-integration.md for full documentation.
    """

    enabled: bool = False
    embedding_provider: str = "openai"  # openai, google, voyage, ollama, local
    embedding_model: str = ""  # empty = provider default
    embedding_base_url: str = ""  # for Ollama or custom endpoints
    embedding_api_key: str = ""  # supports ${ENV_VAR} substitution
    milvus_uri: str = "~/.agent-queue/memsearch/milvus.db"  # file path or server URI
    milvus_token: str = ""
    max_chunk_size: int = 1500
    overlap_lines: int = 2
    auto_remember: bool = True  # auto-save task results as memories
    auto_recall: bool = True  # auto-inject memories at task start
    recall_top_k: int = 5  # number of memories to inject
    compact_enabled: bool = False  # periodic LLM compaction
    compact_interval_hours: int = 24
    compact_llm_provider: str = (
        ""  # LLM for compaction (defaults to revision_provider or chat_provider)
    )
    compact_llm_model: str = ""  # model override for compaction
    compact_recent_days: int = 7  # task memories younger than this are kept as-is
    compact_archive_days: int = 30  # task memories older than this are deleted after digesting
    index_notes: bool = True  # index project notes/ directory
    index_specs: bool = True  # index workspace specs/ directory
    index_docs: bool = True  # index workspace docs/ directory (published documentation)
    index_project_docs: bool = True  # index individual doc files (CLAUDE.md, README.md)
    project_docs_files: tuple[str, ...] = ("CLAUDE.md", "README.md")  # files to index individually
    index_sessions: bool = False  # index session transcripts
    # Phase 1: Project Profile
    profile_enabled: bool = True  # toggle project profiles
    profile_max_size: int = 5000  # max chars for profile content
    # Phase 2: Post-Task Revision
    revision_enabled: bool = True  # toggle post-task profile revision
    revision_provider: str = ""  # LLM provider for revision (defaults to chat_provider)
    revision_model: str = ""  # model override for revision
    # Phase 3: Notes Integration
    auto_generate_notes: bool = False  # auto-note generation (off by default, can be noisy)
    notes_inform_profile: bool = True  # include notes in profile revision context
    # Phase 3.5: Post-Task Fact Extraction
    fact_extraction_enabled: bool = True  # extract structured facts after task completion
    # Phase 3.6: Knowledge Base Topic Files
    index_knowledge: bool = True  # index knowledge/ directory in vector DB
    knowledge_topics: tuple[str, ...] = (
        "architecture",
        "api-and-endpoints",
        "deployment",
        "dependencies",
        "gotchas",
        "conventions",
        "decisions",
    )
    # Knowledge Consolidation (unified: daily, deep/weekly, bootstrap)
    consolidation_enabled: bool = False  # master switch for consolidation
    consolidation_schedule: str = "0 3 * * *"  # daily consolidation cron
    deep_consolidation_schedule: str = "0 4 * * 0"  # weekly deep consolidation
    consolidation_provider: str = ""  # LLM provider (defaults to revision_provider)
    consolidation_model: str = ""  # model override for consolidation
    index_knowledge: bool = True  # index knowledge/ in vector DB
    factsheet_in_context: bool = True  # include factsheet in agent context (Tier 0)
    knowledge_topics: tuple[str, ...] = (
        "architecture",
        "api-and-endpoints",
        "deployment",
        "dependencies",
        "gotchas",
        "conventions",
        "decisions",
    )
    # L2 Topic Detection (spec §3 — pre-filtered memory loading by topic)
    topic_detection_enabled: bool = True  # detect topics from task description for L2 loading
    topic_max_knowledge_files: int = 3  # max knowledge files to inject per task
    topic_max_chars_per_file: int = 2000  # max chars per knowledge topic file in context
    # L2 Topic-Filtered Memories (spec §2 — memories with matching topic frontmatter)
    topic_memory_enabled: bool = True  # load memories filtered by detected topic
    topic_memory_budget_chars: int = 2000  # ~500 token budget for topic-filtered memories
    topic_memory_max_results: int = 5  # max number of topic-matched memory files
    # Enhanced Context Delivery
    context_max_tokens: int = 4000  # soft budget for total memory context
    context_include_recent: int = 3  # number of recent same-project tasks to include
    # Consolidation auto-trigger thresholds
    consolidation_auto_trigger: bool = True  # auto-run consolidation when thresholds are met
    consolidation_growth_threshold: int = 10  # staging files before auto-consolidation fires
    consolidation_min_age_hours: float = 1.0  # min age of staging facts before consolidating
    consolidation_max_batch_size: int = 50  # max staging files per consolidation run
    consolidation_similarity_threshold: float = 0.7  # similarity threshold for memory clustering
    consolidation_cooldown_minutes: int = 30  # min minutes between auto-triggered consolidations
    # Workspace spec/doc change detector (vault.md §4 — reference stubs)
    spec_watcher_enabled: bool = True  # detect spec/doc changes in project workspaces
    spec_watcher_poll_interval: int = 60  # seconds between workspace scans
    spec_watcher_patterns: tuple[str, ...] = (
        "specs/**/*.md",
        "docs/specs/**/*.md",
        "docs/**/*.md",
    )
    spec_watcher_max_excerpt_lines: int = 30  # lines of source to include in stub
    # Reference stub LLM enrichment (roadmap 6.3.2 — vault.md §4)
    stub_enrichment_enabled: bool = True  # enrich stubs with LLM summaries
    stub_enrichment_provider: str = ""  # LLM provider (falls back to revision_provider)
    stub_enrichment_model: str = ""  # model override for enrichment
    stub_enrichment_max_source_chars: int = 20_000  # max chars sent to LLM (~5k tokens)

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.enabled:
            valid_providers = {"openai", "google", "voyage", "ollama", "local"}
            if self.embedding_provider not in valid_providers:
                errors.append(
                    ConfigError(
                        "memory",
                        "embedding_provider",
                        f"must be one of {sorted(valid_providers)}, got '{self.embedding_provider}'",
                    )
                )
            if self.max_chunk_size <= 0:
                errors.append(ConfigError("memory", "max_chunk_size", "must be > 0"))
        return errors


@dataclass
class LoggingConfig:
    """Configuration for structured logging and output format.

    Controls the structlog-powered logging setup.  Three output modes:

    - ``"dev"`` — Rich-colored console output (default, best for terminals)
    - ``"json"`` — Single-line JSON objects for log aggregation / ``jq``
    - ``"plain"`` — Human-readable text without ANSI codes (for piping)

    The ``"text"`` value is accepted as a backward-compatible alias for ``"dev"``.
    """

    level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    format: str = "dev"  # "dev", "json", "plain" (also accepts "text" → "dev")
    include_source: bool = False  # Include filename/lineno in output
    log_file: str = ""  # Path for JSONL file; empty = auto
    log_file_max_bytes: int = 50_000_000  # 50 MB per file
    log_file_backup_count: int = 5  # rotated files to keep
    console_format: str = ""  # Custom format template for dev/plain console output
    # Uses {field} placeholders. Empty = default structlog layout.
    # Examples:
    #   "{timestamp} [{level}] {event} [{logger}:{lineno}] [{component}:{project_id}]"
    #   "{timestamp} {level} {event} [{component}] {*}"
    #   "[{level}] {event} [{component}:{task_id}] {*}"
    # Available fields: timestamp, level, logger, event/message, lineno, filename,
    #   and any context field (task_id, project_id, component, command, plugin, etc.)
    # Special: {*} = all remaining context fields as key=value pairs
    # Bracket groups like [{a}:{b}] collapse when all fields are empty


@dataclass
class ReflectionConfig:
    """Configuration for the Supervisor's action-reflect cycle."""

    level: str = "full"
    periodic_interval: int = 900
    max_depth: int = 3
    per_cycle_token_cap: int = 10000
    hourly_token_circuit_breaker: int = 100000

    _VALID_LEVELS = {"full", "moderate", "minimal", "off"}

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.level not in self._VALID_LEVELS:
            errors.append(
                ConfigError(
                    "reflection",
                    "level",
                    f"must be one of {sorted(self._VALID_LEVELS)}, got '{self.level}'",
                )
            )
        if self.max_depth < 1:
            errors.append(ConfigError("reflection", "max_depth", "must be >= 1"))
        if self.periodic_interval < 0:
            errors.append(ConfigError("reflection", "periodic_interval", "must be >= 0"))
        if self.per_cycle_token_cap < 0:
            errors.append(ConfigError("reflection", "per_cycle_token_cap", "must be >= 0"))
        if self.hourly_token_circuit_breaker < 0:
            errors.append(ConfigError("reflection", "hourly_token_circuit_breaker", "must be >= 0"))
        return errors


@dataclass
class ObservationConfig:
    """Configuration for the Supervisor's passive chat observation."""

    enabled: bool = True
    batch_window_seconds: int = 60
    max_buffer_size: int = 20
    stage1_keywords: list[str] = field(default_factory=list)

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.batch_window_seconds < 5:
            errors.append(ConfigError("observation", "batch_window_seconds", "must be >= 5"))
        if self.max_buffer_size < 1:
            errors.append(ConfigError("observation", "max_buffer_size", "must be >= 1"))
        return errors


@dataclass
class SupervisorConfig:
    """Top-level Supervisor configuration."""

    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    observation: ObservationConfig = field(default_factory=ObservationConfig)

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        errors.extend(self.reflection.validate())
        errors.extend(self.observation.validate())
        return errors


@dataclass
class ChatProviderConfig:
    """LLM provider settings for the Discord chat agent (not the coding agents)."""

    provider: str = "anthropic"  # "anthropic", "ollama", or "gemini"
    model: str = ""  # Empty = provider default
    base_url: str = ""  # For Ollama
    api_key: str = ""  # For Gemini (falls back to GEMINI_API_KEY / GOOGLE_API_KEY env vars)
    keep_alive: str = "1h"  # Ollama: how long to keep model loaded after last request
    num_ctx: int = 0  # Ollama: context window size (0 = model default)

    def __post_init__(self) -> None:
        # YAML may parse numeric model names (e.g. ``model: 4``) as int/float.
        # LLM APIs require the model field to be a string, so coerce here to
        # prevent "cannot unmarshal number … of type string" errors from
        # OpenAI-compatible servers (Ollama, vLLM, etc.).
        if self.model and not isinstance(self.model, str):
            object.__setattr__(self, "model", str(self.model))

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        valid_providers = {"anthropic", "ollama", "gemini"}
        if self.provider and self.provider not in valid_providers:
            errors.append(
                ConfigError(
                    "chat_provider",
                    "provider",
                    f"must be one of {sorted(valid_providers)}, got '{self.provider}'",
                )
            )
        if self.provider == "ollama" and not self.base_url:
            errors.append(
                ConfigError(
                    "chat_provider", "base_url", "base_url is required when provider is 'ollama'"
                )
            )
        return errors


@dataclass
class McpServerConfig:
    """Configuration for the MCP server exposed by the agent-queue system.

    When ``enabled`` is True, the daemon embeds a streamable-http MCP server
    on ``host:port`` so that MCP clients (e.g. Claude Code) can connect via
    URL instead of spawning a separate process.

    ``excluded_commands`` lists command names that should NOT be registered as
    MCP tools.  These are merged with ``DEFAULT_EXCLUDED_COMMANDS`` (hardcoded
    safe defaults) and the ``AGENT_QUEUE_MCP_EXCLUDED`` environment variable
    (comma-separated) to produce the final exclusion set.

    When ``inject_into_tasks`` is True (default when ``enabled`` is True), the
    daemon automatically adds the agent-queue MCP server as an HTTP MCP server
    in every task's ``mcp_servers`` dict.  This gives agents access to all
    agent-queue commands (task management, project operations, etc.) without
    requiring manual ``.mcp.json`` files in each workspace.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8081
    excluded_commands: list[str] = field(default_factory=list)
    inject_into_tasks: bool | None = None  # None = auto (True when enabled)

    @property
    def should_inject_into_tasks(self) -> bool:
        """Whether to auto-inject the MCP server into task contexts."""
        if self.inject_into_tasks is not None:
            return self.inject_into_tasks
        return self.enabled  # default: inject when enabled

    def task_mcp_entry(self) -> dict[str, dict]:
        """Return the MCP server config dict to merge into task contexts.

        Returns an empty dict if injection is disabled or the server isn't
        enabled. Otherwise returns ``{"agent-queue": {"type": "http", "url": ...}}``.
        """
        if not self.enabled or not self.should_inject_into_tasks:
            return {}
        url = f"http://{self.host}:{self.port}/mcp"
        return {"agent-queue": {"type": "http", "url": url}}

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.enabled and not (1 <= self.port <= 65535):
            errors.append(
                ConfigError("mcp_server", "port", f"must be between 1 and 65535, got {self.port}")
            )
        for cmd in self.excluded_commands:
            if not isinstance(cmd, str) or not cmd.strip():
                errors.append(
                    ConfigError(
                        "mcp_server",
                        "excluded_commands",
                        f"excluded command names must be non-empty strings, got: {cmd!r}",
                    )
                )
        return errors


@dataclass
class LLMLoggingConfig:
    """Configuration for logging LLM inputs/outputs to JSONL files."""

    enabled: bool = False
    retention_days: int = 30

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.enabled and self.retention_days <= 0:
            errors.append(ConfigError("llm_logging", "retention_days", "must be > 0 when enabled"))
        return errors


@dataclass
class AgentProfileConfig:
    """Configuration for an agent profile loaded from YAML.

    Profiles from YAML are synced to the database at startup. Profiles can
    also be created dynamically via Discord commands.
    """

    id: str = ""
    name: str = ""
    description: str = ""
    model: str = ""
    permission_mode: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    system_prompt_suffix: str = ""
    install: dict = field(default_factory=dict)

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if not self.id:
            errors.append(
                ConfigError(
                    "agent_profiles", "id", f"profile with name '{self.name}' has an empty id"
                )
            )
        valid_permission_modes = {
            "default",
            "plan",
            "full",
            "bypassPermissions",
            "acceptEdits",
            "auto",
            "",
        }
        if self.permission_mode and self.permission_mode not in valid_permission_modes:
            errors.append(
                ConfigError(
                    "agent_profiles",
                    "permission_mode",
                    f"profile '{self.id}': permission_mode must be one of "
                    f"{sorted(m for m in valid_permission_modes if m)}, got '{self.permission_mode}'",
                )
            )
        return errors


@dataclass
class HealthCheckConfig:
    """Configuration for the HTTP health check server.

    When enabled, the daemon exposes ``/health``, ``/ready``, and
    ``/plans/<task_id>`` endpoints on the configured port.

    ``base_url`` is the externally-reachable URL used to generate links
    (e.g. a tunnel URL like ``https://myqueue.example.com``).  When empty
    the daemon falls back to ``http://localhost:{port}``.
    """

    enabled: bool = False
    port: int = 8080
    base_url: str = ""


@dataclass
class DatabaseConfig:
    """Database backend configuration via a single URL/DSN.

    The ``url`` field determines the backend automatically:

    - Starts with ``postgresql://`` or ``postgres://`` → PostgreSQL (asyncpg)
    - Anything else (file path or empty) → SQLite (aiosqlite)

    Examples::

        # SQLite (default — same as the legacy database_path field):
        database:
          url: ~/.agent-queue/agent-queue.db

        # PostgreSQL:
        database:
          url: postgresql://user:pass@localhost:5432/agent_queue

    Pool settings are only used for PostgreSQL.
    """

    url: str = ""  # DSN or file path — backend is inferred
    pool_min_size: int = 2
    pool_max_size: int = 10

    @property
    def backend(self) -> str:
        """Infer backend from the URL scheme."""
        if self.url.startswith(("postgresql://", "postgres://")):
            return "postgresql"
        return "sqlite"

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if not self.url:
            errors.append(ConfigError("database", "url", "database url/path is required"))
        if self.backend == "postgresql":
            if self.pool_min_size < 1:
                errors.append(ConfigError("database", "pool_min_size", "must be >= 1"))
            if self.pool_max_size < self.pool_min_size:
                errors.append(ConfigError("database", "pool_max_size", "must be >= pool_min_size"))
        return errors


@dataclass
class AppConfig:
    """Top-level application configuration aggregating all subsystem configs.

    Instantiated once by load_config() at startup and threaded through to all
    major components. Each component reads only its relevant sub-config.

    The ``env`` field selects the environment profile (dev, staging, production).
    When set, ``load_config`` will look for an override file named
    ``config.{env}.yaml`` in the same directory as the main config file and
    deep-merge it over the base config.

    The ``validate()`` method performs fail-fast checks on critical settings.
    The ``reload_non_critical()`` method returns a fresh config with only
    non-critical settings updated from disk for hot-reloading.
    """

    data_dir: str = field(default_factory=lambda: os.path.expanduser("~/.agent-queue"))
    workspace_dir: str = field(
        default_factory=lambda: os.path.expanduser("~/agent-queue-workspaces")
    )
    database_path: str = ""  # Legacy SQLite path — use database.url instead
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    profile: str = ""
    env: str = "production"
    validate_events: bool = True
    messaging_platform: str = "discord"  # "discord" or "telegram"
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    agents_config: AgentsDefaultConfig = field(default_factory=AgentsDefaultConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    pause_retry: PauseRetryConfig = field(default_factory=PauseRetryConfig)
    chat_provider: ChatProviderConfig = field(default_factory=ChatProviderConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    health_check: HealthCheckConfig = field(default_factory=HealthCheckConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    auto_task: AutoTaskConfig = field(default_factory=AutoTaskConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    mcp_server: McpServerConfig = field(default_factory=McpServerConfig)
    llm_logging: LLMLoggingConfig = field(default_factory=LLMLoggingConfig)
    agent_profiles: list[AgentProfileConfig] = field(default_factory=list)
    global_token_budget_daily: int | None = None
    max_daily_playbook_tokens: int | None = None
    max_concurrent_playbook_runs: int = 2
    rate_limits: dict[str, dict[str, int]] = field(default_factory=dict)
    memory_extractor: dict = field(default_factory=lambda: {
        "enabled": False,
        "batch_window_seconds": 120,
        "max_buffer_size": 15,
        "max_facts_per_batch": 10,
        "max_input_chars": 8000,
    })
    _config_path: str = field(default="", repr=False)

    # -- Vault path properties (derived from data_dir) -----------------------
    # See docs/specs/design/vault.md Section 2 for the full directory layout.

    @property
    def vault_root(self) -> str:
        """Root of the Obsidian-compatible vault: ``{data_dir}/vault/``."""
        return os.path.join(self.data_dir, "vault")

    @property
    def vault_system(self) -> str:
        """System-scoped vault directory (merged into supervisor): ``{vault_root}/agent-types/supervisor/``."""
        return os.path.join(self.vault_root, "agent-types", "supervisor")

    @property
    def vault_supervisor(self) -> str:
        """Supervisor vault directory: ``{vault_root}/agent-types/supervisor/``."""
        return os.path.join(self.vault_root, "agent-types", "supervisor")

    @property
    def vault_agent_types(self) -> str:
        """Agent-type profiles and memory: ``{vault_root}/agent-types/``."""
        return os.path.join(self.vault_root, "agent-types")

    @property
    def vault_projects(self) -> str:
        """Per-project vault directories: ``{vault_root}/projects/``."""
        return os.path.join(self.vault_root, "projects")

    @property
    def vault_templates(self) -> str:
        """Templates for new profiles, playbooks: ``{vault_root}/templates/``."""
        return os.path.join(self.vault_root, "templates")

    @property
    def compiled_root(self) -> str:
        """Compiled playbook JSON (runtime artifacts): ``{data_dir}/compiled/``."""
        return os.path.join(self.data_dir, "compiled")

    def validate(self) -> list[ConfigError]:
        """Validate all configuration settings, delegating to per-section validators.

        Returns a list of all ConfigError instances found (errors and warnings).
        Does NOT raise — callers decide how to handle errors. The ``load_config()``
        function still raises ``ConfigValidationError`` for backward compatibility.
        """
        errors: list[ConfigError] = []

        # Cross-field: critical path checks
        if not self.workspace_dir:
            errors.append(ConfigError("app", "workspace_dir", "workspace_dir is required"))
        elif not os.access(self.workspace_dir, os.W_OK) and not os.path.exists(self.workspace_dir):
            # Check if parent dir is writable (could create workspace_dir)
            parent = os.path.dirname(self.workspace_dir)
            if parent and os.path.exists(parent) and not os.access(parent, os.W_OK):
                errors.append(
                    ConfigError(
                        "app",
                        "workspace_dir",
                        f"'{self.workspace_dir}' is not writable and parent directory is not writable",
                        severity="warning",
                    )
                )

        # Sync legacy database_path into database.url for backward compat
        if not self.database.url:
            self.database.url = self.database_path

        # Validate database config
        errors.extend(self.database.validate())
        if self.database.backend == "sqlite":
            db_path = self.database.url
            if not db_path:
                errors.append(ConfigError("database", "url", "database path is required"))
            else:
                db_parent = os.path.dirname(db_path)
                if db_parent and not os.path.exists(db_parent):
                    grandparent = os.path.dirname(db_parent)
                    if (
                        grandparent
                        and os.path.exists(grandparent)
                        and not os.access(grandparent, os.W_OK)
                    ):
                        errors.append(
                            ConfigError(
                                "database",
                                "url",
                                f"parent directory '{db_parent}' does not exist "
                                "and cannot be created",
                                severity="warning",
                            )
                        )

        # Validate messaging_platform field
        valid_platforms = {"discord", "telegram"}
        if self.messaging_platform not in valid_platforms:
            errors.append(
                ConfigError(
                    "app",
                    "messaging_platform",
                    f"must be one of {sorted(valid_platforms)}, got '{self.messaging_platform}'",
                )
            )

        # Only validate the active messaging platform's config
        if self.messaging_platform == "discord":
            errors.extend(self.discord.validate())
        elif self.messaging_platform == "telegram":
            errors.extend(self.telegram.validate())

        errors.extend(self.agents_config.validate())
        errors.extend(self.scheduling.validate())
        errors.extend(self.pause_retry.validate())
        errors.extend(self.chat_provider.validate())
        errors.extend(self.supervisor.validate())
        errors.extend(self.auto_task.validate())
        errors.extend(self.archive.validate())
        errors.extend(self.llm_logging.validate())
        errors.extend(self.memory.validate())
        errors.extend(self.mcp_server.validate())
        # Agent profiles
        for profile in self.agent_profiles:
            errors.extend(profile.validate())

        # Health check port range
        if self.health_check.enabled:
            if not (1 <= self.health_check.port <= 65535):
                errors.append(
                    ConfigError(
                        "health_check",
                        "port",
                        f"must be between 1 and 65535, got {self.health_check.port}",
                    )
                )

        # Monitoring threshold
        if self.monitoring.stuck_task_threshold_seconds < 0:
            errors.append(ConfigError("monitoring", "stuck_task_threshold_seconds", "must be >= 0"))

        # Rate limits structure validation
        for scope, limits in self.rate_limits.items():
            if not isinstance(limits, dict):
                errors.append(
                    ConfigError(
                        "rate_limits", scope, f"expected a dict, got {type(limits).__name__}"
                    )
                )

        return errors

    def check_deprecations(self) -> list[str]:
        """Check for deprecated config sections and return warning messages."""
        warnings = []
        return warnings

    def reload_non_critical(self) -> "AppConfig":
        """Return a new AppConfig with non-critical settings refreshed from disk.

        Non-critical settings (safe to change at runtime without restart):
        - scheduling, pause_retry, auto_task, archive, monitoring
        - llm_logging

        Critical settings (NOT reloaded — require restart):
        - discord, database_path, workspace_dir, chat_provider, memory,
          health_check

        Returns a new AppConfig instance; the caller is responsible for
        swapping references.  If the config file cannot be read or parsed,
        the current config is returned unchanged and the error is logged.
        """
        if not self._config_path or not os.path.exists(self._config_path):
            return self

        try:
            fresh = load_config(self._config_path, profile=self.profile or None)
        except Exception as e:
            logger.warning("Config hot-reload failed, keeping current config: %s", e)
            return self

        # Create a copy of current config and update only non-critical sections
        updated = copy.deepcopy(self)
        updated.scheduling = fresh.scheduling
        updated.pause_retry = fresh.pause_retry
        updated.auto_task = fresh.auto_task
        updated.archive = fresh.archive
        updated.monitoring = fresh.monitoring
        updated.llm_logging = fresh.llm_logging
        updated.max_daily_playbook_tokens = fresh.max_daily_playbook_tokens
        updated.max_concurrent_playbook_runs = fresh.max_concurrent_playbook_runs

        return updated


# ---------------------------------------------------------------------------
# Hot-reload classification
# ---------------------------------------------------------------------------

HOT_RELOADABLE_SECTIONS = {
    "scheduling",
    "monitoring",
    "archive",
    "llm_logging",
    "pause_retry",
    "agents_config",
    "auto_task",
    "logging",
    "agent_profiles",
    "max_daily_playbook_tokens",
    "max_concurrent_playbook_runs",
    "rate_limits",
}
"""Config sections that can be safely updated at runtime without restart."""

RESTART_REQUIRED_SECTIONS = {
    "discord",
    "telegram",
    "messaging_platform",
    "data_dir",
    "workspace_dir",
    "database_path",
    "chat_provider",
    "memory",
    "health_check",
}
"""Config sections that require a full restart to take effect."""

# Mapping from AppConfig field names to the section names used in diff output.
# Most fields map to themselves; these are the exceptions.
_SECTION_FIELDS = {
    "data_dir",
    "workspace_dir",
    "database_path",
    "profile",
    "env",
    "messaging_platform",
    "discord",
    "telegram",
    "agents_config",
    "scheduling",
    "pause_retry",
    "chat_provider",
    "health_check",
    "logging",
    "monitoring",
    "archive",
    "auto_task",
    "memory",
    "llm_logging",
    "agent_profiles",
    "global_token_budget_daily",
    "max_daily_playbook_tokens",
    "max_concurrent_playbook_runs",
    "rate_limits",
}


def diff_configs(old: AppConfig, new: AppConfig) -> set[str]:
    """Compare two AppConfig instances and return the set of changed section names.

    Uses ``dataclasses.asdict()`` for deep comparison of each section.
    Skips internal fields (prefixed with ``_``).
    """
    changed: set[str] = set()
    old_dict = dataclasses.asdict(old)
    new_dict = dataclasses.asdict(new)
    for field_name in _SECTION_FIELDS:
        old_val = old_dict.get(field_name)
        new_val = new_dict.get(field_name)
        if old_val != new_val:
            changed.add(field_name)
    return changed


class ConfigWatcher:
    """Watches the config file for changes and emits events on reload.

    Uses mtime-based polling (not filesystem events) for maximum portability.
    On change detection, loads the new config, validates it, diffs against
    the current config, and emits ``config.reloaded`` / ``config.restart_needed``
    events via the EventBus.

    Only hot-reloadable sections are applied; restart-required sections
    trigger a warning event but are not applied.
    """

    def __init__(
        self,
        config_path: str,
        event_bus,  # EventBus — imported lazily to avoid circular imports
        current_config: AppConfig,
        poll_interval: float = 30.0,
    ):
        self._config_path = config_path
        self._bus = event_bus
        self._config = current_config
        self._poll_interval = poll_interval
        self._last_mtime: float = 0.0
        self._task: asyncio.Task | None = None
        # Initialize mtime
        try:
            self._last_mtime = os.path.getmtime(config_path)
        except OSError:
            pass

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop the background polling task."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        """Poll the config file mtime and reload on change."""
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._check_for_changes()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("ConfigWatcher poll error: %s", e)

    async def _check_for_changes(self) -> None:
        """Check if the config file has been modified since last check."""
        try:
            current_mtime = os.path.getmtime(self._config_path)
        except OSError:
            return

        if current_mtime != self._last_mtime:
            self._last_mtime = current_mtime
            await self.reload()

    async def reload(self) -> dict:
        """Reload configuration from disk, diff, and emit events.

        Returns a summary dict with ``changed_sections``,
        ``restart_required``, and ``applied`` keys.
        """
        try:
            new_config = load_config(
                self._config_path,
                profile=self._config.profile or None,
            )
        except Exception as e:
            logger.warning("Config reload failed (keeping current config): %s", e)
            return {"error": str(e), "changed_sections": [], "applied": []}

        changed = diff_configs(self._config, new_config)
        if not changed:
            return {"changed_sections": [], "restart_required": [], "applied": []}

        # Classify changes
        hot_reloadable = changed & HOT_RELOADABLE_SECTIONS
        restart_needed = changed & RESTART_REQUIRED_SECTIONS

        # Apply only hot-reloadable sections
        if hot_reloadable:
            for section in hot_reloadable:
                if hasattr(self._config, section) and hasattr(new_config, section):
                    setattr(self._config, section, getattr(new_config, section))

            await self._bus.emit(
                "config.reloaded",
                {
                    "changed_sections": sorted(hot_reloadable),
                    "config": self._config,
                },
            )
            logger.info(
                "Config hot-reload: updated sections: %s",
                ", ".join(sorted(hot_reloadable)),
            )

        if restart_needed:
            await self._bus.emit(
                "config.restart_needed",
                {
                    "changed_sections": sorted(restart_needed),
                },
            )
            logger.warning(
                "Config reload: sections require restart to take effect: %s",
                ", ".join(sorted(restart_needed)),
            )

        return {
            "changed_sections": sorted(changed),
            "restart_required": sorted(restart_needed),
            "applied": sorted(hot_reloadable),
        }

    @property
    def config(self) -> AppConfig:
        """Return the current config (may have been updated by reload)."""
        return self._config


def _substitute_env_vars(value: str) -> str:
    """Replace ${ENV_VAR} with environment variable values."""

    def replacer(match):
        var_name = match.group(1)
        env_val = os.environ.get(var_name)
        if env_val is None:
            raise ValueError(f"Environment variable {var_name} not set")
        return env_val

    return re.sub(r"\$\{(\w+)\}", replacer, value)


def _process_values(obj):
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _process_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_process_values(v) for v in obj]
    return obj


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (override wins on conflict).

    Used for environment-specific config overlays and profile overlays:
    values in the overlay take precedence, but keys only present in the
    base are preserved.

    Special handling:
    - Dicts are merged recursively
    - Lists are replaced (not appended) to keep behavior predictable
    - ``None`` values in the overlay remove the key from the result
    """
    result = dict(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_env_file(config_path: str) -> None:
    """Load .env file from the same directory as the config file."""
    env_path = os.path.join(os.path.dirname(config_path), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                # Don't override existing env vars
                if key and key not in os.environ:
                    os.environ[key] = value


def load_config(path: str, profile: str | None = None) -> AppConfig:
    """Load and validate application configuration from a YAML file.

    Processing order:
      1. Load ``.env`` from the config file's directory (without overriding
         existing env vars)
      2. Parse the base YAML file
      3. Determine the environment profile (``AGENT_QUEUE_ENV`` env var,
         or ``env`` field in config, default ``"production"``)
      4. If an overlay file ``config.{env}.yaml`` exists in the same
         directory, deep-merge it over the base config
      5. If a *profile* is specified (via ``--profile`` CLI arg or
         ``AGENT_QUEUE_PROFILE`` env var), load the profile overlay from
         ``profiles/{profile}.yaml`` relative to the config directory and
         deep-merge it over the config
      6. Recursively substitute ``${ENV_VAR}`` references in all strings
      7. Map sections into typed dataclass instances
      8. Run ``validate()`` to catch misconfiguration early

    Args:
        path: Path to the base YAML config file.
        profile: Optional profile name. Falls back to ``AGENT_QUEUE_PROFILE``
            env var if not provided. When set, the corresponding file
            ``{config_dir}/profiles/{profile}.yaml`` must exist.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    _load_env_file(path)

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    # Determine environment profile for overlay loading
    env = os.environ.get("AGENT_QUEUE_ENV", raw.get("env", "production"))

    # Load environment-specific overlay (e.g. config.dev.yaml)
    config_dir = os.path.dirname(path) or "."
    base_name = os.path.basename(path)
    name_part, ext = os.path.splitext(base_name)
    overlay_path = os.path.join(config_dir, f"{name_part}.{env}{ext}")
    if os.path.exists(overlay_path):
        with open(overlay_path) as f:
            overlay = yaml.safe_load(f) or {}
        raw = _deep_merge(raw, overlay)

    # Resolve profile: CLI arg > env var > none
    resolved_profile = profile or os.environ.get("AGENT_QUEUE_PROFILE", "") or ""

    if resolved_profile:
        profiles_dir = os.path.join(config_dir, "profiles")
        profile_path = os.path.join(profiles_dir, f"{resolved_profile}.yaml")
        if not os.path.exists(profile_path):
            # List available profiles for a helpful error message
            available: list[str] = []
            if os.path.isdir(profiles_dir):
                available = sorted(
                    os.path.splitext(f)[0]
                    for f in os.listdir(profiles_dir)
                    if f.endswith((".yaml", ".yml"))
                )
            msg = f"Profile '{resolved_profile}' not found: {profile_path}"
            if available:
                msg += f"\nAvailable profiles: {', '.join(available)}"
            else:
                msg += f"\nNo profiles found in {profiles_dir}"
            raise FileNotFoundError(msg)
        with open(profile_path) as f:
            profile_raw = yaml.safe_load(f) or {}
        raw = _deep_merge(raw, profile_raw)

    raw = _process_values(raw)

    config = AppConfig()
    config._config_path = path
    config.profile = resolved_profile
    config.env = env
    # Event validation toggle: YAML key or env var override
    if "validate_events" in raw:
        config.validate_events = bool(raw["validate_events"])
    env_val = os.environ.get("AGENT_QUEUE_VALIDATE_EVENTS")
    if env_val is not None:
        config.validate_events = env_val.lower() not in ("0", "false", "no", "off")

    if "data_dir" in raw:
        config.data_dir = raw["data_dir"]
    if "workspace_dir" in raw:
        config.workspace_dir = raw["workspace_dir"]
    if "database_path" in raw:
        config.database_path = raw["database_path"]
    if "database" in raw and isinstance(raw["database"], dict):
        d = raw["database"]
        config.database = DatabaseConfig(
            url=d.get("url", ""),
            pool_min_size=d.get("pool_min_size", 2),
            pool_max_size=d.get("pool_max_size", 10),
        )
    # Backward compat: if no explicit database section, populate from database_path
    if not config.database.url:
        config.database.url = config.database_path
    if "global_token_budget_daily" in raw:
        config.global_token_budget_daily = raw["global_token_budget_daily"]
    if "max_daily_playbook_tokens" in raw:
        config.max_daily_playbook_tokens = raw["max_daily_playbook_tokens"]
    if "max_concurrent_playbook_runs" in raw:
        config.max_concurrent_playbook_runs = int(raw["max_concurrent_playbook_runs"])
    if "messaging_platform" in raw:
        config.messaging_platform = raw["messaging_platform"]

    if "discord" in raw:
        d = raw["discord"]
        ppc = PerProjectChannelsConfig()
        if "per_project_channels" in d:
            pp = d["per_project_channels"]
            ppc = PerProjectChannelsConfig(
                auto_create=pp.get("auto_create", False),
                naming_convention=pp.get("naming_convention", "{project_id}"),
                category_name=pp.get("category_name", ""),
                private=pp.get("private", True),
            )
        # Backward compat: if old config has separate control/notifications,
        # merge into single "channel" entry (prefer control since that's where
        # the bot listens for chat).
        raw_channels = d.get("channels", config.discord.channels)
        if "channel" not in raw_channels and (
            "control" in raw_channels or "notifications" in raw_channels
        ):
            merged_name = raw_channels.get("control") or raw_channels.get(
                "notifications", "agent-queue"
            )
            raw_channels = {
                "channel": merged_name,
                "agent_questions": raw_channels.get("agent_questions", "agent-questions"),
            }
        config.discord = DiscordConfig(
            bot_token=d.get("bot_token", ""),
            guild_id=d.get("guild_id", ""),
            channels=raw_channels,
            authorized_users=d.get("authorized_users", []),
            per_project_channels=ppc,
            rate_guard_warn=int(d.get("rate_guard_warn", 1000)),
            rate_guard_critical=int(d.get("rate_guard_critical", 5000)),
            rate_guard_halt=int(d.get("rate_guard_halt", 8000)),
        )

    if "telegram" in raw:
        tg = raw["telegram"]
        config.telegram = TelegramConfig(
            bot_token=tg.get("bot_token", ""),
            chat_id=str(tg.get("chat_id", "")),
            authorized_users=[str(u) for u in tg.get("authorized_users", [])],
            per_project_chats={k: str(v) for k, v in tg.get("per_project_chats", {}).items()},
            use_topics=tg.get("use_topics", True),
        )

    if "agents" in raw:
        a = raw["agents"]
        config.agents_config = AgentsDefaultConfig(
            heartbeat_interval_seconds=a.get("heartbeat_interval_seconds", 30),
            stuck_timeout_seconds=a.get("stuck_timeout_seconds", 0),
            graceful_shutdown_timeout_seconds=a.get("graceful_shutdown_timeout_seconds", 30),
        )

    if "scheduling" in raw:
        s = raw["scheduling"]
        config.scheduling = SchedulingConfig(
            rolling_window_hours=s.get("rolling_window_hours", 24),
            min_task_guarantee=s.get("min_task_guarantee", True),
            affinity_wait_seconds=s.get("affinity_wait_seconds", 120),
        )

    if "pause_retry" in raw:
        p = raw["pause_retry"]
        config.pause_retry = PauseRetryConfig(
            rate_limit_backoff_seconds=p.get("rate_limit_backoff_seconds", 60),
            token_exhaustion_retry_seconds=p.get("token_exhaustion_retry_seconds", 300),
            rate_limit_max_retries=p.get("rate_limit_max_retries", 3),
            rate_limit_max_backoff_seconds=p.get("rate_limit_max_backoff_seconds", 300),
        )

    if "chat_provider" in raw:
        cp = raw["chat_provider"]
        raw_model = cp.get("model", "")
        config.chat_provider = ChatProviderConfig(
            provider=cp.get("provider", "anthropic"),
            model=str(raw_model) if raw_model else "",
            base_url=cp.get("base_url", ""),
            api_key=cp.get("api_key", ""),
            keep_alive=cp.get("keep_alive", "1h"),
            num_ctx=cp.get("num_ctx", 0),
        )

    if "supervisor" in raw:
        s = raw["supervisor"]
        reflection = s.get("reflection", {})
        observation = s.get("observation", {})
        config.supervisor = SupervisorConfig(
            reflection=ReflectionConfig(
                level=reflection.get("level", "full"),
                periodic_interval=reflection.get("periodic_interval", 900),
                max_depth=reflection.get("max_depth", 3),
                per_cycle_token_cap=reflection.get("per_cycle_token_cap", 10000),
                hourly_token_circuit_breaker=reflection.get("hourly_token_circuit_breaker", 100000),
            ),
            observation=ObservationConfig(
                enabled=observation.get("enabled", True),
                batch_window_seconds=observation.get("batch_window_seconds", 60),
                max_buffer_size=observation.get("max_buffer_size", 20),
                stage1_keywords=observation.get("stage1_keywords", []),
            ),
        )

    # hook_engine config section removed (playbooks spec §13 Phase 3).
    # Existing config files with hook_engine section are silently ignored.

    if "logging" in raw:
        lg = raw["logging"]
        config.logging = LoggingConfig(
            level=lg.get("level", "INFO"),
            format=lg.get("format", "text"),
            include_source=lg.get("include_source", False),
        )

    if "monitoring" in raw:
        m = raw["monitoring"]
        config.monitoring = MonitoringConfig(
            stuck_task_threshold_seconds=m.get("stuck_task_threshold_seconds", 3600),
        )

    if "archive" in raw:
        ar = raw["archive"]
        config.archive = ArchiveConfig(
            enabled=ar.get("enabled", True),
            after_hours=float(ar.get("after_hours", 24.0)),
            statuses=ar.get("statuses", ["COMPLETED", "FAILED", "BLOCKED"]),
        )

    if "auto_task" in raw:
        at = raw["auto_task"]
        config.auto_task = AutoTaskConfig(
            enabled=at.get("enabled", True),
            plan_file_patterns=at.get(
                "plan_file_patterns",
                [
                    ".claude/plan.md",
                    "plan.md",
                    "docs/plans/*.md",
                    "plans/*.md",
                    "docs/plan.md",
                ],
            ),
            inherit_repo=at.get("inherit_repo", True),
            inherit_approval=at.get("inherit_approval", True),
            base_priority=at.get("base_priority", 100),
            chain_dependencies=at.get("chain_dependencies", True),
            rebase_between_subtasks=at.get("rebase_between_subtasks", False),
            mid_chain_rebase=at.get("mid_chain_rebase", True),
            mid_chain_rebase_push=at.get("mid_chain_rebase_push", False),
            max_plan_depth=at.get("max_plan_depth", 1),
            max_steps_per_plan=at.get("max_steps_per_plan", 20),
            use_llm_parser=at.get("use_llm_parser", False),
            llm_parser_model=at.get("llm_parser_model", ""),
            skip_if_implemented=at.get("skip_if_implemented", True),
        )

    if "memory" in raw:
        mem = raw["memory"]
        config.memory = MemoryConfig(
            enabled=mem.get("enabled", False),
            embedding_provider=mem.get("embedding_provider", "openai"),
            embedding_model=mem.get("embedding_model", ""),
            embedding_base_url=mem.get("embedding_base_url", ""),
            embedding_api_key=mem.get("embedding_api_key", ""),
            milvus_uri=mem.get("milvus_uri", "~/.agent-queue/memsearch/milvus.db"),
            milvus_token=mem.get("milvus_token", ""),
            max_chunk_size=mem.get("max_chunk_size", 1500),
            overlap_lines=mem.get("overlap_lines", 2),
            auto_remember=mem.get("auto_remember", True),
            auto_recall=mem.get("auto_recall", True),
            recall_top_k=mem.get("recall_top_k", 5),
            compact_enabled=mem.get("compact_enabled", False),
            compact_interval_hours=mem.get("compact_interval_hours", 24),
            index_notes=mem.get("index_notes", True),
            index_sessions=mem.get("index_sessions", False),
        )

    if "mcp_server" in raw:
        ms = raw["mcp_server"]
        config.mcp_server = McpServerConfig(
            enabled=ms.get("enabled", False),
            host=ms.get("host", "127.0.0.1"),
            port=ms.get("port", 8081),
            excluded_commands=ms.get("excluded_commands", []),
            inject_into_tasks=ms.get("inject_into_tasks", None),
        )

    if "llm_logging" in raw:
        ll = raw["llm_logging"]
        config.llm_logging = LLMLoggingConfig(
            enabled=ll.get("enabled", False),
            retention_days=ll.get("retention_days", 30),
        )

    if "agent_profiles" in raw:
        profiles = []
        for pid, pdata in raw["agent_profiles"].items():
            if not isinstance(pdata, dict):
                continue
            raw_profile_model = pdata.get("model", "")
            profiles.append(
                AgentProfileConfig(
                    id=pid,
                    name=pdata.get("name", pid),
                    description=pdata.get("description", ""),
                    model=str(raw_profile_model) if raw_profile_model else "",
                    permission_mode=pdata.get("permission_mode", ""),
                    allowed_tools=pdata.get("allowed_tools", []),
                    mcp_servers=pdata.get("mcp_servers", {}),
                    system_prompt_suffix=pdata.get("system_prompt_suffix", ""),
                    install=pdata.get("install", {}),
                )
            )
        config.agent_profiles = profiles

    if "health_check" in raw:
        hc = raw["health_check"]
        config.health_check = HealthCheckConfig(
            enabled=hc.get("enabled", False),
            port=hc.get("port", 8080),
            base_url=hc.get("base_url", ""),
        )

    if "rate_limits" in raw:
        config.rate_limits = raw["rate_limits"]

    if "memory_extractor" in raw:
        # Merge with defaults so missing keys get defaults
        config.memory_extractor = {**config.memory_extractor, **raw["memory_extractor"]}

    # Fail fast on misconfiguration — surface all errors at once.
    # validate() returns ConfigError list; convert fatal errors to exception
    # for backward compatibility.
    config_errors = config.validate()
    fatal_errors = [str(e) for e in config_errors if e.severity == "error"]
    if fatal_errors:
        raise ConfigValidationError(fatal_errors)

    # Log warnings (non-fatal)
    for e in config_errors:
        if e.severity == "warning":
            logger.warning("Config warning: %s", e)

    return config
