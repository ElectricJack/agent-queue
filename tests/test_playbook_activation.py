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


# ---------------------------------------------------------------------------
# A-9 — retention against real rows (child plan §12.1, §12.2), plus the
# ``playbooks.artifact_integrity`` doctor check the same commit registers.
# The pure-file half of the sweep lives in tests/test_playbook_artifact_store.py.
# ---------------------------------------------------------------------------

#: Any horizon comfortably past "everything written by this test is old".
_A_YEAR = 365 * 86400


def _versioned_artifact(index: int, playbook_id: str = "task-review"):
    from src.playbooks.artifact_ref import ArtifactRef

    return ArtifactRef(
        playbook_id=playbook_id,
        artifact_sha256="sha256:" + f"{index:064x}",
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test-build",
        version=index,
    )


async def _store_versions(db, tmp_path, count: int, playbook_id: str = "task-review"):
    """``count`` artifacts, versions 1..count, each with a real file on disk."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    refs = []
    for index in range(1, count + 1):
        ref = _versioned_artifact(index, playbook_id)
        path = artifacts_dir / f"{ref.digest}.json"
        path.write_text("{}")
        await db.upsert_playbook_artifact(
            ref, scope="system", path=str(path), size_bytes=2
        )
        refs.append(ref)
    return refs


async def _insert_run(db, *, run_id: str, artifact_sha256: str, lifecycle="running",
                      completed_at=None, parent_run_id=None):
    import time as _time

    from sqlalchemy import insert as sa_insert

    from src.database.tables import playbook_v2_runs

    async with db._engine.begin() as conn:
        await conn.execute(
            sa_insert(playbook_v2_runs).values(
                run_id=run_id,
                playbook_id="task-review",
                artifact_sha256=artifact_sha256,
                rule_id="on-task-completed",
                lifecycle=lifecycle,
                snapshot="{}",
                started_at=_time.time(),
                updated_at=_time.time(),
                completed_at=completed_at,
                parent_run_id=parent_run_id,
            )
        )


def _sweeper(db, tmp_path, **overrides):
    from src.config import PlaybooksConfig
    from src.playbooks.retention import ArtifactRetentionSweeper

    return ArtifactRetentionSweeper(db, PlaybooksConfig(**overrides), str(tmp_path))


@pytest.mark.asyncio
async def test_retention_never_deletes_a_referenced_artifact(db, tmp_path):
    """The three §12.1 protections, exercised together on one playbook.

    Fourteen aged artifacts: v1 is the activation's, v2 is pinned by a live
    run, v5..v14 are the newest ten.  That leaves exactly v3 and v4 with no
    protection at all, and they are the only two the sweep may take.
    """
    import time as _time

    from src.database.tables import playbook_artifacts

    refs = await _store_versions(db, tmp_path, 14)
    await db.set_playbook_activation(
        playbook_id="task-review",
        scope="system",
        scope_identifier="",
        artifact_sha256=refs[0].artifact_sha256,
        enabled=True,
        activated_by="operator",
        health="ready",
        reasons="[]",
    )
    await _insert_run(db, run_id="run-live", artifact_sha256=refs[1].artifact_sha256)

    counts = await _sweeper(db, tmp_path).sweep(_time.time() + _A_YEAR)

    assert counts["artifact_rows"] == 2
    assert counts["artifact_files"] == 2
    async with db._engine.connect() as conn:
        surviving = set(
            (await conn.execute(select(playbook_artifacts.c.artifact_sha256))).scalars().all()
        )
    assert refs[2].artifact_sha256 not in surviving
    assert refs[3].artifact_sha256 not in surviving
    assert not (tmp_path / "artifacts" / f"{refs[2].digest}.json").exists()
    # The activation's artifact, the run's artifact and the newest ten stay.
    assert {ref.artifact_sha256 for ref in refs} - surviving == {
        refs[2].artifact_sha256,
        refs[3].artifact_sha256,
    }
    assert (tmp_path / "artifacts" / f"{refs[0].digest}.json").exists()


@pytest.mark.asyncio
async def test_retention_keeps_every_version_while_min_versions_covers_them(db, tmp_path):
    """``v2_artifact_min_versions`` is the whole newest-N window, not a hint."""
    import time as _time

    from src.database.tables import playbook_artifacts

    await _store_versions(db, tmp_path, 4)
    counts = await _sweeper(db, tmp_path, v2_artifact_min_versions=4).sweep(
        _time.time() + _A_YEAR
    )

    assert counts["artifact_rows"] == 0
    async with db._engine.connect() as conn:
        remaining = (
            await conn.execute(select(playbook_artifacts.c.artifact_sha256))
        ).scalars().all()
    assert len(remaining) == 4


@pytest.mark.asyncio
async def test_retention_collects_terminal_runs_but_never_live_or_pinning_ones(db, tmp_path):
    """A terminal, aged, unpinned run goes; a live one and a live run's parent stay."""
    import time as _time

    from src.database.tables import playbook_v2_runs

    refs = await _store_versions(db, tmp_path, 1)
    sha = refs[0].artifact_sha256
    long_ago = _time.time() - 10 * _A_YEAR
    await _insert_run(db, run_id="run-done", artifact_sha256=sha,
                      lifecycle="completed", completed_at=long_ago)
    await _insert_run(db, run_id="run-live", artifact_sha256=sha)
    await _insert_run(db, run_id="run-parent", artifact_sha256=sha,
                      lifecycle="completed", completed_at=long_ago)
    await _insert_run(db, run_id="run-child", artifact_sha256=sha,
                      parent_run_id="run-parent")

    counts = await _sweeper(db, tmp_path).sweep(_time.time() + _A_YEAR)

    assert counts["runs"] == 1
    async with db._engine.connect() as conn:
        remaining = set(
            (await conn.execute(select(playbook_v2_runs.c.run_id))).scalars().all()
        )
    assert remaining == {"run-live", "run-parent", "run-child"}


@pytest.mark.asyncio
async def test_sweep_marks_an_activation_unavailable_when_its_file_is_gone(db, tmp_path):
    """§11 clause (b): the sweep persists the one health value a stat can decide."""
    import time as _time

    refs = await _store_versions(db, tmp_path, 1)
    await db.set_playbook_activation(
        playbook_id="task-review",
        scope="system",
        scope_identifier="",
        artifact_sha256=refs[0].artifact_sha256,
        enabled=True,
        activated_by="operator",
        health="ready",
        reasons="[]",
    )
    (tmp_path / "artifacts" / f"{refs[0].digest}.json").unlink()

    counts = await _sweeper(db, tmp_path).sweep(_time.time() + _A_YEAR)

    assert counts["health_downgraded"] == 1
    rows = await db.list_playbook_activations(enabled_only=True)
    assert rows[0]["health"] == "unavailable"
    assert rows[0]["activated_by"] == "operator"
    # Never upgraded back: that needs validation and contract fingerprints.
    assert (await _sweeper(db, tmp_path).sweep(_time.time() + _A_YEAR))[
        "health_downgraded"
    ] == 0


@pytest.mark.asyncio
async def test_artifact_integrity_doctor_check_reports_missing_and_mutated_files(db, tmp_path):
    """``playbooks.artifact_integrity`` — OK, then WARN once a file is tampered with."""
    import hashlib
    from types import SimpleNamespace

    from src.config import PlaybooksConfig
    from src.doctor.models import DoctorContext, Severity
    from src.doctor.playbook_v2_checks import _check_artifact_integrity

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    body = b'{"id":"task-review"}'
    digest = hashlib.sha256(body).hexdigest()
    path = artifacts_dir / f"{digest}.json"
    path.write_bytes(body)

    from src.playbooks.artifact_ref import ArtifactRef

    ref = ArtifactRef(
        playbook_id="task-review",
        artifact_sha256=f"sha256:{digest}",
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test-build",
    )
    await db.upsert_playbook_artifact(
        ref, scope="system", path=str(path), size_bytes=len(body)
    )
    await db.set_playbook_activation(
        playbook_id="task-review",
        scope="system",
        scope_identifier="",
        artifact_sha256=ref.artifact_sha256,
        enabled=True,
        activated_by="operator",
        health="ready",
        reasons="[]",
    )

    ctx = DoctorContext(
        config=SimpleNamespace(playbooks=PlaybooksConfig(v2_storage_enabled=True)), db=db
    )
    assert (await _check_artifact_integrity(ctx)).severity is Severity.OK

    path.write_bytes(b'{"id":"tampered"}')
    result = await _check_artifact_integrity(ctx)
    assert result.severity is Severity.WARN
    assert result.data["faults"][0]["problem"] == "hash_mismatch"
    assert result.fixable is False

    path.unlink()
    result = await _check_artifact_integrity(ctx)
    assert result.data["faults"][0]["problem"] == "file_missing"


@pytest.mark.asyncio
async def test_artifact_integrity_is_inert_while_v2_storage_is_disabled(db):
    """The default install reports info, not a fault, and touches no rows."""
    from types import SimpleNamespace

    from src.config import PlaybooksConfig
    from src.doctor.models import DoctorContext, Severity
    from src.doctor.playbook_v2_checks import CHECK_ID, _check_artifact_integrity

    ctx = DoctorContext(config=SimpleNamespace(playbooks=PlaybooksConfig()), db=db)
    result = await _check_artifact_integrity(ctx)
    assert result.id == CHECK_ID
    assert result.severity is Severity.INFO


def test_artifact_integrity_is_registered_in_the_default_doctor_registry():
    from src.doctor import default_registry
    from src.doctor.playbook_v2_checks import CHECK_ID

    registry = default_registry()
    check = next(c for c in registry.checks() if c.id == CHECK_ID)
    assert check.owner == "playbook-v2"
    assert check.fix is None


# ---------------------------------------------------------------------------
# Orphan artifact files against real rows: the crash between deleting a row
# and unlinking its file, on both backends.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_removes_the_files_a_crash_left_behind(db, tmp_path, monkeypatch):
    """§12.1's "a crash between them leaves a file the next sweep removes".

    The first sweep deletes the two collectable rows and then dies before it
    unlinks their files.  Nothing in the database names those files any more,
    so ``_unlink_artifacts`` -- which is only ever handed the rows of the
    sweep it belongs to -- can never see them again; the orphan step is what
    makes the documented recovery real.
    """
    import time as _time

    from src.database.tables import playbook_artifacts
    from src.playbooks.retention import ArtifactRetentionSweeper

    refs = await _store_versions(db, tmp_path, 12)
    files = {ref.artifact_sha256: tmp_path / "artifacts" / f"{ref.digest}.json" for ref in refs}

    def _crash(self, collected):
        raise RuntimeError("crash between the row delete and the unlink")

    monkeypatch.setattr(ArtifactRetentionSweeper, "_unlink_artifacts", _crash)
    with pytest.raises(RuntimeError):
        await _sweeper(db, tmp_path).sweep(_time.time() + _A_YEAR)
    monkeypatch.undo()

    # The rows for v1 and v2 are gone; every file is still on disk.
    async with db._engine.connect() as conn:
        surviving = set(
            (await conn.execute(select(playbook_artifacts.c.artifact_sha256))).scalars().all()
        )
    orphaned = {refs[0].artifact_sha256, refs[1].artifact_sha256}
    assert surviving == {ref.artifact_sha256 for ref in refs} - orphaned
    assert all(path.exists() for path in files.values())

    counts = await _sweeper(db, tmp_path).sweep(_time.time() + _A_YEAR)

    assert counts["artifact_rows"] == 0
    assert counts["artifact_files"] == 0
    assert counts["orphan_files"] == 2
    assert not any(files[sha].exists() for sha in orphaned)
    assert all(files[sha].exists() for sha in surviving)


@pytest.mark.asyncio
async def test_retention_leaves_files_every_retained_row_still_names(db, tmp_path):
    """A sweep that collects nothing must also unlink nothing.

    The orphan step deletes files the database did not name, so the case that
    matters is the ordinary one: ten artifacts inside the ``min_versions``
    window, one activated and one pinned by a live run, and not a single file
    removed.
    """
    import time as _time

    refs = await _store_versions(db, tmp_path, 10)
    await db.set_playbook_activation(
        playbook_id="task-review",
        scope="system",
        scope_identifier="",
        artifact_sha256=refs[0].artifact_sha256,
        enabled=True,
        activated_by="operator",
        health="ready",
        reasons="[]",
    )
    await _insert_run(db, run_id="run-live", artifact_sha256=refs[1].artifact_sha256)

    counts = await _sweeper(db, tmp_path).sweep(_time.time() + _A_YEAR)

    assert counts["artifact_rows"] == 0
    assert counts["orphan_files"] == 0
    assert all((tmp_path / "artifacts" / f"{ref.digest}.json").exists() for ref in refs)


@pytest.mark.asyncio
async def test_filter_referenced_artifact_shas_spans_all_three_tables(db, tmp_path):
    """The reference query is a superset of "has an artifact row".

    The orphan sweep is about to unlink files, and §7.4 leaves foreign-key
    enforcement optional on SQLite, so an activation or a run naming a hash has
    to protect it on the strength of its own row -- not on the artifact row
    being there.
    """
    refs = await _store_versions(db, tmp_path, 3)
    await db.set_playbook_activation(
        playbook_id="task-review",
        scope="system",
        scope_identifier="",
        artifact_sha256=refs[1].artifact_sha256,
        enabled=True,
        activated_by="operator",
        health="ready",
        reasons="[]",
    )
    await _insert_run(db, run_id="run-live", artifact_sha256=refs[2].artifact_sha256)

    unknown = "sha256:" + "f" * 64
    referenced = await db.filter_referenced_artifact_shas(
        [ref.artifact_sha256 for ref in refs] + [unknown]
    )

    assert referenced == {ref.artifact_sha256 for ref in refs}
    assert await db.filter_referenced_artifact_shas([]) == set()
    assert await db.filter_referenced_artifact_shas([unknown]) == set()


@pytest.mark.asyncio
async def test_filter_referenced_artifact_shas_chunks_past_the_parameter_cap(db, tmp_path):
    """A directory scan is not bounded by the schema, so the IN clause is chunked."""
    from src.database.queries.playbook_artifact_queries import _SHA_BATCH

    refs = await _store_versions(db, tmp_path, 2)
    padding = ["sha256:" + f"{index:064x}" for index in range(10_000, 10_000 + _SHA_BATCH * 2 + 5)]

    referenced = await db.filter_referenced_artifact_shas(
        padding + [ref.artifact_sha256 for ref in refs]
    )

    assert referenced == {ref.artifact_sha256 for ref in refs}


# ---------------------------------------------------------------------------
# Capability-profile staleness: the design spec makes a changed capability
# profile stale an activation exactly like a changed command contract does
# ("a referenced execution contract or capability profile changed").  The
# artifact carries its per-profile fingerprints in ``compiled_against.profiles``
# and the artifact row carries their aggregate in ``profile_fingerprint``;
# both are compared with the live registry here.
# ---------------------------------------------------------------------------


OLD_PROFILE = "sha256:" + "1a" * 32
NEW_PROFILE = "sha256:" + "2b" * 32


def _health(**overrides):
    from src.playbooks.activation import evaluate_health

    kwargs = {
        "enabled": True,
        "artifact": _artifact(),
        "artifact_present": True,
        "validation": {},
        "current_contract_fingerprints": {},
        "artifact_contract_fingerprints": {},
    }
    kwargs.update(overrides)
    return evaluate_health(**kwargs)


def test_health_flips_to_stale_when_a_capability_profile_changes():
    from src.playbooks.activation import ActivationHealth

    health, reasons = _health(
        artifact_profile_fingerprints={"worker": OLD_PROFILE},
        current_profile_fingerprints={"worker": NEW_PROFILE},
    )

    assert health is ActivationHealth.STALE_CONTRACT
    assert [reason.code for reason in reasons] == ["profile_capabilities_changed"]
    assert reasons[0].subject == "worker"
    assert reasons[0].expected_fingerprint == OLD_PROFILE
    assert reasons[0].actual_fingerprint == NEW_PROFILE


def test_health_stays_ready_when_the_capability_profile_is_unchanged():
    from src.playbooks.activation import ActivationHealth, profile_fingerprint

    health, reasons = _health(
        artifact_profile_fingerprints={"worker": OLD_PROFILE},
        current_profile_fingerprints={"worker": OLD_PROFILE},
        stored_profile_fingerprint=profile_fingerprint({"worker": OLD_PROFILE}),
    )

    assert health is ActivationHealth.READY
    assert reasons == ()


def test_health_reports_a_capability_profile_that_is_no_longer_registered():
    from src.playbooks.activation import ActivationHealth

    health, reasons = _health(
        artifact_profile_fingerprints={"worker": OLD_PROFILE},
        current_profile_fingerprints={},
    )

    assert health is ActivationHealth.STALE_CONTRACT
    assert reasons[0].code == "profile_removed"
    assert reasons[0].subject == "worker"
    assert reasons[0].actual_fingerprint is None


def test_a_moved_row_aggregate_is_reported_when_the_artifact_map_cannot_explain_it():
    """The stored ``playbook_artifacts.profile_fingerprint`` is its own check.

    ``ArtifactStore.put`` takes the aggregate as a locked keyword and the row
    keeps it, so an activation whose row disagrees with the live registry is
    stale even when the artifact's own per-profile map is empty.
    """
    from src.playbooks.activation import ActivationHealth, profile_fingerprint

    health, reasons = _health(
        artifact_profile_fingerprints={},
        current_profile_fingerprints={},
        stored_profile_fingerprint=profile_fingerprint({"worker": OLD_PROFILE}),
    )

    assert health is ActivationHealth.STALE_CONTRACT
    assert reasons[0].code == "profile_fingerprint_changed"
    assert reasons[0].actual_fingerprint == profile_fingerprint({})


def test_an_opaque_or_absent_stored_profile_fingerprint_is_not_a_mismatch():
    """Rows written before the column was populated must not all go stale."""
    from src.playbooks.activation import ActivationHealth

    for stored in ("", None, "profile-opaque"):
        health, _ = _health(stored_profile_fingerprint=stored)
        assert health is ActivationHealth.READY, stored


def test_disabled_still_ranks_below_a_profile_fault():
    from src.playbooks.activation import ActivationHealth

    health, reasons = _health(
        enabled=False,
        artifact_profile_fingerprints={"worker": OLD_PROFILE},
        current_profile_fingerprints={"worker": NEW_PROFILE},
    )

    assert health is ActivationHealth.STALE_CONTRACT
    assert reasons[0].code == "profile_capabilities_changed"


# -- the read path: stored rows + live registries ---------------------------


def _definition(profiles: dict[str, str]):
    from src.playbooks.definition import PlaybookDefinition
    from tests.playbook_v2_helpers import twin

    body = twin()
    body["compiled_against"]["profiles"] = dict(profiles)
    return PlaybookDefinition.model_validate(body)


async def _activate_profiled_artifact(db, tmp_path, profile_ids=("worker",)):
    """Store one artifact compiled against ``profile_ids`` and activate it."""
    from src.playbooks.activation import profile_fingerprint
    from src.playbooks.artifact_store import ArtifactStore
    from tests.playbook_v2_helpers import stub_policies

    policies = stub_policies()
    profiles = {pid: policies[pid].fingerprint() for pid in profile_ids}
    store = ArtifactStore(str(tmp_path))
    definition = _definition(profiles)
    aggregate = profile_fingerprint(profiles)
    ref = store.put(
        definition,
        source_digest="sha256:" + "c" * 64,
        contract_fingerprint="sha256:" + "b" * 64,
        profile_fingerprint=aggregate,
        compiler_build="test-build",
        version=1,
    )
    await db.upsert_playbook_artifact(
        ref,
        scope="system",
        profile_fingerprint=aggregate,
        path=store.path_for(ref.artifact_sha256),
        size_bytes=len(store.canonical_bytes(definition)),
    )
    await db.set_playbook_activation(
        playbook_id=ref.playbook_id,
        scope="system",
        scope_identifier="",
        artifact_sha256=ref.artifact_sha256,
        enabled=True,
        activated_by="operator",
        health="ready",
        reasons="[]",
    )
    return ref


@pytest.mark.asyncio
async def test_read_path_reports_ready_then_stale_when_a_profile_changes(db, tmp_path):
    """The finding in one test: with commands current and the capability
    profile changed, health must stop being expressible only as ``ready``."""
    from src.playbooks.activation import ActivationHealth, load_activation_health
    from tests.playbook_v2_helpers import StubContracts, StubProfiles, stub_policies

    await _activate_profiled_artifact(db, tmp_path)
    contracts = StubContracts()

    unchanged = await load_activation_health(
        db, contracts=contracts, profiles=StubProfiles(), enabled_only=True
    )
    assert [record.health for record in unchanged] == [ActivationHealth.READY]

    from src.profiles.capabilities import CapabilityPolicy

    widened = stub_policies()
    widened["worker"] = CapabilityPolicy.from_namespaces(
        aq_commands=frozenset({"demo_command", "other_command"})
    )
    stale = await load_activation_health(
        db, contracts=contracts, profiles=StubProfiles(widened), enabled_only=True
    )

    assert [record.health for record in stale] == [ActivationHealth.STALE_CONTRACT]
    codes = [reason.code for reason in stale[0].reasons]
    assert "profile_capabilities_changed" in codes
    assert stale[0].reasons[0].subject == "worker"
    assert stale[0].active_artifact_sha256 is not None


@pytest.mark.asyncio
async def test_read_path_reports_unavailable_when_the_artifact_file_is_gone(db, tmp_path):
    from src.playbooks.activation import ActivationHealth, load_activation_health
    from tests.playbook_v2_helpers import StubContracts, StubProfiles

    ref = await _activate_profiled_artifact(db, tmp_path)
    (tmp_path / "artifacts" / f"{ref.digest}.json").unlink()

    records = await load_activation_health(
        db, contracts=StubContracts(), profiles=StubProfiles()
    )

    assert [record.health for record in records] == [ActivationHealth.UNAVAILABLE]
    assert records[0].reasons[0].code == "artifact_missing"


@pytest.mark.asyncio
async def test_read_path_command_and_doctors_report_a_mutated_artifact_sha_mismatch(
    db, tmp_path
):
    """Health must reject valid replacement bytes just as ArtifactStore.load does.

    The ``db`` fixture runs this regression against SQLite and PostgreSQL.  In
    particular, this keeps the activation-health path from trusting a mutable
    path after ``ArtifactStore.load`` has rejected the same artifact.
    """
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import _check_activation_stale, _check_artifact_integrity
    from src.playbooks.activation import ActivationHealth, _load_definition, load_activation_health
    from src.playbooks.artifact_store import ArtifactStore, ArtifactVerificationFailed
    from tests.playbook_v2_helpers import StubContracts, StubProfiles

    ref = await _activate_profiled_artifact(db, tmp_path)
    replacement = _definition({"worker": "sha256:" + "f" * 64})
    (tmp_path / "artifacts" / f"{ref.digest}.json").write_bytes(
        ArtifactStore.canonical_bytes(replacement)
    )
    with pytest.raises(ArtifactVerificationFailed):
        _load_definition(str(tmp_path / "artifacts" / f"{ref.digest}.json"), ref.artifact_sha256)

    records = await load_activation_health(
        db, contracts=StubContracts(), profiles=StubProfiles(), enabled_only=True
    )
    assert [record.health for record in records] == [ActivationHealth.UNAVAILABLE]
    assert [reason.code for reason in records[0].reasons] == ["artifact_sha_mismatch"]

    command = await _v2_handler(
        db, (StubContracts(), StubProfiles(), None)
    )._cmd_playbook_activation_health({})
    stale = await _check_activation_stale(_stale_ctx(db))
    integrity = await _check_artifact_integrity(_stale_ctx(db))
    assert command["by_health"] == {"unavailable": 1}
    assert command["activations"][0]["reasons"][0]["code"] == "artifact_sha_mismatch"
    assert stale.severity is Severity.WARN
    assert stale.data["stale"][0]["reasons"][0]["code"] == "artifact_sha_mismatch"
    assert integrity.severity is Severity.WARN
    assert integrity.data["faults"][0]["problem"] == "hash_mismatch"


# -- doctor: playbooks.activation_stale --------------------------------------


def _stale_ctx(db, tmp_path=None, *, enabled=True, profiles=None):
    from types import SimpleNamespace

    from src.config import PlaybooksConfig
    from src.doctor.models import DoctorContext
    from tests.playbook_v2_helpers import StubContracts, StubProfiles

    lookups = (StubContracts(), StubProfiles(profiles), None)

    async def _v2_lookups():
        return lookups

    handler = SimpleNamespace(_v2_lookups=_v2_lookups)
    return DoctorContext(
        config=SimpleNamespace(playbooks=PlaybooksConfig(v2_storage_enabled=enabled)),
        db=db,
        handler=handler,
    )


@pytest.mark.asyncio
async def test_activation_stale_doctor_check_warns_once_a_profile_moves(db, tmp_path):
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import STALE_CHECK_ID, _check_activation_stale
    from src.profiles.capabilities import CapabilityPolicy
    from tests.playbook_v2_helpers import stub_policies

    await _activate_profiled_artifact(db, tmp_path)

    result = await _check_activation_stale(_stale_ctx(db))
    assert result.id == STALE_CHECK_ID
    assert result.severity is Severity.OK
    assert result.data["checked"] == 1

    widened = stub_policies()
    widened["worker"] = CapabilityPolicy.from_namespaces(harness_tools=frozenset({"Bash"}))
    result = await _check_activation_stale(_stale_ctx(db, profiles=widened))

    assert result.severity is Severity.WARN
    assert result.fixable is False
    assert result.data["count"] == 1
    assert result.data["stale"][0]["playbook_id"] == "twin"
    assert "worker" in result.detail


@pytest.mark.asyncio
async def test_activation_stale_is_inert_while_v2_storage_is_disabled(db):
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import STALE_CHECK_ID, _check_activation_stale

    result = await _check_activation_stale(_stale_ctx(db, enabled=False))

    assert result.id == STALE_CHECK_ID
    assert result.severity is Severity.INFO


def test_activation_stale_is_registered_in_the_default_doctor_registry():
    from src.doctor import default_registry
    from src.doctor.playbook_v2_checks import STALE_CHECK_ID

    registry = default_registry()
    check = next(c for c in registry.checks() if c.id == STALE_CHECK_ID)
    assert check.owner == "playbook-v2"
    assert check.fix is None


# -- the operator surface: aq playbook activation-health ---------------------


def _v2_handler(db, lookups, *, v2_api=True, v2_storage=True):
    """The command mixin over one real database, without the whole handler."""
    from types import SimpleNamespace

    from src.commands.playbook_v2_commands import PlaybookV2CommandsMixin

    class _Handler(PlaybookV2CommandsMixin):
        def __init__(self):
            from src.config import PlaybooksConfig

            self.db = db
            self.config = SimpleNamespace(
                playbooks=PlaybooksConfig(v2_api=v2_api, v2_storage_enabled=v2_storage)
            )

        async def _v2_lookups(self):
            return lookups

    return _Handler()


@pytest.mark.asyncio
async def test_activation_health_command_surfaces_a_stale_capability_profile(db, tmp_path):
    from src.commands.playbook_v2_commands import V2_STORAGE_UNAVAILABLE_ERROR
    from src.profiles.capabilities import CapabilityPolicy
    from tests.playbook_v2_helpers import StubContracts, StubProfiles, stub_policies

    await _activate_profiled_artifact(db, tmp_path)
    lookups = (StubContracts(), StubProfiles(), None)

    ready = await _v2_handler(db, lookups)._cmd_playbook_activation_health({})
    assert ready["count"] == 1
    assert ready["by_health"] == {"ready": 1}
    assert ready["activations"][0]["playbook_id"] == "twin"
    assert ready["activations"][0]["reasons"] == []

    widened = stub_policies()
    widened["worker"] = CapabilityPolicy.from_namespaces(harness_tools=frozenset({"Bash"}))
    handler = _v2_handler(db, (StubContracts(), StubProfiles(widened), None))

    stale = await handler._cmd_playbook_activation_health({})
    assert stale["by_health"] == {"stale_contract": 1}
    reason = stale["activations"][0]["reasons"][0]
    assert reason["code"] == "profile_capabilities_changed"
    assert reason["subject"] == "worker"

    # Filters still narrow the same rows, and the seam error is what a build
    # without V2 storage keeps reporting.
    assert (await handler._cmd_playbook_activation_health({"health": "ready"}))["count"] == 0
    assert (await handler._cmd_playbook_activation_health({"playbook_id": "other"}))["count"] == 0
    assert (await handler._cmd_playbook_activation_health({"scope": "project"}))["count"] == 0
    assert await _v2_handler(db, lookups, v2_storage=False)._cmd_playbook_activation_health(
        {}
    ) == {"error": V2_STORAGE_UNAVAILABLE_ERROR}


@pytest.mark.asyncio
async def test_activation_health_response_validates_against_the_wire_contract(db, tmp_path):
    from src.api.models.playbook_v2 import PlaybookActivationHealthResponse
    from tests.playbook_v2_helpers import StubContracts, StubProfiles

    await _activate_profiled_artifact(db, tmp_path)
    result = await _v2_handler(
        db, (StubContracts(), StubProfiles(), None)
    )._cmd_playbook_activation_health({})

    response = PlaybookActivationHealthResponse.model_validate(result)
    assert response.activations[0].health == "ready"
    assert response.activations[0].pending_event_count == 0
