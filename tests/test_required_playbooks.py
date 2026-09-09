"""Required V2 playbooks survive daemon restarts and activation gaps."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.commands.playbook_v2_commands import PlaybookV2CommandsMixin
from src.database import Database
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.required import (
    RequiredPlaybookReconciler,
    ensure_reviewed_playbook_bundles,
    retain_required_route_needed_event,
)
from src.playbooks.runtime import V2PlaybookRuntime
from src.playbooks.profiles import shipped_profile_lookup
from src.playbooks.validation import RegisteredEventLookup, RegistryContractLookup


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
    db = Database(str(tmp_path / "required-playbooks.db"))
    await db.initialize()
    ensure_reviewed_playbook_bundles(str(tmp_path))
    handler = _Handler(tmp_path, db)
    reconciler = RequiredPlaybookReconciler(config=handler.config, db=db, handler=handler)
    return db, handler, reconciler


async def test_fresh_database_bootstraps_required_routing_activation(tmp_path):
    db, _handler, reconciler = await _reconciler(tmp_path)
    try:
        result = await reconciler.reconcile()
        [activation] = await db.list_playbook_activations(enabled_only=True)

        assert result["ok"]
        assert activation["playbook_id"] == "default-assignment-routing"
        assert activation["scope"] == "system"
        assert activation["enabled"] is True
    finally:
        await db.close()


async def test_restart_rehydrates_durable_activation_without_rewriting_it(tmp_path):
    db, _handler, reconciler = await _reconciler(tmp_path)
    try:
        assert (await reconciler.reconcile())["ok"]
        [before] = await db.list_playbook_activations()

        restarted = RequiredPlaybookReconciler(
            config=reconciler._config, db=db, handler=reconciler._handler
        )
        assert (await restarted.reconcile())["ok"]
        [after] = await db.list_playbook_activations()

        assert after["activation_id"] == before["activation_id"]
        assert after["active_artifact_sha256"] == before["active_artifact_sha256"]
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
        assert await db.list_playbook_activations() == []
    finally:
        await db.close()


async def test_route_events_during_an_inactive_gap_are_retained(tmp_path):
    db = Database(str(tmp_path / "pending.db"))
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
