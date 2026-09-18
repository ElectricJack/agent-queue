"""Required V2 playbooks survive daemon restarts and activation gaps."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.commands.playbook_v2_commands import PlaybookV2CommandsMixin
from src.database import Database
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.required import (
    DEFAULT_SYSTEM_PLAYBOOK_IDS,
    REQUIRED_SYSTEM_PLAYBOOK_IDS,
    RequiredPlaybookReconciler,
    ensure_reviewed_playbook_bundles,
    retain_required_route_needed_event,
    reviewed_bundle_source,
    shipped_reviewed_playbook_ids,
)
from src.playbooks.runtime import V2PlaybookRuntime
from src.playbooks.profiles import shipped_profile_lookup
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
    ensure_reviewed_playbook_bundles(str(tmp_path))
    handler = _Handler(tmp_path, db)
    reconciler = RequiredPlaybookReconciler(config=handler.config, db=db, handler=handler)
    return db, handler, reconciler


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
    db, _handler, reconciler = await _reconciler(tmp_path)
    try:
        digest = (
            tmp_path
            / "vault"
            / "reviewed-playbooks"
            / "default-assignment-routing"
            / "artifact.sha256"
        )
        digest.write_text("sha256:" + "0" * 64, encoding="utf-8")

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
            *DEFAULT_SYSTEM_PLAYBOOK_IDS,
        }
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
    db, _handler, reconciler = await _reconciler(tmp_path)
    try:
        await reconciler.reconcile()
        row = next(
            activation
            for activation in await db.list_playbook_activations()
            if activation["playbook_id"] == "blocked-task-escalation"
        )
        await db.set_playbook_activation(
            playbook_id="blocked-task-escalation",
            scope="system",
            scope_identifier="",
            artifact_sha256=row["active_artifact_sha256"],
            enabled=False,
            activated_by="human:operator",
            health="ready",
            reasons="[]",
        )

        restarted = RequiredPlaybookReconciler(
            config=reconciler._config, db=db, handler=reconciler._handler
        )
        result = await restarted.reconcile()

        after = next(
            activation
            for activation in await db.list_playbook_activations()
            if activation["playbook_id"] == "blocked-task-escalation"
        )
        assert after["enabled"] is False
        assert result["defaults"]["blocked-task-escalation"]["activated"] is False
    finally:
        await db.close()


async def test_a_default_that_cannot_be_imported_does_not_fail_readiness(tmp_path):
    import shutil

    db, _handler, reconciler = await _reconciler(tmp_path)
    shutil.rmtree(tmp_path / "vault" / "reviewed-playbooks" / "blocked-task-escalation")
    try:
        result = await reconciler.reconcile()

        assert result["ok"] is True
        assert result["defaults"]["blocked-task-escalation"]["activated"] is False
        assert "error" in result["defaults"]["blocked-task-escalation"]
    finally:
        await db.close()


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
        superseded = (bundle / "artifact.json").read_bytes().replace(b"ci-main-sentinel", b"stale-x")
        (bundle / "artifact.json").write_bytes(superseded)

        ensure_reviewed_playbook_bundles(str(tmp_path))
        handler = _Handler(tmp_path, db)
        imported = await handler._cmd_playbook_v2_import({"path": "reviewed-playbooks/ci-main-sentinel"})

        shipped_sha = (
            (reviewed_bundle_source() / "ci-main-sentinel" / "artifact.sha256")
            .read_text(encoding="utf-8")
            .strip()
        )
        assert imported["success"], imported.get("error")
        assert imported["artifact_sha256"] == shipped_sha
    finally:
        await db.close()
