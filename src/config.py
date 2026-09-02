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
from urllib.parse import urlsplit

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
class AgentsDefaultConfig:
    """Default timeouts for agent health monitoring and graceful shutdown."""

    heartbeat_interval_seconds: int = 30
    stuck_timeout_seconds: int = 1800  # 30 min; 0 = no timeout
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
            errors.append(ConfigError("scheduling", "affinity_wait_seconds", "must be >= 0"))
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
    """Leftovers of the retired plan-to-subtask pipeline that still have
    live consumers: pre-task workspace cleanup deletes files matching
    ``plan_file_patterns``, and git verification reopens a task at most
    ``max_verification_retries`` times.  The plan-discovery fields
    (``enabled``, ``chain_dependencies``, ``max_plan_depth``, …) were
    removed with the discovery flow (llm-direct-path §6.3)."""

    plan_file_patterns: list[str] = field(
        default_factory=lambda: [
            ".claude/plan.md",
            "plan.md",
            "docs/plans/*.md",
            "plans/*.md",
            "docs/plan.md",
        ]
    )
    max_verification_retries: int = 2  # Max reopen attempts for git verification failures

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.max_verification_retries < 0:
            errors.append(
                ConfigError("auto_task", "max_verification_retries", "must be >= 0")
            )
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

    Paused by default during the framework overhaul — see
    docs/specs/design/feature-pauses.md.  Set ``enabled: true`` in the YAML
    config (restart required) to bring the subsystem back.  See
    notes/memsearch-integration.md for full documentation.
    """

    enabled: bool = False
    embedding_provider: str = "ollama"  # openai, google, voyage, ollama, local
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
        ""  # LLM for compaction (defaults to revision_provider or llm)
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
    revision_provider: str = ""  # LLM provider for revision (defaults to llm)
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
    consolidation_enabled: bool = True  # master switch for consolidation
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
    stub_enrichment_class: str = ""  # intelligence class for enrichment (empty = llm.default_class)
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
class ChatAnalyzerConfig:
    """Configuration for the Discord chat-analyzer suggester gate stack.

    The chat analyzer is the post-``observe()`` pipeline that decides
    whether to surface a suggestion to Discord.  Each field is a knob on
    one of the gates introduced by the chat-analyzer suggestion-quality
    overhaul plan.

    * ``min_confidence`` (Phase 4) — minimum
      ``intent_confidence × novelty × actionability`` product required
      before a suggestion is posted.  Suggestions below this threshold
      are dropped silently and tagged ``gate="confidence"`` in the
      structured log.
    * ``in_flight_min_confidence`` (Phase 5) — elevated minimum that
      replaces ``min_confidence`` whenever the project has at least one
      ``IN_PROGRESS`` task.  When a ``Stop Task`` button is showing in
      the channel header the user is watching execution, not shopping
      for new work, so we substantially raise the bar — only
      high-signal suggestions (e.g. "two stuck tasks older than 30
      min") clear it.  Default ``0.85``.  Suppressions are tagged
      ``gate="in_flight_active_task"``.  Set this to a value ``>=
      min_confidence`` for the escalation to mean anything; a value
      ``<= min_confidence`` is technically valid but a no-op.
    * ``dismiss_cooldown_seconds`` (Phase 6) — number of seconds after
      a user dismisses a suggestion in a channel during which no new
      suggestions are posted to that ``(project_id, channel_id)``
      pair.  A user dismissal is the strongest negative signal we
      have; honor it by going quiet for that channel for a window.
      Default ``600`` (10 minutes).  Set to ``0`` to disable the gate
      entirely.  Suppressions are tagged ``gate="dismiss_cooldown"``.
    """

    min_confidence: float = 0.6
    in_flight_min_confidence: float = 0.85
    dismiss_cooldown_seconds: int = 600

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if not 0.0 <= self.min_confidence <= 1.0:
            errors.append(
                ConfigError(
                    "chat_analyzer",
                    "min_confidence",
                    "must be in [0, 1]",
                )
            )
        if not 0.0 <= self.in_flight_min_confidence <= 1.0:
            errors.append(
                ConfigError(
                    "chat_analyzer",
                    "in_flight_min_confidence",
                    "must be in [0, 1]",
                )
            )
        if self.dismiss_cooldown_seconds < 0:
            errors.append(
                ConfigError(
                    "chat_analyzer",
                    "dismiss_cooldown_seconds",
                    "must be >= 0",
                )
            )
        return errors


@dataclass
class GlobalSupervisorConfig:
    """Configuration for the *global* supervisor session (``supervisor-global``).

    The global supervisor is the always-available Agent Q brain the
    dashboard talks to at ``/`` — distinct from per-project
    supervisors. It runs with an admin-scope, loopback-restricted
    bearer token and its own memory scope (``supervisor:global``).
    Idle-timeout drives how long the on-demand session stays warm
    between conversations. See ``docs/superpowers/specs/
    2026-08-22-dashboard-shell-v2-design.md`` §4.
    """

    #: Seconds of inactivity before the on-demand global-supervisor
    #: session is torn down. Default: 45 min.
    idle_timeout_seconds: int = 2700

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.idle_timeout_seconds < 0:
            errors.append(
                ConfigError(
                    "supervisor.global",
                    "idle_timeout_seconds",
                    "must be >= 0",
                )
            )
        return errors


@dataclass
class SupervisorConfig:
    """Top-level Supervisor configuration."""

    #: The trailing underscore in the attribute name avoids the Python
    #: keyword ``global``. In YAML the section is written as ``global``.
    global_: GlobalSupervisorConfig = field(default_factory=GlobalSupervisorConfig)

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        errors.extend(self.global_.validate())
        return errors


LLM_PROVIDER_IDS = frozenset({"anthropic", "google", "openai"})
_LEGACY_LLM_PROVIDER_IDS = {"gemini": "google", "ollama": "openai"}


def normalize_llm_provider(name: str) -> str:
    """Map legacy chat_provider ids (``gemini``, ``ollama``) to ``llm`` ids."""
    return _LEGACY_LLM_PROVIDER_IDS.get(name, name)


@dataclass
class LLMConfig:
    """The direct LLM path (``src/llm``): playbook nodes and transitions, plugin
    ``invoke_llm``, stub enrichment, vault summaries.  Not the coding agents —
    those run as tmux sessions selected by the profile's ``harness``."""

    provider: str = "anthropic"  # "anthropic" | "google" | "openai"
    model: str = ""  # explicit model id; empty = intelligence class, else provider default
    api_key: str = ""  # optional; ANTHROPIC_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY otherwise
    base_url: str = ""  # openai only: OpenAI-compatible endpoint (Ollama: http://localhost:11434/v1)
    max_tokens: int = 4096
    default_class: str = ""  # intelligence class used when a call names none

    def __post_init__(self) -> None:
        # YAML may parse ``model: 4`` as an int; APIs require a string.
        if self.model and not isinstance(self.model, str):
            object.__setattr__(self, "model", str(self.model))

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.provider not in LLM_PROVIDER_IDS:
            errors.append(
                ConfigError(
                    "llm",
                    "provider",
                    f"must be one of {sorted(LLM_PROVIDER_IDS)}, got '{self.provider}'",
                )
            )
        if self.provider == "openai" and not (
            self.base_url or self.api_key or os.environ.get("OPENAI_API_KEY")
        ):
            errors.append(
                ConfigError(
                    "llm",
                    "base_url",
                    "provider 'openai' needs base_url (a local OpenAI-compatible endpoint) "
                    "or an API key (api_key / OPENAI_API_KEY)",
                )
            )
        return errors


@dataclass
class McpTaskScopeConfig:
    """Task-scoped MCP endpoint (``/mcp-task``) settings.

    Substrate placeholder for the aq-surface spec (§7).  Nothing reads
    these fields yet — ``enabled`` stays False until the Phase-3 flip.
    See docs/specs/implementation/aq-surface.md.
    """

    enabled: bool = False
    allowlist_extra: list[str] = field(default_factory=list)

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        for name in self.allowlist_extra:
            if not isinstance(name, str) or not name.strip():
                errors.append(
                    ConfigError(
                        "mcp_server",
                        "task_scope.allowlist_extra",
                        f"command names must be non-empty strings, got: {name!r}",
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

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8081
    excluded_commands: list[str] = field(default_factory=list)
    inject_into_tasks: bool = True
    task_scope: McpTaskScopeConfig = field(default_factory=McpTaskScopeConfig)

    @property
    def should_inject_into_tasks(self) -> bool:
        """Whether to auto-inject the MCP server into task contexts."""
        return self.enabled and self.inject_into_tasks

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
        errors.extend(self.task_scope.validate())
        return errors


@dataclass
class LLMLoggingConfig:
    """Configuration for logging LLM inputs/outputs to JSONL files."""

    enabled: bool = True
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
    # Normalized capability namespaces (Playbook V2 Package 0 §3.1).
    # ``None`` means "not authored — run the legacy ``allowed_tools``
    # adapter"; ``[]`` means "explicitly none".
    harness_tools: list[str] | None = None
    aq_commands: list[str] | None = None
    plugin_tools: list[str] | None = None
    # New shape: ``list[str]`` of MCP registry names.  Legacy YAML profiles
    # may still carry a ``dict[str, dict]`` of inline configs; the
    # inline-mcp-servers migration extracts these to the vault registry
    # at startup.  Both shapes are accepted at load time.
    mcp_servers: list[str] | dict[str, dict] = field(default_factory=list)
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
        # Wildcard capabilities are prohibited in every shape a profile can
        # take (Playbook V2 Package 0 §3.2) — YAML included, not just the
        # vault markdown.
        for field_name in ("allowed_tools", "harness_tools", "aq_commands", "plugin_tools"):
            for name in getattr(self, field_name, None) or []:
                if isinstance(name, str) and any(ch in name for ch in ("*", "?")):
                    errors.append(
                        ConfigError(
                            "agent_profiles",
                            field_name,
                            f"profile '{self.id}': {name!r} contains a wildcard; "
                            "wildcard capabilities are prohibited",
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

    enabled: bool = True
    port: int = 8081
    base_url: str = ""


#: Every DSN scheme that means "this is PostgreSQL".
#:
#: The driver-qualified forms matter as much as the bare ones: SQLAlchemy
#: writes ``postgresql+asyncpg://``, that is what
#: :func:`src.database.engine.create_postgres_engine` normalizes *to*, and it
#: is the form this repo's own tooling passes around (``POSTGRES_TEST_DSN``,
#: ``alembic.ini``).  Matching only ``postgresql://`` made such a URL fall
#: through to the SQLite branch, where it was treated as a *file path* — the
#: daemon then silently ran on an empty SQLite database and
#: :func:`src.main.run` created a directory literally named
#: ``postgresql+asyncpg:/agent_queue:…@host:5533``.  Fail-fast is not
#: possible here (a bare path is a legal value), so the scheme list has to
#: be right.
POSTGRES_URL_SCHEMES: tuple[str, ...] = (
    "postgresql://",
    "postgres://",
    "postgresql+asyncpg://",
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
)


#: PostgreSQL drivers that are recognized as PostgreSQL but cannot actually
#: run this daemon.  ``psycopg2`` has no asyncio support at all, so
#: ``create_async_engine`` rejects it — but only at first connect, deep in
#: SQLAlchemy, with a message about the dialect not being async.  Naming it
#: here turns that into a config error at load with the fix in it.
#: (``postgresql+psycopg`` — psycopg *3* — is async-capable and stays legal.)
SYNC_ONLY_POSTGRES_SCHEMES: tuple[str, ...] = ("postgresql+psycopg2://",)


def is_postgres_url(url: str) -> bool:
    """True when *url* is a PostgreSQL DSN rather than a SQLite file path."""
    return str(url or "").startswith(POSTGRES_URL_SCHEMES)


@dataclass
class DatabaseConfig:
    """Database backend configuration via a single URL/DSN.

    The ``url`` field determines the backend automatically:

    - Any scheme in :data:`POSTGRES_URL_SCHEMES` → PostgreSQL (asyncpg)
    - Anything else (file path or empty) → SQLite (aiosqlite)

    Examples::

        # SQLite (default — same as the legacy database_path field):
        database:
          url: ~/.agent-queue/agent-queue.db

        # PostgreSQL — both spellings work:
        database:
          url: postgresql://user:pass@localhost:5432/agent_queue
        database:
          url: postgresql+asyncpg://user:pass@localhost:5432/agent_queue

    Pool settings are only used for PostgreSQL.
    """

    url: str = ""  # DSN or file path — backend is inferred
    pool_min_size: int = 2
    pool_max_size: int = 10

    @property
    def backend(self) -> str:
        """Infer backend from the URL scheme."""
        return "postgresql" if is_postgres_url(self.url) else "sqlite"

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if not self.url:
            errors.append(ConfigError("database", "url", "database url/path is required"))
        if self.backend == "postgresql":
            if self.url.startswith(SYNC_ONLY_POSTGRES_SCHEMES):
                errors.append(
                    ConfigError(
                        "database",
                        "url",
                        "psycopg2 is sync-only; use postgresql+asyncpg:// "
                        "(the daemon is async end to end)",
                    )
                )
            if self.pool_min_size < 1:
                errors.append(ConfigError("database", "pool_min_size", "must be >= 1"))
            if self.pool_max_size < self.pool_min_size:
                errors.append(ConfigError("database", "pool_max_size", "must be >= pool_min_size"))
        return errors


# ---------------------------------------------------------------------------
# Framework-overhaul substrate sections (Wave 0)
#
# Every section below is a *container only* — no subsystem reads these
# fields yet.  Each owning lane fills in the behavior behind its own flag.
# All feature flags default to off so the daemon behaves exactly as it did
# before this block existed.  See docs/analysis/execution-plan.md §2.
# ---------------------------------------------------------------------------


@dataclass
class PlaybooksConfig:
    """Playbook subsystem switch.

    Paused by default during the framework overhaul — see
    docs/specs/design/feature-pauses.md.  Temporary.
    """

    enabled: bool = False
    #: Add contract-derived intent to V1 graph-view nodes.  Removed in Package 5.
    contract_intent: bool = True
    v2_storage_enabled: bool = False
    v2_max_artifact_bytes: int = 1_048_576
    v2_max_result_bytes: int = 262_144
    v2_max_snapshot_bytes: int = 4_194_304
    v2_max_pending_events_per_playbook: int = 1000
    v2_pending_event_retention_days: int = 7
    v2_receipt_retention_days: int = 90
    v2_artifact_retention_days: int = 90
    v2_artifact_min_versions: int = 10
    v2_retention_sweep_interval_seconds: int = 3600

    #: Playbook V2 semantic-graph read surface (graph, activation health,
    #: artifact diff, pending events, run overlay).  Package 5 owns it;
    #: Package 7 removes the flag together with the V1 graph command.
    v2_api: bool = False

    #: Playbook V2 operator *writes* (activate an artifact, resolve a pending
    #: event).  Deliberately separate from ``v2_api`` so the whole review
    #: surface stays readable with writes disabled — roadmap Package 5's
    #: rollback boundary: "Activation writes are independently feature-gated
    #: from graph reads."  Removed in Package 7.
    v2_activation_writes: bool = False

    #: Review-only Package 2 compiler commands.  Compilation, validation and
    #: shadow reports never persist or activate an artifact.  Removed in
    #: Playbook V2 Package 7.
    v2_compiler_enabled: bool = False

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        for field_name in (
            "v2_max_artifact_bytes",
            "v2_max_result_bytes",
            "v2_max_snapshot_bytes",
            "v2_max_pending_events_per_playbook",
            "v2_pending_event_retention_days",
            "v2_receipt_retention_days",
            "v2_artifact_retention_days",
            "v2_retention_sweep_interval_seconds",
        ):
            if getattr(self, field_name) <= 0:
                errors.append(ConfigError("playbooks", field_name, "must be > 0"))
        if self.v2_max_result_bytes > self.v2_max_snapshot_bytes:
            errors.append(
                ConfigError(
                    "playbooks", "v2_max_result_bytes", "must be <= v2_max_snapshot_bytes"
                )
            )
        if self.v2_artifact_min_versions < 1:
            errors.append(ConfigError("playbooks", "v2_artifact_min_versions", "must be >= 1"))
        return errors


@dataclass
class SessionsConfig:
    """Long-lived agent session runtime — see
    docs/specs/implementation/session-runtime.md §5.

    Not a substrate placeholder any more: since the runtime subsystem was
    removed, a session (a harness CLI wrapped by the configured provider) is
    the *only* way a task ever runs.  ``enabled: false`` therefore means
    "this daemon dispatches nothing" — ``ExecutionMixin._is_session_routed``
    returns False for every profile, ``_execute_task`` raises, and layer 2
    of the execution wrapper puts the task back to READY, so the sole
    outward symptom is a queue that never moves.  It stays expressible — an
    API/Discord-only daemon, and the tests that assert the routing fork —
    but ``AppConfig.validate()`` warns about it at load rather than letting
    it show up once per task as a swallowed traceback.
    """

    enabled: bool = True
    #: ``subprocess`` -- not because tmux is unimplemented (it is not), but
    #: because the default has to be a provider
    #: ``default_session_registry`` can actually build on any host: with
    #: ``tmux`` here, a stock install made
    #: ``providers.create("tmux")`` raise on every launch, which pauses the
    #: task for 60 s *and posts a Discord notification* -- per task, every
    #: 60 s, forever.  ``validate()`` also refuses a provider this host
    #: cannot construct, so the failure is a config error at load rather
    #: than a notification loop at runtime.
    provider: str = "subprocess"  # tmux | subprocess | fake
    tmux_socket: str = "aq"
    lease_ttl_seconds: int = 480
    stall_max_nudges: int = 3
    stall_backoff_seconds: int = 300
    max_restarts: int = 3
    restart_window_seconds: int = 600
    restart_backoff_seconds: int = 30
    dialog_budget_seconds: int = 8
    #: How long the pane must stay free of declared startup dialogs before
    #: a session counts as started.  Claude and Codex both paint their
    #: trust screen after the first frames, so a zero window declares a
    #: still-blocked session ready.
    dialog_settle_seconds: float = 1.5
    nudge_debounce_ms: int = 500
    state_cache_ttl_seconds: int = 2
    transcript_poll_seconds: int = 2
    adopt_on_start: bool = True
    #: Live pane stream (dashboard).  Polling happens only while a
    #: subscriber is attached, so an unwatched daemon pays nothing.
    pane_stream_interval_seconds: float = 1.0
    pane_stream_max_sessions: int = 12
    pane_stream_lines: int = 60

    _VALID_PROVIDERS = ("tmux", "subprocess", "fake")

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.provider not in self._VALID_PROVIDERS:
            errors.append(
                ConfigError(
                    "sessions",
                    "provider",
                    f"must be one of {sorted(self._VALID_PROVIDERS)}, got '{self.provider}'",
                )
            )
        elif self.enabled:
            # A name in the vocabulary is not the same as a class this host
            # can build -- tmux is POSIX-only and is not implemented yet, so
            # the registry simply will not have it.  Catch that here, once,
            # instead of at every task launch.
            try:
                from src.sessions import default_session_registry

                if default_session_registry().get(self.provider) is None:
                    errors.append(
                        ConfigError(
                            "sessions",
                            "provider",
                            f"'{self.provider}' is not available on this host "
                            "(not implemented, or unsupported platform) -- "
                            "sessions.enabled would fail every launch",
                        )
                    )
            except Exception:  # pragma: no cover - registry import problems
                pass
        for name in (
            "lease_ttl_seconds",
            "stall_max_nudges",
            "stall_backoff_seconds",
            "max_restarts",
            "restart_window_seconds",
            "restart_backoff_seconds",
            "dialog_budget_seconds",
            "nudge_debounce_ms",
            "state_cache_ttl_seconds",
            "transcript_poll_seconds",
            "dialog_settle_seconds",
        ):
            if getattr(self, name) < 0:
                errors.append(ConfigError("sessions", name, "must be >= 0"))
        for name in (
            "pane_stream_interval_seconds",
            "pane_stream_max_sessions",
            "pane_stream_lines",
        ):
            if getattr(self, name) <= 0:
                errors.append(ConfigError("sessions", name, "must be > 0"))
        return errors


@dataclass
class WorktreesConfig:
    """Per-slot git worktree execution.

    Worktree-execution spec §5 / §9 (P6 flag flip).
    While ``enabled`` is False every git workspace kind is treated as
    ``exclusive-clone`` regardless of its declared ``mode``.  Default is
    now True: the rollout gate has retired, and the kind's markdown
    ``mode`` is the steady-state knob (principle #1).  Set ``enabled:
    false`` in ``~/.agent-queue/config.yaml`` to opt out.
    """

    enabled: bool = True
    retain_failed_days: int = 7
    merge_slot_ttl_seconds: int = 600
    prune_remote_branches: bool = False
    setup_timeout_seconds: int = 900
    salvage_dirty: bool = True
    # Upper bound on one archived salvage patch, in bytes.  Past it the patch
    # is replaced by its ``--stat`` summary: a slot can hold an arbitrarily
    # large non-ignored build artifact and ``task_contexts`` is not a blob
    # store.  0 disables the cap.
    salvage_max_bytes: int = 5 * 1024 * 1024
    spawn_conflict_continuation: bool = False

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.retain_failed_days < 0:
            errors.append(ConfigError("worktrees", "retain_failed_days", "must be >= 0"))
        if self.salvage_max_bytes < 0:
            errors.append(
                ConfigError("worktrees", "salvage_max_bytes", "must be >= 0")
            )
        if self.merge_slot_ttl_seconds <= 0:
            errors.append(ConfigError("worktrees", "merge_slot_ttl_seconds", "must be > 0"))
        if self.setup_timeout_seconds <= 0:
            errors.append(ConfigError("worktrees", "setup_timeout_seconds", "must be > 0"))
        return errors


@dataclass
class StreamsConfig:
    """Streamable-command registry backing the console-stream pane view.

    See docs/superpowers/specs/2026-08-22-pane-console-stream-design.md §8.1/§8.4.
    """

    buffer_max_lines: int = 5000
    buffer_max_bytes: int = 2 * 1024 * 1024
    retention_seconds: int = 300
    kill_grace_seconds: float = 5.0
    max_concurrent_per_session: int = 3
    client_reconnect_attempts: int = 5

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.buffer_max_lines <= 0:
            errors.append(
                ConfigError("streams", "buffer_max_lines", "must be positive")
            )
        if self.buffer_max_bytes <= 0:
            errors.append(
                ConfigError("streams", "buffer_max_bytes", "must be positive")
            )
        if self.retention_seconds <= 0:
            errors.append(
                ConfigError("streams", "retention_seconds", "must be positive")
            )
        if self.kill_grace_seconds <= 0:
            errors.append(
                ConfigError("streams", "kill_grace_seconds", "must be positive")
            )
        if self.max_concurrent_per_session <= 0:
            errors.append(
                ConfigError(
                    "streams", "max_concurrent_per_session", "must be positive"
                )
            )
        return errors


@dataclass
class ModelPricing:
    """One pricing row: an fnmatch-style model glob and its USD rates.

    Substrate placeholder — see docs/specs/implementation/trust-and-ops.md §2.
    """

    model: str = ""  # glob, fnmatch-style; entries match in order
    input_per_mtok: float = 0.0  # USD per million input tokens
    output_per_mtok: float = 0.0


@dataclass
class PricingConfig:
    """Model price table used for token-ledger cost rollups.

    Substrate placeholder — see docs/specs/implementation/trust-and-ops.md §2.
    Empty by default, which means "no prices known" (cost columns stay unpriced).
    """

    models: list[ModelPricing] = field(default_factory=list)

    def match(self, model: str) -> ModelPricing | None:
        """Return the first entry whose glob matches ``model``, or None."""
        import fnmatch

        for entry in self.models:
            if entry.model and fnmatch.fnmatch(model, entry.model):
                return entry
        return None

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        for idx, entry in enumerate(self.models):
            if not entry.model or not entry.model.strip():
                errors.append(
                    ConfigError("pricing", f"models[{idx}].model", "must be a non-empty glob")
                )
            if entry.input_per_mtok < 0:
                errors.append(
                    ConfigError("pricing", f"models[{idx}].input_per_mtok", "must be >= 0")
                )
            if entry.output_per_mtok < 0:
                errors.append(
                    ConfigError("pricing", f"models[{idx}].output_per_mtok", "must be >= 0")
                )
        return errors


#: Valid values for ``security.capability_enforcement``.
CAPABILITY_ENFORCEMENT_MODES: frozenset[str] = frozenset({"off", "audit", "enforce"})


@dataclass
class SecurityConfig:
    """Env scrubbing and operational-health thresholds.

    Substrate placeholder — see docs/specs/implementation/trust-and-ops.md §2.
    ``env_scrub_enabled`` is a kill switch for a subsystem that does not
    exist yet, so it is inert until lane 1C lands ``src/env_scrub.py``.
    """

    env_scrub_enabled: bool = True  # kill switch, default on
    env_allowlist: list[str] = field(default_factory=list)  # names or globs
    #: Dispatch-time capability enforcement (Playbook V2 Package 0 §3.6).
    #:
    #: ``off``      — never deny.
    #: ``audit``    — deny an explicitly authored ``## Capabilities`` policy;
    #:                allow a *legacy-adapted* one with a
    #:                ``capability_denied_shadow`` warning.  Default, so
    #:                flipping deny-by-default on does not strand a running
    #:                fleet mid-migration.
    #: ``enforce``  — deny either.
    #:
    #: An operator who wrote the block asked for it, which is why only the
    #: adapted shape gets a grace mode.  Flipped to ``enforce`` by **Package
    #: 6**; the flag and its ``off``/``audit`` modes are removed in
    #: **Package 7**.
    capability_enforcement: str = "audit"
    wal_warn_mb: int = 64  # doctor db.wal_size threshold
    llm_log_warn_mb: int = 512  # doctor logs.llm_size threshold

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        for name in self.env_allowlist:
            if not isinstance(name, str) or not name.strip():
                errors.append(
                    ConfigError(
                        "security",
                        "env_allowlist",
                        f"entries must be non-empty strings, got: {name!r}",
                    )
                )
        if self.wal_warn_mb <= 0:
            errors.append(ConfigError("security", "wal_warn_mb", "must be > 0"))
        if self.llm_log_warn_mb <= 0:
            errors.append(ConfigError("security", "llm_log_warn_mb", "must be > 0"))
        if self.capability_enforcement not in CAPABILITY_ENFORCEMENT_MODES:
            errors.append(
                ConfigError(
                    "security",
                    "capability_enforcement",
                    "must be one of "
                    f"{sorted(CAPABILITY_ENFORCEMENT_MODES)}, got "
                    f"{self.capability_enforcement!r}",
                )
            )
        return errors


@dataclass
class MessagesConfig:
    """Inter-agent message queue.

    Substrate placeholder — see docs/specs/implementation/supervisor-agent.md §10.
    """

    enabled: bool = True  # native supervisor messaging is available by default
    delivery_interval: float = 5.0  # piggybacks the cascade cycle
    reply_timeout: float = 120.0  # transcript-tail fallback trigger
    transcript_tail_fallback: bool = True
    max_inject_per_prompt: int = 10

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.delivery_interval <= 0:
            errors.append(ConfigError("messages", "delivery_interval", "must be > 0"))
        if self.reply_timeout < 0:
            errors.append(ConfigError("messages", "reply_timeout", "must be >= 0"))
        if self.max_inject_per_prompt < 0:
            errors.append(ConfigError("messages", "max_inject_per_prompt", "must be >= 0"))
        return errors


@dataclass
class SupervisorAgentConfig:
    """Supervisor-as-a-session rollout switch.

    Substrate placeholder — see docs/specs/implementation/supervisor-agent.md §10.
    """

    enabled: bool = False  # route project chat to supervisor sessions
    idle_timeout: int = 900  # default for the shipped profile

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.idle_timeout < 0:
            errors.append(ConfigError("supervisor_agent", "idle_timeout", "must be >= 0"))
        return errors


@dataclass
class EventsConfig:
    """Event-bus emission toggles.

    Controls whether cross-cutting bus events are emitted.  Turning a flag off
    silences the corresponding stream on high-throughput deployments where
    the extra frames would swamp downstream consumers.
    """

    #: When True (default), ``CommandHandler.execute`` emits a
    #: ``command.invoked`` event on the bus after every dispatch (success or
    #: failure) with a redacted args summary + duration + ok/error.  See
    #: docs/superpowers/plans/2026-08-21-dv2-phase5-observability.md
    #: ("Phase 5 Follow-up") for the motivation.
    command_invoked_enabled: bool = True

    def validate(self) -> list[ConfigError]:
        return []


def _is_http_origin(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return False
    if any(char in value for char in ("\\", "*", "?", "#", "%")):
        return False
    try:
        parts = urlsplit(value)
        parts.port  # Validate the port range and syntax.
        return (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not parts.path
        )
    except ValueError:
        return False


@dataclass
class ApiAuthConfig:
    """Session-token auth for the local HTTP API.

    Substrate placeholder — see docs/specs/implementation/aq-surface.md §7.
    """

    token_ttl_hours: int = 72  # backstop expiry for session tokens
    require_session_token: bool = False  # reserved enforcement hook
    # Additional browser origins allowed to attach interactive terminals.
    # Same-origin dashboards need no entry; this never grants token privileges.
    trusted_dashboard_origins: list[str] = field(default_factory=list)

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.token_ttl_hours <= 0:
            errors.append(ConfigError("api_auth", "token_ttl_hours", "must be > 0"))
        origins = self.trusted_dashboard_origins
        if not isinstance(origins, list) or any(not _is_http_origin(value) for value in origins):
            errors.append(ConfigError(
                "api_auth", "trusted_dashboard_origins",
                "must be a list of explicit http(s) origins without paths, credentials or wildcards",
            ))
        return errors


@dataclass
class SurfaceConfig:
    """Agent-surface ergonomics knobs.

    Substrate placeholder — see docs/specs/implementation/aq-surface.md §7.
    """

    context_cost_ceiling_tokens: int = 8000  # `aq doctor` warning threshold

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.context_cost_ceiling_tokens < 0:
            errors.append(ConfigError("surface", "context_cost_ceiling_tokens", "must be >= 0"))
        return errors


@dataclass
class StateMachineConfig:
    """Task state-machine enforcement.

    Substrate placeholder — see docs/specs/implementation/work-graph.md §9.
    ``enforce: false`` keeps today's warn-only behavior.
    """

    enforce: bool = False

    def validate(self) -> list[ConfigError]:
        return []


@dataclass
class WorkGraphConfig:
    """Persisted blocked-state projection, gates, and conditional edges.

    See docs/specs/implementation/work-graph.md §9.  The three keys gate
    three *independent* rollout stages — deliberately not chained, so that
    flipping one does not silently arm another:

    ``blocked_state_authoritative``
        ``false`` = shadow mode.  Both the legacy dependency scan and the
        ``is_blocked`` projection are computed every cycle and compared; the
        legacy scan still decides.  Flip after an observation window with
        zero divergence warnings.  Rollback is a config flip.
    ``gate_sweep_interval_seconds``
        Cadence of the cascade's gate sweep; ``0`` disables it entirely.
        This — not ``blocked_state_authoritative`` — is what gates step 2b.
    ``conditional_autoclose``
        Whether the cascade disposes of contingency tasks whose
        ``conditional-blocks`` dependency completed.  On by default per
        design §3.1: such tasks can never run again, and without disposal
        they rot in the queue forever.  ``conditional-blocks`` edges only
        exist where someone explicitly created one, so this is inert on a
        graph that uses none.
    ``container_sweep_interval_seconds``
        Backstop cadence for container settlement (swarm-work-model §7);
        ``0`` disables.
    """

    blocked_state_authoritative: bool = False
    gate_sweep_interval_seconds: int = 30
    conditional_autoclose: bool = True
    container_sweep_interval_seconds: int = 60

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.gate_sweep_interval_seconds < 0:
            errors.append(ConfigError("work_graph", "gate_sweep_interval_seconds", "must be >= 0"))
        if self.container_sweep_interval_seconds < 0:
            errors.append(
                ConfigError("work_graph", "container_sweep_interval_seconds", "must be >= 0")
            )
        return errors


@dataclass
class IntegrationConfig:
    """System-wide default integration policy.

    ``default_mode`` is the last link of the integration-policy chain
    (task override → project policy → this).  Shipped as ``pull_request``
    so a project running the default reviewer/final-reviewer pipeline never
    auto-merges worker output before review; set ``direct`` only for
    deployments that explicitly run without a review policy.
    """

    default_mode: str = "pull_request"

    def validate(self) -> list[ConfigError]:
        from src.models import INTEGRATION_MODES

        errors: list[ConfigError] = []
        if self.default_mode not in INTEGRATION_MODES:
            errors.append(
                ConfigError(
                    "integration",
                    "default_mode",
                    f"must be one of {sorted(INTEGRATION_MODES)}",
                )
            )
        return errors


def _opt_int(value) -> int | None:
    """``int(value)`` unless it is null/blank, in which case ``None``.

    Lets ``cores:`` and friends be written as an explicit ``null`` (or left
    out) to mean "derive it", without an ``int(None)`` crash at load time.
    """
    if value is None or value == "":
        return None
    return int(value)


@dataclass
class ResourceCgroupConfig:
    """Hard per-session limits via cgroup v2 (resource-gating layer 3).

    Off by default because it needs a one-time root step: the daemon's user
    slice must have ``Delegate=yes`` (or a sudoers rule for
    ``systemd-run --scope``) before an unprivileged process may create a
    scope with its own CPU/memory controllers.  See
    ``scripts/setup-cgroup-delegation.sh``.  When delegation is missing the
    launcher logs once and falls back to layer 1 (env caps + nice) rather
    than failing the launch — a box without systemd delegation still has to
    be able to run agents.
    """

    enabled: bool = False
    #: ``CPUQuota=`` percent.  100 = one core; 600 = six cores.
    cpu_quota_percent: int = 600
    #: ``MemoryMax=`` value, in systemd's syntax (``6G``, ``512M``).
    memory_max: str = "6G"

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.cpu_quota_percent <= 0:
            errors.append(
                ConfigError("resources.cgroups", "cpu_quota_percent", "must be positive")
            )
        if not str(self.memory_max).strip():
            errors.append(
                ConfigError("resources.cgroups", "memory_max", "must not be empty")
            )
        return errors


@dataclass
class ResourcesConfig:
    """Resource gating so N concurrent agents cannot saturate one box.

    The problem this exists for: 8 agents each running ``pytest -n auto`` on
    a 24-core box is up to 192 test processes, load 60+, and SIGKILLed
    sessions.  Three layers, each usable on its own:

    1. **Session env caps** — every session launch carries
       ``PYTEST_XDIST_AUTO_NUM_WORKERS`` and the BLAS/OpenMP/libuv thread
       caps derived from :meth:`cpu_share`, plus ``nice -n session_nice`` on
       the harness process so the daemon, dashboard and tmux stay
       responsive under load.
    2. **Global test semaphore** — ``aq test`` takes one of ``test_slots``
       flock slots before running pytest, so the *sum* over all sessions is
       bounded, not just each session individually.
    3. **cgroup scopes** — see :class:`ResourceCgroupConfig`.

    ``cores`` defaults to ``os.cpu_count()``; ``max_concurrent_agents`` is
    the denominator of the per-session share and should match the largest
    project's ``max_concurrent_agents``.
    """

    enabled: bool = True
    #: Physical budget.  ``None`` → ``os.cpu_count()`` at read time.
    cores: int | None = None
    #: How many agents the box is expected to run at once.
    max_concurrent_agents: int = 8
    #: Explicit per-session core share.  ``None`` → derived (see
    #: :meth:`cpu_share`).
    per_session_cpu_share: int | None = None
    #: ``nice`` increment applied to the harness process.  0 disables.
    session_nice: int = 10
    #: Concurrent ``aq test`` runs allowed across the whole box.
    test_slots: int = 2
    #: ``-n`` cap ``aq test`` enforces.  ``None`` → :meth:`cpu_share`.
    test_workers: int | None = None
    #: Seconds ``aq test`` waits for a slot before giving up.
    test_wait_timeout: int = 1800
    #: Slot poll interval while waiting, in seconds.
    test_poll_interval: float = 2.0
    #: ``-m`` expression ``aq test`` applies when the caller passed none.
    test_deselect_markers: str = "not perf and not migration and not slow and not tmux and not integration"
    #: doctor ``resources.load`` warns when the 5-minute load average
    #: exceeds ``cores * load_warn_ratio``.
    load_warn_ratio: float = 1.0
    #: doctor ``resources.test_pressure`` warns above this many pytest
    #: processes box-wide.
    max_pytest_processes: int = 24
    cgroups: ResourceCgroupConfig = field(default_factory=ResourceCgroupConfig)

    def core_count(self) -> int:
        """The core budget: configured ``cores``, else the machine's."""
        if self.cores and self.cores > 0:
            return int(self.cores)
        return os.cpu_count() or 1

    def cpu_share(self) -> int:
        """Cores one session may assume it has.

        ``per_session_cpu_share`` when set, else
        ``cores // max_concurrent_agents``, floored at 1 so a small box or a
        large agent count never derives 0 workers (``-n 0`` is not a thing
        xdist accepts, and ``OMP_NUM_THREADS=0`` is undefined behaviour).
        """
        if self.per_session_cpu_share and self.per_session_cpu_share > 0:
            return int(self.per_session_cpu_share)
        agents = max(1, int(self.max_concurrent_agents or 1))
        return max(1, self.core_count() // agents)

    def test_worker_cap(self) -> int:
        """``-n`` value ``aq test`` enforces."""
        if self.test_workers and self.test_workers > 0:
            return int(self.test_workers)
        return self.cpu_share()

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        for key in ("cores", "per_session_cpu_share", "test_workers"):
            value = getattr(self, key)
            if value is not None and value <= 0:
                errors.append(ConfigError("resources", key, "must be positive or null"))
        for key in ("max_concurrent_agents", "test_slots"):
            if getattr(self, key) <= 0:
                errors.append(ConfigError("resources", key, "must be positive"))
        for key in ("test_wait_timeout", "test_poll_interval", "max_pytest_processes"):
            if getattr(self, key) < 0:
                errors.append(ConfigError("resources", key, "must be >= 0"))
        if not -20 <= self.session_nice <= 19:
            errors.append(ConfigError("resources", "session_nice", "must be between -20 and 19"))
        if self.load_warn_ratio <= 0:
            errors.append(ConfigError("resources", "load_warn_ratio", "must be positive"))
        errors.extend(self.cgroups.validate())
        return errors


@dataclass
class SwarmConfig:
    """Pull-based worker pools (swarm-work-model §10–§12, §17).

    ``enabled`` gates ``_reconcile_pools`` and ``lifecycle: pool`` launches.
    Everything else is a tunable read each tick — hot-reloadable.
    """

    enabled: bool = False
    # Retire a pool conversation after each task; the global worker is reused.
    fresh_context_per_task: bool = True
    claim_wait_max: int = 60  # seconds a `task_claim --wait` may block
    max_starts_per_tick: int = 2
    max_drains_per_tick: int = 5
    scale_down_grace: int = 120  # seconds of surplus before a drain
    prepare_timeout: int = 120  # claim_phase='preparing' older than this is released
    max_filings_per_task: int = 20  # worker-filed tasks per held task

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        for key in ("claim_wait_max", "max_starts_per_tick", "max_drains_per_tick",
                    "scale_down_grace", "prepare_timeout", "max_filings_per_task"):
            if getattr(self, key) < 0:
                errors.append(ConfigError("swarm", key, "must be >= 0"))
        return errors


@dataclass
class MetricsConfig:
    """Fleet metrics sampler and time series (dashboard Metrics tab).

    The two intervals exist because the series have very different costs.
    ``interval_seconds`` drives the cheap tier — two grouped counts, the
    load average and ``/proc/meminfo`` — and is what the dashboard's live
    cadence follows.  ``slow_interval_seconds`` drives everything that
    range-scans an append-only table (the token ledger, the sub-agent event
    fold); those values are carried forward between slow ticks so the
    per-second sample is still complete.

    Retention is per resolution tier, and each tier is pruned against its
    own horizon.  Defaults keep 1 hour of per-second detail, 30 days of
    minutes, and a year of hours.
    """

    enabled: bool = True
    interval_seconds: float = 1.0
    slow_interval_seconds: float = 5.0
    #: Seconds of per-second samples buffered before one batched commit.  A
    #: commit is an fsync; batching turns a per-second fsync into one per
    #: window.  The live chart is fed by the WebSocket tick, not by this
    #: write, so the only thing at risk is the newest few seconds of stored
    #: history if the daemon is killed.
    flush_interval_seconds: float = 5.0
    rollup_interval_seconds: float = 60.0
    retain_seconds_1s: int = 3600
    retain_seconds_1m: int = 30 * 86400
    retain_seconds_1h: int = 365 * 86400

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        for key in (
            "interval_seconds",
            "slow_interval_seconds",
            "flush_interval_seconds",
            "rollup_interval_seconds",
        ):
            if getattr(self, key) <= 0:
                errors.append(ConfigError("metrics", key, "must be > 0"))
        if self.slow_interval_seconds < self.interval_seconds:
            errors.append(
                ConfigError(
                    "metrics",
                    "slow_interval_seconds",
                    "must be >= interval_seconds",
                )
            )
        for key in ("retain_seconds_1s", "retain_seconds_1m", "retain_seconds_1h"):
            if getattr(self, key) < 0:
                errors.append(ConfigError("metrics", key, "must be >= 0"))
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
    messaging_platform: str = "discord"  # "discord" or "none"
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    agents_config: AgentsDefaultConfig = field(default_factory=AgentsDefaultConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    pause_retry: PauseRetryConfig = field(default_factory=PauseRetryConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    chat_analyzer: ChatAnalyzerConfig = field(default_factory=ChatAnalyzerConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    health_check: HealthCheckConfig = field(default_factory=HealthCheckConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    auto_task: AutoTaskConfig = field(default_factory=AutoTaskConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    playbooks: PlaybooksConfig = field(default_factory=PlaybooksConfig)
    mcp_server: McpServerConfig = field(default_factory=McpServerConfig)
    llm_logging: LLMLoggingConfig = field(default_factory=LLMLoggingConfig)
    # -- Framework-overhaul substrate sections (all flags default off) ------
    sessions: SessionsConfig = field(default_factory=SessionsConfig)
    worktrees: WorktreesConfig = field(default_factory=WorktreesConfig)
    streams: StreamsConfig = field(default_factory=StreamsConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    supervisor_agent: SupervisorAgentConfig = field(default_factory=SupervisorAgentConfig)
    api_auth: ApiAuthConfig = field(default_factory=ApiAuthConfig)
    surface: SurfaceConfig = field(default_factory=SurfaceConfig)
    state_machine: StateMachineConfig = field(default_factory=StateMachineConfig)
    work_graph: WorkGraphConfig = field(default_factory=WorkGraphConfig)
    integration: IntegrationConfig = field(default_factory=IntegrationConfig)
    swarm: SwarmConfig = field(default_factory=SwarmConfig)
    resources: ResourcesConfig = field(default_factory=ResourcesConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    agent_profiles: list[AgentProfileConfig] = field(default_factory=list)
    global_token_budget_daily: int | None = None
    max_daily_playbook_tokens: int | None = None
    max_concurrent_playbook_runs: int = 2
    rate_limits: dict[str, dict[str, int]] = field(default_factory=dict)
    memory_extractor: dict = field(
        default_factory=lambda: {
            "enabled": True,
            "batch_window_seconds": 30,
            "max_buffer_size": 1,
            "max_facts_per_batch": 10,
            "max_input_chars": 8000,
        }
    )
    inbox: dict = field(
        default_factory=lambda: {
            "enabled": False,
            "projects": [],
            "oauth_token_path": "~/.config/google-docs-mcp/token.json",
            "client_secret": "$GOOGLE_CLIENT_SECRET",
            "mark_read_on_emit": True,
        }
    )
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

        # Validate messaging_platform field. "telegram" gets a dedicated,
        # actionable error rather than folding into the generic "must be
        # one of" message — Telegram support was removed in the M0
        # messaging strip (docs/specs/design/messaging-rework.md §4.6) and
        # users migrating an old config need a clear pointer, not a silent
        # fallback to "discord" or a generic validation error.
        valid_platforms = {"discord", "none"}
        if self.messaging_platform == "telegram":
            errors.append(
                ConfigError(
                    "app",
                    "messaging_platform",
                    "Telegram support was removed (messaging rework M0). "
                    "Set messaging_platform to 'discord' or 'none'. "
                    "See docs/specs/design/messaging-rework.md",
                )
            )
        elif self.messaging_platform not in valid_platforms:
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

        errors.extend(self.agents_config.validate())
        errors.extend(self.scheduling.validate())
        errors.extend(self.pause_retry.validate())
        errors.extend(self.llm.validate())
        errors.extend(self.chat_analyzer.validate())
        errors.extend(self.supervisor.validate())
        errors.extend(self.auto_task.validate())
        errors.extend(self.archive.validate())
        errors.extend(self.llm_logging.validate())
        errors.extend(self.memory.validate())
        errors.extend(self.mcp_server.validate())
        # -- Framework-overhaul substrate sections --------------------------
        errors.extend(self.playbooks.validate())
        errors.extend(self.sessions.validate())
        errors.extend(self.worktrees.validate())
        errors.extend(self.streams.validate())
        errors.extend(self.security.validate())
        errors.extend(self.pricing.validate())
        errors.extend(self.messages.validate())
        errors.extend(self.supervisor_agent.validate())
        errors.extend(self.api_auth.validate())
        errors.extend(self.surface.validate())
        errors.extend(self.state_machine.validate())
        errors.extend(self.work_graph.validate())
        errors.extend(self.integration.validate())
        errors.extend(self.swarm.validate())
        errors.extend(self.resources.validate())
        errors.extend(self.metrics.validate())
        # Sessions are the only execution path (the runtime subsystem was
        # removed), so a disabled session runtime is a daemon that accepts
        # work and never starts any of it.  Warn, don't reject: the mode is
        # still legitimate for an API/Discord-only daemon and for the test
        # suite, but it must not be something an operator discovers by
        # watching tasks sit in READY forever.
        if not self.sessions.enabled:
            errors.append(
                ConfigError(
                    "sessions",
                    "enabled",
                    "sessions are the only execution path; with this off the "
                    "daemon can dispatch no task at all and work stays READY",
                    severity="warning",
                )
            )
        # ``supervisor_agent.enabled`` needs the message queue and named
        # sessions to exist (supervisor-agent spec §10).
        if self.supervisor_agent.enabled and not (self.messages.enabled and self.sessions.enabled):
            errors.append(
                ConfigError(
                    "supervisor_agent",
                    "enabled",
                    "requires messages.enabled and sessions.enabled",
                )
            )
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
        - discord, database_path, workspace_dir, llm, memory,
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
        updated.chat_analyzer = fresh.chat_analyzer
        updated.max_daily_playbook_tokens = fresh.max_daily_playbook_tokens
        updated.max_concurrent_playbook_runs = fresh.max_concurrent_playbook_runs
        # Substrate sections classified hot-reloadable (work-graph spec §9,
        # trust-and-ops §2).  Inert until their owning lane lands.
        updated.state_machine = fresh.state_machine
        updated.work_graph = fresh.work_graph
        updated.integration = fresh.integration
        updated.swarm = fresh.swarm
        updated.resources = fresh.resources
        updated.pricing = fresh.pricing
        updated.surface = fresh.surface

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
    "chat_analyzer",
    # -- Framework-overhaul substrate sections (inert until their lane) ----
    "state_machine",
    "work_graph",
    "integration",
    "swarm",
    "resources",
    "metrics",
    "pricing",
    "surface",
}
"""Config sections that can be safely updated at runtime without restart."""

RESTART_REQUIRED_SECTIONS = {
    "discord",
    "messaging_platform",
    "data_dir",
    "workspace_dir",
    "database_path",
    "llm",
    "memory",
    "health_check",
    # -- Framework-overhaul substrate sections --------------------------
    # Each of these gates subsystem construction at startup, so a change
    # only takes effect on restart.  (``state_machine`` and ``work_graph``
    # are deliberately absent — work-graph spec §9 calls them
    # hot-reloadable like ``monitoring``.)
    "playbooks",
    "sessions",
    "worktrees",
    "security",
    "messages",
    "supervisor_agent",
    "api_auth",
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
    "agents_config",
    "scheduling",
    "pause_retry",
    "llm",
    "chat_analyzer",
    "health_check",
    "logging",
    "monitoring",
    "archive",
    "auto_task",
    "memory",
    "llm_logging",
    # -- Framework-overhaul substrate sections ----------------------------
    "playbooks",
    "sessions",
    "worktrees",
    "security",
    "pricing",
    "messages",
    "supervisor_agent",
    "api_auth",
    "surface",
    "state_machine",
    "work_graph",
    "swarm",
    "resources",
    "metrics",
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
    """Load .env file from the same directory as the config file.

    .env is the source of truth for daemon credentials; values here override
    any stale values inherited from the shell. (Previously this skipped keys
    already in os.environ, which silently masked .env edits when an old value
    was still exported in the parent shell — see issue #30.)
    """
    env_path = os.path.join(os.path.dirname(config_path), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not key:
                continue
            existing = os.environ.get(key)
            if existing is not None and existing != value:
                logger.warning(
                    "env var %s in shell env differs from %s; using .env value",
                    key,
                    env_path,
                )
            os.environ[key] = value


def _llm_config_from_mapping(m: dict, *, legacy: bool) -> LLMConfig:
    raw_provider = str(m.get("provider", "anthropic") or "anthropic")
    provider = normalize_llm_provider(raw_provider)
    raw_model = m.get("model", "")
    base_url = str(m.get("base_url", "") or "")
    if legacy and raw_provider == "ollama" and not base_url:
        base_url = "http://localhost:11434/v1"
    return LLMConfig(
        provider=provider,
        model=str(raw_model) if raw_model else "",
        api_key=str(m.get("api_key", "") or ""),
        base_url=base_url,
        max_tokens=int(m.get("max_tokens", 4096)),
        default_class=str(m.get("default_class", "") or ""),
    )


def _present_kwargs(section: dict, spec: dict) -> dict:
    """Coerce only keys explicitly present in a YAML section.

    Keeping defaults in both this loader and the dataclasses causes them to
    drift: a partial ``sessions`` section used ``tmux`` while
    :class:`SessionsConfig` used ``subprocess``, and a partial ``worktrees``
    section disabled worktrees although :class:`WorktreesConfig` enables them.
    Passing only supplied values keeps the dataclass as the source of truth.
    """
    return {name: coerce(section[name]) for name, coerce in spec.items() if name in section}


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

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # Determine environment profile for overlay loading
    env = os.environ.get("AGENT_QUEUE_ENV", raw.get("env", "production"))

    # Load environment-specific overlay (e.g. config.dev.yaml)
    config_dir = os.path.dirname(path) or "."
    base_name = os.path.basename(path)
    name_part, ext = os.path.splitext(base_name)
    overlay_path = os.path.join(config_dir, f"{name_part}.{env}{ext}")
    if os.path.exists(overlay_path):
        with open(overlay_path, encoding="utf-8") as f:
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
        with open(profile_path, encoding="utf-8") as f:
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
    if "playbooks" in raw and isinstance(raw["playbooks"], dict):
        pb = raw["playbooks"]
        config.playbooks = PlaybooksConfig(
            enabled=bool(pb.get("enabled", False)),
            v2_storage_enabled=bool(pb.get("v2_storage_enabled", False)),
            v2_max_artifact_bytes=int(pb.get("v2_max_artifact_bytes", 1_048_576)),
            v2_max_result_bytes=int(pb.get("v2_max_result_bytes", 262_144)),
            v2_max_snapshot_bytes=int(pb.get("v2_max_snapshot_bytes", 4_194_304)),
            v2_max_pending_events_per_playbook=int(pb.get("v2_max_pending_events_per_playbook", 1000)),
            v2_pending_event_retention_days=int(pb.get("v2_pending_event_retention_days", 7)),
            v2_receipt_retention_days=int(pb.get("v2_receipt_retention_days", 90)),
            v2_artifact_retention_days=int(pb.get("v2_artifact_retention_days", 90)),
            v2_artifact_min_versions=int(pb.get("v2_artifact_min_versions", 10)),
            v2_retention_sweep_interval_seconds=int(pb.get("v2_retention_sweep_interval_seconds", 3600)),
        )
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

    llm_raw = raw.get("llm")
    legacy_raw = raw.get("chat_provider")
    if isinstance(llm_raw, dict):
        if isinstance(legacy_raw, dict):
            logger.warning(
                "%s: both 'llm:' and legacy 'chat_provider:' are present — using 'llm:' "
                "and ignoring 'chat_provider:'",
                path,
            )
        config.llm = _llm_config_from_mapping(llm_raw, legacy=False)
    elif isinstance(legacy_raw, dict):
        logger.warning(
            "%s: 'chat_provider:' is deprecated — rename the block to 'llm:' "
            "(provider ids: gemini→google, ollama→openai)",
            path,
        )
        config.llm = _llm_config_from_mapping(legacy_raw, legacy=True)

    if "supervisor" in raw:
        s = raw["supervisor"]
        global_section = s.get("global", {}) or {}
        config.supervisor = SupervisorConfig(
            global_=GlobalSupervisorConfig(
                idle_timeout_seconds=global_section.get("idle_timeout_seconds", 2700),
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
            max_verification_retries=at.get("max_verification_retries", 2),
        )

    if "memory" in raw:
        mem = raw["memory"]
        # Defaults must match MemoryConfig dataclass defaults
        # (enabled=True, embedding_provider="ollama") so a partial memory:
        # section (e.g. just an api_key override) does not silently flip
        # callers onto a different provider.  The aq-memory plugin's
        # pyproject.toml installs `memsearch[ollama]` to support the
        # "ollama" default; deploying with `embedding_provider: openai`
        # is opt-in and requires an api key.
        config.memory = MemoryConfig(
            enabled=mem.get("enabled", False),
            embedding_provider=mem.get("embedding_provider", "ollama"),
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
            stub_enrichment_enabled=mem.get("stub_enrichment_enabled", True),
            stub_enrichment_class=str(mem.get("stub_enrichment_class", "") or ""),
            stub_enrichment_max_source_chars=int(
                mem.get("stub_enrichment_max_source_chars", 20_000)
            ),
        )

    if "mcp_server" in raw:
        ms = raw["mcp_server"]
        config.mcp_server = McpServerConfig(
            enabled=ms.get("enabled", False),
            host=ms.get("host", "127.0.0.1"),
            port=ms.get("port", 8081),
            excluded_commands=ms.get("excluded_commands", []),
            inject_into_tasks=ms.get("inject_into_tasks", True),
        )
        ts = ms.get("task_scope")
        if isinstance(ts, dict):
            config.mcp_server.task_scope = McpTaskScopeConfig(
                enabled=bool(ts.get("enabled", False)),
                allowlist_extra=ts.get("allowlist_extra", []),
            )

    if "llm_logging" in raw:
        ll = raw["llm_logging"]
        config.llm_logging = LLMLoggingConfig(
            enabled=ll.get("enabled", False),
            retention_days=ll.get("retention_days", 30),
        )

    # -- Framework-overhaul substrate sections (Wave 0) ---------------------
    # Defaults belong to the dataclasses.  Supplying only the YAML keys that
    # exist makes a partial section equivalent to its default construction.

    if "sessions" in raw and isinstance(raw["sessions"], dict):
        config.sessions = SessionsConfig(
            **_present_kwargs(
                raw["sessions"],
                {
                    "enabled": bool,
                    "provider": str,
                    "tmux_socket": str,
                    "lease_ttl_seconds": int,
                    "stall_max_nudges": int,
                    "stall_backoff_seconds": int,
                    "max_restarts": int,
                    "restart_window_seconds": int,
                    "restart_backoff_seconds": int,
                    "dialog_budget_seconds": int,
                    "dialog_settle_seconds": float,
                    "nudge_debounce_ms": int,
                    "state_cache_ttl_seconds": int,
                    "transcript_poll_seconds": int,
                    "adopt_on_start": bool,
                    "pane_stream_interval_seconds": float,
                    "pane_stream_max_sessions": int,
                    "pane_stream_lines": int,
                },
            )
        )

    if "worktrees" in raw and isinstance(raw["worktrees"], dict):
        config.worktrees = WorktreesConfig(
            **_present_kwargs(
                raw["worktrees"],
                {
                    "enabled": bool,
                    "retain_failed_days": int,
                    "merge_slot_ttl_seconds": int,
                    "prune_remote_branches": bool,
                    "setup_timeout_seconds": int,
                    "salvage_dirty": bool,
                    "salvage_max_bytes": int,
                    "spawn_conflict_continuation": bool,
                },
            )
        )

    if "security" in raw and isinstance(raw["security"], dict):
        sec = raw["security"]
        config.security = SecurityConfig(
            env_scrub_enabled=bool(sec.get("env_scrub_enabled", True)),
            env_allowlist=sec.get("env_allowlist", []),
            wal_warn_mb=int(sec.get("wal_warn_mb", 64)),
            llm_log_warn_mb=int(sec.get("llm_log_warn_mb", 512)),
            capability_enforcement=str(sec.get("capability_enforcement", "audit")),
        )

    if "pricing" in raw:
        # YAML shape is a *list* of {model, input_per_mtok, output_per_mtok}
        # maps (trust-and-ops §2); a mapping with a "models" key is also
        # accepted so the round-trip writer can emit either form.
        pricing_raw = raw["pricing"]
        if isinstance(pricing_raw, dict):
            pricing_raw = pricing_raw.get("models", [])
        entries: list[ModelPricing] = []
        for row in pricing_raw or []:
            if not isinstance(row, dict):
                continue
            entries.append(
                ModelPricing(
                    model=str(row.get("model", "")),
                    input_per_mtok=float(row.get("input_per_mtok", 0.0)),
                    output_per_mtok=float(row.get("output_per_mtok", 0.0)),
                )
            )
        config.pricing = PricingConfig(models=entries)

    if "messages" in raw and isinstance(raw["messages"], dict):
        ms_cfg = raw["messages"]
        config.messages = MessagesConfig(
            enabled=bool(ms_cfg.get("enabled", True)),
            delivery_interval=float(ms_cfg.get("delivery_interval", 5.0)),
            reply_timeout=float(ms_cfg.get("reply_timeout", 120.0)),
            transcript_tail_fallback=bool(ms_cfg.get("transcript_tail_fallback", True)),
            max_inject_per_prompt=int(ms_cfg.get("max_inject_per_prompt", 10)),
        )

    if "events" in raw and isinstance(raw["events"], dict):
        ev = raw["events"]
        config.events = EventsConfig(
            command_invoked_enabled=bool(ev.get("command_invoked_enabled", True)),
        )

    if "supervisor_agent" in raw and isinstance(raw["supervisor_agent"], dict):
        sa = raw["supervisor_agent"]
        config.supervisor_agent = SupervisorAgentConfig(
            enabled=bool(sa.get("enabled", False)),
            idle_timeout=int(sa.get("idle_timeout", 900)),
        )

    if "api_auth" in raw and isinstance(raw["api_auth"], dict):
        aa = raw["api_auth"]
        config.api_auth = ApiAuthConfig(
            token_ttl_hours=int(aa.get("token_ttl_hours", 72)),
            require_session_token=bool(aa.get("require_session_token", False)),
            trusted_dashboard_origins=aa.get("trusted_dashboard_origins", []),
        )

    if "surface" in raw and isinstance(raw["surface"], dict):
        sf = raw["surface"]
        config.surface = SurfaceConfig(
            context_cost_ceiling_tokens=int(sf.get("context_cost_ceiling_tokens", 8000)),
        )

    if "state_machine" in raw and isinstance(raw["state_machine"], dict):
        sm = raw["state_machine"]
        config.state_machine = StateMachineConfig(enforce=bool(sm.get("enforce", False)))

    if "work_graph" in raw and isinstance(raw["work_graph"], dict):
        wg = raw["work_graph"]
        config.work_graph = WorkGraphConfig(
            blocked_state_authoritative=bool(wg.get("blocked_state_authoritative", False)),
            gate_sweep_interval_seconds=int(wg.get("gate_sweep_interval_seconds", 30)),
            conditional_autoclose=bool(wg.get("conditional_autoclose", True)),
            container_sweep_interval_seconds=int(wg.get("container_sweep_interval_seconds", 60)),
        )

    if "integration" in raw and isinstance(raw["integration"], dict):
        integ = raw["integration"]
        config.integration = IntegrationConfig(
            default_mode=str(integ.get("default_mode", "pull_request")),
        )

    if "swarm" in raw and isinstance(raw["swarm"], dict):
        sw = raw["swarm"]
        config.swarm = SwarmConfig(
            enabled=bool(sw.get("enabled", False)),
            fresh_context_per_task=bool(sw.get("fresh_context_per_task", True)),
            claim_wait_max=int(sw.get("claim_wait_max", 60)),
            max_starts_per_tick=int(sw.get("max_starts_per_tick", 2)),
            max_drains_per_tick=int(sw.get("max_drains_per_tick", 5)),
            scale_down_grace=int(sw.get("scale_down_grace", 120)),
            prepare_timeout=int(sw.get("prepare_timeout", 120)),
            max_filings_per_task=int(sw.get("max_filings_per_task", 20)),
        )

    if "resources" in raw and isinstance(raw["resources"], dict):
        res = raw["resources"]
        cg_raw = res.get("cgroups")
        cgroups = ResourceCgroupConfig()
        if isinstance(cg_raw, dict):
            cgroups = ResourceCgroupConfig(
                enabled=bool(cg_raw.get("enabled", False)),
                cpu_quota_percent=int(cg_raw.get("cpu_quota_percent", 600)),
                memory_max=str(cg_raw.get("memory_max", "6G")),
            )
        config.resources = ResourcesConfig(
            enabled=bool(res.get("enabled", True)),
            cores=_opt_int(res.get("cores")),
            max_concurrent_agents=int(res.get("max_concurrent_agents", 8)),
            per_session_cpu_share=_opt_int(res.get("per_session_cpu_share")),
            session_nice=int(res.get("session_nice", 10)),
            test_slots=int(res.get("test_slots", 2)),
            test_workers=_opt_int(res.get("test_workers")),
            test_wait_timeout=int(res.get("test_wait_timeout", 1800)),
            test_poll_interval=float(res.get("test_poll_interval", 2.0)),
            test_deselect_markers=str(
                res.get("test_deselect_markers", ResourcesConfig.test_deselect_markers)
            ),
            load_warn_ratio=float(res.get("load_warn_ratio", 1.0)),
            max_pytest_processes=int(res.get("max_pytest_processes", 24)),
            cgroups=cgroups,
        )

    if "metrics" in raw and isinstance(raw["metrics"], dict):
        met = raw["metrics"]
        config.metrics = MetricsConfig(
            enabled=bool(met.get("enabled", True)),
            interval_seconds=float(met.get("interval_seconds", 1.0)),
            slow_interval_seconds=float(met.get("slow_interval_seconds", 5.0)),
            flush_interval_seconds=float(met.get("flush_interval_seconds", 5.0)),
            rollup_interval_seconds=float(met.get("rollup_interval_seconds", 60.0)),
            retain_seconds_1s=int(met.get("retain_seconds_1s", 3600)),
            retain_seconds_1m=int(met.get("retain_seconds_1m", 30 * 86400)),
            retain_seconds_1h=int(met.get("retain_seconds_1h", 365 * 86400)),
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
                    harness_tools=pdata.get("harness_tools"),
                    aq_commands=pdata.get("aq_commands"),
                    plugin_tools=pdata.get("plugin_tools"),
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

    if "inbox" in raw:
        config.inbox = {**config.inbox, **raw["inbox"]}

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
