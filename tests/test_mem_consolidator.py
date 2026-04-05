"""Integration tests for the MemoryConsolidator service and integration layer.

Tests cover:
- Threshold-based auto-triggering (growth threshold, cooldown, min age)
- Manual consolidation (with and without force)
- Deep consolidation pass
- Bootstrap consolidation
- Memory clustering / topic grouping
- Status reporting
- Integration lifecycle hooks (on_task_completed)
- Command handler responses
- Error resilience (non-fatal failures)
- Config edge cases (disabled, defaults)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

from src.config import MemoryConfig
from src.memory import MemoryManager
from src.services.memory_consolidator import ConsolidationResult, MemoryConsolidator
from src.integrations.memory_consolidation import MemoryConsolidationIntegration


# ---------------------------------------------------------------------------
# Lightweight fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    id: str = "task-100"
    project_id: str = "test-proj"
    title: str = "Implement feature X"
    description: str = "Add feature X with tests"
    task_type: MagicMock = field(default_factory=lambda: MagicMock(value="feature"))


@dataclass
class FakeOutput:
    result: MagicMock = field(default_factory=lambda: MagicMock(value="completed"))
    summary: str = "Implemented feature X with full test coverage."
    files_changed: list = field(default_factory=lambda: ["src/feature_x.py", "tests/test_x.py"])
    tokens_used: int = 3000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> MemoryConfig:
    """Create a MemoryConfig with consolidation enabled by default."""
    defaults = {
        "enabled": True,
        "consolidation_enabled": True,
        "consolidation_auto_trigger": True,
        "consolidation_growth_threshold": 5,
        "consolidation_min_age_hours": 0,  # no age requirement in tests
        "consolidation_cooldown_minutes": 0,  # no cooldown in tests
        "consolidation_max_batch_size": 50,
        "consolidation_similarity_threshold": 0.7,
    }
    defaults.update(overrides)
    return MemoryConfig(**defaults)


def _make_manager(tmp_path, **config_overrides) -> MemoryManager:
    cfg = _make_config(**config_overrides)
    return MemoryManager(cfg, storage_root=str(tmp_path))


def _make_consolidator(tmp_path, **config_overrides) -> MemoryConsolidator:
    mm = _make_manager(tmp_path, **config_overrides)
    return MemoryConsolidator(mm)


def _make_integration(tmp_path, **config_overrides) -> MemoryConsolidationIntegration:
    mm = _make_manager(tmp_path, **config_overrides)
    return MemoryConsolidationIntegration(mm)


def _write_staging_file(
    tmp_path,
    project_id: str,
    task_id: str,
    facts: list[dict] | None = None,
    age_seconds: float = 0,
) -> str:
    """Write a staging JSON file and optionally backdate it."""
    staging_dir = tmp_path / "memory" / project_id / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    if facts is None:
        facts = [
            {"category": "tech_stack", "key": f"lib_{task_id}", "value": "some-lib 1.0"},
            {"category": "decision", "key": f"dec_{task_id}", "value": "Use pattern X"},
        ]

    # Use backdated timestamp inside the doc so sort-by-extracted_at is stable
    ts = time.time() - age_seconds
    extracted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

    doc = {
        "task_id": task_id,
        "project_id": project_id,
        "task_title": f"Task {task_id}",
        "task_type": "feature",
        "extracted_at": extracted_at,
        "facts": facts,
    }

    path = staging_dir / f"{task_id}.json"
    path.write_text(json.dumps(doc, indent=2))

    if age_seconds > 0:
        old_time = time.time() - age_seconds
        os.utime(str(path), (old_time, old_time))

    return str(path)


# ===========================================================================
# ConsolidationResult tests
# ===========================================================================


class TestConsolidationResult:
    def test_default_values(self):
        r = ConsolidationResult()
        assert r.triggered is False
        assert r.reason == ""
        assert r.status == ""
        assert r.facts_consolidated == 0
        assert r.topics_updated == []
        assert r.clusters == []
        assert r.error == ""

    def test_to_dict(self):
        r = ConsolidationResult(
            triggered=True,
            reason="growth_threshold_exceeded",
            status="consolidated",
            facts_consolidated=5,
            topics_updated=["architecture", "decisions"],
            factsheet_updated=True,
            duration_seconds=1.234,
        )
        d = r.to_dict()
        assert d["triggered"] is True
        assert d["facts_consolidated"] == 5
        assert d["duration_seconds"] == 1.234
        assert "architecture" in d["topics_updated"]

    def test_to_dict_rounds_duration(self):
        r = ConsolidationResult(duration_seconds=1.23456789)
        assert r.to_dict()["duration_seconds"] == 1.235


# ===========================================================================
# MemoryConsolidator — threshold checks
# ===========================================================================


class TestConsolidatorThresholds:
    async def test_disabled_returns_immediately(self, tmp_path):
        c = _make_consolidator(tmp_path, consolidation_enabled=False)
        result = await c.check_and_consolidate("proj")
        assert not result.triggered
        assert result.status == "disabled"

    async def test_auto_trigger_disabled(self, tmp_path):
        c = _make_consolidator(tmp_path, consolidation_auto_trigger=False)
        result = await c.check_and_consolidate("proj")
        assert not result.triggered
        assert result.status == "skipped"

    async def test_below_threshold(self, tmp_path):
        # Write 3 staging files, threshold is 5
        for i in range(3):
            _write_staging_file(tmp_path, "proj", f"task-{i}")
        c = _make_consolidator(tmp_path)
        result = await c.check_and_consolidate("proj")
        assert not result.triggered
        assert result.status == "below_threshold"
        assert "3/5" in result.reason

    async def test_threshold_met_triggers_consolidation(self, tmp_path):
        """When staging file count >= threshold, consolidation should trigger."""
        for i in range(6):
            _write_staging_file(tmp_path, "proj", f"task-{i}")

        c = _make_consolidator(tmp_path)
        # Mock the MemoryManager's run_daily_consolidation
        c._mm.run_daily_consolidation = AsyncMock(return_value={
            "status": "consolidated",
            "staging_files_processed": 6,
            "facts_consolidated": 12,
            "topics_updated": ["architecture"],
            "factsheet_updated": True,
        })

        result = await c.check_and_consolidate("proj")
        assert result.triggered
        assert result.status == "consolidated"
        assert result.facts_consolidated == 12
        c._mm.run_daily_consolidation.assert_awaited_once()

    async def test_cooldown_blocks_trigger(self, tmp_path):
        c = _make_consolidator(tmp_path, consolidation_cooldown_minutes=60)
        # Simulate a recent run
        c._last_consolidation["proj"] = time.time()

        for i in range(6):
            _write_staging_file(tmp_path, "proj", f"task-{i}")

        result = await c.check_and_consolidate("proj")
        assert not result.triggered
        assert result.status == "cooldown"
        assert "remaining" in result.reason

    async def test_cooldown_expired_allows_trigger(self, tmp_path):
        c = _make_consolidator(tmp_path, consolidation_cooldown_minutes=1)
        # Simulate an old run (2 minutes ago)
        c._last_consolidation["proj"] = time.time() - 120

        for i in range(6):
            _write_staging_file(tmp_path, "proj", f"task-{i}")

        c._mm.run_daily_consolidation = AsyncMock(return_value={
            "status": "consolidated",
            "staging_files_processed": 6,
            "facts_consolidated": 12,
            "topics_updated": [],
            "factsheet_updated": False,
        })

        result = await c.check_and_consolidate("proj")
        assert result.triggered

    async def test_min_age_not_met(self, tmp_path):
        """Files must be old enough (consolidation_min_age_hours)."""
        for i in range(6):
            _write_staging_file(tmp_path, "proj", f"task-{i}", age_seconds=0)

        c = _make_consolidator(tmp_path, consolidation_min_age_hours=24.0)
        result = await c.check_and_consolidate("proj")
        assert not result.triggered
        assert result.status == "too_recent"

    async def test_min_age_met(self, tmp_path):
        """At least one file older than min_age_hours triggers."""
        for i in range(6):
            age = 7200 if i == 0 else 0  # first file is 2 hours old
            _write_staging_file(tmp_path, "proj", f"task-{i}", age_seconds=age)

        c = _make_consolidator(tmp_path, consolidation_min_age_hours=1.0)
        c._mm.run_daily_consolidation = AsyncMock(return_value={
            "status": "consolidated",
            "staging_files_processed": 6,
            "facts_consolidated": 10,
            "topics_updated": [],
            "factsheet_updated": False,
        })

        result = await c.check_and_consolidate("proj")
        assert result.triggered


# ===========================================================================
# MemoryConsolidator — manual operations
# ===========================================================================


class TestConsolidatorManual:
    async def test_consolidate_now_skips_threshold(self, tmp_path):
        """consolidate_now() runs even with zero staging files."""
        c = _make_consolidator(tmp_path)
        c._mm.run_daily_consolidation = AsyncMock(return_value={
            "status": "no_staging",
            "staging_files_processed": 0,
            "facts_consolidated": 0,
            "topics_updated": [],
            "factsheet_updated": False,
        })

        result = await c.consolidate_now("proj")
        assert result.triggered
        c._mm.run_daily_consolidation.assert_awaited_once()

    async def test_consolidate_now_respects_cooldown(self, tmp_path):
        c = _make_consolidator(tmp_path, consolidation_cooldown_minutes=60)
        c._last_consolidation["proj"] = time.time()

        result = await c.consolidate_now("proj")
        assert not result.triggered
        assert result.status == "cooldown"

    async def test_consolidate_now_force_ignores_cooldown(self, tmp_path):
        c = _make_consolidator(tmp_path, consolidation_cooldown_minutes=60)
        c._last_consolidation["proj"] = time.time()

        c._mm.run_daily_consolidation = AsyncMock(return_value={
            "status": "consolidated",
            "staging_files_processed": 3,
            "facts_consolidated": 5,
            "topics_updated": ["decisions"],
            "factsheet_updated": False,
        })

        result = await c.consolidate_now("proj", force=True)
        assert result.triggered
        assert result.status == "consolidated"

    async def test_consolidate_now_disabled(self, tmp_path):
        c = _make_consolidator(tmp_path, consolidation_enabled=False)
        result = await c.consolidate_now("proj")
        assert not result.triggered
        assert result.status == "disabled"


# ===========================================================================
# MemoryConsolidator — deep consolidation
# ===========================================================================


class TestDeepConsolidation:
    async def test_deep_consolidate_success(self, tmp_path):
        c = _make_consolidator(tmp_path)
        c._mm.run_deep_consolidation = AsyncMock(return_value={
            "status": "consolidated",
            "topics_reviewed": 7,
            "topics_updated": ["architecture", "gotchas"],
            "factsheet_updated": True,
            "pruned_facts": ["Removed stale CI URL"],
        })

        result = await c.deep_consolidate("proj")
        assert result.triggered
        assert result.reason == "deep_consolidation"
        assert result.factsheet_updated
        assert "Removed stale CI URL" in result.pruned_facts
        assert result.duration_seconds >= 0

    async def test_deep_consolidate_disabled(self, tmp_path):
        c = _make_consolidator(tmp_path, consolidation_enabled=False)
        result = await c.deep_consolidate("proj")
        assert not result.triggered
        assert result.status == "disabled"

    async def test_deep_consolidate_error_handling(self, tmp_path):
        c = _make_consolidator(tmp_path)
        c._mm.run_deep_consolidation = AsyncMock(
            side_effect=RuntimeError("LLM unavailable")
        )

        result = await c.deep_consolidate("proj")
        assert result.triggered
        assert result.status == "error"
        assert "LLM unavailable" in result.error


# ===========================================================================
# MemoryConsolidator — bootstrap
# ===========================================================================


class TestBootstrap:
    async def test_bootstrap_success(self, tmp_path):
        c = _make_consolidator(tmp_path)
        c._mm.bootstrap_consolidation = AsyncMock(return_value={
            "status": "bootstrapped",
            "tasks_processed": 15,
            "topics_created": ["architecture", "decisions", "conventions"],
            "factsheet_created": True,
        })

        result = await c.bootstrap("proj", project_name="My Project")
        assert result.triggered
        assert result.status == "bootstrapped"
        assert result.facts_consolidated == 15  # tasks_processed maps here
        assert result.factsheet_updated

    async def test_bootstrap_error(self, tmp_path):
        c = _make_consolidator(tmp_path)
        c._mm.bootstrap_consolidation = AsyncMock(
            side_effect=ValueError("No tasks found")
        )

        result = await c.bootstrap("proj")
        assert result.triggered
        assert result.status == "error"
        assert "No tasks found" in result.error


# ===========================================================================
# MemoryConsolidator — clustering
# ===========================================================================


class TestClustering:
    def test_cluster_empty_staging(self, tmp_path):
        c = _make_consolidator(tmp_path)
        clusters = c.cluster_staging_facts("proj")
        assert clusters == []

    def test_cluster_groups_by_topic(self, tmp_path):
        """Facts are grouped into topic clusters via category mapping."""
        facts = [
            {"category": "architecture", "key": "component_layout", "value": "Microservices"},
            {"category": "decision", "key": "db_choice", "value": "PostgreSQL for ACID"},
            {"category": "convention", "key": "naming", "value": "snake_case everywhere"},
            {"category": "tech_stack", "key": "orm", "value": "SQLAlchemy 2.0"},
        ]
        _write_staging_file(tmp_path, "proj", "task-1", facts=facts)

        c = _make_consolidator(tmp_path)
        clusters = c.cluster_staging_facts("proj")

        topic_names = {cl["topic"] for cl in clusters}
        # architecture fact -> architecture topic; decision -> decisions + architecture
        assert "architecture" in topic_names
        assert "decisions" in topic_names
        assert "conventions" in topic_names
        assert "dependencies" in topic_names

    def test_cluster_deduplicates(self, tmp_path):
        """Same (category, key) from multiple files -> single fact per cluster."""
        facts1 = [{"category": "tech_stack", "key": "orm", "value": "SQLAlchemy 1.4"}]
        facts2 = [{"category": "tech_stack", "key": "orm", "value": "SQLAlchemy 2.0"}]
        _write_staging_file(tmp_path, "proj", "task-old", facts=facts1, age_seconds=100)
        _write_staging_file(tmp_path, "proj", "task-new", facts=facts2, age_seconds=0)

        c = _make_consolidator(tmp_path)
        clusters = c.cluster_staging_facts("proj")

        # Should have 1 fact (newer wins) in the dependencies topic
        deps_cluster = next(
            (cl for cl in clusters if cl["topic"] == "dependencies"), None
        )
        assert deps_cluster is not None
        assert deps_cluster["fact_count"] == 1
        assert deps_cluster["facts"][0]["value"] == "SQLAlchemy 2.0"

    def test_cluster_tracks_source_tasks(self, tmp_path):
        """Each cluster records which tasks contributed facts."""
        _write_staging_file(tmp_path, "proj", "task-a", facts=[
            {"category": "architecture", "key": "pattern_a", "value": "CQRS"},
        ])
        _write_staging_file(tmp_path, "proj", "task-b", facts=[
            {"category": "architecture", "key": "pattern_b", "value": "Event sourcing"},
        ])

        c = _make_consolidator(tmp_path)
        clusters = c.cluster_staging_facts("proj")
        arch_cluster = next(
            (cl for cl in clusters if cl["topic"] == "architecture"), None
        )
        assert arch_cluster is not None
        assert "task-a" in arch_cluster["task_ids"]
        assert "task-b" in arch_cluster["task_ids"]


# ===========================================================================
# MemoryConsolidator — status
# ===========================================================================


class TestStatus:
    def test_status_report(self, tmp_path):
        for i in range(3):
            _write_staging_file(tmp_path, "proj", f"task-{i}")

        c = _make_consolidator(tmp_path, consolidation_growth_threshold=10)
        status = c.get_consolidation_status("proj")

        assert status["consolidation_enabled"] is True
        assert status["staging_files"] == 3
        assert status["growth_threshold"] == 10
        assert status["threshold_met"] is False
        assert status["last_consolidation"] is None
        assert status["cooldown_ok"] is True

    def test_status_after_consolidation(self, tmp_path):
        c = _make_consolidator(tmp_path)
        c._last_consolidation["proj"] = time.time() - 600  # 10 min ago
        status = c.get_consolidation_status("proj")
        assert status["last_consolidation"] is not None
        assert status["cooldown_ok"] is True


# ===========================================================================
# MemoryConsolidator — error resilience
# ===========================================================================


class TestErrorResilience:
    async def test_daily_consolidation_exception(self, tmp_path):
        """Service catches exceptions from MemoryManager."""
        for i in range(6):
            _write_staging_file(tmp_path, "proj", f"task-{i}")

        c = _make_consolidator(tmp_path)
        c._mm.run_daily_consolidation = AsyncMock(
            side_effect=RuntimeError("Database locked")
        )

        result = await c.check_and_consolidate("proj")
        assert result.triggered
        assert result.status == "error"
        assert "Database locked" in result.error

    async def test_staging_dir_missing(self, tmp_path):
        """No staging directory -> below threshold, not an error."""
        c = _make_consolidator(tmp_path)
        result = await c.check_and_consolidate("proj")
        assert not result.triggered
        assert result.status == "below_threshold"


# ===========================================================================
# Integration — on_task_completed
# ===========================================================================


class TestIntegrationLifecycle:
    async def test_on_task_completed_extracts_and_checks(self, tmp_path):
        integration = _make_integration(tmp_path)
        # Pre-fill staging to exceed threshold
        for i in range(6):
            _write_staging_file(tmp_path, "proj", f"task-{i}")

        # Mock both fact extraction and consolidation
        integration._mm.extract_task_facts = AsyncMock(return_value="/fake/path.json")
        integration._consolidator._mm.run_daily_consolidation = AsyncMock(
            return_value={
                "status": "consolidated",
                "staging_files_processed": 6,
                "facts_consolidated": 10,
                "topics_updated": ["architecture"],
                "factsheet_updated": True,
            }
        )

        result = await integration.on_task_completed(
            "proj", FakeTask(), FakeOutput()
        )
        assert result.triggered
        integration._mm.extract_task_facts.assert_awaited_once()

    async def test_on_task_completed_fact_extraction_failure_nonfatal(self, tmp_path):
        """Fact extraction failure doesn't prevent threshold check."""
        integration = _make_integration(tmp_path)
        integration._mm.extract_task_facts = AsyncMock(
            side_effect=RuntimeError("LLM down")
        )

        result = await integration.on_task_completed(
            "proj", FakeTask(), FakeOutput()
        )
        # Should still return a result (not raise)
        assert isinstance(result, ConsolidationResult)

    async def test_on_task_completed_below_threshold(self, tmp_path):
        integration = _make_integration(tmp_path)
        integration._mm.extract_task_facts = AsyncMock(return_value=None)

        result = await integration.on_task_completed(
            "proj", FakeTask(), FakeOutput()
        )
        assert not result.triggered


# ===========================================================================
# Integration — command handlers
# ===========================================================================


class TestIntegrationCommands:
    async def test_consolidate_command(self, tmp_path):
        integration = _make_integration(tmp_path)
        integration._consolidator._mm.run_daily_consolidation = AsyncMock(
            return_value={
                "status": "no_staging",
                "staging_files_processed": 0,
                "facts_consolidated": 0,
                "topics_updated": [],
                "factsheet_updated": False,
            }
        )

        resp = await integration.handle_consolidate_command("proj")
        assert resp["success"] is True
        assert "message" in resp

    async def test_deep_consolidate_command(self, tmp_path):
        integration = _make_integration(tmp_path)
        integration._consolidator._mm.run_deep_consolidation = AsyncMock(
            return_value={
                "status": "consolidated",
                "topics_reviewed": 7,
                "topics_updated": ["gotchas"],
                "factsheet_updated": False,
                "pruned_facts": [],
            }
        )

        resp = await integration.handle_deep_consolidate_command("proj")
        assert resp["success"] is True

    async def test_bootstrap_command(self, tmp_path):
        integration = _make_integration(tmp_path)
        integration._consolidator._mm.bootstrap_consolidation = AsyncMock(
            return_value={
                "status": "bootstrapped",
                "tasks_processed": 10,
                "topics_created": ["architecture"],
                "factsheet_created": True,
            }
        )

        resp = await integration.handle_bootstrap_command(
            "proj", project_name="Test", repo_url="https://example.com"
        )
        assert resp["success"] is True
        assert resp["status"] == "bootstrapped"

    async def test_status_command(self, tmp_path):
        integration = _make_integration(tmp_path)
        for i in range(3):
            _write_staging_file(tmp_path, "proj", f"task-{i}")

        resp = await integration.handle_status_command("proj")
        assert resp["success"] is True
        assert resp["staging_files"] == 3
        assert "pending_clusters" in resp

    async def test_force_consolidate_command(self, tmp_path):
        integration = _make_integration(
            tmp_path, consolidation_cooldown_minutes=60
        )
        integration._consolidator._last_consolidation["proj"] = time.time()

        integration._consolidator._mm.run_daily_consolidation = AsyncMock(
            return_value={
                "status": "consolidated",
                "staging_files_processed": 0,
                "facts_consolidated": 0,
                "topics_updated": [],
                "factsheet_updated": False,
            }
        )

        # Without force — should be blocked by cooldown
        resp = await integration.handle_consolidate_command("proj")
        assert resp["status"] == "cooldown"

        # With force — should bypass cooldown
        resp = await integration.handle_consolidate_command("proj", force=True)
        assert resp["triggered"] is True


# ===========================================================================
# Integration — result formatting
# ===========================================================================


class TestResultFormatting:
    def test_format_skipped(self):
        r = ConsolidationResult(triggered=False, reason="below_threshold")
        msg = MemoryConsolidationIntegration._format_result_message(r)
        assert "skipped" in msg.lower()
        assert "below_threshold" in msg

    def test_format_error(self):
        r = ConsolidationResult(
            triggered=True, status="error", error="connection refused"
        )
        msg = MemoryConsolidationIntegration._format_result_message(r)
        assert "failed" in msg.lower()
        assert "connection refused" in msg

    def test_format_success(self):
        r = ConsolidationResult(
            triggered=True,
            status="consolidated",
            facts_consolidated=8,
            staging_files_processed=4,
            topics_updated=["architecture", "decisions"],
            factsheet_updated=True,
            duration_seconds=2.5,
        )
        msg = MemoryConsolidationIntegration._format_result_message(r)
        assert "8 facts" in msg
        assert "4 staging files" in msg
        assert "architecture" in msg
        assert "factsheet updated" in msg
        assert "2.5s" in msg

    def test_format_pruned(self):
        r = ConsolidationResult(
            triggered=True,
            status="consolidated",
            pruned_facts=["Stale URL removed", "Duplicate convention merged"],
        )
        msg = MemoryConsolidationIntegration._format_result_message(r)
        assert "2 facts pruned" in msg


# ===========================================================================
# Config defaults and edge cases
# ===========================================================================


class TestConfigEdgeCases:
    def test_new_threshold_fields_exist(self):
        cfg = MemoryConfig()
        assert hasattr(cfg, "consolidation_auto_trigger")
        assert hasattr(cfg, "consolidation_growth_threshold")
        assert hasattr(cfg, "consolidation_min_age_hours")
        assert hasattr(cfg, "consolidation_max_batch_size")
        assert hasattr(cfg, "consolidation_similarity_threshold")
        assert hasattr(cfg, "consolidation_cooldown_minutes")

    def test_default_values(self):
        cfg = MemoryConfig()
        assert cfg.consolidation_auto_trigger is True
        assert cfg.consolidation_growth_threshold == 10
        assert cfg.consolidation_min_age_hours == 1.0
        assert cfg.consolidation_max_batch_size == 50
        assert cfg.consolidation_similarity_threshold == 0.7
        assert cfg.consolidation_cooldown_minutes == 30

    def test_no_duplicate_consolidation_fields(self):
        """Verify the duplicate Phase4/Phase5 config fields are resolved."""
        cfg = MemoryConfig()
        # consolidation_enabled should be False by default (master switch)
        assert cfg.consolidation_enabled is False
        # consolidation_schedule should use cron syntax
        assert cfg.consolidation_schedule == "0 3 * * *"

    def test_custom_overrides(self):
        cfg = MemoryConfig(
            consolidation_growth_threshold=20,
            consolidation_cooldown_minutes=5,
        )
        assert cfg.consolidation_growth_threshold == 20
        assert cfg.consolidation_cooldown_minutes == 5
