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


def task(task_id, *, project="one", status=TaskStatus.READY, age=3600, profile="worker"):
    return SimpleNamespace(
        id=task_id, project_id=project, status=status, updated_at=NOW-age,
        profile_id=profile, title=task_id, task_type=None,
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


class DB:
    def __init__(self, tasks=(), completions=None, sessions=(), repos=(), providers=(), profiles=()):
        self.tasks = list(tasks)
        self.completions = completions or {}
        self.sessions = list(sessions)
        self.repos = list(repos)
        self.providers = list(providers)
        self.profiles = list(profiles)

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

    async def list_provider_availability(self):
        return self.providers

    async def list_profiles(self):
        return self.profiles


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
        import_module("src.doctor.integration_checks"), "_find_stranded_dependents", candidates
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
    monkeypatch.setattr(module, "_validation_findings", validation)
    assert module.stall_checks()[0].fix is None
    result = await module._check_sweep(context)
    assert result.severity is Severity.WARN
    assert {f["project_id"] for f in result.data["findings"]} == {"one", "two"}
    assert result.data["vault_root"] == context.config.vault_root
    assert "unclaimed_work one: one-ready" in result.detail
    assert "unclaimed_work two: two-ready" in result.detail
