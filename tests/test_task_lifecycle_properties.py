"""Bounded lifecycle histories against PostgreSQL and the real command paths.

CI defaults to a fixed, derandomized profile. For longer local exploration:
AQ_HYPOTHESIS_PROFILE=deep aq test tests/test_task_lifecycle_properties.py

Function-scoped database fixtures are intentionally shared across examples:
lifecycle/recovery examples create fresh identities; close examples reopen the
task into a fresh incarnation and compare append-only evidence. Only external
Git/provider work is mocked, as in the focused suites whose fixtures we reuse.
"""

from __future__ import annotations

import itertools
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from src.database.queries.result_queries import PENDING_COMPLETION_KEY
from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
from src.git.manager import GitManager, RemoteRefState
from src.models import Task, TaskStatus
from src.state_machine import is_valid_status_transition
from tests import test_ci_completion as ci_tests
from tests import test_claim_commands as claim_tests
from tests import test_supervisor_recovery as recovery_tests

db = claim_tests.db
config = claim_tests.config
handler = claim_tests.handler
env = recovery_tests.env
setup = ci_tests.setup
PROJECT_ID = claim_tests.PROJECT_ID
pool_session = claim_tests.pool_session
scoped = claim_tests.scoped
HEAD, REQUIRED, run = ci_tests.HEAD, ci_tests.REQUIRED, ci_tests.run

settings.register_profile(
    "lifecycle-ci",
    max_examples=3,
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.register_profile(
    "lifecycle-deep",
    parent=settings.get_profile("lifecycle-ci"),
    max_examples=200,
)
PROFILE = os.environ.get("AQ_HYPOTHESIS_PROFILE", "ci")
PROPERTY_SETTINGS = settings.get_profile(f"lifecycle-{PROFILE}")
IDENTITIES = itertools.count()


@st.composite
def lifecycle_histories(draw):
    """Choose only operations admitted by an independent lifecycle model."""
    status = "READY"
    history = []
    choices = {
        "READY": {"claim": "IN_PROGRESS"},
        "IN_PROGRESS": {
            "stop": "BLOCKED",
            "pause": "PAUSED",
            "recover": "READY",
            "complete": "COMPLETED",
        },
        "BLOCKED": {"restart": "READY", "reopen": "READY"},
        "PAUSED": {"resume": "READY"},
        "COMPLETED": {"restart": "READY", "reopen": "READY"},
    }
    for _ in range(draw(st.integers(min_value=4, max_value=6))):
        operation = draw(st.sampled_from(tuple(choices[status])))
        status = choices[status][operation]
        history.append((operation, TaskStatus(status)))
    return history


@PROPERTY_SETTINGS
@given(history=lifecycle_histories())
@example(
    history=[
        ("claim", TaskStatus.IN_PROGRESS),
        ("stop", TaskStatus.BLOCKED),
        ("restart", TaskStatus.READY),
        ("claim", TaskStatus.IN_PROGRESS),
        ("pause", TaskStatus.PAUSED),
        ("resume", TaskStatus.READY),
        ("claim", TaskStatus.IN_PROGRESS),
        ("recover", TaskStatus.READY),
        ("claim", TaskStatus.IN_PROGRESS),
        ("complete", TaskStatus.COMPLETED),
        ("reopen", TaskStatus.READY),
    ]
)
@pytest.mark.asyncio
async def test_generated_lifecycle_histories(handler, db, tmp_path, history):
    git = create_autospec(GitManager, instance=True)
    git.avalidate_checkout.return_value = False  # No Git files in these pool workspaces.
    git.arev_parse.return_value = HEAD
    git.als_remote_ref.return_value = SimpleNamespace(state=RemoteRefState.PRESENT, oid=HEAD)
    handler.orchestrator.git = git
    handler._current_scope = None
    created = await handler.execute(
        "create_task",
        {
            "project_id": PROJECT_ID,
            "title": f"property-{next(IDENTITIES)}",
        },
    )
    tid = created["created"]
    assert await db.update_task_routing(
        tid,
        profile_id="worker",
        route_source="legacy",
        intelligence_class="standard-medium",
        preferred_workspace_id=None,
    )
    handler.config.swarm.fresh_context_per_task = False
    provider = SimpleNamespace(stop=AsyncMock(), confirm_stopped=AsyncMock(return_value=True))
    registry = handler.orchestrator.session_providers
    sid = None
    for operation, expected in history:
        before = await db.get_task(tid)
        handler._current_scope = None
        if operation == "claim":
            # Administrative rework invalidates the route; emulate the router
            # before the next claim, as in the focused claim fixtures.
            assert await db.update_task_routing(
                tid,
                profile_id="worker",
                route_source="legacy",
                intelligence_class="standard-medium",
                preferred_workspace_id=None,
            )
            identity = str(next(IDENTITIES))
            sid, _ = await pool_session(db, tmp_path, f"s-{identity}", f"a-{identity}")
            result = await scoped(handler, sid)._cmd_task_claim({"task_id": tid})
            assert result["result"] == "claimed", (
                operation,
                expected,
                before.status,
                before.is_blocked,
                before.assigned_agent_id,
                before.intelligence_class,
                before.route_source,
                before.task_type,
                result.get("why"),
                await db.claim_frontier_exclusions(tid),
            )
        elif operation == "recover":
            # The database boundary used after a dead pool process is observed.
            released = await db.release_claim(
                sid,
                task_status=TaskStatus.READY,
                context="recovery",
                now=time.time(),
                expected_task_id=tid,
                expected_claim_epoch=before.claim_epoch,
                stop_after_release=True,
            )
            assert released.released
        elif operation == "complete":
            result = await handler.execute(
                "task_close",
                {
                    "task_id": tid,
                    "session_id": sid,
                    "claim_epoch": before.claim_epoch,
                    "outcome": "pass",
                    "summary": "Generated lifecycle completed",
                    "tests": ["aq test tests/test_task_lifecycle_properties.py"],
                },
            )
            assert result["success"], result
        else:
            command = {
                "stop": "stop_task",
                "pause": "pause_task",
                "resume": "resume_task",
                "restart": "restart_task",
                "reopen": "reopen_with_feedback",
            }[operation]
            handler.orchestrator.session_providers = SimpleNamespace(create=lambda *_: provider)
            try:
                result = await handler.execute(
                    command,
                    {
                        "task_id": tid,
                        "feedback": "Generated rework request",
                    },
                )
            finally:
                handler.orchestrator.session_providers = registry
            assert "error" not in result, result
        after = await db.get_task(tid)
        assert after.status == expected, (history, operation, after)
        assert is_valid_status_transition(before.status, after.status), operation
        if sid:
            session = await db.get_session(sid)
            if after.status == TaskStatus.IN_PROGRESS:
                assert session.task_id == tid and session.claim_phase == "active"
            if session.task_id == tid and session.claim_phase in {"preparing", "active"}:
                assert after.assigned_agent_id == session.agent_id
                agent = await db.get_agent(session.agent_id)
                assert agent.current_task_id == tid
                assert session.last_claim_epoch == after.claim_epoch
        if after.status == TaskStatus.COMPLETED:
            completion = await db.get_task_completion(tid)
            assert completion is not None and completion.outcome == "pass"
            assert completion.summary == "Generated lifecycle completed"
            assert completion.tests == ["aq test tests/test_task_lifecycle_properties.py"]


class RecoveryHoldPersists(AssertionError):
    """The confirmed swift-impact-20 failure, distinct from fixture failures."""


@pytest.mark.xfail(
    strict=True,
    raises=RecoveryHoldPersists,
    reason="swift-impact-20: new attempts retain supervisor recovery holds",
)
@pytest.mark.parametrize("operation", ["restart_task", "reopen_with_feedback"])
@PROPERTY_SETTINGS
@given(rounds=st.integers(min_value=1, max_value=4))
@example(rounds=1)  # Minimized real failure; avoids repeatedly shrinking a known bug.
@pytest.mark.asyncio
async def test_generated_new_attempts_supersede_recovery_holds(env, operation, rounds):
    for _ in range(rounds):
        tid = f"failed-{next(IDENTITIES)}"
        await env.db.create_task(
            Task(
                id=tid,
                project_id="p",
                title=tid,
                description="Generated failed task",
                status=TaskStatus.FAILED,
                profile_id="worker",
                route_source="legacy",
            )
        )
        notification = await env.handler.execute(
            "task_recovery_notify",
            {
                "task_id": tid,
                "project_id": "p",
            },
        )
        assert "error" not in notification, notification
        incident = await env.db.get_task_meta(tid, "supervisor_recovery_incident")
        held = await env.handler.execute(
            "task_recover",
            {
                "task_id": tid,
                "incident_id": incident["id"],
                "decision": "hold",
                "reason": "Generated diagnosis pending",
            },
        )
        assert "error" not in held, held
        result = await env.handler.execute(
            operation,
            {
                "task_id": tid,
                "feedback": "Cause diagnosed; start a fresh attempt",
            },
        )
        assert "error" not in result, result
        task = await env.db.get_task(tid)
        assert task.status == TaskStatus.READY
        assert is_valid_status_transition(TaskStatus.FAILED, task.status)
        current = await env.db.get_task_meta(tid, "supervisor_recovery_incident")
        if current is not None and current["decision"] == "hold":
            raise RecoveryHoldPersists((operation, current))
        assert "frontier_recovery_hold" not in {
            item["code"] for item in await env.db.claim_frontier_exclusions(tid)
        }


class DaemonRestart(BaseException):
    """Bypass exception cleanup, as process death between committed writes does."""


CI_OBSERVATIONS = st.sampled_from(
    [
        "green",
        "red",
        "pending",
        "cancelled",
        "skipped",
        "wrong_head",
        "wrong_producer",
        "missing",
        "unknown",
    ]
)


@PROPERTY_SETTINGS
@given(
    observations=st.lists(CI_OBSERVATIONS, min_size=1, max_size=4),
    interrupted=st.booleans(),
    recovery_passes=st.integers(min_value=1, max_value=3),
)
@example(
    observations=[
        "red",
        "pending",
        "cancelled",
        "skipped",
        "wrong_head",
        "wrong_producer",
        "missing",
        "unknown",
    ],
    interrupted=True,
    recovery_passes=3,
)
@pytest.mark.asyncio
async def test_generated_close_evidence_and_restart(
    setup, observations, interrupted, recovery_passes
):
    handler, db, git, orch, _ = setup
    # Reopen via the real command so each example starts a fresh incarnation.
    if (await db.get_task("t")).status == TaskStatus.COMPLETED:
        result = await handler.execute(
            "reopen_with_feedback",
            {
                "task_id": "t",
                "feedback": "Generated next incarnation",
            },
        )
        assert "error" not in result, result
        await db.transition_task("t", TaskStatus.IN_PROGRESS, force=True)
    args = {
        "task_id": "t",
        "outcome": "pass",
        "summary": "Verified generated source",
        "tests": ["aq test tests/test_task_lifecycle_properties.py"],
        "commands": ["ruff check tests/test_task_lifecycle_properties.py"],
    }
    observations = [*observations, "green"]  # Every sequence can eventually complete.
    for observation in observations:
        if (await db.get_task("t")).status == TaskStatus.COMPLETED:
            break
        entries = {
            "green": [run(), run("optional", "failure")],
            "red": [run(conclusion="failure")],
            "pending": [run(conclusion=None, status="in_progress")],
            "cancelled": [run(conclusion="cancelled")],
            "skipped": [run(conclusion="skipped")],
            "wrong_head": [run(head="b" * 40)],
            "wrong_producer": [run(producer=999)],
            "missing": [],
            "unknown": None,
        }[observation]
        git.acommit_check_runs.return_value = entries
        previous = await db.get_task_completions("t")
        save = db.save_task_completion

        async def crash(*args, **kwargs):
            raise DaemonRestart()

        if observation == "green" and interrupted:
            db.save_task_completion = crash
            try:
                with pytest.raises(DaemonRestart):
                    await handler.execute("task_close", args)
            finally:
                db.save_task_completion = save
            draft = await db.get_task_meta("t", PENDING_COMPLETION_KEY)
            assert draft is not None
            assert await db.get_task_meta("t", ACCEPTED_CLOSE_KEY) == draft["identity"]
            assert await db.recover_pending_completions() == ["t"]
        else:
            result = await handler.execute("task_close", args)
            if observation != "green":
                assert result["success"] is False and result["code"] == "completion.required_ci"
                assert (await db.get_task("t")).status == TaskStatus.IN_PROGRESS
                assert await db.get_task_completions("t") == previous
                assert await db.get_task_meta("t", PENDING_COMPLETION_KEY) is None
                continue
            assert result["success"], result
        for _ in range(recovery_passes):
            assert await db.recover_pending_completions() == []
        assert (await db.get_task("t")).status == TaskStatus.COMPLETED
        completions = await db.get_task_completions("t")
        assert len(completions) == len(previous) + 1
        accepted = await db.get_task_meta("t", ACCEPTED_CLOSE_KEY)
        completion = next(c for c in completions if c.id == accepted["completion_id"])
        assert completion.summary == args["summary"] and completion.outcome == "pass"
        assert completion.tests == args["tests"] and completion.commands == args["commands"]
        assert completion.commits == [HEAD]
        ci = await db.get_task_meta("t", "completion_ci")
        assert ci["state"] == "green" and ci["head_sha"] == HEAD
        assert ci["required_checks"] == [REQUIRED]
