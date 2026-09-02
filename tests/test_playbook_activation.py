"""Explicit activation health remains separate from enablement."""

from __future__ import annotations

import pytest

from src.database import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "playbook-activation.db"))
    await database.initialize()
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
