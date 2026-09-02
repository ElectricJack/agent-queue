"""Explicit activation health remains separate from enablement."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.database import Database
from tests.pg_dsn import ensure_worker_postgres_dsn


POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
    else:
        database = Database(str(tmp_path / "playbook-activation.db"))
    await database.initialize()
    if request.param == "postgres":
        await database.reset_for_tests()
    yield database
    await database.close()


def _artifact():
    from src.playbooks.artifact_ref import ArtifactRef

    return ArtifactRef(
        playbook_id="task-review",
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test-build",
    )


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
async def test_reactivation_updates_one_row_and_preserves_activation_id(db):
    """Changing activation state must update the existing scope record in place."""
    from src.database.tables import playbook_activations

    artifact = _artifact()
    await db.upsert_playbook_artifact(
        artifact,
        scope="system",
        path="/artifacts/task-review.json",
        size_bytes=123,
    )
    await db.set_playbook_activation(
        playbook_id="task-review",
        scope="system",
        scope_identifier="",
        artifact_sha256=artifact.artifact_sha256,
        enabled=True,
        activated_by="operator-a",
        health="ready",
        reasons="[]",
    )
    async with db._engine.connect() as conn:
        initial = (
            await conn.execute(
                select(playbook_activations.c.activation_id).where(
                    playbook_activations.c.playbook_id == "task-review"
                )
            )
        ).scalar_one()

    await db.set_playbook_activation(
        playbook_id="task-review",
        scope="system",
        scope_identifier="",
        artifact_sha256=artifact.artifact_sha256,
        enabled=False,
        activated_by="operator-b",
        health="disabled",
        reasons='["operator_disabled"]',
    )

    async with db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(playbook_activations).where(
                    playbook_activations.c.playbook_id == "task-review"
                )
            )
        ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["activation_id"] == initial
    assert rows[0]["enabled"] is False
    assert rows[0]["health"] == "disabled"
    assert rows[0]["reasons"] == '["operator_disabled"]'
    assert rows[0]["activated_by"] == "operator-b"


@pytest.mark.asyncio
async def test_artifact_upsert_refreshes_mutable_metadata(db):
    """A re-stored hash retains its identity while refreshing mutable metadata."""
    from src.database.tables import playbook_artifacts

    artifact = _artifact()
    await db.upsert_playbook_artifact(
        artifact,
        scope="system",
        profile_fingerprint="profile-old",
        path="/artifacts/old.json",
        size_bytes=123,
        validation='{"status":"old"}',
    )
    await db.upsert_playbook_artifact(
        artifact,
        scope="system",
        profile_fingerprint="profile-new",
        path="/artifacts/new.json",
        size_bytes=456,
        validation='{"status":"new"}',
    )

    async with db._engine.connect() as conn:
        row = (
            await conn.execute(
                select(playbook_artifacts).where(
                    playbook_artifacts.c.artifact_sha256 == artifact.artifact_sha256
                )
            )
        ).mappings().one()
    assert row["profile_fingerprint"] == "profile-new"
    assert row["path"] == "/artifacts/new.json"
    assert row["size_bytes"] == 456
    assert row["validation"] == '{"status":"new"}'
