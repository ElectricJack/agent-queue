"""Startup reconciliation for reviewed system playbooks that the daemon requires.

The activation table is durable, while the runtime subscription is not.  This
module bridges that boundary: it installs the reviewed bundle on a new data
directory, records the first activation, and makes an inactive required policy
an explicit readiness failure instead of an empty event subscription.
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
_ACTOR = "service:required-playbook-reconciler"


def reviewed_bundle_source() -> Path:
    """The reviewed bundles shipped with the daemon, not test fixtures."""
    return Path(__file__).resolve().parents[1] / "prompts" / "reviewed_playbooks"


def ensure_reviewed_playbook_bundles(data_dir: str) -> list[str]:
    """Seed immutable reviewed bundles into the vault without overwriting it."""
    destination_root = Path(data_dir) / "vault" / "reviewed-playbooks"
    installed: list[str] = []
    for playbook_id in REQUIRED_SYSTEM_PLAYBOOK_IDS:
        source = reviewed_bundle_source() / playbook_id
        destination = destination_root / playbook_id
        if destination.exists() or not source.is_dir():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        installed.append(playbook_id)
    return installed


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

        self.status = {"ok": all(item["ok"] for item in required.values()), "required": required}
        for playbook_id, item in required.items():
            if not item["ok"]:
                logger.error("%s", item["diagnostic"])
        return self.status

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
