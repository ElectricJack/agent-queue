"""One-time playbook installation, read-only readiness and explicit repair.

Restart rehydrates subscriptions from durable activations. It never restores
deleted policy or replaces an operator's activation with a shipped artifact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# These policies must be active for the daemon to provide its baseline
# guarantees.  The usage probe remains opt-out through
# ``providers.claude.usage_probe_enabled``; making its timer required removes
# the accidental manual activation step that otherwise leaves Claude's meter
# empty on every fresh install.
REQUIRED_SYSTEM_PLAYBOOK_IDS = (
    "default-assignment-routing",
    "provider-usage-probe",
    # Moving work off an unavailable provider is policy (provider-failover
    # D11).  Without it tasks on a dead provider hold and never move.
    "provider-failover",
)
# Activated only by the first-install receipt, never on an existing installation.
# These defaults are not readiness requirements.
DEFAULT_SYSTEM_PLAYBOOK_IDS = ("supervisor-failure-triage", "default-pipeline", "stuck-task-watch")
# Explicit installation inventory. Morning reporting still requires operator
# activation and reports.morning.enabled. Project defaults are code-owned
# creation/graft settings in this release, not a separate shipped playbook.
# Retired policies remain available as reviewed bundles for explicit import,
# but are absent from both this inventory and the default source directory.
INITIAL_SYSTEM_PLAYBOOK_IDS = (
    *REQUIRED_SYSTEM_PLAYBOOK_IDS,
    *DEFAULT_SYSTEM_PLAYBOOK_IDS,
    "morning-report",
)
INSTALL_RECEIPT = "playbook-installation.json"
# A missing grant fails readiness; only explicit repair replaces the artifact.
# Mandatory routing (spec 2026-09-28 §6.7): only ``task_route_apply`` writes a
# router route, so an older router -- the shipped ``task_route`` artifact, or a
# vault compile that diverged from it -- would stay ready and route nothing a
# worker may claim.  An operator's own router that grants it is left alone.
REQUIRED_GRANTS = {"default-assignment-routing": "task_route_apply"}
_ACTOR = "service:required-playbook-reconciler"


def reviewed_bundle_source() -> Path:
    """The reviewed bundles shipped with the daemon, not test fixtures."""
    return Path(__file__).resolve().parents[1] / "prompts" / "reviewed_playbooks"


def install_initial_playbooks(data_dir: str) -> list[str]:
    """Install once for an absent/empty vault, never infer intent from deletion.

    The receipt is outside the vault so removing the vault cannot reseed it.
    Record intent before writes: a partial install needs explicit doctor repair.
    Existing vaults get only a receipt, with no pending activation or migration.
    """
    root = Path(data_dir)
    receipt = root / INSTALL_RECEIPT
    if receipt.exists():
        return []
    vault = root / "vault"
    root.mkdir(parents=True, exist_ok=True)
    first = not vault.exists() or not any(vault.iterdir())
    try:
        with receipt.open("x", encoding="utf-8") as handle:
            json.dump({"version": 1, "activation_pending": first}, handle)
    except FileExistsError:
        return []
    if not first:
        return []
    written = ensure_reviewed_playbook_bundles(data_dir, ids=INITIAL_SYSTEM_PLAYBOOK_IDS)
    sources = vault / "system" / "playbooks"
    sources.mkdir(parents=True, exist_ok=True)
    for identifier in written:
        shutil.copy2(reviewed_bundle_source() / identifier / "source.md", sources / f"{identifier}.md")
    return written


def _consume_initial_activation(data_dir: str) -> bool:
    receipt = Path(data_dir) / INSTALL_RECEIPT
    if not receipt.is_file():
        return False
    try:
        state = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.error("Cannot read playbook installation receipt; use explicit doctor repair")
        return False
    if not isinstance(state, dict) or state.get("version") != 1:
        logger.error("Invalid playbook installation receipt; use explicit doctor repair")
        return False
    if not state.get("activation_pending"):
        return False
    state["activation_pending"] = False
    staging = receipt.with_suffix(".tmp")
    staging.write_text(json.dumps(state) + "\n", encoding="utf-8")
    staging.replace(receipt)
    return True


def shipped_reviewed_playbook_ids() -> tuple[str, ...]:
    """Every reviewed bundle available for deliberate installation."""
    source_root = reviewed_bundle_source()
    if not source_root.is_dir():
        return ()
    return tuple(
        sorted(
            entry.name
            for entry in source_root.iterdir()
            if entry.is_dir() and not entry.name.startswith((".", "_"))
        )
    )


def _drifted_files(source: Path, destination: Path) -> list[str]:
    """Shipped bundle files the installed copy lacks or holds other bytes for.

    Files only the installed copy has are not drift: they cannot change what
    an import reads.  A refresh carries them into the new copy.
    """
    drifted: list[str] = []
    for shipped in sorted(source.iterdir()):
        if not shipped.is_file():
            continue
        installed = destination / shipped.name
        if not installed.is_file() or installed.read_bytes() != shipped.read_bytes():
            drifted.append(shipped.name)
    return drifted


def _recorded_sha(bundle: Path) -> str | None:
    try:
        return (bundle / "artifact.sha256").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def reviewed_bundle_drift(data_dir: str, *, ids: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Each shipped bundle whose vault copy is missing or differs from it.

    Read-only; ``ensure_reviewed_playbook_bundles`` is the repair.  An empty
    list means every vault copy is byte-identical to what this daemon ships.
    """
    destination_root = Path(data_dir) / "vault" / "reviewed-playbooks"
    drift: list[dict[str, Any]] = []
    for playbook_id in ids if ids is not None else shipped_reviewed_playbook_ids():
        source = reviewed_bundle_source() / playbook_id
        destination = destination_root / playbook_id
        shipped_sha = _recorded_sha(source)
        if not destination.is_dir():
            drift.append(
                {"playbook_id": playbook_id, "problem": "missing", "shipped_sha256": shipped_sha}
            )
            continue
        files = _drifted_files(source, destination)
        if files:
            drift.append(
                {
                    "playbook_id": playbook_id,
                    "problem": "drifted",
                    "files": files,
                    "installed_sha256": _recorded_sha(destination),
                    "shipped_sha256": shipped_sha,
                }
            )
    return drift


def _superseded_path(destination: Path) -> Path:
    """A free ``<id>.bak-<epoch>`` sibling for a vault bundle being replaced.

    The name no longer matches the playbook id, so ``playbook_v2_import``
    refuses it and nothing can activate the superseded bytes by accident.
    """
    stem = f"{destination.name}.bak-{int(time.time())}"
    candidate = destination.with_name(stem)
    suffix = 1
    while candidate.exists():
        candidate = destination.with_name(f"{stem}-{suffix}")
        suffix += 1
    return candidate


def _carry_operator_files(installed: Path, staging: Path) -> None:
    """Copy what the operator kept beside the recording into the refreshed copy.

    Only the shipped recording is daemon-owned.  A note or review an operator
    put in the bundle directory is theirs, so it stays where they put it; the
    superseded directory keeps its own copy too.
    """
    for entry in installed.iterdir():
        target = staging / entry.name
        if target.exists() or target.is_symlink():
            continue  # a shipped file: the recording wins
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target, follow_symlinks=False)


def ensure_reviewed_playbook_bundles(
    data_dir: str, *, ids: tuple[str, ...] | None = None
) -> list[str]:
    """Seed the shipped reviewed bundles into the vault and refresh stale copies.

    Seeding used to be write-if-absent, so the vault copy was written once and
    kept forever while the shipped bundle moved on with each release.  Once a
    referenced profile's grants changed, the old copy could never validate
    again and ``default-assignment-routing`` stayed inactive -- ``/health``
    503 -- on every later start.

    A vault copy that differs from the shipped recording is now replaced.  The
    old directory is moved aside to ``<id>.bak-<epoch>`` rather than
    overwritten, so the superseded bytes survive.  The new copy is staged
    beside the destination and renamed into place, so a failed copy never
    leaves a half-written bundle under the importable name.  Files an operator
    kept beside the recording are carried into the new copy, so they stay
    where they put it.  An id AQ does not ship is never touched.

    A reviewed bundle is content-addressed and the artifact an activation
    points at lives in the compiled store, so replacing these bytes retires
    nothing by itself: it changes what the *next* import reads.

    Returns the ids whose vault copy this call wrote, new or refreshed.
    """
    destination_root = Path(data_dir) / "vault" / "reviewed-playbooks"
    written: list[str] = []
    for playbook_id in ids if ids is not None else shipped_reviewed_playbook_ids():
        source = reviewed_bundle_source() / playbook_id
        destination = destination_root / playbook_id
        present = destination.exists() or destination.is_symlink()
        staging = destination_root / f".{playbook_id}.staging-{os.getpid()}"
        try:
            if present and destination.is_dir() and not _drifted_files(source, destination):
                continue
            destination_root.mkdir(parents=True, exist_ok=True)
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(source, staging)
            if present and destination.is_dir():
                _carry_operator_files(destination, staging)
            superseded = None
            if present:
                superseded = _superseded_path(destination)
                destination.rename(superseded)
            staging.rename(destination)
        except OSError:
            logger.exception("Could not seed the reviewed %s bundle into the vault", playbook_id)
            shutil.rmtree(staging, ignore_errors=True)
            continue
        if superseded is not None:
            logger.warning(
                "Refreshed the reviewed %s bundle in the vault from the shipped recording "
                "(%s -> %s); the superseded copy is at %s",
                playbook_id,
                _recorded_sha(superseded),
                _recorded_sha(destination),
                superseded,
            )
        written.append(playbook_id)
    return written


def _system_activation(records: Any, playbook_id: str) -> Any:
    return next(
        (
            record
            for record in records
            if record.playbook_id == playbook_id
            and record.scope == "system"
            and record.scope_identifier == ""
        ),
        None,
    )


def _is_ready(activation: Any) -> bool:
    return activation is not None and activation.enabled and activation.health.value == "ready"


def _import_error(imported: dict[str, Any]) -> str:
    """The import failure, with the first blocking diagnostic spelled out.

    ``artifact does not validate against the live registries`` alone sends an
    operator to ``aq playbook v2-validate`` to learn which contract or profile
    moved; the diagnostic that says so is already in the import result.
    """
    message = str(imported.get("error") or "unknown error")
    blocking = [
        row
        for row in imported.get("diagnostics") or ()
        if isinstance(row, dict) and row.get("severity") in {"error", "question"}
    ]
    if blocking:
        first = blocking[0]
        where = " ".join(str(part) for part in (first.get("code"), first.get("field")) if part)
        message += f" ({where}: {first.get('message')}"
        if len(blocking) > 1:
            message += f"; {len(blocking) - 1} more"
        message += ")"
    return message


def _required_state(playbook_id: str, activation: Any, import_error: str | None) -> dict[str, Any]:
    """Readiness of one required playbook: is its system activation serving?

    A ready activation is readiness even when the reviewed import failed -- an
    operator may have activated their own compatible artifact -- so the import
    error only explains a failure, it is not one by itself.
    """
    if _is_ready(activation):
        return {"ok": True, "artifact_sha256": activation.active_artifact_sha256}
    prefix = f"required-playbook-inactive: {playbook_id}: "
    if activation is None and import_error is not None:
        diagnostic = f"{prefix}reviewed artifact import failed: {import_error}"
    else:
        health = activation.health.value if activation is not None else "missing"
        diagnostic = f"{prefix}activation is {health}"
        if import_error is not None:
            diagnostic += f"; reviewed artifact import failed: {import_error}"
    return {"ok": False, "diagnostic": diagnostic,
            "fix_command": "aq doctor --check playbooks.required --fix"}


class RequiredPlaybookReconciler:
    """Observe durable policy; bootstrap once and repair only when requested."""

    def __init__(self, *, config: Any, db: Any, handler: Any) -> None:
        self._config = config
        self._db = db
        self._handler = handler
        self.status: dict[str, Any] = {"ok": True, "required": {}}
        # The last reconcile's import failures, so a status refresh can keep
        # explaining *why* a required activation is not serving.
        self._import_errors: dict[str, str] = {}
        # reconcile() and refresh_status() both publish ``status``; a health
        # read racing the late startup reconcile must not overwrite it with an
        # older verdict.
        self._lock = asyncio.Lock()

    async def reconcile(self, *, repair: bool = False) -> dict[str, Any]:
        """Observe existing policy; install only once or on explicit repair.

        A missing activation is not evidence of a new installation. The external
        installation receipt authorizes the one initial attempt, consumed before
        writes so a crash cannot turn later restarts into automatic repairs.
        """
        async with self._lock:
            initial = await asyncio.to_thread(_consume_initial_activation, self._config.data_dir)
            if not initial and not repair:
                return await self._refresh_status_locked()
            required: dict[str, dict[str, Any]] = {}
            import_errors: dict[str, str] = {}
            for playbook_id in REQUIRED_SYSTEM_PLAYBOOK_IDS:
                activation, import_error, repointed_from = await self._install_shipped(
                    playbook_id, enable_disabled=repair
                )
                if import_error is not None:
                    import_errors[playbook_id] = import_error
                required[playbook_id] = _required_state(playbook_id, activation, import_error)
                if repointed_from is not None:
                    required[playbook_id]["repointed_from"] = repointed_from

            # Optional defaults are installed once, never resurrected by a
            # required-policy repair. Their deliberate import remains available.
            defaults = await self._activate_defaults() if initial else {}
            self._import_errors = import_errors
            self.status = {
                "ok": all(item["ok"] for item in required.values()),
                "required": required,
                "defaults": defaults,
            }
            for item in required.values():
                if not item["ok"]:
                    logger.error("%s", item["diagnostic"])
            return self.status

    async def refresh_status(self) -> dict[str, Any]:
        """Recompute readiness from the activations as they are right now.

        Read-only.  ``reconcile`` runs only at startup, so its verdict used to
        be the one ``/health`` served for the life of the process: a required
        activation an operator repaired by hand kept the daemon ``degraded``
        until a restart.  Health is computed against the live registries, so
        a profile edit that breaks or heals an activation shows up here too.
        """
        async with self._lock:
            return await self._refresh_status_locked()

    async def _refresh_status_locked(self) -> dict[str, Any]:
        records, _contracts, _profiles = await self._handler._v2_health_records()
        required = {
            playbook_id: _required_state(
                playbook_id,
                _system_activation(records, playbook_id),
                self._import_errors.get(playbook_id),
            )
            for playbook_id in REQUIRED_SYSTEM_PLAYBOOK_IDS
        }
        for playbook_id, state in required.items():
            if state["ok"]:
                missing = await self._missing_grant(playbook_id, state["artifact_sha256"])
                if missing:
                    state.update(ok=False, diagnostic=f"{playbook_id}: missing grant {missing}")
            if not state["ok"]:
                state["fix_command"] = "aq doctor --check playbooks.required --fix"
        defaults = {
            identifier: {
                "activated": False,
                "enabled": bool(activation and activation.enabled),
                "health": activation.health.value if activation else "missing",
            }
            for identifier in DEFAULT_SYSTEM_PLAYBOOK_IDS
            for activation in [_system_activation(records, identifier)]
        }
        self.status = {
            **self.status,
            "defaults": defaults,
            "ok": all(item["ok"] for item in required.values()),
            "required": required,
        }
        return self.status

    async def _install_shipped(
        self, playbook_id: str, *, enable_disabled: bool = False
    ) -> tuple[Any, str | None, str | None]:
        """Import the shipped bundle and point the system activation at it if needed.

        Returns ``(activation, import_error, repointed_from)``.  The activation
        is written only when it is missing, or when it is enabled, the shipped
        artifact is a different hash, and it is either not ready or its
        artifact lacks the command :data:`REQUIRED_GRANTS` names for it;
        otherwise the durable row is left exactly as it is.
        """
        imported = await self._handler._cmd_playbook_v2_import(
            {"path": f"reviewed-playbooks/{playbook_id}"}
        )
        records, _contracts, _profiles = await self._handler._v2_health_records()
        activation = _system_activation(records, playbook_id)
        if not imported.get("success"):
            return activation, _import_error(imported), None

        shipped_sha = imported["artifact_sha256"]
        repointed_from = None
        if activation is not None:
            if (not activation.enabled and not enable_disabled) or (
                activation.enabled and activation.active_artifact_sha256 == shipped_sha
            ):
                return activation, None, None
            why = activation.health.value
            if why == "ready":
                missing = await self._missing_grant(
                    playbook_id, activation.active_artifact_sha256
                )
                if missing is None:
                    return activation, None, None
                why = f"ready, but its artifact does not grant {missing}"
            repointed_from = activation.active_artifact_sha256
            logger.warning(
                "Re-pointing the %s system activation from %s (%s) to the shipped reviewed "
                "artifact %s",
                playbook_id,
                repointed_from,
                why,
                shipped_sha,
            )
        await self._db.set_playbook_activation(
            playbook_id=playbook_id,
            scope="system",
            scope_identifier="",
            artifact_sha256=shipped_sha,
            enabled=True,
            activated_by=_ACTOR,
            health="ready",
            reasons="[]",
        )
        records, _contracts, _profiles = await self._handler._v2_health_records()
        return _system_activation(records, playbook_id), None, repointed_from

    async def _missing_grant(self, playbook_id: str, artifact_sha256: str) -> str | None:
        """The :data:`REQUIRED_GRANTS` command *playbook_id*'s artifact lacks, if any.

        The grant is read from the immutable artifact itself
        (:func:`~src.playbooks.definition.granted_aq_commands`), exactly as
        ``task_route_apply`` reads it off the live invocation.  An artifact
        that cannot be loaded grants nothing: the runtime could not run it
        either.
        """
        required = REQUIRED_GRANTS.get(playbook_id)
        if required is None:
            return None
        from src.playbooks.definition import granted_aq_commands

        _ref, definition, error = await self._handler._v2_load_artifact(
            artifact_sha256, playbook_id
        )
        if error is not None:
            logger.warning(
                "Could not read the active %s artifact %s to check its grants: %s",
                playbook_id,
                artifact_sha256,
                error,
            )
            return required
        return None if required in granted_aq_commands(definition) else required

    async def _activate_defaults(self) -> dict[str, dict[str, Any]]:
        """Activate each shipped default playbook the first time it is seen.

        Unlike a required playbook, a default is not part of readiness: a
        failed import is logged and reported here.  An existing activation --
        including one an operator disabled -- is left exactly as it is, unless
        it is enabled and broken, in which case it is re-pointed at the shipped
        artifact exactly as a required one is.
        """
        defaults: dict[str, dict[str, Any]] = {}
        for playbook_id in DEFAULT_SYSTEM_PLAYBOOK_IDS:
            records, _contracts, _profiles = await self._handler._v2_health_records()
            existing = _system_activation(records, playbook_id)
            if existing is not None and (not existing.enabled or _is_ready(existing)):
                defaults[playbook_id] = {
                    "activated": False,
                    "enabled": existing.enabled,
                    "health": existing.health.value,
                }
                continue
            activation, import_error, repointed_from = await self._install_shipped(playbook_id)
            if import_error is not None:
                logger.warning(
                    "default playbook %s was not activated: reviewed artifact import failed: %s",
                    playbook_id,
                    import_error,
                )
                defaults[playbook_id] = {"activated": False, "error": import_error}
                if existing is not None:
                    defaults[playbook_id].update(
                        enabled=existing.enabled, health=existing.health.value
                    )
                continue
            health = activation.health.value if activation is not None else "missing"
            if health == "ready":
                logger.info("Activated default system playbook %s", playbook_id)
            else:
                logger.warning(
                    "default playbook %s was activated but its health is %s", playbook_id, health
                )
            defaults[playbook_id] = {
                "activated": existing is None,
                "enabled": True,
                "health": health,
            }
            if repointed_from is not None:
                defaults[playbook_id]["repointed_from"] = repointed_from
        return defaults

    async def replay_route_needed_events(self) -> dict[str, Any]:
        """Replay held routing events after the runtime has refreshed its snapshot."""
        routing = self.status.get("required", {}).get("default-assignment-routing", {})
        if not routing.get("ok"):
            return {"replayed": False, "reason": routing.get("diagnostic", "inactive")}

        records, _contracts, _profiles = await self._handler._v2_health_records()
        activation = self._handler._v2_activation_for(records, "default-assignment-routing")
        if activation is None or not activation.enabled or activation.health.value != "ready":
            return {"replayed": False, "reason": "required-playbook-inactive"}
        rows = await self._db.list_pending_events(
            playbook_id="default-assignment-routing", include_resolved=False
        )
        rows = [row for row in rows if row.get("event_type") == "task.route_needed"]
        from src.commands.principal import ExecutionPrincipal

        principal = ExecutionPrincipal.service(_ACTOR)
        actor = principal.describe()
        dispatched: list[str] = []
        errors: list[str] = []
        for row in rows:
            claimed, run_ids, error = await self._handler._v2_replay_held_event(
                row, principal, actor
            )
            if error:
                errors.append(f"{row['pending_event_id']}: {error}")
            elif claimed:
                dispatched.extend(run_ids)
        return {"replayed": True, "considered": len(rows), "run_ids": dispatched, "errors": errors}


async def retain_required_route_needed_event(
    db: Any, event: dict[str, Any], *, ttl_days: int
) -> None:
    """Durably retain a route event while its required activation is inactive."""
    event_id = event.get("event_id")
    task_id = event.get("task_id") or event.get("id") or event_id or "unknown"
    await db.retain_pending_event(
        playbook_id="default-assignment-routing",
        scope="system",
        scope_identifier="",
        event_type="task.route_needed",
        event=event,
        event_id=event_id,
        dedup_key=f"required-route-needed:{task_id}",
        # Pending-event storage has a deliberately closed reason vocabulary.
        # A missing required activation has the same safe dispatch semantics
        # as a disabled one; readiness carries the more specific diagnostic.
        reason="disabled",
        now=time.time(),
        ttl_seconds=max(1, ttl_days) * 86_400,
    )
