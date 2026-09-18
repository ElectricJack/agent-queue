"""Startup reconciliation for reviewed system playbooks that the daemon requires.

The activation table is durable, while the runtime subscription is not.  This
module bridges that boundary: it installs the reviewed bundle on a new data
directory, records the first activation, and makes an inactive required policy
an explicit readiness failure instead of an empty event subscription.

Seeding is also the only supply line a corrected bundle has.  ``playbook_v2_import``
refuses every path outside the vault root, so a bundle rebuilt against a changed
command contract reaches an install only by being written into the vault here.
"""

from __future__ import annotations

import logging
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
)
# Shared system playbooks every install gets, but which are not a readiness
# requirement: activated once, on the first start that finds no activation for
# them, and never re-enabled after an operator disables one.  Before this set
# existed a fresh install activated only the two required policies above, and
# Settings -> Playbooks showed nothing else even though these ship with AQ.
DEFAULT_SYSTEM_PLAYBOOK_IDS = ("blocked-task-escalation", "default-pipeline")
#: The bundle files a reviewed recording is made of.  Seeding rewrites exactly
#: these and leaves anything else in the directory alone.
_BUNDLE_FILES = ("artifact.json", "artifact.sha256", "source.md", "manifest.md", "diagnostics.json")
_ACTOR = "service:required-playbook-reconciler"


def reviewed_bundle_source() -> Path:
    """The reviewed bundles shipped with the daemon, not test fixtures."""
    return Path(__file__).resolve().parents[1] / "prompts" / "reviewed_playbooks"


def shipped_reviewed_playbook_ids() -> tuple[str, ...]:
    """Every reviewed bundle this daemon carries, activated or not.

    The directory is the list.  ``ci-main-sentinel`` is project-scoped and is
    never activated by the reconciler, but ``playbook_v2_import`` refuses any
    path outside the vault root, so a bundle an operator cannot find in the
    vault is a bundle they cannot import at all.
    """
    source_root = reviewed_bundle_source()
    if not source_root.is_dir():
        return ()
    return tuple(sorted(entry.name for entry in source_root.iterdir() if entry.is_dir()))


def ensure_reviewed_playbook_bundles(data_dir: str) -> list[str]:
    """Seed the shipped reviewed bundles into the vault, refreshing stale bytes.

    Seeding used to skip any id the vault already had, which made the vault
    copy permanent: a bundle rebuilt against a changed command contract never
    reached an existing install, and because ``playbook_v2_import`` can only
    read paths under the vault root, the operator had no supported way to
    import the corrected bytes either.  That is how three activations sat at
    ``stale_contract`` while the repository's own fixtures were current.

    A reviewed bundle is content-addressed and the artifact an activation
    points at lives in the compiled store, so replacing these bytes retires
    nothing: it only changes what the *next* import reads.  Operator variants
    live under their own ids and are untouched.

    Returns the ids whose vault bytes this call wrote, newly or refreshed.
    """
    destination_root = Path(data_dir) / "vault" / "reviewed-playbooks"
    written: list[str] = []
    for playbook_id in shipped_reviewed_playbook_ids():
        source = reviewed_bundle_source() / playbook_id
        destination = destination_root / playbook_id
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
            written.append(playbook_id)
            continue
        refreshed = False
        for name in _BUNDLE_FILES:
            shipped_file = source / name
            if not shipped_file.is_file():
                continue
            current = destination / name
            if current.is_file() and current.read_bytes() == shipped_file.read_bytes():
                continue
            shutil.copyfile(shipped_file, current)
            refreshed = True
        if refreshed:
            logger.info("Refreshed the stale reviewed bundle for %s in the vault", playbook_id)
            written.append(playbook_id)
    return written


class RequiredPlaybookReconciler:
    """Persist and validate the required system activation before subscribing."""

    def __init__(self, *, config: Any, db: Any, handler: Any) -> None:
        self._config = config
        self._db = db
        self._handler = handler
        self.status: dict[str, Any] = {"ok": True, "required": {}}

    async def reconcile(self) -> dict[str, Any]:
        """Import reviewed bytes and create only missing system activations.

        Existing activation rows are intentionally never replaced: they are
        durable operator state.  They are nevertheless verified on every
        start, and an unhealthy or disabled required activation leaves a
        machine-readable readiness diagnostic.
        """
        required: dict[str, dict[str, Any]] = {}
        for playbook_id in REQUIRED_SYSTEM_PLAYBOOK_IDS:
            imported = await self._handler._cmd_playbook_v2_import(
                {"path": f"reviewed-playbooks/{playbook_id}"}
            )
            if not imported.get("success"):
                required[playbook_id] = {
                    "ok": False,
                    "diagnostic": f"required-playbook-inactive: {playbook_id}: "
                    f"reviewed artifact import failed: {imported.get('error', 'unknown error')}",
                }
                continue

            artifact_sha256 = imported["artifact_sha256"]
            records, _contracts, _profiles = await self._handler._v2_health_records()
            activation = next(
                (
                    record
                    for record in records
                    if record.playbook_id == playbook_id
                    and record.scope == "system"
                    and record.scope_identifier == ""
                ),
                None,
            )
            if activation is None:
                await self._db.set_playbook_activation(
                    playbook_id=playbook_id,
                    scope="system",
                    scope_identifier="",
                    artifact_sha256=artifact_sha256,
                    enabled=True,
                    activated_by=_ACTOR,
                    health="ready",
                    reasons="[]",
                )
                records, _contracts, _profiles = await self._handler._v2_health_records()
                activation = next(
                    (
                        record
                        for record in records
                        if record.playbook_id == playbook_id
                        and record.scope == "system"
                        and record.scope_identifier == ""
                    ),
                    None,
                )

            healthy = (
                activation is not None and activation.enabled and activation.health.value == "ready"
            )
            if healthy:
                required[playbook_id] = {
                    "ok": True,
                    "artifact_sha256": activation.active_artifact_sha256,
                }
            else:
                health = activation.health.value if activation is not None else "missing"
                required[playbook_id] = {
                    "ok": False,
                    "diagnostic": (
                        f"required-playbook-inactive: {playbook_id}: activation is {health}"
                    ),
                }

        defaults = await self._activate_defaults()
        self.status = {
            "ok": all(item["ok"] for item in required.values()),
            "required": required,
            "defaults": defaults,
        }
        for playbook_id, item in required.items():
            if not item["ok"]:
                logger.error("%s", item["diagnostic"])
        return self.status

    async def _activate_defaults(self) -> dict[str, dict[str, Any]]:
        """Activate each shipped default playbook the first time it is seen.

        Unlike a required playbook, a default is not part of readiness: a
        failed import is logged and reported here, and an existing activation
        -- including one an operator disabled -- is left exactly as it is.
        """
        defaults: dict[str, dict[str, Any]] = {}
        for playbook_id in DEFAULT_SYSTEM_PLAYBOOK_IDS:
            records, _contracts, _profiles = await self._handler._v2_health_records()
            existing = next(
                (
                    record
                    for record in records
                    if record.playbook_id == playbook_id
                    and record.scope == "system"
                    and record.scope_identifier == ""
                ),
                None,
            )
            if existing is not None:
                defaults[playbook_id] = {
                    "activated": False,
                    "enabled": existing.enabled,
                    "health": existing.health.value,
                }
                continue
            imported = await self._handler._cmd_playbook_v2_import(
                {"path": f"reviewed-playbooks/{playbook_id}"}
            )
            if not imported.get("success"):
                message = imported.get("error", "unknown error")
                logger.warning(
                    "default playbook %s was not activated: reviewed artifact import failed: %s",
                    playbook_id,
                    message,
                )
                defaults[playbook_id] = {"activated": False, "error": message}
                continue
            await self._db.set_playbook_activation(
                playbook_id=playbook_id,
                scope="system",
                scope_identifier="",
                artifact_sha256=imported["artifact_sha256"],
                enabled=True,
                activated_by=_ACTOR,
                health="ready",
                reasons="[]",
            )
            records, _contracts, _profiles = await self._handler._v2_health_records()
            activation = next(
                (
                    record
                    for record in records
                    if record.playbook_id == playbook_id
                    and record.scope == "system"
                    and record.scope_identifier == ""
                ),
                None,
            )
            health = activation.health.value if activation is not None else "missing"
            if health == "ready":
                logger.info("Activated default system playbook %s", playbook_id)
            else:
                logger.warning(
                    "default playbook %s was activated but its health is %s", playbook_id, health
                )
            defaults[playbook_id] = {"activated": True, "enabled": True, "health": health}
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
