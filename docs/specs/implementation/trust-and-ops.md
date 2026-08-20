---
tags: [implementation, trust, security, ops, doctor, costs, invariants]
---

# Trust & Ops — Implementation Spec

**Status:** Draft — approved direction (2026-08-19)
**Related:** [[design/trust-and-ops]] (design), [[design/session-runtime]], [[design/worktree-execution]], [[design/aq-surface]], [[design/feature-pauses]], `docs/analysis/framework-overhaul-todo.md` (§10)

Implements the design spec `docs/specs/design/trust-and-ops.md`: env scrubbing,
trust-boundary remediations, `aq doctor`, `aq costs`, and the invariant test suite.
All integration points below were verified against the code on 2026-08-19.

---

## 1. Module Layout

| Path | Status | Contents |
|---|---|---|
| `src/env_scrub.py` | new | `scrub_env()`, pattern/exemption constants |
| `src/doctor/__init__.py` | new | re-exports |
| `src/doctor/models.py` | new | `Severity`, `CheckResult`, `DoctorCheck`, `DoctorContext` |
| `src/doctor/runner.py` | new | registry + concurrent runner + `--fix` orchestration |
| `src/doctor/builtin.py` | new | the generic built-in checks (§5.2 of design) |
| `src/commands/ops_commands.py` | new | `OpsCommandsMixin`: `_cmd_doctor`, `_cmd_get_costs` |
| `src/cli/doctor.py` | new | `aq doctor`, `aq costs` Click commands |
| `src/config.py` | change | `SecurityConfig`, `ModelPricing`, pricing/security wiring |
| `src/runtimes/_subprocess.py` | change | `isolated_env()` delegates to `scrub_env()` |
| `src/git/manager.py` | change | refname validation, `--` separators |
| `src/commands/system_commands.py` | change | `_cmd_run_command` uses scrubbed env |
| `src/commands/handler.py` | change | add `OpsCommandsMixin` to bases |
| `src/plugins/base.py` | change | `PluginContext.register_doctor_check()` |
| `src/database/queries/token_queries.py` | change | extended writer + `get_cost_rollup()` |
| `src/database/tables.py` + migration | change | `token_ledger` gains `model`, `input_tokens`, `output_tokens` |
| `tests/test_env_scrub.py`, `tests/test_doctor.py`, `tests/test_costs.py`, `tests/test_docs_sync.py`, `tests/test_command_surface.py` | new | see §8 |

---

## 2. Config (`src/config.py`)

Follow the existing dataclass-per-section pattern (each section has
`validate() -> list[ConfigError]`; `AppConfig` aggregates at line 803 and delegates
in `AppConfig.validate()` at line 912).

```python
@dataclass
class ModelPricing:
    model: str = ""                 # glob, fnmatch-style; entries match in order
    input_per_mtok: float = 0.0     # USD per million input tokens
    output_per_mtok: float = 0.0

@dataclass
class PricingConfig:
    models: list[ModelPricing] = field(default_factory=list)

    def match(self, model: str) -> ModelPricing | None: ...   # first fnmatch win
    def validate(self) -> list[ConfigError]: ...              # non-negative, non-empty model

@dataclass
class SecurityConfig:
    env_scrub_enabled: bool = True          # kill switch, default on
    env_allowlist: list[str] = field(default_factory=list)   # names or globs
    wal_warn_mb: int = 64                   # doctor db.wal_size threshold
    llm_log_warn_mb: int = 512              # doctor logs.llm_size threshold

    def validate(self) -> list[ConfigError]: ...
```

Integration points:

- `AppConfig` gains `security: SecurityConfig` and `pricing: PricingConfig` fields
  (insert alongside `llm_logging` at `src/config.py:845`).
- `AppConfig.validate()` appends `self.security.validate()` and
  `self.pricing.validate()` next to `errors.extend(self.llm_logging.validate())`
  (`src/config.py:991`).
- `load_config()` parses the new sections in the same `m.get(...)` style used for
  `monitoring` (`src/config.py:1605`). YAML shape: `pricing:` is a **list** of
  `{model, input_per_mtok, output_per_mtok}` maps (per the canonical decision), which
  `load_config` wraps into `PricingConfig(models=[...])`; `security:` is a map.
- `get_config_schema` in `src/config_editor.py` picks the new sections up from the
  dataclasses; verify the round-trip writer preserves the pricing list.

---

## 3. Env Scrubbing (`src/env_scrub.py`)

```python
SENSITIVE_ENV_PATTERNS: tuple[str, ...] = (
    "TOKEN", "API_KEY", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY", "AUTH",
)
# False positives of the AUTH pattern — always exempt:
BUILTIN_EXEMPT: tuple[str, ...] = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE")
STRIP_ALWAYS: tuple[str, ...] = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")

@dataclass
class ScrubResult:
    env: dict[str, str]
    dropped: list[str]          # key NAMES only — never values — for audit/logging

def scrub_env(
    base: Mapping[str, str] | None = None,   # default: os.environ
    *,
    allowlist: Iterable[str] = (),           # config security.env_allowlist (names/globs)
    explicit: Mapping[str, str] | None = None,  # harness/profile env — always wins
    enabled: bool = True,                    # security.env_scrub_enabled
) -> ScrubResult: ...
```

Semantics (matches design §3): drop a key when `any(p in key.upper() for p in
SENSITIVE_ENV_PATTERNS)` unless the key matches `BUILTIN_EXEMPT` or an `allowlist`
entry (exact or `fnmatch`, case-insensitive); `STRIP_ALWAYS` keys are removed
regardless; `explicit` entries are merged last and are never scrubbed. When
`enabled=False`, only `STRIP_ALWAYS` applies (preserves today's behavior as the
escape hatch). Pure function, no I/O — trivially testable.

Integration points:

- `src/runtimes/_subprocess.py:25` — `isolated_env(extra)` becomes:
  `return scrub_env(allowlist=..., explicit=extra, enabled=...).env`. Callers
  (`ACPXRuntime`) are unchanged. The scrub policy thereby survives this module's
  planned deletion (overhaul A.8): [[session-runtime]]'s `SessionSpec` builder calls
  `scrub_env()` directly and injects `AQ_*` markers plus `AQ_API_TOKEN` (minted by
  [[aq-surface]]) via `explicit`.
- `src/commands/system_commands.py:690` — `_cmd_run_command` passes
  `env=scrub_env(...).env` into `_run_subprocess_shell` (add an `env` kwarg to
  `_run_subprocess_shell` at `src/commands/helpers.py:127`). The command remains in
  `DEFAULT_EXCLUDED_COMMANDS` (`src/mcp_registration.py:51-57`) — do not remove it.
- Plumbing the allowlist requires config access; both call sites already hold
  `self.config`.

---

## 4. GitManager Remediation (`src/git/manager.py`)

Audit findings and fixes (design §2.5):

1. Add a module-level guard used by every API that accepts a ref/branch name:

```python
_REFNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")   # no leading '-', no spaces

def _validate_ref(name: str) -> str:
    """Raise GitError unless *name* is a plausible refname (git check-ref-format
    subset). Blocks argument injection via names beginning with '-'."""
```

   Apply in: `acreate_branch`, `acheckout_branch`, `aswitch_to_branch`,
   `apull_branch`, `apush_branch`, `arebase_onto`, `amerge_branch`,
   `adelete_branch`, `aget_diff(base_branch=...)`, and `acreate_pr(base=, branch=)`.

2. Add `--` separators on commands that take pathspecs or where a ref precedes
   free-form args (e.g. `["checkout", "--", ...]` is not valid for branches — use
   `["switch", branch]` after validation instead; for file-level ops the existing
   `["reset", "HEAD", "--", pattern]` at line 1589 is already correct — keep it the
   template).

3. `acommit_all` (line 1560) and `acreate_pr` (line 1637) are verified compliant
   (untrusted text only as flag values) — no change, but note them in the docstring
   as trust-boundary examples.

4. `_SUBPROCESS_ENV` (line 90) keeps `**os.environ` for now (daemon-side tool);
   revisit when [[worktree-execution]] centralizes git invocation.

---

## 5. Doctor

### 5.1 Models (`src/doctor/models.py`)

```python
class Severity(str, Enum):
    OK = "ok"; INFO = "info"; WARN = "warn"; ERROR = "error"

@dataclass
class CheckResult:
    id: str
    severity: Severity
    detail: str
    fixable: bool = False
    fix_applied: bool = False
    duration_ms: int = 0
    data: dict = field(default_factory=dict)   # structured extras for --json/dashboard

@dataclass
class DoctorCheck:
    id: str                                     # namespaced: "db.wal_size"
    run: Callable[[DoctorContext], Awaitable[CheckResult]]
    fix: Callable[[DoctorContext], Awaitable[CheckResult]] | None = None
    timeout_s: float = 5.0                      # mcp.probes overrides to 10.0
    owner: str = "core"                         # "core" | "session-runtime" | "plugin:<name>" | ...

@dataclass
class DoctorContext:
    config: AppConfig
    db: Database
    handler: "CommandHandler"      # for checks that reuse commands (tasks.stuck)
```

### 5.2 Runner (`src/doctor/runner.py`)

```python
class DoctorRegistry:
    def register(self, check: DoctorCheck) -> None: ...      # raises on duplicate id
    def checks(self) -> list[DoctorCheck]: ...

async def run_doctor(
    registry: DoctorRegistry, ctx: DoctorContext, *,
    fix: bool = False, only: list[str] | None = None,
) -> dict:
    """Run all (or *only*) checks concurrently via asyncio.gather with per-check
    asyncio.timeout. A crashed/timed-out check yields
    CheckResult(id, ERROR, "check failed: ...", fixable=False). When fix=True and a
    failing check has .fix, await fix() then re-run run() and set fix_applied.
    Returns {"checks": [...], "summary": {"ok": n, "info": n, "warn": n,
    "error": n, "fixes_applied": n}, "exit_code": 0|1|2}."""
```

Exit-code mapping (design §5.6): errors→2, else warns→1, else 0; the CLI maps a
runner crash to 3.

### 5.3 Built-in checks (`src/doctor/builtin.py`)

One `def builtin_checks(...) -> list[DoctorCheck]` factory. Implementation notes per
check (ids per design §5.2):

- `config.parse` — re-run `load_config(config._config_path)` in-process, catch
  `ConfigValidationError`; map warnings to WARN.
- `db.connect` — `await ctx.db.list_agents()` (same probe `_health_checks` uses,
  `src/main.py:256-261`).
- `db.migrations` — `alembic.script.ScriptDirectory.from_config` head vs
  `alembic_version` row read via the engine. Not fixable; `detail` includes the
  remediation command.
- `vault.parse` — reuse the profile parser (`src/profiles/parser.py`), workspace-kind
  parser, MCP registry loader in report-only mode; one ERROR result per broken file,
  collapsed into `data["files"]`.
- `harness.binaries` — `shutil.which` + `--version` subprocess (argv list, 5 s
  timeout) for `git`, `gh`, plus each harness/runtime binary referenced by an active
  profile; `tmux` deferred to the contributed check.
- `leases.stale`, `sessions.stale`, `tmux.server`, `worktrees.orphans` — **not
  implemented here**: [[session-runtime]] and [[worktree-execution]] register them
  at startup via `DoctorRegistry.register()`. Doctor ships the ids reserved and
  reports `info: "check not registered (subsystem not enabled)"` when absent.
- `db.wal_size` — stat `<db>.url + "-wal"` (SQLite only; INFO on PostgreSQL);
  fix = `PRAGMA wal_checkpoint(TRUNCATE)` through the engine.
- `logs.llm_size` — walk `logs/llm/` (base dir from `LLMLogger`); warn over
  `security.llm_log_warn_mb` or when date dirs exceed `llm_logging.retention_days`;
  fix = `LLMLogger.cleanup_old_logs()` (`src/llm_logger.py:324`).
- `tasks.stuck` — delegate to `_cmd_get_stuck_tasks`
  (`src/commands/system_commands.py:25`) with
  `monitoring.stuck_task_threshold_seconds`.
- `pauses.active` — read pause flags ([[feature-pauses]]: `memory.enabled`,
  `playbooks.enabled`, orchestrator paused state); always INFO.
- `events.registry` — `registered_event_types()` (`src/event_schemas.py:518`) vs the
  union of core emit sites and `PluginContext._event_type_registry`; WARN listing
  unregistered types.
- `mcp.probes` — `probe_many` (`src/profiles/mcp_probe.py:212`), 10 s timeout.

### 5.4 Command surface (`src/commands/ops_commands.py`)

```python
class OpsCommandsMixin:
    async def _cmd_doctor(self, args: dict) -> dict:
        """Run health checks. args: {fix: bool = False, checks: list[str] | None,
        json: ignored (CLI concern)}. Returns {"success": True, "checks": [...],
        "summary": {...}, "exit_code": int}."""

    async def _cmd_get_costs(self, args: dict) -> dict:
        """Cost rollup. args: {project_id: str | None, since: str | None
        (ISO date or '7d'), group_by: 'project'|'profile'|'day' = 'project'}.
        Returns {"success": True, "rows": [...], "total_cost_usd": float,
        "unpriced_tokens": int, "pricing_models": [...]}."""
```

Integration points (verified):

- Add `OpsCommandsMixin` to the `CommandHandler` bases list at
  `src/commands/handler.py:108-122` (after `SystemCommandsMixin`).
- The `DoctorRegistry` instance is constructed in `src/main.py` startup, populated
  with `builtin_checks()`, handed to `CommandHandler` (new ctor kwarg, default
  `None` → doctor reports "not configured") and to the plugin loader.
- MCP: auto-discovery in `register_command_tools` (`src/mcp_registration.py:6-13`)
  exposes both commands; add explicit entries to `_ALL_TOOL_DEFINITIONS`
  (`src/tools/definitions.py`) so schemas are typed rather than docstring-derived.
  Do **not** add them to `DEFAULT_EXCLUDED_COMMANDS`.
- Health endpoint: `_health_checks` (`src/main.py:248`) stays as the cheap liveness
  probe for `/health` and `/ready` (`src/api/health.py:46,70`); the dashboard doctor
  page calls the command through the existing command-execution API. No change to
  `src/api/health.py` beyond a docstring cross-reference.

### 5.5 Plugin hook (`src/plugins/base.py`)

Next to `register_command` (`src/plugins/base.py:316`):

```python
def register_doctor_check(self, check: "DoctorCheck") -> None:
    """Contribute a doctor check. The id is prefixed 'plugin.<name>.'.
    Fixes must obey the --fix safety rules (design §5.4)."""
```

`PluginContext.__init__` gains `doctor_registry: DoctorRegistry | None = None`
(threaded through `src/plugins/registry.py` the same way `command_registry` is —
`Orchestrator.doctor_registry` is set in `src/main.py` *before* `initialize()` and
passed into `PluginRegistry`). When `None` (tests, minimal contexts), registration
is a logged no-op.

#### Contributed-check registration contract (as landed)

The reserved ids live in `src/doctor/models.py` as `RESERVED_CHECK_IDS`, mapping
each id to its owning workstream:

| id | owner |
|---|---|
| `sessions.stale` | [[session-runtime]] |
| `tmux.server` | [[session-runtime]] |
| `worktrees.orphans` | [[worktree-execution]] |
| `leases.stale` | [[worktree-execution]] |

Doctor does **not** pre-register them. Until an owner claims an id, `run_doctor`
synthesises `info: "check not registered (subsystem not enabled)"` with
`data.owner` set and `data.reserved = true`, so the catalog stays complete and CI
never fails on an absent subsystem. To claim one:

1. Build a `DoctorCheck` whose `id` is exactly the reserved string and whose
   `owner` names the workstream.
2. Register it on the daemon-wide registry at startup —
   `orchestrator.doctor_registry.register(check)` for core subsystems,
   `PluginContext.register_doctor_check()` for plugins (which prefixes
   `plugin.<name>.`). Registering a reserved id replaces the placeholder;
   registering any *other* duplicate id raises `ValueError`.
3. Obey the `--fix` safety rules for anything declared fixable — in particular
   `worktrees.orphans` may only run `git worktree prune` and must never delete a
   directory.

`tests/test_doctor.py::TestReservedChecks` pins all three clauses.

### 5.6 CLI (`src/cli/doctor.py`)

`aq doctor [--fix] [--json] [--check ID ...]` and `aq costs [--project ID]
[--since 7d|YYYY-MM-DD] [--group-by project|profile|day] [--json]` — Click commands
delegating to the daemon via the REST client like `src/cli/tasks.py`; registered by
import in `src/cli/app.py` (module-import registration block, `src/cli/app.py:246`).
`aq doctor` exits with the returned `exit_code` (3 on transport/daemon failure).
Rich table output by default; raw command result under `--json`.

---

## 6. Costs

### 6.1 Schema (`src/database/tables.py` + Alembic)

Add to `token_ledger` (line 163): `Column("model", Text, nullable=True)`,
`Column("input_tokens", Integer, nullable=True)`,
`Column("output_tokens", Integer, nullable=True)`. Generate with
`alembic revision --autogenerate -m "token_ledger pricing columns"`; verify the
autogen for both SQLite and PostgreSQL; update `docs/specs/database.md` (the
docs-sync test will enforce this).

### 6.2 Queries (`src/database/queries/token_queries.py`)

- Extend `record_token_usage(project_id, agent_id, task_id, tokens, *, model=None,
  input_tokens=None, output_tokens=None)` (line 17). Current call sites
  (`src/orchestrator/execution.py:866`, `src/orchestrator/sync_workflow.py:324`)
  pass the split/model where their runtime result carries usage; otherwise unchanged.
  Transcript readers ([[session-runtime]] A.6) become the primary fully-populated
  writer.
- New `get_cost_rollup(*, project_id=None, since_ts=None, group_by="project")
  -> list[dict]`: SUM of `input_tokens`/`output_tokens`/`tokens_used` grouped by
  project / profile (join `agents.profile_id`, `src/database/tables.py:151`) / day
  (group in Python like `get_token_audit`, line 196, to stay dialect-agnostic).
  Rows with NULL model or NULL splits aggregate into `unpriced_tokens`.

### 6.3 Pricing application

`_cmd_get_costs` maps each rollup row's `model` through `PricingConfig.match()` and
computes `cost_usd = input_tokens * in_rate / 1e6 + output_tokens * out_rate / 1e6`.
No pricing entry, or no split → the row's tokens count toward `unpriced_tokens` and
`cost_usd` is null for that row. Never estimate.

---

## 7. Checklist

- [x] `src/env_scrub.py` with `scrub_env`, constants, `ScrubResult`
- [x] `SecurityConfig` + `PricingConfig`/`ModelPricing` in `src/config.py`; wired into `AppConfig`, `validate()`, `load_config()`; config_editor round-trip verified
- [x] `isolated_env` delegates to `scrub_env` (`src/runtimes/_subprocess.py`)
- [x] `_cmd_run_command` + `_run_subprocess_shell` accept/pass scrubbed env
- [x] `_validate_ref` guard + `--` audit applied across `GitManager` branch APIs
- [x] `src/doctor/` package: models, runner, builtin checks
- [x] `OpsCommandsMixin` (`_cmd_doctor`, `_cmd_get_costs`) added to `CommandHandler` bases
- [x] `DoctorRegistry` constructed in `src/main.py`, handed to handler + plugin loader
- [x] `PluginContext.register_doctor_check` + registry threading
- [x] Explicit tool definitions for `doctor` / `get_costs` in `src/tools/definitions.py`
- [x] `src/cli/doctor.py` (`aq doctor`, `aq costs`) registered in `src/cli/app.py`; exit codes 0/1/2/3
- [x] Alembic migration: `token_ledger.model/input_tokens/output_tokens` (landed in Wave 0, revision `93a8a9e48fb8`); `docs/specs/database.md` updated — the table catalog is now enforced by `tests/test_docs_sync.py`
- [x] `record_token_usage` extension + `get_cost_rollup`
- [x] Invariant tests: `tests/test_docs_sync.py`, `tests/test_command_surface.py`; event-registry and state-machine tests extended
- [ ] Golden harness test scaffold — **deferred**: it asserts against a `SessionSpec` type that [[design/session-runtime]] owns and has not landed
- [x] Reserve contributed-check ids (`sessions.stale`, `tmux.server`, `worktrees.orphans`, `leases.stale`) and document the registration contract for session-runtime / worktree-execution
- [x] Document `docs/gates/<change>.md` convention in `docs/specs/design/trust-and-ops.md` §8 and exercise it — see `docs/gates/wave1-1c-trust-ops.md`. No PR template exists yet to reference it from.

## 8. Test Plan

Naming follows the existing `tests/test_<subject>.py` convention; all async via
pytest-asyncio auto mode; suite runs under `pytest tests/ -n auto`.

| File | Coverage |
|---|---|
| `tests/test_env_scrub.py` | each pattern drops matching keys case-insensitively (`MY_TOKEN`, `api_key_2`, `GithubAuth`); `GIT_AUTHOR_NAME` exempt; allowlist exact + glob; `explicit` wins over patterns; `enabled=False` strips only `STRIP_ALWAYS`; `dropped` lists names, values never appear; `os.environ` untouched |
| `tests/test_doctor.py` | runner: concurrency, per-check timeout → ERROR, crashing check isolated; duplicate id rejected; `only` filter; severity→exit-code mapping; `--fix` calls fix then re-runs; **fix idempotency**: every builtin fix invoked twice on the same fixture state without error or second-run mutation; fake checks for reserved contributed ids report INFO when unregistered |
| `tests/test_doctor.py` (builtins) | `config.parse` against a broken temp config; `db.migrations` against an in-memory DB behind head; `db.wal_size` fix truncates WAL on a real temp SQLite file; `logs.llm_size` fix removes only beyond-retention dirs (reuses `LLMLogger` fixture); `events.registry` flags a synthetic unregistered emit |
| `tests/test_costs.py` | rollup by project/profile/day on seeded ledger rows; glob pricing match order; rows without split → `unpriced_tokens`, no estimated cost; migration columns present (extend `tests/test_database.py` for both dialects) |
| `tests/test_docs_sync.py` | `metadata.tables` ⇄ `docs/specs/database.md` names, exclusion list explicit — expected to **fail on first run** (the doc is stale, design §6) and drive the doc update |
| `tests/test_command_surface.py` | every `_cmd_*` on `CommandHandler` is in `_ALL_TOOL_DEFINITIONS` ∪ `get_effective_exclusions()` ∪ `KNOWN_AUTO_REGISTERED` (a checked-in list that must shrink, never silently grow) |
| existing `tests/test_event_schema_registry_validation.py`, `tests/test_state_machine.py` | extended for registry completeness and the enforcement-flag contract (flag itself lands with Workstream D) |
| `tests/test_git_manager*.py` | `_validate_ref` rejects `-oops`, `a b`, empty; accepts `aq/task-1`, `feature/x.y`; branch APIs raise `GitError` before spawning git |
| CLI | `aq doctor` exit codes asserted via `CliRunner` with a stubbed client |

## 9. Risks

| Risk | Mitigation |
|---|---|
| Scrub breaks a working install (agent needed `OPENAI_API_KEY` etc.) | explicit-env-wins rule + `env_allowlist` + `env_scrub_enabled` kill switch; `dropped` names logged at session start so the cause is visible in `aq doctor`/logs |
| `AUTH` pattern false positives beyond `GIT_AUTHOR_*` | built-in exemptions cover the known set; allowlist covers the rest; doctor surfaces dropped names |
| Refname validation rejects a legitimate exotic branch name | regex is a conservative subset of `git check-ref-format`; error message names the offending value and the escape hatch (rename or allow via explicit operator action) |
| `--fix` deletes something it shouldn't | safety rules are enforced by review + the double-run idempotency tests; fixes limited to the enumerated list; worktree fix restricted to `git worktree prune` (registrations only) |
| Doctor check hangs the command path | per-check `asyncio.timeout`, bounded overall; checks run read-only except explicit fixes |
| Docs-sync tests are brittle (prose parsing) | match only backtick-quoted table names against an explicit exclusion list; failure message says exactly which side to update |
| Ledger columns nullable forever, costs stay mostly "unpriced" | acceptable by design (honesty over estimates); transcript readers make new rows fully priced; `aq costs` prints the unpriced share so coverage is visible |
| Cross-spec drift (contributed check ids, `AQ_API_TOKEN` contract) | ids and contracts stated identically in both this spec and the owning specs; the design spec §9 ownership table is the tie-breaker |
