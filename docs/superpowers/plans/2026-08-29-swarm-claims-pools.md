# Swarm Work Model — Plan 2: Claims, Pools, Worker Loop, Worker-Filed Work

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let agents pull their own work: an atomic, epoch-fenced `task_claim`; worker pools sized per (project, profile) by a new cascade step; a bounded worker loop (`close --claim-next`); and worker-filed work with server-enforced constraints and playbook-owned routing policy.

**Architecture:** A new `ClaimQueryMixin` owns every write that records who holds what (claim transaction, activation, release, terminate) on one connection opened with `immediate()`. A new `PoolsMixin` on the orchestrator adds `_reconcile_pools` (desired-state sizing via a pure `size_pools` function, then start/drain convergence) beside the existing session reconciler, whose lifecycle checks gain `pool` carve-outs. The push scheduler stays for `lifecycle: task` profiles and is taught to ignore pool-profile tasks. `task.ready` becomes a first-class frontier-entry event so long-polling claims never spin. Worker-filed tasks get a routing gate in the creation transaction; the default pipeline resolves it.

**Tech Stack:** Python 3.12, SQLAlchemy Core 2.x async (`sqlite+aiosqlite` / `postgresql+asyncpg`), Alembic, pytest-asyncio (auto), Click CLI, FastAPI, ruff (line-length 100).

**Spec:** `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` — Part II (§9–§12), §14 surface, §15 performance, §16 tests, §17 rollout. Plan 1 (`docs/superpowers/plans/2026-08-28-swarm-hierarchy.md`, branch `swarm/hierarchy` at tag `plan1-complete`) is the base; this plan continues on that branch.

## Global Constraints

- The claim records the holder in ONE transaction: session row taken with a conditional write first (row lock on Postgres, writer lock on SQLite via `immediate()`), then the task (`FOR UPDATE SKIP LOCKED` on Postgres; select + CAS on SQLite, ≤ 1 reselect), then session/agent/workspace/metadata (spec §10).
- Per-claim fence: `tasks.claim_epoch` increments on every claim; every mutation on a held task carries the epoch and writes `WHERE assigned_agent_id = :agent AND claim_epoch = :epoch`; mismatch → `stale_claim` (spec §10).
- The epoch travels in `<work_dir>/.aq/claim.json`, written by the daemon before `claimed` is returned; never in provider meta, never in the environment (spec §10).
- `max_claims_per_session`: `NULL` = unlimited, `0` = parse error, everywhere (spec D5, §9).
- Pool sizing: `want = busy + ready`, `desired = clamp(want, min_active, max_active)` floored at `busy + starting`; project and global caps bind; scale-up ≤ `max_starts_per_tick`, scale-down ≤ `max_drains_per_tick` after `scale_down_grace`, never mid-task (spec §11).
- `release_claim` retains the session's workspace lock (`locked_by_agent_id`); only `terminate_pool_session` clears it, retires the agent (`RETIRED`) and revokes the token (spec §11.2).
- Worker-filed tasks: project pinned to the token; `discovered-from` (or `parent-child`) to the held task; idle sessions cannot file; `filed_count` reserved atomically; start `DEFINED`; root-level ones get a `routing` gate in the creation transaction (spec §12).
- Every cascade step is O(statements), not O(tasks); hot paths one transaction, index-backed; Postgres semantics first, SQLite parity with its own exact statement count (spec §15).
- No raw `UPDATE tasks SET status` — `_apply_transition` only; `settle_containers` returns `TransitionResult`; all post-commit emission after the transaction (Plan 1 contract).
- Every new bus event type is registered in `src/event_schemas.py` (the emit validator raises in dev).
- Migrations work on SQLite (batch) and PostgreSQL; never edit `tables.py` without a revision; fresh test DBs are built by `alembic upgrade head`.
- Full suite command for every task: `timeout 580 pytest tests/ --ignore=tests/chat_eval -n auto -q -p no:cacheprovider 2>&1 | tail -5` in the FOREGROUND with the Bash tool `timeout` parameter 600000. `tests/chat_eval` needs live LLM access. Known-flaky under load: `tests/test_tmux_integration.py::TestNudge::test_nudge_does_not_ratchet_activity` — re-run alone if it fails.
- `ruff check src tests` on touched files; `ruff format` new files; commit trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Controller rulings folded into this plan (each departs from the spec text; the spec's intent wins)

1. **Profile comes from the session row, not the token.** `RequestScope` has no `profile_id` and `api_session_tokens` has no column; adding one means three envelope sites. `task_claim` reads `sessions.profile_id` for `scope["session_id"]` — same identity, no schema change.
2. **Pipeline `when` gains `equals` / `is_null` comparators.** The runner's `_eval_pipeline_when` only knows `field+truthy`, `field+not_null`, `all`, `any`; the spec's bare-mapping `when` would evaluate to `True`. The shipped rule uses the new comparators.
3. **`create_gate` and `log_event` gain `conn=None`** (same pattern as `recompute_blocked`) so the routing gate and the `task.ready` audit row are written in the creation/transition transaction.
4. **Pool-profile tasks are invisible to the push scheduler.** `_schedule` drops tasks whose effective profile has `lifecycle == "pool"` before building `SchedulerState`, and `AgentReconciler` creates no agent rows for pool profiles. Otherwise a pool task would be push-launched into a `s-` session.
5. **Two new indexes via revision C** (`idx_events_type_project_id (event_type, project_id, id)` for the long-poll fallback; `idx_sessions_pool (lifecycle, project_id, profile_id, state)` for the supply aggregate). Additive DDL.
6. **CLI timeouts:** `_COMMAND_TIMEOUTS["task_claim"] = 180.0` and `["task_close"] = 180.0` (close may chain a claim with `--wait`).
7. **Hand-crafted CLI for the fenced commands.** `aq task claim`, `aq task close`, `aq task heartbeat`, `aq task set`, `aq handoff` read `.aq/claim.json` (fallback `$AQ_CLAIM_EPOCH`) and send `claim_epoch`; they are registered in `src/cli/agent_surface.py` before the auto-generated commands so their names win.
8. **`AgentState.RETIRED`** exists since Plan 1; retired rows are deleted at startup by the existing over-cap reaper, extended to include `RETIRED`.
9. **Postgres perf tests are opt-in** via `POSTGRES_TEST_DSN` (the existing `tests/test_database_postgresql.py` convention); the CI workflow gains a Postgres service so they run there.

---

## File structure

| File | Responsibility |
|---|---|
| `src/models.py` | `SessionRecord` + `AgentProfile` pool fields; `ClaimResult`/`ClaimOutcome` enums |
| `src/database/queries/session_queries.py`, `profile_queries.py`, `src/profiles/{parser,sync}.py` | map/validate the Part II columns |
| `src/config.py`, `src/config_editor.py` | `SwarmConfig` |
| `migrations/versions/c3d4e5f6a7b8_swarm_indexes.py` | revision C — two indexes |
| `src/database/queries/event_queries.py` | `log_event(conn=)`; `src/event_bus.py` `wait_for` |
| `src/database/queries/task_queries.py`, `blocked_state.py`, `hierarchy_queries.py` | `_note_frontier_entry`; `task.ready` bookkeeping in `_apply_transition` / `recompute_blocked` / label removal |
| `src/database/queries/claim_queries.py` (**new**) | `ClaimQueryMixin`: `claim_task`, `activate_claim`, `release_claim`, `terminate_pool_session`, `reserve_filing`, helpers |
| `src/database/queries/gate_queries.py` | `create_gate(conn=)` |
| `src/commands/claim_commands.py` (**new**) | `ClaimCommandsMixin`: `_cmd_task_claim`, long-poll, claim file, `_assert_session_owns` |
| `src/commands/session_commands.py`, `surface_commands.py` | fence in close/heartbeat/set/handoff; `--claim-next`; `_cmd_prime` session→task |
| `src/commands/task_commands.py` | worker-filed constraints, quota, routing gate, `task.created` extras |
| `src/orchestrator/core.py`, `src/orchestrator/pools.py` (**new**), `src/scheduler.py` | `_reconcile_pools`, `_launch_pool_session`, `size_pools`; push-scheduler exclusion |
| `src/orchestrator/agent_reconciler.py` | ignore pool profiles; reap `RETIRED` |
| `src/sessions/spec.py`, `env.py`, `reconciler.py` | `build_pool_spec`, `POOL_BOOTSTRAP_PROMPT`, git author env, `p-` adoption, pool carve-outs, `_step_prepare_timeout` |
| `src/playbooks/pipeline_compiler.py`, `src/orchestrator/core.py` (`_eval_pipeline_when`), `src/prompts/default_playbooks/default-pipeline.md` | `equals`/`is_null`; `worker-filed-triage` rule |
| `src/api/scope.py`, `src/tools/definitions.py`, `src/api/models/task.py`, `src/cli/agent_surface.py`, `src/cli/client.py`, `src/cli/formatter_registry.py`, `src/commands/surface_commands.py` (`get_schema`), `src/commands/ops_commands.py` (`pool_status`, `pool_scale`) | surface |
| `src/doctor/pool_checks.py` (**new**) | `pools.*`, `claims.holder_consistency` |
| `src/skills/aq-tasks/SKILL.md`, `src/prime/templates/*.md`, docs | protocol text |
| `tests/test_claim_queries.py`, `tests/test_claim_commands.py`, `tests/test_pool_sizing.py`, `tests/test_pool_reconciler.py`, `tests/test_worker_filing.py`, `tests/test_task_ready_event.py`, `tests/test_pool_doctor.py`, `tests/perf/test_claim_statements.py`, `tests/test_swarm_integration.py` | tests |

**Shared fixtures** (copy where needed; same shape as Plan 1):

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    return cfg


@pytest.fixture
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    return CommandHandler(orchestrator, config)


async def mktask(db, tid, status=TaskStatus.READY, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )
    return tid
```

---

### Task 1: Models, profile config, `SwarmConfig`, revision C

**Files:**
- Modify: `src/models.py:200-212` (`AgentState` — confirm `RETIRED` present), `:407-422` (`Agent` — add `created_at`), `:702-772` (`AgentProfile`), `:1205-1245` (`SessionRecord`)
- Modify: `src/database/queries/session_queries.py:106-127` (`_row_to_session`), `:133-176` (`create_session`), `:209-243` (`list_sessions` — add `agent_id`, `claim_phase` filters)
- Modify: `src/database/queries/profile_queries.py:18-45` (`create_profile`), `:80-119` (`upsert_profile`), `:134-171` (`_row_to_profile`)
- Modify: `src/database/queries/agent_queries.py:96` (`_row_to_agent` — map `created_at`)
- Modify: `src/profiles/parser.py:47-67` (`CONFIG_KNOWN_KEYS`), `:72` (`VALID_LIFECYCLES`), `:570-666` (`_validate_session_config`), `:972-980` (`parsed_profile_to_agent_profile`)
- Modify: `src/profiles/sync.py:284-306`
- Modify: `src/config.py` (new `SwarmConfig`; wire at the seven `WorkGraphConfig` touch points: dataclass, `validate`, `AppConfig` field `:1343`, `AppConfig.validate` `:1521`, hot-reload merge `:1623`, `HOT_RELOADABLE_SECTIONS` `:1650` + `_SECTION_FIELDS` `:1715`, `load_config` `:2377`)
- Create: `migrations/versions/c3d4e5f6a7b8_swarm_indexes.py`
- Modify: `src/database/tables.py` (the two indexes)
- Test: `tests/test_swarm_models.py`

**Interfaces:**
- Produces: `SessionRecord.{claims: int = 0, agent_id: str | None = None, claim_phase: str | None = None, claim_phase_at: float | None = None, last_claim_epoch: int | None = None, last_claim_result: str | None = None}`; `AgentProfile.{min_active: int | None = None, max_active: int | None = None, max_claims_per_session: int | None = None}`; `Agent.created_at: float = 0.0`; `VALID_LIFECYCLES = {"task", "named", "pool"}`; `SwarmConfig(enabled=False, claim_wait_max=60, max_starts_per_tick=2, max_drains_per_tick=5, scale_down_grace=120, prepare_timeout=120, max_filings_per_task=20)`; `list_sessions(..., agent_id=None, claim_phase=None)`; revision `c3d4e5f6a7b8`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_models.py
"""Part II models, profile config and SwarmConfig — spec §9."""

from __future__ import annotations

import time

import pytest

from src.config import AppConfig, ConfigValidationError, SwarmConfig
from src.database import Database
from src.models import AgentProfile, AgentState, Project, SessionRecord
from src.profiles.parser import VALID_LIFECYCLES, parse_profile_markdown

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


def _session(**over):
    now = time.time()
    base = dict(
        id="s1", project_id=PROJECT_ID, profile_id="worker-standard", harness="claude",
        provider="fake", name="p-worker-standard--proj--1", lifecycle="pool",
        work_dir="/wd", epoch="e", instance_token="t", started_at=now, state="running",
    )
    base.update(over)
    return SessionRecord(**base)


class TestSessionRecord:
    async def test_pool_fields_round_trip(self, db):
        await db.create_session(_session(agent_id="agent-1", claims=2, claim_phase="active",
                                         claim_phase_at=1.0, last_claim_epoch=3,
                                         last_claim_result="claimed"))
        row = await db.get_session("s1")
        assert (row.agent_id, row.claims, row.claim_phase, row.claim_phase_at,
                row.last_claim_epoch, row.last_claim_result) == (
            "agent-1", 2, "active", 1.0, 3, "claimed")

    async def test_list_sessions_filters_by_agent_and_phase(self, db):
        await db.create_session(_session(id="s1", name="p-a", agent_id="agent-1", claim_phase="active"))
        await db.create_session(_session(id="s2", name="p-b", agent_id="agent-2"))
        assert [s.id for s in await db.list_sessions(agent_id="agent-1")] == ["s1"]
        assert [s.id for s in await db.list_sessions(claim_phase="active")] == ["s1"]


class TestAgentProfile:
    async def test_pool_columns_round_trip(self, db):
        await db.create_profile(AgentProfile(id="w", name="w", lifecycle="pool", min_active=1,
                                             max_active=3, max_claims_per_session=None))
        p = await db.get_profile("w")
        assert (p.lifecycle, p.min_active, p.max_active, p.max_claims_per_session) == ("pool", 1, 3, None)

    def test_agent_state_retired(self):
        assert AgentState.RETIRED.value == "RETIRED"


POOL_PROFILE = """---
id: worker-fast
name: Worker fast
---
## Config
```json
{"harness": "claude", "lifecycle": "pool", "min_active": 0, "max_active": 3,
 "max_claims_per_session": 2, "needs_workspace": true}
```
## Role
Fast worker.
"""


class TestProfileParser:
    def test_pool_lifecycle_accepted(self):
        assert "pool" in VALID_LIFECYCLES
        parsed = parse_profile_markdown(POOL_PROFILE)
        assert parsed.config["lifecycle"] == "pool"
        assert parsed.config["max_claims_per_session"] == 2

    @pytest.mark.parametrize("bad", ['"max_claims_per_session": 0', '"max_active": -1',
                                     '"min_active": true'])
    def test_rejects_zero_negative_and_bool(self, bad):
        text = POOL_PROFILE.replace('"max_claims_per_session": 2', bad)
        with pytest.raises(Exception):
            parse_profile_markdown(text)

    def test_pool_keys_rejected_on_task_lifecycle(self):
        text = POOL_PROFILE.replace('"lifecycle": "pool"', '"lifecycle": "task"')
        with pytest.raises(Exception):
            parse_profile_markdown(text)


class TestSwarmConfig:
    def test_defaults(self):
        cfg = SwarmConfig()
        assert (cfg.enabled, cfg.claim_wait_max, cfg.max_starts_per_tick, cfg.max_drains_per_tick,
                cfg.scale_down_grace, cfg.prepare_timeout, cfg.max_filings_per_task) == (
            False, 60, 2, 5, 120, 120, 20)

    def test_validate_rejects_negative(self):
        cfg = SwarmConfig(prepare_timeout=-1)
        assert cfg.validate()

    def test_app_config_has_swarm(self):
        assert isinstance(AppConfig().swarm, SwarmConfig)
```

Check the real name of the markdown parse entry point in `src/profiles/parser.py` (grep `^def parse`) and the exception class it raises on config errors (`ProfileParseError` or similar) — use the real names in the tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_swarm_models.py -v`
Expected: FAIL — `SessionRecord.__init__() got an unexpected keyword argument 'agent_id'`, `ImportError: cannot import name 'SwarmConfig'`, etc.

- [ ] **Step 3: Models**

`src/models.py` — `SessionRecord` (after `sleep_reason`):

```python
    # Pool lifecycle (swarm-work-model §9–§11).
    claims: int = 0
    agent_id: str | None = None
    claim_phase: str | None = None  # claiming | preparing | active
    claim_phase_at: float | None = None
    last_claim_epoch: int | None = None
    last_claim_result: str | None = None  # claimed | prepare_failed | released
```

`AgentProfile` (after `max_session_age`):

```python
    # lifecycle: pool (swarm-work-model §9).  NULL = unlimited claims.
    min_active: int | None = None
    max_active: int | None = None
    max_claims_per_session: int | None = None
```

`Agent`: add `created_at: float = 0.0` at the end; `agent_queries._row_to_agent` maps `created_at=row.get("created_at", 0.0) or 0.0`.

Add near `AgentState`:

```python
class ClaimResult(Enum):
    """Result codes of ``task_claim`` (swarm-work-model §10)."""

    CLAIMED = "claimed"
    NO_READY_WORK = "no_ready_work"
    CLAIM_CONFLICT = "claim_conflict"
    PREPARE_FAILED = "prepare_failed"
    CLAIM_IN_PROGRESS = "claim_in_progress"
    NOT_ADMISSIBLE = "not_admissible"
    SESSION_EXHAUSTED = "session_exhausted"
    DRAIN_REQUESTED = "drain_requested"
    STALE_CLAIM = "stale_claim"
    OUT_OF_SCOPE = "out_of_scope"


CLAIM_PHASES = ("claiming", "preparing", "active")
```

- [ ] **Step 4: Query mappings**

`session_queries._row_to_session`: add the six fields (`row.get(...)`, `claims=int(row.get("claims") or 0)`). `create_session`: add the six columns to the `insert(...).values(...)`. `list_sessions`: add kwargs `agent_id: str | None = None, claim_phase: str | None = None` with equality filters.

`profile_queries`: `create_profile` values + `upsert_profile`'s `update_profile(...)` kwarg list + `_row_to_profile` gain `min_active`, `max_active`, `max_claims_per_session` (`row.get(...)`).

- [ ] **Step 5: Profile parser and sync**

`parser.py`: add `"min_active", "max_active", "max_claims_per_session"` to `CONFIG_KNOWN_KEYS`; `VALID_LIFECYCLES = frozenset({"task", "named", "pool"})`. In `_validate_session_config`, after the named-only integer loop, add:

```python
    # Pool-only sizing keys (swarm-work-model §9).  NULL/absent = unlimited
    # for max_claims_per_session; 0 is a parse error everywhere.
    for key in ("min_active", "max_active", "max_claims_per_session"):
        if key not in config:
            continue
        value = config[key]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"Config '{key}' must be an integer, got {type(value).__name__}")
            continue
        if key == "min_active":
            if value < 0:
                errors.append(f"Config 'min_active' must be >= 0, got {value}")
        elif value <= 0:
            errors.append(f"Config '{key}' must be positive (omit it for unlimited), got {value}")
        if lifecycle != "pool":
            errors.append(
                f"Config '{key}' is only valid with lifecycle 'pool' "
                f"(this profile's lifecycle is '{lifecycle}')"
            )
```

In `parsed_profile_to_agent_profile`, after the `idle_timeout`/`max_session_age` loop:

```python
    for key in ("min_active", "max_active", "max_claims_per_session"):
        value = parsed.config.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
```

`sync.py:284-306`: pass `min_active=profile_dict.get("min_active")`, `max_active=profile_dict.get("max_active")`, `max_claims_per_session=profile_dict.get("max_claims_per_session")` to `AgentProfile(...)`.

- [ ] **Step 6: `SwarmConfig`**

`src/config.py`, next to `WorkGraphConfig`:

```python
@dataclass
class SwarmConfig:
    """Pull-based worker pools (swarm-work-model §10–§12, §17).

    ``enabled`` gates ``_reconcile_pools`` and ``lifecycle: pool`` launches.
    Everything else is a tunable read each tick — hot-reloadable.
    """

    enabled: bool = False
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
```

Wire it exactly where `WorkGraphConfig` is wired: `AppConfig.swarm: SwarmConfig = field(default_factory=SwarmConfig)`; `errors.extend(self.swarm.validate())`; `updated.swarm = fresh.swarm` in the hot-reload merge; `"swarm"` in `HOT_RELOADABLE_SECTIONS` and `_SECTION_FIELDS`; in `load_config`:

```python
    if "swarm" in raw and isinstance(raw["swarm"], dict):
        sw = raw["swarm"]
        config.swarm = SwarmConfig(
            enabled=bool(sw.get("enabled", False)),
            claim_wait_max=int(sw.get("claim_wait_max", 60)),
            max_starts_per_tick=int(sw.get("max_starts_per_tick", 2)),
            max_drains_per_tick=int(sw.get("max_drains_per_tick", 5)),
            scale_down_grace=int(sw.get("scale_down_grace", 120)),
            prepare_timeout=int(sw.get("prepare_timeout", 120)),
            max_filings_per_task=int(sw.get("max_filings_per_task", 20)),
        )
```

`config_editor.py` derives its schema automatically (verified in Plan 1) — confirm `aq config schema` (or the `get_config_schema` command) lists `swarm` after the change; add an `x-reload: hot` annotation if the section map there is explicit.

- [ ] **Step 7: Revision C**

`tables.py`: in `events` add `Index("idx_events_type_project_id", "event_type", "project_id", "id")`; in `sessions` add `Index("idx_sessions_pool", "lifecycle", "project_id", "profile_id", "state")`.

```python
# migrations/versions/c3d4e5f6a7b8_swarm_indexes.py
"""swarm work model — long-poll and pool-supply indexes (revision C)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("idx_events_type_project_id", "events", ["event_type", "project_id", "id"])
    op.create_index("idx_sessions_pool", "sessions", ["lifecycle", "project_id", "profile_id", "state"])


def downgrade() -> None:
    op.drop_index("idx_sessions_pool", table_name="sessions")
    op.drop_index("idx_events_type_project_id", table_name="events")
```

Confirm `alembic heads` prints `b2c3d4e5f6a7` before writing; `alembic revision --autogenerate -m check` after must show no diff (delete the check revision).

- [ ] **Step 8: Run tests**

Run: `pytest tests/test_swarm_models.py tests/test_profile_parser.py tests/test_profile_sync.py tests/test_config.py tests/test_session_queries.py tests/test_hierarchy_migration.py -v -n auto`, then the full suite.
Expected: PASS. Fix any exhaustive-assertion tests (config section lists, `VALID_LIFECYCLES`, migration head assertions) by extending them.

- [ ] **Step 9: Commit**

```bash
git add src/models.py src/database/queries/session_queries.py src/database/queries/profile_queries.py src/database/queries/agent_queries.py src/profiles/parser.py src/profiles/sync.py src/config.py src/config_editor.py src/database/tables.py migrations/versions/c3d4e5f6a7b8_swarm_indexes.py tests/test_swarm_models.py
git commit -m "feat(swarm): pool profile config, session/profile model fields, SwarmConfig, revision C

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `task.ready` frontier-entry event, `log_event(conn=)`, `EventBus.wait_for`

**Files:**
- Modify: `src/database/queries/event_queries.py:15-46` (`log_event`)
- Modify: `src/database/queries/task_queries.py:242-402` (`_apply_transition`), `:401-420` (`transition_task`)
- Modify: `src/database/queries/blocked_state.py:220-300` (`recompute_blocked` — return flips; frontier entries recorded by callers), `:355` (`log_blocked_flips`)
- Modify: `src/database/queries/task_queries.py:748` (`remove_task_label`)
- Modify: `src/commands/task_commands.py` (`_cmd_task_route` — after resolving a routing gate)
- Modify: `src/orchestrator/events.py:33-58` (`_emit_blocked_flips` → also emit `task.ready`)
- Modify: `src/event_schemas.py:63` (`task.ready`), `src/event_bus.py` (`wait_for`)
- Test: `tests/test_task_ready_event.py`

**Interfaces:**
- Produces:
  - `log_event(self, event_type, project_id=None, task_id=None, agent_id=None, payload=None, *, conn=None) -> int`.
  - `TransitionResult.ready: list[tuple[str, str]]` (new field) — `(task_id, reason)` for every task that entered the frontier in this transaction.
  - `async _note_frontier_entry(self, conn, task_ids: set[str], *, reason: str) -> list[str]` in `TaskQueryMixin`: for each id, if now `READY ∧ is_blocked = 0 ∧ no hold:* label` (one query over the set), write a `task.ready` audit row (`log_event(..., conn=conn, payload=reason)`) and return the ids. Callers pass only ids whose pre-state was not in the frontier.
  - `transition_task` emits `task.ready` on the bus after commit for `result.ready` (via a `ready_listener` registered by the orchestrator, same pattern as the settlement listener), with `reason`.
  - `recompute_blocked` unchanged in signature; `log_blocked_flips(flipped)` additionally computes frontier entries for the flipped-to-unblocked set (`status = READY`) and logs `task.ready` rows; the orchestrator's `_emit_blocked_flips` emits `task.ready` for those.
  - `remove_task_label(task_id, label)`: when `label` starts with `hold:`, records a frontier entry (reason `hold_removed`) and returns the list; `_cmd_task_set` emits.
  - `EventBus.wait_for(self, event_types: Iterable[str], *, filter: dict | None = None, timeout: float) -> dict | None` — one-shot subscribe/await/unsubscribe.
  - Schema: `"task.ready": {"required": ["task_id", "project_id", "title"], "optional": ["reason"]}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_task_ready_event.py
"""task.ready — every entry into the frontier is recorded and emitted (spec §9)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from src.database import Database
from src.database.tables import events
from src.event_bus import EventBus
from src.models import Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid,
                              status=status, **kw))


async def ready_rows(db, task_id):
    async with db._engine.begin() as conn:
        rows = (await conn.execute(select(events.c.payload).where(
            (events.c.event_type == "task.ready") & (events.c.task_id == task_id)))).fetchall()
    return [r[0] for r in rows]


class TestFrontierEntry:
    async def test_promotion_defined_to_ready_records_audit_row_in_transaction(self, db):
        await mktask(db, "a")
        seen = []
        async def listener(entries):  # list[tuple[task_id, reason]]
            seen.append(list(entries))

        db.set_ready_listener(listener)
        await db.transition_task("a", TaskStatus.READY, context="promotion")
        assert await ready_rows(db, "a") == ["promoted"]
        assert seen == [[("a", "promoted")]]

    async def test_ready_but_blocked_is_not_a_frontier_entry(self, db):
        await mktask(db, "dep")
        await mktask(db, "a")
        await db.add_dependency("a", "dep", "blocks")
        await db.transition_task("a", TaskStatus.READY)
        assert await ready_rows(db, "a") == []

    async def test_unblocking_a_ready_task_records_entry(self, db):
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "a", status=TaskStatus.READY)
        await db.add_dependency("a", "dep", "blocks")
        assert (await db.get_task("a")).is_blocked is True
        await db.transition_task("dep", TaskStatus.COMPLETED)
        assert await ready_rows(db, "a") == ["unblocked"]

    async def test_hold_removal_records_entry(self, db):
        await mktask(db, "a", status=TaskStatus.READY)
        await db.add_task_label("a", "hold:triage")
        entered = await db.remove_task_label("a", "hold:triage")
        assert entered == ["a"]
        assert await ready_rows(db, "a") == ["hold_removed"]

    async def test_same_status_write_does_not_double_record(self, db):
        await mktask(db, "a", status=TaskStatus.READY)
        await db.transition_task("a", TaskStatus.READY, priority=5)
        assert await ready_rows(db, "a") == []


class TestWaitFor:
    async def test_wait_for_returns_matching_event(self):
        bus = EventBus()

        async def fire():
            await asyncio.sleep(0.01)
            await bus.emit("task.ready", {"task_id": "t", "project_id": "p", "title": "t"})

        asyncio.create_task(fire())
        got = await bus.wait_for(["task.ready"], filter={"project_id": "p"}, timeout=1.0)
        assert got["task_id"] == "t"
        assert bus.subscriber_count("task.ready") == 0

    async def test_wait_for_times_out(self):
        bus = EventBus()
        assert await bus.wait_for(["task.ready"], timeout=0.05) is None
```

Check `EventBus`'s constructor and whether it has a subscriber-count accessor; if not, add `subscriber_count(event_type) -> int` for the test. Check `EventBus` payload validation runs against the registry (register `task.ready` before emitting in the test — the registry is module-global, so Step 3 covers it).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_task_ready_event.py -v`
Expected: FAIL — `set_ready_listener` / `wait_for` missing; no `task.ready` rows.

- [ ] **Step 3: Implement**

`event_schemas.py` `_TASK_SCHEMAS`: add `"task.ready": {"required": ["task_id", "project_id", "title"], "optional": ["reason"]}`.

`event_queries.log_event`: add `*, conn=None`; when `conn` is given, execute the insert on it and return the id without opening a transaction.

`task_queries.py`:

```python
@dataclass
class TransitionResult:
    flipped: set[str] = field(default_factory=set)
    settled: list[str] = field(default_factory=list)
    abandoned: list[str] = field(default_factory=list)
    ready: list[str] = field(default_factory=list)  # entered the frontier
```

```python
    async def _note_frontier_entry(self, conn, task_ids: set[str], *, reason: str) -> list[str]:
        """Record every id in *task_ids* that is now in the ready frontier (spec §9).

        Callers pass ids whose pre-state was outside the frontier; this checks
        the post-state in one statement and writes the ``task.ready`` audit row
        on the caller's connection so a crash after commit cannot lose it.
        """
        if not task_ids:
            return []
        from src.database.queries.blocked_state import apply_label_filters

        stmt = select(tasks.c.id, tasks.c.project_id, tasks.c.title).where(
            and_(tasks.c.id.in_(sorted(task_ids)), tasks.c.status == TaskStatus.READY.value,
                 tasks.c.is_blocked == 0)
        )
        stmt = apply_label_filters(stmt, exclude_hold=True)
        rows = (await conn.execute(stmt)).fetchall()
        for tid, pid, _title in rows:
            await self.log_event("task.ready", project_id=pid, task_id=tid, payload=reason, conn=conn)
        return [r[0] for r in rows]
```

`TransitionResult.ready` is a list of `(task_id, reason)` tuples — a transition can produce entries with two reasons (the task itself, and dependents it unblocked).

In `_apply_transition`, in the different-status branch: read `status, is_blocked` before the write (the method already reads `status`; extend the select to `is_blocked`); after the status write and `recompute_blocked`, when the pre-state was not `(READY, is_blocked=0)`:

```python
                reason = _READY_REASONS.get(context, "promoted")
                for tid in await self._note_frontier_entry(conn, {task_id}, reason=reason):
                    result.ready.append((tid, reason))
```

`_READY_REASONS = {"promotion": "promoted", "reopen_with_feedback": "restarted", "retry": "released", "rate_limit": "resumed", "resume_paused": "resumed", "session_not_live": "released", "slot_reset_failed": "released", "prepare_timeout": "released", "session_close": "released"}` — a module constant; every context starting with `session_` maps to `"released"`; unknown contexts map to `"promoted"`.

Also after `recompute_blocked` returns `flipped`: `for tid in await self._note_frontier_entry(conn, {t for t in flipped if t != task_id}, reason="unblocked"): result.ready.append((tid, "unblocked"))`.

`transition_task`: after commit, `await self._notify_ready(result.ready)` — mirror `_notify_settled`: `set_ready_listener(cb)`, `_ready_listener`, `async _notify_ready(entries: list[tuple[str, str]])` (no-op when the list is empty or no listener is set; exceptions logged, never raised).

`dependency_queries.add_dependency/remove_dependency` and `gate_queries.resolve_gate`: they call `recompute_blocked` then `log_blocked_flips(flipped)`; extend `log_blocked_flips` to also call `_note_frontier_entry` for flipped ids whose `is_blocked` is now 0 (it already re-reads `is_blocked` for the audit rows — add `status` to that select and record `task.ready` with reason `unblocked` for `READY ∧ 0` rows, in the same transaction it opens) and return the entries; callers pass them to `_notify_ready`. Note `log_blocked_flips` opens its own transaction post-commit — that is acceptable here (the flips themselves already committed; the audit row for them is best-effort by existing design), but for `_apply_transition`'s own path the in-transaction write above is authoritative.

`remove_task_label`: if `label.startswith(HOLD_LABEL_PREFIX)`, after the delete in the same transaction call `_note_frontier_entry(conn, {task_id}, reason="hold_removed")` and return the list; otherwise return `[]`. `_cmd_task_set`'s `labels_remove` loop: `entered = await self.db.remove_task_label(...)`; `await self.db._notify_ready([(t, "hold_removed") for t in entered])`.

`_cmd_task_route`: after it resolves the routing gate (find the `resolve_gate` call), the gate resolution's `recompute_blocked` flip path covers it (reason `unblocked`); no extra code unless the task was never `is_blocked` (a routing gate always blocks — verify by reading `_gate_open` in `blocked_state.py`; if so, no change).

Orchestrator: in `monitoring.register_settlement_listener` also `self.db.set_ready_listener(self._on_frontier_entries)`:

```python
    async def _on_frontier_entries(self, entries: list[tuple[str, str]]) -> None:
        for task_id, reason in entries:
            task = await self.db.get_task(task_id)
            if task is None:
                continue
            try:
                await self._emit_task_event("task.ready", task, reason=reason)
            except Exception:
                logger.exception("task.ready emit failed for %s", task_id)
```

`event_bus.py`:

```python
    async def wait_for(self, event_types, *, filter=None, timeout: float):
        """Await the first event of any of *event_types* matching *filter*, or None on timeout."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        async def _on(data):
            if not fut.done():
                fut.set_result(dict(data))

        unsubs = [self.subscribe(t, _on, filter=filter) for t in event_types]
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            for u in unsubs:
                u()

    def subscriber_count(self, event_type: str) -> int:
        return len(self._handlers.get(event_type, []))
```

(Check the handler-list attribute name in `event_bus.py` and whether handlers may be sync — `_on` is async; the bus supports both.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_task_ready_event.py tests/test_blocked_state.py tests/test_work_graph_cascade.py tests/test_event_schema_registry_validation.py tests/test_emit_schema_compliance.py tests/test_hierarchy_settlement.py -v -n auto`, then the full suite. The invariant test that every `task.*` emitter goes through `_emit_task_event` with the base triple must pass (it does — `_on_frontier_entries` uses it).

- [ ] **Step 5: Commit**

```bash
git add src/event_schemas.py src/event_bus.py src/database/queries/event_queries.py src/database/queries/task_queries.py src/database/queries/blocked_state.py src/database/queries/dependency_queries.py src/database/queries/gate_queries.py src/commands/surface_commands.py src/orchestrator/monitoring.py src/orchestrator/events.py tests/test_task_ready_event.py
git commit -m "feat(events): task.ready on every frontier entry, in-transaction audit row, EventBus.wait_for

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `ClaimQueryMixin` — the claim transaction, activation, release, termination

**Files:**
- Create: `src/database/queries/claim_queries.py`
- Modify: `src/database/queries/__init__.py`, `src/database/adapters/sqlite.py:59-85`, `src/database/adapters/postgresql.py` (register `ClaimQueryMixin` right after `HierarchyQueryMixin`)
- Modify: `src/database/queries/task_queries.py` (`_apply_transition` gains `expect_claim_epoch: int | None = None`; `transition_task` threads it; new `StaleClaim` exception)
- Modify: `src/database/queries/workspace_queries.py:517` (`release_workspaces_for_agent(agent_id, *, conn=None)`; new `get_workspace_for_agent`)
- Modify: `src/database/queries/event_queries.py` (`max_event_id`, `count_events_after`)
- Modify: `src/database/base.py` (protocol methods)
- Test: `tests/test_claim_queries.py`

**Interfaces:**
- Consumes: `immediate()` (Plan 1), `_apply_transition`, `_upsert_meta` (HierarchyQueryMixin), `_row_to_session`, `_row_to_task`, `log_blocked_flips`, `_notify_settled`, `_notify_ready` (Task 2).
- Produces (all reachable on the composed adapter):
  - `class StaleClaim(Exception)` in `task_queries.py` — raised by `_apply_transition` when `expect_claim_epoch` is given and the UPDATE matched no row. `transition_task(..., expect_claim_epoch=None)` passes it through.
  - `async take_claim_slot(self, conn, session_id, *, now: float, cap: int | None) -> tuple[str, SessionRecord | None]` → `("slot", row)` when the conditional UPDATE hit; otherwise one of `"active" | "preparing" | "claiming" | "drain_requested" | "session_exhausted" | "not_found"` with the fresh row.
  - `async release_claim_slot(self, conn, session_id) -> None` — `claim_phase = NULL, claim_phase_at = NULL` where `claim_phase = 'claiming'` (used when no task was taken).
  - `async select_ready_for_profile(self, conn, *, project_id, profile_id, default_profile_id, agent_id, task_id=None) -> str | None` — the §10 work query; Postgres adds `FOR UPDATE SKIP LOCKED`; returns the id.
  - `async take_task(self, conn, task_id, *, agent_id, now) -> Task | None` — epoch-bump CAS then `_apply_transition(conn, task_id, IN_PROGRESS, context="claim", assigned_agent_id=agent_id)`; `None` when the CAS missed.
  - `async record_holder(self, conn, *, session_id, task_id, agent_id, work_dir, now) -> None`.
  - `async activate_claim(self, session_id, task_id, *, epoch: int, now: float) -> bool` — own `immediate()` transaction; `rowcount == 1`.
  - `async release_claim(self, session_id, *, task_status: TaskStatus, context: str, now: float, result: str = "released", needs_attention: str | None = None, conn=None) -> TransitionResult`.
  - `async terminate_pool_session(self, session_id, *, reason: str, task_status: TaskStatus = TaskStatus.READY, conn=None) -> TransitionResult`.
  - `async bump_claim_epoch(self, task_id) -> int` — one statement; used by push launches (Task 4).
  - `async reserve_filing(self, conn, task_id, *, max_filings: int) -> bool`.
  - `async count_ready_by_profile(self, project_id) -> dict[str | None, int]` — `GROUP BY profile_id` over the frontier (READY ∧ unblocked ∧ unassigned ∧ not plan-subtask ∧ no hold label); used by pool sizing (Task 5).
  - `async get_workspace_for_agent(self, agent_id) -> Workspace | None`; `release_workspaces_for_agent(agent_id, *, conn=None)`.
  - `async max_event_id(self) -> int`; `async count_events_after(self, seq: int, *, event_type: str, project_id: str) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claim_queries.py
"""ClaimQueryMixin — spec §10 claim transaction, §11.2 ownership lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from src.database import Database
from src.database.queries.task_queries import StaleClaim
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus, Workspace

PROJECT_ID = "proj"
NOW = 1_000_000.0


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.READY, **kw):
    await db.create_task(Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid,
                              status=status, **kw))


async def pool_session(db, sid="s1", agent_id="agent-1", **over):
    await db.create_agent(Agent(id=agent_id, name=agent_id, profile_id="worker",
                                state=AgentState.IDLE))
    await db.create_workspace(Workspace(id=f"ws-{agent_id}", project_id=PROJECT_ID,
                                        workspace_path=f"/wd/{agent_id}", kind_id="project-repo",
                                        locked_by_agent_id=agent_id))
    base = dict(id=sid, project_id=PROJECT_ID, profile_id="worker", harness="claude",
                provider="fake", name=f"p-worker--proj--{sid}", lifecycle="pool",
                work_dir=f"/wd/{agent_id}", epoch="e", instance_token="t",
                started_at=NOW, state="running", agent_id=agent_id)
    base.update(over)
    await db.create_session(SessionRecord(**base))
    return sid


async def claim_once(db, sid, *, cap=None, task_id=None):
    async with db.immediate() as conn:
        kind, row = await db.take_claim_slot(conn, sid, now=NOW, cap=cap)
        if kind != "slot":
            return kind, None
        tid = await db.select_ready_for_profile(
            conn, project_id=PROJECT_ID, profile_id="worker", default_profile_id=None,
            agent_id=row.agent_id, task_id=task_id)
        if tid is None:
            await db.release_claim_slot(conn, sid)
            return "no_ready_work", None
        task = await db.take_task(conn, tid, agent_id=row.agent_id, now=NOW)
        if task is None:
            await db.release_claim_slot(conn, sid)
            return "claim_conflict", None
        await db.record_holder(conn, session_id=sid, task_id=tid, agent_id=row.agent_id,
                               work_dir=row.work_dir, now=NOW)
        return "claimed", task


class TestClaimTransaction:
    async def test_claim_records_holder_everywhere(self, db):
        await mktask(db, "t1", profile_id="worker")
        sid = await pool_session(db)
        kind, task = await claim_once(db, sid)
        assert kind == "claimed" and task.id == "t1"
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id, t.claim_epoch) == (
            TaskStatus.IN_PROGRESS, "agent-1", 1)
        s = await db.get_session(sid)
        assert (s.task_id, s.claim_phase, s.claims) == ("t1", "preparing", 0)
        a = await db.get_agent("agent-1")
        assert (a.state, a.current_task_id) == (AgentState.BUSY, "t1")
        assert (await db.get_workspace_for_agent("agent-1")).locked_by_task_id == "t1"
        assert await db.get_task_meta("t1", "claimed_by_session") == sid

    async def test_second_slot_take_classifies_phase(self, db):
        await mktask(db, "t1", profile_id="worker")
        sid = await pool_session(db)
        await claim_once(db, sid)
        async with db.immediate() as conn:
            kind, _ = await db.take_claim_slot(conn, sid, now=NOW, cap=None)
        assert kind == "preparing"
        assert await db.activate_claim(sid, "t1", epoch=1, now=NOW) is True
        async with db.immediate() as conn:
            kind, _ = await db.take_claim_slot(conn, sid, now=NOW, cap=None)
        assert kind == "active"
        assert (await db.get_session(sid)).claims == 1

    async def test_cap_and_drain_classification(self, db):
        sid = await pool_session(db, claims=1)
        async with db.immediate() as conn:
            kind, _ = await db.take_claim_slot(conn, sid, now=NOW, cap=1)
        assert kind == "session_exhausted"
        await db.update_session(sid, desired_state="stopped")
        async with db.immediate() as conn:
            kind, _ = await db.take_claim_slot(conn, sid, now=NOW, cap=None)
        assert kind == "drain_requested"

    async def test_work_query_excludes_other_profiles_holds_and_plan_subtasks(self, db):
        await mktask(db, "other", profile_id="reviewer")
        await mktask(db, "held", profile_id="worker")
        await db.add_task_label("held", "hold:triage")
        await mktask(db, "plan", profile_id="worker", is_plan_subtask=True)
        await mktask(db, "mine", profile_id="worker", priority=50)
        sid = await pool_session(db)
        kind, task = await claim_once(db, sid)
        assert kind == "claimed" and task.id == "mine"
        sid2 = await pool_session(db, sid="s2", agent_id="agent-2")
        assert (await claim_once(db, sid2))[0] == "no_ready_work"

    async def test_default_profile_takes_unrouted_tasks(self, db):
        await mktask(db, "unrouted")  # profile_id None
        sid = await pool_session(db)
        async with db.immediate() as conn:
            kind, row = await db.take_claim_slot(conn, sid, now=NOW, cap=None)
            assert await db.select_ready_for_profile(
                conn, project_id=PROJECT_ID, profile_id="worker", default_profile_id="worker",
                agent_id=row.agent_id) == "unrouted"
            assert await db.select_ready_for_profile(
                conn, project_id=PROJECT_ID, profile_id="worker", default_profile_id="other",
                agent_id=row.agent_id) is None

    async def test_exactly_once_under_concurrency(self, db):
        for i in range(10):
            await mktask(db, f"t{i}", profile_id="worker")
        sids = [await pool_session(db, sid=f"s{i}", agent_id=f"agent-{i}") for i in range(20)]
        results = await asyncio.gather(*(claim_once(db, s) for s in sids))
        kinds = [k for k, _ in results]
        assert kinds.count("claimed") == 10 and kinds.count("no_ready_work") == 10
        assert sorted(t.id for k, t in results if k == "claimed") == sorted(
            f"t{i}" for i in range(10))

    async def test_activate_loses_to_release(self, db):
        await mktask(db, "t1", profile_id="worker")
        sid = await pool_session(db)
        await claim_once(db, sid)
        await db.release_claim(sid, task_status=TaskStatus.READY, context="slot_reset_failed",
                               now=NOW, result="prepare_failed", needs_attention="slot_reset_failed")
        assert await db.activate_claim(sid, "t1", epoch=1, now=NOW) is False
        s = await db.get_session(sid)
        assert (s.task_id, s.claim_phase, s.last_claim_epoch, s.last_claim_result) == (
            None, None, 1, "prepare_failed")
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id, t.claim_epoch) == (TaskStatus.READY, None, 1)
        assert await db.get_task_meta("t1", "needs_attention") == "slot_reset_failed"
        ws = await db.get_workspace_for_agent("agent-1")
        assert (ws.locked_by_task_id, ws.locked_by_agent_id) == (None, "agent-1")
        assert (await db.get_agent("agent-1")).state == AgentState.IDLE

    async def test_epoch_fence_rejects_stale_writer(self, db):
        await mktask(db, "t1", profile_id="worker")
        sid = await pool_session(db)
        await claim_once(db, sid)  # epoch 1
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=NOW)
        await claim_once(db, sid)  # epoch 2
        async with db.immediate() as conn:
            with pytest.raises(StaleClaim):
                await db._apply_transition(conn, "t1", TaskStatus.COMPLETED, context="close",
                                           force=True, expect_claim_epoch=1)
        assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS

    async def test_terminate_releases_everything(self, db):
        await mktask(db, "t1", profile_id="worker")
        sid = await pool_session(db)
        await claim_once(db, sid)
        await db.terminate_pool_session(sid, reason="stopped")
        assert await db.get_workspace_for_agent("agent-1") is None
        assert (await db.get_agent("agent-1")).state == AgentState.RETIRED
        s = await db.get_session(sid)
        assert (s.task_id, s.claim_phase) == (None, None)
        assert (await db.get_task("t1")).status == TaskStatus.READY

    async def test_reserve_filing_is_atomic(self, db):
        await mktask(db, "t1", status=TaskStatus.IN_PROGRESS)

        async def one():
            async with db.immediate() as conn:
                return await db.reserve_filing(conn, "t1", max_filings=20)

        got = await asyncio.gather(*(one() for _ in range(25)))
        assert got.count(True) == 20 and got.count(False) == 5
        assert (await db.get_task("t1")).filed_count == 20

    async def test_count_ready_by_profile(self, db):
        await mktask(db, "a", profile_id="worker")
        await mktask(db, "b", profile_id="worker")
        await mktask(db, "c")
        await mktask(db, "d", status=TaskStatus.DEFINED, profile_id="worker")
        assert await db.count_ready_by_profile(PROJECT_ID) == {"worker": 2, None: 1}
```

Check `Workspace`'s constructor and `create_workspace` in `src/models.py` / `workspace_queries.py`; adjust the helper's field names (e.g. `path` vs `workspace_path`). Check `get_task_meta` exists (Plan 1 added `_upsert_meta`; a reader may be named `get_task_metadata(task_id) -> dict` — use whatever exists).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claim_queries.py -v`
Expected: FAIL — `AttributeError: ... 'take_claim_slot'`.

- [ ] **Step 3: `StaleClaim` and the fenced UPDATE**

`task_queries.py`:

```python
class StaleClaim(Exception):
    """A fenced write found the task held under a different claim epoch (spec §10)."""
```

In `_apply_transition`, both the same-status and different-status UPDATE statements:

```python
                stmt = update(tasks).where(tasks.c.id == task_id)
                if expect_claim_epoch is not None:
                    stmt = stmt.where(
                        and_(
                            tasks.c.claim_epoch == expect_claim_epoch,
                            tasks.c.assigned_agent_id.isnot(None),
                        )
                    )
                res = await conn.execute(stmt.values(**values))
                if expect_claim_epoch is not None and res.rowcount == 0:
                    raise StaleClaim(
                        f"{task_id}: claim epoch {expect_claim_epoch} is not current"
                    )
```

`transition_task(...)` gains `expect_claim_epoch: int | None = None` and passes it through. Note `assigned_agent_id=None` is one of the `values` a fenced close writes — the WHERE evaluates against the pre-image, so the fence still holds.

- [ ] **Step 4: `claim_queries.py`**

```python
"""Claims — who holds what (swarm-work-model §10, §11.2).

Every write that records a holder goes through this mixin on a caller-owned
connection opened by ``immediate()``: the session slot is taken with a
conditional UPDATE first (row lock on Postgres, writer lock on SQLite), then
the task, then agent/workspace/metadata.  ``activate_claim`` and
``release_claim`` are the two ways a claim leaves ``preparing`` and they
race by design — the conditional UPDATEs make exactly one of them win.
"""

from __future__ import annotations

import time

from sqlalchemy import and_, case, exists, func, literal, select, update

from src.database.queries.blocked_state import apply_label_filters
from src.database.queries.task_queries import TransitionResult
from src.database.tables import agents, sessions, task_workspace_requirements, tasks, workspaces
from src.models import AgentState, SessionRecord, Task, TaskStatus


def _frontier_where(project_id: str):
    return and_(
        tasks.c.project_id == project_id,
        tasks.c.status == TaskStatus.READY.value,
        tasks.c.is_blocked == 0,
        tasks.c.assigned_agent_id.is_(None),
        tasks.c.is_plan_subtask == 0,
    )


class ClaimQueryMixin:
    """Expects ``self._engine`` plus Task/Session/Workspace/Hierarchy mixins."""

    async def take_claim_slot(self, conn, session_id: str, *, now: float, cap: int | None):
        cond = [
            sessions.c.id == session_id,
            sessions.c.task_id.is_(None),
            sessions.c.claim_phase.is_(None),
            sessions.c.desired_state == "running",
        ]
        if cap is not None:
            cond.append(sessions.c.claims < cap)
        res = await conn.execute(
            update(sessions).where(and_(*cond)).values(claim_phase="claiming", claim_phase_at=now)
        )
        row = (
            await conn.execute(select(sessions).where(sessions.c.id == session_id))
        ).mappings().fetchone()
        if row is None:
            return "not_found", None
        record = self._row_to_session(row)
        if res.rowcount == 1:
            return "slot", record
        if record.claim_phase in ("active", "preparing", "claiming"):
            return record.claim_phase, record
        if record.desired_state != "running":
            return "drain_requested", record
        if cap is not None and record.claims >= cap:
            return "session_exhausted", record
        return ("active" if record.task_id else "drain_requested"), record

    async def release_claim_slot(self, conn, session_id: str) -> None:
        await conn.execute(
            update(sessions)
            .where(and_(sessions.c.id == session_id, sessions.c.claim_phase == "claiming"))
            .values(claim_phase=None, claim_phase_at=None)
        )

    async def select_ready_for_profile(
        self, conn, *, project_id, profile_id, default_profile_id, agent_id, task_id=None
    ) -> str | None:
        """The §10 work query.  Postgres takes the row FOR UPDATE SKIP LOCKED."""
        profile_ok = tasks.c.profile_id == profile_id
        if default_profile_id == profile_id:
            profile_ok = (tasks.c.profile_id == profile_id) | tasks.c.profile_id.is_(None)
        req = task_workspace_requirements.alias("req")
        stmt = (
            select(tasks.c.id)
            .where(
                _frontier_where(project_id),
                profile_ok,
                ~exists(
                    select(literal(1)).where(
                        and_(req.c.task_id == tasks.c.id, req.c.kind_id != "project-repo")
                    )
                ),
            )
            .order_by(
                case((tasks.c.affinity_agent_id == agent_id, 0), else_=1),
                tasks.c.priority.asc(),
                tasks.c.created_at.asc(),
            )
            .limit(1)
        )
        stmt = apply_label_filters(stmt, exclude_hold=True)
        if task_id is not None:
            stmt = stmt.where(tasks.c.id == task_id)
        if conn.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        row = (await conn.execute(stmt)).fetchone()
        return row[0] if row else None

    async def take_task(self, conn, task_id: str, *, agent_id: str, now: float) -> Task | None:
        res = await conn.execute(
            update(tasks)
            .where(
                and_(
                    tasks.c.id == task_id,
                    tasks.c.status == TaskStatus.READY.value,
                    tasks.c.is_blocked == 0,
                    tasks.c.assigned_agent_id.is_(None),
                )
            )
            .values(claim_epoch=tasks.c.claim_epoch + 1)
        )
        if res.rowcount != 1:
            return None
        await self._apply_transition(
            conn, task_id, TaskStatus.IN_PROGRESS, context="claim", assigned_agent_id=agent_id
        )
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().fetchone()
        return self._row_to_task(row)

    async def bump_claim_epoch(self, task_id: str) -> int:
        async with self.immediate() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == task_id)
                .values(claim_epoch=tasks.c.claim_epoch + 1)
            )
            return (
                await conn.execute(select(tasks.c.claim_epoch).where(tasks.c.id == task_id))
            ).scalar() or 0

    async def record_holder(self, conn, *, session_id, task_id, agent_id, work_dir, now) -> None:
        await conn.execute(
            update(sessions).where(sessions.c.id == session_id)
            .values(task_id=task_id, claim_phase="preparing", claim_phase_at=now)
        )
        await conn.execute(
            update(agents).where(agents.c.id == agent_id)
            .values(state=AgentState.BUSY.value, current_task_id=task_id)
        )
        await conn.execute(
            update(workspaces).where(workspaces.c.locked_by_agent_id == agent_id)
            .values(locked_by_task_id=task_id)
        )
        await self._upsert_meta(task_id, "claimed_by_session", session_id, conn=conn)
        await self._upsert_meta(task_id, "work_dir", work_dir, conn=conn)

    async def activate_claim(self, session_id, task_id, *, epoch: int, now: float) -> bool:
        async with self.immediate() as conn:
            res = await conn.execute(
                update(sessions)
                .where(
                    and_(
                        sessions.c.id == session_id,
                        sessions.c.claim_phase == "preparing",
                        sessions.c.task_id == task_id,
                    )
                )
                .values(
                    claim_phase="active", claim_phase_at=now, claims=sessions.c.claims + 1,
                    last_claim_epoch=epoch, last_claim_result="claimed",
                )
            )
            return res.rowcount == 1

    async def _release_claim_on(
        self, conn, session_id, *, task_status, context, now, result, needs_attention
    ) -> TransitionResult:
        row = (
            await conn.execute(select(sessions).where(sessions.c.id == session_id))
        ).mappings().fetchone()
        out = TransitionResult()
        if row is None:
            return out
        task_id, agent_id = row["task_id"], row["agent_id"]
        epoch = None
        if task_id:
            epoch = (
                await conn.execute(select(tasks.c.claim_epoch).where(tasks.c.id == task_id))
            ).scalar()
            out = await self._apply_transition(
                conn, task_id, task_status, context=context, force=True, assigned_agent_id=None
            )
            if needs_attention:
                await self._upsert_meta(task_id, "needs_attention", needs_attention, conn=conn)
            await conn.execute(
                update(workspaces).where(workspaces.c.locked_by_agent_id == agent_id)
                .values(locked_by_task_id=None)
            )
        if agent_id:
            await conn.execute(
                update(agents).where(agents.c.id == agent_id)
                .values(state=AgentState.IDLE.value, current_task_id=None)
            )
        await conn.execute(
            update(sessions).where(sessions.c.id == session_id)
            .values(task_id=None, claim_phase=None, claim_phase_at=None,
                    last_claim_epoch=epoch, last_claim_result=result)
        )
        return out

    async def _after_release(self, out: TransitionResult) -> None:
        await self.log_blocked_flips(out.flipped)
        await self._notify_settled(out.settled)
        await self._notify_ready(out.ready)

    async def release_claim(self, session_id, *, task_status, context, now, result="released",
                            needs_attention=None, conn=None) -> TransitionResult:
        kwargs = dict(task_status=task_status, context=context, now=now, result=result,
                      needs_attention=needs_attention)
        if conn is not None:
            return await self._release_claim_on(conn, session_id, **kwargs)
        async with self.immediate() as conn:
            out = await self._release_claim_on(conn, session_id, **kwargs)
        await self._after_release(out)
        return out

    async def terminate_pool_session(self, session_id, *, reason, task_status=TaskStatus.READY,
                                     conn=None) -> TransitionResult:
        async def _run(c):
            out = await self._release_claim_on(
                c, session_id, task_status=task_status, context=f"session_{reason}",
                now=time.time(), result="released", needs_attention=None,
            )
            row = (
                await c.execute(select(sessions.c.agent_id).where(sessions.c.id == session_id))
            ).fetchone()
            agent_id = row[0] if row else None
            if agent_id:
                await self.release_workspaces_for_agent(agent_id, conn=c)
                await c.execute(
                    update(agents).where(agents.c.id == agent_id)
                    .values(state=AgentState.RETIRED.value, current_task_id=None)
                )
            return out

        if conn is not None:
            return await _run(conn)
        async with self.immediate() as c:
            out = await _run(c)
        await self._after_release(out)
        return out

    async def reserve_filing(self, conn, task_id: str, *, max_filings: int) -> bool:
        res = await conn.execute(
            update(tasks)
            .where(and_(tasks.c.id == task_id, tasks.c.filed_count < max_filings))
            .values(filed_count=tasks.c.filed_count + 1)
        )
        return res.rowcount == 1

    async def count_ready_by_profile(self, project_id: str) -> dict[str | None, int]:
        stmt = (
            select(tasks.c.profile_id, func.count())
            .where(_frontier_where(project_id))
            .group_by(tasks.c.profile_id)
        )
        stmt = apply_label_filters(stmt, exclude_hold=True)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return {pid: int(n) for pid, n in rows}
```

`_READY_REASONS` (Task 2) must map `"claim"` to no entry (a claim leaves the frontier — `_note_frontier_entry` only fires when post-state is READY, so no special case is needed) and every `session_*` / `slot_reset_failed` context to `"released"`. `apply_label_filters(stmt, exclude_hold=True)` — confirm its exact keyword names in `blocked_state.py:591`.

`workspace_queries.py`: `get_workspace_for_agent(agent_id)` = `select(workspaces).where(locked_by_agent_id == agent_id)` first row → `_row_to_workspace`; `release_workspaces_for_agent(agent_id, *, conn=None)` executes on `conn` when given, else opens `self._engine.begin()` as today. `event_queries.py`: `max_event_id` (`select(func.max(events.c.id))`, `0` when `None`); `count_events_after(seq, *, event_type, project_id)` = `select(func.count()).where(events.c.id > seq, event_type ==, project_id ==)`. Register `ClaimQueryMixin` in `queries/__init__.py` (`__all__`), in both adapters (immediately after `HierarchyQueryMixin`), and add the public methods to `DatabaseBackend` in `base.py`.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_claim_queries.py tests/test_db_immediate.py tests/test_hierarchy_queries.py tests/test_hierarchy_settlement.py tests/test_task_ready_event.py tests/test_workspace_queries.py -v -n auto`, then the full suite. The concurrency test on SQLite goes through a single pooled connection — `asyncio.gather` still interleaves at await points and `immediate()` serialises the writers; exactly-once must hold. If `POSTGRES_TEST_DSN` is set locally, parametrize the `db` fixture over both adapters the way `tests/test_database_postgresql.py` does and run the file against Postgres too.

- [ ] **Step 6: Commit**

```bash
git add src/database/queries/claim_queries.py src/database/queries/__init__.py src/database/adapters/sqlite.py src/database/adapters/postgresql.py src/database/queries/task_queries.py src/database/queries/workspace_queries.py src/database/queries/event_queries.py src/database/base.py tests/test_claim_queries.py
git commit -m "feat(claims): claim transaction, activate/release/terminate, epoch fence, filing quota

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `task_claim` command, long-poll, claim file, fence on mutations, `close --claim-next`, prime for pools

**Files:**
- Create: `src/commands/claim_commands.py`
- Modify: `src/commands/handler.py` (add `ClaimCommandsMixin` to the bases)
- Modify: `src/orchestrator/core.py:379-381` (declare `self.claim_waiters: dict[tuple[str, int], asyncio.Future] = {}`), `:3144-3146` (`_schedule` emits `snapshot.refreshed` after caching the snapshot)
- Modify: `src/commands/session_commands.py:435-671` (`_cmd_task_close` fence + `claim_next` + pool release path; `_cmd_task_heartbeat` fence)
- Modify: `src/commands/surface_commands.py:114-185` (`_cmd_task_set` fence), `:191-240` (`_cmd_prime` session→task), `:246-304` (`_cmd_task_handoff` fence)
- Modify: `src/orchestrator/execution.py:1776-1967` (push launch bumps the epoch, writes the claim file, sets `AQ_CLAIM_EPOCH`), `:1991-2134` (`complete_session_task(..., expect_claim_epoch=None, pool=False)`)
- Modify: `src/sessions/spec.py:180-195,279-349` (`extra_env` through `build_task_spec` → `_build`), `src/sessions/env.py:98-172` (`build_session_env(..., extra_env=None)`)
- Modify: `src/event_bus.py` (`EventWaiter`, `waiter()`), `src/event_schemas.py` (`task.claimed`, `task.claim_conflict`, `snapshot.refreshed`, `project.resumed`, `constraint.released`)
- Modify: `src/commands/project_commands.py` (`resume_project` emits `project.resumed`; `release_project_constraint` emits `constraint.released`)
- Modify: `src/api/scope.py:14-32` (`AGENT_COMMAND_SET` += `task_claim`, `create_task`, `project_ready`), `tests/test_api_scope.py::test_agent_command_set_contents`
- Modify: `src/prime/sections.py` (task section prints `Claim epoch: N` when `> 0`)
- Test: `tests/test_claim_commands.py`

**Interfaces:**
- Consumes: everything in Task 3; `EventBus.subscribe` (returns unsubscribe); `swarm.claim_wait_max` (Task 1); `_worktree_slots().reset_slot_for_task(slot_ws, task, *, base_branch=None, resume_branch=None, kind=None)`.
- Produces:
  - `_cmd_task_claim(args) -> {"success": bool, "result": <ClaimResult value>, "task": <task_show payload> | None, "claim_epoch": int | None, "session": {"id", "claims", "cap", "desired_state", "claim_phase"}, "reason"?: str, "error"?: str}`. Args: `task_id?`, `next?: bool`, `wait?: int` (clamped to `[0, swarm.claim_wait_max]`), `session_id?` (elevated callers only).
  - Claim file `<work_dir>/.aq/claim.json` = `{"task_id", "claim_epoch", "session_id", "claimed_at"}`, written atomically (`.tmp` + `os.replace`) by module functions `write_claim_file(work_dir, payload) -> str` and `remove_claim_file(work_dir)`.
  - `EventBus.waiter(event_types: Iterable[str], *, filter: dict | None = None) -> EventWaiter` (subscribes at construction); `EventWaiter.wait(timeout: float) -> dict | None`; `EventWaiter.close()`; `wait_for` (Task 2) reimplemented on top.
  - `ClaimCommandsMixin._assert_session_owns(task_id, *, session_id, claim_epoch) -> dict | None` — `None` when the caller holds the task; else an error dict with `result` `stale_claim` / `out_of_scope`. Rules: no session in scope → `None` (local/elevated callers are not fenced); `sessions.task_id != task_id` → `out_of_scope`; `claim_epoch` given and `!= tasks.claim_epoch` → `stale_claim`; `claim_epoch` absent: pool sessions → `stale_claim` ("read .aq/claim.json"), task sessions → accepted (legacy).
  - `_cmd_task_close` args += `claim_epoch?: int`, `claim_next?: bool`, `wait?: int`; response += `"next": <task_claim response>` when `claim_next`. Pool sessions release via `db.release_claim(...)` (workspace agent-lock retained, token kept) and remove the claim file; task sessions keep today's `release_session_task_resources` + revoke.
  - `complete_session_task(..., expect_claim_epoch: int | None = None, pool: bool = False)` → its `transition_task` call passes `expect_claim_epoch`; `StaleClaim` propagates to the command which returns `{"success": False, "result": "stale_claim"}`; `pool=True` skips `release_session_task_resources` and the token revoke.
  - `_cmd_task_heartbeat`, `_cmd_task_set`, `_cmd_task_handoff` accept `claim_epoch?` and call `_assert_session_owns` when `session_id` is in scope.
  - `_cmd_prime`: when no `task_id` in args or scope and a `session_id` is in scope, uses `sessions.task_id`.
  - Push launches: `epoch = await db.bump_claim_epoch(task.id)` before `provider.start`; claim file written into `work_dir`; `AQ_CLAIM_EPOCH=<epoch>` in the env via `build_task_spec(..., extra_env=)` → `_build(..., extra_env=)` → `build_session_env(..., extra_env=)` (applied after the markers, before scrubbing).
  - Events (registered): `task.claimed {task_id, project_id, title; optional session_id, profile_id, claim_epoch}`, `task.claim_conflict {task_id, project_id, title; optional session_id}`, `snapshot.refreshed {tick}`, `project.resumed {project_id}`, `constraint.released {project_id}`.
  - `orchestrator.claim_waiters[(session_id, epoch)] -> asyncio.Future[str]` resolved with `"claimed"` / `"prepare_failed"` / `"released"` by `_resolve_claim_waiters(session_id, epoch, result)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claim_commands.py
"""aq task claim / close --claim-next / epoch fence — spec §10, §14."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import (Agent, AgentProfile, AgentState, Project, SessionRecord, Task,
                        TaskStatus, Workspace)
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"
NOW = time.time()


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(AgentProfile(id="worker", name="w", lifecycle="pool",
                                               needs_workspace=False))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                    workspace_dir=str(tmp_path / "ws"), database_path=str(tmp_path / "test.db"),
                    data_dir=str(tmp_path / "data"))
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    cfg.swarm.claim_wait_max = 5
    return cfg


@pytest.fixture
async def handler(db, config):
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch._worktree_slots = MagicMock(return_value=MagicMock(
        reset_slot_for_task=AsyncMock(return_value="aq/t")))
    orch._last_scheduler_state = None  # no snapshot yet → admissible
    return CommandHandler(orch, config)


async def mktask(db, tid, status=TaskStatus.READY, **kw):
    await db.create_task(Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid,
                              status=status, **kw))


async def pool_session(db, tmp_path, sid="s1", agent_id="agent-1"):
    work_dir = tmp_path / agent_id
    work_dir.mkdir()
    await db.create_agent(Agent(id=agent_id, name=agent_id, profile_id="worker",
                                state=AgentState.IDLE))
    await db.create_workspace(Workspace(id=f"ws-{agent_id}", project_id=PROJECT_ID,
                                        workspace_path=str(work_dir), kind_id="project-repo",
                                        locked_by_agent_id=agent_id))
    await db.create_session(SessionRecord(
        id=sid, project_id=PROJECT_ID, profile_id="worker", harness="claude", provider="fake",
        name=f"p-worker--proj--{sid}", lifecycle="pool", work_dir=str(work_dir), epoch="e",
        instance_token="t", started_at=NOW, state="running", agent_id=agent_id))
    return sid, work_dir


def scoped(handler, sid):
    handler._current_scope = {"kind": "session", "session_id": sid, "task_id": None,
                              "project_id": PROJECT_ID, "elevated": False}
    return handler


def emitted(handler):
    return [c.args[0] for c in handler.orchestrator.bus.emit.await_args_list]


class TestClaim:
    async def test_claim_next_returns_task_epoch_and_writes_file(self, handler, db, tmp_path):
        handler.orchestrator.bus.emit = AsyncMock()
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert (res["result"], res["task"]["id"], res["claim_epoch"]) == ("claimed", "t1", 1)
        data = json.loads((wd / ".aq" / "claim.json").read_text())
        assert (data["task_id"], data["claim_epoch"], data["session_id"]) == ("t1", 1, sid)
        assert (await db.get_session(sid)).claim_phase == "active"
        assert "task.claimed" in emitted(handler) and "task.started" in emitted(handler)

    async def test_no_ready_work_without_wait(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "no_ready_work"
        assert (await db.get_session(sid)).claim_phase is None

    async def test_wait_wakes_on_task_ready(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)

        async def promote():
            await asyncio.sleep(0.05)
            await mktask(db, "late", status=TaskStatus.DEFINED, profile_id="worker")
            await db.transition_task("late", TaskStatus.READY, context="promotion")

        asyncio.create_task(promote())
        t0 = time.monotonic()
        res = await scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 3})
        assert (res["result"], res["task"]["id"]) == ("claimed", "late")
        assert time.monotonic() - t0 < 2.0  # woke on the event, not the deadline

    async def test_wait_clamped_and_times_out(self, handler, db, tmp_path):
        sid, _ = await pool_session(db, tmp_path)
        handler.config.swarm.claim_wait_max = 0
        res = await scoped(handler, sid)._cmd_task_claim({"next": True, "wait": 100})
        assert res["result"] == "no_ready_work"

    async def test_prepare_failed_releases_and_reports(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        handler.orchestrator._worktree_slots.return_value.reset_slot_for_task = AsyncMock(
            side_effect=RuntimeError("git exploded"))
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert res["result"] == "prepare_failed"
        assert not (wd / ".aq" / "claim.json").exists()
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        s = await db.get_session(sid)
        assert (s.claims, s.last_claim_result, s.claim_phase) == (0, "prepare_failed", None)

    async def test_duplicate_claim_is_idempotent_once_active(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        second = await h._cmd_task_claim({"next": True})
        assert (second["result"], second["claim_epoch"]) == ("claimed", first["claim_epoch"])

    async def test_specific_task_held_by_other_is_conflict(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        s1, _ = await pool_session(db, tmp_path, sid="s1", agent_id="agent-1")
        s2, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        await scoped(handler, s1)._cmd_task_claim({"task_id": "t1"})
        res = await scoped(handler, s2)._cmd_task_claim({"task_id": "t1"})
        assert res["result"] == "claim_conflict"

    async def test_session_exhausted_after_cap_via_close_claim_next(self, handler, db, tmp_path):
        await db.update_profile("worker", max_claims_per_session=1)
        await mktask(db, "t1", profile_id="worker")
        await mktask(db, "t2", profile_id="worker")
        sid, wd = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        first = await h._cmd_task_claim({"next": True})
        closed = await h._cmd_task_close({"task_id": "t1", "outcome": "pass",
                                          "summary": "done",
                                          "claim_epoch": first["claim_epoch"],
                                          "claim_next": True})
        assert closed["success"] is True
        assert closed["next"]["result"] == "session_exhausted"
        assert not (wd / ".aq" / "claim.json").exists()
        assert (await db.get_task("t1")).status == TaskStatus.COMPLETED
        assert (await db.get_workspace_for_agent("agent-1")).locked_by_agent_id == "agent-1"

    async def test_not_admissible_when_project_paused(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        await db.update_project(PROJECT_ID, status="PAUSED")
        res = await scoped(handler, sid)._cmd_task_claim({"next": True})
        assert (res["result"], res["reason"]) == ("not_admissible", "project_inactive")

    async def test_task_lifecycle_session_reclaims_own_task_only(self, handler, db, tmp_path):
        await mktask(db, "mine", status=TaskStatus.IN_PROGRESS, profile_id="worker",
                     assigned_agent_id="agent-1", claim_epoch=1)
        await mktask(db, "other", profile_id="worker")
        await db.create_session(SessionRecord(
            id="s-task", project_id=PROJECT_ID, profile_id="worker", harness="claude",
            provider="fake", name="s-task", lifecycle="task", task_id="mine", work_dir="/x",
            epoch="e", instance_token="t", started_at=NOW, state="running"))
        h = scoped(handler, "s-task")
        assert (await h._cmd_task_claim({"next": True}))["result"] == "claimed"
        assert (await h._cmd_task_claim({"task_id": "other"}))["result"] == "out_of_scope"


class TestFence:
    async def test_stale_epoch_rejected_on_close(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})  # epoch 1
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=NOW)
        await h._cmd_task_claim({"next": True})  # epoch 2
        res = await h._cmd_task_close({"task_id": "t1", "outcome": "pass", "summary": "x",
                                       "claim_epoch": 1})
        assert res["result"] == "stale_claim"
        assert (await db.get_task("t1")).status == TaskStatus.IN_PROGRESS

    async def test_pool_session_must_send_epoch(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        res = await h._cmd_task_close({"task_id": "t1", "outcome": "pass", "summary": "x"})
        assert res["result"] == "stale_claim"

    async def test_heartbeat_set_handoff_require_matching_epoch(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        assert (await h._cmd_task_heartbeat({"task_id": "t1", "claim_epoch": 1}))["success"]
        assert (await h._cmd_task_heartbeat({"task_id": "t1", "claim_epoch": 7}))["result"] == "stale_claim"
        assert (await h._cmd_task_set({"task_id": "t1", "note": "x", "claim_epoch": 7}))["result"] == "stale_claim"
        assert (await h._cmd_task_handoff({"task_id": "t1", "reason": "x", "claim_epoch": 7}))["result"] == "stale_claim"

    async def test_other_session_is_out_of_scope(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        s1, _ = await pool_session(db, tmp_path, sid="s1", agent_id="agent-1")
        s2, _ = await pool_session(db, tmp_path, sid="s2", agent_id="agent-2")
        await scoped(handler, s1)._cmd_task_claim({"next": True})
        res = await scoped(handler, s2)._cmd_task_heartbeat({"task_id": "t1", "claim_epoch": 1})
        assert res["result"] == "out_of_scope"

    async def test_prime_resolves_task_from_session(self, handler, db, tmp_path):
        await mktask(db, "t1", profile_id="worker")
        sid, _ = await pool_session(db, tmp_path)
        h = scoped(handler, sid)
        await h._cmd_task_claim({"next": True})
        res = await h._cmd_prime({})
        assert res["success"] is True
        body = res.get("body") or res.get("prompt") or json.dumps(res)
        assert "t1" in body and "Claim epoch: 1" in body


class TestEventWaiter:
    async def test_waiter_subscribes_before_check(self):
        from src.event_bus import EventBus

        bus = EventBus()
        w = bus.waiter(["task.ready"], filter={"project_id": "p"})
        await bus.emit("task.ready", {"task_id": "t", "project_id": "p", "title": "t"})
        assert (await w.wait(0.5))["task_id"] == "t"
        w.close()
        assert bus.subscriber_count("task.ready") == 0
```

Read `_cmd_prime`'s actual response shape (`body`? `prompt`?) and `_cmd_task_handoff`'s required args before finalising the two asserts; `db.update_project(..., status=)` / `update_profile` signatures — use the existing writers.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_claim_commands.py -v`
Expected: FAIL — `AttributeError: 'CommandHandler' object has no attribute '_cmd_task_claim'`.

- [ ] **Step 3: `EventWaiter`**

`src/event_bus.py`:

```python
class EventWaiter:
    """One-shot subscription created *before* a check so no event is missed."""

    def __init__(self, bus: "EventBus", event_types, filter=None):
        self._fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._unsubs = [bus.subscribe(t, self._on, filter=filter) for t in event_types]

    async def _on(self, data):
        if not self._fut.done():
            self._fut.set_result(dict(data))

    async def wait(self, timeout: float):
        try:
            return await asyncio.wait_for(asyncio.shield(self._fut), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []
```

`EventBus.waiter(self, event_types, *, filter=None) -> EventWaiter`; `wait_for` becomes `w = self.waiter(...); try: return await w.wait(timeout) finally: w.close()`. Check how `subscribe`'s `filter` is applied (dict equality on the payload keys) — the waiter relies on it.

- [ ] **Step 4: `ClaimCommandsMixin`**

```python
# src/commands/claim_commands.py
"""``aq task claim`` — pull-based work selection (swarm-work-model §10)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from src.models import ClaimResult, TaskStatus

logger = logging.getLogger(__name__)

CLAIM_FILE = os.path.join(".aq", "claim.json")
_ADMISSION_EVENTS = ("project.resumed", "constraint.released", "snapshot.refreshed")
_FRONTIER_EVENTS = ("task.ready", "gate.resolved", "task.restarted")


def write_claim_file(work_dir: str, payload: dict) -> str:
    path = os.path.join(work_dir, CLAIM_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    return path


def remove_claim_file(work_dir: str) -> None:
    try:
        os.remove(os.path.join(work_dir, CLAIM_FILE))
    except FileNotFoundError:
        pass


class ClaimCommandsMixin:
    """Mixed into CommandHandler.  Needs ``self.db``, ``self.orchestrator``, ``self.config``."""

    # -- fence ---------------------------------------------------------------

    async def _assert_session_owns(self, task_id, *, session_id, claim_epoch) -> dict | None:
        if not session_id:
            return None
        session = await self.db.get_session(session_id)
        if session is None:
            return {"success": False, "result": ClaimResult.OUT_OF_SCOPE.value,
                    "error": f"No session '{session_id}'"}
        if session.task_id != task_id:
            return {"success": False, "result": ClaimResult.OUT_OF_SCOPE.value,
                    "error": f"session {session_id} does not hold task {task_id}"}
        task = await self.db.get_task(task_id)
        if task is None:
            return {"success": False, "error": f"No task '{task_id}'"}
        if claim_epoch is None:
            if session.lifecycle == "pool":
                return {"success": False, "result": ClaimResult.STALE_CLAIM.value,
                        "error": "claim_epoch is required for pool sessions "
                                 "(read it from .aq/claim.json)"}
            return None
        if int(claim_epoch) != task.claim_epoch:
            return {"success": False, "result": ClaimResult.STALE_CLAIM.value,
                    "error": f"claim epoch {claim_epoch} is not current for {task_id} "
                             f"(current {task.claim_epoch}); the task is no longer yours"}
        return None

    # -- admission -----------------------------------------------------------

    def _admission_reason(self, project) -> str | None:
        if project is None or getattr(project.status, "value", project.status) != "ACTIVE":
            return "project_inactive"
        state = getattr(self.orchestrator, "_last_scheduler_state", None)
        if state is None:
            return None
        from src.scheduler import _is_scheduling_paused

        if _is_scheduling_paused(project.id, state.project_constraints):
            return "scheduling_paused"
        if state.global_budget is not None and state.global_tokens_used >= state.global_budget:
            return "budget_exhausted"
        limit = getattr(project, "token_budget", None)
        if limit and state.project_token_usage.get(project.id, 0) >= limit:
            return "budget_exhausted"
        return None

    # -- the command -----------------------------------------------------------

    async def _cmd_task_claim(self, args: dict) -> dict:
        """Claim a ready task for the calling session (``aq task claim``)."""
        scope = self._current_scope or {}
        session_id = scope.get("session_id") or (args.get("session_id") if scope.get("elevated", True) else None)
        if not session_id:
            return {"success": False, "result": ClaimResult.OUT_OF_SCOPE.value,
                    "error": "task_claim needs a session in scope"}
        session = await self.db.get_session(session_id)
        if session is None or session.lifecycle not in ("pool", "task"):
            return {"success": False, "result": ClaimResult.OUT_OF_SCOPE.value,
                    "error": "not a claimable session"}
        if scope.get("project_id") and scope["project_id"] != session.project_id:
            return {"success": False, "result": ClaimResult.OUT_OF_SCOPE.value,
                    "error": "project_id mismatch"}
        want_id = args.get("task_id")
        if not want_id and not args.get("next"):
            return {"success": False, "error": "task_id or next=true is required"}
        wait = max(0, min(int(args.get("wait") or 0), int(self.config.swarm.claim_wait_max)))
        deadline = time.monotonic() + wait

        if session.lifecycle == "task":
            if session.task_id and want_id in (None, session.task_id):
                task = await self.db.get_task(session.task_id)
                return await self._claimed_response(task, task.claim_epoch, session)
            return {"success": False, "result": ClaimResult.OUT_OF_SCOPE.value,
                    "error": "task sessions cannot claim other work"}

        profile = await self.db.get_profile(session.profile_id)
        cap = getattr(profile, "max_claims_per_session", None)
        project = await self.db.get_project(session.project_id)
        default_profile = getattr(project, "default_profile_id", None)

        while True:
            reason = self._admission_reason(project)
            if reason:
                if time.monotonic() >= deadline:
                    return {"success": False, "result": ClaimResult.NOT_ADMISSIBLE.value,
                            "reason": reason, "task": None, "claim_epoch": None}
                w = self.orchestrator.bus.waiter(_ADMISSION_EVENTS)
                try:
                    await w.wait(max(0.0, deadline - time.monotonic()))
                finally:
                    w.close()
                project = await self.db.get_project(session.project_id)
                continue

            waiter = self.orchestrator.bus.waiter(
                _FRONTIER_EVENTS, filter={"project_id": session.project_id}
            )
            try:
                seq0 = await self.db.max_event_id()
                outcome = await self._attempt_claim(session, want_id, cap, default_profile)
                result = outcome["result"]
                if result == ClaimResult.NO_READY_WORK.value and wait:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return outcome
                    if await self.db.count_events_after(
                        seq0, event_type="task.ready", project_id=session.project_id
                    ):
                        continue
                    if await waiter.wait(remaining) is None:
                        return outcome
                    continue
                if result == ClaimResult.CLAIM_IN_PROGRESS.value and wait:
                    settled = await self._await_attempt(session.id, outcome.get("claim_epoch"),
                                                        deadline)
                    if settled is None:
                        return outcome
                    continue  # re-run: active → claimed; released → new attempt
                return outcome
            finally:
                waiter.close()

    async def _attempt_claim(self, session, want_id, cap, default_profile) -> dict:
        now = time.time()
        async with self.db.immediate() as conn:
            kind, row = await self.db.take_claim_slot(conn, session.id, now=now, cap=cap)
            if kind == "active":
                if want_id and want_id != row.task_id:
                    return self._simple(ClaimResult.OUT_OF_SCOPE, "session already holds a task", row)
                task = await self.db.get_task(row.task_id)
                return await self._claimed_response(task, task.claim_epoch, row)
            if kind in ("preparing", "claiming"):
                epoch = None
                if row.task_id:
                    held = await self.db.get_task(row.task_id)
                    epoch = held.claim_epoch if held else None
                out = self._simple(ClaimResult.CLAIM_IN_PROGRESS, kind, row)
                out.update(task_id=row.task_id, claim_epoch=epoch, claim_phase=row.claim_phase,
                           claim_phase_at=row.claim_phase_at)
                return out
            if kind == "session_exhausted":
                return self._simple(ClaimResult.SESSION_EXHAUSTED, "max_claims_per_session reached", row)
            if kind == "drain_requested":
                return self._simple(ClaimResult.DRAIN_REQUESTED, "pool scaled down", row)
            if kind != "slot":
                return self._simple(ClaimResult.OUT_OF_SCOPE, kind, row)
            tid = await self.db.select_ready_for_profile(
                conn, project_id=session.project_id, profile_id=session.profile_id,
                default_profile_id=default_profile, agent_id=row.agent_id, task_id=want_id)
            task = None
            if tid is not None:
                task = await self.db.take_task(conn, tid, agent_id=row.agent_id, now=now)
            if task is None:
                await self.db.release_claim_slot(conn, session.id)
                miss = ClaimResult.CLAIM_CONFLICT if want_id else ClaimResult.NO_READY_WORK
                return self._simple(miss, "", row)
            await self.db.record_holder(conn, session_id=session.id, task_id=tid,
                                        agent_id=row.agent_id, work_dir=row.work_dir, now=now)
        if want_id and task is not None:
            pass  # nothing else to do inside the transaction
        return await self._prepare_and_activate(session, row, task)

    async def _prepare_and_activate(self, session, row, task) -> dict:
        epoch = task.claim_epoch
        try:
            slot = await self.db.get_workspace_for_agent(row.agent_id)
            if slot is None:
                raise RuntimeError("session holds no workspace slot")
            await self.orchestrator._worktree_slots().reset_slot_for_task(slot, task)
        except Exception as exc:
            logger.warning("claim %s/%s: slot reset failed: %s", session.id, task.id, exc)
            await self.db.release_claim(
                session.id, task_status=TaskStatus.READY, context="slot_reset_failed",
                now=time.time(), result="prepare_failed", needs_attention="slot_reset_failed")
            self._resolve_claim_waiters(session.id, epoch, "prepare_failed")
            return self._simple(ClaimResult.PREPARE_FAILED, str(exc), row)
        write_claim_file(row.work_dir, {"task_id": task.id, "claim_epoch": epoch,
                                        "session_id": session.id, "claimed_at": time.time()})
        if not await self.db.activate_claim(session.id, task.id, epoch=epoch, now=time.time()):
            remove_claim_file(row.work_dir)
            self._resolve_claim_waiters(session.id, epoch, "prepare_failed")
            return self._simple(ClaimResult.PREPARE_FAILED, "released before activation", row)
        self._resolve_claim_waiters(session.id, epoch, "claimed")
        await self.orchestrator._emit_task_event(
            "task.claimed", task, session_id=session.id, profile_id=session.profile_id,
            claim_epoch=epoch)
        await self.orchestrator._emit_task_event("task.started", task, agent_id=row.agent_id)
        fresh = await self.db.get_session(session.id)
        return await self._claimed_response(task, epoch, fresh)

    # -- helpers -------------------------------------------------------------------

    def _session_block(self, row) -> dict:
        if row is None:
            return {}
        return {"id": row.id, "claims": row.claims, "cap": None, "desired_state": row.desired_state,
                "claim_phase": row.claim_phase}

    def _simple(self, result: ClaimResult, reason: str, row=None) -> dict:
        out = {"success": False, "result": result.value, "task": None, "claim_epoch": None,
               "session": self._session_block(row)}
        if reason:
            out["reason"] = reason
        return out

    async def _claimed_response(self, task, epoch: int, row) -> dict:
        shown = await self._cmd_task_show({"task_id": task.id})
        return {"success": True, "result": ClaimResult.CLAIMED.value,
                "task": shown.get("task", shown), "claim_epoch": epoch,
                "session": self._session_block(row)}

    def _resolve_claim_waiters(self, session_id: str, epoch: int | None, result: str) -> None:
        fut = self.orchestrator.claim_waiters.pop((session_id, epoch), None)
        if fut is not None and not fut.done():
            fut.set_result(result)

    async def _await_attempt(self, session_id: str, epoch: int | None, deadline: float):
        """Wait for the in-flight attempt to settle; poll the row when no future exists."""
        key = (session_id, epoch)
        fut = self.orchestrator.claim_waiters.get(key)
        while time.monotonic() < deadline:
            if fut is not None:
                try:
                    return await asyncio.wait_for(asyncio.shield(fut),
                                                  timeout=max(0.0, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    return None
            await asyncio.sleep(0.2)
            row = await self.db.get_session(session_id)
            if row is None:
                return None
            if row.claim_phase in (None, "active"):
                return row.last_claim_result or "released"
        return None
```

Add `ClaimCommandsMixin` to `CommandHandler`'s bases (before the other command mixins so its helper names resolve first) and `self.claim_waiters: dict[tuple[str, int | None], asyncio.Future] = {}` to `Orchestrator.__init__`. Fill `"cap"` in `_session_block` from the profile when cheap (the caller already loaded it — pass it in).

- [ ] **Step 5: Fence and `--claim-next` in the existing commands**

`_cmd_task_close` (`session_commands.py:435`): right after the caller-validation block, add

```python
        caller_session_id = (self._current_scope or {}).get("session_id")
        claim_epoch = args.get("claim_epoch")
        err = await self._assert_session_owns(task_id, session_id=caller_session_id,
                                              claim_epoch=claim_epoch)
        if err:
            return err
        session = await self.db.get_session(caller_session_id) if caller_session_id else None
        is_pool = bool(session and session.lifecycle == "pool")
```

Pass `expect_claim_epoch=int(claim_epoch) if claim_epoch is not None else None, pool=is_pool` into `complete_session_task(...)`; wrap the call in `try/except StaleClaim` returning `{"success": False, "result": "stale_claim", "error": str(exc)}`. When `is_pool`: after `complete_session_task` returns, `await self.db.release_claim(session.id, task_status=<final status of the task>, context="session_close", now=time.time())` — note the task is already terminal here, so `release_claim` must tolerate `task_status == current` (the same-status branch of `_apply_transition` handles it; pass the task's current status), then `remove_claim_file(session.work_dir)`; skip the token revoke. When `args.get("claim_next")`: `nxt = await self._cmd_task_claim({"next": True, "wait": int(args.get("wait") or 0)})`; `result["next"] = nxt`.

`complete_session_task` (`execution.py:1991`): new kwargs `expect_claim_epoch=None, pool=False`; pass `expect_claim_epoch` to its `transition_task(...)`; when `pool`, skip `release_session_task_resources` and the token revoke (`if not pool:` around both).

`_cmd_task_heartbeat`, `_cmd_task_set`, `_cmd_task_handoff`: first statement after arg parsing:

```python
        err = await self._assert_session_owns(
            task_id, session_id=(self._current_scope or {}).get("session_id"),
            claim_epoch=args.get("claim_epoch"))
        if err:
            return err
```

`_cmd_prime`: after the existing scope fallback: `if not task_id and scope.get("session_id"): s = await self.db.get_session(scope["session_id"]); task_id = s.task_id if s else None`. `src/prime/sections.py`: in the task section, after the status line, `if task.claim_epoch: lines.append(f"Claim epoch: {task.claim_epoch}")`.

- [ ] **Step 6: Push launches join the fence**

`_launch_session_for_task` (`execution.py:1776`): after the workspace is prepared and before `build_task_spec`: `epoch = await self.db.bump_claim_epoch(task.id)`; `write_claim_file(work_dir, {"task_id": task.id, "claim_epoch": epoch, "session_id": session_id, "claimed_at": time.time()})`; `spec = build_task_spec(..., extra_env={"AQ_CLAIM_EPOCH": str(epoch)})`. Thread `extra_env: dict[str, str] | None = None` through `build_task_spec` → `_build` → `build_session_env(..., extra_env=extra_env)`, where it is merged after the markers: `if extra_env: env.update(extra_env)`. Add `AQ_CLAIM_EPOCH` next to the other `AQ_*` markers in `env.py`'s docstring.

- [ ] **Step 7: Events, emitters, scope**

`event_schemas.py`: `"task.claimed": {"required": ["task_id", "project_id", "title"], "optional": ["session_id", "profile_id", "claim_epoch"]}`, `"task.claim_conflict": {..., "optional": ["session_id"]}`; in a new `_SWARM_SCHEMAS` dict: `"snapshot.refreshed": {"required": ["tick"]}`, `"project.resumed": {"required": ["project_id"]}`, `"constraint.released": {"required": ["project_id"]}` — merged into the registry like `_SESSION_SCHEMAS`. `_schedule` (core.py, after the three cache assignments at 3144-3146): `await self.bus.emit("snapshot.refreshed", {"tick": time.time()})`. `project_commands.py`: `resume_project` emits `project.resumed`; `release_project_constraint` emits `constraint.released` (grep both names; if the constraint command is named differently, use the real one). `AGENT_COMMAND_SET` += `"task_claim", "create_task", "project_ready"`; update `tests/test_api_scope.py::test_agent_command_set_contents`.

- [ ] **Step 8: Run tests**

Run: `pytest tests/test_claim_commands.py tests/test_claim_queries.py tests/test_session_commands.py tests/test_task_close_summary_enforcement.py tests/test_surface_commands.py tests/test_api_scope.py tests/test_event_schema_registry_validation.py tests/test_emit_schema_compliance.py tests/test_sessions_spec.py tests/test_sessions_env.py -v -n auto`, then the full suite.

- [ ] **Step 9: Commit**

```bash
git add src/commands/claim_commands.py src/commands/handler.py src/commands/session_commands.py src/commands/surface_commands.py src/commands/project_commands.py src/orchestrator/execution.py src/orchestrator/core.py src/sessions/spec.py src/sessions/env.py src/event_bus.py src/event_schemas.py src/api/scope.py src/prime/sections.py tests/test_claim_commands.py tests/test_api_scope.py
git commit -m "feat(claims): task_claim with long-poll and claim file; epoch fence on close/heartbeat/set/handoff; close --claim-next

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Pool sizing (`size_pools`), `_reconcile_pools`, `_launch_pool_session`, push-scheduler exclusion

**Files:**
- Modify: `src/scheduler.py` (pure `size_pools`, `PoolKey`, `PoolSupply`, `PoolAction`)
- Create: `src/orchestrator/pools.py` (`PoolsMixin`)
- Modify: `src/orchestrator/core.py` (`Orchestrator` bases += `PoolsMixin`; `__init__` declares `self._pool_surplus_since: dict = {}` and `self._pool_quarantine: dict = {}`; `run_one_cycle` step right after `_schedule` at `:2468`: `await self._reconcile_pools()`; `_schedule` filters pool-profile tasks; `_recover_stale_state` reaper `:1984-2027` also deletes `RETIRED` agents and pool-profile agents without a session row)
- Modify: `src/orchestrator/execution.py:1712-1722` (`_is_session_routed` → `False` for pool profiles)
- Modify: `src/orchestrator/agent_reconciler.py:40-222` (skip `lifecycle == "pool"` profiles)
- Modify: `src/sessions/spec.py` (`POOL_BOOTSTRAP_PROMPT`, `pool_session_name`, `build_pool_spec`), `src/sessions/env.py` (docstring for the new markers), `src/env_scrub.py:168` (allow `GIT_AUTHOR_*`/`GIT_COMMITTER_*` if stripped)
- Modify: `src/sessions/reconciler.py:212` (adopt prefix `("s-", "n-", "p-")`)
- Modify: `src/event_schemas.py` (`pool.scaled {project_id, profile_id, kind, count}`)
- Test: `tests/test_pool_sizing.py`, `tests/test_pool_reconciler.py`

**Interfaces:**
- Consumes: `count_ready_by_profile` (Task 3), `list_sessions(lifecycle=, project_id=)` + pool fields (Task 1), `SwarmConfig` (Task 1), `terminate_pool_session` (Task 3), `remove_claim_file` (Task 4), `extra_env` hook in `_build` (Task 4), `SessionTokenStore.mint(session_id=, task_id=None, project_id=)`, `acquire_one_unlocked(project_id, kind_id, mode, locked_by_task_id, locked_by_agent_id, prefer_workspace_id, kind_mode, worktree_slot_cap)`, `session_providers`, `harness_registry`, `session_spec_builder`.
- Produces:
  - `scheduler.py`:
    ```python
    @dataclass(frozen=True)
    class PoolKey:
        project_id: str
        profile_id: str

    @dataclass
    class PoolSupply:
        running_idle: int = 0     # running, claim_phase NULL, task_id NULL
        running_busy: int = 0     # running, task_id set (any claim_phase)
        starting: int = 0         # state == 'starting'
        draining: int = 0         # desired_state == 'stopped'
        idle_session_ids: list[str] = field(default_factory=list)  # oldest first

    @dataclass(frozen=True)
    class PoolAction:
        key: PoolKey
        kind: str          # "start" | "drain"
        count: int
        session_ids: tuple[str, ...] = ()   # for drains

    def size_pools(*, supply: dict[PoolKey, PoolSupply], demand: dict[PoolKey, int],
                   bounds: dict[PoolKey, tuple[int, int | None]],     # (min_active, max_active)
                   project_caps: dict[str, int | None], global_cap: int | None,
                   surplus_since: dict[PoolKey, float], now: float,
                   scale_down_grace: float, max_starts_per_tick: int,
                   max_drains_per_tick: int) -> tuple[list[PoolAction], dict[PoolKey, float]]
    ```
    Returns the actions and the updated `surplus_since` map. Rules (spec §11.1): `want = busy + ready`; `desired = clamp(want, min, max)` (`max=None` unbounded); `desired = max(desired, busy + starting)`; `current = idle + busy + starting`; the project cap applies to the sum of that project's pools and the global cap to everything — when a cap binds, remaining capacity is handed out round-robin one start at a time across the pools that still want more (fair-share); starts total ≤ `max_starts_per_tick`; scale-down: when `current - draining > desired` and the key has been in surplus ≥ `scale_down_grace` (tracked via `surplus_since`), drain `min(surplus, idle, drains remaining this tick)` idle sessions oldest first; keys no longer in surplus drop out of `surplus_since`.
  - `PoolsMixin` (`src/orchestrator/pools.py`):
    - `async _pool_profiles(self, project_id) -> dict[str, AgentProfile]` — effective pool profiles (project override wins; an override with a non-pool lifecycle removes the system pool profile for that project).
    - `async _pool_profile_ids(self, project_id) -> set[str]`.
    - `async _measure_pools(self, project_ids=None) -> tuple[supply, demand, bounds, profiles_by_key, project_caps, projects]` — one `count_ready_by_profile` + one `list_sessions` per project; unrouted ready tasks (`profile_id IS NULL`) count toward the project's default profile's pool.
    - `async _reconcile_pools(self) -> None` — no-op unless `config.swarm.enabled and config.sessions.enabled`; measures, calls `size_pools`, skips starts for keys quarantined in `self._pool_quarantine` (until > now), executes starts via `_launch_pool_session` and drains via `update_session(sid, desired_state="stopped")`; emits `pool.scaled` per action with the count actually executed.
    - `async _launch_pool_session(self, project, profile) -> str | None`.
    - `async _terminate_pool_session(self, session, *, reason, task_status=TaskStatus.READY) -> None` — `db.terminate_pool_session` + `token_store.revoke_session(session.id)` (when a store exists) + `remove_claim_file(session.work_dir)` + provider stop (guarded) + `update_session(state="stopped")` if not already terminal.
  - `spec.py`: `pool_session_name(profile_id, project_id, nonce) -> str` = `f"p-{profile_id}--{project_id}--{nonce}"` (session id == name); `POOL_BOOTSTRAP_PROMPT`; `build_pool_spec(*, profile, project, session_id, agent_id, work_dir, harness, token, config, ...)` mirroring `build_named_spec` with `lifecycle="pool"` and `extra_env={"AQ_SESSION_KIND": "pool", "AQ_AGENT_ID": agent_id, "AQ_PROFILE_ID": profile.id, "GIT_AUTHOR_NAME": f"aq {profile.id}", "GIT_COMMITTER_NAME": f"aq {profile.id}", "GIT_AUTHOR_EMAIL": f"{profile.id}@agent-queue.local", "GIT_COMMITTER_EMAIL": f"{profile.id}@agent-queue.local"}`.
  - `POOL_BOOTSTRAP_PROMPT` (spec §11.3), formatted with `project_name` and `profile_id`:
    ```
    You are a pool worker for project {project_name} (profile {profile_id}).
    Loop: run `aq task claim --next --wait 60`. On `claimed`, run `aq prime`, do the
    work, then `aq task close --outcome pass|fail --summary "..." --claim-next --wait 60`.
    On `no_ready_work`, claim again. On `session_exhausted` or `drain_requested`, exit 0.
    On `not_admissible`, wait as instructed and claim again. Never touch tasks you do not
    hold; .aq/claim.json in your workspace is the proof of what you hold.
    ```
  - `_schedule` drops tasks whose resolved profile (`task.profile_id` or the project default) is in `_pool_profile_ids(project_id)` before building `SchedulerState`; `_is_session_routed(task)` returns `False` for them; `AgentReconciler.reconcile` ignores pool profiles.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pool_sizing.py
"""size_pools — spec §11.1 desired-state sizing.  Pure function, no I/O."""

from __future__ import annotations

from src.scheduler import PoolKey, PoolSupply, size_pools

K = PoolKey("proj", "worker")
K2 = PoolKey("proj", "reviewer")
KB = PoolKey("other", "worker")


def run(**over):
    kw = dict(supply={}, demand={}, bounds={}, project_caps={}, global_cap=None,
              surplus_since={}, now=1000.0, scale_down_grace=120, max_starts_per_tick=2,
              max_drains_per_tick=5)
    kw.update(over)
    return size_pools(**kw)


def test_scale_up_to_want_bounded_by_max_and_tick():
    actions, _ = run(supply={K: PoolSupply()}, demand={K: 5}, bounds={K: (0, 3)})
    assert [(a.kind, a.count) for a in actions] == [("start", 2)]  # min(3, 5) capped at 2/tick


def test_min_active_keeps_idle_workers():
    actions, _ = run(supply={K: PoolSupply()}, demand={K: 0}, bounds={K: (1, 3)})
    assert [(a.kind, a.count) for a in actions] == [("start", 1)]


def test_never_below_busy_plus_starting():
    actions, _ = run(supply={K: PoolSupply(running_busy=2, starting=1)}, demand={K: 0},
                     bounds={K: (0, 1)})
    assert actions == []


def test_scale_down_waits_for_grace_then_drains_idle_oldest_first():
    sup = {K: PoolSupply(running_idle=3, idle_session_ids=["a", "b", "c"])}
    actions, since = run(supply=sup, demand={K: 0}, bounds={K: (1, 5)})
    assert actions == [] and since == {K: 1000.0}
    actions, since = run(supply=sup, demand={K: 0}, bounds={K: (1, 5)}, surplus_since=since,
                         now=1000.0 + 121)
    assert [(a.kind, a.count, a.session_ids) for a in actions] == [("drain", 2, ("a", "b"))]


def test_surplus_clears_when_demand_returns():
    actions, since = run(supply={K: PoolSupply(running_idle=2, idle_session_ids=["a", "b"])},
                         demand={K: 2}, bounds={K: (0, 5)}, surplus_since={K: 1.0})
    assert actions == [] and since == {}


def test_draining_sessions_do_not_count_as_surplus_again():
    sup = {K: PoolSupply(running_idle=1, draining=1, idle_session_ids=["a"])}
    actions, _ = run(supply=sup, demand={K: 0}, bounds={K: (1, 5)}, surplus_since={K: 0.0},
                     now=500.0)
    assert actions == []  # current(2) - draining(1) == desired(1)


def test_project_cap_is_fair_shared_across_pools():
    sup = {K: PoolSupply(), K2: PoolSupply()}
    actions, _ = run(supply=sup, demand={K: 4, K2: 4}, bounds={K: (0, 4), K2: (0, 4)},
                     project_caps={"proj": 2}, max_starts_per_tick=10)
    starts = {a.key: a.count for a in actions if a.kind == "start"}
    assert starts == {K: 1, K2: 1}


def test_global_cap_counts_running_sessions():
    sup = {K: PoolSupply(running_busy=2), KB: PoolSupply()}
    actions, _ = run(supply=sup, demand={K: 3, KB: 3}, bounds={K: (0, 5), KB: (0, 5)},
                     global_cap=3, max_starts_per_tick=10)
    starts = {a.key: a.count for a in actions if a.kind == "start"}
    assert sum(starts.values()) == 1


def test_drains_bounded_per_tick():
    sup = {K: PoolSupply(running_idle=8, idle_session_ids=list("abcdefgh"))}
    actions, _ = run(supply=sup, demand={K: 0}, bounds={K: (0, 8)}, surplus_since={K: 0.0},
                     now=500.0, max_drains_per_tick=3)
    assert [(a.kind, a.count) for a in actions] == [("drain", 3)]
```

```python
# tests/test_pool_reconciler.py
"""_reconcile_pools / _launch_pool_session — spec §11 (fake provider)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, AgentState, Project, Task, TaskStatus, Workspace
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(AgentProfile(id="worker", name="w", lifecycle="pool",
                                               min_active=0, max_active=2, harness="claude"))
    for i in range(2):
        await database.create_workspace(Workspace(id=f"ws{i}", project_id=PROJECT_ID,
                                                  workspace_path=str(tmp_path / f"ws{i}"),
                                                  kind_id="project-repo"))
    yield database
    await database.close()


@pytest.fixture
async def orch(db, tmp_path):
    # Build the session runtime the same way tests/test_session_reconciler.py does
    # (fake provider registered under cfg.sessions.provider = "fake").
    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                    workspace_dir=str(tmp_path / "ws"), database_path=str(tmp_path / "test.db"),
                    data_dir=str(tmp_path / "data"))
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    cfg.swarm.max_starts_per_tick = 5
    o = Orchestrator(cfg)
    o.db = db
    o.git = MagicMock()
    o.bus.emit = AsyncMock()
    return o


async def ready(db, tid):
    await db.create_task(Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid,
                              status=TaskStatus.READY, profile_id="worker"))


class TestReconcilePools:
    async def test_starts_sessions_for_ready_work(self, orch, db):
        for t in ("t1", "t2", "t3"):
            await ready(db, t)
        await orch._reconcile_pools()
        pool = await db.list_sessions(lifecycle="pool", project_id=PROJECT_ID)
        assert len(pool) == 2  # max_active
        assert all(s.id.startswith("p-worker--proj--") and s.agent_id for s in pool)
        agents = await db.list_agents()
        assert sorted(a.state for a in agents) == [AgentState.IDLE, AgentState.IDLE]
        for s in pool:
            assert (await db.get_workspace_for_agent(s.agent_id)) is not None
        kinds = [c.args[0] for c in orch.bus.emit.await_args_list]
        assert kinds.count("pool.scaled") == 1

    async def test_no_starts_when_disabled(self, orch, db):
        orch.config.swarm.enabled = False
        await ready(db, "t1")
        await orch._reconcile_pools()
        assert await db.list_sessions(lifecycle="pool") == []

    async def test_starved_pool_starts_nothing_when_no_workspace(self, orch, db):
        for ws in await db.list_workspaces(PROJECT_ID):
            await db.delete_workspace(ws.id)
        await ready(db, "t1")
        await orch._reconcile_pools()
        assert await db.list_sessions(lifecycle="pool") == []
        assert await db.list_agents() == []

    async def test_quarantined_key_starts_nothing(self, orch, db):
        import time

        orch._pool_quarantine[(PROJECT_ID, "worker")] = time.time() + 60
        await ready(db, "t1")
        await orch._reconcile_pools()
        assert await db.list_sessions(lifecycle="pool") == []

    async def test_drain_marks_idle_sessions_after_grace(self, orch, db):
        await ready(db, "t1")
        await orch._reconcile_pools()
        for s in await db.list_sessions(lifecycle="pool"):
            await db.update_session(s.id, state="running")
        await db.delete_task("t1")
        orch.config.swarm.scale_down_grace = 0
        await orch._reconcile_pools()
        await orch._reconcile_pools()
        pool = await db.list_sessions(lifecycle="pool")
        assert [s.desired_state for s in pool] == ["stopped"]

    async def test_push_scheduler_ignores_pool_profile_tasks(self, orch, db):
        await ready(db, "t1")
        assert await orch._pool_profile_ids(PROJECT_ID) == {"worker"}
        assert await orch._is_session_routed(await db.get_task("t1")) is False
```

Read `tests/test_session_reconciler.py`'s orchestrator/provider fixture and reproduce its setup in `orch` (the fake provider must be registered so `_launch_pool_session` can `start`). `_is_session_routed` may be sync — match its real signature.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pool_sizing.py tests/test_pool_reconciler.py -v`
Expected: FAIL — `ImportError: cannot import name 'PoolKey'`.

- [ ] **Step 3: `size_pools`**

```python
def size_pools(*, supply, demand, bounds, project_caps, global_cap, surplus_since, now,
               scale_down_grace, max_starts_per_tick, max_drains_per_tick):
    """Desired-state pool sizing (swarm-work-model §11.1).  Pure."""
    actions: list[PoolAction] = []
    new_surplus: dict[PoolKey, float] = {}
    keys = sorted(set(supply) | set(demand) | set(bounds),
                  key=lambda k: (k.project_id, k.profile_id))
    desired: dict[PoolKey, int] = {}
    current: dict[PoolKey, int] = {}
    for key in keys:
        sup = supply.get(key, PoolSupply())
        lo, hi = bounds.get(key, (0, None))
        want = sup.running_busy + demand.get(key, 0)
        d = max(lo, want)
        if hi is not None:
            d = min(d, hi)
        d = max(d, sup.running_busy + sup.starting)
        desired[key] = d
        current[key] = sup.running_idle + sup.running_busy + sup.starting

    # --- scale up: round-robin under project caps, then the global cap -------
    starts: dict[PoolKey, int] = {k: 0 for k in keys}
    used_project = {p: sum(current[k] for k in keys if k.project_id == p)
                    for p in {k.project_id for k in keys}}
    used_global = sum(current.values())
    budget = max_starts_per_tick
    progressed = True
    while budget > 0 and progressed:
        progressed = False
        for key in keys:
            if current[key] + starts[key] >= desired[key]:
                continue
            cap = project_caps.get(key.project_id)
            if cap is not None and used_project[key.project_id] >= cap:
                continue
            if global_cap is not None and used_global >= global_cap:
                continue
            starts[key] += 1
            used_project[key.project_id] += 1
            used_global += 1
            budget -= 1
            progressed = True
            if budget == 0:
                break
    for key in keys:
        if starts[key]:
            actions.append(PoolAction(key=key, kind="start", count=starts[key]))

    # --- scale down: grace, then idle sessions oldest-first, bounded per tick ---
    drains_left = max_drains_per_tick
    for key in keys:
        sup = supply.get(key, PoolSupply())
        surplus = current[key] - sup.draining - desired[key]
        if surplus <= 0:
            continue
        since = surplus_since.get(key, now)
        new_surplus[key] = since
        if now - since < scale_down_grace or drains_left == 0:
            continue
        n = min(surplus, sup.running_idle, drains_left)
        if n <= 0:
            continue
        actions.append(PoolAction(key=key, kind="drain", count=n,
                                  session_ids=tuple(sup.idle_session_ids[:n])))
        drains_left -= n
    return actions, new_surplus
```

- [ ] **Step 4: `PoolsMixin`**

```python
# src/orchestrator/pools.py
"""Worker pools — sizing and convergence (swarm-work-model §11).

One cascade step per tick: measure supply and demand per (project, profile),
ask ``size_pools`` what to do, then start or drain.  The step reads one
``count_ready_by_profile`` and one session list per active project.
"""

from __future__ import annotations

import logging
import time
import uuid

from src.commands.claim_commands import remove_claim_file
from src.models import Agent, AgentState, SessionRecord, TaskStatus
from src.scheduler import PoolKey, PoolSupply, size_pools
from src.sessions.spec import build_pool_spec, pool_session_name

logger = logging.getLogger(__name__)

_LIVE = ("running", "stalled")


class PoolsMixin:
    async def _pool_profiles(self, project_id: str) -> dict:
        out = {}
        for p in await self.db.list_profiles():
            if getattr(p, "lifecycle", "task") == "pool":
                out[p.id] = p
        for p in await self.db.list_project_profiles(project_id):
            if getattr(p, "lifecycle", "task") == "pool":
                out[p.id] = p
            else:
                out.pop(p.id, None)
        return out

    async def _pool_profile_ids(self, project_id: str) -> set[str]:
        return set(await self._pool_profiles(project_id))

    async def _measure_pools(self, project_ids=None):
        supply, demand, bounds, profiles_by_key, project_caps, projects = {}, {}, {}, {}, {}, {}
        for project in await self.db.list_projects():
            if project.status.value != "ACTIVE":
                continue
            if project_ids and project.id not in project_ids:
                continue
            pool_profiles = await self._pool_profiles(project.id)
            if not pool_profiles:
                continue
            projects[project.id] = project
            project_caps[project.id] = getattr(project, "max_concurrent_agents", None)
            ready = await self.db.count_ready_by_profile(project.id)
            default = getattr(project, "default_profile_id", None)
            sessions = await self.db.list_sessions(lifecycle="pool", project_id=project.id)
            for prof_id, prof in pool_profiles.items():
                key = PoolKey(project.id, prof_id)
                profiles_by_key[key] = prof
                bounds[key] = (prof.min_active or 0, prof.max_active)
                demand[key] = ready.get(prof_id, 0) + (ready.get(None, 0) if default == prof_id else 0)
                sup = PoolSupply()
                for s in sorted((s for s in sessions if s.profile_id == prof_id),
                                key=lambda s: s.started_at or 0):
                    if s.state == "starting":
                        sup.starting += 1
                    elif s.state not in _LIVE:
                        continue
                    elif s.desired_state == "stopped":
                        sup.draining += 1
                    elif s.task_id or s.claim_phase:
                        sup.running_busy += 1
                    else:
                        sup.running_idle += 1
                        sup.idle_session_ids.append(s.id)
                supply[key] = sup
        return supply, demand, bounds, profiles_by_key, project_caps, projects

    async def _reconcile_pools(self) -> None:
        if not (self.config.swarm.enabled and self.config.sessions.enabled):
            return
        supply, demand, bounds, profiles_by_key, project_caps, projects = await self._measure_pools()
        now = time.time()
        actions, self._pool_surplus_since = size_pools(
            supply=supply, demand=demand, bounds=bounds, project_caps=project_caps,
            global_cap=getattr(self.config, "max_concurrent_agents", None),
            surplus_since=self._pool_surplus_since, now=now,
            scale_down_grace=self.config.swarm.scale_down_grace,
            max_starts_per_tick=self.config.swarm.max_starts_per_tick,
            max_drains_per_tick=self.config.swarm.max_drains_per_tick)
        for action in actions:
            count = 0
            if action.kind == "start":
                until = self._pool_quarantine.get((action.key.project_id, action.key.profile_id))
                if until and until > now:
                    continue
                for _ in range(action.count):
                    sid = await self._launch_pool_session(projects[action.key.project_id],
                                                          profiles_by_key[action.key])
                    if sid is None:
                        break
                    count += 1
            else:
                for sid in action.session_ids:
                    await self.db.update_session(sid, desired_state="stopped")
                    count += 1
            if count:
                await self.bus.emit("pool.scaled", {"project_id": action.key.project_id,
                                                    "profile_id": action.key.profile_id,
                                                    "kind": action.kind, "count": count})
```

`_launch_pool_session(project, profile)` follows `_launch_session_for_task` (`execution.py:1776-1967`) step for step — read it first and copy its harness/provider/token/error handling. Differences: create the agent row first (`Agent(id=f"agent-{uuid.uuid4().hex[:12]}", name=f"{profile.id}-{uuid.uuid4().hex[:4]}", profile_id=profile.id, state=AgentState.IDLE, created_at=time.time())`, `db.create_agent`); acquire the workspace for the agent (`acquire_one_unlocked(project.id, "project-repo", mode=<the mode the task path resolves for project-repo>, locked_by_task_id=None, locked_by_agent_id=agent.id, prefer_workspace_id=None, kind_mode=..., worktree_slot_cap=...)`; `None` → `delete_agent`, `logger.info("pool %s/%s starved: no free workspace", ...)`, return `None`); `session_id = pool_session_name(profile.id, project.id, uuid.uuid4().hex[:8])`; token minted with `task_id=None`; `spec = build_pool_spec(...)`; `provider.start(spec)`; `create_session(SessionRecord(id=session_id, name=session_id, lifecycle="pool", agent_id=agent.id, task_id=None, state="starting", ...))`. On any failure after the agent row exists: `release_workspaces_for_agent(agent.id)`, `delete_agent(agent.id)`, revoke the token if minted, return `None`.

`_terminate_pool_session(session, *, reason, task_status=TaskStatus.READY)`: `await self.db.terminate_pool_session(session.id, reason=reason, task_status=task_status)`; `self.token_store.revoke_session(session.id)` if `self.token_store`; `remove_claim_file(session.work_dir)`; provider stop in `try/except Exception: logger.warning(...)`; `update_session(session.id, state="stopped")` unless already terminal (reuse `SessionReconciler._stop_session` if it is importable without a cycle).

`spec.py`: `pool_session_name`, `POOL_BOOTSTRAP_PROMPT`, `build_pool_spec` — copy `build_named_spec` (`:227-242`) and change lifecycle/bootstrap/extra_env. Confirm the `GIT_*` names survive `env_scrub` (`STRIP_ALWAYS` at `src/env_scrub.py:168`); allow-list them if not.

`reconciler.py:212`: `("s-", "n-")` → `("s-", "n-", "p-")`.

`core.py`: `class Orchestrator(PoolsMixin, ...)`; the two `__init__` dicts; `run_one_cycle`: after `await self._schedule(...)` (line 2468) add `await self._reconcile_pools()`; in `_schedule`, per project compute `pool_ids = await self._pool_profile_ids(project_id)` and filter `tasks = [t for t in tasks if (t.profile_id or default_profile_id) not in pool_ids]` before `SchedulerState`; `_is_session_routed` returns `False` when the resolved profile id is in the project's pool ids; `AgentReconciler.reconcile` skips profiles with `lifecycle == "pool"`; the startup reaper deletes `RETIRED` agents unconditionally and pool-profile agents with no session row (`list_sessions(agent_id=...)` empty), releasing their workspaces first.

Register `pool.scaled` in `event_schemas.py` (`_SWARM_SCHEMAS`, Task 4).

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_pool_sizing.py tests/test_pool_reconciler.py tests/test_scheduler.py tests/test_agent_reconciler.py tests/test_orchestrator.py tests/test_session_reconciler.py tests/test_sessions_spec.py tests/test_env_scrub.py -v -n auto`, then the full suite.

- [ ] **Step 6: Commit**

```bash
git add src/scheduler.py src/orchestrator/pools.py src/orchestrator/core.py src/orchestrator/execution.py src/orchestrator/agent_reconciler.py src/sessions/spec.py src/sessions/env.py src/sessions/reconciler.py src/env_scrub.py src/event_schemas.py tests/test_pool_sizing.py tests/test_pool_reconciler.py
git commit -m "feat(pools): desired-state pool sizing, _reconcile_pools cascade step, pool session launch

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Session reconciler carve-outs for pool sessions

**Files:**
- Modify: `src/sessions/reconciler.py:173-192` (`tick` — add `_step_prepare_timeout` after `_step_drain_ack`), `:350` (`_step_drain_ack`), `:417-540` (`_step_exits` / `_apply_verdict`), `:544` (`_step_stall_ladder`), `:651` (`_step_orphans`), `:915` (`_step_backstop`), `:983-1012` (`_release_task`)
- Modify: `src/event_schemas.py` (`session.claim_timeout {session_id; optional task_id}`)
- Test: `tests/test_pool_reconciler_carveouts.py`

**Interfaces:**
- Consumes: `orchestrator._terminate_pool_session(session, *, reason, task_status=READY)` and `orchestrator._pool_quarantine` (Task 5), `db.release_claim(...)` (Task 3), `swarm.prepare_timeout` (Task 1), `db.list_sessions(lifecycle="pool", claim_phase=...)` (Task 1), `orchestrator._resolve_claim_waiters` (Task 4), `classify_exit(session, task, last_peek, *, now, rapid_crash_window, rate_limit_cooldown)`.
- Produces:
  - `SessionReconciler._step_prepare_timeout()` — pool sessions with `claim_phase in ("claiming", "preparing")` and `claim_phase_at < now - swarm.prepare_timeout`: with a task → `release_claim(sid, task_status=READY, context="prepare_timeout", now=now, result="prepare_failed", needs_attention="prepare_timeout")`; without → `update_session(claim_phase=None, claim_phase_at=None)`; resolves the session's claim waiters with `"prepare_failed"`; emits `session.claim_timeout`.
  - Pool verdicts (`_apply_pool_verdict(session, verdict, task)`): `RAPID_CRASH` → held task gets `needs_attention=rapid_crash`, `_terminate_pool_session(reason="rapid_crash")`, `orchestrator._pool_quarantine[(project_id, profile_id)] = now + rapid_crash_window`; `RATE_LIMIT` → terminate + the existing provider cooldown write; every other verdict → terminate with `task_status=READY`, and when a task was held `needs_attention=exited_holding_task`.
  - Stall ladder: pool sessions are eligible only while `claim_phase == "active"`; the restart rung becomes `_terminate_pool_session(reason="stalled")` (no in-place restart for pool sessions — the pool step starts a fresh one).
  - Orphans: pool → `_terminate_pool_session(reason="orphaned")`. Backstop: pool sessions with `task_id is None` are never stale. Drain-ack: pool sessions with `desired_state == "stopped"` and `task_id is None` → `_terminate_pool_session(reason="drained")`. `_release_task`: pool → `_terminate_pool_session(reason=<reason>)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pool_reconciler_carveouts.py
"""Session reconciler — pool lifecycle carve-outs (spec §10.4, §11.2, §11.4)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus, Workspace

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    from src.database import Database

    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


async def held_pool_session(db, sid="s1", agent_id="agent-1", phase="active", phase_at=None):
    await db.create_agent(Agent(id=agent_id, name=agent_id, profile_id="worker",
                                state=AgentState.BUSY, current_task_id="t1"))
    await db.create_workspace(Workspace(id=f"ws-{agent_id}", project_id=PROJECT_ID,
                                        workspace_path=f"/wd/{agent_id}", kind_id="project-repo",
                                        locked_by_agent_id=agent_id, locked_by_task_id="t1"))
    await db.create_task(Task(id="t1", project_id=PROJECT_ID, title="t1", description="t1",
                              status=TaskStatus.IN_PROGRESS, assigned_agent_id=agent_id,
                              claim_epoch=1, profile_id="worker"))
    await db.create_session(SessionRecord(
        id=sid, project_id=PROJECT_ID, profile_id="worker", harness="claude", provider="fake",
        name=sid, lifecycle="pool", work_dir=f"/wd/{agent_id}", epoch="e", instance_token="t",
        started_at=time.time() - 600, state="running", agent_id=agent_id, task_id="t1",
        claim_phase=phase, claim_phase_at=phase_at if phase_at is not None else time.time()))
    return sid


class TestPrepareTimeout:
    async def test_stuck_preparing_is_released(self, db, reconciler):
        sid = await held_pool_session(db, phase="preparing", phase_at=time.time() - 1000)
        await reconciler._step_prepare_timeout()
        s = await db.get_session(sid)
        assert (s.task_id, s.claim_phase, s.last_claim_result) == (None, None, "prepare_failed")
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        assert await db.get_task_meta("t1", "needs_attention") == "prepare_timeout"

    async def test_fresh_preparing_is_left_alone(self, db, reconciler):
        sid = await held_pool_session(db, phase="preparing")
        await reconciler._step_prepare_timeout()
        assert (await db.get_session(sid)).claim_phase == "preparing"


class TestExits:
    async def test_pool_exit_holding_task_returns_task_and_retires_agent(self, db, reconciler,
                                                                         provider):
        sid = await held_pool_session(db)
        provider.peek = AsyncMock(return_value=MagicMock(alive=False, exit_code=0))
        await reconciler._step_exits()
        t = await db.get_task("t1")
        assert (t.status, t.assigned_agent_id) == (TaskStatus.READY, None)
        assert await db.get_task_meta("t1", "needs_attention") == "exited_holding_task"
        assert (await db.get_agent("agent-1")).state == AgentState.RETIRED
        assert await db.get_workspace_for_agent("agent-1") is None
        assert (await db.get_session(sid)).state == "stopped"

    async def test_rapid_crash_quarantines_pool_key(self, db, reconciler, provider, orch):
        sid = await held_pool_session(db)
        await db.update_session(sid, started_at=time.time() - 1)
        provider.peek = AsyncMock(return_value=MagicMock(alive=False, exit_code=1))
        await reconciler._step_exits()
        assert orch._pool_quarantine[(PROJECT_ID, "worker")] > time.time()
        assert await db.get_task_meta("t1", "needs_attention") == "rapid_crash"

    async def test_idle_pool_drain_ack_stops_session(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=time.time())
        await db.update_session(sid, desired_state="stopped")
        await reconciler._step_drain_ack()
        assert (await db.get_session(sid)).state == "stopped"
        assert (await db.get_agent("agent-1")).state == AgentState.RETIRED

    async def test_idle_pool_session_is_not_stale_for_backstop(self, db, reconciler):
        sid = await held_pool_session(db)
        await db.release_claim(sid, task_status=TaskStatus.READY, context="x", now=time.time())
        await db.update_session(sid, last_activity_at=time.time() - 10_000)
        await reconciler._step_backstop()
        assert (await db.get_session(sid)).state == "running"
```

Provide `orch`, `provider`, `reconciler` fixtures by copying the ones in `tests/test_session_reconciler.py` (same constructor call, same fake provider); the `orch` here must have `swarm.enabled = True` and `sessions.provider = "fake"`. Check the exact `exit_code`/`alive` attribute names on the peek result the classifier reads.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pool_reconciler_carveouts.py -v`
Expected: FAIL — `_step_prepare_timeout` missing; pool exits go down the task-session path.

- [ ] **Step 3: Implement**

```python
    async def _step_prepare_timeout(self) -> None:
        """Release claims stuck in claiming/preparing (swarm-work-model §10.4)."""
        timeout = self.config.swarm.prepare_timeout
        now = time.time()
        for phase in ("claiming", "preparing"):
            for s in await self.db.list_sessions(lifecycle="pool", claim_phase=phase):
                if (s.claim_phase_at or now) > now - timeout:
                    continue
                if s.task_id:
                    await self.db.release_claim(
                        s.id, task_status=TaskStatus.READY, context="prepare_timeout", now=now,
                        result="prepare_failed", needs_attention="prepare_timeout")
                else:
                    await self.db.update_session(s.id, claim_phase=None, claim_phase_at=None)
                for key in [k for k in self.orchestrator.claim_waiters if k[0] == s.id]:
                    self.orchestrator._resolve_claim_waiters(key[0], key[1], "prepare_failed")
                await self.orchestrator.bus.emit(
                    "session.claim_timeout", {"session_id": s.id, "task_id": s.task_id})
```

Carve-outs at the lifecycle checks the line references point at:
- `_step_drain_ack`: include pool sessions; `desired_state == "stopped" and task_id is None` → `await self.orchestrator._terminate_pool_session(s, reason="drained")`.
- `_apply_verdict`: first line `if session.lifecycle == "pool": return await self._apply_pool_verdict(session, verdict, task)`:

```python
    async def _apply_pool_verdict(self, session, verdict, task) -> None:
        now = time.time()
        orch = self.orchestrator
        if task is not None:
            note = {"RAPID_CRASH": "rapid_crash"}.get(verdict.name, "exited_holding_task")
            await self.db.set_task_meta(task.id, "needs_attention", note)
        if verdict.name == "RAPID_CRASH":
            orch._pool_quarantine[(session.project_id, session.profile_id)] = (
                now + self.config.sessions.rapid_crash_window)
        elif verdict.name == "RATE_LIMIT":
            self._apply_rate_limit_cooldown(session)  # the existing cooldown write
        await orch._terminate_pool_session(session, reason=verdict.name.lower())
```

  (Use the real verdict enum member names from `exit_classifier.py` and the real cooldown helper; `set_task_meta` = whatever public wrapper `_upsert_meta` has — add one if none exists.)
- `_step_stall_ladder`: `if s.lifecycle == "pool" and s.claim_phase != "active": continue`; at the restart rung `if s.lifecycle == "pool": await self.orchestrator._terminate_pool_session(s, reason="stalled"); continue`.
- `_step_orphans`: pool → `_terminate_pool_session(s, reason="orphaned")`.
- `_step_backstop`: `if s.lifecycle == "pool" and s.task_id is None: continue`.
- `_release_task`: `if session.lifecycle == "pool": await self.orchestrator._terminate_pool_session(session, reason=reason); return`.
- `tick()`: `await self._step_prepare_timeout()` after `_step_drain_ack`.

Register `session.claim_timeout` in `_SESSION_SCHEMAS`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_pool_reconciler_carveouts.py tests/test_session_reconciler.py tests/test_pool_reconciler.py tests/test_exit_classifier.py -v -n auto`, then the full suite.

- [ ] **Step 5: Commit**

```bash
git add src/sessions/reconciler.py src/event_schemas.py src/database/queries/hierarchy_queries.py tests/test_pool_reconciler_carveouts.py
git commit -m "feat(pools): reconciler carve-outs — prepare timeout, pool exits/orphans/stalls terminate and return work

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Worker-filed work — constraints, quota, in-transaction routing gate, pipeline `equals`/`is_null`, default triage rule

**Files:**
- Modify: `src/commands/task_commands.py:878-1305` (`_cmd_create_task`)
- Modify: `src/database/queries/gate_queries.py` (`create_gate(..., conn=None)`)
- Modify: `src/database/queries/task_queries.py` / `dependency_queries.py` (`create_task(task, *, conn=None)`, `add_dependency(..., conn=None)` if they do not accept one yet — Plan 1's `set_parent(..., conn=)` already does)
- Modify: `src/orchestrator/core.py:123-191` (`_eval_pipeline_when` — `equals`, `is_null`), `src/playbooks/pipeline_compiler.py:380-417` (`_validate_when`)
- Modify: `src/prompts/default_playbooks/default-pipeline.md` (rule `worker-filed-triage`)
- Modify: `src/event_schemas.py:68-71` (`task.created` optional += `created_by_kind`, `created_by_id`, `filed_by_profile_id`, `discovered_from`, `parent_task_id`)
- Test: `tests/test_worker_filing.py`, `tests/test_pipeline_when_comparators.py`

**Interfaces:**
- Consumes: `reserve_filing(conn, task_id, *, max_filings)` (Task 3), `swarm.max_filings_per_task` (Task 1), `immediate()`, `subtree_ids(task_id)`, `set_parent(task_id, parent_id, *, conn)` and `child_task_id(conn, parent)` (Plan 1), `create_task` in `AGENT_COMMAND_SET` (Task 4).
- Produces:
  - `_cmd_create_task` under a session scope (`scope["kind"] == "session"` and not `elevated`):
    - `project_id` forced to the session's project; a different explicit `project_id` → `{"success": False, "error": "worker-filed tasks are pinned to the session's project"}`.
    - the session must hold a task; else `{"success": False, "code": "idle_session_cannot_file", "error": ...}`.
    - `status` argument ignored → `DEFINED`.
    - relation: `parent_id` given (must be the held task or in its subtree) → parent-child; else `discovered-from` edge `new → held` (explicit `discovered_from` must be the held task or in its subtree).
    - `created_by_kind = "session"`, `created_by_id = session_id`.
    - inside the creation transaction, first `reserve_filing(conn, held_id, max_filings=swarm.max_filings_per_task)`; `False` → `{"success": False, "code": "filing_quota_exceeded", ...}` with nothing written.
    - root-level (no `parent_id`): routing gate in the same transaction — `create_gate(project_id, "routing", f"Route: {title}", waiter_task_ids=[new_id], conn=conn)`; response carries `"gate_id"`.
    - post-commit `task.created` carries `created_by_kind`, `created_by_id`, `filed_by_profile_id`, `discovered_from`, `parent_task_id` (all present; `None` when not applicable).
  - `create_gate(self, project_id, gate_type, title, *, question=None, await_id=None, timeout_at=None, waiter_task_ids=(), conn=None) -> tuple[str, bool]`.
  - Pipeline `when` leaf comparators: `{"field": ..., "equals": <value>}`, `{"field": ..., "is_null": true|false}`; `_validate_when` accepts exactly one of `truthy | not_null | equals | is_null` per leaf.
  - Default pipeline rule (appended to `rules`):
    ```json
    {"id": "worker-filed-triage", "on": "task.created",
     "when": {"all": [{"field": "event.created_by_kind", "equals": "session"},
                      {"field": "event.parent_task_id", "is_null": true}]},
     "entry": "route",
     "nodes": {"route": {"command": "task_route",
                         "args": {"task_id": "{{event.task_id}}",
                                  "profile_id": "{{event.filed_by_profile_id}}"}}}}
    ```
    Routes the filed task to the filer's own profile (resolving the routing gate); projects override the rule to triage differently.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_worker_filing.py
"""Worker-filed work — spec §12 constraints."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_project(Project(id="other", name="o"))
    yield database
    await database.close()


@pytest.fixture
async def handler(db, tmp_path):
    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                    workspace_dir=str(tmp_path / "ws"), database_path=str(tmp_path / "test.db"),
                    data_dir=str(tmp_path / "data"))
    cfg.swarm.enabled = True
    cfg.swarm.max_filings_per_task = 2
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = MagicMock()
    orch.bus.emit = AsyncMock()
    return CommandHandler(orch, cfg)


async def holding_session(db, sid="s1", task_id="held"):
    await db.create_agent(Agent(id="agent-1", name="a", profile_id="worker", state=AgentState.BUSY))
    await db.create_task(Task(id=task_id, project_id=PROJECT_ID, title=task_id, description="x",
                              status=TaskStatus.IN_PROGRESS, assigned_agent_id="agent-1",
                              claim_epoch=1))
    await db.create_session(SessionRecord(
        id=sid, project_id=PROJECT_ID, profile_id="worker", harness="claude", provider="fake",
        name=sid, lifecycle="pool", work_dir="/wd", epoch="e", instance_token="t",
        started_at=time.time(), state="running", agent_id="agent-1", task_id=task_id,
        claim_phase="active"))
    return sid


def scoped(handler, sid):
    handler._current_scope = {"kind": "session", "session_id": sid, "task_id": None,
                              "project_id": PROJECT_ID, "elevated": False}
    return handler


def created_events(handler):
    return [c.args[1] for c in handler.orchestrator.bus.emit.await_args_list
            if c.args[0] == "task.created"]


class TestFiling:
    async def test_root_filing_gets_discovered_from_and_routing_gate(self, handler, db):
        sid = await holding_session(db)
        res = await scoped(handler, sid)._cmd_create_task({"title": "found a bug",
                                                            "description": "d",
                                                            "status": "READY"})
        assert res["success"] is True and res["gate_id"]
        new = await db.get_task(res["task_id"])
        assert (new.status, new.created_by_kind, new.created_by_id, new.project_id) == (
            TaskStatus.DEFINED, "session", sid, PROJECT_ID)
        deps = await db.get_dependencies(new.id)
        assert [(d.depends_on_id, d.edge_type) for d in deps] == [("held", "discovered-from")]
        gates = await db.list_gates(PROJECT_ID, waiter_task_id=new.id)
        assert [g.gate_type for g in gates] == ["routing"]
        assert (await db.get_task("held")).filed_count == 1
        ev = created_events(handler)[0]
        assert (ev["created_by_kind"], ev["filed_by_profile_id"], ev["discovered_from"],
                ev["parent_task_id"]) == ("session", "worker", "held", None)

    async def test_child_filing_under_held_task_has_no_gate(self, handler, db):
        sid = await holding_session(db)
        res = await scoped(handler, sid)._cmd_create_task({"title": "sub", "description": "d",
                                                            "parent_id": "held"})
        assert res["success"] is True and res.get("gate_id") is None
        new = await db.get_task(res["task_id"])
        assert new.parent_task_id == "held" and new.id.startswith("held.")

    async def test_project_pin(self, handler, db):
        sid = await holding_session(db)
        res = await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d",
                                                            "project_id": "other"})
        assert res["success"] is False and "pinned" in res["error"]

    async def test_idle_session_cannot_file(self, handler, db):
        sid = await holding_session(db)
        await db.update_session(sid, task_id=None, claim_phase=None)
        res = await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d"})
        assert res["success"] is False and res["code"] == "idle_session_cannot_file"

    async def test_parent_outside_subtree_rejected(self, handler, db):
        sid = await holding_session(db)
        await db.create_task(Task(id="elsewhere", project_id=PROJECT_ID, title="e",
                                  description="e", status=TaskStatus.READY))
        res = await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d",
                                                            "parent_id": "elsewhere"})
        assert res["success"] is False

    async def test_quota_is_enforced_atomically(self, handler, db):
        sid = await holding_session(db)
        h = scoped(handler, sid)
        assert (await h._cmd_create_task({"title": "a", "description": "d"}))["success"]
        assert (await h._cmd_create_task({"title": "b", "description": "d"}))["success"]
        res = await h._cmd_create_task({"title": "c", "description": "d"})
        assert res["success"] is False and res["code"] == "filing_quota_exceeded"
        assert len(await db.list_tasks(PROJECT_ID)) == 3  # held + a + b

    async def test_gate_failure_rolls_back_task(self, handler, db, monkeypatch):
        sid = await holding_session(db)

        async def boom(*a, **k):
            raise RuntimeError("gate write failed")

        monkeypatch.setattr(db, "create_gate", boom)
        with pytest.raises(RuntimeError):
            await scoped(handler, sid)._cmd_create_task({"title": "x", "description": "d"})
        assert len(await db.list_tasks(PROJECT_ID)) == 1
        assert (await db.get_task("held")).filed_count == 0

    async def test_elevated_caller_is_unconstrained(self, handler, db):
        handler._current_scope = {"kind": "local", "elevated": True}
        res = await handler._cmd_create_task({"title": "x", "description": "d",
                                              "project_id": PROJECT_ID, "status": "READY"})
        assert res["success"] and (await db.get_task(res["task_id"])).status == TaskStatus.READY
```

```python
# tests/test_pipeline_when_comparators.py
"""Pipeline `when` — equals / is_null (controller ruling 2)."""

from __future__ import annotations

import pytest

from src.orchestrator.core import _eval_pipeline_when
from src.playbooks.pipeline_compiler import _validate_when


def ev(**kw):
    return {"event": kw}


def test_equals_and_is_null():
    when = {"all": [{"field": "event.created_by_kind", "equals": "session"},
                    {"field": "event.parent_task_id", "is_null": True}]}
    assert _eval_pipeline_when(when, ev(created_by_kind="session", parent_task_id=None))
    assert not _eval_pipeline_when(when, ev(created_by_kind="human", parent_task_id=None))
    assert not _eval_pipeline_when(when, ev(created_by_kind="session", parent_task_id="p"))


def test_is_null_false():
    assert _eval_pipeline_when({"field": "event.x", "is_null": False}, ev(x=1))
    assert not _eval_pipeline_when({"field": "event.x", "is_null": False}, ev(x=None))


def test_validator_accepts_new_and_rejects_two_comparators():
    _validate_when({"field": "event.x", "equals": "y"}, "rule")
    _validate_when({"field": "event.x", "is_null": True}, "rule")
    with pytest.raises(Exception):
        _validate_when({"field": "event.x", "equals": "y", "truthy": True}, "rule")


def test_default_pipeline_triage_rule_present():
    import json
    import re
    from pathlib import Path

    text = Path("src/prompts/default_playbooks/default-pipeline.md").read_text(encoding="utf-8")
    block = re.search(r"```json\n(.*?)\n```", text, re.S).group(1)
    rules = {r["id"]: r for r in json.loads(block)["rules"]}
    rule = rules["worker-filed-triage"]
    assert rule["on"] == "task.created"
    assert rule["nodes"]["route"]["command"] == "task_route"
```

Read `_eval_pipeline_when` / `_validate_when`'s real signatures (second-argument name and whether the validator raises or returns errors) and adjust the two tests; if the default pipeline is loaded through a helper, use it instead of the regex.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_worker_filing.py tests/test_pipeline_when_comparators.py -v`
Expected: FAIL — no gate, no `discovered-from` edge, `equals` evaluates to True for everything.

- [ ] **Step 3: Implement**

`gate_queries.create_gate`: add `conn=None`; factor the body into `_create_gate_on(conn, ...)`; without `conn` wrap it in `self._engine.begin()` as today.

`_cmd_create_task` — after arg parsing:

```python
        scope = self._current_scope or {}
        filing_session = None
        held_id = None
        if scope.get("kind") == "session" and not scope.get("elevated"):
            filing_session = await self.db.get_session(scope.get("session_id") or "")
            if filing_session is None:
                return {"success": False, "error": "no session in scope"}
            if args.get("project_id") and args["project_id"] != filing_session.project_id:
                return {"success": False,
                        "error": "worker-filed tasks are pinned to the session's project"}
            args["project_id"] = filing_session.project_id
            if not filing_session.task_id:
                return {"success": False, "code": "idle_session_cannot_file",
                        "error": "idle sessions cannot file work; claim a task first"}
            args.pop("status", None)  # worker-filed work always starts DEFINED
            held_id = filing_session.task_id
            allowed = {held_id} | set(await self.db.subtree_ids(held_id))
            if args.get("discovered_from") and args["discovered_from"] not in allowed:
                return {"success": False,
                        "error": "discovered_from must be the held task or one of its descendants"}
            if args.get("parent_id") and args["parent_id"] not in allowed:
                return {"success": False,
                        "error": "parent must be the held task or one of its descendants"}
```

Where the row and its edges are written today (`Task(...)` at ~1110, `create_task` ~1130, parent ~1131-1142, `depends_on` edges 1150-1170), route the worker-filed case through one `immediate()` connection:

```python
class _FilingQuota(Exception):
    pass

        gate_id = None
        if filing_session is not None:
            task.created_by_kind = "session"
            task.created_by_id = filing_session.id
            try:
                async with self.db.immediate() as conn:
                    if not await self.db.reserve_filing(
                        conn, held_id, max_filings=self.config.swarm.max_filings_per_task
                    ):
                        raise _FilingQuota()
                    if parent_id:
                        task.id, _capped = await child_task_id(conn, parent_id)
                    await self.db.create_task(task, conn=conn)
                    if parent_id:
                        await self.db.set_parent(task.id, parent_id, conn=conn)
                    else:
                        origin = args.get("discovered_from") or held_id
                        await self.db.add_dependency(task.id, origin, "discovered-from", conn=conn)
                        gate_id, _ = await self.db.create_gate(
                            task.project_id, "routing", f"Route: {task.title}",
                            waiter_task_ids=[task.id], conn=conn)
                    for dep_id in depends_on_ids:
                        await self.db.add_dependency(task.id, dep_id, "blocks", conn=conn)
            except _FilingQuota:
                return {"success": False, "code": "filing_quota_exceeded",
                        "error": f"task {held_id} has already filed "
                                 f"{self.config.swarm.max_filings_per_task} tasks "
                                 f"(swarm.max_filings_per_task)"}
        else:
            ...  # existing path unchanged
```

`recompute_blocked` for the new task must run on the same connection (`add_dependency(conn=)` does it when it accepts a conn; if `create_task` does not compute `is_blocked`, call `recompute_blocked({task.id}, conn=conn)` after the edges). The `task.created` emit adds `created_by_kind=task.created_by_kind, created_by_id=task.created_by_id, filed_by_profile_id=(filing_session.profile_id if filing_session else None), discovered_from=(origin if filing_session and not parent_id else None), parent_task_id=task.parent_task_id`; add the five keys to the schema's `optional` list. Response adds `"gate_id": gate_id` and `"status": task.status.value`.

`_eval_pipeline_when` leaf branch:

```python
            if "equals" in cond:
                return value == cond["equals"]
            if "is_null" in cond:
                return (value is None) == bool(cond["is_null"])
```

`_validate_when`: comparator set `{"truthy", "not_null", "equals", "is_null"}`, exactly one per leaf, `is_null` must be a bool. `default-pipeline.md`: append the rule; if compiled pipelines are cached, bump the marker that forces a recompile (grep `default-pipeline` under `src/playbooks/`).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_worker_filing.py tests/test_pipeline_when_comparators.py tests/test_task_commands.py tests/test_gate_commands.py tests/test_gate_queries.py tests/test_pipeline_compiler.py tests/test_default_pipeline.py tests/test_hierarchy_commands.py tests/test_event_schema_registry_validation.py -v -n auto`, then the full suite.

- [ ] **Step 5: Commit**

```bash
git add src/commands/task_commands.py src/database/queries/gate_queries.py src/database/queries/task_queries.py src/database/queries/dependency_queries.py src/orchestrator/core.py src/playbooks/pipeline_compiler.py src/prompts/default_playbooks/default-pipeline.md src/event_schemas.py tests/test_worker_filing.py tests/test_pipeline_when_comparators.py
git commit -m "feat(swarm): worker-filed work — project pin, discovered-from, atomic quota, routing gate in the creation transaction, pipeline equals/is_null

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Surface — tool definitions, API models, hand-crafted CLI, `pool status|scale`, `aq schema` enums, skill + prime text

**Files:**
- Modify: `src/tools/definitions.py` (`task_claim` new; `task_close` += `claim_epoch`, `claim_next`, `wait`; `task_heartbeat`/`task_set`/`task_handoff` += `claim_epoch`; `create_task` += `depends_on`, `discovered_from`, `dedup_key` (and `parent_id` if missing); `pool_status`, `pool_scale` new; `_CLI_CATEGORY_OVERRIDES` → `pool` group)
- Modify: `src/api/models/task.py` (`TaskClaimResponse`, `PoolStatusResponse`, `PoolScaleResponse` in `RESPONSE_MODELS`)
- Modify: `src/commands/ops_commands.py` (`_cmd_pool_status`, `_cmd_pool_scale`)
- Modify: `src/cli/agent_surface.py` (`read_claim_epoch`; hand-crafted `aq task claim|close|heartbeat|set` and `aq handoff --claim-epoch`), `src/cli/auto_commands.py` (skip names in `HAND_CRAFTED`), `src/cli/client.py` (`_COMMAND_TIMEOUTS["task_claim"] = 180.0`, `["task_close"] = 180.0`), `src/cli/formatter_registry.py` (formatters for `task_claim`, `pool_status`)
- Modify: `src/commands/surface_commands.py:39-74` (`_cmd_get_schema` `enums`)
- Modify: `src/skills/aq-tasks/SKILL.md`, `src/prime/templates/tool_guidance.md`, `src/prime/templates/completion_protocol.md`
- Test: `tests/test_swarm_surface.py`

**Interfaces:**
- Consumes: `_cmd_task_claim` (Task 4), `_measure_pools` / `_terminate_pool_session` / `_pool_quarantine` (Task 5), `ClaimResult`/`CLAIM_PHASES` (Task 1), `VALID_LIFECYCLES`, `VALID_OUTCOMES`, `AgentState`.
- Produces:
  - Tool definitions: `task_claim` (category `tasks`; `task_id?: string`, `next?: boolean`, `wait?: integer` 0..3600); `task_close` += `claim_epoch?: integer`, `claim_next?: boolean`, `wait?: integer`; `task_heartbeat`/`task_set`/`task_handoff` += `claim_epoch?: integer`; `create_task` += `depends_on?: string[]`, `discovered_from?: string`, `dedup_key?: string`; `pool_status` (`project_id?`) and `pool_scale` (`project_id`, `profile_id`, `min?: integer`, `max?: integer`, `now?: boolean`) in category `ops`, CLI group `pool`.
  - `_cmd_pool_status(args) -> {"success": True, "pools": [{"project_id", "profile_id", "min_active", "max_active", "desired", "running_idle", "running_busy", "starting", "draining", "ready", "quarantined_until"?}]}`.
  - `_cmd_pool_scale(args) -> {"success": True, "profile_id", "project_id", "min_active", "max_active", "terminated": [session ids]}` — validates `min >= 0`, `max >= 1`, `min <= max`; writes the bounds on the project-scoped profile through the same writer `aq profile set` uses (if that writer targets the vault file, the vault watcher re-syncs the row; if no such writer exists, update the `agent_profiles` row and ledger that vault sync will overwrite it); `now=True` terminates idle sessions above the new max oldest-first via `_terminate_pool_session(reason="scaled")`.
  - CLI: `aq task claim [TASK_ID] [--next] [--wait N]`; `aq task close TASK_ID --outcome pass|fail --summary S [--claim-next] [--wait N] [--claim-epoch N]`; `aq task heartbeat TASK_ID [--claim-epoch N]`; `aq task set TASK_ID ... [--claim-epoch N]`; `aq handoff ... [--claim-epoch N]`; `aq pool status [--project P]`; `aq pool scale PROFILE --project P [--min N] [--max N] [--now]`. `read_claim_epoch(cwd: str | None = None) -> int | None` reads `<cwd>/.aq/claim.json` then `$AQ_CLAIM_EPOCH`; the commands send `claim_epoch` only when it resolves.
  - `_cmd_get_schema` adds `"enums": {"claim_result": [...], "claim_phase": [...], "lifecycle": ["task", "named", "pool"], "session_state": [...], "agent_state": [...], "outcome": ["pass", "fail"]}` (merged with any existing enums).
  - SKILL.md and the prime templates describe the worker loop, the result codes, the claim file, and `--outcome pass|fail`; the stale `success|needs_context|failure` text is removed everywhere under `src/skills` and `src/prime`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_swarm_surface.py
"""Generated surface for the swarm commands — spec §14."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project
from src.orchestrator import Orchestrator
from src.tools.definitions import _ALL_TOOL_DEFINITIONS

PROJECT_ID = "proj"


def defs():
    return {d["name"]: d for d in _ALL_TOOL_DEFINITIONS}


def test_task_claim_definition():
    d = defs()["task_claim"]
    assert set(d["parameters"]["properties"]) >= {"task_id", "next", "wait"}
    assert d.get("category") == "tasks"


def test_close_and_mutators_carry_claim_epoch():
    d = defs()
    for name in ("task_close", "task_heartbeat", "task_set", "task_handoff"):
        assert "claim_epoch" in d[name]["parameters"]["properties"], name
    assert {"claim_next", "wait"} <= set(d["task_close"]["parameters"]["properties"])


def test_create_task_accepts_swarm_fields():
    props = defs()["create_task"]["parameters"]["properties"]
    assert {"depends_on", "discovered_from", "dedup_key", "parent_id"} <= set(props)


def test_pool_commands_defined():
    d = defs()
    assert d["pool_status"]["category"] == "ops"
    assert {"project_id", "profile_id", "min", "max", "now"} <= set(
        d["pool_scale"]["parameters"]["properties"])


def test_read_claim_epoch_prefers_file(tmp_path, monkeypatch):
    from src.cli.agent_surface import read_claim_epoch

    monkeypatch.setenv("AQ_CLAIM_EPOCH", "9")
    assert read_claim_epoch(str(tmp_path)) == 9
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text(json.dumps({"task_id": "t", "claim_epoch": 3}))
    assert read_claim_epoch(str(tmp_path)) == 3


def test_cli_close_sends_claim_epoch(tmp_path, monkeypatch):
    from src.cli import agent_surface
    from src.cli.main import cli

    sent = {}

    class FakeClient:
        def execute(self, command, args):
            sent.update(command=command, args=args)
            return {"success": True}

    monkeypatch.setattr(agent_surface, "_client", lambda *a, **k: FakeClient())
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text(json.dumps({"task_id": "t1", "claim_epoch": 4}))
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli, ["task", "close", "t1", "--outcome", "pass", "--summary", "s",
                                 "--claim-next"])
    assert r.exit_code == 0, r.output
    assert sent["command"] == "task_close"
    assert (sent["args"]["claim_epoch"], sent["args"]["claim_next"]) == (4, True)


def test_cli_claim_timeout_is_long():
    from src.cli.client import _COMMAND_TIMEOUTS

    assert _COMMAND_TIMEOUTS["task_claim"] >= 180 and _COMMAND_TIMEOUTS["task_close"] >= 180


@pytest.fixture
async def handler(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    await db.create_project(Project(id=PROJECT_ID, name="p"))
    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                    workspace_dir=str(tmp_path / "ws"), database_path=str(tmp_path / "test.db"),
                    data_dir=str(tmp_path / "data"))
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = MagicMock()
    yield CommandHandler(orch, cfg)
    await db.close()


async def test_schema_enums(handler):
    res = await handler._cmd_get_schema({})
    enums = res["enums"]
    assert enums["lifecycle"] == ["task", "named", "pool"]
    assert "stale_claim" in enums["claim_result"] and enums["outcome"] == ["pass", "fail"]


async def test_pool_status_empty(handler):
    res = await handler._cmd_pool_status({})
    assert res == {"success": True, "pools": []}


def test_skill_documents_worker_loop():
    text = open("src/skills/aq-tasks/SKILL.md", encoding="utf-8").read()
    assert "aq task claim --next" in text and "--claim-next" in text
    assert "--outcome pass|fail" in text and "needs_context" not in text
```

Check how `agent_surface.py` obtains its client (the symbol may be `_client`, `get_client`, or an import from `src.cli.client`) and patch the real one; check the CLI entry (`src/cli/main.py: cli`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_swarm_surface.py -v`
Expected: FAIL — `KeyError: 'task_claim'`.

- [ ] **Step 3: Implement**

Definitions: copy `task_close`'s block (`definitions.py:3498-3537`) as the template for `task_claim`; add parameters per Interfaces; one-sentence descriptions; `task_close.claim_epoch` description says "the CLI reads it from .aq/claim.json". `_CLI_CATEGORY_OVERRIDES`: `pool_status`/`pool_scale` → `pool` (read how `auto_commands.py` turns a category into a Click group and how `task_children` from Plan 1 was placed — follow it).

`ops_commands.py`:

```python
    async def _cmd_pool_status(self, args: dict) -> dict:
        project_ids = [args["project_id"]] if args.get("project_id") else None
        supply, demand, bounds, _profiles, _caps, _projects = (
            await self.orchestrator._measure_pools(project_ids))
        now = time.time()
        pools = []
        for key in sorted(supply, key=lambda k: (k.project_id, k.profile_id)):
            sup, (lo, hi) = supply[key], bounds[key]
            want = sup.running_busy + demand.get(key, 0)
            desired = max(lo, want) if hi is None else min(max(lo, want), hi)
            desired = max(desired, sup.running_busy + sup.starting)
            row = {"project_id": key.project_id, "profile_id": key.profile_id,
                   "min_active": lo, "max_active": hi, "desired": desired,
                   "running_idle": sup.running_idle, "running_busy": sup.running_busy,
                   "starting": sup.starting, "draining": sup.draining,
                   "ready": demand.get(key, 0)}
            until = self.orchestrator._pool_quarantine.get((key.project_id, key.profile_id))
            if until and until > now:
                row["quarantined_until"] = until
            pools.append(row)
        return {"success": True, "pools": pools}

    async def _cmd_pool_scale(self, args: dict) -> dict:
        project_id, profile_id = args.get("project_id"), args.get("profile_id")
        if not project_id or not profile_id:
            return {"success": False, "error": "project_id and profile_id are required"}
        lo, hi = args.get("min"), args.get("max")
        if lo is not None and lo < 0:
            return {"success": False, "error": "min must be >= 0"}
        if hi is not None and hi < 1:
            return {"success": False, "error": "max must be >= 1"}
        if lo is not None and hi is not None and lo > hi:
            return {"success": False, "error": "min must be <= max"}
        profile = await self._write_pool_bounds(project_id, profile_id, lo, hi)
        if profile is None:
            return {"success": False, "error": f"no pool profile '{profile_id}' for {project_id}"}
        terminated = []
        if args.get("now") and hi is not None:
            live = [s for s in await self.db.list_sessions(lifecycle="pool", project_id=project_id)
                    if s.profile_id == profile_id and s.state in ("running", "stalled")]
            idle = sorted((s for s in live if not s.task_id), key=lambda s: s.started_at or 0)
            for s in idle[: max(0, len(live) - hi)]:
                await self.orchestrator._terminate_pool_session(s, reason="scaled")
                terminated.append(s.id)
        return {"success": True, "project_id": project_id, "profile_id": profile_id,
                "min_active": profile.min_active, "max_active": profile.max_active,
                "terminated": terminated}
```

`_write_pool_bounds` uses the profile writer `aq profile set` uses (grep `_cmd_profile_set` / `profiles/sync.py` for the write path); returns the updated `AgentProfile` or `None`.

CLI: `read_claim_epoch`; the hand-crafted commands built with the module's existing client/emit helpers, sending `claim_epoch` only when resolved; `HAND_CRAFTED = {"task_claim", "task_close", "task_heartbeat", "task_set", "task_handoff", ...}` consulted by `auto_commands.py` so auto-generation skips them. `client.py`: the two timeouts. Formatters: `task_claim` prints `result`, task id/title, `claim_epoch`; `pool_status` prints a table. `_cmd_get_schema`: the `enums` mapping. Docs/text: rewrite SKILL.md's lifecycle section around the worker loop; `grep -rn "needs_context" src/skills src/prime` must come back empty; `completion_protocol.md` gets the `--claim-next` variant for pool sessions (check how `prime/sections.py` picks templates by lifecycle and render the pool variant only for `AQ_SESSION_KIND=pool` / `lifecycle == "pool"`).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_swarm_surface.py tests/test_tool_definitions.py tests/test_definitions_registry.py tests/test_cli_agent_surface.py tests/test_cli_auto_commands.py tests/test_api_models.py tests/test_surface_commands.py tests/test_prime.py -v -n auto`, then the full suite. If a client/OpenAPI generation check exists (`grep -rn codegen pyproject.toml Makefile scripts/`), run it; otherwise state in the report that `openapi.json` / `packages/aq-client` / `packages/aq-ts-client` need regeneration.

- [ ] **Step 5: Commit**

```bash
git add src/tools/definitions.py src/api/models/task.py src/commands/ops_commands.py src/orchestrator/pools.py src/cli/agent_surface.py src/cli/auto_commands.py src/cli/client.py src/cli/formatter_registry.py src/commands/surface_commands.py src/skills/aq-tasks/SKILL.md src/prime tests/test_swarm_surface.py
git commit -m "feat(surface): task_claim and pool commands in tool defs, API models, claim-aware CLI, schema enums, worker-loop skill text

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Doctor checks, statement-count perf tests (SQLite + Postgres), end-to-end swarm integration test, CI Postgres service

**Files:**
- Create: `src/doctor/pool_checks.py`; register it where `src/doctor/hierarchy_checks.py` is registered
- Create: `tests/test_pool_doctor.py`, `tests/perf/conftest.py`, `tests/perf/test_claim_statements.py`, `tests/test_swarm_integration.py`
- Modify: `tests/perf/test_hierarchy_statements.py` (`seed_scale(db, n_tasks=5000, n_edges=2500, profile_id=None)`), `.github/workflows/tests.yml` (postgres service + `POSTGRES_TEST_DSN`; install `.[dev,cli,postgresql]`)

**Interfaces:**
- Consumes: everything above; `count_statements` and `seed_scale` from `tests/perf/test_hierarchy_statements.py`; the doctor check API in `src/doctor/hierarchy_checks.py`.
- Produces:
  - Doctor checks (owner `"swarm-work-model"`), same dataclass/registration API as the hierarchy checks: `pools.stuck` — pool sessions `running` with `task_id` whose task is not `IN_PROGRESS`/`ASSIGNED` → repair `release_claim(sid, task_status=<task's current status>, context="doctor", ...)`; `pools.orphan_agents` — agents of pool profiles with no session row → repair `release_workspaces_for_agent` + `delete_agent`; `pools.preparing_stuck` — `claim_phase in (claiming, preparing)` older than `2 × swarm.prepare_timeout` → repair as `_step_prepare_timeout`; `claims.holder_consistency` — for every `IN_PROGRESS` task with `assigned_agent_id`: `agents.current_task_id == task.id`, exactly one session with `task_id == task.id`, `claimed_by_session` meta matches (report-only).
  - Perf budgets (spec §15), asserted with `count_statements` on SQLite and — when `POSTGRES_TEST_DSN` is set — Postgres, at `seed_scale(n_tasks=5000, profile_id="worker")`: whole `task_claim` happy path (slot reset stubbed) ≤ 14 statements SQLite / ≤ 13 Postgres (**ruling:** the spec's "≤ 6 logical" counts the claim transaction alone; `_apply_transition`, activation and metadata bring the command to this budget — the measured number goes in the test docstring); `no_ready_work` path ≤ 6; `release_claim` ≤ 9; `count_ready_by_profile` = 1; `_reconcile_pools` with 3 projects × 3 pool profiles and no starts ≤ 3 + 3 × 3 (profiles + ready + sessions per project, counted from the real calls). Latency: claim p99 ≤ 50 ms on SQLite at 5,000 tasks over 50 iterations (`sorted(times)[48] < 0.05`), marked `@pytest.mark.perf` and skipped unless `AQ_PERF_STRICT=1` (xdist load makes it flaky otherwise).
  - Integration test (fake provider): pool profile `max_active=1, max_claims_per_session=2`, 3 ready tasks → `run_one_cycle` starts one `p-` session → simulated worker via the handler: `task_claim --next` → `close --claim-next` → second task → `close --claim-next` → `session_exhausted` → provider reports exit 0 → next cycle terminates it (agent `RETIRED`, workspace unlocked) and starts a fresh session for the third task; every `task.ready`/`task.claimed`/`task.completed` emitted with the base triple; a task filed by the worker lands `DEFINED` with a routing gate and the `worker-filed-triage` rule matches it (`_eval_pipeline_when` against the emitted payload).
  - CI: `postgres:18` service (`POSTGRES_USER=agent_queue`, `POSTGRES_PASSWORD=agent_queue`, `POSTGRES_DB=agent_queue`, port 5432, health check) and `POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue:agent_queue@localhost:5432/agent_queue` in the job env.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pool_doctor.py
"""Doctor checks for pools and claims — spec §16."""

from __future__ import annotations

import time

import pytest

from src.doctor import pool_checks
from src.models import (Agent, AgentProfile, AgentState, Project, SessionRecord, Task,
                        TaskStatus, Workspace)

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    from src.database import Database

    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(AgentProfile(id="worker", name="w", lifecycle="pool"))
    yield database
    await database.close()


def test_check_names():
    names = {c.name for c in pool_checks.CHECKS}
    assert names == {"pools.stuck", "pools.orphan_agents", "pools.preparing_stuck",
                     "claims.holder_consistency"}
    assert all(c.owner == "swarm-work-model" for c in pool_checks.CHECKS)


async def test_orphan_agent_detected_and_repaired(db):
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.IDLE))
    await db.create_workspace(Workspace(id="ws", project_id=PROJECT_ID, workspace_path="/w",
                                        kind_id="project-repo", locked_by_agent_id="a1"))
    finding = await pool_checks.run_check(db, "pools.orphan_agents", config=None)
    assert finding.count == 1
    await pool_checks.run_check(db, "pools.orphan_agents", config=None, repair=True)
    assert await db.get_agent("a1") is None
    assert (await db.get_workspace("ws")).locked_by_agent_id is None


async def test_stuck_pool_session_released(db):
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.BUSY))
    await db.create_task(Task(id="t1", project_id=PROJECT_ID, title="t", description="d",
                              status=TaskStatus.COMPLETED))
    await db.create_session(SessionRecord(
        id="s1", project_id=PROJECT_ID, profile_id="worker", harness="claude", provider="fake",
        name="s1", lifecycle="pool", work_dir="/w", epoch="e", instance_token="t",
        started_at=time.time(), state="running", agent_id="a1", task_id="t1",
        claim_phase="active"))
    assert (await pool_checks.run_check(db, "pools.stuck", config=None)).count == 1
    await pool_checks.run_check(db, "pools.stuck", config=None, repair=True)
    assert (await db.get_session("s1")).task_id is None
    assert (await db.get_agent("a1")).state == AgentState.IDLE


async def test_holder_consistency_reports_mismatch(db):
    await db.create_agent(Agent(id="a1", name="a1", profile_id="worker", state=AgentState.BUSY,
                                current_task_id="other"))
    await db.create_task(Task(id="t1", project_id=PROJECT_ID, title="t", description="d",
                              status=TaskStatus.IN_PROGRESS, assigned_agent_id="a1"))
    finding = await pool_checks.run_check(db, "claims.holder_consistency", config=None)
    assert finding.count == 1 and finding.repairable is False
```

Mirror `hierarchy_checks.py`'s finding dataclass, `CHECKS` list and `run_check` signature exactly (read it first; rename in the test if the real API differs).

`tests/perf/conftest.py`:

```python
import os

import pytest

POSTGRES_TEST_DSN = os.environ.get("POSTGRES_TEST_DSN")


@pytest.fixture(params=["sqlite", "postgres"])
async def any_db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        db = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await db.initialize()
        await db.reset_for_tests()  # add if missing: drop + alembic upgrade head (see tests/test_database_postgresql.py)
    else:
        from src.database import Database

        db = Database(str(tmp_path / "perf.db"))
        await db.initialize()
    yield db
    await db.close()
```

`tests/perf/test_claim_statements.py` — one test per budget in Interfaces using `count_statements(any_db, coro)`; the claim happy path drives `handler._cmd_task_claim` with `_worktree_slots` stubbed as in `tests/test_claim_commands.py`; the latency test uses `time.perf_counter` around 50 claim/release cycles.

`tests/test_swarm_integration.py` — one test with commented phases per the Interfaces narrative plus one for worker filing; build the orchestrator with the fake provider as `tests/test_session_lens.py` does and drive `run_one_cycle()`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pool_doctor.py tests/perf/test_claim_statements.py tests/test_swarm_integration.py -v`
Expected: FAIL — `src.doctor.pool_checks` missing; `seed_scale()` rejects `profile_id`.

- [ ] **Step 3: Implement**

`pool_checks.py`: four checks; `claims.holder_consistency` has `repairable = False`. Register with the hierarchy checks (`grep -rn hierarchy_checks src/doctor src/commands`). `seed_scale(..., profile_id=None)` sets `profile_id` on every ready task. CI workflow: service block + env + extra.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_pool_doctor.py tests/perf -v`, `pytest tests/test_swarm_integration.py -v`, then the full suite. If Postgres is reachable (`docker compose up -d postgres`; DSN `postgresql+asyncpg://agent_queue:agent_queue@localhost:5533/agent_queue`), run `POSTGRES_TEST_DSN=... pytest tests/perf tests/test_claim_queries.py -v` and put the statement counts in the report. If it is not reachable, say so in the report — never let the skip pass silently.

- [ ] **Step 5: Commit**

```bash
git add src/doctor tests/test_pool_doctor.py tests/perf tests/test_swarm_integration.py .github/workflows/tests.yml
git commit -m "test(swarm): pool/claim doctor checks, statement and latency budgets (sqlite+postgres), end-to-end pool worker loop

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Documentation, config reference, crosswalk, whole-branch verification

**Files:**
- Modify: `docs/specs/work-graph.md` (short "Claims and pools" section pointing at the design spec), `docs/specs/sessions.md` (pool lifecycle: `p-` prefix, claim phases, bootstrap, reconciler carve-outs), `docs/specs/implementation/work-graph.md` (module map += `claim_queries.py`, `claim_commands.py`, `pools.py`, `pool_checks.py`), `docs/specs/design/agent-coordination.md` (hybrid dispatch paragraph + link), the config reference doc that documents sections (grep `work_graph` under `docs/` to find it — add `swarm`), `docs/specs/events.md` or equivalent (new event types), `CLAUDE.md` Quick Reference, `profile.md` codebase map, `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` §18 (fill the Part II rows with file paths)
- Test: none new — verification task

- [ ] **Step 1: Write the docs**

Each addition states the contract, not the implementation: pool profile keys and defaults; the worker loop and the result-code table (spec §10.3); the claim file and the fence rule ("every mutation of a held task carries `claim_epoch`; pool sessions must send it, task sessions may"); pool sizing rules (spec §11.1); worker-filed constraints (spec §12) and the default `worker-filed-triage` rule; `swarm` config keys (hot-reloadable); new events (`task.ready`, `task.claimed`, `task.claim_conflict`, `pool.scaled`, `session.claim_timeout`, `snapshot.refreshed`, `project.resumed`, `constraint.released`). Fill spec §18's Part II rows. CLAUDE.md gets one bullet under Quick Reference:

```
- **Swarm (claims/pools):** `src/database/queries/claim_queries.py` (claim transaction, epoch fence), `src/commands/claim_commands.py` (`task_claim`), `src/orchestrator/pools.py` (`_reconcile_pools`; pure `size_pools` in `scheduler.py`), pool carve-outs in `src/sessions/reconciler.py`, checks in `src/doctor/pool_checks.py`. Profiles with `lifecycle: pool` pull work via `aq task claim`; `lifecycle: task` keeps push. Off by default (`swarm.enabled`). Spec: `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` Part II.
```

- [ ] **Step 2: Verify the branch**

Run each in the foreground:
1. `ruff check src tests` and `ruff format --check src/database/queries/claim_queries.py src/commands/claim_commands.py src/orchestrator/pools.py src/doctor/pool_checks.py`
2. `alembic heads` → exactly `c3d4e5f6a7b8`; `alembic revision --autogenerate -m check` → "No changes in schema detected" (delete any file it creates)
3. `timeout 580 pytest tests/ --ignore=tests/chat_eval -n auto -q -p no:cacheprovider 2>&1 | tail -5` → all passed
4. `pytest tests/perf -v` → budgets hold
5. `aq --help` shows `pool`; `aq task --help` shows `claim`
6. `grep -rn "needs_context" src/skills src/prime` → no matches

- [ ] **Step 3: Commit**

```bash
git add docs CLAUDE.md profile.md
git commit -m "docs(swarm): claims, pools, worker loop and worker-filed work — specs, config, crosswalk

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage (Part II + §14–§17):**
- §9 profile config (`lifecycle: pool`, `min_active`, `max_active`, `max_claims_per_session` NULL/0 rule), `task.ready` on every frontier entry with the in-transaction audit row → Tasks 1, 2.
- §10 claim transaction order, `SKIP LOCKED`/CAS, `claim_epoch` fence on every mutation, claim file, `claim_phase`/`claim_phase_at`, attempt outcomes, all ten result codes, long-poll subscribe-before-check with the `max_event_id` fallback, admission wait, prepare timeout, idempotent re-claim → Tasks 3, 4, 6.
- §11 sizing formula and floors, caps with fair-share, per-tick start/drain limits, grace, never mid-task; `release_claim` vs `terminate_pool_session`; exit/orphan/stall/backstop carve-outs; rapid-crash quarantine per pool key; bootstrap prompt; `p-` adoption; push exclusion → Tasks 5, 6.
- §12 project pin, `discovered-from`/`parent-child`, DEFINED start, routing gate in the creation transaction, atomic quota, idle sessions cannot file, playbook-owned policy via the default rule → Task 7.
- §14 surface (tool defs, API models, claim-aware CLI, `aq pool`, `aq schema` enums, skill/prime text) → Task 8. §15 budgets and the Postgres path → Task 9. §16 tests → every task plus Task 9's doctor/integration. §17 rollout: `swarm.enabled=False` keeps push behaviour unchanged; revision C is additive; `lifecycle: pool` is opt-in per profile.
- §13 formulas are Plan 3 by design.

**Placeholder scan:** every "check the real name" note points at a specific file/line and a specific fallback; the `orch`/`reconciler` fixtures in Tasks 5 and 6 are to be copied from a named existing test file; Task 9's perf and integration tests are specified by budget and narrative with the fixture code given. No TBDs.

**Type consistency:** `TransitionResult.ready: list[tuple[str, str]]` (Task 2) is what `_notify_ready(entries)` (Tasks 2, 3) and `_on_frontier_entries(entries)` consume. `take_claim_slot -> (kind, SessionRecord | None)`, `select_ready_for_profile -> str | None`, `take_task -> Task | None`, `release_claim(..., conn=None) -> TransitionResult`, `terminate_pool_session(..., conn=None)` (Task 3) are used unchanged in Tasks 4, 6, 8, 9. `PoolKey/PoolSupply/PoolAction/size_pools` and `_measure_pools`'s 6-tuple (Task 5) are what Task 8's `pool_status` consumes. `_terminate_pool_session(session, *, reason, task_status=READY)` (Task 5) is used by Tasks 6 and 8. `_resolve_claim_waiters(session_id, epoch, result)` and `write_claim_file/remove_claim_file` (Task 4) are used by Tasks 5 and 6.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-29-swarm-claims-pools.md`. Per the standing instruction, execution proceeds with **superpowers:subagent-driven-development** on branch `swarm/hierarchy` in this worktree: fresh implementer per task, task review after each, one whole-branch review at the end, then Plan 3.
