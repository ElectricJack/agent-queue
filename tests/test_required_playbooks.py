"""One-shot V2 policy installation, read-only restarts and deliberate repair."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.commands.playbook_v2_commands import PlaybookV2CommandsMixin
from src.database import Database
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.profiles import shipped_profile_lookup
from src.playbooks.required import (
    DEFAULT_SYSTEM_PLAYBOOK_IDS,
    INITIAL_SYSTEM_PLAYBOOK_IDS,
    INSTALL_RECEIPT,
    REQUIRED_SYSTEM_PLAYBOOK_IDS,
    RequiredPlaybookReconciler,
    ensure_reviewed_playbook_bundles,
    install_initial_playbooks,
    retain_required_route_needed_event,
    reviewed_bundle_drift,
    reviewed_bundle_source,
    shipped_reviewed_playbook_ids,
)
from src.playbooks.runtime import V2PlaybookRuntime
from src.playbooks.validation import RegisteredEventLookup, RegistryContractLookup
from tests.db_fixtures import lease_dsn


class _Handler(PlaybookV2CommandsMixin):
    def __init__(self, tmp_path, db) -> None:
        self.db = db
        self.config = SimpleNamespace(
            data_dir=str(tmp_path),
            vault_root=str(tmp_path / "vault"),
            compiled_root=str(tmp_path / "compiled"),
            playbooks=SimpleNamespace(enabled=True, v2_max_artifact_bytes=1_048_576),
        )
        self._store = ArtifactStore(self.config.compiled_root)

    def _v2_engine(self):
        return SimpleNamespace(services=SimpleNamespace(artifact_store=self._store))

    async def _v2_lookups(self):
        return RegistryContractLookup(), shipped_profile_lookup(), RegisteredEventLookup()


async def _reconciler(tmp_path):
    db = Database(lease_dsn("required-playbooks.db"))
    await db.initialize()
    install_initial_playbooks(str(tmp_path))
    ensure_reviewed_playbook_bundles(str(tmp_path))
    handler = _Handler(tmp_path, db)
    reconciler = RequiredPlaybookReconciler(config=handler.config, db=db, handler=handler)
    return db, handler, reconciler


@pytest.mark.parametrize("empty_vault", [False, True], ids=["absent", "empty"])
def test_first_install_has_an_explicit_minimal_inventory(tmp_path, empty_vault):
    if empty_vault:
        (tmp_path / "vault").mkdir()
    expected = {
        "default-assignment-routing", "provider-usage-probe", "provider-failover",
        "default-pipeline", "supervisor-failure-triage", "morning-report", "stuck-task-watch",
    }
    assert set(INITIAL_SYSTEM_PLAYBOOK_IDS) == expected
    assert set(install_initial_playbooks(str(tmp_path))) == expected
    assert {p.stem for p in (tmp_path / "vault/system/playbooks").iterdir()} == expected
    assert {p.name for p in (tmp_path / "vault/reviewed-playbooks").iterdir()} == expected
    assert not list((tmp_path / "vault").glob("agent-types/*/playbooks/*.md"))
    assert json.loads((tmp_path / INSTALL_RECEIPT).read_bytes()) == {
        "version": 1, "activation_pending": True,
    }


def _policy_bytes(tmp_path):
    """Snapshot policy and immutable imports, excluding unrelated vault scaffolding."""
    receipt = tmp_path / INSTALL_RECEIPT
    paths = [receipt, receipt.with_suffix(".tmp")]
    for directory in ("vault/system/playbooks", "vault/reviewed-playbooks", "compiled"):
        paths.extend((tmp_path / directory).rglob("*"))
    paths.extend((tmp_path / "vault").glob("agent-types/*/playbooks/*"))
    return {str(path.relative_to(tmp_path)): path.read_bytes() for path in paths if path.is_file()}


async def _read_only_restart(tmp_path, db):
    """Use fresh startup objects, then assert both health reads leave policy untouched."""
    from src.doctor.models import DoctorContext
    from src.doctor.playbook_v2_checks import _check_required, _check_reviewed_bundles
    from src.vault import ensure_vault_layout

    before = _policy_bytes(tmp_path)
    activations = await db.list_playbook_activations(enabled_only=False)
    handler = _Handler(tmp_path, db)
    handler._cmd_playbook_v2_import = AsyncMock(wraps=handler._cmd_playbook_v2_import)
    restarted = RequiredPlaybookReconciler(config=handler.config, db=db, handler=handler)
    handler.orchestrator = SimpleNamespace(reconcile_required_playbooks=restarted.reconcile)
    ctx = DoctorContext(config=handler.config, db=db, handler=handler)
    with (
        patch("src.playbooks.required.ensure_reviewed_playbook_bundles",
              wraps=ensure_reviewed_playbook_bundles) as refresh,
        patch("src.vault.ensure_default_aq_skills"),  # never seed the operator's harness homes
        patch("src.vault_index.VaultIndexGenerator.generate_all"),  # unrelated generated hubs
        patch.object(db, "set_playbook_activation", wraps=db.set_playbook_activation) as activate,
    ):
        ensure_vault_layout(str(tmp_path))
        status = await restarted.reconcile()
        await restarted.refresh_status()
        required = await _check_required(ctx)
        bundles = await _check_reviewed_bundles(ctx)
        refresh.assert_not_called()
        activate.assert_not_awaited()
    handler._cmd_playbook_v2_import.assert_not_awaited()
    assert _policy_bytes(tmp_path) == before
    assert await db.list_playbook_activations(enabled_only=False) == activations
    return status, required, bundles, ctx


@pytest.mark.parametrize("interrupt_at", REQUIRED_SYSTEM_PLAYBOOK_IDS[:2])
async def test_interrupted_initial_import_is_consumed_before_import_and_not_retried(
    tmp_path, interrupt_at,
):
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import _fix_required

    db, handler, reconciler = await _reconciler(tmp_path)
    receipt = tmp_path / INSTALL_RECEIPT
    imported = handler._cmd_playbook_v2_import

    async def interrupt(params):
        assert json.loads(receipt.read_bytes())["activation_pending"] is False
        if params["path"] == f"reviewed-playbooks/{interrupt_at}":
            raise asyncio.CancelledError("startup interrupted during import")
        return await imported(params)

    handler._cmd_playbook_v2_import = AsyncMock(side_effect=interrupt)
    try:
        with pytest.raises(asyncio.CancelledError, match="startup interrupted"):
            await reconciler.reconcile()
        position = REQUIRED_SYSTEM_PLAYBOOK_IDS.index(interrupt_at)
        assert [call.args[0]["path"] for call in handler._cmd_playbook_v2_import.await_args_list] == [
            f"reviewed-playbooks/{identifier}"
            for identifier in REQUIRED_SYSTEM_PLAYBOOK_IDS[:position + 1]
        ]
        assert {row["playbook_id"] for row in await db.list_playbook_activations()} == set(
            REQUIRED_SYSTEM_PLAYBOOK_IDS[:position]
        )

        status, diagnostic, _bundles, ctx = await _read_only_restart(tmp_path, db)
        assert not status["ok"]
        assert diagnostic.severity is Severity.ERROR
        assert "aq doctor --check playbooks.required --fix" in diagnostic.detail
        assert (await _fix_required(ctx)).severity is Severity.OK
        assert {row["playbook_id"] for row in await db.list_playbook_activations()} == set(
            REQUIRED_SYSTEM_PLAYBOOK_IDS
        )  # optional defaults and morning reporting are not resurrected by repair
        assert (await _read_only_restart(tmp_path, db))[0]["ok"]
    finally:
        await db.close()


@pytest.mark.parametrize("receipt_bytes", [
    b"", b'{"version":', b"[]", b'{"version": 2, "activation_pending": true}',
    b'{"version": 1}', b'{"version": 1, "activation_pending": false}',
], ids=["empty", "partial", "non-object", "unsupported", "missing-pending", "consumed"])
async def test_unusable_receipt_needs_explicit_repair_without_receipt_recovery(
    tmp_path, receipt_bytes,
):
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import _fix_required

    db, _handler, _reconciled = await _reconciler(tmp_path)
    receipt = tmp_path / INSTALL_RECEIPT
    receipt.write_bytes(receipt_bytes)
    # A leftover staging file is not authority to recover a partial/consumed receipt.
    receipt.with_suffix(".tmp").write_bytes(b'{"version": 1, "activation_pending": true}')
    try:
        status, diagnostic, _bundles, ctx = await _read_only_restart(tmp_path, db)
        assert not status["ok"]
        assert diagnostic.severity is Severity.ERROR
        assert "aq doctor --check playbooks.required --fix" in diagnostic.detail
        assert (await _fix_required(ctx)).severity is Severity.OK
        assert receipt.read_bytes() == receipt_bytes
        assert {row["playbook_id"] for row in await db.list_playbook_activations()} == set(
            REQUIRED_SYSTEM_PLAYBOOK_IDS
        )
        assert (await _read_only_restart(tmp_path, db))[0]["ok"]
    finally:
        await db.close()


@pytest.mark.parametrize("remove_entire_vault", [False, True])
def test_vault_restart_does_not_reseed_deleted_or_retired_policy(tmp_path, remove_entire_vault):
    import shutil

    from src.vault import ensure_vault_layout

    ensure_vault_layout(str(tmp_path))
    vault = tmp_path / "vault"
    if remove_entire_vault:
        shutil.rmtree(vault)
    else:
        source = vault / "system/playbooks/default-pipeline.md"
        source.rename(source.with_suffix(".md.retired"))
        shutil.rmtree(vault / "reviewed-playbooks/default-pipeline")
    ensure_vault_layout(str(tmp_path))
    assert not (vault / "system/playbooks/default-pipeline.md").exists()
    assert not (vault / "reviewed-playbooks/default-pipeline").exists()
    assert not list(vault.glob("agent-types/*/playbooks/*.md"))


async def test_existing_vault_is_not_migrated(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "operator.md").write_text("existing vault")
    assert install_initial_playbooks(str(tmp_path)) == []
    assert list(vault.iterdir()) == [vault / "operator.md"]
    assert json.loads((tmp_path / INSTALL_RECEIPT).read_bytes())["activation_pending"] is False
    db = Database(lease_dsn("existing-playbooks.db"))
    await db.initialize()
    try:
        assert not (await _read_only_restart(tmp_path, db))[0]["ok"]
        assert (vault / "operator.md").read_text() == "existing vault"
    finally:
        await db.close()


async def test_no_receipt_never_creates_missing_activations(tmp_path):
    db = Database(lease_dsn("existing-playbooks.db"))
    await db.initialize()
    try:
        ensure_reviewed_playbook_bundles(str(tmp_path))
        handler = _Handler(tmp_path, db)
        reconciler = RequiredPlaybookReconciler(config=handler.config, db=db, handler=handler)
        assert not (await reconciler.reconcile())["ok"]
        assert await db.list_playbook_activations() == []
        assert (await reconciler.reconcile(repair=True))["ok"]
        assert {r["playbook_id"] for r in await db.list_playbook_activations()} == set(REQUIRED_SYSTEM_PLAYBOOK_IDS)
    finally:
        await db.close()


@pytest.mark.parametrize("deleted", ["activations", "sources", "bundles", "vault"])
async def test_restart_keeps_deleted_policy_deleted(tmp_path, deleted):
    import shutil

    from sqlalchemy import delete

    from src.database.tables import playbook_activations
    from src.doctor.models import Severity

    db, _handler, reconciler = await _reconciler(tmp_path)
    try:
        assert (await reconciler.reconcile())["ok"]
        if deleted == "activations":
            async with db._engine.begin() as conn:
                await conn.execute(delete(playbook_activations))
        else:
            path = {"sources": "vault/system/playbooks", "bundles": "vault/reviewed-playbooks",
                    "vault": "vault"}[deleted]
            shutil.rmtree(tmp_path / path)
        status, diagnostic, _bundles, _ctx = await _read_only_restart(tmp_path, db)
        if deleted == "activations":
            assert not status["ok"]
            assert await db.list_playbook_activations() == []
            assert diagnostic.severity is Severity.ERROR
            assert "aq doctor --check playbooks.required --fix" in diagnostic.detail
        else:
            assert status["ok"]  # readiness follows the surviving immutable artifacts
    finally:
        await db.close()


async def test_restart_never_repoints_a_stale_activation(tmp_path):
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        await reconciler.reconcile()
        stale_sha = await _activate_an_older_release(db, handler)
        result, diagnostic, _bundles, _ctx = await _read_only_restart(tmp_path, db)
        assert not result["ok"]
        assert "aq doctor --check playbooks.required --fix" in diagnostic.detail
        assert (await _system_row(db, "default-assignment-routing"))["active_artifact_sha256"] == stale_sha
        assert (await reconciler.reconcile(repair=True))["ok"]
        assert (await _system_row(db, "default-assignment-routing"))["active_artifact_sha256"] != stale_sha
    finally:
        await db.close()


@pytest.mark.parametrize("enabled", [False, True])
async def test_doctor_reports_and_explicitly_repairs_required_policy(tmp_path, enabled):
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import _check_required, _fix_required

    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        await reconciler.reconcile()
        older = _shipped_variant("default-assignment-routing", _router_without_task_route_apply)
        older_sha = await _store_variant(db, handler, older)
        await _activate(db, "default-assignment-routing", older_sha, enabled=enabled)

        status, diagnostic, _bundles, ctx = await _read_only_restart(tmp_path, db)
        assert not status["ok"]
        assert diagnostic.severity is Severity.ERROR
        assert (await _system_row(db, "default-assignment-routing"))["active_artifact_sha256"] == older_sha
        assert (await _fix_required(ctx)).severity is Severity.OK
        assert (await _check_required(ctx)).severity is Severity.OK
        row = await _system_row(db, "default-assignment-routing")
        assert row["enabled"] and row["active_artifact_sha256"] != older_sha
    finally:
        await db.close()


async def test_required_doctor_repair_without_a_running_handler_does_not_write(tmp_path):
    from src.doctor.models import DoctorContext, Severity
    from src.doctor.playbook_v2_checks import _fix_required

    db, handler, _reconciled = await _reconciler(tmp_path)
    try:
        digest = _vault_bundle(tmp_path, "default-assignment-routing") / "artifact.sha256"
        digest.write_bytes(b"operator bundle awaiting repair\n")
        before = _policy_bytes(tmp_path)
        activations = await db.list_playbook_activations(enabled_only=False)

        result = await _fix_required(DoctorContext(config=handler.config, db=db, handler=handler))

        assert result.severity is Severity.ERROR
        assert result.detail == "Required policy repair needs the running daemon command handler"
        assert not result.fix_applied
        assert _policy_bytes(tmp_path) == before
        assert await db.list_playbook_activations(enabled_only=False) == activations
    finally:
        await db.close()


async def test_fresh_database_bootstraps_required_system_activations(tmp_path):
    db, _handler, reconciler = await _reconciler(tmp_path)
    try:
        result = await reconciler.reconcile()
        activations = await db.list_playbook_activations(enabled_only=True)

        assert result["ok"]
        assert {activation["playbook_id"] for activation in activations} == set(
            REQUIRED_SYSTEM_PLAYBOOK_IDS
        ) | set(DEFAULT_SYSTEM_PLAYBOOK_IDS)
        assert {(activation["scope"], activation["enabled"]) for activation in activations} == {
            ("system", True)
        }
    finally:
        await db.close()


async def test_restart_rehydrates_durable_activation_without_rewriting_it(tmp_path):
    db, _handler, reconciler = await _reconciler(tmp_path)
    try:
        assert (await reconciler.reconcile())["ok"]
        before = await db.list_playbook_activations()

        restarted = RequiredPlaybookReconciler(
            config=reconciler._config, db=db, handler=reconciler._handler
        )
        assert (await restarted.reconcile())["ok"]
        after = await db.list_playbook_activations()

        assert {
            (row["playbook_id"], row["activation_id"], row["active_artifact_sha256"])
            for row in after
        } == {
            (row["playbook_id"], row["activation_id"], row["active_artifact_sha256"])
            for row in before
        }
    finally:
        await db.close()


async def test_hash_mismatched_reviewed_bundle_is_a_readiness_diagnostic(tmp_path):
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import _fix_required

    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        digest = (
            tmp_path
            / "vault"
            / "reviewed-playbooks"
            / "default-assignment-routing"
            / "artifact.sha256"
        )
        digest.write_text("sha256:" + "0" * 64, encoding="utf-8")
        note = digest.parent / "operator-note.md"
        note.write_bytes(b"keep this review note\n")
        source = tmp_path / "vault/system/playbooks/default-assignment-routing.md"
        source.write_bytes(b"operator-owned routing source\n")
        consumed_receipt = None
        imported = handler._cmd_playbook_v2_import

        async def observe_receipt(params):
            nonlocal consumed_receipt
            consumed_receipt = (tmp_path / INSTALL_RECEIPT).read_bytes()
            assert json.loads(consumed_receipt)["activation_pending"] is False
            return await imported(params)

        handler._cmd_playbook_v2_import = AsyncMock(side_effect=observe_receipt)

        result = await reconciler.reconcile()

        assert not result["ok"]
        assert (
            "required-playbook-inactive"
            in result["required"]["default-assignment-routing"]["diagnostic"]
        )
        # One required bundle being invalid must not prevent the independent
        # Claude usage probe -- or the shared defaults -- from being activated.
        activations = await db.list_playbook_activations(enabled_only=True)
        assert {activation["playbook_id"] for activation in activations} == {
            "provider-usage-probe",
            "provider-failover",
            *DEFAULT_SYSTEM_PLAYBOOK_IDS,
        }
        assert "morning-report" not in {row["playbook_id"] for row in activations}
        assert handler._cmd_playbook_v2_import.await_count == len(
            REQUIRED_SYSTEM_PLAYBOOK_IDS + DEFAULT_SYSTEM_PLAYBOOK_IDS
        )
        before = {row["playbook_id"]: row for row in activations}

        status, diagnostic, bundles, ctx = await _read_only_restart(tmp_path, db)
        assert not status["ok"]
        assert diagnostic.severity is Severity.ERROR
        assert bundles.severity is Severity.WARN
        assert "aq doctor --check playbooks.reviewed_bundles --fix" in bundles.detail
        assert (await _fix_required(ctx)).severity is Severity.OK
        assert (tmp_path / INSTALL_RECEIPT).read_bytes() == consumed_receipt
        assert note.read_bytes() == b"keep this review note\n"
        assert source.read_bytes() == b"operator-owned routing source\n"
        backups = list(digest.parent.parent.glob("default-assignment-routing.bak-*"))
        assert len(backups) == 1
        assert (backups[0] / "artifact.sha256").read_bytes() == b"sha256:" + b"0" * 64
        assert (backups[0] / note.name).read_bytes() == note.read_bytes()
        after = {row["playbook_id"]: row for row in await db.list_playbook_activations()}
        assert {identifier: after[identifier] for identifier in before} == before
        assert set(after) == set(REQUIRED_SYSTEM_PLAYBOOK_IDS + DEFAULT_SYSTEM_PLAYBOOK_IDS)
        assert (await _read_only_restart(tmp_path, db))[0]["ok"]
    finally:
        await db.close()


async def test_route_events_during_an_inactive_gap_are_retained(tmp_path):
    db = Database(lease_dsn("pending.db"))
    await db.initialize()
    try:
        runtime = object.__new__(V2PlaybookRuntime)
        runtime._required_inactive_ids = {"default-assignment-routing"}
        runtime._db = db
        runtime._config = SimpleNamespace(
            playbooks=SimpleNamespace(v2_pending_event_retention_days=7)
        )
        runtime._engine = MagicMock()
        runtime._engine.dispatch_event = AsyncMock()

        await runtime._dispatch(
            {"_event_type": "task.route_needed", "event_id": "route-1", "task_id": "task-1"}
        )

        [pending] = await db.list_pending_events(playbook_id="default-assignment-routing")
        assert pending["event_type"] == "task.route_needed"
        assert pending["reason"] == "disabled"
        runtime._engine.dispatch_event.assert_awaited_once()
    finally:
        await db.close()


async def test_reconciliation_replays_retained_route_events_after_activation(tmp_path):
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        assert (await reconciler.reconcile())["ok"]
        await retain_required_route_needed_event(
            db, {"event_id": "route-1", "task_id": "task-1"}, ttl_days=7
        )
        handler._v2_replay_held_event = AsyncMock(return_value=(True, ("run-1",), None))

        replay = await reconciler.replay_route_needed_events()

        assert replay == {"replayed": True, "considered": 1, "run_ids": ["run-1"], "errors": []}
        handler._v2_replay_held_event.assert_awaited_once()
    finally:
        await db.close()


async def test_a_fresh_install_activates_the_shared_default_playbooks_healthy(tmp_path):
    """Settings -> Playbooks showed nothing on a fresh install but the required two.

    Health is computed against the live command contract registry, so a shipped
    bundle that has gone stale fails here rather than activating as stale.
    """
    db, _handler, reconciler = await _reconciler(tmp_path)
    try:
        result = await reconciler.reconcile()

        for playbook_id in DEFAULT_SYSTEM_PLAYBOOK_IDS:
            assert result["defaults"][playbook_id] == {
                "activated": True,
                "enabled": True,
                "health": "ready",
            }
        assert set(DEFAULT_SYSTEM_PLAYBOOK_IDS).isdisjoint(result["required"])
    finally:
        await db.close()


async def test_a_default_playbook_the_operator_disabled_stays_disabled(tmp_path):
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import _fix_required

    db, _handler, reconciler = await _reconciler(tmp_path)
    try:
        await reconciler.reconcile()
        row = next(
            activation
            for activation in await db.list_playbook_activations()
            if activation["playbook_id"] == "supervisor-failure-triage"
        )
        await db.set_playbook_activation(
            playbook_id="supervisor-failure-triage",
            scope="system",
            scope_identifier="",
            artifact_sha256=row["active_artifact_sha256"],
            enabled=False,
            activated_by="human:operator",
            health="ready",
            reasons="[]",
        )

        result, _diagnostic, _bundles, ctx = await _read_only_restart(tmp_path, db)

        after = next(
            activation
            for activation in await db.list_playbook_activations()
            if activation["playbook_id"] == "supervisor-failure-triage"
        )
        assert after["enabled"] is False
        assert result["defaults"]["supervisor-failure-triage"]["activated"] is False
        assert (await _fix_required(ctx)).severity is Severity.OK
        assert await _system_row(db, "supervisor-failure-triage") == after
        assert not any(row["playbook_id"] == "morning-report"
                       for row in await db.list_playbook_activations())
    finally:
        await db.close()


async def test_a_default_that_cannot_be_imported_does_not_fail_readiness(tmp_path):
    import shutil

    db, _handler, reconciler = await _reconciler(tmp_path)
    shutil.rmtree(tmp_path / "vault" / "reviewed-playbooks" / "supervisor-failure-triage")
    try:
        result = await reconciler.reconcile()

        assert result["ok"] is True
        assert result["defaults"]["supervisor-failure-triage"]["activated"] is False
        assert "error" in result["defaults"]["supervisor-failure-triage"]
    finally:
        await db.close()


async def test_restart_preserves_existing_predecessor_activation(tmp_path):
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        imported = await handler._cmd_playbook_v2_import(
            {"path": "reviewed-playbooks/blocked-task-escalation"}
        )
        assert imported["success"] is True
        await _activate(db, "blocked-task-escalation", imported["artifact_sha256"])

        await reconciler.reconcile()

        old = await _system_row(db, "blocked-task-escalation")
        successor = await _system_row(db, "supervisor-failure-triage")
        assert old["enabled"] is True
        assert old["health"] == "ready"
        assert successor["enabled"] is True
    finally:
        await db.close()


# -- reviewed bundles are daemon-owned: seeded, and refreshed on upgrade ------


def _vault_bundle(tmp_path, playbook_id):
    return tmp_path / "vault" / "reviewed-playbooks" / playbook_id


def _age_bundle(bundle):
    """Make a vault copy look like one an older release seeded."""
    (bundle / "artifact.json").write_bytes(b'{"seeded_by":"an older release"}')
    (bundle / "artifact.sha256").write_text("sha256:" + "d" * 64 + "\n", encoding="utf-8")


def test_every_shipped_reviewed_bundle_is_seeded_verbatim(tmp_path):
    written = ensure_reviewed_playbook_bundles(str(tmp_path))

    assert set(written) == set(shipped_reviewed_playbook_ids())
    assert set(REQUIRED_SYSTEM_PLAYBOOK_IDS) | set(DEFAULT_SYSTEM_PLAYBOOK_IDS) <= set(written)
    assert reviewed_bundle_drift(str(tmp_path)) == []
    assert ensure_reviewed_playbook_bundles(str(tmp_path)) == []


def test_a_drifted_vault_bundle_is_moved_aside_and_replaced(tmp_path):
    """Seeding was write-if-absent, so an upgraded install kept its first copy.

    That copy froze the capability fingerprints of the release that seeded it
    and could never validate again once a shipped profile's grants moved.
    """
    ensure_reviewed_playbook_bundles(str(tmp_path))
    bundle = _vault_bundle(tmp_path, "default-assignment-routing")
    shipped = reviewed_bundle_source() / "default-assignment-routing"
    _age_bundle(bundle)
    (bundle / "notes.md").write_text("operator note\n", encoding="utf-8")

    refreshed = ensure_reviewed_playbook_bundles(str(tmp_path))

    assert refreshed == ["default-assignment-routing"]
    for name in ("artifact.json", "artifact.sha256", "source.md", "manifest.md"):
        assert (bundle / name).read_bytes() == (shipped / name).read_bytes()
    # The old directory is moved aside whole, not overwritten: the stale bytes
    # and anything the operator kept beside them survive under a name the
    # importer refuses, because it no longer matches the playbook id.
    [superseded] = bundle.parent.glob("default-assignment-routing.bak-*")
    assert (superseded / "artifact.json").read_bytes() == b'{"seeded_by":"an older release"}'
    assert (superseded / "notes.md").read_text(encoding="utf-8") == "operator note\n"
    # Only the recording is daemon-owned: the operator's note is carried into
    # the refreshed copy, so it stays where they put it.
    assert (bundle / "notes.md").read_text(encoding="utf-8") == "operator note\n"
    assert not list(bundle.parent.glob(".*staging*"))
    # Idempotent: a vault that matches what ships is left alone.
    assert ensure_reviewed_playbook_bundles(str(tmp_path)) == []


def test_seeding_never_touches_a_bundle_aq_does_not_ship(tmp_path):
    custom = _vault_bundle(tmp_path, "operator-policy")
    custom.mkdir(parents=True)
    (custom / "artifact.json").write_text("{}", encoding="utf-8")

    ensure_reviewed_playbook_bundles(str(tmp_path))

    assert (custom / "artifact.json").read_text(encoding="utf-8") == "{}"
    assert not list(custom.parent.glob("operator-policy.bak-*"))


def test_reviewed_bundle_drift_names_what_differs(tmp_path):
    ensure_reviewed_playbook_bundles(str(tmp_path))
    import shutil

    shutil.rmtree(_vault_bundle(tmp_path, "provider-usage-probe"))
    _age_bundle(_vault_bundle(tmp_path, "default-assignment-routing"))

    drift = {row["playbook_id"]: row for row in reviewed_bundle_drift(str(tmp_path))}

    assert set(drift) == {"default-assignment-routing", "provider-usage-probe"}
    assert drift["provider-usage-probe"]["problem"] == "missing"
    routing = drift["default-assignment-routing"]
    assert routing["problem"] == "drifted"
    assert routing["files"] == ["artifact.json", "artifact.sha256"]
    assert routing["installed_sha256"] == "sha256:" + "d" * 64
    assert routing["shipped_sha256"] == (
        (reviewed_bundle_source() / "default-assignment-routing" / "artifact.sha256")
        .read_text(encoding="utf-8")
        .strip()
    )


# -- a broken system activation is repaired, a healthy or disabled one kept ---


def _shipped_variant(playbook_id, change):
    """The shipped artifact for *playbook_id*, with *change* applied to its body."""
    import json

    from src.playbooks.definition import PlaybookDefinition

    body = json.loads(
        (reviewed_bundle_source() / playbook_id / "artifact.json").read_text(encoding="utf-8")
    )
    change(body)
    return PlaybookDefinition.model_validate(body)


def _compiled_before_the_grants_moved(body):
    body["compiled_against"]["profiles"]["playbook-compiler"] = "sha256:" + "d" * 64


async def _store_variant(db, handler, definition):
    """Persist *definition* as an artifact row without the import's validation."""
    from src.playbooks.activation import profile_fingerprint

    profiles = dict(definition.compiled_against.profiles)
    ref = handler._store.put(
        definition,
        source_digest=definition.source_hash,
        contract_fingerprint=definition.contract_fingerprint(),
        profile_fingerprint=profile_fingerprint(profiles),
        compiler_build=definition.compiler_build or "test-build",
        version=definition.version,
    )
    await db.upsert_playbook_artifact(
        ref,
        scope="system",
        profile_fingerprint=profile_fingerprint(profiles),
        path=handler._store.path_for(ref.artifact_sha256),
        size_bytes=len(handler._store.canonical_bytes(definition)),
    )
    return ref.artifact_sha256


async def _activate(db, playbook_id, sha, *, enabled=True):
    await db.set_playbook_activation(
        playbook_id=playbook_id,
        scope="system",
        scope_identifier="",
        artifact_sha256=sha,
        enabled=enabled,
        activated_by="human:operator",
        health="ready" if enabled else "disabled",
        reasons="[]",
    )


async def _system_row(db, playbook_id):
    return next(
        row
        for row in await db.list_playbook_activations()
        if row["playbook_id"] == playbook_id and row["scope"] == "system"
    )


async def _activate_an_older_release(db, handler):
    """Point routing at the artifact an older release compiled.

    It names ``playbook-compiler`` with the capability fingerprint that profile
    had before its grants moved -- the ``da0fb778`` of the field report.
    """
    stale = _shipped_variant("default-assignment-routing", _compiled_before_the_grants_moved)
    sha = await _store_variant(db, handler, stale)
    await _activate(db, "default-assignment-routing", sha)
    return sha


async def test_a_stale_required_activation_is_repointed_at_the_shipped_artifact(tmp_path):
    """The field report: routing sat at stale_contract and no restart could fix it."""
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        stale_sha = await _activate_an_older_release(db, handler)
        shipped_sha = (
            (reviewed_bundle_source() / "default-assignment-routing" / "artifact.sha256")
            .read_text(encoding="utf-8")
            .strip()
        )
        assert stale_sha != shipped_sha

        result = await reconciler.reconcile()

        routing = result["required"]["default-assignment-routing"]
        assert result["ok"] is True
        assert routing == {
            "ok": True,
            "artifact_sha256": shipped_sha,
            "repointed_from": stale_sha,
        }
        row = await _system_row(db, "default-assignment-routing")
        assert row["active_artifact_sha256"] == shipped_sha
        assert row["enabled"] is True
    finally:
        await db.close()


async def test_a_disabled_required_activation_is_never_repointed(tmp_path):
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        stale_sha = await _activate_an_older_release(db, handler)
        await _activate(db, "default-assignment-routing", stale_sha, enabled=False)

        result = await reconciler.reconcile()

        row = await _system_row(db, "default-assignment-routing")
        assert (row["active_artifact_sha256"], row["enabled"]) == (stale_sha, False)
        assert result["required"]["default-assignment-routing"]["ok"] is False
    finally:
        await db.close()


async def test_a_healthy_operator_activation_is_kept(tmp_path):
    """Durable operator state: a compatible artifact of their own keeps serving."""
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        own = _shipped_variant("provider-usage-probe", lambda body: body.update(version=2))
        own_sha = await _store_variant(db, handler, own)
        await _activate(db, "provider-usage-probe", own_sha)

        result = await reconciler.reconcile()

        row = await _system_row(db, "provider-usage-probe")
        assert row["active_artifact_sha256"] == own_sha
        assert result["required"]["provider-usage-probe"] == {
            "ok": True,
            "artifact_sha256": own_sha,
        }
    finally:
        await db.close()


def _router_without_task_route_apply(body):
    """A router that plans but never writes: every ``task_route_apply`` step gone.

    Stands in for the superseded ``task_route`` router and for a vault compile
    that diverged from the shipped one: both validate and serve, and neither
    can write a router route (mandatory routing §6.7).
    """
    applies = {
        step_id
        for step_id, step in body["steps"].items()
        if step.get("command") == "task_route_apply"
    }
    for step_id in applies:
        del body["steps"][step_id]
    for step in body["steps"].values():
        for outcome, target in list((step.get("transitions") or {}).items()):
            if target in applies:
                step["transitions"][outcome] = "route-task--done"
    del body["compiled_against"]["commands"]["task_route_apply"]


async def _routing_health(handler):
    from src.playbooks.required import _system_activation

    records, _contracts, _profiles = await handler._v2_health_records()
    return _system_activation(records, "default-assignment-routing").health.value


async def test_a_ready_router_that_cannot_apply_a_route_is_repointed(tmp_path):
    """Upgrade path of §6.7: a healthy activation is re-pointed when it lacks the grant.

    Without this, the enabled, ready activation of the previous router would
    be durable operator state the reconciler never touches, and the rebuilt
    bundle would never serve.
    """
    from src.playbooks.definition import granted_aq_commands

    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        older = _shipped_variant("default-assignment-routing", _router_without_task_route_apply)
        assert "task_route_apply" not in granted_aq_commands(older)
        older_sha = await _store_variant(db, handler, older)
        await _activate(db, "default-assignment-routing", older_sha)
        assert await _routing_health(handler) == "ready"
        shipped_sha = (
            (reviewed_bundle_source() / "default-assignment-routing" / "artifact.sha256")
            .read_text(encoding="utf-8")
            .strip()
        )

        result = await reconciler.reconcile()

        assert result["required"]["default-assignment-routing"] == {
            "ok": True,
            "artifact_sha256": shipped_sha,
            "repointed_from": older_sha,
        }
        row = await _system_row(db, "default-assignment-routing")
        assert (row["active_artifact_sha256"], row["enabled"]) == (shipped_sha, True)
    finally:
        await db.close()


async def test_an_operator_router_that_grants_task_route_apply_is_kept(tmp_path):
    """An operator's own compatible router keeps serving (§6.7)."""
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        own = _shipped_variant("default-assignment-routing", lambda body: body.update(version=2))
        own_sha = await _store_variant(db, handler, own)
        await _activate(db, "default-assignment-routing", own_sha)
        assert await _routing_health(handler) == "ready"

        result = await reconciler.reconcile()

        assert result["required"]["default-assignment-routing"] == {
            "ok": True,
            "artifact_sha256": own_sha,
        }
        row = await _system_row(db, "default-assignment-routing")
        assert row["active_artifact_sha256"] == own_sha
    finally:
        await db.close()


async def test_a_disabled_router_without_the_grant_stays_disabled(tmp_path):
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        older = _shipped_variant("default-assignment-routing", _router_without_task_route_apply)
        older_sha = await _store_variant(db, handler, older)
        await _activate(db, "default-assignment-routing", older_sha, enabled=False)

        await reconciler.reconcile()

        row = await _system_row(db, "default-assignment-routing")
        assert (row["active_artifact_sha256"], row["enabled"]) == (older_sha, False)
    finally:
        await db.close()


async def test_a_stale_default_activation_is_repointed_too(tmp_path):
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        stale = _shipped_variant("supervisor-failure-triage", _compiled_before_the_grants_moved)
        stale_sha = await _store_variant(db, handler, stale)
        await _activate(db, "supervisor-failure-triage", stale_sha)

        result = await reconciler.reconcile()

        escalation = result["defaults"]["supervisor-failure-triage"]
        assert escalation["health"] == "ready"
        assert escalation["activated"] is False
        assert escalation["repointed_from"] == stale_sha
    finally:
        await db.close()


class _WidenedProfiles:
    """The operator's vault profile carries grants the shipped one does not."""

    def __init__(self, inner, profile_id):
        self._inner = inner
        self._profile_id = profile_id

    def policy(self, profile_id):
        import dataclasses

        policy = self._inner.policy(profile_id)
        if profile_id != self._profile_id or policy is None:
            return policy
        return dataclasses.replace(policy, aq_commands=policy.aq_commands | {"playbook_install"})

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _widen(handler, profile_id="playbook-compiler"):
    async def _v2_lookups():
        return (
            RegistryContractLookup(),
            _WidenedProfiles(shipped_profile_lookup(), profile_id),
            RegisteredEventLookup(),
        )

    handler._v2_lookups = _v2_lookups


async def test_an_unimportable_bundle_names_the_moved_fingerprint(tmp_path):
    """The diagnostic used to stop at 'does not validate against the live registries'."""
    from src.doctor.models import DoctorContext, Severity
    from src.doctor.playbook_v2_checks import _fix_required

    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        assert (await reconciler.reconcile())["ok"]
        _widen(handler)
        handler.orchestrator = SimpleNamespace(reconcile_required_playbooks=reconciler.reconcile)
        before = _policy_bytes(tmp_path)
        activations = await db.list_playbook_activations(enabled_only=False)

        with patch.object(db, "set_playbook_activation", wraps=db.set_playbook_activation) as activate:
            fixed = await _fix_required(DoctorContext(config=handler.config, db=db, handler=handler))
            activate.assert_not_awaited()
        assert fixed.severity is Severity.ERROR
        assert fixed.fix_applied
        assert not fixed.data["ok"]
        assert _policy_bytes(tmp_path) == before
        assert await db.list_playbook_activations(enabled_only=False) == activations
        result = fixed.data

        diagnostic = result["required"]["default-assignment-routing"]["diagnostic"]
        assert diagnostic.startswith(
            "required-playbook-inactive: default-assignment-routing: activation is stale_contract; "
            "reviewed artifact import failed: artifact does not validate against the live "
            "registries ("
        )
        assert "playbook-compiler" in diagnostic
        # Nothing was re-pointed: the shipped artifact is no better than the row.
        assert result["required"]["provider-usage-probe"]["ok"] is True
    finally:
        await db.close()


# -- readiness is recomputed, not a startup snapshot ---------------------------


async def test_refresh_status_clears_a_failure_repaired_after_startup(tmp_path):
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        digest = _vault_bundle(tmp_path, "default-assignment-routing") / "artifact.sha256"
        good = digest.read_text(encoding="utf-8")
        digest.write_text("sha256:" + "0" * 64, encoding="utf-8")
        assert (await reconciler.reconcile())["ok"] is False

        # The operator's hand repair: restore the bundle, import, activate.
        digest.write_text(good, encoding="utf-8")
        imported = await handler._cmd_playbook_v2_import(
            {"path": "reviewed-playbooks/default-assignment-routing"}
        )
        await _activate(db, "default-assignment-routing", imported["artifact_sha256"])

        status = await reconciler.refresh_status()

        assert status["ok"] is True
        assert status["required"]["default-assignment-routing"]["ok"] is True
        assert "defaults" in status
        assert reconciler.status is status
    finally:
        await db.close()


async def test_refresh_status_reports_a_break_after_startup(tmp_path):
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        assert (await reconciler.reconcile())["ok"] is True
        _widen(handler)

        status = await reconciler.refresh_status()

        assert status["ok"] is False
        assert status["required"]["default-assignment-routing"]["diagnostic"] == (
            "required-playbook-inactive: default-assignment-routing: activation is stale_contract"
        )
        # Read-only: the row still names the artifact it named.
        row = await _system_row(db, "default-assignment-routing")
        assert row["enabled"] is True
    finally:
        await db.close()


async def test_the_runtime_required_inactive_set_is_rebuilt_not_accumulated():
    runtime = object.__new__(V2PlaybookRuntime)
    ready_row = {
        "playbook_id": "default-assignment-routing",
        "scope": "system",
        "scope_identifier": "",
        "enabled": True,
        "health": "ready",
        "active_artifact_sha256": "sha256:" + "a" * 64,
    }
    probe_row = {**ready_row, "playbook_id": "provider-usage-probe"}
    failover_row = {**ready_row, "playbook_id": "provider-failover"}
    runtime._db = SimpleNamespace(
        list_playbook_activations=AsyncMock(return_value=[ready_row, probe_row, failover_row])
    )
    broken = {
        "ok": False,
        "required": {"default-assignment-routing": {"ok": False, "diagnostic": "stale"}},
    }
    await runtime.apply_required_playbook_status(broken)
    assert runtime._required_inactive_ids == {"default-assignment-routing"}

    await runtime.apply_required_playbook_status(
        {"ok": True, "required": {"default-assignment-routing": {"ok": True}}}
    )

    assert runtime._required_inactive_ids == set()


async def test_the_orchestrator_publishes_a_refreshed_status_to_the_runtime():
    from src.orchestrator.core import Orchestrator

    fresh = {"ok": True, "required": {}}
    manager = SimpleNamespace(apply_required_playbook_status=AsyncMock())
    orch = SimpleNamespace(
        required_playbook_reconciler=SimpleNamespace(refresh_status=AsyncMock(return_value=fresh)),
        required_playbook_status={"ok": False, "required": {}},
        playbook_manager=manager,
    )

    assert await Orchestrator.refresh_required_playbook_status(orch) is fresh
    assert orch.required_playbook_status is fresh
    manager.apply_required_playbook_status.assert_awaited_once_with(fresh)

    # A failed refresh keeps the last verdict rather than failing /health.
    orch.required_playbook_reconciler.refresh_status = AsyncMock(side_effect=RuntimeError("db"))
    assert await Orchestrator.refresh_required_playbook_status(orch) is fresh


async def test_health_reads_the_recomputed_required_status():
    from src.main import _health_checks

    fresh = {"ok": True, "required": {}}
    orch = MagicMock()
    orch._running_tasks = {}
    orch.required_playbook_status = {"ok": False, "required": {}}
    orch.refresh_required_playbook_status = AsyncMock(return_value=fresh)
    orch.db.list_agents = AsyncMock(return_value=[])
    orch.db.count_tasks_by_status = AsyncMock(return_value={})
    adapter = MagicMock()

    checks = await _health_checks(orch, adapter)

    assert checks["required_playbooks"] is fresh
    orch.refresh_required_playbook_status.assert_awaited_once()


async def test_an_activation_write_publishes_required_status(tmp_path):
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        await reconciler.reconcile()
        refresh = AsyncMock()
        handler.orchestrator = SimpleNamespace(
            playbook_manager=None, refresh_required_playbook_status=refresh
        )

        result = await handler._cmd_set_playbook_enabled(
            {"playbook_id": "provider-usage-probe", "enabled": False}
        )

        assert result["success"] is True
        refresh.assert_awaited_once()
    finally:
        await db.close()



async def test_activation_writes_reach_the_timer_service_without_a_restart(tmp_path):
    """The timer service follows the runtime's trigger map.  Activation writes
    used to refresh only the routing snapshot, so a playbook activated on a
    ``timer.*`` interval nothing else used (``morning-report`` on
    ``timer.1m``) never fired until the daemon restarted."""
    from src.config import PlaybooksConfig
    from src.timer_service import TimerService

    db, handler, reconciler = await _reconciler(tmp_path)
    runtime = V2PlaybookRuntime(
        config=SimpleNamespace(
            compiled_root=handler.config.compiled_root,
            playbooks=PlaybooksConfig(enabled=True),
            security=SimpleNamespace(capability_enforcement="enforce"),
        ),
        db=db,
        handler=SimpleNamespace(),
        llm=None,
        bus=None,
    )
    try:
        await reconciler.reconcile()
        await runtime.refresh()
        handler.orchestrator = SimpleNamespace(playbook_manager=runtime)
        timer = TimerService(event_bus=SimpleNamespace(emit=AsyncMock()), playbook_manager=runtime)
        timer.start()
        assert "timer.10m" in timer.active_intervals  # probe and stuck-task watch

        paused = await handler._cmd_set_playbook_enabled(
            {"playbook_id": "provider-usage-probe", "enabled": False}
        )
        assert paused["success"] is True
        await timer.tick()
        assert "timer.10m" in timer.active_intervals  # the watch still subscribes
        paused_watch = await handler._cmd_set_playbook_enabled(
            {"playbook_id": "stuck-task-watch", "enabled": False}
        )
        assert paused_watch["success"] is True
        await timer.tick()
        assert "timer.10m" not in timer.active_intervals

        [row] = [
            row
            for row in await db.list_playbook_activations(enabled_only=False)
            if row["playbook_id"] == "provider-usage-probe"
        ]
        activated = await handler._cmd_playbook_activate(
            {
                "playbook_id": "provider-usage-probe",
                "artifact_sha256": row["active_artifact_sha256"],
            }
        )
        assert activated.get("success") is True, activated
        await timer.tick()
        assert "timer.10m" in timer.active_intervals
    finally:
        await runtime.shutdown()
        await db.close()

# -- doctor: playbooks.reviewed_bundles ----------------------------------------


def _doctor_ctx(tmp_path, handler=None):
    from src.doctor.models import DoctorContext

    return DoctorContext(
        config=SimpleNamespace(data_dir=str(tmp_path), playbooks=SimpleNamespace(enabled=True)),
        handler=handler,
    )


async def test_the_reviewed_bundles_doctor_check_warns_and_fixes(tmp_path):
    from src.doctor.models import Severity
    from src.doctor.playbook_v2_checks import (
        REVIEWED_BUNDLES_CHECK_ID,
        _check_reviewed_bundles,
        _fix_reviewed_bundles,
    )

    ensure_reviewed_playbook_bundles(str(tmp_path))
    assert (await _check_reviewed_bundles(_doctor_ctx(tmp_path))).severity is Severity.OK
    _age_bundle(_vault_bundle(tmp_path, "default-assignment-routing"))

    result = await _check_reviewed_bundles(_doctor_ctx(tmp_path))

    assert result.id == REVIEWED_BUNDLES_CHECK_ID
    assert result.severity is Severity.WARN
    assert "default-assignment-routing" in result.detail
    assert result.data["drift"][0]["problem"] == "drifted"

    reconcile = AsyncMock(return_value={"ok": True})
    handler = SimpleNamespace(orchestrator=SimpleNamespace(reconcile_required_playbooks=reconcile))
    fixed = await _fix_reviewed_bundles(_doctor_ctx(tmp_path, handler))

    assert fixed.data["written"] == ["default-assignment-routing"]
    reconcile.assert_awaited_once()
    assert (await _check_reviewed_bundles(_doctor_ctx(tmp_path))).severity is Severity.OK


def test_the_reviewed_bundles_check_is_registered_with_a_fix():
    from src.doctor import default_registry
    from src.doctor.playbook_v2_checks import REVIEWED_BUNDLES_CHECK_ID

    check = next(c for c in default_registry().checks() if c.id == REVIEWED_BUNDLES_CHECK_ID)
    assert check.owner == "playbook-v2"
    assert check.fix is not None


def test_every_shipped_reviewed_bundle_reaches_the_vault(tmp_path):
    """A bundle the vault does not hold is one no operator can import.

    ``playbook_v2_import`` refuses any path outside the vault root, so the
    project-scoped ``ci-main-sentinel`` recording — which the reconciler never
    activates — is importable only because seeding copies it across.
    """
    written = ensure_reviewed_playbook_bundles(str(tmp_path))
    root = tmp_path / "vault" / "reviewed-playbooks"

    assert set(written) == set(shipped_reviewed_playbook_ids())
    assert "ci-main-sentinel" in written
    for playbook_id in shipped_reviewed_playbook_ids():
        shipped = reviewed_bundle_source() / playbook_id
        for name in ("artifact.json", "artifact.sha256", "source.md", "manifest.md"):
            assert (root / playbook_id / name).read_bytes() == (shipped / name).read_bytes()


def test_a_stale_vault_bundle_is_refreshed_from_the_shipped_bytes(tmp_path):
    """Seeding used to skip any id the vault already had, which stranded upgrades.

    A bundle rebuilt against a changed command contract never reached an
    existing install: the vault copy was written once and kept forever, and
    ``playbook_v2_import`` cannot read the corrected bytes from anywhere else.
    Three activations sat at ``stale_contract`` for that reason alone.
    """
    ensure_reviewed_playbook_bundles(str(tmp_path))
    bundle = tmp_path / "vault" / "reviewed-playbooks" / "default-pipeline"
    shipped = reviewed_bundle_source() / "default-pipeline"
    (bundle / "artifact.json").write_bytes(b'{"stale":true}')
    (bundle / "artifact.sha256").write_text("sha256:stale\n", encoding="utf-8")
    (bundle / "review.md").write_text("operator note\n", encoding="utf-8")

    refreshed = ensure_reviewed_playbook_bundles(str(tmp_path))

    assert refreshed == ["default-pipeline"]
    assert (bundle / "artifact.json").read_bytes() == (shipped / "artifact.json").read_bytes()
    assert (bundle / "artifact.sha256").read_bytes() == (shipped / "artifact.sha256").read_bytes()
    # Only the recording's own files are rewritten; anything else is the
    # operator's and is left where they put it.
    assert (bundle / "review.md").read_text(encoding="utf-8") == "operator note\n"
    assert ensure_reviewed_playbook_bundles(str(tmp_path)) == []


async def test_a_refreshed_bundle_imports_as_the_current_artifact(tmp_path):
    """The end of the supply line: stale vault bytes must not outlive a restart.

    Importing the seeded bundle has to yield the artifact the repository
    recorded, not the superseded one the vault happened to be holding.
    """
    db = Database(lease_dsn("required-playbooks.db"))
    await db.initialize()
    try:
        ensure_reviewed_playbook_bundles(str(tmp_path))
        bundle = tmp_path / "vault" / "reviewed-playbooks" / "ci-main-sentinel"
        superseded = (
            (bundle / "artifact.json").read_bytes().replace(b"ci-main-sentinel", b"stale-x")
        )
        (bundle / "artifact.json").write_bytes(superseded)

        ensure_reviewed_playbook_bundles(str(tmp_path))
        handler = _Handler(tmp_path, db)
        imported = await handler._cmd_playbook_v2_import(
            {"path": "reviewed-playbooks/ci-main-sentinel"}
        )

        shipped_sha = (
            (reviewed_bundle_source() / "ci-main-sentinel" / "artifact.sha256")
            .read_text(encoding="utf-8")
            .strip()
        )
        assert imported["success"], imported.get("error")
        assert imported["artifact_sha256"] == shipped_sha
    finally:
        await db.close()
