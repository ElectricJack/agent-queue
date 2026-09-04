"""Resume Playbooks V2 runs when workflow stages complete."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class WorkflowStageResumeHandler:
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
            self._bus.subscribe("workflow.stage.completed", self._on_stage_completed)
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

    async def _on_stage_completed(self, data: dict[str, Any]) -> None:
        workflow_id = str(data.get("workflow_id") or "")
        if not workflow_id:
            logger.warning("workflow.stage.completed event missing workflow_id")
            return
        workflow = await self._db.get_workflow(workflow_id)
        run_id = str(getattr(workflow, "playbook_run_id", "") or "")
        if not run_id:
            return
        current = self._running_resumes.get(run_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._resume_run(run_id, data))
        self._running_resumes[run_id] = task
        task.add_done_callback(lambda _task: self._running_resumes.pop(run_id, None))

    async def _resume_run(self, run_id: str, event_data: dict[str, Any]) -> None:
        from src.commands.principal import ExecutionPrincipal
        from src.playbooks.engine import EventArrived
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
            EventArrived(
                event_id=str(event_data.get("event_id") or ""),
                payload={k: v for k, v in event_data.items() if not k.startswith("_")},
            ),
            ExecutionPrincipal.service("workflow-stage-resume"),
        )
