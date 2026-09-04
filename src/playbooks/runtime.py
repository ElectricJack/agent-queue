"""The sole production runtime for Playbooks V2."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.commands.principal import ExecutionPrincipal
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.routing import install_routing_activation_snapshot
from src.playbooks.services import build_v2_engine

logger = logging.getLogger(__name__)


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
        self._unsubscribe = None
        self._tasks: set[asyncio.Task[Any]] = set()

    async def refresh(self) -> None:
        rows = await self._db.list_playbook_activations(enabled_only=True)
        install_routing_activation_snapshot(self, rows, artifact_store=self._store)
        triggers: set[str] = set()
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
        self._triggers = tuple(sorted(triggers))

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

    async def shutdown(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

