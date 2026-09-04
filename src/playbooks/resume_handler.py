"""Event-driven resume of Playbooks V2 runs waiting for a decision."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class PlaybookResumeHandler:
    """Translate a decision event into a V2 engine resume cause."""

    def __init__(self, *, db: Any, event_bus: Any, orchestrator: Any, config: Any, **_: Any) -> None:
        self._db = db
        self._bus = event_bus
        self._orchestrator = orchestrator
        self._config = config
        self._unsubscribes: list[Callable[[], None]] = []
        self._running_resumes: dict[str, asyncio.Task[Any]] = {}

    def subscribe(self) -> None:
        self.unsubscribe()
        self._unsubscribes.append(
            self._bus.subscribe("human.review.completed", self._on_decision)
        )

    def unsubscribe(self) -> None:
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes.clear()

    def shutdown(self) -> None:
        self.unsubscribe()
        for task in self._running_resumes.values():
            task.cancel()
        self._running_resumes.clear()

    async def _on_decision(self, data: dict[str, Any]) -> None:
        run_id = str(data.get("run_id") or "")
        decision = str(data.get("decision") or "")
        if not run_id or not decision:
            logger.warning("decision event requires run_id and decision")
            return
        current = self._running_resumes.get(run_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._resume(run_id, decision, data))
        self._running_resumes[run_id] = task
        task.add_done_callback(lambda _task: self._running_resumes.pop(run_id, None))

    # Kept as an alias for callers/tests using the old callback name.
    _on_human_review_completed = _on_decision

    async def _resume(self, run_id: str, decision: str, data: dict[str, Any]) -> None:
        from src.commands.principal import ExecutionPrincipal
        from src.playbooks.engine import HumanDecision
        from src.playbooks.services import build_v2_engine, load_v2_snapshot

        snapshot = await load_v2_snapshot(self._db, run_id)
        if snapshot is None:
            logger.warning("Playbooks V2 run '%s' not found", run_id)
            return
        handler = getattr(self._orchestrator, "_command_handler", None)
        if handler is None:
            logger.error("Cannot resume V2 run '%s': command handler unavailable", run_id)
            return
        engine = build_v2_engine(
            config=self._config,
            db=self._db,
            handler=handler,
            llm=getattr(self._orchestrator, "llm", None),
            bus=self._bus,
        )
        await engine.resume(
            run_id,
            HumanDecision(decision=decision, payload=dict(data)),
            ExecutionPrincipal.service("playbook-resume"),
        )

    async def _resume_run(self, run_id: str, decision: str, event_data: dict[str, Any]) -> None:
        await self._resume(run_id, decision, event_data)
