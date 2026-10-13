"""Subject schedule/hold diagnostics and the retained App trust check."""
from __future__ import annotations
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from src.doctor import default_registry
from src.doctor.models import DoctorContext, Severity
from src.models import Project
from tests.test_integration_gitops import git_history_template as git_history_template
from tests.test_promotion_steps import promotion as promotion, setup as setup
# integration.trust (App-mode integration train spec §9.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("git_first,reconciler,mode,broken", [
    ("active", False, "development", False),
    ("active", False, "hierarchy", False),
    ("active", False, "train", False),
    ("shadow", True, "development", False),
    ("shadow", False, "development", True),
    ("shadow", False, "hierarchy", True),
    ("shadow", False, "train", True),
    ("shadow", False, "disabled", False),
    ("shadow", False, "observe", False),
])
async def test_delivery_engine_matches_startup_selection(git_first, reconciler, mode, broken):
    from src.config import AppConfig
    from src.doctor.integration_checks import _BY_ID

    config = AppConfig()
    config.integration.git_first = git_first
    config.integration.reconciler_active = reconciler
    project = _app_project("p", mode, repository_id="r")
    db = SimpleNamespace(
        list_projects=AsyncMock(return_value=[project]),
        get_repo=AsyncMock(return_value=SimpleNamespace(id="r", project_id="p")),
    )
    check = _BY_ID["integration.delivery_engine"]
    result = await check.run(DoctorContext(config=config, db=db))
    assert result.severity == (Severity.ERROR if broken else Severity.OK)
    if broken:
        assert result.data["projects"][0]["mode"] == mode
        assert "no delivery engine for this project" in result.detail
        assert "delivery visits are paused" in result.detail
    if mode in {"disabled", "observe"} or broken:
        db.get_repo.assert_not_awaited()


@pytest.mark.parametrize("repository_id,repository,reason", [
    (None, None, "repository is missing"),
    ("gone", None, "repository is missing"),
    ("wrong", SimpleNamespace(project_id="other"), "belongs to another project"),
])
async def test_delivery_engine_names_repository_configuration(repository_id, repository, reason):
    from src.config import AppConfig
    from src.doctor.integration_checks import _BY_ID

    db = SimpleNamespace(
        list_projects=AsyncMock(return_value=[_app_project("p", "development",
                                                         repository_id=repository_id)]),
        get_repo=AsyncMock(return_value=repository),
    )
    result = await _BY_ID["integration.delivery_engine"].run(
        DoctorContext(config=AppConfig(), db=db),
    )
    assert result.severity == Severity.ERROR
    assert reason in result.detail
    assert result.fixable is False


@pytest.mark.parametrize("runtime,expected", [
    ("train", {"development", "hierarchy", "train"}),
    ("development", {"development"}),
    ("root", {"hierarchy", "train"}),
    ("none", set()),
])
async def test_delivery_engine_uses_selected_service_and_reports_stops(runtime, expected):
    from src.config import AppConfig
    from src.integration.delivery_engine import engine_refusal
    from src.integration.service import IntegrationService

    config = AppConfig()
    service = IntegrationService(
        None, None, train=object() if runtime == "train" else None,
        development_subject_runtime=object() if runtime == "development" else None,
        subject_runtime=object() if runtime == "root" else None,
        parent_subject_runtime=object() if runtime == "root" else None,
    )
    orch = SimpleNamespace(integration_service=service)
    assert service.delivery_modes() == expected
    for mode in ("development", "hierarchy", "train"):
        assert (engine_refusal(mode, config, orchestrator=orch) is None) == (mode in expected)
    # A stopped service overrides even an active-train configuration.
    service._stop_event.set()
    config.integration.git_first = "active"
    assert service.delivery_modes() == set()
    assert "no engine" in engine_refusal("development", config, orchestrator=orch)


async def test_delivery_engine_refuses_mode_activation_before_writing():
    from unittest.mock import patch

    from src.commands.project_commands import ProjectCommandsMixin
    from src.config import AppConfig
    from src.integration.records import PolicyActivation

    config = AppConfig()
    config.integration.reconciler_active = False
    db = SimpleNamespace(get_project=AsyncMock(return_value=_app_project("p", "disabled")))
    handler = SimpleNamespace(db=db, config=config, orchestrator=SimpleNamespace())
    args = {"project_id": "p", "hierarchical_integration_mode": "development",
            "expected_integration_generation": 0}
    with patch.object(PolicyActivation, "configure", new_callable=AsyncMock) as configure:
        refused = await ProjectCommandsMixin._cmd_edit_project(handler, args)
        assert refused["success"] is False
        assert refused["error_code"] == "delivery_engine_unavailable"
        assert "no delivery engine for this project" in refused["error"]
        configure.assert_not_awaited()
        await ProjectCommandsMixin._cmd_edit_project(
            handler, {**args, "hierarchical_integration_mode": "disabled"},
        )
        configure.assert_awaited_once()


async def test_delivery_engine_allows_development_mode_on_running_train():
    from unittest.mock import patch

    from src.commands.project_commands import ProjectCommandsMixin
    from src.config import AppConfig
    from src.integration.records import PolicyActivation
    from src.integration.service import IntegrationService

    config = AppConfig()
    config.integration.git_first = "active"
    db = SimpleNamespace(get_project=AsyncMock(return_value=_app_project("p", "disabled")))
    handler = SimpleNamespace(db=db, config=config, orchestrator=SimpleNamespace(
        integration_service=IntegrationService(None, None, train=object()),
    ))
    with patch.object(PolicyActivation, "configure", new_callable=AsyncMock) as configure:
        await ProjectCommandsMixin._cmd_edit_project(handler, {
            "project_id": "p", "hierarchical_integration_mode": "development",
            "expected_integration_generation": 0,
        })
        configure.assert_awaited_once()


async def test_delivery_engine_check_is_registered_and_needs_database():
    from src.doctor.integration_checks import _BY_ID

    assert "integration.delivery_engine" in {c.id for c in default_registry().checks()}
    result = await _BY_ID["integration.delivery_engine"].run(DoctorContext(config=None))
    assert result.severity == Severity.INFO


def _item(item_id, status="ok", *codes, fix=None):
    return {"id": item_id, "status": status, "codes": list(codes), "fix": fix}


def _verified(*items):
    return {
        "success": True,
        "outcome": "verified",
        "ready": not any(item["status"] == "fail" for item in items),
        "items": list(items),
    }


def _app_mode_ctx(projects, responses, *, app=True, repos=None):
    config = SimpleNamespace(integration=SimpleNamespace(github_app=object() if app else None))
    repos = repos or {}

    async def get_repo(repository_id):
        return repos.get(repository_id)

    db = SimpleNamespace(list_projects=AsyncMock(return_value=projects), get_repo=get_repo)

    async def execute(command, args):
        assert command == "integration_app_verify"
        return responses[args["project_id"]]

    handler = SimpleNamespace(execute=AsyncMock(side_effect=execute))
    return DoctorContext(config=config, db=db, handler=handler), handler


def _app_project(project_id, mode, url="https://github.com/o/{}.git", repository_id=None):
    return Project(
        id=project_id,
        name=project_id,
        repo_url=url.format(project_id),
        hierarchical_integration_mode=mode,
        integration_repository_id=repository_id,
    )


async def _run_app_mode(ctx):
    from src.doctor.integration_checks import _BY_ID

    return await _BY_ID["integration.trust"].run(ctx)


@pytest.mark.asyncio
async def test_app_mode_check_is_info_without_an_app():
    ctx, handler = _app_mode_ctx([_app_project("p", "train")], {}, app=False)

    result = await _run_app_mode(ctx)

    assert result.severity == Severity.INFO
    handler.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_app_mode_check_reports_fail_items_as_error_and_warn_items_as_warn():
    projects = [
        _app_project("train", "train"),
        _app_project("off", "disabled"),
        _app_project("local", "development", url="/srv/remotes/{}.git"),
    ]
    responses = {
        "train": _verified(
            _item("credential"),
            _item("variables", "fail", "hosted_workflow_variables_unavailable", fix="app-setup"),
            _item("protection", "warn", "main_protection_app_bypass"),
        )
    }
    ctx, handler = _app_mode_ctx(projects, responses)

    result = await _run_app_mode(ctx)

    assert result.severity == Severity.ERROR
    assert [call.args[1] for call in handler.execute.await_args_list] == [{"project_id": "train"}]
    assert [(f["item"], f["status"], f["codes"]) for f in result.data["findings"]] == [
        ("variables", "fail", ["hosted_workflow_variables_unavailable"]),
        ("protection", "warn", ["main_protection_app_bypass"]),
    ]
    assert "train: variables fail (hosted_workflow_variables_unavailable)" in result.detail


@pytest.mark.asyncio
async def test_app_mode_check_warns_when_only_warn_items_remain():
    responses = {"p": _verified(_item("audit_workflow", "warn", "audit_workflow_missing"))}
    ctx, _handler = _app_mode_ctx([_app_project("p", "observe")], responses)

    result = await _run_app_mode(ctx)

    assert result.severity == Severity.WARN


@pytest.mark.asyncio
async def test_app_mode_check_judges_development_on_what_the_publisher_needs():
    responses = {
        "dev": _verified(
            _item("credential"),
            _item("variables", "fail", "hosted_workflow_variables_unavailable"),
            _item("protection"),
        ),
        "blocked": _verified(
            _item("protection", "fail", "main_protection_blocks_development_publisher"),
        ),
        "fresh": {"success": False, "outcome": "policy_missing", "error": "no policy"},
    }
    projects = [_app_project(name, "development") for name in responses]
    ctx, _handler = _app_mode_ctx(projects, responses)

    result = await _run_app_mode(ctx)

    assert result.severity == Severity.ERROR
    assert [(f["project_id"], f["item"]) for f in result.data["findings"]] == [
        ("blocked", "protection")
    ]
    assert [entry["project_id"] for entry in result.data["unverified"]] == ["fresh"]
    dev = next(report for report in result.data["projects"] if report["project_id"] == "dev")
    assert {item["id"]: item["judged"] for item in dev["items"]} == {
        "credential": True,
        "variables": False,
        "protection": True,
    }


@pytest.mark.asyncio
async def test_app_mode_check_is_an_error_when_the_command_refuses_an_enabled_project():
    responses = {
        "p": {"success": False, "outcome": "repository_binding_failed", "error": "no binding"}
    }
    ctx, _handler = _app_mode_ctx([_app_project("p", "hierarchy")], responses)

    result = await _run_app_mode(ctx)

    assert result.severity == Severity.ERROR
    assert result.data["findings"][0]["codes"] == ["repository_binding_failed"]


@pytest.mark.asyncio
async def test_app_mode_check_reads_the_designated_repository_url():
    """A project whose designated repository is on GitHub counts, whatever its record URL."""
    from src.models import RepoConfig, RepoSourceType

    repos = {
        "r": RepoConfig(
            id="r", project_id="p", source_type=RepoSourceType.CLONE,
            url="https://github.com/o/r.git",
        )
    }
    project = _app_project("p", "train", url="/srv/{}", repository_id="r")
    ctx, handler = _app_mode_ctx([project], {"p": _verified(_item("credential"))}, repos=repos)

    result = await _run_app_mode(ctx)

    assert result.severity == Severity.OK
    handler.execute.assert_awaited_once()


def test_app_mode_check_is_registered():
    assert "integration.trust" in {check.id for check in default_registry().checks()}


# ---------------------------------------------------------------------------
# integration.ci_source (policy-selected CI source)
# ---------------------------------------------------------------------------


async def test_ci_source_check_names_local_runners_and_invalid_blocks(reuse_database):
    from sqlalchemy import update

    from src.database.tables import projects
    from src.doctor.integration_checks import _BY_ID
    from tests.test_integration_service import _minimal_policy_values

    check = _BY_ID["integration.ci_source"]
    assert "integration.ci_source" in {c.id for c in default_registry().checks()}
    assert (await check.run(DoctorContext(config=SimpleNamespace()))).severity == Severity.INFO
    db = await reuse_database("doctor-ci-source.db")
    for project_id in ("hosted", "local", "broken"):
        await db.create_project(Project(id=project_id, name=project_id))
    ctx = DoctorContext(config=SimpleNamespace(), db=db)
    result = await check.run(ctx)
    assert result.severity == Severity.OK
    assert result.detail == "every project reads hosted checks"

    commands = {"unit": "aq test tests/test_x.py"}
    stored = {
        "hosted": _minimal_policy_values(),
        "local": {**_minimal_policy_values(),
                  "ci": {"source": "local", "promotion": "hosted", "commands": commands}},
        "broken": {**_minimal_policy_values(), "ci": {"source": "local"}},
    }
    async with db._engine.begin() as conn:
        for project_id, policy in stored.items():
            await conn.execute(update(projects).where(projects.c.id == project_id)
                               .values(hierarchical_integration_policy=policy))
    result = await check.run(ctx)
    assert result.severity == Severity.ERROR
    assert result.data["invalid"][0]["project_id"] == "broken"
    assert "broken:" in result.detail and "requires ci.commands" in result.detail
    assert "Rollback hazard" in result.detail and "unknown policy field" in result.detail
    assert result.data["rollback_projects"] == ["broken", "local"]
    assert result.data["projects"] == [{
        "project_id": "local", "root": "local", "epic": "local", "promotion": "hosted",
        "origin": "policy"}]

    del stored["broken"]
    async with db._engine.begin() as conn:
        await conn.execute(update(projects).where(projects.c.id == "broken")
                           .values(hierarchical_integration_policy=None))
    result = await check.run(ctx)
    assert result.severity == Severity.OK
    assert result.detail.startswith("1 project(s) run required checks locally")
    assert "Before downgrading" in result.detail
    assert result.data["rollback_projects"] == ["local"]


# ---------------------------------------------------------------------------
# integration.promotion_stranded (an open intent whose task StepAdmission refuses)
# ---------------------------------------------------------------------------


async def test_promotion_stranded_names_the_repair_for_a_refused_task(promotion):
    from sqlalchemy import insert, update

    from src.database.tables import agent_profiles, tasks
    from src.doctor.integration_checks import _BY_ID

    check = _BY_ID["integration.promotion_stranded"]
    assert check.id in {c.id for c in default_registry().checks()}
    assert (await check.run(DoctorContext(config=SimpleNamespace()))).severity == Severity.INFO
    ctx = DoctorContext(config=SimpleNamespace(), db=promotion.db)
    result = await check.run(ctx)
    assert (result.severity, result.data) == (Severity.OK, {"stranded": []})

    async with promotion.db._engine.begin() as conn:
        await conn.execute(insert(agent_profiles).values(
            id="worker", name="Worker", description="", model="test", created_at=1, updated_at=1))
        await conn.execute(update(tasks).values(
            status="READY", profile_id="worker", route_source="override"))
    result = await check.run(ctx)
    assert result.severity == Severity.ERROR
    assert result.data["stranded"] == [{
        "batch_id": promotion.batch.id, "project_id": "p",
        "request_id": "promotion:r:release:1.2.3", "task_id": "promotion-task",
        "reason": "task promotion-task is a READY promotion task, "
                  "not an IN_PROGRESS or COMPLETED promotion"}]
    assert "aq promote repair promotion:r:release:1.2.3 --project p" in result.detail

    async with promotion.db._engine.begin() as conn:
        await conn.execute(update(tasks).values(status="COMPLETED"))
    result = await check.run(ctx)
    assert result.data["stranded"][0]["reason"] == (
        "promotion task promotion-task is routed to profile 'worker'")
    await promotion.store.set_intent(promotion.batch.id, "aborted")
    assert (await check.run(ctx)).severity == Severity.OK
