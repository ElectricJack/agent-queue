"""Tests for the workflow pipeline view module.

Validates that the pipeline view produces correct, dashboard-ready JSON
structures from workflow and task data.  All tests use mock data — no
database or orchestrator required.

Roadmap 7.6.1 — spec §11 Q6.
"""

from __future__ import annotations


from src.models import Task, TaskStatus, TaskType, Workflow, WorkspaceMode
from src.workflow_pipeline_view import (
    AFFINITY_SYMBOLS,
    AGENT_TYPE_COLORS,
    STAGE_STATUS_COLORS,
    STAGE_STATUS_SYMBOLS,
    _infer_stages_from_tasks,
    _resolve_stages,
    _task_progress,
    _task_status_category,
    build_affinity_overlay,
    build_agent_summary,
    build_pipeline_view,
    build_progress_summary,
    build_stage_connections,
    build_stages,
    build_task_card,
)


# ---------------------------------------------------------------------------
# Test helpers — factory functions for test data
# ---------------------------------------------------------------------------


def _make_workflow(
    *,
    workflow_id: str = "wf-1",
    playbook_id: str = "coord-playbook",
    playbook_run_id: str = "run-1",
    project_id: str = "proj-1",
    status: str = "running",
    current_stage: str | None = None,
    task_ids: list[str] | None = None,
    agent_affinity: dict[str, str] | None = None,
    stages: list[dict] | None = None,
    created_at: float = 1000.0,
    completed_at: float | None = None,
) -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        playbook_id=playbook_id,
        playbook_run_id=playbook_run_id,
        project_id=project_id,
        status=status,
        current_stage=current_stage,
        task_ids=task_ids or [],
        agent_affinity=agent_affinity or {},
        stages=stages or [],
        created_at=created_at,
        completed_at=completed_at,
    )


def _make_task(
    *,
    task_id: str = "task-1",
    project_id: str = "proj-1",
    title: str = "Implement feature",
    description: str = "Do the work",
    status: TaskStatus = TaskStatus.DEFINED,
    assigned_agent_id: str | None = None,
    agent_type: str | None = None,
    affinity_agent_id: str | None = None,
    affinity_reason: str | None = None,
    workflow_id: str | None = "wf-1",
    workspace_mode: WorkspaceMode | None = None,
    task_type: TaskType | None = None,
    created_at: float = 1000.0,
    pr_url: str | None = None,
    retry_count: int = 0,
    max_retries: int = 3,
) -> Task:
    return Task(
        id=task_id,
        project_id=project_id,
        title=title,
        description=description,
        status=status,
        assigned_agent_id=assigned_agent_id,
        agent_type=agent_type,
        affinity_agent_id=affinity_agent_id,
        affinity_reason=affinity_reason,
        workflow_id=workflow_id,
        workspace_mode=workspace_mode,
        task_type=task_type,
        created_at=created_at,
        pr_url=pr_url,
        retry_count=retry_count,
        max_retries=max_retries,
    )


# ---------------------------------------------------------------------------
# Task classification tests
# ---------------------------------------------------------------------------


class TestTaskStatusCategory:
    """Tests for _task_status_category mapping."""

    def test_defined_is_pending(self):
        task = _make_task(status=TaskStatus.DEFINED)
        assert _task_status_category(task) == "pending"

    def test_ready_is_pending(self):
        task = _make_task(status=TaskStatus.READY)
        assert _task_status_category(task) == "pending"

    def test_in_progress_is_active(self):
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        assert _task_status_category(task) == "active"

    def test_assigned_is_active(self):
        task = _make_task(status=TaskStatus.ASSIGNED)
        assert _task_status_category(task) == "active"

    def test_waiting_input_is_active(self):
        task = _make_task(status=TaskStatus.WAITING_INPUT)
        assert _task_status_category(task) == "active"

    def test_completed_is_completed(self):
        task = _make_task(status=TaskStatus.COMPLETED)
        assert _task_status_category(task) == "completed"

    def test_failed_is_failed(self):
        task = _make_task(status=TaskStatus.FAILED)
        assert _task_status_category(task) == "failed"

    def test_blocked_is_blocked(self):
        task = _make_task(status=TaskStatus.BLOCKED)
        assert _task_status_category(task) == "blocked"

    def test_paused_is_paused(self):
        task = _make_task(status=TaskStatus.PAUSED)
        assert _task_status_category(task) == "paused"


class TestTaskProgress:
    """Tests for _task_progress computation."""

    def test_completed_is_1(self):
        task = _make_task(status=TaskStatus.COMPLETED)
        assert _task_progress(task) == 1.0

    def test_in_progress_is_half(self):
        task = _make_task(status=TaskStatus.IN_PROGRESS)
        assert _task_progress(task) == 0.5

    def test_defined_is_zero(self):
        task = _make_task(status=TaskStatus.DEFINED)
        assert _task_progress(task) == 0.0

    def test_failed_is_zero(self):
        task = _make_task(status=TaskStatus.FAILED)
        assert _task_progress(task) == 0.0


# ---------------------------------------------------------------------------
# Stage inference tests
# ---------------------------------------------------------------------------


class TestInferStagesFromTasks:
    """Tests for _infer_stages_from_tasks heuristic grouping."""

    def test_empty_tasks(self):
        workflow = _make_workflow()
        assert _infer_stages_from_tasks(workflow, []) == []

    def test_single_task_single_stage(self):
        workflow = _make_workflow(current_stage="impl")
        tasks = [_make_task(task_id="t1", created_at=1000.0)]
        stages = _infer_stages_from_tasks(workflow, tasks)
        assert len(stages) == 1
        assert stages[0]["name"] == "impl"
        assert stages[0]["task_ids"] == ["t1"]

    def test_tasks_close_together_same_stage(self):
        """Tasks within 30s of each other should group into one stage."""
        workflow = _make_workflow()
        tasks = [
            _make_task(task_id="t1", created_at=1000.0),
            _make_task(task_id="t2", created_at=1010.0),
            _make_task(task_id="t3", created_at=1020.0),
        ]
        stages = _infer_stages_from_tasks(workflow, tasks)
        assert len(stages) == 1
        assert set(stages[0]["task_ids"]) == {"t1", "t2", "t3"}

    def test_tasks_far_apart_different_stages(self):
        """Tasks more than 30s apart should split into separate stages."""
        workflow = _make_workflow(current_stage="review")
        tasks = [
            _make_task(task_id="t1", created_at=1000.0),
            _make_task(task_id="t2", created_at=1010.0),
            _make_task(task_id="t3", created_at=1100.0),  # >30s gap
        ]
        stages = _infer_stages_from_tasks(workflow, tasks)
        assert len(stages) == 2
        assert stages[0]["task_ids"] == ["t1", "t2"]
        assert stages[1]["task_ids"] == ["t3"]
        # Last stage gets current_stage name
        assert stages[1]["name"] == "review"

    def test_stage_status_computed_from_tasks(self):
        """Stage status should reflect task statuses."""
        workflow = _make_workflow()
        tasks = [
            _make_task(task_id="t1", status=TaskStatus.COMPLETED, created_at=1000.0),
            _make_task(task_id="t2", status=TaskStatus.COMPLETED, created_at=1010.0),
        ]
        stages = _infer_stages_from_tasks(workflow, tasks)
        assert stages[0]["status"] == "completed"

    def test_active_tasks_make_active_stage(self):
        workflow = _make_workflow()
        tasks = [
            _make_task(task_id="t1", status=TaskStatus.COMPLETED, created_at=1000.0),
            _make_task(task_id="t2", status=TaskStatus.IN_PROGRESS, created_at=1010.0),
        ]
        stages = _infer_stages_from_tasks(workflow, tasks)
        assert stages[0]["status"] == "active"

    def test_failed_task_makes_failed_stage(self):
        workflow = _make_workflow()
        tasks = [
            _make_task(task_id="t1", status=TaskStatus.COMPLETED, created_at=1000.0),
            _make_task(task_id="t2", status=TaskStatus.FAILED, created_at=1010.0),
        ]
        stages = _infer_stages_from_tasks(workflow, tasks)
        assert stages[0]["status"] == "failed"

    def test_three_stages_from_timestamps(self):
        """Three distinct time clusters produce three stages."""
        workflow = _make_workflow(current_stage="qa")
        tasks = [
            _make_task(task_id="t1", created_at=1000.0, status=TaskStatus.COMPLETED),
            _make_task(task_id="t2", created_at=1005.0, status=TaskStatus.COMPLETED),
            _make_task(task_id="t3", created_at=1100.0, status=TaskStatus.COMPLETED),
            _make_task(task_id="t4", created_at=1200.0, status=TaskStatus.IN_PROGRESS),
        ]
        stages = _infer_stages_from_tasks(workflow, tasks)
        assert len(stages) == 3
        assert stages[2]["name"] == "qa"  # last stage gets workflow.current_stage


class TestResolveStages:
    """Tests for _resolve_stages — explicit vs inferred stages."""

    def test_uses_explicit_stages_when_available(self):
        explicit = [{"name": "build", "task_ids": ["t1"], "status": "completed"}]
        workflow = _make_workflow(stages=explicit)
        tasks = [_make_task(task_id="t1")]
        stages = _resolve_stages(workflow, tasks)
        assert stages == explicit

    def test_falls_back_to_inference_when_no_explicit_stages(self):
        workflow = _make_workflow(stages=[])
        tasks = [_make_task(task_id="t1", created_at=1000.0)]
        stages = _resolve_stages(workflow, tasks)
        assert len(stages) == 1
        assert stages[0]["task_ids"] == ["t1"]


# ---------------------------------------------------------------------------
# Task card tests
# ---------------------------------------------------------------------------


class TestBuildTaskCard:
    """Tests for build_task_card."""

    def test_basic_card(self):
        task = _make_task(title="Add API endpoint", status=TaskStatus.IN_PROGRESS)
        card = build_task_card(task)
        assert card["task_id"] == "task-1"
        assert card["title"] == "Add API endpoint"
        assert card["status"] == "IN_PROGRESS"
        assert card["status_category"] == "active"
        assert card["progress"] == 0.5
        assert "colors" in card

    def test_card_with_agent_assignment(self):
        task = _make_task(assigned_agent_id="claude-1")
        card = build_task_card(task)
        assert card["assigned_agent"] == "claude-1"

    def test_card_with_agent_type(self):
        task = _make_task(agent_type="coding")
        card = build_task_card(task)
        assert card["agent_type"] == "coding"
        assert card["agent_type_colors"] == AGENT_TYPE_COLORS["coding"]

    def test_card_with_affinity(self):
        task = _make_task(
            affinity_agent_id="claude-1",
            affinity_reason="context",
        )
        card = build_task_card(task)
        assert card["affinity_agent"] == "claude-1"
        assert card["affinity_reason"] == "context"
        assert card["affinity_symbol"] == AFFINITY_SYMBOLS["context"]

    def test_card_with_task_type(self):
        task = _make_task(task_type=TaskType.FEATURE)
        card = build_task_card(task)
        assert card["task_type"] == "feature"

    def test_card_with_workspace_mode(self):
        task = _make_task(workspace_mode=WorkspaceMode.BRANCH_ISOLATED)
        card = build_task_card(task)
        assert card["workspace_mode"] == "branch-isolated"

    def test_card_with_pr_url(self):
        task = _make_task(pr_url="https://github.com/foo/bar/pull/42")
        card = build_task_card(task)
        assert card["pr_url"] == "https://github.com/foo/bar/pull/42"

    def test_card_with_retry_count(self):
        task = _make_task(retry_count=2, max_retries=5)
        card = build_task_card(task)
        assert card["retry_count"] == 2
        assert card["max_retries"] == 5

    def test_card_no_retry_when_zero(self):
        task = _make_task(retry_count=0)
        card = build_task_card(task)
        assert "retry_count" not in card

    def test_card_with_timestamp(self):
        task = _make_task(created_at=12345.0)
        card = build_task_card(task)
        assert card["created_at"] == 12345.0

    def test_card_no_optional_fields_when_absent(self):
        task = _make_task()
        card = build_task_card(task)
        assert "assigned_agent" not in card
        assert "agent_type" not in card
        assert "affinity_agent" not in card
        assert "pr_url" not in card
        assert "task_type" not in card
        assert "workspace_mode" not in card


# ---------------------------------------------------------------------------
# Stage building tests
# ---------------------------------------------------------------------------


class TestBuildStages:
    """Tests for build_stages."""

    def test_single_stage_with_tasks(self):
        workflow = _make_workflow(
            current_stage="impl",
            task_ids=["t1", "t2"],
            stages=[
                {
                    "name": "impl",
                    "task_ids": ["t1", "t2"],
                    "status": "active",
                    "started_at": 1000.0,
                    "completed_at": None,
                }
            ],
        )
        tasks = [
            _make_task(task_id="t1", status=TaskStatus.COMPLETED),
            _make_task(task_id="t2", status=TaskStatus.IN_PROGRESS, assigned_agent_id="claude-1"),
        ]
        stages = build_stages(workflow, tasks)

        assert len(stages) == 1
        stage = stages[0]
        assert stage["name"] == "impl"
        assert stage["order"] == 0
        assert stage["status"] == "active"  # computed from tasks
        assert stage["task_count"] == 2
        assert stage["completed_count"] == 1
        assert stage["progress"] == 0.5
        assert stage["is_current"] is True
        assert len(stage["tasks"]) == 2
        assert "agent_assignments" in stage
        assert stage["agent_assignments"]["claude-1"] == ["t2"]

    def test_multiple_stages(self):
        workflow = _make_workflow(
            current_stage="review",
            task_ids=["t1", "t2", "t3"],
            stages=[
                {
                    "name": "impl",
                    "task_ids": ["t1", "t2"],
                    "status": "completed",
                    "started_at": 1000.0,
                    "completed_at": 1100.0,
                },
                {
                    "name": "review",
                    "task_ids": ["t3"],
                    "status": "active",
                    "started_at": 1100.0,
                    "completed_at": None,
                },
            ],
        )
        tasks = [
            _make_task(task_id="t1", status=TaskStatus.COMPLETED),
            _make_task(task_id="t2", status=TaskStatus.COMPLETED),
            _make_task(task_id="t3", status=TaskStatus.IN_PROGRESS, assigned_agent_id="claude-2"),
        ]
        stages = build_stages(workflow, tasks)

        assert len(stages) == 2
        assert stages[0]["name"] == "impl"
        assert stages[0]["status"] == "completed"
        assert stages[0]["progress"] == 1.0
        assert stages[0]["is_current"] is False
        assert stages[1]["name"] == "review"
        assert stages[1]["status"] == "active"
        assert stages[1]["is_current"] is True

    def test_without_task_details(self):
        workflow = _make_workflow(
            stages=[{"name": "s1", "task_ids": ["t1"], "status": "active"}],
        )
        tasks = [_make_task(task_id="t1")]
        stages = build_stages(workflow, tasks, include_task_details=False)
        assert "tasks" not in stages[0]

    def test_stage_with_no_matching_tasks(self):
        """Stage references task IDs that don't exist in the task list."""
        workflow = _make_workflow(
            stages=[{"name": "s1", "task_ids": ["t-missing"], "status": "pending"}],
        )
        stages = build_stages(workflow, [])
        assert stages[0]["status"] == "pending"
        assert stages[0]["task_count"] == 1  # from task_ids length
        assert stages[0]["completed_count"] == 0

    def test_stage_colors_match_status(self):
        workflow = _make_workflow(
            stages=[
                {
                    "name": "done",
                    "task_ids": ["t1"],
                    "status": "completed",
                }
            ],
        )
        tasks = [_make_task(task_id="t1", status=TaskStatus.COMPLETED)]
        stages = build_stages(workflow, tasks)
        assert stages[0]["colors"] == STAGE_STATUS_COLORS["completed"]
        assert stages[0]["symbol"] == STAGE_STATUS_SYMBOLS["completed"]


# ---------------------------------------------------------------------------
# Stage connections tests
# ---------------------------------------------------------------------------


class TestBuildStageConnections:
    """Tests for build_stage_connections."""

    def test_no_connections_for_single_stage(self):
        stages = [{"name": "s1", "order": 0, "status": "active"}]
        conns = build_stage_connections(stages)
        assert conns == []

    def test_single_connection(self):
        stages = [
            {"name": "impl", "order": 0, "status": "completed"},
            {"name": "review", "order": 1, "status": "active"},
        ]
        conns = build_stage_connections(stages)
        assert len(conns) == 1
        assert conns[0]["from_stage"] == "impl"
        assert conns[0]["to_stage"] == "review"
        assert conns[0]["status"] == "completed"
        assert conns[0]["color"] == "#4CAF50"  # green for completed source

    def test_multiple_connections(self):
        stages = [
            {"name": "s1", "order": 0, "status": "completed"},
            {"name": "s2", "order": 1, "status": "completed"},
            {"name": "s3", "order": 2, "status": "active"},
        ]
        conns = build_stage_connections(stages)
        assert len(conns) == 2

    def test_pending_connection(self):
        stages = [
            {"name": "s1", "order": 0, "status": "pending"},
            {"name": "s2", "order": 1, "status": "pending"},
        ]
        conns = build_stage_connections(stages)
        assert conns[0]["status"] == "pending"
        assert conns[0]["color"] == "#BDBDBD"

    def test_active_connection(self):
        stages = [
            {"name": "s1", "order": 0, "status": "active"},
            {"name": "s2", "order": 1, "status": "pending"},
        ]
        conns = build_stage_connections(stages)
        assert conns[0]["status"] == "active"
        assert conns[0]["color"] == "#2196F3"


# ---------------------------------------------------------------------------
# Progress summary tests
# ---------------------------------------------------------------------------


class TestBuildProgressSummary:
    """Tests for build_progress_summary."""

    def test_all_completed(self):
        workflow = _make_workflow()
        tasks = [
            _make_task(task_id="t1", status=TaskStatus.COMPLETED),
            _make_task(task_id="t2", status=TaskStatus.COMPLETED),
        ]
        stages = [{"status": "completed"}, {"status": "completed"}]
        progress = build_progress_summary(workflow, tasks, stages)
        assert progress["total_tasks"] == 2
        assert progress["completed_tasks"] == 2
        assert progress["overall_progress"] == 1.0
        assert progress["completed_stages"] == 2

    def test_mixed_statuses(self):
        workflow = _make_workflow()
        tasks = [
            _make_task(task_id="t1", status=TaskStatus.COMPLETED),
            _make_task(task_id="t2", status=TaskStatus.IN_PROGRESS),
            _make_task(task_id="t3", status=TaskStatus.DEFINED),
            _make_task(task_id="t4", status=TaskStatus.FAILED),
        ]
        stages = [{"status": "completed"}, {"status": "active"}]
        progress = build_progress_summary(workflow, tasks, stages)
        assert progress["total_tasks"] == 4
        assert progress["completed_tasks"] == 1
        assert progress["active_tasks"] == 1
        assert progress["pending_tasks"] == 1
        assert progress["failed_tasks"] == 1
        assert progress["overall_progress"] == 0.25

    def test_empty_workflow(self):
        workflow = _make_workflow()
        progress = build_progress_summary(workflow, [], [])
        assert progress["total_tasks"] == 0
        assert progress["overall_progress"] == 0.0
        assert progress["total_stages"] == 0

    def test_stage_counts(self):
        workflow = _make_workflow()
        stages = [
            {"status": "completed"},
            {"status": "active"},
            {"status": "pending"},
            {"status": "pending"},
        ]
        progress = build_progress_summary(workflow, [], stages)
        assert progress["total_stages"] == 4
        assert progress["completed_stages"] == 1
        assert progress["active_stages"] == 1
        assert progress["pending_stages"] == 2


# ---------------------------------------------------------------------------
# Agent summary tests
# ---------------------------------------------------------------------------


class TestBuildAgentSummary:
    """Tests for build_agent_summary."""

    def test_single_agent(self):
        tasks = [
            _make_task(
                task_id="t1",
                assigned_agent_id="claude-1",
                status=TaskStatus.COMPLETED,
                agent_type="coding",
            ),
            _make_task(
                task_id="t2",
                assigned_agent_id="claude-1",
                status=TaskStatus.IN_PROGRESS,
                agent_type="coding",
            ),
        ]
        summary = build_agent_summary(tasks)
        assert "claude-1" in summary
        agent = summary["claude-1"]
        assert agent["tasks_completed"] == 1
        assert agent["tasks_assigned"] == 2
        assert agent["current_task"] == "t2"
        assert agent["agent_type"] == "coding"

    def test_multiple_agents(self):
        tasks = [
            _make_task(task_id="t1", assigned_agent_id="claude-1", status=TaskStatus.COMPLETED),
            _make_task(task_id="t2", assigned_agent_id="claude-2", status=TaskStatus.IN_PROGRESS),
        ]
        summary = build_agent_summary(tasks)
        assert len(summary) == 2
        assert "claude-1" in summary
        assert "claude-2" in summary

    def test_no_agents(self):
        tasks = [_make_task(task_id="t1")]  # no assigned_agent_id
        summary = build_agent_summary(tasks)
        assert summary == {}

    def test_failed_task_count(self):
        tasks = [
            _make_task(task_id="t1", assigned_agent_id="a1", status=TaskStatus.FAILED),
        ]
        summary = build_agent_summary(tasks)
        assert summary["a1"]["tasks_failed"] == 1

    def test_agent_enrichment(self):
        tasks = [
            _make_task(task_id="t1", assigned_agent_id="claude-1", status=TaskStatus.COMPLETED),
        ]
        agents = [{"id": "claude-1", "name": "Claude Worker 1", "state": "IDLE"}]
        summary = build_agent_summary(tasks, agents=agents)
        assert summary["claude-1"]["name"] == "Claude Worker 1"
        assert summary["claude-1"]["state"] == "IDLE"


# ---------------------------------------------------------------------------
# Affinity overlay tests
# ---------------------------------------------------------------------------


class TestBuildAffinityOverlay:
    """Tests for build_affinity_overlay."""

    def test_no_affinities(self):
        workflow = _make_workflow()
        tasks = [_make_task(task_id="t1")]
        overlay = build_affinity_overlay(workflow, tasks)
        assert overlay["total"] == 0
        assert overlay["honored"] == 0
        assert overlay["honor_rate"] == 0.0

    def test_honored_affinity(self):
        workflow = _make_workflow()
        tasks = [
            _make_task(
                task_id="t1",
                affinity_agent_id="claude-1",
                assigned_agent_id="claude-1",
                affinity_reason="context",
            ),
        ]
        overlay = build_affinity_overlay(workflow, tasks)
        assert overlay["total"] == 1
        assert overlay["honored"] == 1
        assert overlay["honor_rate"] == 1.0
        assert overlay["affinities"][0]["is_honored"] is True
        assert overlay["affinities"][0]["symbol"] == AFFINITY_SYMBOLS["context"]

    def test_unmatched_affinity(self):
        workflow = _make_workflow()
        tasks = [
            _make_task(
                task_id="t1",
                affinity_agent_id="claude-1",
                assigned_agent_id="claude-2",
                affinity_reason="workspace",
            ),
        ]
        overlay = build_affinity_overlay(workflow, tasks)
        assert overlay["honored"] == 0
        assert overlay["honor_rate"] == 0.0
        assert overlay["affinities"][0]["is_honored"] is False

    def test_workflow_level_affinity_conflict(self):
        """Workflow-level affinity differs from task-level affinity."""
        workflow = _make_workflow(agent_affinity={"t1": "claude-3"})
        tasks = [
            _make_task(
                task_id="t1",
                affinity_agent_id="claude-1",
                assigned_agent_id="claude-1",
            ),
        ]
        overlay = build_affinity_overlay(workflow, tasks)
        assert overlay["affinities"][0]["workflow_affinity"] == "claude-3"


# ---------------------------------------------------------------------------
# Full pipeline view tests
# ---------------------------------------------------------------------------


class TestBuildPipelineView:
    """Tests for build_pipeline_view — the main entry point."""

    def test_empty_workflow(self):
        workflow = _make_workflow()
        view = build_pipeline_view(workflow, [])
        assert view["workflow"]["workflow_id"] == "wf-1"
        assert view["pipeline"]["stages"] == []
        assert view["pipeline"]["connections"] == []
        assert view["progress"]["total_tasks"] == 0
        assert view["agents"] == {}
        assert "legend" in view
        assert "layout" in view

    def test_single_stage_workflow(self):
        workflow = _make_workflow(
            current_stage="impl",
            task_ids=["t1", "t2"],
            stages=[
                {
                    "name": "impl",
                    "task_ids": ["t1", "t2"],
                    "status": "active",
                    "started_at": 1000.0,
                }
            ],
        )
        tasks = [
            _make_task(
                task_id="t1",
                status=TaskStatus.COMPLETED,
                assigned_agent_id="claude-1",
                agent_type="coding",
            ),
            _make_task(
                task_id="t2",
                status=TaskStatus.IN_PROGRESS,
                assigned_agent_id="claude-2",
                agent_type="coding",
            ),
        ]
        view = build_pipeline_view(workflow, tasks)

        # Workflow metadata
        assert view["workflow"]["status"] == "running"
        assert view["workflow"]["current_stage"] == "impl"

        # Pipeline
        assert len(view["pipeline"]["stages"]) == 1
        assert view["pipeline"]["connections"] == []
        assert view["pipeline"]["stage_count"] == 1

        # Progress
        assert view["progress"]["total_tasks"] == 2
        assert view["progress"]["completed_tasks"] == 1
        assert view["progress"]["active_tasks"] == 1

        # Agents
        assert "claude-1" in view["agents"]
        assert "claude-2" in view["agents"]

    def test_multi_stage_pipeline(self):
        """Full multi-stage pipeline with varied task statuses."""
        workflow = _make_workflow(
            current_stage="review",
            task_ids=["t1", "t2", "t3", "t4"],
            stages=[
                {
                    "name": "impl",
                    "task_ids": ["t1", "t2"],
                    "status": "completed",
                    "started_at": 1000.0,
                    "completed_at": 1100.0,
                },
                {
                    "name": "review",
                    "task_ids": ["t3"],
                    "status": "active",
                    "started_at": 1100.0,
                },
                {
                    "name": "qa",
                    "task_ids": ["t4"],
                    "status": "pending",
                    "started_at": None,
                },
            ],
        )
        tasks = [
            _make_task(
                task_id="t1",
                status=TaskStatus.COMPLETED,
                assigned_agent_id="claude-1",
                agent_type="coding",
            ),
            _make_task(
                task_id="t2",
                status=TaskStatus.COMPLETED,
                assigned_agent_id="claude-1",
                agent_type="coding",
            ),
            _make_task(
                task_id="t3",
                status=TaskStatus.IN_PROGRESS,
                assigned_agent_id="claude-2",
                agent_type="code-review",
            ),
            _make_task(
                task_id="t4",
                status=TaskStatus.DEFINED,
                agent_type="qa",
            ),
        ]
        view = build_pipeline_view(workflow, tasks)

        # Three stages
        stages = view["pipeline"]["stages"]
        assert len(stages) == 3
        assert stages[0]["name"] == "impl"
        assert stages[0]["status"] == "completed"
        assert stages[1]["name"] == "review"
        assert stages[1]["status"] == "active"
        assert stages[2]["name"] == "qa"
        assert stages[2]["status"] == "pending"

        # Two connections
        conns = view["pipeline"]["connections"]
        assert len(conns) == 2
        assert conns[0]["from_stage"] == "impl"
        assert conns[0]["to_stage"] == "review"
        assert conns[0]["status"] == "completed"
        assert conns[1]["from_stage"] == "review"
        assert conns[1]["to_stage"] == "qa"
        assert conns[1]["status"] == "active"

        # Progress
        assert view["progress"]["total_tasks"] == 4
        assert view["progress"]["completed_tasks"] == 2
        assert view["progress"]["overall_progress"] == 0.5
        assert view["progress"]["completed_stages"] == 1
        assert view["progress"]["active_stages"] == 1

        # Agents — claude-1 completed 2, claude-2 doing 1
        assert view["agents"]["claude-1"]["tasks_completed"] == 2
        assert view["agents"]["claude-2"]["current_task"] == "t3"

    def test_direction_parameter(self):
        workflow = _make_workflow()
        view_lr = build_pipeline_view(workflow, [], direction="LR")
        assert view_lr["layout"]["direction"] == "LR"

        view_td = build_pipeline_view(workflow, [], direction="TD")
        assert view_td["layout"]["direction"] == "TD"

    def test_without_task_details(self):
        workflow = _make_workflow(
            stages=[{"name": "s1", "task_ids": ["t1"]}],
            task_ids=["t1"],
        )
        tasks = [_make_task(task_id="t1")]
        view = build_pipeline_view(workflow, tasks, include_task_details=False)
        assert "tasks" not in view["pipeline"]["stages"][0]

    def test_without_affinity(self):
        workflow = _make_workflow()
        tasks = [_make_task(task_id="t1")]
        view = build_pipeline_view(workflow, tasks, include_affinity=False)
        assert "affinity" not in view

    def test_affinity_included_by_default(self):
        workflow = _make_workflow(task_ids=["t1"])
        tasks = [_make_task(task_id="t1")]
        view = build_pipeline_view(workflow, tasks)
        assert "affinity" in view

    def test_legend_has_all_sections(self):
        workflow = _make_workflow()
        view = build_pipeline_view(workflow, [])
        legend = view["legend"]
        assert "stage_statuses" in legend
        assert "task_statuses" in legend
        assert "agent_types" in legend
        assert "affinity_reasons" in legend

    def test_tasks_filtered_by_workflow_id(self):
        """Only tasks matching the workflow_id should be included."""
        workflow = _make_workflow(
            workflow_id="wf-1",
            task_ids=["t1"],
            stages=[{"name": "s1", "task_ids": ["t1"]}],
        )
        tasks = [
            _make_task(task_id="t1", workflow_id="wf-1", status=TaskStatus.COMPLETED),
            _make_task(task_id="t2", workflow_id="wf-other", status=TaskStatus.COMPLETED),
        ]
        view = build_pipeline_view(workflow, tasks)
        assert view["progress"]["total_tasks"] == 1

    def test_inferred_stages_when_no_explicit(self):
        """Pipeline view falls back to inferred stages."""
        workflow = _make_workflow(
            current_stage="impl",
            task_ids=["t1", "t2"],
            stages=[],  # no explicit stages
        )
        tasks = [
            _make_task(task_id="t1", created_at=1000.0, status=TaskStatus.COMPLETED),
            _make_task(task_id="t2", created_at=1005.0, status=TaskStatus.IN_PROGRESS),
        ]
        view = build_pipeline_view(workflow, tasks)
        # Should infer one stage since tasks are close together
        assert len(view["pipeline"]["stages"]) == 1

    def test_completed_workflow(self):
        workflow = _make_workflow(
            status="completed",
            current_stage="qa",
            completed_at=2000.0,
            stages=[
                {"name": "impl", "task_ids": ["t1"], "status": "completed"},
                {"name": "qa", "task_ids": ["t2"], "status": "completed"},
            ],
            task_ids=["t1", "t2"],
        )
        tasks = [
            _make_task(task_id="t1", status=TaskStatus.COMPLETED),
            _make_task(task_id="t2", status=TaskStatus.COMPLETED),
        ]
        view = build_pipeline_view(workflow, tasks)
        assert view["workflow"]["status"] == "completed"
        assert view["workflow"]["completed_at"] == 2000.0
        assert view["progress"]["overall_progress"] == 1.0

    def test_failed_workflow(self):
        workflow = _make_workflow(
            status="failed",
            current_stage="impl",
            stages=[{"name": "impl", "task_ids": ["t1"], "status": "failed"}],
            task_ids=["t1"],
        )
        tasks = [_make_task(task_id="t1", status=TaskStatus.FAILED)]
        view = build_pipeline_view(workflow, tasks)
        assert view["workflow"]["status"] == "failed"
        assert view["progress"]["failed_tasks"] == 1

    def test_with_agent_enrichment(self):
        workflow = _make_workflow(
            task_ids=["t1"],
            stages=[{"name": "s1", "task_ids": ["t1"]}],
        )
        tasks = [
            _make_task(
                task_id="t1",
                assigned_agent_id="claude-1",
                status=TaskStatus.IN_PROGRESS,
            ),
        ]
        agents = [{"id": "claude-1", "name": "Worker 1", "state": "BUSY"}]
        view = build_pipeline_view(workflow, tasks, agents=agents)
        assert view["agents"]["claude-1"]["name"] == "Worker 1"


# ---------------------------------------------------------------------------
# Legend tests
# ---------------------------------------------------------------------------


class TestPipelineLegend:
    """Tests for _build_pipeline_legend."""

    def test_legend_structure(self):
        workflow = _make_workflow()
        view = build_pipeline_view(workflow, [])
        legend = view["legend"]

        # Stage statuses
        for status in ("completed", "active", "pending", "failed", "paused"):
            assert status in legend["stage_statuses"]
            assert "symbol" in legend["stage_statuses"][status]
            assert "colors" in legend["stage_statuses"][status]
            assert "label" in legend["stage_statuses"][status]

        # Task statuses
        for category in ("pending", "active", "completed", "failed", "paused", "blocked"):
            assert category in legend["task_statuses"]
            assert "colors" in legend["task_statuses"][category]

        # Agent types
        for atype in ("coding", "code-review", "qa", "research", "default"):
            assert atype in legend["agent_types"]

        # Affinity reasons
        for reason in ("context", "workspace", "type"):
            assert reason in legend["affinity_reasons"]
            assert "symbol" in legend["affinity_reasons"][reason]


# ---------------------------------------------------------------------------
# Edge cases and robustness
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and robustness tests."""

    def test_task_with_string_status(self):
        """Handle tasks where status might be a string instead of enum."""
        task = _make_task(status=TaskStatus.COMPLETED)
        card = build_task_card(task)
        assert card["status"] == "COMPLETED"

    def test_large_workflow(self):
        """Pipeline view handles many stages and tasks."""
        stages_data = [
            {"name": f"stage-{i}", "task_ids": [f"t-{i}"], "status": "completed"} for i in range(20)
        ]
        tasks = [
            _make_task(task_id=f"t-{i}", status=TaskStatus.COMPLETED, created_at=1000.0 + i)
            for i in range(20)
        ]
        workflow = _make_workflow(
            task_ids=[f"t-{i}" for i in range(20)],
            stages=stages_data,
        )
        view = build_pipeline_view(workflow, tasks)
        assert len(view["pipeline"]["stages"]) == 20
        assert len(view["pipeline"]["connections"]) == 19
        assert view["progress"]["overall_progress"] == 1.0

    def test_workflow_with_paused_tasks(self):
        workflow = _make_workflow(
            stages=[{"name": "s1", "task_ids": ["t1"]}],
            task_ids=["t1"],
        )
        tasks = [_make_task(task_id="t1", status=TaskStatus.PAUSED)]
        view = build_pipeline_view(workflow, tasks)
        assert view["progress"]["paused_tasks"] == 1
        stages = view["pipeline"]["stages"]
        assert stages[0]["status"] == "paused"

    def test_workflow_with_blocked_tasks(self):
        workflow = _make_workflow(
            stages=[{"name": "s1", "task_ids": ["t1"]}],
            task_ids=["t1"],
        )
        tasks = [_make_task(task_id="t1", status=TaskStatus.BLOCKED)]
        view = build_pipeline_view(workflow, tasks)
        assert view["progress"]["blocked_tasks"] == 1

    def test_no_affinity_in_empty_workflow(self):
        """Affinity overlay not included when workflow has no tasks."""
        workflow = _make_workflow()
        view = build_pipeline_view(workflow, [])
        assert "affinity" not in view

    def test_json_serializable(self):
        """The entire pipeline view should be JSON-serializable."""
        import json

        workflow = _make_workflow(
            current_stage="review",
            task_ids=["t1", "t2"],
            stages=[
                {"name": "impl", "task_ids": ["t1"], "status": "completed"},
                {"name": "review", "task_ids": ["t2"], "status": "active"},
            ],
            agent_affinity={"t2": "claude-1"},
        )
        tasks = [
            _make_task(
                task_id="t1",
                status=TaskStatus.COMPLETED,
                assigned_agent_id="claude-1",
                agent_type="coding",
                task_type=TaskType.FEATURE,
                workspace_mode=WorkspaceMode.BRANCH_ISOLATED,
            ),
            _make_task(
                task_id="t2",
                status=TaskStatus.IN_PROGRESS,
                assigned_agent_id="claude-2",
                agent_type="code-review",
                affinity_agent_id="claude-1",
                affinity_reason="context",
            ),
        ]
        view = build_pipeline_view(workflow, tasks)

        # Should not raise
        serialized = json.dumps(view)
        assert isinstance(serialized, str)

        # Round-trip
        deserialized = json.loads(serialized)
        assert deserialized["workflow"]["workflow_id"] == "wf-1"
