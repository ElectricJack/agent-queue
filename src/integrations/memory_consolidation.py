"""Memory consolidation integration with the orchestrator lifecycle.

Wires the ``MemoryConsolidator`` service into the task completion pipeline
so consolidation is automatically checked after every task, and exposes
command-handler extensions for manual operations.

Usage::

    from src.integrations.memory_consolidation import (
        MemoryConsolidationIntegration,
    )

    # During startup, after orchestrator and memory manager are ready:
    integration = MemoryConsolidationIntegration(memory_manager)

    # In the post-task hook (called by orchestrator after task completion):
    result = await integration.on_task_completed(project_id, task, output, workspace)

    # Manual commands (exposed via command handler or Discord):
    result = await integration.handle_consolidate_command(project_id, workspace)
    status = await integration.handle_status_command(project_id)
"""

from __future__ import annotations

import logging
from typing import Any

from src.memory import MemoryManager
from src.services.memory_consolidator import ConsolidationResult, MemoryConsolidator

logger = logging.getLogger(__name__)


class MemoryConsolidationIntegration:
    """Connects the consolidation service to the orchestrator lifecycle.

    This integration is the single entry point for all consolidation-related
    interactions.  It owns the ``MemoryConsolidator`` instance and handles:

    * **Post-task hook** — after every task completion, extract facts and
      check whether auto-consolidation should fire.
    * **Command interface** — manual consolidation, deep consolidation,
      bootstrap, and status queries.
    * **Event emission** — logs structured events that can be consumed by
      the event bus or Discord notifications.

    Parameters:
        memory_manager: The ``MemoryManager`` instance to operate on.
    """

    def __init__(self, memory_manager: MemoryManager) -> None:
        self._mm = memory_manager
        self._consolidator = MemoryConsolidator(memory_manager)

    @property
    def consolidator(self) -> MemoryConsolidator:
        """Expose the underlying service for advanced use cases."""
        return self._consolidator

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    async def on_task_completed(
        self,
        project_id: str,
        task: Any,
        output: Any,
        workspace_path: str = "",
    ) -> ConsolidationResult:
        """Post-task hook: extract facts, then check consolidation thresholds.

        This should be called by the orchestrator after a task finishes
        successfully.  It performs two steps:

        1. **Fact extraction** — calls ``MemoryManager.extract_task_facts``
           to pull structured facts from the task output into a staging file.
        2. **Threshold check** — calls ``MemoryConsolidator.check_and_consolidate``
           to see if the staging directory has grown past the auto-trigger
           threshold, and runs consolidation if so.

        Returns a ``ConsolidationResult`` describing what happened.  If fact
        extraction or threshold checking fails, the error is logged but
        never propagated — the task result is not affected.
        """
        # Step 1: Extract facts (non-blocking on failure)
        try:
            staging_path = await self._mm.extract_task_facts(
                project_id, task, output, workspace_path
            )
            if staging_path:
                logger.debug(
                    "Extracted facts from task %s to %s",
                    task.id,
                    staging_path,
                )
        except Exception as exc:
            logger.warning(
                "Fact extraction failed for task %s: %s (non-fatal)",
                getattr(task, "id", "?"),
                exc,
            )

        # Step 2: Check thresholds and maybe consolidate
        try:
            result = await self._consolidator.check_and_consolidate(
                project_id, workspace_path
            )
            if result.triggered:
                logger.info(
                    "Auto-consolidation triggered for %s: %s",
                    project_id,
                    result.reason,
                )
                self._emit_consolidation_event(project_id, result)
            return result
        except Exception as exc:
            logger.warning(
                "Auto-consolidation check failed for %s: %s (non-fatal)",
                project_id,
                exc,
            )
            return ConsolidationResult(
                triggered=False,
                reason="error_during_check",
                status="error",
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def handle_consolidate_command(
        self,
        project_id: str,
        workspace_path: str = "",
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Handle ``!memory consolidate`` command.

        Returns a dict suitable for the ``{"success": bool, ...}``
        command-handler response format.
        """
        result = await self._consolidator.consolidate_now(
            project_id, workspace_path, force=force
        )
        self._emit_consolidation_event(project_id, result)

        return {
            "success": result.status not in ("error", "disabled"),
            "message": self._format_result_message(result),
            **result.to_dict(),
        }

    async def handle_deep_consolidate_command(
        self,
        project_id: str,
        workspace_path: str = "",
    ) -> dict[str, Any]:
        """Handle ``!memory deep-consolidate`` command."""
        result = await self._consolidator.deep_consolidate(
            project_id, workspace_path
        )
        self._emit_consolidation_event(project_id, result)

        return {
            "success": result.status not in ("error", "disabled"),
            "message": self._format_result_message(result),
            **result.to_dict(),
        }

    async def handle_bootstrap_command(
        self,
        project_id: str,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> dict[str, Any]:
        """Handle ``!memory bootstrap`` command."""
        result = await self._consolidator.bootstrap(
            project_id,
            workspace_path,
            project_name=project_name,
            repo_url=repo_url,
        )

        return {
            "success": result.status not in ("error", "disabled"),
            "message": self._format_result_message(result),
            **result.to_dict(),
        }

    async def handle_status_command(
        self,
        project_id: str,
    ) -> dict[str, Any]:
        """Handle ``!memory consolidation-status`` command."""
        status = self._consolidator.get_consolidation_status(project_id)
        clusters = self._consolidator.cluster_staging_facts(project_id)

        # Summarise clusters without the raw facts for the status view
        cluster_summaries = [
            {
                "topic": c["topic"],
                "fact_count": c["fact_count"],
                "categories": c["categories"],
                "task_ids": c["task_ids"],
            }
            for c in clusters
        ]

        return {
            "success": True,
            **status,
            "pending_clusters": cluster_summaries,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_result_message(result: ConsolidationResult) -> str:
        """Build a human-readable summary of a consolidation result."""
        if not result.triggered:
            return f"Consolidation skipped: {result.reason}"

        if result.error:
            return f"Consolidation failed: {result.error}"

        parts: list[str] = []

        if result.facts_consolidated:
            parts.append(f"{result.facts_consolidated} facts consolidated")
        if result.staging_files_processed:
            parts.append(
                f"{result.staging_files_processed} staging files processed"
            )
        if result.topics_updated:
            parts.append(
                f"topics updated: {', '.join(result.topics_updated)}"
            )
        if result.factsheet_updated:
            parts.append("factsheet updated")
        if result.pruned_facts:
            parts.append(f"{len(result.pruned_facts)} facts pruned")
        if result.duration_seconds:
            parts.append(f"took {result.duration_seconds:.1f}s")

        if not parts:
            return f"Consolidation completed: {result.status}"

        return "Consolidation completed: " + "; ".join(parts)

    @staticmethod
    def _emit_consolidation_event(
        project_id: str, result: ConsolidationResult
    ) -> None:
        """Log a structured consolidation event.

        If an event bus is available in the future, this is the hook point
        for publishing ``consolidation.completed`` events.
        """
        if result.triggered:
            logger.info(
                "consolidation.completed project=%s status=%s facts=%d "
                "topics=%d factsheet=%s duration=%.1fs",
                project_id,
                result.status,
                result.facts_consolidated,
                len(result.topics_updated),
                result.factsheet_updated,
                result.duration_seconds,
            )
