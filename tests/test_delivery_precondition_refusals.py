"""Delivery-precondition refusals name what failed and who can fix it.

``resolve_workspace_checkpoint`` used to collapse four unrelated
preconditions into one sentence about the workspace, and the development
completion arm handed every one of them back as "issues you can still fix
from this workspace".  A worker whose only missing precondition was daemon
state then either looped or closed ``--outcome fail`` over passing work
(task fleet-willow).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.database.queries.hierarchy_queries import HierarchyError
from src.integration.hierarchy import resolve_workspace_checkpoint
from src.models import (
    Agent,
    AgentOutput,
    AgentResult,
    PipelineContext,
    Task,
    TaskStatus,
)
from src.orchestrator.git_ops import GitOpsMixin

REPO = SimpleNamespace(id="repo")


def _db(workspace):
    return SimpleNamespace(get_workspace_for_task=AsyncMock(return_value=workspace))


def _ws(**kw):
    return SimpleNamespace(
        id=kw.pop("id", "ws-1"),
        locked_by_task_id=kw.pop("locked_by_task_id", "t1"),
        workspace_path=kw.pop("workspace_path", "/slot"),
        **kw,
    )


class TestCheckpointPreconditions:
    async def test_missing_workspace_names_the_task_not_the_branch(self):
        task = {"id": "t1", "repo_id": "repo", "branch_name": "aq/t1"}
        with pytest.raises(HierarchyError) as exc:
            await resolve_workspace_checkpoint(_db(None), SimpleNamespace(), task, REPO)
        assert exc.value.context["precondition"] == "no_integration_workspace"
        assert exc.value.context["fixable_by"] == "operator"
        assert "t1" in exc.value.detail

    async def test_workspace_locked_by_another_task_names_that_task(self):
        task = {"id": "t1", "repo_id": "repo", "branch_name": "aq/t1"}
        db = _db(_ws(locked_by_task_id="other"))
        with pytest.raises(HierarchyError) as exc:
            await resolve_workspace_checkpoint(db, SimpleNamespace(), task, REPO)
        assert exc.value.context["precondition"] == "workspace_not_owned"
        assert exc.value.context["locked_by_task_id"] == "other"
        assert "other" in exc.value.detail

    async def test_repo_mismatch_names_both_repositories(self):
        task = {"id": "t1", "repo_id": "elsewhere", "branch_name": "aq/t1"}
        with pytest.raises(HierarchyError) as exc:
            await resolve_workspace_checkpoint(_db(_ws()), SimpleNamespace(), task, REPO)
        assert exc.value.context["precondition"] == "repo_mismatch"
        assert "elsewhere" in exc.value.detail and "repo" in exc.value.detail

    async def test_unrecorded_branch_is_worker_fixable_and_names_the_command(self):
        task = {"id": "t1", "repo_id": "repo", "branch_name": None}
        with pytest.raises(HierarchyError) as exc:
            await resolve_workspace_checkpoint(_db(_ws()), SimpleNamespace(), task, REPO)
        assert exc.value.context["precondition"] == "branch_not_recorded"
        assert exc.value.context["fixable_by"] == "worker"
        assert exc.value.context["remedy"] == "aq task set t1 --branch <branch>"

    async def test_every_precondition_met_still_returns_the_pushed_head(self):
        from src.git.manager import RemoteRefResult, RemoteRefState

        head = "a" * 40
        git = SimpleNamespace(
            aget_current_branch=AsyncMock(return_value="aq/t1"),
            _arun=AsyncMock(side_effect=["", head]),
            als_remote_ref=AsyncMock(
                return_value=RemoteRefResult(RemoteRefState.PRESENT, oid=head)
            ),
        )
        task = {"id": "t1", "repo_id": "repo", "branch_name": "aq/t1"}
        assert await resolve_workspace_checkpoint(_db(_ws()), git, task, REPO) == head


def _ctx():
    task = Task(id="t1", project_id="p", title="T", description="d", status=TaskStatus.IN_PROGRESS)
    return PipelineContext(
        task=task,
        agent=Agent(id="a", name="a", profile_id="worker"),
        output=AgentOutput(result=AgentResult.COMPLETED),
        workspace_path="/slot",
        workspace_id="ws-1",
        repo=None,
    )


class _Orch(GitOpsMixin):
    """The real mixin over the two collaborators the refusal path touches."""

    def __init__(self):
        self.db = SimpleNamespace(set_task_meta=AsyncMock())
        self.bus = SimpleNamespace(emit=AsyncMock())


def _orch():
    return _Orch()


class TestDevelopmentDeliveryRefusal:
    async def test_operator_precondition_escalates_and_flags_the_task(self):
        ctx = _ctx()
        orch = _orch()
        exc = HierarchyError(
            "dirty",
            "task t1 holds no integration workspace",
            {"precondition": "no_integration_workspace", "fixable_by": "operator"},
        )

        await orch._development_delivery_refusal(ctx, exc)

        assert ctx.verification_retry_in_session is True
        assert ctx.verification_escalated is True
        orch.db.set_task_meta.assert_awaited_once_with(
            "t1", "needs_attention", "delivery_no_integration_workspace"
        )
        assert orch.bus.emit.await_args.args[0] == "task.needs_attention"
        assert "workspace can change" in ctx.verification_feedback
        assert "user:dashboard" in ctx.verification_feedback

    async def test_worker_precondition_appends_the_remedy_without_escalating(self):
        ctx = _ctx()
        orch = _orch()
        exc = HierarchyError(
            "dirty",
            "task t1 has no branch_name recorded",
            {
                "precondition": "branch_not_recorded",
                "fixable_by": "worker",
                "remedy": "aq task set t1 --branch <branch>",
            },
        )

        await orch._development_delivery_refusal(ctx, exc)

        assert ctx.verification_escalated is False
        assert ctx.verification_retry_in_session is True
        orch.db.set_task_meta.assert_not_awaited()
        assert "aq task set t1 --branch <branch>" in ctx.verification_feedback

    async def test_plain_delivery_failure_keeps_the_old_refusal(self):
        ctx = _ctx()
        orch = _orch()

        await orch._development_delivery_refusal(ctx, ValueError("boom"))

        assert ctx.verification_escalated is False
        assert ctx.verification_issues == ["boom"]
        orch.db.set_task_meta.assert_not_awaited()

    async def test_a_failed_flag_write_still_refuses(self):
        ctx = _ctx()
        orch = _orch()
        orch.db.set_task_meta = AsyncMock(side_effect=RuntimeError("db down"))
        exc = HierarchyError(
            "dirty", "nope", {"precondition": "repo_mismatch", "fixable_by": "operator"}
        )

        await orch._development_delivery_refusal(ctx, exc)

        assert ctx.verification_escalated is True
        assert ctx.verification_retry_in_session is True
