# Playbook V2 — Package 3 child plan: Content-addressed artifacts and durable run state

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` (or `superpowers:subagent-driven-development`) to run this plan task by task. Every task below is a red/green unit with a named failing assertion, a named implementation, and its own verification command. Do not reorder tasks across commit boundaries.

**Parent roadmap:** `docs/superpowers/plans/2026-09-01-playbook-v2-implementation-roadmap.md` § "Package 3 — Content-addressed artifacts and durable run state"
**Design spec:** `docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md` §§ "Storage and activation", "Run-state persistence", "Execution receipts", "Storage invariants"
**Branch:** `feature/playbook-v2-pkg3` (see §4.1 — this package is delivered on **two** parallel sub-branches that merge into it)
**Consumes:** Package 2's `PlaybookDefinition` and its canonical serialization; Package 1's `CommandContract` fingerprints (read as opaque strings only); Package 0's `CapabilityPolicy` (read as an opaque fingerprint only).
**Produces:** content-addressed artifact files, artifact + activation tables, V2 run snapshots, execution receipts, durable waits, pending events, activation health, retention, and the two repository interfaces Package 4 executes against.

**Status of the tree this was written against:** `origin/main` at `a2f946a0` (2026-09-02). Where things stand upstream:

- **Package 0's code has landed.** `src/profiles/capabilities.py` (`CapabilityPolicy` with `to_canonical()`, `fingerprint()`, `intersect()`, `is_subset_of()`, plus `capability_policy_for`) and `src/commands/principal.py` are on `origin/main`. This package consumes `CapabilityPolicy.fingerprint()` and nothing else from it.
- **Packages 1 and 2 have landed their child plans, not their code.** `docs/superpowers/plans/2026-09-01-playbook-v2-contracts-intent.md` and `...-typed-model-compiler.md` are on `origin/main`; `src/commands/contracts/` and `src/playbooks/definition.py` do not exist yet. §3 pins the exact symbols those two plans commit to, so this package builds against their published interfaces rather than guesses.
- Every symbol this package *creates* is specified here against the live tree; §3.2's reconciliation script must be run *before commit 1* on both sub-branches.

---

## 1. Why this package exists (what the live tree actually does today)

Four facts, each read from the tree, not inferred from the roadmap.

### 1.1 "Active" today means "the newest file at a predictable path"

`CompiledPlaybookStore` (`src/playbooks/store.py:61`) writes compiled JSON to `{data_dir}/compiled/<scope-dir>/<id>.compiled.json` (`src/playbooks/store.py:114` `compiled_path`, `:148` `save`, `:200` `load`). The path is derived from the playbook id and scope, so **saving a new compilation overwrites the running definition in place**. There is no hash in the path, no second copy, and no database row that says which bytes are authoritative. `store.save()` is a plain `open(path, "w")` + `json.dump` — not atomic, so a crash mid-write leaves a truncated file. `load()` catches `json.JSONDecodeError` and returns `None` after one `logger.warning` (`src/playbooks/store.py:243`) — the playbook drops out of the active registry quietly rather than failing loudly, which is the failure mode content-addressed storage plus a database activation pointer removes.

`compiled_root` is `{data_dir}/compiled` (`src/config.py:1720`, surfaced as `VaultManager.compiled_root`, `src/vault_manager.py:100`). This package adds `{compiled_root}/artifacts/<sha256>.json` beside the existing tree and does not touch the V1 layout.

### 1.2 There is no artifact table and no activation table

`grep -rn "playbook_artifacts\|playbook_activations\|artifact_sha" src/` returns nothing. The only playbook table in `src/database/tables.py` is `playbook_runs` (`src/database/tables.py:921`), whose `pinned_graph` column (`:941`) stores **a full copy of the compiled graph JSON per run**. That is how V1 achieves overlay pinning: by duplicating the entire graph into every run row. A V2 run stores a 71-byte hash instead.

Enablement today lives *inside* the compiled artifact (`CompiledPlaybook.enabled`, `src/playbooks/models.py:421`), and the way an operator flips it is `_cmd_set_playbook_enabled` (`src/commands/playbook_commands.py:1491`): it rewrites the **authored Markdown's frontmatter** and recompiles. Pausing a playbook therefore edits a source file and produces a new artifact. That is exactly what the design spec forbids: "Operational activation metadata, including `enabled`, health, and the active artifact hash, lives in `playbook_activations` rather than inside the immutable artifact."

### 1.3 Run state is a single mutable row with no version and no receipts

`PlaybookQueryMixin` (`src/database/queries/playbook_queries.py:22`) offers `create_playbook_run`, `get_playbook_run`, `update_playbook_run(run_id, **kwargs)` (`:77`) and `delete_playbook_run`. `update_playbook_run` is an unconditional `UPDATE ... WHERE run_id = ?` — last writer wins. There is no `snapshot_version`, so two resumes of the same run (a restart racing a wait) silently interleave. Per-step history is a JSON blob in `playbook_runs.node_trace` parsed back out by `_parse_node_trace` (`src/playbooks/health.py:102`), so it is neither indexed, nor immutable, nor redacted.

There is no wait table: a V1 run that waits records `waiting_for_event` on the run row (`src/database/tables.py:943`) and is swept by `_check_paused_playbook_timeouts` (`src/orchestrator/monitoring.py:649`). Between "decide to wait" and "write the column" there is no transaction covering both the state change and the wait registration, which is the race the roadmap's `WaitRepository` exists to close.

### 1.4 The database already has the primitive this package needs

`TransactionQueryMixin.immediate()` (`src/database/queries/transaction_queries.py:60`) yields a connection that holds the write lock from the first statement — `BEGIN IMMEDIATE` on SQLite (guarded by a per-adapter `asyncio.Lock`), plain `engine.begin()` on PostgreSQL. Every atomicity requirement in this package is expressed as "one `immediate()` block", and every compare-and-set is a single `UPDATE ... WHERE <cas columns>` inside one. Nothing here needs a new concurrency primitive.

---

## 2. Deviations from the roadmap's file list

The roadmap (§3, §5 Package 3) permits refinement after inspecting the live tree and requires the deviation be documented. This is that record.

| Roadmap says | Live tree | This plan does |
|---|---|---|
| Create `tests/playbooks/test_artifact_store.py`, `test_activation.py`, `test_run_repository.py`, `test_wait_repository.py` | **`tests/playbooks/` does not exist.** All suites are flat `tests/test_*.py`; only `tests/fixtures/`, `tests/llm/`, `tests/perf/` are subdirectories | Flat: `tests/test_playbook_artifact_store.py`, `tests/test_playbook_activation.py`, `tests/test_playbook_run_repository.py`, `tests/test_playbook_wait_repository.py`. Same deviation Packages 0, 5 and 6 recorded |
| Create `tests/database/test_playbook_v2_migrations.py` | **`tests/database/` does not exist**; migration suites are `tests/test_migration_<topic>.py` and carry `pytestmark = pytest.mark.migration` (`tests/test_migration_agent_flock.py:8`) | `tests/test_migration_playbook_v2.py`, marked `migration` |
| `ruff check src/playbooks src/database tests/playbooks tests/database` | those test dirs do not exist | `ruff check src/playbooks src/database tests/test_playbook_artifact_store.py tests/test_playbook_activation.py tests/test_playbook_run_repository.py tests/test_playbook_wait_repository.py tests/test_migration_playbook_v2.py` |
| Modify `src/playbooks/health.py` | `health.py` computes **V1 run metrics** (`compute_node_metrics`, `compute_transition_paths`, `compute_playbook_health` at `:406`), consumed by `_cmd_playbook_health` (`src/commands/playbook_commands.py:1138`). It has nothing to do with activation readiness | **Not modified.** `ActivationHealth` and its evaluation live in the new `src/playbooks/activation.py`. Touching `health.py` would couple V1 run analytics to V2 activation state and break `tests/test_playbook_health.py` for no gain. Package 7 deletes `health.py` with the rest of V1 |
| Modify `src/database/queries/playbook_queries.py` "only where compatibility reads are required" | V2 rows live in new tables; no V1 read needs them | **Not modified.** The V1 mixin stays byte-identical, which is what keeps this package's rollback boundary clean |
| Modify "API models … for retention and size limits" | Package 5's plan checks in `src/api/models/playbook_v2.py` and owns every V2 DTO (`2026-09-01-playbook-v2-graph-api-ui.md` §4) | **No API model changes here.** This package ships the *config* half (`src/config.py`) and the storage half only. Package 5 projects `ArtifactRef` → `ArtifactRefDTO` and the activation row → `ActivationStateDTO`; §4.6 below pins the field names so that projection is a rename-free copy |
| The design spec names the run table `playbook_runs` | `playbook_runs` is V1's table and must stay readable after cutover ("Historical V1 runs remain readable after V1 execution is removed") | The V2 table is **`playbook_v2_runs`**. The name is permanent — Package 7 does not rename it, because renaming would break every artifact-pinned overlay and every receipt FK for zero user-visible gain |
| Roadmap §4 lists five `ActivationHealth` values | Package 5's checked-in DTO uses **six** (`ready`, `question_required`, `invalid`, `disabled`, `stale_contract`, `unavailable`) and its §16 explicitly asks Package 3 to emit six (`2026-09-01-playbook-v2-graph-api-ui.md` §4.4, §16) | **Six values**, `stale_contract` as the wire value for a fingerprint mismatch, no `needs_rebuild` alias. §4 records this as an interface amendment under roadmap §7 |
| (not mentioned) | `tests/test_docs_sync.py::TestDatabaseSpecSync` fails the moment `src/database/tables.py` gains a table with no ``### Table: `name` `` section in `docs/specs/database.md`, and also checks that every backticked column name in a doc section exists in code | Every migration commit ships its `docs/specs/database.md` section **in the same commit**. §6.8 carries the exact prose |
| (not mentioned) | `src/database/engine.py:64` `_schema_cache_key` hashes `tables.py` + every file in `migrations/versions/`, and `_cached_template_is_valid` (`:79`) compares the cached template's `alembic_version` against `alembic heads` | No action needed — the cache self-invalidates. Recorded because it explains why the first test run after each migration commit is slow |
| (not mentioned) | `src/database/engine.py:218` `_preflight_check_alembic_version` raises on an `alembic_version` row naming a revision this checkout lacks | **Operator hazard for this package specifically** — see §7.5. Two parallel branches with different migrations pointed at one dev database will trip it |

---

## 3. Expected symbols from earlier packages

### 3.1 What this package imports but does not create

| Symbol | Owner | State | Used for |
|---|---|---|---|
| `src/profiles/capabilities.py::CapabilityPolicy.fingerprint()` | P0 | **landed** | `playbook_artifacts.profile_fingerprint`, compared as an opaque string |
| `src/playbooks/definition.py::PlaybookDefinition` (strict Pydantic, `extra="forbid"`, `schema_version`, `id`, `version`, `purpose`, `rules`, `steps`, `compiled_against`) | P2 | plan landed | the object `ArtifactStore.put` / `load` serializes and validates |
| `src/playbooks/definition.py::canonical_bytes(d) -> bytes`, `artifact_sha256(d) -> str`, `source_digest(markdown) -> str`, `contract_fingerprint(d) -> str`, `COMPILER_BUILD` | P2 | plan landed (`...-typed-model-compiler.md` §4.7) | **the** canonicalizer and **the** three fingerprints. This package calls them; it does not re-derive them (§5.1) |
| `src/commands/contracts/registry.py::CONTRACTS` (`ContractRegistry`) with `.get(name) -> CommandRegistration | None` and `.fingerprint(name) -> str` | P1 | plan landed (`...-contracts-intent.md` §3.5) | `stale_contract` health only — fingerprints are compared as opaque strings, never introspected |
| `ExecutionContract.sensitive_args`, `.sensitive_result_fields`, `.receipt_projection` | P1 | plan landed (§3.2, §3.5) | the receipt projection's allow-list and redaction inputs (§8.3). P1's plan states explicitly that "Package 3 consumes this" |

Nothing else. This package **must not** import the compiler, the validator, the explanation renderer, or any executor. If a storage decision seems to need one of those, it belongs in Package 2 or 4.

### 3.2 Reconciliation checklist — run before commit 1, on **both** sub-branches

```bash
python - <<'PY'
import importlib
WANT = {
    "src.playbooks.definition": [
        "PlaybookDefinition", "canonical_bytes", "artifact_sha256",
        "source_digest", "contract_fingerprint", "COMPILER_BUILD",
    ],
    "src.commands.contracts.registry": ["CONTRACTS", "ContractRegistry"],
    "src.profiles.capabilities": ["CapabilityPolicy"],
}
for mod, names in WANT.items():
    try:
        m = importlib.import_module(mod)
    except Exception as exc:
        print(f"MISSING MODULE {mod}: {exc}")
        continue
    for n in names:
        if not hasattr(m, n):
            print(f"MISSING SYMBOL {mod}.{n}")
    if mod == "src.playbooks.definition":
        d = getattr(m, "PlaybookDefinition", None)
        print("strict:", getattr(d, "model_config", {}).get("extra") if d else "n/a")
    if mod == "src.commands.contracts.registry":
        reg = getattr(m, "CONTRACTS", None)
        print("registry lookup:", [n for n in ("get", "fingerprint") if hasattr(reg, n)])
PY

# The revision this package's first migration must chain from:
python -m alembic heads          # expect exactly one line
# Tables that must NOT already exist:
python -c "from src.database.tables import metadata; \
print(sorted(t for t in metadata.tables if t.startswith('playbook')))"
```

**If `src.playbooks.definition` is absent** (Package 2 has not merged), the storage branch may still proceed: every signature below is written against a `PlaybookDefinitionT` type alias declared once in `src/playbooks/artifact_store.py`:

```python
if TYPE_CHECKING:
    from src.playbooks.definition import PlaybookDefinition as PlaybookDefinitionT
else:  # pragma: no cover - P2 not merged yet
    PlaybookDefinitionT = Any
```

and the tests use the fixture definition of §16.1, which is a plain dict round-tripped through whatever loader is present. Delete the `else` branch in the reconciliation commit (commit 5) once P2 has merged. **This alias is the only permitted stub. Do not stub `CommandContract` or `CapabilityPolicy` — this package only ever holds their fingerprints as strings.**

---

## 4. The parallelism lock

Roadmap §7: *"Package 3 artifact storage and run-state schema may be separate branches only if they share an agreed `ArtifactRef` and migration ordering."* This section is that agreement. It is normative: an implementation task may **add** an optional field with a default; it may not rename, retype, reorder or remove anything below without amending this document in the same commit as the code.

### 4.1 Branch and commit protocol

| Branch | Owns | Commits |
|---|---|---|
| `feature/playbook-v2-pkg3` | the integration branch; nothing is written directly on it except the merge and commit 5 | — |
| `feature/playbook-v2-pkg3-artifacts` (**Task A**) | `artifact_ref.py`, `artifact_store.py`, `activation.py`, `playbook_artifact_queries.py`, migration `a3f1c0de0001`, retention, the doctor check, config | roadmap commits 1 and 4 |
| `feature/playbook-v2-pkg3-runstate` (**Task B**) | `run_state.py`, `receipts.py`, `waits.py`, `playbook_run_queries.py`, migrations `b3f2c0de0002` and `b3f2c0de0003` | roadmap commits 2 and 3 |

**Commit 0 — the shared seed.** Whichever task starts first commits `src/playbooks/artifact_ref.py` (§4.2, verbatim) and the `tables.py` import-line change, alone, as `chore: seed playbook v2 pkg3 shared artifact reference`, and pushes `feature/playbook-v2-pkg3` immediately. The other task branches from it. If both tasks create commit 0 independently the file contents are byte-identical, so git merges them without a conflict — that is the point of carrying the file verbatim here. **Do not paraphrase it.**

**Task B branches from Task A's commit 1** (or from commit 0 plus a cherry-pick of A's migration file) because of §4.3. If A's commit 1 is not yet pushed when B starts, B creates `migrations/versions/a3f1c0de0001_playbook_v2_artifacts.py` from §7.1 verbatim — again byte-identical, again conflict-free — and notes the stacking in its PR.

### 4.2 `src/playbooks/artifact_ref.py` — locked, verbatim

Kept in its own module, not in `artifact_store.py`, so the run-state branch can name an artifact without importing the storage branch's file I/O.

```python
"""Immutable artifact identity shared by every Playbook V2 consumer.

Roadmap section 4 ``ArtifactRef``.  Deliberately tiny and dependency-free:
Package 3's two implementation branches both need it, Package 4 pins runs
with it, and Package 5 projects it into ``ArtifactRefDTO`` field-for-field.

The hash is computed from the canonical artifact bytes by
``src.playbooks.definition.canonical_bytes`` (Package 2) and is carried everywhere
in its full ``sha256:<64 lowercase hex>`` form.  Truncation is a display
concern and happens in the dashboard, never here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

#: Canonical hash form used on the wire, in the database and in filenames.
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: ``PlaybookDefinition.schema_version`` this generation of the store accepts.
ARTIFACT_SCHEMA_GENERATION = 2


class ArtifactRefError(ValueError):
    """An artifact reference field is missing or malformed."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Identifies exactly one immutable artifact.

    Field names are the Package 5 wire names (``ArtifactRefDTO``); the
    projection is a copy, not a rename.
    """

    playbook_id: str
    artifact_sha256: str
    schema_generation: int
    contract_fingerprint: str
    source_digest: str
    compiler_build: str
    compiled_at: str | None = None
    version: int = 0

    def __post_init__(self) -> None:
        if not self.playbook_id:
            raise ArtifactRefError("playbook_id is required")
        for name in ("artifact_sha256", "contract_fingerprint", "source_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not SHA256_RE.match(value):
                raise ArtifactRefError(f"{name} must be 'sha256:<64 lowercase hex>', got {value!r}")
        if self.schema_generation != ARTIFACT_SCHEMA_GENERATION:
            raise ArtifactRefError(
                f"schema_generation {self.schema_generation} is not supported "
                f"(this build stores generation {ARTIFACT_SCHEMA_GENERATION})"
            )
        if not self.compiler_build:
            raise ArtifactRefError("compiler_build is required")
        if self.version < 0:
            raise ArtifactRefError("version must be >= 0")

    @property
    def digest(self) -> str:
        """The bare 64-hex digest — the artifact's filename stem."""
        return self.artifact_sha256.split(":", 1)[1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "artifact_sha256": self.artifact_sha256,
            "schema_generation": self.schema_generation,
            "contract_fingerprint": self.contract_fingerprint,
            "source_digest": self.source_digest,
            "compiler_build": self.compiler_build,
            "compiled_at": self.compiled_at,
            "version": self.version,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ArtifactRef:
        """Build from a ``playbook_artifacts`` row mapping."""
        return cls(
            playbook_id=row["playbook_id"],
            artifact_sha256=row["artifact_sha256"],
            schema_generation=int(row["schema_generation"]),
            contract_fingerprint=row["contract_fingerprint"],
            source_digest=row["source_digest"],
            compiler_build=row["compiler_build"],
            compiled_at=row["compiled_at"],
            version=int(row["version"]),
        )
```

### 4.3 Migration ordering — locked

Three revisions, pre-allocated here so two branches cannot both claim `down_revision = "d3e7b1c9a204"` and split the chain into two heads.

| Order | Revision id | File | `down_revision` | Owner | Creates |
|---:|---|---|---|---|---|
| 1 | `a3f1c0de0001` | `migrations/versions/a3f1c0de0001_playbook_v2_artifacts.py` | `d3e7b1c9a204` | Task A | `playbook_artifacts`, `playbook_activations` |
| 2 | `b3f2c0de0002` | `migrations/versions/b3f2c0de0002_playbook_v2_run_state.py` | `a3f1c0de0001` | Task B | `playbook_v2_runs`, `playbook_step_receipts` |
| 3 | `b3f2c0de0003` | `migrations/versions/b3f2c0de0003_playbook_v2_waits.py` | `b3f2c0de0002` | Task B | `playbook_waits`, `playbook_pending_events` |

Rules:

1. **No other Package 3 revision may chain from `d3e7b1c9a204`.** If `origin/main` gains a new head before Task A merges, Task A rebases and updates *only* `a3f1c0de0001.down_revision`. Task B never changes its `down_revision`.
2. `python -m alembic heads` must print exactly one line on every branch, at every commit. This is asserted by `tests/test_migration_playbook_v2.py::test_single_head`.
3. Downgrade order is the reverse: `b3f2c0de0003` → `b3f2c0de0002` → `a3f1c0de0001` → `d3e7b1c9a204`. `playbook_v2_runs.artifact_sha256` references `playbook_artifacts`, so dropping the artifact tables first would leave a dangling FK on PostgreSQL.
4. Neither branch may `alembic revision --autogenerate` a *second* revision for a late column. Add the column to the revision that creates its table and re-run `alembic downgrade` + `upgrade` locally. Two revisions per table would defeat rule 1.

### 4.4 `RunRepository` — locked signatures

Implemented by `PlaybookRunQueryMixin` in `src/database/queries/playbook_run_queries.py`, composed into both adapters. Declared as a `typing.Protocol` in `src/playbooks/run_state.py` so Package 4 can depend on the shape without importing the database package.

```python
class RunRepository(Protocol):
    async def create_run(self, snapshot: RunSnapshot) -> RunSnapshot: ...
    async def load_run(self, run_id: str) -> RunSnapshot | None: ...

    async def commit_boundary(
        self,
        snapshot: RunSnapshot,
        receipt: StepReceipt,
        wait_changes: WaitChangeSet = EMPTY_WAIT_CHANGES,
    ) -> RunSnapshot: ...

    async def request_cancel(
        self, run_id: str, *, expected_version: int, reason: str, requested_by: str
    ) -> RunSnapshot: ...

    async def list_runs(
        self,
        *,
        playbook_id: str | None = None,
        lifecycle: str | None = None,
        artifact_sha256: str | None = None,
        limit: int = 50,
    ) -> list[RunSnapshot]: ...

    async def list_receipts(
        self, run_id: str, *, limit: int = 500, offset: int = 0
    ) -> list[StepReceipt]: ...
```

`commit_boundary` contract, in full:

- One `immediate()` block. Inside it, in this order: (1) CAS-update `playbook_v2_runs` `SET ... snapshot_version = :next WHERE run_id = :id AND snapshot_version = :expected`, where `:expected` is `snapshot.version` as loaded and `:next` is `snapshot.version + 1`; (2) insert one `playbook_step_receipts` row; (3) apply `wait_changes` (registrations and clears) on the same connection.
- Returns the snapshot with `version` incremented. **Callers must use the returned object**; the argument is not mutated (`RunSnapshot` is frozen).
- Raises `SnapshotVersionConflict(run_id, expected, actual)` when the CAS matches zero rows. The whole block rolls back — no receipt, no wait change.
- Raises `DuplicateAttempt(run_id, step_id, iteration, attempt)` when the receipt insert violates `uq_playbook_step_receipts_attempt`. This is the idempotency fence: a replayed attempt after an ambiguous interruption is rejected by the database, not by an in-memory guard.
- Raises `StateLimitExceeded(run_id, step_id, kind, size, limit)` **before** opening the transaction when the serialized snapshot exceeds `playbooks.v2_max_snapshot_bytes` or the receipt's bound result exceeded `playbooks.v2_max_result_bytes`.
- Never emits an event, never touches the bus, never logs the snapshot body.

`request_cancel` is the same CAS on `snapshot_version`, setting `cancel_requested_at`/`cancel_reason`/`cancel_requested_by` and moving `lifecycle` `running → cancelling` or `paused → cancelled` (design spec: "A paused run cancels immediately"). It writes no receipt: the acknowledgement receipt is Package 4's, written through `commit_boundary`.

### 4.5 `WaitRepository` — locked signatures

Same module split: protocol in `src/playbooks/waits.py`, implementation in the same mixin as `RunRepository` (one mixin, because `commit_boundary` must apply wait changes on its own connection).

```python
class WaitRepository(Protocol):
    async def register(
        self, wait: WaitSpec, snapshot_version: int, *, conn: AsyncConnection | None = None
    ) -> str: ...

    async def claim_for_event(
        self, event: MatchableEvent, *, now: float, limit: int = 100
    ) -> list[WaitClaim]: ...

    async def expire_due(self, now: float, *, limit: int = 100) -> list[WaitClaim]: ...

    async def clear_for_run(
        self, run_id: str, *, conn: AsyncConnection | None = None
    ) -> int: ...

    async def list_active(self, run_id: str) -> list[WaitSpec]: ...
```

- `conn` is the atomicity seam. When `commit_boundary` applies `wait_changes` it passes **its own** connection, so registration and the snapshot advance commit or roll back together. When `conn is None` the method opens its own `immediate()` block.
- `register` requires `snapshot_version` to equal the version the boundary is *writing* (`snapshot.version + 1`). The row therefore records the exact snapshot that is suspended on it; a resume that finds `playbook_waits.snapshot_version != playbook_v2_runs.snapshot_version` refuses and reports `wait_version_mismatch` rather than resuming into a state that has moved on.
- `claim_for_event` is one `immediate()` block: select active waits whose `event_type` matches and whose `match` predicate is satisfied, then CAS each with `UPDATE playbook_waits SET state='claimed', claimed_event_id=:eid, claimed_at=:now WHERE wait_id=:wid AND state='active'`. Only rows whose update affected one row are returned. Two concurrent dispatches of the same event therefore produce exactly one claim per wait.
- `expire_due` is the same CAS with `state='expired'` for `deadline_at <= now`.
- `clear_for_run` sets `state='cleared'` for every active wait of a run (used when a run terminates or a step advances past its wait).

**The race the design spec names — "an event cannot be lost between registration and suspension" — is closed by construction:** there is no interval in which a run is suspended and its wait is not visible, because both are one transaction. The complementary direction (a wait visible while the run still shows `running`) is harmless: a claim only records `claimed_event_id`; resuming is Package 4's job and re-reads the snapshot under its own CAS.

### 4.6 Field names Package 5 will project

`ArtifactRef` (§4.2) is field-for-field `ArtifactRefDTO`. The activation row's `playbook_id`, `scope`, `scope_identifier`, `enabled`, `active_artifact_sha256`, `health`, `reasons`, `activated_at`, `activated_by` are field-for-field `ActivationStateDTO` minus its two computed counts. The pending-event row's `pending_event_id`, `playbook_id`, `event_type`, `event`, `received_at`, `reason`, `attempts`, `last_error`, `expires_at` are field-for-field `PendingEventDTO`. Renaming any of them silently breaks Package 5's already-written plan.

---

## 5. Artifact storage

### 5.1 Canonical bytes and the hash — owned by Package 2

**This package does not define a canonicalizer.** Package 2's child plan (`...-typed-model-compiler.md` §4.7) checks the following into `src/playbooks/definition.py` and states the constraint on this package explicitly:

```python
def canonical_bytes(d: PlaybookDefinition) -> bytes:
    return json.dumps(
        d.model_dump(mode="json", exclude_none=True),
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")

def artifact_sha256(d) -> Sha256:            # "sha256:" + sha256(canonical_bytes(d)).hexdigest()
def source_digest(markdown: str) -> Sha256   # over PlaybookCompiler._normalize_content output
def contract_fingerprint(d) -> Sha256        # over canonical JSON of d.compiled_against.commands
COMPILER_BUILD: str                          # hand-bumped constant, never derived from git
```

`ArtifactStore` imports all five. Consequences this package must honour:

- **`exclude_none=True` is load-bearing** and is safe only because Package 2's model treats absent and `null` as the same value (its §4.1 invariant 2, pinned by `test_absent_and_null_are_the_same_model`). Nothing here may re-dump with `exclude_none=False` "to be safe" — that produces different bytes and therefore a different hash for the same definition.
- **The artifact never round-trips through a JSON or JSONB column.** PostgreSQL `jsonb` reorders keys, strips whitespace and collapses duplicates; a stored-then-reloaded artifact would no longer hash to its own name. The bytes live in the content-addressed file; the database keeps the digest, the path and a validation summary as `TEXT` (§6.1). This is Package 2's §4.7 constraint, restated here because this package is where it can be violated.
- `ArtifactRef.contract_fingerprint`, `.source_digest` and `.compiler_build` are **copied** from `contract_fingerprint(d)`, the compiler's `source_digest(markdown)` and `COMPILER_BUILD`. `put()` takes them as keyword arguments rather than computing them, because `source_digest` is over the Markdown the caller compiled and the store never sees it.

**Fallback while Package 2 is unmerged.** If §3.2 reports `src.playbooks.definition` missing, `artifact_store.py` defines a module-private `_canonical_bytes` with **exactly** the body above and a module-level `# TODO(P2): delete when src/playbooks/definition.canonical_bytes exists` marker, and `tests/test_playbook_artifact_store.py::test_canonical_bytes_match_package_two` is written now as an `xfail(strict=False)` that flips to a hard equality assertion in the reconciliation commit (J-6). Two canonicalizers is two hash functions, and the second one silently invalidates every stored artifact — the test exists so that can only happen loudly.

### 5.2 `ArtifactStore` — the write path

`src/playbooks/artifact_store.py`:

```python
class ArtifactStore:
    def __init__(self, compiled_root: str, *, max_artifact_bytes: int = 1_048_576) -> None: ...
    def put(self, definition: PlaybookDefinitionT, *, source_digest: str,
            contract_fingerprint: str, profile_fingerprint: str,
            compiler_build: str, version: int = 0) -> ArtifactRef: ...
    def load(self, artifact_sha256: str) -> PlaybookDefinitionT: ...
    def path_for(self, artifact_sha256: str) -> str: ...
    def exists(self, artifact_sha256: str) -> bool: ...
    def delete(self, artifact_sha256: str) -> bool: ...
```

`put` in order:

1. `data = canonical_bytes(definition)`; refuse with `ArtifactTooLarge` if `len(data) > max_artifact_bytes`.
2. `sha = artifact_sha256(data)`; `path = {compiled_root}/artifacts/{sha[7:]}.json`.
3. `os.makedirs(dirname, mode=0o700, exist_ok=True)`.
4. If `path` exists: read it, compare bytes. Equal → return the ref without writing (a re-put of identical content is a no-op). Different → raise `ArtifactHashCollision(sha, path)`. A SHA-256 collision is not the realistic cause; a hand-edited artifact file is, and that is exactly what must never be silently accepted.
5. Write `{sha[7:]}.json.tmp-{os.getpid()}-{uuid4().hex}` in the **same directory** via `os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)`, `os.write`, `os.fsync(fd)`, close.
6. `os.replace(tmp, path)` — atomic within a filesystem, and the temp file is a sibling so it always is one.
7. `fsync` the **directory** fd so the rename itself is durable.
8. Re-read `path` and re-hash. Mismatch → unlink and raise `ArtifactVerificationFailed`. This is the roadmap's "verify after write".
9. Return `ArtifactRef(...)`.

On any exception after step 5, unlink the temp file in a `finally`. A crash between 5 and 6 leaves an unreferenced `*.tmp-*` file; the retention sweep (§12.2) removes temp files older than one hour. A crash between 6 and the database insert leaves an unreferenced immutable artifact — exactly the design spec's "A crash can leave an unreferenced immutable file for garbage collection; it cannot expose a half-written active artifact."

`load` in order: validate `artifact_sha256` against `SHA256_RE` **before** touching the filesystem (this is the entire path-traversal defense — the filename is 64 hex characters, never anything caller-supplied); read; re-hash and compare, raising `ArtifactVerificationFailed` on mismatch; parse with the strict model, letting `ValidationError` propagate. **Order matters: hash first, parse second.** Parsing an unverified file first would run the strict loader over attacker-controlled bytes for no benefit.

### 5.3 What the store does not do

No `enabled`, no health, no "latest", no scope directories, no compaction. Two artifacts of the same playbook are two files with no relationship the store can see; the ordering lives in `playbook_artifacts.version` and the pointer lives in `playbook_activations`.

---

## 6. Database schema

All six tables are added to `src/database/tables.py` after the existing `playbook_runs` block (`src/database/tables.py:958`), in the order below. Every timestamp is `Float` POSIX seconds, matching the rest of the file. Every JSON payload is `Text` holding canonical JSON, matching `playbook_runs.trigger_event`. Booleans use `Boolean` with `false()` / `true()` server defaults — never `"0"`, which is the `DatatypeMismatchError` that took CI down in revision `33bdb059ceff` (`tests/test_migration_postgres_upgrade_head.py:12`).

### 6.1 `playbook_artifacts`

```python
playbook_artifacts = Table(
    "playbook_artifacts",
    metadata,
    Column("artifact_sha256", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column("scope", Text, nullable=False, server_default="'system'"),
    Column("scope_identifier", Text, nullable=False, server_default="''"),
    Column("schema_generation", Integer, nullable=False, server_default="2"),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("source_digest", Text, nullable=False),
    Column("contract_fingerprint", Text, nullable=False),
    Column("profile_fingerprint", Text, nullable=False, server_default="''"),
    Column("compiler_build", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False, server_default="0"),
    Column("validation", Text, nullable=False, server_default="'{}'"),
    Column("compiled_at", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    CheckConstraint(
        "scope IN ('system', 'project', 'agent_type', 'supervisor')",
        name="ck_playbook_artifacts_scope",
    ),
    Index("idx_playbook_artifacts_playbook", "playbook_id", "version"),
    Index("idx_playbook_artifacts_source", "source_digest"),
    Index("idx_playbook_artifacts_created", "created_at"),
)
```

`scope` values are `src/vault_manager.py:44`'s `Scope` literal exactly (`system | supervisor | agent_type | project`). `scope_identifier` is `NOT NULL DEFAULT ''` rather than nullable — see §6.2. `validation` holds the compiler's validation summary (`{"diagnostics": 0, "questions": [], "warnings": 0}`), not the diagnostics themselves. **No column here holds the artifact body**, and `path`/`validation` are `Text` rather than a JSON/JSONB type on purpose: PostgreSQL `jsonb` normalization would break hash verification for anything that round-tripped through it (§5.1).

### 6.2 `playbook_activations`

```python
playbook_activations = Table(
    "playbook_activations",
    metadata,
    Column("activation_id", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column("scope", Text, nullable=False, server_default="'system'"),
    Column("scope_identifier", Text, nullable=False, server_default="''"),
    Column(
        "active_artifact_sha256",
        Text,
        ForeignKey("playbook_artifacts.artifact_sha256", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("enabled", Boolean, nullable=False, server_default=false()),
    Column("health", Text, nullable=False, server_default="'disabled'"),
    Column("reasons", Text, nullable=False, server_default="'[]'"),
    Column("activated_at", Float, nullable=True),
    Column("activated_by", Text, nullable=True),
    Column("updated_at", Float, nullable=False),
    CheckConstraint(
        "health IN ('ready', 'question_required', 'invalid', 'disabled', "
        "'stale_contract', 'unavailable')",
        name="ck_playbook_activations_health",
    ),
    UniqueConstraint(
        "playbook_id", "scope", "scope_identifier",
        name="uq_playbook_activations_scope",
    ),
    Index("idx_playbook_activations_health", "health"),
)
```

**Why `scope_identifier` is `NOT NULL DEFAULT ''`:** a nullable column inside a `UNIQUE` constraint does not constrain anything on either SQLite or PostgreSQL — `NULL` is distinct from `NULL`, so two system-scoped activations of the same playbook would both be legal. `''` makes the constraint total. Readers translate `''` back to `None` at the DTO boundary (Package 5's `scope_identifier: str | None`).

`enabled` and `health` are deliberately separate columns: the roadmap requires "activation enablement separately from artifact validity". `enabled=True, health='stale_contract'` is a real and important state — the operator wants this playbook running, and it cannot run until it is rebuilt. `active_artifact_sha256` is nullable so a disabled playbook that has never been activated still has a row.

### 6.3 `playbook_v2_runs`

```python
playbook_v2_runs = Table(
    "playbook_v2_runs",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column(
        "artifact_sha256",
        Text,
        ForeignKey("playbook_artifacts.artifact_sha256", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("rule_id", Text, nullable=False),
    Column("lifecycle", Text, nullable=False, server_default="'running'"),
    Column("mode", Text, nullable=False, server_default="'live'"),
    Column("current_step_id", Text, nullable=True),
    Column("snapshot_version", Integer, nullable=False, server_default="0"),
    Column("snapshot", Text, nullable=False, server_default="'{}'"),
    Column("snapshot_bytes", Integer, nullable=False, server_default="0"),
    Column("event_type", Text, nullable=False, server_default="''"),
    Column("event_id", Text, nullable=True),
    Column("dispatch_id", Text, nullable=True),
    Column("parent_run_id", Text, nullable=True),
    Column("parent_step_id", Text, nullable=True),
    Column("deadline_at", Float, nullable=True),
    Column("cancel_requested_at", Float, nullable=True),
    Column("cancel_requested_by", Text, nullable=True),
    Column("cancel_reason", Text, nullable=True),
    Column("summary", Text, nullable=False, server_default="''"),
    Column("error", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("started_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("completed_at", Float, nullable=True),
    CheckConstraint(
        "lifecycle IN ('running', 'paused', 'cancelling', 'completed', "
        "'failed', 'timed_out', 'cancelled')",
        name="ck_playbook_v2_runs_lifecycle",
    ),
    CheckConstraint(
        "mode IN ('live', 'dry_run', 'shadow')", name="ck_playbook_v2_runs_mode"
    ),
    Index(
        "uq_playbook_v2_runs_dispatch_rule",
        "dispatch_id", "rule_id",
        unique=True,
        sqlite_where=text("dispatch_id IS NOT NULL"),
        postgresql_where=text("dispatch_id IS NOT NULL"),
    ),
    Index("idx_playbook_v2_runs_playbook", "playbook_id", "started_at"),
    Index("idx_playbook_v2_runs_lifecycle", "lifecycle"),
    Index("idx_playbook_v2_runs_artifact", "artifact_sha256"),
)
```

The seven-value `lifecycle` check is the design spec's single lifecycle enum, verbatim and in its order. The partial unique index on `(dispatch_id, rule_id)` is the "one matching event may create multiple rule runs, but each run executes exactly one rule" invariant made unforgeable: one dispatch of one event produces at most one run per rule, and a retried dispatch cannot duplicate them. It mirrors the existing `uq_playbook_runs_pb_event` idiom (`src/database/tables.py:945`), including the dialect-specific `where` clauses.

### 6.4 `playbook_step_receipts`

```python
playbook_step_receipts = Table(
    "playbook_step_receipts",
    metadata,
    Column("receipt_id", Text, primary_key=True),
    Column(
        "run_id", Text,
        ForeignKey("playbook_v2_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("artifact_sha256", Text, nullable=False),
    Column("rule_id", Text, nullable=False),
    Column("step_id", Text, nullable=False),
    Column("step_kind", Text, nullable=False),
    Column("iteration", Integer, nullable=False, server_default="-1"),
    Column("attempt", Integer, nullable=False, server_default="1"),
    Column("idempotency_key", Text, nullable=False),
    Column("snapshot_version", Integer, nullable=False, server_default="0"),
    Column("contract_fingerprint", Text, nullable=False, server_default="''"),
    Column("principal", Text, nullable=False, server_default="'{}'"),
    Column("inputs", Text, nullable=False, server_default="'{}'"),
    Column("result", Text, nullable=False, server_default="'{}'"),
    Column("outcome", Text, nullable=False),
    Column("selected_transition", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("tokens_in", Integer, nullable=False, server_default="0"),
    Column("tokens_out", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=True),
    Column("wait_id", Text, nullable=True),
    Column("timed_out", Boolean, nullable=False, server_default=false()),
    Column("cancelled_at", Float, nullable=True),
    Column("started_at", Float, nullable=False),
    Column("completed_at", Float, nullable=True),
    Column("duration_ms", Integer, nullable=False, server_default="0"),
    CheckConstraint(
        "outcome IN ('success', 'failure', 'skipped', 'timeout', 'cancelled', "
        "'operator_decision_required')",
        name="ck_playbook_step_receipts_outcome",
    ),
    UniqueConstraint(
        "run_id", "step_id", "iteration", "attempt",
        name="uq_playbook_step_receipts_attempt",
    ),
    Index("idx_playbook_step_receipts_run", "run_id", "started_at"),
    Index("idx_playbook_step_receipts_key", "idempotency_key"),
)
```

`iteration` is `-1` for a step outside a loop and `0..n` inside one. `selected_transition` is the string `f"{rule_id}::{step_id}::{outcome}"` — Package 5's overlay joins graph edges on exactly that id (`2026-09-01-playbook-v2-graph-api-ui.md` §16, "Package 4 must record `selected_transition` on every receipt"). Recording it here rather than leaving it to Package 4 means the column exists and is indexed by the time the engine needs it.

### 6.5 `playbook_waits`

```python
playbook_waits = Table(
    "playbook_waits",
    metadata,
    Column("wait_id", Text, primary_key=True),
    Column(
        "run_id", Text,
        ForeignKey("playbook_v2_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("step_id", Text, nullable=False),
    Column("iteration", Integer, nullable=False, server_default="-1"),
    Column("kind", Text, nullable=False),
    Column("event_type", Text, nullable=False, server_default="''"),
    Column("correlation_key", Text, nullable=False, server_default="''"),
    Column("match", Text, nullable=False, server_default="'{}'"),
    Column("deadline_at", Float, nullable=True),
    Column("snapshot_version", Integer, nullable=False),
    Column("state", Text, nullable=False, server_default="'active'"),
    Column("claimed_event_id", Text, nullable=True),
    Column("claimed_at", Float, nullable=True),
    Column("created_at", Float, nullable=False),
    CheckConstraint(
        "kind IN ('event', 'timer', 'human', 'agent_task')",
        name="ck_playbook_waits_kind",
    ),
    CheckConstraint(
        "state IN ('active', 'claimed', 'expired', 'cleared')",
        name="ck_playbook_waits_state",
    ),
    Index(
        "uq_playbook_waits_active_step",
        "run_id", "step_id", "iteration",
        unique=True,
        sqlite_where=text("state = 'active'"),
        postgresql_where=text("state = 'active'"),
    ),
    Index("idx_playbook_waits_match", "state", "event_type"),
    Index("idx_playbook_waits_deadline", "state", "deadline_at"),
)
```

The partial unique index is the "one live wait per step instance" invariant: a resumed run cannot register a second wait for a step that is already waiting, so a duplicated resume produces an `IntegrityError` rather than two claimable rows. `correlation_key` is a canonical digest of the matcher, stored for operator search; matching itself uses `event_type` plus the `match` predicate evaluated in Python over the candidate rows (SQLite has no JSON operators in this codebase's SQLAlchemy Core usage, and the active-wait set is small and index-narrowed by `(state, event_type)`).

### 6.6 `playbook_pending_events`

```python
playbook_pending_events = Table(
    "playbook_pending_events",
    metadata,
    Column("pending_event_id", Text, primary_key=True),
    Column("playbook_id", Text, nullable=False),
    Column("scope", Text, nullable=False, server_default="'system'"),
    Column("scope_identifier", Text, nullable=False, server_default="''"),
    Column("event_type", Text, nullable=False),
    Column("event", Text, nullable=False, server_default="'{}'"),
    Column("event_id", Text, nullable=True),
    Column("dedup_key", Text, nullable=False, server_default="''"),
    Column("reason", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text, nullable=True),
    Column("received_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("resolved_at", Float, nullable=True),
    Column("resolved_by", Text, nullable=True),
    Column("resolution", Text, nullable=True),
    CheckConstraint(
        "reason IN ('stale_contract', 'invalid_artifact', 'disabled', "
        "'unavailable', 'question_required')",
        name="ck_playbook_pending_events_reason",
    ),
    CheckConstraint(
        "resolution IS NULL OR resolution IN ('dispatched', 'discarded', 'expired')",
        name="ck_playbook_pending_events_resolution",
    ),
    Index(
        "uq_playbook_pending_events_dedup",
        "playbook_id", "dedup_key",
        unique=True,
        sqlite_where=text("resolved_at IS NULL AND dedup_key <> ''"),
        postgresql_where=text("resolved_at IS NULL AND dedup_key <> ''"),
    ),
    Index("idx_playbook_pending_events_playbook", "playbook_id", "received_at"),
    Index("idx_playbook_pending_events_expiry", "expires_at"),
)
```

`resolved_at` / `resolved_by` / `resolution` are the operator-audit columns Package 5's §16 asked this package to ship; shipping them here deletes Package 5's §9 migration entirely. Replay order is `ORDER BY received_at, pending_event_id` — the design spec's "replay in arrival order under rule-level deduplication".

### 6.7 Adapter registration

`PlaybookArtifactQueryMixin` (Task A) and `PlaybookRunQueryMixin` (Task B) are each imported and added to the base list of **both** `SQLiteDatabaseAdapter` (`src/database/adapters/sqlite.py:62`) and `PostgreSQLDatabaseAdapter` (`src/database/adapters/postgresql.py:62`), directly after `PlaybookQueryMixin` (`postgresql.py:53`). Two branches editing the same two import blocks is the one predictable textual conflict in this package; each task adds **exactly one import line and one base-class line per adapter**, alphabetically after `PlaybookQueryMixin`, which keeps the conflict to a two-line resolution.

### 6.8 `docs/specs/database.md`

`tests/test_docs_sync.py::TestDatabaseSpecSync::test_every_table_in_code_is_documented` fails on the first `tables.py` edit that lands without its doc section, and `::test_documented_column_names_match_code` fails if a documented column name does not exist in code. Each migration commit therefore carries its ``### Table: `name` `` sections, inserted after the existing `### Table: playbook_runs` section (`docs/specs/database.md:743`), one row per column in the table order above, plus a one-paragraph preamble naming the design spec section. **This is a commit-blocking requirement, not a nicety** — the suite runs in CI's default job.

---

## 7. Alembic migrations

### 7.1 `a3f1c0de0001_playbook_v2_artifacts.py` (Task A)

```python
"""playbook v2: content-addressed artifacts and explicit activations

Revision ID: a3f1c0de0001
Revises: d3e7b1c9a204
Create Date: 2026-09-01

Additive: two new tables, no existing table touched.  Ordered first in the
Package 3 chain because playbook_v2_runs.artifact_sha256 references
playbook_artifacts (roadmap section 7 / child plan section 4.3).
"""

from alembic import op
import sqlalchemy as sa

revision = "a3f1c0de0001"
down_revision = "d3e7b1c9a204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "playbook_artifacts",
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        # ... every column of section 6.1, same order, same types ...
        sa.PrimaryKeyConstraint("artifact_sha256"),
        sa.CheckConstraint(
            "scope IN ('system', 'project', 'agent_type', 'supervisor')",
            name="ck_playbook_artifacts_scope",
        ),
    )
    op.create_index("idx_playbook_artifacts_playbook", "playbook_artifacts",
                    ["playbook_id", "version"])
    op.create_index("idx_playbook_artifacts_source", "playbook_artifacts", ["source_digest"])
    op.create_index("idx_playbook_artifacts_created", "playbook_artifacts", ["created_at"])

    op.create_table(
        "playbook_activations",
        # ... every column of section 6.2 ...
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(
            ["active_artifact_sha256"], ["playbook_artifacts.artifact_sha256"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("activation_id"),
        sa.UniqueConstraint("playbook_id", "scope", "scope_identifier",
                            name="uq_playbook_activations_scope"),
        sa.CheckConstraint(
            "health IN ('ready', 'question_required', 'invalid', 'disabled', "
            "'stale_contract', 'unavailable')",
            name="ck_playbook_activations_health",
        ),
    )
    op.create_index("idx_playbook_activations_health", "playbook_activations", ["health"])


def downgrade() -> None:
    op.drop_index("idx_playbook_activations_health", table_name="playbook_activations")
    op.drop_table("playbook_activations")
    op.drop_index("idx_playbook_artifacts_created", table_name="playbook_artifacts")
    op.drop_index("idx_playbook_artifacts_source", table_name="playbook_artifacts")
    op.drop_index("idx_playbook_artifacts_playbook", table_name="playbook_artifacts")
    op.drop_table("playbook_artifacts")
```

### 7.2 `b3f2c0de0002_playbook_v2_run_state.py` (Task B)

Creates `playbook_v2_runs` then `playbook_step_receipts` (FK order), with the partial unique index written by hand — autogenerate renders partial indexes badly (`migrations/versions/93a8a9e48fb8_substrate_overhaul_schema.py:51`):

```python
_DISPATCH_WHERE = "dispatch_id IS NOT NULL"

op.create_index(
    "uq_playbook_v2_runs_dispatch_rule",
    "playbook_v2_runs",
    ["dispatch_id", "rule_id"],
    unique=True,
    sqlite_where=sa.text(_DISPATCH_WHERE),
    postgresql_where=sa.text(_DISPATCH_WHERE),
)
```

`downgrade` drops `playbook_step_receipts` first (it has the FK), then the run table's indexes, then the run table.

### 7.3 `b3f2c0de0003_playbook_v2_waits.py` (Task B)

Creates `playbook_waits` (FK to `playbook_v2_runs`) and `playbook_pending_events` (no FK — a pending event exists precisely because no run does). Both partial unique indexes are hand-written with matching `sqlite_where` / `postgresql_where`, following `migrations/versions/90d653cbed1d_add_partial_unique_index_on_open_gates_.py:24`.

### 7.4 SQLite and PostgreSQL notes

- **Booleans:** `sa.Boolean()` with `sa.false()` / `sa.true()`. Never `server_default="0"` — PostgreSQL raises `DatatypeMismatchError`, which is the failure `tests/test_migration_postgres_upgrade_head.py` exists to catch.
- **Timestamps:** `sa.Float()` everywhere, POSIX seconds. No `DateTime`; the codebase has none for these domains and mixing would break `PlaybookRunSummary.started_at`'s float contract.
- **Text:** `sa.Text()`, never `sa.String(n)`. SQLite ignores lengths; PostgreSQL enforces them, so a length is a future truncation bug.
- **Partial indexes:** both `sqlite_where` and `postgresql_where` on every partial index, with identical predicate strings, and the same pair repeated on `drop_index` in `downgrade` (SQLAlchemy needs them to render the drop on SQLite's batch path).
- **`CHECK` constraints:** named. An unnamed check cannot be dropped by a downgrade on PostgreSQL.
- **No `batch_alter_table`:** these revisions only create and drop whole tables. Batch mode exists for SQLite `ALTER`, and using it here would only obscure the DDL.
- **FKs are not enforced on SQLite by default in this codebase** (`PRAGMA foreign_keys` is only turned on inside specific tests, e.g. `tests/test_migration_agent_flock.py:22`). `ondelete="RESTRICT"` on `active_artifact_sha256` and `artifact_sha256` is therefore documentation on SQLite and enforcement on PostgreSQL. **Retention must not rely on it** — §12.1's delete query does its own reference check, and `tests/test_playbook_artifact_store.py::test_retention_never_deletes_a_referenced_artifact` runs on both backends.
- **Cascade deletes:** `playbook_step_receipts.run_id` and `playbook_waits.run_id` are `ON DELETE CASCADE` for PostgreSQL; the retention sweep deletes children explicitly first so SQLite behaves identically.

### 7.5 The parallel-branch operator hazard

`src/database/engine.py:218` `_preflight_check_alembic_version` raises a `RuntimeError` when `alembic_version` names a revision the current checkout lacks. A dev daemon whose database was migrated on `feature/playbook-v2-pkg3-runstate` (stamped `b3f2c0de0003`) will refuse to start on `feature/playbook-v2-pkg3-artifacts`, which only knows `a3f1c0de0001`. This is correct behavior and must not be "fixed". The two supported recoveries, in the plan so neither task wastes an hour on it:

1. `python -m alembic downgrade a3f1c0de0001` **before** switching branches, or
2. point the other branch at its own database: `AGENT_QUEUE_DB_URL=sqlite+aiosqlite:///$PWD/.aq-pkg3.db`.

Tests are unaffected: every suite builds a fresh database in `tmp_path` (or a per-xdist-worker PostgreSQL database, `tests/pg_dsn.py`), and the SQLite schema cache keys on the migration files themselves (`src/database/engine.py:64`).

---

## 8. Run state

### 8.1 `RunSnapshot` — `src/playbooks/run_state.py`

Frozen dataclass; `version` is the optimistic-concurrency token; the body is what `playbook_v2_runs.snapshot` holds as canonical JSON.

```python
@dataclass(frozen=True, slots=True)
class LoopFrame:
    step_id: str
    item_binding: str
    collection_digest: str      # sha256 of the canonical collection, pins resumption
    index: int
    total: int
    partial: tuple[Any, ...] = ()
    resume_step_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run_id: str
    playbook_id: str
    artifact_sha256: str
    rule_id: str
    lifecycle: RunLifecycle = RunLifecycle.RUNNING
    mode: str = "live"
    version: int = 0
    current_step_id: str | None = None
    event: Mapping[str, Any] = field(default_factory=dict)     # validated trigger event
    context: Mapping[str, Any] = field(default_factory=dict)   # validated context values
    bindings: Mapping[str, Any] = field(default_factory=dict)  # declared step outputs only
    sensitive: Mapping[str, Any] = field(default_factory=dict) # handle -> value, never receipted
    loop: LoopFrame | None = None
    wait: WaitSpec | None = None
    budget: RunBudget = field(default_factory=RunBudget)
    agent_task_ids: tuple[str, ...] = ()
    llm_turns: tuple[Mapping[str, Any], ...] = ()
    operator_decision: OperatorDecision | None = None
    cancel_requested_at: float | None = None
    deadline_at: float | None = None
    started_at: float = 0.0
    updated_at: float = 0.0
```

`RunLifecycle` is a `str`-valued `Enum` with the seven design-spec values in the design-spec order, and `LEGAL_TRANSITIONS: dict[RunLifecycle, frozenset[RunLifecycle]]` beside it. `commit_boundary` validates the transition before the CAS and raises `IllegalLifecycleTransition` — a state machine in one dict, checked in one place. V1's equivalent is `VALID_PLAYBOOK_RUN_TRANSITIONS` (`src/playbooks/state_machine.py`); the V2 set has seven states to V1's six — `cancelling` is new, and it is what makes "signal, then acknowledge" survive a restart.

`bindings` holds **only** validated declared outputs. Nothing writes a raw handler dict into it; that is the design spec's "A bound result contains only the step's validated declared output, not an arbitrary handler dictionary", and `tests/test_playbook_run_repository.py::test_bindings_reject_undeclared_keys` pins it.

### 8.2 Size limits and the oversize decision

Two limits, both config-backed (§12.3), both checked before any write:

| Limit | Default | Checked in | On breach |
|---|---:|---|---|
| one bound result | 256 KiB | `run_state.check_result_size(step_id, value)` | `StateLimitExceeded(kind="result")` |
| one snapshot | 4 MiB | `run_state.serialize_snapshot(snapshot)` | `StateLimitExceeded(kind="snapshot")` |

**Decision: reject, do not externalize.** The roadmap requires this plan to choose. Rejection is chosen because externalization would add a second content store with its own GC, its own redaction boundary, and its own failure mode *inside the transaction that must stay atomic* — and because a step that produces 256 KiB of JSON is a modeling error the operator should see, not a storage problem to absorb. The rejection is explicit and legible:

- the failing step's receipt is written with `outcome="failure"`, `error_code="state_limit_exceeded"`, and an `error` naming the step, the byte count and the limit;
- the run moves to `failed`;
- **the oversized value never reaches the database** — `check_result_size` runs on the in-memory value before it is bound, so the failure path stores the size, not the payload;
- nothing is truncated. The roadmap's "never silently truncated into an invalid binding" is satisfied by never truncating at all.

Package 4 may add an `externalize` policy later; it would be a new step-contract field and a new package, not a silent change here.

### 8.3 Sensitive values

Contracts mark sensitive fields (Package 1). This package provides the storage half:

- a sensitive value is replaced in `bindings` by an opaque handle string `f"sensitive:{sha256(run_id + '|' + path + '|' + canonical(value)).hexdigest()[:32]}"`;
- the value itself lives in `snapshot.sensitive[handle]`, which is written only to `playbook_v2_runs.snapshot` and returned only by `RunRepository.load_run` in-process;
- **no read path in this package returns `sensitive`**; `RunSnapshot.redacted()` drops it and is what any future API projection must use;
- receipts store the handle, never the value.

`receipts.project_receipt` is default-deny and takes Package 1's contract classification directly — `receipt_projection` is an allow-list over the result model, `sensitive_args` and `sensitive_result_fields` are its redaction sets, and Package 1's plan states that Package 3 is the consumer of all three:

```python
def project_receipt(
    inputs: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    receipt_projection: Sequence[str] = (),          # ExecutionContract.receipt_projection
    sensitive_args: Collection[str] = (),            # ExecutionContract.sensitive_args
    sensitive_result_fields: Collection[str] = (),   # ExecutionContract.sensitive_result_fields
    input_projection: Sequence[str] | None = None,   # see section 21 — no P1 field yet
) -> tuple[dict[str, Any], dict[str, Any]]:
```

With every argument at its default — which is what Package 3's own tests pass — nothing is projected: both dicts come back as `{"__redacted__": <key count>}`. A key in `receipt_projection` is copied unless it is also in `sensitive_result_fields`, in which case it becomes its sensitive handle. Package 4 supplies the contract's values; there is no path by which a caller can widen the projection beyond what the contract declares.

`tests/test_playbook_run_repository.py::test_sensitive_value_never_lands_in_a_receipt` writes a run whose result contains a marked secret and greps the serialized receipt row for the plaintext.

---

## 9. Receipts and attempt identity

### 9.1 The idempotency key

```python
def idempotency_key(run_id: str, step_id: str, iteration: int, attempt: int) -> str:
    """Deterministic attempt identity (roadmap Package 3: run, step, loop
    iteration, attempt)."""
    loop_part = "-" if iteration < 0 else str(iteration)
    return f"{run_id}:{step_id}:{loop_part}:{attempt}"
```

**Deviation from the design spec, deliberate:** the spec's prose gives `<run_id>:<step_id>:<attempt>`. That key collides across iterations of the same step inside a `ForEachStep` — two iterations calling the same side-effecting command would present the same key and the second would be suppressed as a duplicate. The roadmap's Package 3 requirement ("Derive attempt idempotency from run, step, loop iteration, and attempt number") is the stricter and correct statement, so the four-part key wins and the spec's three-part form is read as a simplification of the non-loop case (`-` in the loop position). Recorded as an amendment in §20.

The database enforces the same identity independently via `uq_playbook_step_receipts_attempt (run_id, step_id, iteration, attempt)`, so a replayed attempt fails on insert even if a caller constructs the key by hand.

### 9.2 `StepReceipt`, `RunBudget`, `OperatorDecision` — locked

Frozen dataclasses in `src/playbooks/receipts.py` (`StepReceipt`) and `src/playbooks/run_state.py` (the other two). Field names are the column names of §6.4 exactly, so persisting is a `dataclasses.asdict` plus two `json.dumps`, not a mapping layer.

```python
@dataclass(frozen=True, slots=True)
class StepReceipt:
    receipt_id: str
    run_id: str
    artifact_sha256: str
    rule_id: str
    step_id: str
    step_kind: str
    outcome: str                       # the six-value CHECK of section 6.4
    started_at: float
    snapshot_version: int
    iteration: int = -1
    attempt: int = 1
    idempotency_key: str = ""          # filled by __post_init__ when empty
    contract_fingerprint: str = ""
    principal: Mapping[str, Any] = field(default_factory=dict)   # redacted
    inputs: Mapping[str, Any] = field(default_factory=dict)      # redacted
    result: Mapping[str, Any] = field(default_factory=dict)      # redacted
    selected_transition: str | None = None
    error: str | None = None
    error_code: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    wait_id: str | None = None
    timed_out: bool = False
    cancelled_at: float | None = None
    completed_at: float | None = None
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class RunBudget:
    llm_calls: int = 0
    total_tokens: int = 0
    max_total_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class OperatorDecision:
    step_id: str
    attempt: int
    reason: str
    raised_at: float
    options: tuple[str, ...] = ("accept_outcome", "retry", "fail", "cancel")
```

`RunBudget` and `OperatorDecision` are field-for-field Package 5's `RunBudgetDTO` and `OperatorDecisionDTO`.

### 9.3 `operator_decision_required`

A non-retry-safe command interrupted ambiguously does **not** get replayed. `commit_boundary` writes a receipt with `outcome="operator_decision_required"`, the snapshot goes to `paused` with `operator_decision=OperatorDecision(step_id, attempt, reason, options, raised_at)`, and `options` is exactly `("accept_outcome", "retry", "fail", "cancel")` — the four resolutions the design spec allows, and the same tuple Package 5's `OperatorDecisionDTO.options` renders. Recording the operator's resolution is itself a `commit_boundary` call with a receipt whose `step_kind="operator_decision"`; Package 4 owns the command that makes it.

---

## 10. Waits and pending events

### 10.1 `WaitSpec`

```python
@dataclass(frozen=True, slots=True)
class WaitSpec:
    wait_id: str
    run_id: str
    step_id: str
    iteration: int = -1
    kind: str = "event"                       # event | timer | human | agent_task
    event_type: str = ""
    match: Mapping[str, Any] = field(default_factory=dict)   # field -> required value
    deadline_at: float | None = None

    @property
    def correlation_key(self) -> str:
        """Stable digest of (kind, event_type, match) for operator search."""
```

`match` is a flat mapping of event field path → required literal, evaluated by `waits.matches(spec, event)`. Nested paths use dots (`"task.id"`). No expressions, no callables, nothing that could execute — a durable wait predicate must be inert data, because it is read back from the database after a restart.

`WaitClaim`, `WaitChangeSet` and the event shape, all in `src/playbooks/waits.py`:

```python
@dataclass(frozen=True, slots=True)
class WaitClaim:
    wait_id: str
    run_id: str
    step_id: str
    iteration: int
    kind: str
    snapshot_version: int
    claimed_event_id: str | None      # None for an expiry claim
    claimed_at: float
    expired: bool = False


@dataclass(frozen=True, slots=True)
class WaitChangeSet:
    """What one commit boundary changes about a run's waits."""

    register: tuple[WaitSpec, ...] = ()
    clear_wait_ids: tuple[str, ...] = ()
    clear_run_waits: bool = False     # clear every active wait of the run


EMPTY_WAIT_CHANGES = WaitChangeSet()


class MatchableEvent(Protocol):
    """The only thing wait matching needs from Package 4's event object."""

    event_type: str
    event_id: str | None
    fields: Mapping[str, Any]
```

`WaitChangeSet` applies `clear_run_waits` first, then `clear_wait_ids`, then `register` — so a step that finishes one wait and opens another in the same boundary cannot trip the `uq_playbook_waits_active_step` partial unique index.

### 10.2 The restart proof

`tests/test_playbook_wait_repository.py::test_wait_survives_a_process_restart` is the exit-gate test in miniature: build repo → create run → `commit_boundary` that suspends on a wait → **close the adapter entirely and open a new one against the same file** → `claim_for_event` matches → `load_run` returns a snapshot whose `version`, `bindings` and `loop` are byte-identical to what was committed. No in-memory state is carried across the close; that is what makes it a restart rather than a reset.

### 10.3 Pending events

An event that matches an activation which is not `ready` is retained rather than dropped:

```python
async def retain_pending_event(self, *, playbook_id, scope, scope_identifier,
                               event_type, event, event_id, dedup_key, reason,
                               now, ttl_seconds) -> str | None
```

Returns the new `pending_event_id`, or `None` when the partial unique index rejects it as a duplicate of an unresolved event with the same `dedup_key` — deduplication is the index, not a pre-read (a pre-read would race). `expires_at = now + ttl_seconds` (7 days by default). `resolve_pending_event(pending_event_id, *, resolution, resolved_by, now)` CASes on `resolved_at IS NULL`, so two operators clicking "dispatch" produce one dispatch.

Capacity: `retain_pending_event` refuses with `PendingEventQuotaExceeded` past `playbooks.v2_max_pending_events_per_playbook` (default 1000) and logs one warning per playbook per minute. An unbounded retention table is a denial-of-service surface reachable by any event producer.

---

## 11. Activation and health

`src/playbooks/activation.py`:

```python
class ActivationHealth(str, Enum):
    READY = "ready"
    QUESTION_REQUIRED = "question_required"
    INVALID = "invalid"
    DISABLED = "disabled"
    STALE_CONTRACT = "stale_contract"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class HealthReason:
    code: str
    message: str
    subject: str | None = None
    expected_fingerprint: str | None = None
    actual_fingerprint: str | None = None


def evaluate_health(
    *,
    enabled: bool,
    artifact: ArtifactRef | None,
    artifact_present: bool,
    validation: Mapping[str, Any],
    current_contract_fingerprints: Mapping[str, str],
    artifact_contract_fingerprints: Mapping[str, str],
) -> tuple[ActivationHealth, tuple[HealthReason, ...]]:
```

Evaluation order — first match wins, and the order is the contract:

1. no artifact pointer, or `artifact_present` is False → `UNAVAILABLE` (`code="artifact_missing"`). The file was deleted or never written; this is the state the doctor check reports.
2. `validation["errors"]` non-empty → `INVALID` (`code="validation_failed"`, one reason per error, `subject` = the failing step id).
3. `validation["questions"]` non-empty → `QUESTION_REQUIRED` (one reason per unresolved question).
4. any command whose fingerprint in `artifact_contract_fingerprints` (the artifact's own `compiled_against.commands`) differs from `current_contract_fingerprints` (built by the caller as `{name: CONTRACTS.fingerprint(name) for name in ...}`, with an unregistered name omitted so it reads as removed), or is absent from it → `STALE_CONTRACT` (`code="command_contract_changed"` / `"command_removed"`, `subject` = the command name, both fingerprints filled in).
5. `enabled` False → `DISABLED`.
6. otherwise `READY`.

**`disabled` ranks below the fault states on purpose.** An operator who disables a broken playbook still needs to see *why* it is broken; collapsing it to `disabled` would hide the rebuild work Package 6 has to schedule. `enabled` is always readable as its own column, so nothing is lost.

Health is recomputed and persisted (a) when an activation is written, (b) by the retention/health sweep (§12.2), and (c) on demand by Package 5's read command. It is never computed inside a hot dispatch path.

`set_activation(...)` writes `enabled`, `active_artifact_sha256`, `health`, `reasons`, `activated_at`, `activated_by` in one `immediate()` block after asserting the target artifact row exists. **Compilation never calls it** — the roadmap's "Compilation never activates an artifact automatically" is preserved by there being no call site outside the explicit operator command Package 5 adds.

---

## 12. Retention, cleanup and configuration

### 12.1 What is collectable

| Object | Default horizon | Never collected while |
|---|---:|---|
| pending events (resolved or expired) | 7 days | unresolved and unexpired |
| execution receipts | 90 days | their run is not yet terminal |
| completed run snapshots | 90 days | lifecycle is not terminal, or the run is pinned |
| artifact rows + files | 90 days | referenced by an activation, referenced by any retained run, or within the newest 10 versions of its playbook |
| `*.tmp-*` files under `artifacts/` | 1 hour | — |

The artifact reference check is an explicit query, not a foreign key (§7.4): a candidate must have no row in `playbook_activations.active_artifact_sha256` and no row in `playbook_v2_runs.artifact_sha256`, and must not be among the ten highest `version` values for its `playbook_id`. Deleting the row and the file happens in that order, in one `immediate()` block for the row followed by the unlink — a crash between them leaves an unreferenced file, which the next sweep removes; the reverse order could leave a row pointing at a missing file, which would read as `unavailable` health on a playbook that was fine.

### 12.2 The sweep

`ArtifactRetentionSweeper.sweep(now) -> dict[str, int]` (mirroring `MetricsSampler.prune`, `src/metrics/sampler.py:571`) returns counts per category. It is called from `run_one_cycle` at most once an hour, as a new step 13 immediately after `_check_paused_playbook_timeouts()` (`src/orchestrator/core.py:2758`), guarded by `self._last_playbook_retention_sweep` initialized beside `self._last_log_cleanup` (`src/orchestrator/core.py:300`) and wrapped in its own `try/except` so a sweep failure cannot abort a cycle. It no-ops unless `playbooks.v2_storage_enabled` is true.

The same commit adds the doctor check `playbooks.artifact_integrity` (`src/doctor/playbook_v2_checks.py`, registered in `src/doctor/default_registry`, owner `playbook-v2`, severity `warn`): every enabled activation's `active_artifact_sha256` has a row, a file, and a file whose hash matches its name. Its fix is `None` — a missing or mutated artifact is a rebuild decision (Package 6), not something a doctor should repair.

### 12.3 Config

Added to `PlaybooksConfig` (`src/config.py:857`) as flat fields — the section is parsed by a single literal block at `src/config.py:2394`, which each field extends by one line:

```python
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
```

`PlaybooksConfig.validate()` (currently `return []`) gains: every `v2_max_*` and `v2_*_days` must be `> 0`; `v2_max_result_bytes` must be `<= v2_max_snapshot_bytes` (a result that cannot fit in a snapshot is a limit set that can never succeed); `v2_artifact_min_versions >= 1`. Errors use the existing `ConfigError("playbooks", field, message)` shape.

`"playbooks"` is in `RESTART_REQUIRED_SECTIONS` (`src/config.py:1989`) and in the section list `_SECTION_FIELDS` (`:2022`) — it is *not* hot-reloadable, so every field added here takes effect on daemon restart, and neither list needs an edit. `src/config_editor.py`'s schema is derived from the dataclass, so the new fields appear without an edit; one `FLAG_NOTES["playbooks.v2_storage_enabled"]` entry is added naming Package 7 as its removal package.

### 12.4 Feature-flag ownership

| Flag | Default | Owner | Gates | Removed in |
|---|---|---|---|---|
| `playbooks.v2_storage_enabled` | `False` | this package | the retention sweep and the doctor check only | Package 7 |

It deliberately does **not** gate the repositories or the store: gating a pure library behind a runtime flag makes tests carry the flag for no safety gain, and nothing calls those repositories until Package 4. The write surfaces an operator can reach are gated by Package 5's `playbooks.v2_activation_writes`. New tables with no writers are inert.

---

## 13. Security analysis

| Boundary | Threat | Control | Test |
|---|---|---|---|
| artifact filename | path traversal via a caller-supplied hash (`../../etc/cron.d/x`) | the only thing interpolated into a path is a string that matched `SHA256_RE`; validation happens **before** the join, in both `path_for` and `load` | `test_load_rejects_a_non_hash_identifier` (parametrized over `../`, absolute paths, `%2e%2e`, a 63-char hash, uppercase hex) |
| artifact file | an operator or a compromised agent edits an installed artifact to change execution | `load` re-hashes before parsing and raises `ArtifactVerificationFailed`; `put` refuses to overwrite differing bytes at the same hash | `test_mutated_artifact_file_fails_to_load`, `test_put_refuses_a_hash_collision` |
| artifact write | a half-written file becomes active after a crash | `O_EXCL` temp in the same directory → fsync → `os.replace` → dir fsync → re-read verify; the activation pointer is a separate database write that happens after | `test_interrupted_write_leaves_no_active_artifact` (simulates by injecting a failure between rename and verify) |
| receipts | secrets in prompts, command arguments or results leak into an operator-visible record | default-deny projection: unmarked keys are replaced by a count; sensitive values are replaced by an opaque handle; the plaintext lives only in the snapshot | `test_sensitive_value_never_lands_in_a_receipt`, `test_projection_is_default_deny` |
| snapshot | unbounded state growth as a denial of service | 4 MiB snapshot cap, 256 KiB result cap, both checked before the transaction opens | `test_oversize_result_fails_the_run_without_storing_it` |
| pending events | an event producer floods the retention table | per-playbook quota (1000) plus a 7-day TTL; refusal is an explicit error, never a silent drop | `test_pending_event_quota_is_enforced` |
| idempotency | an agent forges an attempt key to suppress or duplicate a side effect | the key contains the server-generated `run_id` and is enforced by a unique index; nothing accepts a caller-supplied key | `test_duplicate_attempt_is_rejected_by_the_database` |
| activation | a compiler agent activates its own artifact | this package exposes no command; `set_activation` requires an existing artifact row and an explicit `activated_by`, and its only caller is Package 5's operator command behind `playbooks.v2_activation_writes` | `test_no_module_in_package_three_calls_set_activation_implicitly` (a grep test over `src/playbooks/` and `src/commands/`) |
| SQL | injection through playbook ids, event types or correlation keys | SQLAlchemy Core everywhere; no `text()` with interpolation; the only `text()` uses are the two constant partial-index predicates | `ruff` + review; `test_no_raw_sql_interpolation` greps the two new query modules for f-string `text(` |
| identity | `activated_by` / `resolved_by` spoofed by a request body | both are written from the server-derived principal by the caller (Package 5), and this package's signatures make them required positional keywords so they cannot be defaulted from user input | reviewed at the Package 5 boundary |

Threats explicitly **out of scope** here: encryption at rest (the whole database is unencrypted; a per-column scheme for one subsystem would be theatre), and multi-tenant isolation between projects (scope columns are for lookup, not authorization — authorization is Package 0's `CapabilityPolicy` at dispatch).

---

## 14. Observability and operator failure behavior

- **Every error type is named and typed** — `ArtifactTooLarge`, `ArtifactHashCollision`, `ArtifactVerificationFailed`, `SnapshotVersionConflict`, `DuplicateAttempt`, `StateLimitExceeded`, `IllegalLifecycleTransition`, `WaitVersionMismatch`, `PendingEventQuotaExceeded` — all subclasses of `PlaybookStorageError` in `src/playbooks/run_state.py`, so a caller can catch the family and an operator sees a code, not a traceback string.
- **`error_code` is a column**, on both `playbook_v2_runs` and `playbook_step_receipts`, holding the exception's `code` (`snake_case` of the class name). "Why did this run fail?" is a query, not a log grep.
- **Logging:** one `logger.info` per activation change (playbook id, scope, old hash → new hash, health, actor), one `logger.warning` per verification failure or quota refusal, one `logger.info` per sweep with the returned counts. **No snapshot bodies, no bindings, no event payloads are ever logged** — that is the same leak the receipt redaction exists to prevent.
- **No bus events from this package.** Run lifecycle events belong to the engine (Package 4); emitting them from the repository would double-emit once the engine lands.
- **What an operator sees when it breaks:** a missing artifact file → `unavailable` health on the activation, a `playbooks.artifact_integrity` doctor finding naming the playbook and the hash, and every incoming event for that playbook retained in `playbook_pending_events` with `reason="unavailable"` rather than lost. Recovery is: rebuild the artifact (Package 6), activate it (Package 5), dispatch the retained events. Nothing in that path requires deleting a row by hand.

---

## 15. Fixtures

### 15.1 `tests/fixtures/playbook_v2/task_review_artifact.json`

One realistic artifact, not a placeholder: playbook id `task-review`, `schema_version: 2`, `version: 3`, `purpose: "routine"`, one rule `on-task-completed` triggered by `task.completed`, and five steps — a `CommandStep` (`ensure_task`), a `DecisionStep` on the command's typed result, an `LlmStep` with `profile_id: "reviewer"` and a 4000-token budget, a `WaitStep` on `pr.merged` with a 24-hour deadline, and a `TerminalStep`. `compiled_against.commands` names `ensure_task` with a fixed fingerprint; `compiled_against.profiles` names `reviewer`. Its canonical bytes hash to a constant that `tests/test_playbook_artifact_store.py` pins by name (`EXPECTED_TASK_REVIEW_SHA`), so a change to the canonicalizer fails one obvious assertion rather than fourteen obscure ones.

A second fixture, `task_review_artifact_v4.json`, differs from it **only** in the `ensure_task` fingerprint — the `stale_contract` case, and the input to Package 5's diff work.

### 15.2 Python fixtures

`tests/test_playbook_run_repository.py` builds its runs with a module-level `make_snapshot(**overrides)` helper seeded from the §15.1 artifact's hash, and a `db` fixture parametrized `["sqlite", "postgres"]` copied from `tests/test_claim_queries.py:41` (including the `ensure_worker_postgres_dsn()` skip and `reset_for_tests()`). **Every concurrency test in this package must run on both backends**: on SQLite the per-adapter `asyncio.Lock` inside `immediate()` serializes callers, so a green SQLite run proves the *result* is correct but not that the CAS is what enforced it. Only PostgreSQL proves the fence.

---

## 16. Tasks

Red step first in every case: write the test, watch it fail for the reason named, then implement. `aq test` is the runner (`CLAUDE.md`); `-q` throughout; nothing here needs `--aq-all-markers` except the migration suite.

### Task A — `feature/playbook-v2-pkg3-artifacts`

| # | Red (failing assertion) | Green |
|---|---|---|
| A-0 | — (seed commit, §4.1) | `src/playbooks/artifact_ref.py` verbatim from §4.2 |
| A-1 | `tests/test_playbook_artifact_ref.py::test_rejects_a_bare_digest`, `::test_rejects_uppercase_hex`, `::test_digest_strips_the_prefix`, `::test_as_dict_field_names_match_package_five` (asserts the exact eight keys of §4.6) — fail: module missing | A-0's module |
| A-2 | `tests/test_playbook_artifact_store.py::test_canonical_bytes_match_package_two` (hard equality once P2 has merged, `xfail(strict=False)` before that) and `::test_hash_is_over_the_bytes_written` — fail: nothing to import | the `canonical_bytes` / `artifact_sha256` import from `src.playbooks.definition`, or §5.1's marked fallback |
| A-3 | `::test_put_writes_a_hash_named_file_and_returns_a_ref`, `::test_put_is_idempotent_for_identical_bytes`, `::test_put_refuses_a_hash_collision`, `::test_put_refuses_an_oversize_artifact` | `ArtifactStore.put` (§5.2) |
| A-4 | `::test_load_verifies_the_hash_before_parsing`, `::test_mutated_artifact_file_fails_to_load`, `::test_load_rejects_a_non_hash_identifier` (parametrized, §13) | `ArtifactStore.load`, `path_for`, `exists` |
| A-5 | `::test_interrupted_write_leaves_no_active_artifact` (monkeypatches `os.replace` to raise; asserts no `.json` file and no leftover `.tmp-*`) | the `finally` unlink + dir fsync of §5.2 |
| A-6 | `tests/test_playbook_activation.py::test_upsert_artifact_row_round_trips`, `::test_activation_is_unique_per_scope` (second insert with the same `(playbook_id, scope, scope_identifier)` raises `IntegrityError`), `::test_activation_defaults_to_disabled_with_no_artifact` — fail: tables missing | §6.1/§6.2 in `tables.py`, migration `a3f1c0de0001` (§7.1), `PlaybookArtifactQueryMixin`, adapter registration (§6.7), `docs/specs/database.md` sections (§6.8) |
| A-7 | `::test_health_is_evaluated_in_order` (six parametrized cases, one per value, asserting both the value and the reason codes), `::test_enabled_and_stale_contract_coexist` | `activation.evaluate_health` (§11) |
| A-8 | `::test_set_activation_requires_an_existing_artifact`, `::test_set_activation_records_actor_and_time`, `::test_no_module_in_package_three_calls_set_activation_implicitly` | `set_activation` |
| A-9 | `::test_retention_never_deletes_a_referenced_artifact` (activation ref, run ref, newest-ten), `::test_retention_removes_stale_temp_files`, `::test_sweep_returns_counts_per_category` | `ArtifactRetentionSweeper` (§12.2), config fields + validation (§12.3), the `run_one_cycle` step, `src/doctor/playbook_v2_checks.py` |

### Task B — `feature/playbook-v2-pkg3-runstate`

| # | Red (failing assertion) | Green |
|---|---|---|
| B-1 | `tests/test_playbook_run_repository.py::test_snapshot_round_trips_through_canonical_json`, `::test_lifecycle_rejects_an_illegal_transition` (parametrized over the illegal pairs), `::test_bindings_reject_undeclared_keys` — fail: `run_state` missing | `src/playbooks/run_state.py`: `RunLifecycle`, `LEGAL_TRANSITIONS`, `RunSnapshot`, `LoopFrame`, `RunBudget`, `serialize_snapshot`, the error family (§14) |
| B-2 | `::test_oversize_result_fails_the_run_without_storing_it`, `::test_oversize_snapshot_raises_before_the_transaction` | `check_result_size`, the `StateLimitExceeded` path (§8.2) |
| B-3 | `tests/test_playbook_receipts.py::test_idempotency_key_includes_the_loop_iteration`, `::test_projection_is_default_deny`, `::test_sensitive_value_is_replaced_by_a_stable_handle` | `src/playbooks/receipts.py`: `StepReceipt`, `idempotency_key`, `project_receipt`, `sensitive_handle` (§8.3, §9.1) |
| B-4 | `::test_create_and_load_a_run` — fail: tables missing | §6.3/§6.4 in `tables.py`, migration `b3f2c0de0002` (§7.2), `PlaybookRunQueryMixin.create_run`/`load_run`, adapter registration, `database.md` sections |
| B-5 | `::test_commit_boundary_advances_the_version_and_writes_one_receipt`, `::test_commit_boundary_rolls_back_the_receipt_when_the_cas_fails`, `::test_stale_version_raises_snapshot_version_conflict` (both backends) | `commit_boundary` (§4.4) |
| B-6 | `::test_duplicate_attempt_is_rejected_by_the_database`, `::test_a_second_iteration_of_the_same_step_is_not_a_duplicate` | the unique constraint + `DuplicateAttempt` mapping |
| B-7 | `::test_concurrent_boundaries_produce_one_winner` (twenty concurrent `commit_boundary` calls from the same loaded snapshot; exactly one succeeds, nineteen raise, receipt count is 1) — **must run on PostgreSQL to be meaningful** | — (proves B-5) |
| B-8 | `::test_paused_run_cancels_immediately`, `::test_running_run_enters_cancelling`, `::test_cancel_requires_the_current_version` | `request_cancel` |
| B-9 | `tests/test_playbook_wait_repository.py::test_register_and_claim`, `::test_a_second_active_wait_for_a_step_is_rejected`, `::test_claim_is_exactly_once_under_concurrency` — fail: tables missing | §6.5/§6.6 in `tables.py`, migration `b3f2c0de0003` (§7.3), `waits.py`, the wait half of the mixin, `database.md` sections |
| B-10 | `::test_wait_registration_and_snapshot_commit_are_atomic` (inject a failure after the wait insert; assert neither the wait nor the version advance survives) | `wait_changes` applied on `commit_boundary`'s connection (§4.5) |
| B-11 | `::test_wait_survives_a_process_restart` (§10.2) | — (proves B-9/B-10) |
| B-12 | `::test_expire_due_claims_only_past_deadlines`, `::test_clear_for_run_deactivates_every_wait` | `expire_due`, `clear_for_run` |
| B-13 | `::test_pending_event_is_deduplicated_by_the_index`, `::test_pending_events_replay_in_arrival_order`, `::test_resolve_is_exactly_once`, `::test_pending_event_quota_is_enforced` | `retain_pending_event`, `resolve_pending_event`, `list_pending_events` (§10.3) |

### Joint — on `feature/playbook-v2-pkg3` after both merge

| # | Red | Green |
|---|---|---|
| J-1 | `tests/test_migration_playbook_v2.py::test_single_head` (asserts `len(ScriptDirectory.get_heads()) == 1`) | — (proves §4.3) |
| J-2 | `::test_upgrade_creates_every_table_and_index`, `::test_downgrade_removes_them_in_fk_safe_order`, `::test_upgrade_downgrade_upgrade_is_stable` — SQLite, following `tests/test_migration_agent_flock.py`'s `migrate()` helper | — |
| J-3 | `::test_partial_indexes_are_created_on_both_dialects` (SQLite via `PRAGMA index_list`; PostgreSQL via `pg_indexes.indexdef` containing the `WHERE`) | — |
| J-4 | `::test_upgrade_head_on_postgres` (skipped without `POSTGRES_TEST_DSN`, mirroring `tests/test_migration_postgres_upgrade_head.py`) | — |
| J-5 | `tests/test_docs_sync.py` (existing) must be green | the `database.md` sections of §6.8 |
| J-6 | the §3.2 reconciliation script reports no missing symbol; delete the `PlaybookDefinitionT` fallback | commit 5 |

### Commit sequence (roadmap Package 3, mapped)

| Roadmap commit | Branch | Tasks |
|---|---|---|
| 0. `chore: seed playbook v2 pkg3 shared artifact reference` (added by this plan, §4.1) | A, pushed first | A-0, A-1 |
| 1. `feat: store immutable playbook artifacts and activations` | A | A-2 … A-8 |
| 2. `feat: persist v2 snapshots and execution receipts` | B | B-1 … B-8 |
| 3. `feat: add durable waits and pending events` | B | B-9 … B-13 |
| 4. `feat: report activation health and retention` | A | A-9 |
| 5. `test: verify sqlite and postgres migration behavior` | integration | J-1 … J-6 |

---

## 17. Verification

Per task, as each lands:

```bash
aq test tests/test_playbook_artifact_ref.py tests/test_playbook_artifact_store.py -q     # A-1..A-5, A-9
aq test tests/test_playbook_activation.py -q                                             # A-6..A-9
aq test tests/test_playbook_run_repository.py tests/test_playbook_receipts.py -q         # B-1..B-8
aq test tests/test_playbook_wait_repository.py -q                                        # B-9..B-13
aq test tests/test_migration_playbook_v2.py -m migration -q                              # J-1..J-4
aq test tests/test_docs_sync.py -q                                                       # J-5
ruff check src/playbooks src/database src/doctor/playbook_v2_checks.py \
  tests/test_playbook_artifact_ref.py tests/test_playbook_artifact_store.py \
  tests/test_playbook_activation.py tests/test_playbook_run_repository.py \
  tests/test_playbook_receipts.py tests/test_playbook_wait_repository.py \
  tests/test_migration_playbook_v2.py
```

Expected outcomes: every suite green; the migration suite reports one head; `test_concurrent_boundaries_produce_one_winner` and `test_claim_is_exactly_once_under_concurrency` **skip** without `POSTGRES_TEST_DSN` and must be run at least once with it before the exit gate is claimed (`POSTGRES_TEST_DSN=postgresql+asyncpg://…:5533/… aq test tests/test_playbook_run_repository.py -q`).

Once, before closing the package (not during — supervisor guidance, `aq task comments solid-harbor.34`):

```bash
aq test tests/test_playbook_artifact_ref.py tests/test_playbook_artifact_store.py \
        tests/test_playbook_activation.py tests/test_playbook_run_repository.py \
        tests/test_playbook_receipts.py tests/test_playbook_wait_repository.py \
        tests/test_migration_playbook_v2.py tests/test_docs_sync.py \
        tests/test_database.py tests/test_playbook_health.py tests/test_playbook_store.py \
        tests/test_config.py tests/test_config_editor.py tests/test_doctor.py -q
python -m alembic heads          # exactly one line
python -m alembic upgrade head && python -m alembic downgrade d3e7b1c9a204 && \
  python -m alembic upgrade head
```

`tests/test_database.py`, `tests/test_playbook_health.py`, `tests/test_playbook_store.py`, `tests/test_config*.py` and `tests/test_doctor.py` are the untouched-neighbour suites: this package must not move them.

---

## 18. Exit-gate mapping

> **Gate (roadmap Package 3):** *A reviewed artifact can be stored and activated by hash, and a synthetic run can cross every durable boundary, restart the process, and resume without losing state, duplicating an acknowledged attempt, or reading mutable playbook content.*

| Gate clause | Proof |
|---|---|
| stored by hash | A-3 (`test_put_writes_a_hash_named_file_and_returns_a_ref`), A-2 (canonical bytes are the hashed bytes) |
| activated by hash | A-6 (`test_upsert_artifact_row_round_trips`), A-8 (`test_set_activation_requires_an_existing_artifact`) |
| crosses every durable boundary | B-5 (snapshot + receipt in one transaction), B-9/B-10 (wait registration inside that same transaction), B-13 (pending events) |
| restarts the process | B-11 (`test_wait_survives_a_process_restart` — the adapter is closed and reopened, no in-memory carry-over) |
| resumes without losing state | B-11's byte-identical `version`/`bindings`/`loop` assertion; B-5's rollback assertion proves a failed boundary loses nothing either |
| without duplicating an acknowledged attempt | B-6 (`test_duplicate_attempt_is_rejected_by_the_database`), B-7 (one winner under concurrency) |
| without reading mutable playbook content | A-4 (`load` re-hashes before parsing; a mutated file cannot be read), plus `playbook_v2_runs.artifact_sha256` being the only graph pointer a run holds — there is no path from a run to `CompiledPlaybookStore` |
| migrations up and down on both engines | J-2, J-3, J-4 |
| M3 milestone evidence (roadmap §6: hash verification, transaction, wait-race, restart, migration tests) | A-4, B-5, B-9/B-10, B-11, J-2..J-4 respectively |

---

## 19. Rollback boundary

Six new tables, one new artifact directory, ten new config fields with safe defaults, and seven new modules. Nothing existing changes behavior:

- `src/playbooks/store.py`, `health.py`, `manager.py`, `runner*.py` and `src/database/queries/playbook_queries.py` are **untouched**; V1 continues to compile, run, pause and resume exactly as before.
- Activations default to `enabled=False` with `health='disabled'`, and no code in this package writes one.
- Reverting commits 4→1 in order removes the tables (`alembic downgrade d3e7b1c9a204`) and the modules; the only edits to existing files are additive: two import lines and two base-class lines per adapter, ten dataclass fields plus their parse lines and validators in `src/config.py`, one `run_one_cycle` step, one doctor registration, and the `database.md` sections.
- Rollback that *keeps* the data is the documented default: leave the tables, set `playbooks.v2_storage_enabled: false`, and the artifact directory plus every row stays readable for inspection. That is the roadmap's stated rollback boundary for this package.

---

## 20. Interface amendments recorded by this plan

Roadmap §7 requires that a change to a locked interface be propagated to every not-yet-completed child plan. Three changes are recorded here; all three are additions or refinements, none contradicts an executed package.

1. **`ActivationHealth` has six values, not five.** `unavailable` is added for "the artifact row or file is gone", which is otherwise indistinguishable from `invalid` and is the only state a doctor check can act on. Package 5's plan already assumes six (`2026-09-01-playbook-v2-graph-api-ui.md` §4.4, §16); no other plan references the enum.
2. **The attempt idempotency key is four-part, not three-part** (§9.1). The design spec's `<run_id>:<step_id>:<attempt>` collides across loop iterations; the roadmap's own Package 3 requirement names the iteration. Package 4's plan must construct keys with `receipts.idempotency_key(...)` rather than by hand.
3. **Canonicalization is Package 2's, not this package's** (§5.1). An earlier draft of this plan proposed `exclude_none=False`; Package 2's landed plan specifies `exclude_none=True` backed by its absent-≡-null model invariant, and its version wins because the hash must be computed by exactly one function. This plan carries no second canonicalizer beyond the temporary, test-guarded fallback of §5.1.
4. **`ArtifactRef` lives in `src/playbooks/artifact_ref.py`**, not in `artifact_store.py` as the roadmap's module map implies (§4.2). The roadmap's map is a module *ownership* statement; splitting one dataclass out of it is what lets two branches build in parallel without one importing the other's file I/O. Package 5's reconciliation script imports `src.playbooks.artifact_store.ArtifactRef`, so `artifact_store.py` **re-exports** it (`from src.playbooks.artifact_ref import ArtifactRef  # re-exported for the roadmap's module map`) and both import paths work.

---

## 21. Open items for the next child plans

- **Package 4** must (a) construct attempt keys with `receipts.idempotency_key`, (b) call `commit_boundary` exactly once per step boundary and use its **returned** snapshot, (c) pass wait registrations through `wait_changes` rather than calling `WaitRepository.register` separately — a separate call re-opens the race this package closed — and (d) populate `selected_transition` on every receipt as `f"{rule_id}::{step_id}::{outcome}"`, which Package 5's overlay joins on.
- **Package 4** owns the resume path: `load_run` → validate `playbook_waits.snapshot_version == playbook_v2_runs.snapshot_version` → advance. This package deliberately ships no `resume()`; it would be an engine method living in a repository.
- **Package 5** may delete its §9 migration: the operator-audit columns (`playbook_activations.activated_by`, `playbook_pending_events.resolved_at`/`resolved_by`/`resolution`) ship here.
- **Package 6** rebuilds artifacts through `ArtifactStore.put` and reviews them by hash; it must not write activation rows outside Package 5's command.
- **Package 7** removes `playbooks.v2_storage_enabled`, and leaves `playbook_runs` (V1) in place and readable. It does **not** rename `playbook_v2_runs`.
- **Package 1 (or Package 4) must decide whether an *argument-side* receipt projection exists.** `ExecutionContract` declares `receipt_projection` over the **result** model and `sensitive_args` over the argument model, but no argument allow-list. This package therefore redacts every input by default and accepts an optional `input_projection` (§8.3) that nothing populates yet. Either P1 adds `argument_projection` to `ExecutionContract` — the tidier answer, since it keeps the classification in one place — or P4 documents that inputs are permanently receipt-opaque. Until then a receipt shows *which* inputs existed, never their values, which satisfies the spec's default-deny but gives an operator less than the design intends.
- **Deferred deliberately:** result externalization (§8.2 rejects instead), artifact compaction/packing, and cross-database artifact replication. None is required by the exit gate, and each would add a GC or consistency surface that the M3 evidence does not cover.
