"""Shared builders for the Package 4 engine and executor suites.

Kept beside the suites rather than in ``conftest.py`` so the fixture
construction is importable from a plain script when a failure needs
reproducing outside pytest.
"""

from __future__ import annotations

import json
import pathlib
import pickle
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from src.playbooks.artifact_ref import ARTIFACT_SCHEMA_GENERATION, ArtifactRef
from src.playbooks.definition import (
    PlaybookDefinition,
    artifact_sha256,
    contract_fingerprint,
    load_definition_json,
)
from src.playbooks.receipts import StepReceipt
from src.playbooks.run_state import (
    DuplicateRun,
    IllegalLifecycleTransition,
    RunLifecycle,
    RunSnapshot,
    SnapshotVersionConflict,
)
from src.playbooks.waits import EMPTY_WAIT_CHANGES

FIXTURES = pathlib.Path("tests/fixtures/playbooks/v2")
GOLDEN = FIXTURES / "review-pipeline.artifact.json"
EVENTS = FIXTURES / "engine-events"

COMPILER_BUILD = "pkg4-test"


def golden_artifact() -> PlaybookDefinition:
    """The Package 5 artifact, verbatim (§6.1).

    Package 4 deliberately does not fork it: needing a differently shaped
    artifact to *execute* what the API *renders* would be a real semantic
    disagreement, and it should surface as a failing test rather than as two
    fixtures that quietly diverge.
    """
    return load_definition_json(GOLDEN.read_text())


def load_artifact(name: str) -> PlaybookDefinition:
    return load_definition_json((FIXTURES / name).read_text())


def event(name: str) -> dict[str, Any]:
    return json.loads((EVENTS / f"{name}.json").read_text())


def minimal_artifact() -> PlaybookDefinition:
    """The smallest legal artifact — one rule, one command, one terminal.

    Used where a test needs *an* artifact for identity but asserts nothing
    about its graph.
    """
    payload = {
        "schema_version": 2,
        "id": "minimal",
        "version": 1,
        "scope": {"type": "system"},
        "source_hash": "sha256:" + "1" * 64,
        "compiled_at": "2026-09-01T00:00:00Z",
        "purpose": "routine",
        "rules": [
            {
                "id": "r",
                "name": "Rule",
                "trigger": {"event_type": "task.completed"},
                "entry_step": "ensure-review-task",
                "source": {"path": "x.md", "start_line": 1, "end_line": 1},
            }
        ],
        "steps": {
            "ensure-review-task": {
                "type": "command",
                "rule": "r",
                "title": "Ensure",
                "command": "ensure_task",
                "inputs": {},
                "save_result_as": "review",
                "transitions": {"created": "done", "reused": "done", "rejected": "bad"},
                "source": {"path": "x.md", "start_line": 1, "end_line": 1},
            },
            "done": {
                "type": "terminal",
                "rule": "r",
                "title": "Done",
                "outcome": "completed",
                "source": {"path": "x.md", "start_line": 2, "end_line": 2},
            },
            "bad": {
                "type": "terminal",
                "rule": "r",
                "title": "Bad",
                "outcome": "failed",
                "source": {"path": "x.md", "start_line": 3, "end_line": 3},
            },
        },
    }
    return PlaybookDefinition.model_validate(payload)


def artifact_ref_for(definition: PlaybookDefinition) -> ArtifactRef:
    """An ``ArtifactRef`` computed from *definition*, never hand-written.

    Computing it keeps ``receipt.artifact_sha256 == ref.artifact_sha256``
    true by construction, which is what T-4's pinning assertion checks.
    """
    return ArtifactRef(
        playbook_id=definition.id,
        artifact_sha256=artifact_sha256(definition),
        schema_generation=ARTIFACT_SCHEMA_GENERATION,
        contract_fingerprint=contract_fingerprint(definition),
        source_digest=definition.source_hash,
        compiler_build=definition.compiler_build or COMPILER_BUILD,
        compiled_at=definition.compiled_at.isoformat(),
        version=definition.version,
    )


class InMemoryArtifactStore:
    """``ArtifactStore.load`` over a dict, for tests with no compiled root."""

    def __init__(self, definitions: Mapping[str, PlaybookDefinition] | None = None) -> None:
        self._by_sha: dict[str, PlaybookDefinition] = {}
        for definition in (definitions or {}).values():
            self.put(definition)

    def put(self, definition: PlaybookDefinition) -> str:
        sha = artifact_sha256(definition)
        self._by_sha[sha] = definition
        return sha

    def exists(self, artifact_sha256_value: str) -> bool:
        return artifact_sha256_value in self._by_sha

    def load(self, artifact_sha256_value: str) -> PlaybookDefinition:
        if artifact_sha256_value not in self._by_sha:
            raise FileNotFoundError(artifact_sha256_value)
        return self._by_sha[artifact_sha256_value]


class RecordingBus:
    """An ``EventBus`` that records instead of dispatching."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        self.events.append((event_type, dict(data or {})))


class StubActivations:
    """The activation lookup ``dispatch_event`` resolves against.

    Every ref it holds is enabled and ``ready``; the unhealthy paths belong to
    §4.13 and are driven by :attr:`pending`, so a test that wants a queued
    event says so rather than constructing a health record.
    """

    def __init__(self, refs: Sequence[ArtifactRef] = (), *, pending: Sequence[str] = ()) -> None:
        self.refs = list(refs)
        self.pending = list(pending)
        self.queued: list[tuple[str, dict[str, Any]]] = []

    async def ready_activations(self, event_type: str) -> list[ArtifactRef]:
        return [ref for ref in self.refs if ref.playbook_id not in self.pending]

    async def queue_pending_event(self, playbook_id: str, event: Mapping[str, Any]) -> None:
        self.queued.append((playbook_id, dict(event)))


class RecordingRunRepository:
    """An in-memory ``RunRepository`` that counts what the engine writes.

    It is not a mock of the real repository's *storage*; it is a mock of its
    *contract*, so the assertions it supports are the ones the child plan
    states: one commit per durable boundary, no retry after a version
    conflict, and nothing written before the boundary.
    """

    def __init__(
        self,
        *,
        conflict_on_commit: int | None = None,
        fail_commit_with: BaseException | None = None,
    ) -> None:
        self.snapshots: dict[str, RunSnapshot] = {}
        self.receipts: list[StepReceipt] = []
        self.commit_calls = 0
        self.create_calls = 0
        self.conflicts = 0
        #: ``(run_id, reason, requested_by)`` per ``request_cancel``.
        self.cancel_reasons: list[tuple[str, str, str]] = []
        #: Runs whose waits this double retired, from either seam.
        self.cleared_runs: list[str] = []
        self._conflict_at = conflict_on_commit
        self._fail_with = fail_commit_with
        self.dispatch_keys: set[tuple[str | None, str]] = set()

    async def create_run(self, snapshot: RunSnapshot) -> RunSnapshot:
        key = (snapshot.dispatch_id, snapshot.rule_id)
        if snapshot.dispatch_id is not None and key in self.dispatch_keys:
            # The shipped ``uq_playbook_v2_runs_dispatch_rule`` partial unique
            # index, expressed as the failure the engine has to handle.
            raise DuplicateRun(snapshot.dispatch_id or "", snapshot.rule_id)
        self.dispatch_keys.add(key)
        self.create_calls += 1
        self.snapshots[snapshot.run_id] = snapshot
        return snapshot

    async def load_run(self, run_id: str) -> RunSnapshot | None:
        return self.snapshots.get(run_id)

    async def find_run_for_dispatch(
        self, dispatch_id: str, rule_id: str
    ) -> RunSnapshot | None:
        for snapshot in self.snapshots.values():
            if snapshot.dispatch_id == dispatch_id and snapshot.rule_id == rule_id:
                return snapshot
        return None

    async def commit_boundary(
        self,
        snapshot: RunSnapshot,
        receipt: StepReceipt,
        wait_changes: Any = EMPTY_WAIT_CHANGES,
    ) -> RunSnapshot:
        self.commit_calls += 1
        if getattr(wait_changes, "clear_run_waits", False):
            self.cleared_runs.append(snapshot.run_id)
        if self._fail_with is not None:
            raise self._fail_with
        if self._conflict_at is not None and self.commit_calls == self._conflict_at:
            self.conflicts += 1
            stored = self.snapshots.get(snapshot.run_id)
            raise SnapshotVersionConflict(
                snapshot.run_id,
                snapshot.version,
                (stored.version + 5) if stored else None,
            )
        stored = replace(snapshot, version=snapshot.version + 1)
        self.snapshots[snapshot.run_id] = stored
        self.receipts.append(receipt)
        return stored

    async def request_cancel(
        self, run_id: str, *, expected_version: int, reason: str, requested_by: str
    ) -> RunSnapshot:
        """The shipped repository's contract, not a convenience stub.

        Three properties the engine depends on and the double therefore has
        to reproduce: a paused run goes straight to ``cancelled`` while a
        running one enters ``cancelling``; a terminal run is refused; and the
        write advances the version exactly as a boundary does, which is what
        makes "the walk must adopt the post-cancel version" testable here at
        all rather than only against PostgreSQL.
        """
        snapshot = self.snapshots[run_id]
        if snapshot.is_terminal:
            raise IllegalLifecycleTransition(
                run_id, snapshot.lifecycle.value, RunLifecycle.CANCELLED.value
            )
        target = (
            RunLifecycle.CANCELLED
            if snapshot.lifecycle is RunLifecycle.PAUSED
            else RunLifecycle.CANCELLING
        )
        now = 1_000.0
        updated = replace(
            snapshot,
            lifecycle=target,
            cancel_requested_at=now,
            version=snapshot.version + 1,
            completed_at=now if target is RunLifecycle.CANCELLED else snapshot.completed_at,
        )
        self.snapshots[run_id] = updated
        self.cancel_reasons.append((run_id, reason, requested_by))
        if target is RunLifecycle.CANCELLED:
            self.cleared_runs.append(run_id)
        return updated

    async def list_receipts(
        self, run_id: str, *, limit: int = 500, offset: int = 0
    ) -> list[StepReceipt]:
        """The restart boundary reads durable attempt receipts by run."""
        return [receipt for receipt in self.receipts if receipt.run_id == run_id][
            offset : offset + limit
        ]


class SQLiteRunRepository:
    """Tiny test-only durable repository for process-restart boundary tests.

    It deliberately persists only the engine's snapshot and receipt values.
    Its schema is private to this test double, created in pytest's temporary
    directory, and never invokes the application's migration machinery.
    """

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS test_v2_runs (run_id TEXT PRIMARY KEY, version INTEGER, snapshot BLOB)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS test_v2_receipts (receipt_id TEXT PRIMARY KEY, run_id TEXT, receipt BLOB)"
            )

    def _connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    async def create_run(self, snapshot: RunSnapshot) -> RunSnapshot:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO test_v2_runs (run_id, version, snapshot) VALUES (?, ?, ?)",
                (snapshot.run_id, snapshot.version, pickle.dumps(snapshot)),
            )
        return snapshot

    async def load_run(self, run_id: str) -> RunSnapshot | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT snapshot FROM test_v2_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return pickle.loads(row[0]) if row is not None else None

    async def find_run_for_dispatch(
        self, dispatch_id: str, rule_id: str
    ) -> RunSnapshot | None:
        with self._connection() as connection:
            rows = connection.execute("SELECT snapshot FROM test_v2_runs").fetchall()
        return next(
            (
                snapshot
                for (payload,) in rows
                if (snapshot := pickle.loads(payload)).dispatch_id == dispatch_id
                and snapshot.rule_id == rule_id
            ),
            None,
        )

    async def commit_boundary(
        self,
        snapshot: RunSnapshot,
        receipt: StepReceipt,
        wait_changes: Any = EMPTY_WAIT_CHANGES,
    ) -> RunSnapshot:
        del wait_changes
        with self._connection() as connection:
            current = connection.execute(
                "SELECT version FROM test_v2_runs WHERE run_id = ?", (snapshot.run_id,)
            ).fetchone()
            if current is None or int(current[0]) != snapshot.version:
                raise SnapshotVersionConflict(
                    snapshot.run_id, snapshot.version, None if current is None else int(current[0])
                )
            advanced = replace(snapshot, version=snapshot.version + 1)
            connection.execute(
                "UPDATE test_v2_runs SET version = ?, snapshot = ? WHERE run_id = ?",
                (advanced.version, pickle.dumps(advanced), advanced.run_id),
            )
            connection.execute(
                "INSERT INTO test_v2_receipts (receipt_id, run_id, receipt) VALUES (?, ?, ?)",
                (receipt.receipt_id, receipt.run_id, pickle.dumps(receipt)),
            )
        return advanced

    async def list_receipts(
        self, run_id: str, *, limit: int = 500, offset: int = 0
    ) -> list[StepReceipt]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT receipt FROM test_v2_receipts WHERE run_id = ? ORDER BY rowid LIMIT ? OFFSET ?",
                (run_id, limit, offset),
            ).fetchall()
        return [pickle.loads(payload) for (payload,) in rows]

    async def request_cancel(
        self, run_id: str, *, expected_version: int, reason: str, requested_by: str
    ) -> RunSnapshot:
        del reason, requested_by
        snapshot = await self.load_run(run_id)
        if snapshot is None or snapshot.version != expected_version:
            raise SnapshotVersionConflict(
                run_id, expected_version, None if snapshot is None else snapshot.version
            )
        updated = replace(
            snapshot,
            cancel_requested_at=1_000.0,
            version=snapshot.version + 1,
        )
        with self._connection() as connection:
            connection.execute(
                "UPDATE test_v2_runs SET version = ?, snapshot = ? WHERE run_id = ?",
                (updated.version, pickle.dumps(updated), run_id),
            )
        return updated


def with_step(
    definition: PlaybookDefinition, step_id: str, step: Any
) -> PlaybookDefinition:
    """A copy of *definition* with one step replaced.

    The V2 models are frozen, so a test that wants a one-defect artifact
    rebuilds it rather than mutating one — which is also what a compiler
    change would do, so the fixture and the real path stay the same shape.
    """
    steps = dict(definition.steps)
    steps[step_id] = step
    return definition.model_copy(update={"steps": steps})
