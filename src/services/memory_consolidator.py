"""Memory consolidation service.

Provides intelligent, threshold-driven consolidation of fragmented task
memories into cohesive knowledge artifacts (project factsheets, topic
knowledge files, decision logs).  Designed to work non-disruptively
alongside the existing ``MemoryManager`` — it delegates all reads/writes
to ``MemoryManager`` and adds:

* **Automatic triggering** — monitors staging file growth and fires
  consolidation when a configurable threshold is reached.
* **Memory clustering** — groups related facts by topic/project using
  the category-to-topic mapping and optional similarity scoring.
* **Artifact generation** — orchestrates daily and deep consolidation
  passes that produce consolidated knowledge artifacts.
* **Source traceability** — every consolidated fact links back to its
  originating task through ``task_id`` references.
* **Cooldown & batching** — prevents redundant consolidation runs via a
  cooldown timer and processes staging files in configurable batches.

Usage::

    from src.services.memory_consolidator import MemoryConsolidator

    consolidator = MemoryConsolidator(memory_manager)
    # After each task completes:
    result = await consolidator.check_and_consolidate(project_id, workspace_path)
    # Manual trigger:
    result = await consolidator.consolidate_now(project_id, workspace_path)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from src.config import MemoryConfig
from src.memory import MemoryManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ConsolidationResult:
    """Outcome of a consolidation attempt.

    Attributes:
        triggered: Whether consolidation actually ran (vs. skipped).
        reason: Human-readable explanation of why it ran or was skipped.
        status: Underlying status from MemoryManager (e.g. ``"consolidated"``).
        staging_files_processed: Number of staging files consumed.
        facts_consolidated: Number of unique facts merged.
        topics_updated: Knowledge topics that were modified.
        factsheet_updated: Whether the project factsheet changed.
        clusters: Topic clusters detected during this run.
        duration_seconds: Wall-clock time of the consolidation pass.
        pruned_facts: Facts removed during deep consolidation.
        error: Error message if consolidation failed.
    """

    triggered: bool = False
    reason: str = ""
    status: str = ""
    staging_files_processed: int = 0
    facts_consolidated: int = 0
    topics_updated: list[str] = field(default_factory=list)
    factsheet_updated: bool = False
    clusters: list[dict[str, Any]] = field(default_factory=list)
    duration_seconds: float = 0.0
    pruned_facts: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise for command handler / event bus."""
        return {
            "triggered": self.triggered,
            "reason": self.reason,
            "status": self.status,
            "staging_files_processed": self.staging_files_processed,
            "facts_consolidated": self.facts_consolidated,
            "topics_updated": self.topics_updated,
            "factsheet_updated": self.factsheet_updated,
            "clusters": self.clusters,
            "duration_seconds": round(self.duration_seconds, 3),
            "pruned_facts": self.pruned_facts,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Core service
# ---------------------------------------------------------------------------


class MemoryConsolidator:
    """Intelligent memory consolidation service.

    Wraps ``MemoryManager`` consolidation primitives with threshold-based
    auto-triggering, cooldown management, memory clustering, and batching.

    The service is stateful — it tracks per-project timestamps to enforce
    cooldown periods and avoid redundant work.

    Parameters:
        memory_manager: The ``MemoryManager`` instance to delegate to.
        config: Optional override; defaults to ``memory_manager.config``.
    """

    def __init__(
        self,
        memory_manager: MemoryManager,
        config: MemoryConfig | None = None,
    ) -> None:
        self._mm = memory_manager
        self._config = config or memory_manager.config
        # Per-project state: last time a consolidation was triggered
        self._last_consolidation: dict[str, float] = {}
        # Per-project state: last staging-file count we observed
        self._last_staging_count: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def config(self) -> MemoryConfig:
        return self._config

    async def check_and_consolidate(
        self,
        project_id: str,
        workspace_path: str = "",
    ) -> ConsolidationResult:
        """Check thresholds and consolidate if warranted.

        This is the main entry point — call it after every task completion.
        It inspects the staging directory, applies the growth threshold and
        cooldown rules, and runs daily consolidation if the criteria are met.

        Returns a ``ConsolidationResult`` describing what happened.
        """
        if not self._config.consolidation_enabled:
            return ConsolidationResult(
                triggered=False,
                reason="consolidation_disabled",
                status="disabled",
            )

        if not self._config.consolidation_auto_trigger:
            return ConsolidationResult(
                triggered=False,
                reason="auto_trigger_disabled",
                status="skipped",
            )

        # Check cooldown
        cooldown_ok, cooldown_reason = self._check_cooldown(project_id)
        if not cooldown_ok:
            return ConsolidationResult(
                triggered=False,
                reason=cooldown_reason,
                status="cooldown",
            )

        # Count staging files
        staging_count = self._count_staging_files(project_id)
        threshold = self._config.consolidation_growth_threshold

        if staging_count < threshold:
            self._last_staging_count[project_id] = staging_count
            return ConsolidationResult(
                triggered=False,
                reason=(
                    f"below_threshold: {staging_count}/{threshold} staging files"
                ),
                status="below_threshold",
            )

        # Check minimum age of oldest staging file
        if not self._check_min_age(project_id):
            return ConsolidationResult(
                triggered=False,
                reason="staging_files_too_recent",
                status="too_recent",
            )

        # Threshold met — run consolidation
        return await self._run_daily(project_id, workspace_path)

    async def consolidate_now(
        self,
        project_id: str,
        workspace_path: str = "",
        *,
        force: bool = False,
    ) -> ConsolidationResult:
        """Manually trigger consolidation, bypassing threshold checks.

        If *force* is ``True``, also bypasses the cooldown timer.
        """
        if not self._config.consolidation_enabled:
            return ConsolidationResult(
                triggered=False,
                reason="consolidation_disabled",
                status="disabled",
            )

        if not force:
            cooldown_ok, cooldown_reason = self._check_cooldown(project_id)
            if not cooldown_ok:
                return ConsolidationResult(
                    triggered=False,
                    reason=cooldown_reason,
                    status="cooldown",
                )

        return await self._run_daily(project_id, workspace_path)

    async def deep_consolidate(
        self,
        project_id: str,
        workspace_path: str = "",
    ) -> ConsolidationResult:
        """Run a deep (weekly) consolidation pass.

        Reviews the entire knowledge base, prunes stale facts, resolves
        conflicts, and regenerates the factsheet summary.  Intended to be
        called on a schedule or manually.
        """
        if not self._config.consolidation_enabled:
            return ConsolidationResult(
                triggered=False,
                reason="consolidation_disabled",
                status="disabled",
            )

        start = time.monotonic()
        try:
            stats = await self._mm.run_deep_consolidation(
                project_id, workspace_path
            )
        except Exception as exc:
            logger.exception(
                "Deep consolidation failed for project %s", project_id
            )
            return ConsolidationResult(
                triggered=True,
                reason="deep_consolidation",
                status="error",
                error=str(exc),
                duration_seconds=time.monotonic() - start,
            )

        elapsed = time.monotonic() - start
        self._last_consolidation[project_id] = time.time()

        return ConsolidationResult(
            triggered=True,
            reason="deep_consolidation",
            status=stats.get("status", "unknown"),
            topics_updated=stats.get("topics_updated", []),
            factsheet_updated=stats.get("factsheet_updated", False),
            pruned_facts=stats.get("pruned_facts", []),
            duration_seconds=elapsed,
        )

    async def bootstrap(
        self,
        project_id: str,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> ConsolidationResult:
        """Bootstrap a project's knowledge base from task history.

        One-time operation for projects with task memories but no
        structured knowledge base yet.
        """
        start = time.monotonic()
        try:
            stats = await self._mm.bootstrap_consolidation(
                project_id,
                workspace_path,
                project_name=project_name,
                repo_url=repo_url,
            )
        except Exception as exc:
            logger.exception("Bootstrap failed for project %s", project_id)
            return ConsolidationResult(
                triggered=True,
                reason="bootstrap",
                status="error",
                error=str(exc),
                duration_seconds=time.monotonic() - start,
            )

        elapsed = time.monotonic() - start
        return ConsolidationResult(
            triggered=True,
            reason="bootstrap",
            status=stats.get("status", "unknown"),
            facts_consolidated=stats.get("tasks_processed", 0),
            topics_updated=stats.get("topics_created", []),
            factsheet_updated=stats.get("factsheet_created", False),
            duration_seconds=elapsed,
        )

    def get_consolidation_status(self, project_id: str) -> dict[str, Any]:
        """Return diagnostic info about the consolidation state for a project.

        Useful for ``!memory status`` commands and health checks.
        """
        staging_count = self._count_staging_files(project_id)
        threshold = self._config.consolidation_growth_threshold
        last_run = self._last_consolidation.get(project_id)
        cooldown_ok, cooldown_reason = self._check_cooldown(project_id)

        return {
            "consolidation_enabled": self._config.consolidation_enabled,
            "auto_trigger_enabled": self._config.consolidation_auto_trigger,
            "staging_files": staging_count,
            "growth_threshold": threshold,
            "threshold_met": staging_count >= threshold,
            "last_consolidation": (
                time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_run)
                )
                if last_run
                else None
            ),
            "cooldown_ok": cooldown_ok,
            "cooldown_reason": cooldown_reason if not cooldown_ok else None,
            "min_age_hours": self._config.consolidation_min_age_hours,
            "max_batch_size": self._config.consolidation_max_batch_size,
            "similarity_threshold": self._config.consolidation_similarity_threshold,
        }

    def cluster_staging_facts(self, project_id: str) -> list[dict[str, Any]]:
        """Cluster unprocessed staging facts by topic.

        Reads staging files and groups facts using the category-to-topic
        mapping from the consolidation prompts.  Returns a list of cluster
        descriptors, each with:

        * ``topic`` — the knowledge topic slug
        * ``fact_count`` — number of facts in this cluster
        * ``categories`` — set of unique fact categories present
        * ``task_ids`` — set of unique source task IDs
        * ``facts`` — the actual fact dicts

        This is useful for previewing what consolidation will do, or for
        building a UI that shows pending knowledge updates.
        """
        staging_docs = self._mm._read_staging_files(project_id)
        if not staging_docs:
            return []

        unique_facts = self._mm._deduplicate_facts(staging_docs)
        if not unique_facts:
            return []

        facts_by_topic = self._mm._group_facts_by_topic(unique_facts)

        clusters: list[dict[str, Any]] = []
        for topic, facts in sorted(facts_by_topic.items()):
            clusters.append({
                "topic": topic,
                "fact_count": len(facts),
                "categories": sorted({f.get("category", "") for f in facts}),
                "task_ids": sorted({f.get("task_id", "") for f in facts}),
                "facts": facts,
            })

        return clusters

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_cooldown(self, project_id: str) -> tuple[bool, str]:
        """Return ``(ok, reason)`` — whether the cooldown has elapsed."""
        last = self._last_consolidation.get(project_id)
        if last is None:
            return True, "no_previous_run"

        cooldown_secs = self._config.consolidation_cooldown_minutes * 60
        elapsed = time.time() - last
        if elapsed < cooldown_secs:
            remaining = int(cooldown_secs - elapsed)
            return False, f"cooldown_active: {remaining}s remaining"

        return True, "cooldown_elapsed"

    def _count_staging_files(self, project_id: str) -> int:
        """Count unprocessed ``.json`` staging files for a project."""
        staging_dir = self._mm._staging_dir(project_id)
        if not os.path.isdir(staging_dir):
            return 0

        count = 0
        for filename in os.listdir(staging_dir):
            if filename.endswith(".json") and os.path.isfile(
                os.path.join(staging_dir, filename)
            ):
                count += 1
        return count

    def _check_min_age(self, project_id: str) -> bool:
        """Check whether the oldest staging file meets the minimum age."""
        staging_dir = self._mm._staging_dir(project_id)
        if not os.path.isdir(staging_dir):
            return False

        min_age_secs = self._config.consolidation_min_age_hours * 3600
        now = time.time()

        for filename in os.listdir(staging_dir):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(staging_dir, filename)
            if not os.path.isfile(filepath):
                continue
            try:
                mtime = os.path.getmtime(filepath)
                if (now - mtime) >= min_age_secs:
                    return True
            except OSError:
                continue

        return False

    async def _run_daily(
        self,
        project_id: str,
        workspace_path: str,
    ) -> ConsolidationResult:
        """Execute a daily consolidation pass with batching and clustering."""
        start = time.monotonic()

        # Build clusters first for the result metadata
        clusters = self.cluster_staging_facts(project_id)

        try:
            stats = await self._mm.run_daily_consolidation(
                project_id, workspace_path
            )
        except Exception as exc:
            logger.exception(
                "Daily consolidation failed for project %s", project_id
            )
            return ConsolidationResult(
                triggered=True,
                reason="growth_threshold_exceeded",
                status="error",
                error=str(exc),
                clusters=clusters,
                duration_seconds=time.monotonic() - start,
            )

        elapsed = time.monotonic() - start
        self._last_consolidation[project_id] = time.time()
        self._last_staging_count[project_id] = self._count_staging_files(
            project_id
        )

        result = ConsolidationResult(
            triggered=True,
            reason="growth_threshold_exceeded",
            status=stats.get("status", "unknown"),
            staging_files_processed=stats.get("staging_files_processed", 0),
            facts_consolidated=stats.get("facts_consolidated", 0),
            topics_updated=stats.get("topics_updated", []),
            factsheet_updated=stats.get("factsheet_updated", False),
            clusters=clusters,
            duration_seconds=elapsed,
        )

        logger.info(
            "Consolidation result for %s: %d facts across %d clusters, "
            "%d topics updated in %.1fs",
            project_id,
            result.facts_consolidated,
            len(clusters),
            len(result.topics_updated),
            elapsed,
        )

        return result
