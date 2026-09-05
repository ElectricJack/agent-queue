"""The sole production runtime for Playbooks V2."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from src.commands.principal import ExecutionPrincipal
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.routing import install_routing_activation_snapshot
from src.playbooks.services import build_v2_engine
from src.playbooks.waits import PENDING_EVENT_DISPATCH_LEASE_SECONDS

logger = logging.getLogger(__name__)
_INTEGRATION_REPLAY_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class _IntegrationDestination:
    playbook_id: str
    scope: str
    scope_identifier: str
    definition: Any


class V2PlaybookRuntime:
    """Dispatch ready immutable activations and expose their timer triggers."""

    def __init__(self, *, config: Any, db: Any, handler: Any, llm: Any, bus: Any) -> None:
        self._config = config
        self._db = db
        self._bus = bus
        self._engine = build_v2_engine(config=config, db=db, handler=handler, llm=llm, bus=bus)
        self._store = ArtifactStore(
            config.compiled_root,
            max_artifact_bytes=config.playbooks.v2_max_artifact_bytes,
        )
        self._routing_activation_snapshot = None
        self._routing_activation_refresh_lock = asyncio.Lock()
        self._triggers: tuple[str, ...] = ()
        self._integration_destinations: tuple[_IntegrationDestination, ...] = ()
        self._unsubscribe = None
        self._tasks: set[asyncio.Task[Any]] = set()

    async def refresh(self) -> None:
        rows = await self._db.list_playbook_activations(enabled_only=True)
        install_routing_activation_snapshot(self, rows, artifact_store=self._store)
        triggers: set[str] = set()
        integration_destinations: list[_IntegrationDestination] = []
        for row in rows:
            if getattr(row.get("health"), "value", row.get("health")) != "ready":
                continue
            sha = row.get("active_artifact_sha256")
            if not sha:
                continue
            try:
                definition = self._store.load(sha)
            except Exception:
                logger.exception("Could not load active V2 artifact %s", sha)
                continue
            triggers.update(rule.trigger.event_type for rule in definition.rules)
            integration_destinations.append(
                _IntegrationDestination(
                    playbook_id=row["playbook_id"],
                    scope=row["scope"],
                    scope_identifier=row.get("scope_identifier") or "",
                    definition=definition,
                )
            )
        self._triggers = tuple(sorted(triggers))
        self._integration_destinations = tuple(integration_destinations)
        list_pending = getattr(self._db, "list_pending_integration_events", None)
        if callable(list_pending):
            pending = await list_pending(
                playbook_ids=[
                    destination.playbook_id for destination in integration_destinations
                ],
                limit=_INTEGRATION_REPLAY_PAGE_SIZE,
            )
            self._schedule_integration_pending(pending)

    def get_all_triggers(self) -> list[str]:
        return list(self._triggers)

    def subscribe_to_events(self) -> int:
        if self._unsubscribe is None:
            self._unsubscribe = self._bus.subscribe("*", self._on_event)
        return len(self._triggers)

    def _on_event(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("_received_at", time.time())
        task = asyncio.create_task(
            self._dispatch(payload),
            name=f"playbook-v2:{payload.get('_event_type', 'event')}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch(self, event: dict[str, Any]) -> None:
        try:
            await self._engine.dispatch_event(
                event, ExecutionPrincipal.service("playbook-dispatch")
            )
        except Exception:
            logger.exception(
                "V2 playbook dispatch failed for event=%s", event.get("_event_type")
            )

    async def accept_integration_event(
        self, event_type: str, payload: dict[str, Any], event_id: str
    ) -> bool:
        """Persist matching destinations, then replay them asynchronously.

        This is the durable acceptance boundary used by the integration
        outbox.  It intentionally does not call :class:`EventBus`: the normal
        subscription schedules a background task and cannot say whether a run
        or pending row committed.
        """
        event = dict(payload)
        event["event_id"] = event_id
        event["_event_type"] = event_type
        hydrated = await self._engine._hydrate_event(event)
        project_id = hydrated.get("project_id")
        agent_type = hydrated.get("agent_type")
        destinations: list[_IntegrationDestination] = []
        for destination in self._integration_destinations:
            if destination.scope == "project" and destination.scope_identifier != project_id:
                continue
            if destination.scope == "agent_type" and (
                project_id is None or destination.scope_identifier != agent_type
            ):
                continue
            if destination.scope not in {"system", "project", "agent_type"}:
                continue
            if any(
                self._engine._rule_selected(rule, event_type, hydrated)
                for rule in destination.definition.rules
            ):
                destinations.append(destination)
        if not destinations:
            return False

        pending_ids: list[str] = []
        accepted_at = time.time()
        for destination in destinations:
            pending_ids.append(
                await self._db.retain_integration_event(
                    playbook_id=destination.playbook_id,
                    scope=destination.scope,
                    scope_identifier=destination.scope_identifier,
                    event_type=event_type,
                    event=event,
                    event_id=event_id,
                    now=accepted_at,
                )
            )

        # Schedule only after every destination committed. A partial failure
        # leaves the outbox unacknowledged; retry fills the missing rows.
        unresolved = await self._db.get_pending_events(pending_ids)
        self._schedule_integration_pending(unresolved)
        return True

    def _schedule_integration_pending(self, rows: list[dict[str, Any]]) -> None:
        """Start asynchronous replay for a bounded, already-persisted page."""
        for row in rows:
            task = asyncio.create_task(
                self._dispatch_integration_pending(row),
                name=f"playbook-v2-integration:{row['pending_event_id']}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _dispatch_integration_pending(self, row: dict[str, Any]) -> None:
        """Dispatch one protected row; every failure leaves it retryable."""
        pending_event_id = row["pending_event_id"]
        actor = "service:integration-outbox"
        now = time.time()
        claim_token = await self._db.claim_pending_event_dispatch(
            pending_event_id,
            claimed_by=actor,
            now=now,
            stale_before=now - PENDING_EVENT_DISPATCH_LEASE_SECONDS,
        )
        if claim_token is None:
            return
        try:
            dispatch_id = hashlib.sha256(
                f"v2-dispatch|{row['event_id']}".encode()
            ).hexdigest()[:12]
            result = await self._engine.dispatch_event(
                dict(row["event"]),
                ExecutionPrincipal.service("integration-outbox"),
                playbook_ids=[row["playbook_id"]],
                dispatch_id=dispatch_id,
            )
            if not result.rules_selected or len(result.run_ids) != len(result.rules_selected):
                raise RuntimeError("integration event produced no durable playbook run")
            finalized = await self._db.finalize_pending_event_dispatch(
                pending_event_id,
                claim_token=claim_token,
                resolved_by=actor,
                now=time.time(),
            )
            if not finalized:
                raise RuntimeError("integration pending-event claim was lost before finalization")
        except Exception as exc:
            logger.exception(
                "V2 integration dispatch failed for pending_event=%s", pending_event_id
            )
            await self._db.record_pending_event_dispatch_failure(
                pending_event_id,
                claim_token=claim_token,
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )

    async def shutdown(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
