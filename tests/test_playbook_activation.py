"""Explicit activation health remains separate from enablement."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.database import Database
from src.database.tables import playbook_activations
from tests.pg_dsn import ensure_worker_postgres_dsn

#: Re-activation writes an INSERT that can violate
#: ``uq_playbook_activations_scope``.  On PostgreSQL a constraint violation
#: aborts the whole transaction, so a recovery statement issued on the same
#: connection fails with "current transaction is aborted" -- a failure mode
#: SQLite does not have.  The Postgres arm is what pins that; it skips when
#: ``POSTGRES_TEST_DSN`` is unset (the common local-dev case).
POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "playbook-activation.db"))
        await database.initialize()
    yield database
    await database.close()


def _artifact(digest_char: str = "a"):
    from src.playbooks.artifact_ref import ArtifactRef

    return ArtifactRef(
        playbook_id="task-review",
        artifact_sha256="sha256:" + digest_char * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test-build",
    )


async def _store(db, ref):
    await db.upsert_playbook_artifact(
        ref, scope="system", path=f"/artifacts/{ref.digest}.json", size_bytes=1
    )


async def _activations(db):
    async with db._engine.begin() as conn:
        rows = await conn.execute(
            select(playbook_activations).order_by(playbook_activations.c.scope_identifier)
        )
        return rows.mappings().fetchall()


def test_health_faults_rank_above_disabled_and_contract_is_checked():
    from src.playbooks.activation import ActivationHealth, evaluate_health

    health, reasons = evaluate_health(
        enabled=False,
        artifact=_artifact(),
        artifact_present=True,
        validation={"errors": []},
        current_contract_fingerprints={"task_close": "sha256:" + "d" * 64},
        artifact_contract_fingerprints={"task_close": "sha256:" + "e" * 64},
    )
    assert health is ActivationHealth.STALE_CONTRACT
    assert reasons[0].code == "command_contract_changed"


def test_health_without_an_artifact_is_unavailable():
    from src.playbooks.activation import ActivationHealth, evaluate_health

    health, reasons = evaluate_health(
        enabled=True,
        artifact=None,
        artifact_present=False,
        validation={},
        current_contract_fingerprints={},
        artifact_contract_fingerprints={},
    )
    assert health is ActivationHealth.UNAVAILABLE
    assert reasons[0].code == "artifact_missing"


@pytest.mark.asyncio
async def test_activation_defaults_disabled_and_requires_stored_artifact(db):
    from src.database.queries.playbook_artifact_queries import ArtifactNotFound

    with pytest.raises(ArtifactNotFound):
        await db.set_playbook_activation(
            playbook_id="task-review",
            scope="system",
            scope_identifier="",
            artifact_sha256="sha256:" + "a" * 64,
            enabled=True,
            activated_by="operator",
            health="ready",
            reasons="[]",
        )


@pytest.mark.asyncio
async def test_reactivating_the_same_scope_updates_the_row_in_place(db):
    """The second write is an update, not a duplicate row or a dead transaction.

    Regression: the recovery path used to catch ``IntegrityError`` and issue
    the UPDATE on the *same* connection inside the already-aborted
    transaction, which PostgreSQL rejects outright.
    """
    first, second = _artifact("a"), _artifact("d")
    await _store(db, first)
    await _store(db, second)

    await db.set_playbook_activation(
        playbook_id="task-review",
        scope="system",
        scope_identifier="",
        artifact_sha256=first.artifact_sha256,
        enabled=False,
        activated_by="operator",
        health="disabled",
        reasons="[]",
    )
    original = (await _activations(db))[0]

    await db.set_playbook_activation(
        playbook_id="task-review",
        scope="system",
        scope_identifier="",
        artifact_sha256=second.artifact_sha256,
        enabled=True,
        activated_by="reviewer",
        health="ready",
        reasons='["promoted"]',
    )

    rows = await _activations(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["activation_id"] == original["activation_id"]
    assert row["active_artifact_sha256"] == second.artifact_sha256
    assert bool(row["enabled"]) is True
    assert row["health"] == "ready"
    assert row["reasons"] == '["promoted"]'
    assert row["activated_by"] == "reviewer"
    assert row["activated_at"] is not None
    assert row["updated_at"] >= original["updated_at"]


@pytest.mark.asyncio
async def test_clearing_the_artifact_on_reactivation_clears_activated_at(db):
    ref = _artifact("a")
    await _store(db, ref)
    for artifact_sha256, enabled, health in (
        (ref.artifact_sha256, True, "ready"),
        (None, False, "unavailable"),
    ):
        await db.set_playbook_activation(
            playbook_id="task-review",
            scope="system",
            scope_identifier="",
            artifact_sha256=artifact_sha256,
            enabled=enabled,
            activated_by="operator",
            health=health,
            reasons="[]",
        )

    rows = await _activations(db)
    assert len(rows) == 1
    assert rows[0]["active_artifact_sha256"] is None
    assert rows[0]["activated_at"] is None
    assert rows[0]["health"] == "unavailable"
    assert bool(rows[0]["enabled"]) is False


@pytest.mark.asyncio
async def test_distinct_scopes_get_their_own_activation_rows(db):
    ref = _artifact("a")
    await _store(db, ref)
    for scope_identifier in ("", "proj-a", "proj-b"):
        await db.set_playbook_activation(
            playbook_id="task-review",
            scope="system",
            scope_identifier=scope_identifier,
            artifact_sha256=ref.artifact_sha256,
            enabled=True,
            activated_by="operator",
            health="ready",
            reasons="[]",
        )

    rows = await _activations(db)
    assert [row["scope_identifier"] for row in rows] == ["", "proj-a", "proj-b"]
    assert len({row["activation_id"] for row in rows}) == 3
