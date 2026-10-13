"""Required CI binds ordinary PR closes and audits to recorded source commits."""

from unittest.mock import AsyncMock, create_autospec

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig
from src.database import Database
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitManager
from src.integration.completion_ci import completion_checks, verify_completion_ci
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskCompletion, TaskStatus, Workspace
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn

HEAD = "a" * 40
OTHER = "b" * 40
URL = "https://github.com/example/widgets.git"
REQUIRED = "Native Windows build and tests"


def run(name=REQUIRED, conclusion="success", *, head=HEAD, producer=15368, job=1,
        status="completed"):
    return {"id": job, "name": name, "head_sha": head, "app": {"id": producer},
            "status": status, "conclusion": conclusion,
            "html_url": f"https://github.com/example/widgets/actions/runs/1/job/{job}"}


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    db = Database(lease_dsn("completion-ci"))
    await db.initialize()
    policy = {"root": {"required_checks": {
        "version": "v1", "names": [REQUIRED], "producer_id": "15368",
    }}}
    await db.create_project(Project(id="p", name="P", repo_url=URL,
                                    hierarchical_integration_policy=policy))
    await db.create_repo(RepoConfig(id="integration-repo", project_id="p", url=URL,
                                   source_type=RepoSourceType.CLONE))
    await db.update_project("p", integration_repository_id="integration-repo")
    await db.create_task(Task(id="t", project_id="p", title="Windows source",
                              description="", status=TaskStatus.IN_PROGRESS, branch_name="aq/t"))
    await db.create_workspace(Workspace(
        id="ws", project_id="p", workspace_path=str(tmp_path), source_type=RepoSourceType.LINK,
        locked_by_task_id="t",
    ))
    config = AppConfig(data_dir=str(tmp_path), database=DatabaseConfig(url=lease_dsn("orch-ci")))
    config.messages.enabled = False
    git = create_autospec(GitManager, instance=True)
    git.bind_github_repository.return_value = GitHubRepositoryBinding(303, "example/widgets")
    git.arev_parse.return_value = HEAD
    git.aremote_branch_head.return_value = HEAD
    git.acommit_check_runs.return_value = [run(), run("Lint", "failure")]
    orch = Orchestrator(config)
    orch.db, orch.git = db, git
    monkeypatch.setattr(orch, "_get_default_branch", AsyncMock(return_value="main"))
    monkeypatch.setattr(orch, "_run_completion_pipeline", AsyncMock(return_value=(None, True)))
    monkeypatch.setattr(orch, "release_session_task_resources", AsyncMock())
    handler = CommandHandler(orch, config)
    yield handler, db, git, orch, config
    await db.close()


async def close(handler, **kwargs):
    return await handler.execute("task_close", {
        "task_id": "t", "outcome": "pass", "summary": "Published Windows source", **kwargs,
    })


@pytest.mark.parametrize("entries,state", [
    ([run(conclusion="failure")], "red"),
    ([run(conclusion=None, status="in_progress")], "pending"),
    ([run(conclusion="cancelled")], "cancelled"),
    ([run(head=OTHER)], "pending"),
    ([run(producer=999)], "pending"),
    ([run(conclusion="skipped")], "pending"),
    ([], "pending"),
    (None, "unknown"),
])
async def test_required_ci_refuses_before_close_side_effects(setup, entries, state):
    handler, db, git, orch, _ = setup
    git.acommit_check_runs.return_value = entries
    await db.add_task_subtasks("t", "p", [{"title": "Evidence"}])
    result = await close(handler, skip_open_subtasks=True)
    assert result["success"] is False
    assert result["code"] == "completion.required_ci"
    assert result["ci"]["state"] == state
    assert (await db.get_task("t")).status is TaskStatus.IN_PROGRESS
    assert (await db.get_workspace("ws")).locked_by_task_id == "t"
    assert await db.get_task_completion("t") is None
    assert await db.get_task_meta("t", "outcome") is None
    assert (await db.list_task_subtasks("t"))[0]["status"] == "pending"
    orch._run_completion_pipeline.assert_not_awaited()
    orch.release_session_task_resources.assert_not_awaited()


async def test_green_required_checks_record_exact_source(setup):
    handler, db, git, _, _ = setup
    result = await close(handler)
    assert result["success"] is True, result
    assert (await db.get_task("t")).status is TaskStatus.COMPLETED
    completion = await db.get_task_completion("t")
    assert completion.commits == [HEAD]
    assert (await db.get_task_meta("t", "completion_ci"))["head_sha"] == HEAD
    assert git.acommit_head_sha.await_count == 0
    assert all(call.args[1] == HEAD for call in git.acommit_check_runs.await_args_list)


async def test_restart_recovers_the_verified_source(setup, monkeypatch):
    handler, db, _, _, _ = setup

    class Restart(BaseException):
        pass

    async def crash(*args, **kwargs):
        raise Restart()

    with monkeypatch.context() as patch:
        patch.setattr(db, "save_task_completion", crash)
        with pytest.raises(Restart):
            await close(handler)
    assert (await db.get_task("t")).status is TaskStatus.COMPLETED
    assert await db.get_task_completion("t") is None
    assert await db.recover_pending_completions() == ["t"]
    assert (await db.get_task_completion("t")).commits == [HEAD]


@pytest.mark.parametrize("kind", ["old_commit", "unpublished", "local_moves", "remote_moves",
                                      "wrong_repository", "invalid_policy"])
async def test_source_identity_failures_keep_task_open(setup, kind):
    handler, db, git, _, _ = setup
    kwargs = {}
    if kind == "old_commit":
        kwargs["commit"] = OTHER
    elif kind == "unpublished":
        git.aremote_branch_head.return_value = OTHER
    elif kind == "local_moves":
        git.arev_parse.side_effect = [HEAD, OTHER]
    elif kind == "remote_moves":
        git.aremote_branch_head.side_effect = [HEAD, OTHER]
    elif kind == "wrong_repository":
        git.bind_github_repository.return_value = GitHubRepositoryBinding(999, "other/repo")
    else:
        await db.update_project("p", hierarchical_integration_policy={"root": {}})
    result = await close(handler, **kwargs)
    assert result["success"] is False
    assert result["ci"]["state"] == "unknown"
    assert (await db.get_task("t")).status is TaskStatus.IN_PROGRESS
    assert await db.get_task_completion("t") is None


@pytest.mark.parametrize("change", ["red", "new_head"])
async def test_pipeline_changes_require_fresh_ci(setup, change):
    handler, db, git, orch, _ = setup

    async def pipeline(ctx):
        if change == "red":
            git.acommit_check_runs.return_value = [run(conclusion="failure")]
        else:
            git.arev_parse.return_value = OTHER
            git.aremote_branch_head.return_value = OTHER
        return None, True

    orch._run_completion_pipeline.side_effect = pipeline
    result = await close(handler)
    assert result["success"] is False, result
    assert result["code"] == "completion.required_ci"
    assert (await db.get_task("t")).status is TaskStatus.IN_PROGRESS
    assert await db.get_task_completion("t") is None
    orch.release_session_task_resources.assert_not_awaited()


async def test_failing_close_remains_available(setup):
    handler, db, git, _, _ = setup
    git.acommit_check_runs.return_value = [run(conclusion="failure")]
    result = await close(handler, outcome="fail", failure_class="hard")
    assert result["success"] is True, result
    assert (await db.get_task("t")).status is TaskStatus.BLOCKED
    git.acommit_check_runs.assert_not_awaited()


@pytest.mark.parametrize("mode", ["train", "hierarchy", "development"])
async def test_managed_publishers_keep_their_completion_owner(setup, mode):
    _, db, git, _, config = setup
    await db.update_project("p", hierarchical_integration_mode=mode)
    result = await verify_completion_ci(db, git, config, await db.get_task("t"),
                                        await db.get_project("p"))
    assert result is None
    git.acommit_check_runs.assert_not_awaited()


async def test_configuration_fallback_and_latest_rerun(setup):
    handler, db, git, _, config = setup
    await db.update_project("p", hierarchical_integration_policy=None)
    task, project = await db.get_task("t"), await db.get_project("p")
    assert await completion_checks(db, config, task, project) is None
    config.integration.merge_required_checks = [REQUIRED]
    git.acommit_check_runs.return_value = [run(conclusion="failure"), run(job=2)]
    assert (await close(handler))["success"] is True


async def test_project_policy_is_not_waived_by_merge_off(setup):
    handler, _, git, _, config = setup
    config.integration.merge_ci_policy = "off"
    git.acommit_check_runs.return_value = [run(conclusion="failure")]
    assert (await close(handler))["success"] is False


async def test_required_config_without_names_still_needs_reported_checks(setup):
    handler, db, git, _, config = setup
    await db.update_project("p", hierarchical_integration_policy=None)
    config.integration.merge_ci_policy = "required"
    git.acommit_check_runs.return_value = []
    assert (await close(handler))["ci"]["state"] == "pending"


async def test_task_repository_binding_overrides_project_url(setup):
    handler, db, git, _, _ = setup
    await db.create_repo(RepoConfig(id="repo", project_id="p", url="https://github.com/other/source",
                                   source_type=RepoSourceType.CLONE))
    await db.update_task("t", repo_id="repo")
    git.bind_github_repository.return_value = GitHubRepositoryBinding(123, "other/source")
    assert (await close(handler))["success"] is True
    assert git.acommit_check_runs.await_args.args[0] == "other/source"


async def completed(db, tid, head):
    await db.create_task(Task(id=tid, project_id="p", title=tid, description="",
                              status=TaskStatus.COMPLETED, branch_name="aq/" + tid))
    if head:
        await db.save_task_completion(TaskCompletion(id=tid + "-close", task_id=tid,
                                                      outcome="pass", commits=[head]))


async def test_supervisor_audit_uses_recorded_heads_and_keeps_unknowns(setup):
    handler, db, git, _, _ = setup
    await completed(db, "red", HEAD)
    await completed(db, "same", HEAD)
    await completed(db, "pending", OTHER)
    await completed(db, "missing", None)

    async def checks(slug, head, **kwargs):
        return [run(conclusion="failure")] if head == HEAD else []

    git.acommit_check_runs.side_effect = checks
    result = await handler.execute("ci_completed_status", {"project_id": "p"})
    assert result["success"] is True, result
    assert result["checked"] == 4
    assert {row["task_id"] for row in result["failing"]} == {"red", "same"}
    assert {row["task_id"]: row["state"] for row in result["unresolved"]} == {
        "pending": "pending", "missing": "unknown",
    }
    assert result["failing"][0]["failing_checks"] == [REQUIRED]
    assert result["failing"][0]["checks"][0]["url"].startswith("https://github.com/")
    assert git.acommit_check_runs.await_count == 2
    git.aremote_branch_head.assert_not_awaited()
    git.acommit_head_sha.assert_not_awaited()
    assert (await db.get_task("red")).status is TaskStatus.COMPLETED


async def test_audit_includes_managed_source_failures_and_legacy_commit(setup):
    handler, db, git, _, _ = setup
    await db.update_project("p", hierarchical_integration_mode="train")
    await completed(db, "legacy", None)
    await db.set_task_meta("legacy", "work_commit_auto", HEAD)
    git.acommit_check_runs.return_value = [run(conclusion="failure")]
    result = await handler.execute("ci_completed_status", {"project_id": "p"})
    assert result["failing"][0]["head_sha"] == HEAD


def test_audit_contract_and_worker_scope():
    from src.api.auth import LOCAL_SCOPE, RequestScope
    from src.api.scope import AGENT_COMMAND_SET, check_command_scope
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import _outcome_of

    assert "ci_completed_status" not in AGENT_COMMAND_SET
    assert CONTRACTS.get("ci_completed_status") is not None
    assert _outcome_of("ci_completed_status", {"success": True, "checked": 0}) == "listed"
    assert _outcome_of("ci_completed_status", {"success": False, "error": "unknown"}) == "rejected"
    worker = RequestScope(kind="session", session_id="s", project_id="p", task_id="t")
    supervisor = RequestScope(kind="session", session_id="s", project_id="p", elevated=True)
    assert check_command_scope("ci_completed_status", {"project_id": "p"}, worker)
    assert check_command_scope("ci_completed_status", {"project_id": "p"}, supervisor) is None
    assert check_command_scope("ci_completed_status", {"project_id": "other"}, supervisor)
    assert check_command_scope("ci_completed_status", {"project_id": "other"}, LOCAL_SCOPE) is None
