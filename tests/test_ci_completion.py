"""Required CI binds ordinary PR closes and audits to recorded source commits."""

from unittest.mock import AsyncMock, create_autospec

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig
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
async def setup(tmp_path, monkeypatch, reuse_database):
    db = await reuse_database("completion-ci")
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


@pytest.mark.parametrize("published", [HEAD, None])
async def test_unchanged_branch_closes_without_source_ci_and_audits_cleanly(setup, published):
    handler, db, git, _, _ = setup
    git.ahas_uncommitted_changes.return_value = False
    git.aget_current_branch.return_value = "aq/t"
    git.aremote_branch_head.side_effect = lambda *, branch, repository: (
        HEAD if branch == "main" else published
    )
    git.acommit_check_runs.return_value = []
    result = await close(handler)
    assert result["success"] is True, result
    proof = await db.get_task_meta("t", "completion_ci")
    assert proof["reason"] == "no_source_changes"
    assert proof["head_sha"] == proof["base_sha"] == HEAD
    assert (await db.get_task_completion("t")).commits == [HEAD]
    audit = await handler.execute("ci_completed_status", {"project_id": "p"})
    assert audit["checked"] == 0
    assert audit["unresolved"] == []
    git.acommit_check_runs.assert_not_awaited()


@pytest.mark.parametrize("kind", ["dirty", "unknown_status", "other_branch", "changed_base",
                                      "unknown_base", "foreign_source", "foreign_repository"])
async def test_no_change_exemption_requires_git_proof(setup, kind):
    handler, _, git, _, _ = setup
    git.ahas_uncommitted_changes.return_value = False
    git.aget_current_branch.return_value = "aq/t"
    git.acommit_check_runs.return_value = []
    if kind == "dirty":
        git.ahas_uncommitted_changes.return_value = True
    elif kind == "unknown_status":
        git.ahas_uncommitted_changes.return_value = None
    elif kind == "other_branch":
        git.aget_current_branch.return_value = "other-work"
    elif kind in {"changed_base", "unknown_base", "foreign_source"}:
        git.aremote_branch_head.side_effect = lambda *, branch, repository: (
            (OTHER if kind != "unknown_base" else None)
            if branch == ("aq/t" if kind == "foreign_source" else "main") else HEAD
        )
    else:
        git.bind_github_repository.return_value = GitHubRepositoryBinding(999, "other/repo")
    result = await close(handler, work_outcome="no-op")
    assert result["success"] is False
    assert result["code"] == "completion.required_ci"


@pytest.mark.parametrize("change", ["dirty", "checkout", "local", "base", "published"])
async def test_unchanged_source_is_rechecked_before_waiving_ci(setup, change):
    _, db, git, _, config = setup
    git.ahas_uncommitted_changes.return_value = False
    git.aget_current_branch.return_value = "aq/t"
    if change == "dirty":
        git.ahas_uncommitted_changes.side_effect = [False, True]
    elif change == "checkout":
        git.aget_current_branch.side_effect = ["aq/t", "other-work"]
    elif change == "local":
        git.arev_parse.side_effect = [HEAD, HEAD, OTHER]
    else:
        calls = {}

        async def remote(*, branch, repository):
            calls[branch] = calls.get(branch, 0) + 1
            moved = branch == ("main" if change == "base" else "aq/t")
            return OTHER if moved and calls[branch] > 1 else HEAD

        git.aremote_branch_head.side_effect = remote
    result = await verify_completion_ci(db, git, config, await db.get_task("t"),
                                        await db.get_project("p"))
    assert result["state"] == "unknown"
    git.acommit_check_runs.assert_not_awaited()


async def test_no_change_proof_uses_bound_repository_default_branch(setup):
    _, db, git, _, config = setup
    await db.create_repo(RepoConfig(id="repo", project_id="p", url=URL,
                                   default_branch="trunk", source_type=RepoSourceType.CLONE))
    await db.update_task("t", repo_id="repo")
    git.ahas_uncommitted_changes.return_value = False
    git.aget_current_branch.return_value = "aq/t"
    result = await verify_completion_ci(db, git, config, await db.get_task("t"),
                                        await db.get_project("p"))
    assert result["reason"] == "no_source_changes"
    assert result["base_branch"] == "trunk"
    assert {call.kwargs["branch"] for call in git.aremote_branch_head.await_args_list} == {
        "aq/t", "trunk",
    }


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
    assert (f"failing: {REQUIRED}" if change == "red" else f"missing: {REQUIRED}") in (
        result["feedback"])
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


def project_policy():
    return {
        "parent": {"required_checks": {"version": "v1", "names": [REQUIRED],
                                       "producer_id": "15368"},
                   "repair": {"debug_intelligence_class": "standard-high"}},
        "root": {"required_checks": {"version": "v1", "names": [REQUIRED],
                                     "producer_id": "15368"},
                 "repair": {"debug_intelligence_class": "standard-high"}},
        "branchless_parent": "skip", "on_failed_child": "block",
    }


@pytest.mark.parametrize("boundary", ["parent", "root"])
@pytest.mark.parametrize("producer", ["github-actions", "", "0", "-1", "015368", " 15368",
                                          "15368\n", "١٥٣٦٨", 15368, True, None])
async def test_project_policy_write_rejects_invalid_producers_without_changes(setup, boundary, producer):
    handler, db, _, _, _ = setup
    policy = project_policy()
    policy[boundary]["required_checks"]["producer_id"] = producer
    before = await db.get_project("p")
    result = await handler.execute("edit_project", {
        "project_id": "p", "hierarchical_integration_policy": policy,
        "expected_integration_generation": 0, "reason": "test producer validation",
    })
    assert result["success"] is False, result
    assert f"{boundary}.required_checks.producer_id" in result["error"]
    assert result["error_code"] == "invalid_ci_producer"
    after = await db.get_project("p")
    assert after.hierarchical_integration_policy == before.hierarchical_integration_policy
    assert after.hierarchical_integration_generation == 0


async def test_project_policy_write_accepts_numeric_producers(setup):
    handler, db, _, _, _ = setup
    result = await handler.execute("edit_project", {
        "project_id": "p", "hierarchical_integration_policy": project_policy(),
        "expected_integration_generation": 0, "reason": "test producer validation",
    })
    assert result["outcome"] == "configured", result
    stored = (await db.get_project("p")).hierarchical_integration_policy
    for boundary in ("parent", "root"):
        assert stored[boundary]["required_checks"]["producer_id"] == "15368"


async def test_legacy_slug_is_unavailable_completion_ci(setup):
    handler, db, git, _, _ = setup
    policy = project_policy()
    policy["root"]["required_checks"]["producer_id"] = "github-actions"
    await db.update_project("p", hierarchical_integration_policy=policy)
    assert (await close(handler))["ci"]["state"] == "unknown"
    git.acommit_check_runs.assert_not_awaited()


SECRET = "ghs_" + "S" * 36
SECRET_URL = "https://x-access-token:hunter2@github.com/example/widgets.git"


def access_error(category, *, status=None, retry_at=None, message="GitHub request failed"):
    from src.git.github_contracts import GitHubAccessError

    return GitHubAccessError(category, message, http_status=status, retry_at=retry_at)


def wrapped(error, text=f"Authorization: token {SECRET} via {SECRET_URL}"):
    from src.git.manager import GitError

    try:
        raise error
    except Exception as exc:
        try:
            raise GitError(text) from exc
        except GitError as outer:
            return outer


@pytest.mark.parametrize("kind,stage,lookup", [
    ("binding_auth", "repository_binding",
     {"error_type": "GitError", "category": "credentials", "http_status": 401,
      "detail": "authenticated repository identity did not match"}),
    ("binding_unconfigured", "repository_binding", {"error_type": "GitError"}),
    ("binding_transport", "repository_binding",
     {"error_type": "GitError", "category": "transient", "http_status": 502,
      "detail": "GitHub request failed"}),
    ("foreign_repository", "repository_binding", None),
    ("checks_permission", "check_runs",
     {"error_type": "GitHubAccessError", "category": "permission", "http_status": 403,
      "detail": "GitHub request failed"}),
    ("checks_hidden", "check_runs",
     {"error_type": "GitHubAccessError", "category": "not_found_or_hidden", "http_status": 404,
      "detail": "GitHub request failed"}),
    ("checks_rate_limited", "check_runs",
     {"error_type": "GitHubAccessError", "category": "rate_limited", "http_status": 429,
      "retry_at": 1_800_000_000.0, "detail": "GitHub request failed"}),
    ("checks_pagination", "check_runs",
     {"error_type": "GitHubAccessError", "category": "transient",
      "detail": "GitHub pagination exceeded the page limit"}),
    ("checks_secret_message", "check_runs",
     {"error_type": "GitHubAccessError", "category": "other"}),
    ("checks_unreadable", "check_runs", None),
    ("absent_ref", "published_ref", None),
    ("changed_ref", "published_ref", None),
    ("ref_transport", "published_ref",
     {"error_type": "GitError", "category": "transient", "http_status": 503,
      "detail": "GitHub request failed"}),
    ("old_commit", "source_commit", None),
    ("invalid_policy", "policy", {"error_type": "ValueError"}),
    ("stale_observation", "currentness", None),
])
async def test_unknown_ci_names_the_failed_stage_without_secrets(setup, kind, stage, lookup):
    """Unknown stays fail-closed but says which lookup, source or policy step failed."""
    import json

    from src.git.manager import GitError

    handler, db, git, _, _ = setup
    kwargs = {}
    if kind == "binding_auth":
        git.bind_github_repository.side_effect = wrapped(access_error(
            "credentials", status=401,
            message="authenticated repository identity did not match"))
    elif kind == "binding_unconfigured":
        git.bind_github_repository.side_effect = GitError(f"no access {SECRET}")
    elif kind == "binding_transport":
        git.bind_github_repository.side_effect = wrapped(access_error("transient", status=502))
    elif kind == "foreign_repository":
        git.bind_github_repository.return_value = GitHubRepositoryBinding(999, "other/repo")
    elif kind == "checks_permission":
        git.acommit_check_runs.side_effect = access_error("permission", status=403)
    elif kind == "checks_hidden":
        git.acommit_check_runs.side_effect = access_error("not_found_or_hidden", status=404)
    elif kind == "checks_rate_limited":
        git.acommit_check_runs.side_effect = access_error(
            "rate_limited", status=429, retry_at=1_800_000_000.0)
    elif kind == "checks_pagination":
        git.acommit_check_runs.side_effect = access_error(
            "transient", message="GitHub pagination exceeded the page limit")
    elif kind == "checks_secret_message":
        git.acommit_check_runs.side_effect = access_error(
            "novel", message=f"denied for {SECRET_URL}?token={SECRET}")
    elif kind == "checks_unreadable":
        git.acommit_check_runs.return_value = None
    elif kind == "absent_ref":
        git.aremote_branch_head.return_value = None
    elif kind == "changed_ref":
        git.aremote_branch_head.return_value = OTHER
    elif kind == "ref_transport":
        git.aremote_branch_head.side_effect = wrapped(access_error("transient", status=503))
    elif kind == "old_commit":
        kwargs["commit"] = OTHER
    elif kind == "invalid_policy":
        await db.update_project("p", hierarchical_integration_policy={"root": {
            "required_checks": {"version": "v1", "names": [REQUIRED]}}})
    else:
        git.aremote_branch_head.side_effect = [HEAD, OTHER]

    result = await close(handler, **kwargs)

    assert result["success"] is False
    assert result["code"] == "completion.required_ci"
    ci = result["ci"]
    assert ci["state"] == "unknown"
    assert ci["stage"] == stage
    assert ci["next"]
    assert ci.get("lookup") == lookup
    assert f"at {stage}" in result["error"]
    assert ci["error"] in result["error"]
    if stage not in {"policy", "source_commit"}:
        assert ci["head_sha"] == HEAD
    if kind == "absent_ref":
        assert ci["published_sha"] is None
    elif kind == "changed_ref":
        assert ci["published_sha"] == OTHER
    elif kind == "stale_observation":
        assert (ci["local_sha"], ci["published_sha"]) == (HEAD, OTHER)
    rendered = json.dumps(result)
    for secret in (SECRET, "hunter2", "x-access-token", "Authorization"):
        assert secret not in rendered
    assert (await db.get_task("t")).status is TaskStatus.IN_PROGRESS
    assert await db.get_task_completion("t") is None


async def test_pending_ci_explains_checks_that_cannot_count(setup):
    """PR merge SHA, renamed and foreign-producer checks stay pending, and say why."""
    handler, db, git, _, _ = setup
    git.acommit_check_runs.return_value = [
        run(head=OTHER, job=1),
        run("Build & test", job=2),
        run("Check release", job=3),
        run(producer=999, job=4),
    ]
    result = await close(handler)
    assert result["success"] is False
    ci = result["ci"]
    assert ci["state"] == "pending"
    assert ci["missing_checks"] == [REQUIRED]
    assert ci["ignored_checks"] == {
        "other_head_count": 1, "other_heads": [OTHER],
        "not_required": ["Build & test", "Check release"],
        "untrusted_producer": [{"name": REQUIRED, "producer_id": "999"}],
    }
    assert f"missing: {REQUIRED}" in result["error"]
    assert "stage" not in ci
    assert (await db.get_task("t")).status is TaskStatus.IN_PROGRESS


@pytest.mark.parametrize("entries,state,label", [
    ([run(conclusion="failure")], "red", "failing"),
    ([run(conclusion=None, status="queued")], "pending", "pending"),
    ([run(conclusion="cancelled")], "cancelled", "failing"),
])
async def test_close_refusal_names_unmet_required_checks(setup, entries, state, label):
    handler, _, git, _, _ = setup
    git.acommit_check_runs.return_value = entries
    result = await close(handler)
    assert result["ci"]["state"] == state
    assert f"{label}: {REQUIRED}" in result["error"]


async def test_trusted_latest_rerun_still_decides_green(setup):
    handler, _, git, _, _ = setup
    first = {**run(conclusion="failure", job=1), "started_at": "2026-10-01T00:00:00Z"}
    rerun = {**run(job=2), "started_at": "2026-10-01T01:00:00Z"}
    git.acommit_check_runs.return_value = [first, rerun, run(head=OTHER, conclusion="failure")]
    assert (await close(handler))["success"] is True


async def test_supervisor_audit_rows_carry_stage_and_redacted_lookup(setup):
    import json

    handler, db, git, _, _ = setup
    await completed(db, "denied", HEAD)
    git.acommit_check_runs.side_effect = access_error(
        "permission", status=403, message=f"denied {SECRET}")
    result = await handler.execute("ci_completed_status", {"project_id": "p"})
    row = next(row for row in result["unresolved"] if row["task_id"] == "denied")
    assert row["state"] == "unknown"
    assert row["stage"] == "check_runs"
    assert row["lookup"] == {"error_type": "GitHubAccessError", "category": "permission",
                             "http_status": 403}
    assert SECRET not in json.dumps(result)


async def test_supervisor_audit_names_invalid_policy_stage(setup, monkeypatch):
    handler, db, _, _, _ = setup
    await completed(db, "invalid", HEAD)
    import src.integration.completion_ci as completion_ci

    async def invalid(*args, **kwargs):
        raise ValueError(f"bad policy {SECRET}")

    monkeypatch.setattr(completion_ci, "completion_checks", invalid)
    result = await handler.execute("ci_completed_status", {"project_id": "p"})
    row = next(row for row in result["unresolved"] if row["task_id"] == "invalid")
    assert (row["state"], row["stage"]) == ("unknown", "policy")
    assert row["lookup"] == {"error_type": "ValueError"}


async def test_commit_check_runs_strict_raises_and_default_stays_none():
    from src.git.manager import GitError

    manager = GitManager()
    repository = GitHubRepositoryBinding(303, "example/widgets")
    assert await manager.acommit_check_runs("example/widgets", HEAD,
                                            repository=repository) is None
    with pytest.raises(GitError):
        await manager.acommit_check_runs("example/widgets", HEAD, repository=repository,
                                         strict=True)


def test_lookup_failure_keeps_only_allowlisted_fields():
    from src.integration.completion_ci import lookup_failure
    from src.projects.github import GitHubError, GitHubErrorCode

    error = GitHubError(GitHubErrorCode.AUTH_REQUIRED, f"login {SECRET}")
    assert lookup_failure(wrapped(error)) == {"error_type": "GitError",
                                              "code": "github_auth_required"}
    assert lookup_failure(RuntimeError(SECRET_URL)) == {"error_type": "RuntimeError"}
