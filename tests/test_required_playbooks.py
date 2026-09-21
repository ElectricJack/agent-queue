"""Required V2 playbooks survive daemon restarts and activation gaps."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.commands.playbook_v2_commands import PlaybookV2CommandsMixin
from src.database import Database
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.profiles import shipped_profile_lookup
from src.playbooks.required import (
    DEFAULT_SYSTEM_PLAYBOOK_IDS,
    REQUIRED_SYSTEM_PLAYBOOK_IDS,
    RequiredPlaybookReconciler,
    ensure_reviewed_playbook_bundles,
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
    assert not (bundle / "notes.md").exists()
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


async def test_a_stale_default_activation_is_repointed_too(tmp_path):
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        stale = _shipped_variant("blocked-task-escalation", _compiled_before_the_grants_moved)
        stale_sha = await _store_variant(db, handler, stale)
        await _activate(db, "blocked-task-escalation", stale_sha)

        result = await reconciler.reconcile()

        escalation = result["defaults"]["blocked-task-escalation"]
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
    db, handler, reconciler = await _reconciler(tmp_path)
    try:
        assert (await reconciler.reconcile())["ok"]
        _widen(handler)

        result = await reconciler.reconcile()

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
    runtime._db = SimpleNamespace(
        list_playbook_activations=AsyncMock(return_value=[ready_row, probe_row])
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
    orch.db.list_tasks = AsyncMock(return_value=[])
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
