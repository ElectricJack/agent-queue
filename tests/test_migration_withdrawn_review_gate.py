"""``a00000000076`` closes the gates withdrawn reviews orphaned, and holds their waiters.

``aq review withdraw`` used to close a review and leave its ``review`` gate
``open`` forever: nothing could resolve it (``aq gate resolve`` refused review
gates, pointing at a decision a withdrawn review cannot take), so the gate's
escalation stayed live and went on asking the human about a review that no
longer existed.  This revision backfills the rows that path already produced.

Three arms, one per property: the upgrade closes exactly the orphans and holds
their waiters rather than releasing work onto an unapproved design; it leaves a
``rejected`` review's gate alone, because a rejection is not terminal; and the
downgrade puts the gates back.
"""

from __future__ import annotations

import os
import subprocess
import sys

import asyncpg
import pytest

from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.migration, pytest.mark.integration]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTGRES_DSN = ensure_worker_postgres_dsn()
REVISION = "a00000000076"
PRECEDING = previous_revision(REVISION)
PROJECT = "p-withdrawn"
HOLD = "hold:review_withdrawn"


def alembic(dsn: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(
            os.environ, AGENT_QUEUE_DB_URL=dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        ),
        capture_output=True,
        text=True,
        check=False,
    )


async def _seed_project(conn) -> None:
    await conn.execute(
        "INSERT INTO projects (id, name, status, created_at) "
        "VALUES ($1, 'p', 'ACTIVE', 1.0) ON CONFLICT DO NOTHING",
        PROJECT,
    )


async def _seed_review(conn, suffix: str, state: str) -> str:
    """A review in *state* with an open gate and one waiting task."""
    gate_id = f"gate-{suffix}"
    review_id = f"rev-{suffix}"
    task_id = f"task-{suffix}"
    await conn.execute(
        "INSERT INTO gates (id, project_id, gate_type, title, question, await_id, status, "
        "created_at) VALUES ($1, $2, 'review', $3, 'approve?', $4, 'open', 1.0)",
        gate_id, PROJECT, f"review {suffix}", review_id,
    )
    await conn.execute(
        "INSERT INTO doc_reviews (id, project_id, author_task_id, kind, title, vault_path, "
        "current_revision, state, gate_id, decider, notified_revision, created_at, updated_at) "
        "VALUES ($1, $2, NULL, 'spec', $3, $4, 1, $5, $6, 'user', 0, 1.0, 1.0)",
        review_id, PROJECT, f"Review {suffix}", f"specs/{suffix}.md", state, gate_id,
    )
    await conn.execute(
        "INSERT INTO tasks (id, project_id, title, description, status, is_blocked, "
        "priority, created_at, updated_at) "
        "VALUES ($1, $2, 'impl', 'impl', 'READY', 1, 100, 1.0, 1.0)",
        task_id, PROJECT,
    )
    await conn.execute(
        "INSERT INTO task_gates (task_id, gate_id) VALUES ($1, $2)", task_id, gate_id
    )
    await conn.execute(
        "INSERT INTO escalations (id, project_id, task_id, source_kind, source_identity, "
        "incident_key, supervisor_owner, summary, investigation, decision_requested, "
        "severity, state, revision, created_at, updated_at) "
        "VALUES ($1, $2, NULL, 'gate', $3, $4, $5, 'Review', 'legacy gate card', "
        "'approve?', 'medium', 'needs_human', 0, 1.0, 1.0)",
        f"escalation-{gate_id}", PROJECT, gate_id, f"gate:{gate_id}", f"supervisor-{PROJECT}",
    )
    return gate_id


async def connect(dsn: str):
    return await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://", 1))


async def legacy_database(suffix: str) -> str:
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database(suffix)
    result = alembic(dsn, "upgrade", PRECEDING)
    assert result.returncode == 0, result.stderr
    return dsn


async def test_upgrade_closes_withdrawn_review_gates_and_holds_their_waiters():
    dsn = await legacy_database("withdrawnreviewgate")
    conn = await connect(dsn)
    try:
        await _seed_project(conn)
        withdrawn_gate = await _seed_review(conn, "wd", "withdrawn")
        live_gate = await _seed_review(conn, "live", "in_review")
        rejected_gate = await _seed_review(conn, "rej", "rejected")
    finally:
        await conn.close()

    result = alembic(dsn, "upgrade", REVISION)
    assert result.returncode == 0, result.stderr

    conn = await connect(dsn)
    try:
        gate = await conn.fetchrow("SELECT * FROM gates WHERE id = $1", withdrawn_gate)
        assert (gate["status"], gate["resolution"]) == ("resolved", "withdrawn")
        assert gate["resolved_by"] == "migration:a00000000076"

        # The waiter is released from the gate and held instead: unblocked, but
        # never scheduled, and still flagged for a human.
        task = await conn.fetchrow("SELECT is_blocked FROM tasks WHERE id = $1", "task-wd")
        assert task["is_blocked"] == 0
        labels = await conn.fetchval(
            "SELECT label FROM task_labels WHERE task_id = 'task-wd' AND label = $1", HOLD
        )
        assert labels == HOLD
        meta = await conn.fetchval(
            "SELECT value FROM task_metadata WHERE task_id = 'task-wd' AND key = 'needs_attention'"
        )
        assert meta == '"review_withdrawn"'

        escalation = await conn.fetchrow(
            "SELECT * FROM escalations WHERE source_identity = $1", withdrawn_gate
        )
        assert escalation["state"] == "stale"
        assert escalation["outcome"] == "gate_resolved"
        assert escalation["terminal_at"] is not None
        assert "withdrawn" in escalation["terminal_outcome"]

        # A review that can still decide keeps its gate, escalation and waiter.
        for gate_id in (live_gate, rejected_gate):
            still = await conn.fetchrow("SELECT status FROM gates WHERE id = $1", gate_id)
            assert still["status"] == "open"
            assert await conn.fetchval(
                "SELECT is_blocked FROM tasks WHERE id = $1", f"task-{gate_id[5:]}"
            ) == 1
            assert await conn.fetchval(
                "SELECT state FROM escalations WHERE source_identity = $1", gate_id
            ) == "needs_human"
    finally:
        await conn.close()


async def test_upgrade_is_idempotent_and_downgrade_reopens_the_gates():
    dsn = await legacy_database("withdrawnreviewgateroll")
    conn = await connect(dsn)
    try:
        await _seed_project(conn)
        gate_id = await _seed_review(conn, "wd", "withdrawn")
    finally:
        await conn.close()

    for command in (("upgrade", REVISION), ("stamp", PRECEDING), ("upgrade", "head")):
        result = alembic(dsn, *command)
        assert result.returncode == 0, result.stderr

    result = alembic(dsn, "downgrade", PRECEDING)
    assert result.returncode == 0, result.stderr
    conn = await connect(dsn)
    try:
        gate = await conn.fetchrow("SELECT * FROM gates WHERE id = $1", gate_id)
        assert gate["status"] == "open"
        assert gate["resolution"] is None
        assert await conn.fetchval(
            "SELECT is_blocked FROM tasks WHERE id = 'task-wd'"
        ) == 1
    finally:
        await conn.close()
