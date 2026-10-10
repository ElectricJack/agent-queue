"""Fixture coverage for the operator stall sweep's distinct symptoms."""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace

import pytest

from src.config import AppConfig
from src.doctor.models import DoctorContext, Severity
from src.models import ProjectStatus, TaskStatus

module = import_module("src.doctor.stall_checks")

NOW = 1_800_000_000.0


def task(task_id, *, project="one", status=TaskStatus.READY, age=3600, profile="worker",
         priority=100):
    return SimpleNamespace(
        id=task_id, project_id=project, status=status, updated_at=NOW-age,
        profile_id=profile, title=task_id, task_type=None, priority=priority,
    )


def reason(code, detail="", ref=None):
    return {"code": code, "detail": detail, "ref": ref}


class Handler:
    def __init__(self, reasons):
        self.reasons = reasons
        self.calls = []
        self.in_flight = 0
        self.maximum = 0

    async def execute(self, command, args):
        assert command == "explain_task"
        self.calls.append(args["task_id"])
        self.in_flight += 1
        self.maximum = max(self.maximum, self.in_flight)
        await asyncio.sleep(0.001)
        self.in_flight -= 1
        return {"success": True, "reasons": self.reasons.get(args["task_id"], [])}


class _Rows:
    """The one read a statement-backed probe needs from the fake engine."""

    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def scalars(self):
        return iter(self._rows)


class _Connect:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        return _Rows(self._rows)


class DB:
    def __init__(self, tasks=(), completions=None, sessions=(), repos=(), providers=(), profiles=(),
                 unowned=0):
        self.tasks = list(tasks)
        self.completions = completions or {}
        self.sessions = list(sessions)
        self.repos = list(repos)
        self.providers = list(providers)
        self.profiles = list(profiles)
        self.unowned = unowned
        self.rows: list = []
        self._engine = SimpleNamespace(connect=lambda: _Connect(self.rows))

    async def list_projects(self, status=None):
        assert status is ProjectStatus.ACTIVE
        return [SimpleNamespace(id="one"), SimpleNamespace(id="two")]

    async def list_tasks(self, project_id):
        return [t for t in self.tasks if t.project_id == project_id]

    async def get_task_completion(self, task_id):
        completed_at = self.completions.get(task_id)
        return SimpleNamespace(completed_at=completed_at) if completed_at else None

    async def list_sessions(self):
        return self.sessions

    async def list_repos(self):
        return self.repos

    async def get_project(self, project_id):
        return SimpleNamespace(id=project_id, git_identity_name=None, git_identity_email=None)

    async def list_provider_availability(self):
        return self.providers

    async def list_profiles(self):
        return self.profiles

    async def stalled_conversation_queue(self):
        return {
            "unowned": self.unowned,
            "oldest_at": NOW - 1800 if self.unowned else None,
        }


@pytest.fixture
def context(tmp_path):
    return DoctorContext(config=AppConfig(data_dir=str(tmp_path)), db=DB(), handler=Handler({}))


@pytest.mark.parametrize(
    ("reasons", "completion_age", "expected"),
    [
        ([], None, "unclaimed_work"),
        ([reason("awaiting_pool_session", "waiting for a claim")], None, "unclaimed_work"),
        ([reason("blocked_dependency", "dep 'blocker' status=COMPLETED", "blocker")],
         3600, "undelivered_blocker"),
        ([reason("blocked_dependency", "dep 'blocker' status=COMPLETED", "blocker")],
         600, None),
        ([reason("blocked_dependency", "dep 'blocker' status=IN_PROGRESS", "blocker")],
         None, None),
    ],
)
async def test_stale_work_respects_daemon_explanation(context, reasons, completion_age, expected):
    context.handler = Handler({"child": reasons})
    if completion_age:
        context.db.completions["blocker"] = NOW - completion_age
    result = await module._work_findings(
        context, [task("child")], NOW, module._Explains(context),
    )
    assert [item["kind"] for item in result] == ([expected] if expected else [])


async def test_explain_calls_are_bounded_and_cached(context):
    rows = [task(f"work-{i}") for i in range(20)]
    explains = module._Explains(context)
    await module._work_findings(context, rows, NOW, explains)
    await asyncio.gather(explains.get("work-0"), explains.get("work-0"))
    assert context.handler.maximum > 1
    assert context.handler.maximum <= 8
    assert context.handler.calls.count("work-0") == 1


@pytest.mark.parametrize("unowned", [0, 2])
async def test_untracked_sessions_are_an_error_even_without_active_projects(
    context, monkeypatch, unowned,
):
    from unittest.mock import AsyncMock
    from src.doctor.models import CheckResult

    monkeypatch.setattr(context.db, "list_projects", AsyncMock(return_value=[]))
    context.db.unowned = unowned
    sessions = import_module("src.doctor.session_checks")
    monkeypatch.setattr(sessions, "_check_flock", AsyncMock(return_value=CheckResult(
        "sessions.untracked", Severity.ERROR, "hidden", data={"findings": [{
            "kind": "provider_untracked", "detail": "live n-supervisor--global is hidden",
        }]},
    )))
    result = await module._check_sweep(context)
    assert result.severity == Severity.ERROR
    assert result.data["findings"][0]["kind"] == "flock_untracked"
    assert "n-supervisor--global" in result.detail
    assert {finding["kind"] for finding in result.data["findings"]} == (
        {"flock_untracked", "conversation_unowned"} if unowned else {"flock_untracked"}
    )


async def test_restore_branch_requires_explain_to_name_that_blocker(context, monkeypatch):
    async def candidates(_ctx, *, candidate_statuses):
        assert set(candidate_statuses) == {
            TaskStatus.DEFINED, TaskStatus.READY, TaskStatus.COMPLETED,
        }
        return [
            {"project_id": "one", "dependent_task_id": "claimed", "blocker_task_id": "done",
             "source_sha": "abcdef", "delivery_id": "d1"},
            {"project_id": "two", "dependent_task_id": "unclaimed", "blocker_task_id": "done",
             "source_sha": "abcdef", "delivery_id": "d1"},
        ]

    monkeypatch.setattr(
        import_module("src.doctor.stall_checks"), "_find_stranded_dependents", candidates
    )
    context.handler = Handler({
        "claimed": [reason("blocked_dependency", "status=COMPLETED", "done")],
        "unclaimed": [reason("awaiting_pool_session", "nobody has claimed it")],
    })
    result = await module._branch_findings(context, {"one", "two"}, module._Explains(context))
    assert [(item["kind"], item["dependent_task_id"]) for item in result] == [
        ("restore_branch", "claimed")
    ]


def session(name, *, lifecycle="pool", project="one", task_id=None, started=NOW-3600):
    return SimpleNamespace(
        name=name, lifecycle=lifecycle, project_id=project, task_id=task_id,
        state="running", profile_id="worker", started_at=started, last_activity=NOW-3600,
    )


async def test_idle_pool_worker_and_crash_loop(context, monkeypatch):
    context.db.sessions = [
        session("pool"), session("worker", lifecycle="task", task_id="held"),
        *(session("s-loop", started=NOW-100-i) for i in range(5)),
    ]

    async def pane(*args, **kwargs):
        return 0, "usage limit reached\n❯\n"

    monkeypatch.setattr(module, "_command", pane)
    findings = await module._session_findings(context, {"one"}, [task("ready")], NOW)
    assert {item["kind"] for item in findings} == {"idle_pool", "idle_worker", "crash_loop"}
    assert "usage limit" in next(f["detail"] for f in findings if f["kind"] == "idle_pool")


async def test_idle_pool_requires_prompt_or_limit(context, monkeypatch):
    context.db.sessions = [session("pool")]

    async def pane(*args, **kwargs):
        return 0, "aq task claim --next --wait 60\n"

    monkeypatch.setattr(module, "_command", pane)
    assert await module._session_findings(context, {"one"}, [task("ready")], NOW) == []


def test_publisher_skip_and_error_logs_use_configured_data_dir(context):
    log_dir = context.config.data_dir + "/logs"
    from pathlib import Path

    Path(log_dir).mkdir()
    Path(log_dir, "agent-queue.log").write_text(
        "development publisher: skipping child because dependency done is unavailable\n"
    )
    Path(context.config.data_dir, "daemon.log").write_text(
        "development publisher tick failed: bad branch\n"
    )
    findings = module._log_findings(context, {"one"}, [task("child")])
    assert {item["kind"] for item in findings} == {"publisher_skip", "publisher_error"}
    assert findings[0]["blocker_id"] == "done"


async def test_delivery_uses_configured_repo_checkout(context, monkeypatch):
    context.db.repos = [SimpleNamespace(
        project_id="two", checkout_base_path="/checkout/two", source_path="",
        default_branch="trunk",
    )]
    seen = []

    async def command(*args, **kwargs):
        seen.append(args)
        return 0, f"{int(NOW-8*3600)} abc123\n"

    monkeypatch.setattr(module, "_command", command)
    findings = await module._delivery_findings(
        context, {"one", "two"}, [task("pending", project="two", status=TaskStatus.DEFINED)], NOW,
    )
    assert findings[0]["kind"] == "delivery_stale"
    assert seen[0][:4] == ("git", "-C", "/checkout/two", "log")
    # AQ deliveries are read from the committer: the project's resolved
    # identity (here the unset install's fallback) and earlier fixed ones.
    assert "--fixed-strings" in seen[0]
    assert "--committer=<agent-queue@localhost>" in seen[0]
    assert "--committer=@agent-queue.local>" in seen[0]


async def test_disabled_provider_with_queued_task(context):
    context.db.providers = [{"provider": "claude", "state": "disabled"}]
    context.db.profiles = [SimpleNamespace(id="worker", harness="claude")]
    findings = await module._provider_findings(context, [task("ready")])
    assert findings[0]["kind"] == "disabled_provider"
    assert findings[0]["provider"] == "claude"


def test_high_cost_non_design_route_across_projects():
    rows = [
        task("implement widget", project="two", profile="deep-high-claude"),
        task("design widget", project="one", profile="deep-high-claude"),
    ]
    findings = module._route_findings(rows)
    assert [(f["kind"], f["project_id"]) for f in findings] == [
        ("non_design_expensive_route", "two")
    ]


async def test_validation_container_not_running(context, monkeypatch):
    async def command(*args, **kwargs):
        return 0, "exited\n"

    monkeypatch.setattr(module, "_command", command)
    findings = await module._validation_findings()
    assert findings[0]["kind"] == "validation_db"
    assert findings[0]["state"] == "exited"


async def test_sweep_is_registered_and_reports_all_active_projects(context, monkeypatch):
    context.db.tasks = [
        task("one-ready", project="one", age=NOW),
        task("two-ready", project="two", age=NOW),
    ]

    async def branches(*args):
        return []

    async def validation():
        return []

    monkeypatch.setattr(module, "_branch_findings", branches)
    monkeypatch.setattr(module, "_unmaterialized_pr_findings", lambda *args: branches())
    monkeypatch.setattr(module, "_unadmitted_parent_findings", lambda *args: branches())
    monkeypatch.setattr(module, "_stranded_child_findings", lambda *args: branches())
    monkeypatch.setattr(module, "_reviewed_file_guard_findings", lambda *args: branches())
    monkeypatch.setattr(module, "_validation_findings", validation)
    assert module.stall_checks()[0].fix is None
    result = await module._check_sweep(context)
    assert result.severity is Severity.WARN
    assert {f["project_id"] for f in result.data["findings"]} == {"one", "two"}
    assert result.data["vault_root"] == context.config.vault_root
    assert "unclaimed_work one: one-ready" in result.detail
    assert "unclaimed_work two: two-ready" in result.detail


async def test_conversation_inputs_with_no_live_supervisor_are_named(context):
    """An input nobody is live to answer must not look like a healthy install."""
    assert await module._conversation_findings(context, NOW) == []
    context.db.unowned = 3
    findings = await module._conversation_findings(context, NOW)
    assert [item["kind"] for item in findings] == ["conversation_unowned"]
    assert findings[0]["unowned"] == 3 and findings[0]["oldest_at"] == NOW - 1800
    assert "30m" in findings[0]["detail"] and "discord.project_id" in findings[0]["detail"]


async def test_the_conversation_line_survives_an_install_with_no_active_project(context, monkeypatch):
    async def nothing(*args, **kwargs):
        raise AssertionError("the full sweep must not run with no active project")

    for name in ("_work_findings", "_branch_findings", "_session_findings", "_orphaned_pr_findings"):
        monkeypatch.setattr(module, name, nothing)

    async def inactive(*args, **kwargs):
        return []

    monkeypatch.setattr(context.db, "list_projects", inactive)
    assert (await module._check_sweep(context)).severity is Severity.OK

    context.db.unowned = 1
    result = await module._check_sweep(context)
    assert result.severity is Severity.WARN
    assert [item["kind"] for item in result.data["findings"]] == ["conversation_unowned"]
    assert "conversation_unowned -" in result.detail


async def test_reviewed_file_guard_stall_names_batch_and_recovery(context, monkeypatch):
    async def blocked(ctx):
        return [
            dict(
                project_id=pid,
                batch_id=f"batch-{pid}",
                invariant="added_reviewed_path_does_not_match_source",
            )
            for pid in ("one", "inactive")
        ]

    monkeypatch.setattr(
        import_module("src.doctor.stall_checks"),
        "_find_reviewed_file_blocked_batches",
        blocked,
    )
    findings = await module._reviewed_file_guard_findings(context, {"one"})
    assert len(findings) == 1
    assert findings[0]["kind"] == "reviewed_file_guard"
    assert "batch-one" in findings[0]["detail"] and "policy gate" in findings[0]["detail"]


async def test_stall_sweep_names_orphan_pr_and_inventory_failures(context, monkeypatch):
    from unittest.mock import AsyncMock

    finder = AsyncMock(return_value=(
        [{"project_id": "one", "pr_url": "https://github.com/o/r/pull/92",
          "branch": "aq/orphan", "age_seconds": 26 * 3600}],
        [{"project_id": "two", "error": "offline"}],
    ))
    monkeypatch.setattr(import_module("src.doctor.stall_checks"), "_find_orphaned_prs", finder)
    findings = await module._orphaned_pr_findings(context, {"one", "two"}, NOW)
    finder.assert_awaited_once_with(context, {"one", "two"}, now=NOW)
    assert [item["kind"] for item in findings] == ["orphaned_pr", "pr_inventory_failed"]
    assert "pull/92" in findings[0]["detail"] and "26h" in findings[0]["detail"]
    assert findings[1]["detail"] == "offline"


async def test_a_stranded_child_line_names_the_missing_path(context):
    """The shape both PR lines miss: completed, pushed, no PR, no delivery."""
    context.db.rows = [
        {"task_id": "container.1", "project_id": "one", "parent_task_id": "container",
         "branch": "aq/container.1", "updated_at": NOW - 22 * 60, "head_sha": "c" * 40,
         "base_sha": "b" * 40, "parent_status": "PAUSED",
         "parent_state": "awaiting_children", "operation_state": None,
         "subject_phase": None},
        {"task_id": "container.2", "project_id": "one", "parent_task_id": "container",
         "branch": "aq/container.2", "updated_at": NOW - 19 * 60, "head_sha": "d" * 40,
         "base_sha": "b" * 40, "parent_status": "COMPLETED",
         "parent_state": None, "operation_state": None, "subject_phase": None},
    ]

    findings = await module._stranded_child_findings(context, NOW)

    assert [item["kind"] for item in findings] == ["stranded_child", "stranded_child"]
    first = findings[0]
    assert first["project_id"] == "one" and first["parent_task_id"] == "container"
    assert "22m" in first["detail"] and "aq/container.1" in first["detail"]
    # Every part of the container's path is named, including the absent ones.
    assert "checkpoint awaiting_children" in first["detail"]
    assert "operation none" in first["detail"] and "Subject none" in first["detail"]
    assert "no collector will carry it" in findings[1]["detail"]


async def test_the_stranded_child_line_is_quiet_on_a_healthy_train(context):
    context.db.rows = []
    assert await module._stranded_child_findings(context, NOW) == []


async def test_the_stranded_child_line_is_quiet_under_the_active_train(context):
    """The git-first train collects children without a parent Subject."""
    context.db.rows = [
        {"task_id": "container.1", "project_id": "one", "parent_task_id": "container",
         "branch": "aq/container.1", "updated_at": NOW - 22 * 60, "head_sha": "c" * 40,
         "base_sha": "b" * 40, "parent_status": "PAUSED",
         "parent_state": "awaiting_children", "operation_state": None,
         "subject_phase": None},
    ]
    context.config.integration.git_first = "active"

    assert await module._stranded_child_findings(context, NOW) == []


def test_unknown_subject_journal_replay_reports_error_after_five_minutes():
    def entry(seq, timestamp, rule="unknown-facts", **fields):
        return dict(subject_id="30b7d7f1", project_id="one",
                    batch_id="integration-batch-66ee241c", seq=seq, recorded_at=timestamp,
                    rule=rule, payload={"facts": {"unknown": ["ancestry_unknown:source"]}},
                    **fields)

    journal = [entry(5, NOW-60), entry(3, NOW-200), entry(1, NOW-400)]
    findings = module._unknown_subject_streaks(journal, NOW)
    assert len(findings) == 1
    assert findings[0]["severity"] == "error"
    assert findings[0]["batch_id"] == "integration-batch-66ee241c"
    assert "ancestry_unknown:source" in findings[0]["detail"]
    assert not module._unknown_subject_streaks(journal[:2], NOW)
    assert not module._unknown_subject_streaks([entry(6, NOW, "ci-current"), *journal], NOW)
    assert not module._unknown_subject_streaks(
        [journal[0], entry(4, NOW-100, "construct"), *journal[1:]], NOW
    )


def test_observation_failure_journal_replay_is_visible():
    rows = [dict(subject_id="s", project_id="one", batch_id=None, seq=1,
                 recorded_at=NOW-301, rule=None, primitive="integration_observe_subject",
                 outcome="unknown", payload={"result": {"reason": "source unavailable"}})]
    assert "source unavailable" in module._unknown_subject_streaks(rows, NOW)[0]["detail"]
