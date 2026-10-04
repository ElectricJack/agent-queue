"""Subject schedule/hold diagnostics and the retained App trust check."""
from __future__ import annotations
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from src.doctor import default_registry
from src.doctor.models import DoctorContext, Severity
from src.models import Project
# integration.trust (App-mode integration train spec §9.6)
# ---------------------------------------------------------------------------


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
