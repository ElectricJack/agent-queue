"""Exact published-source CI for ordinary PR completion and supervisor audits."""

from __future__ import annotations

from dataclasses import dataclass

from src.git.ci_gate import classify_rollup
from src.git.manager import is_valid_git_oid
from src.integration.delivery_path import (
    MANAGED_MODES, effective_integration_mode, task_repository_url,
)
from src.integration.models import RequiredCheckSet
from src.integration.source_ci import classify_source_checks


@dataclass(frozen=True)
class CompletionChecks:
    names: tuple[str, ...]
    trusted: RequiredCheckSet | None = None


async def completion_checks(db, config, task, project, *, audit=False) -> CompletionChecks | None:
    if (project is None or not task.branch_name
            or (not audit and project.hierarchical_integration_mode in MANAGED_MODES)):
        return None
    mode, _ = await effective_integration_mode(
        db, task, default_mode=config.integration.default_mode,
    )
    if not audit and mode != "pull_request":
        return None
    policy = project.hierarchical_integration_policy
    if policy is not None:
        # A configured policy must not silently become an empty requirement.
        required = RequiredCheckSet.model_validate(policy["root"]["required_checks"])
        return CompletionChecks(required.names, required)
    names = tuple(config.integration.merge_required_checks)
    if names or config.integration.merge_ci_policy == "required":
        return CompletionChecks(names)
    return None


def unavailable(head: str | None, reason: str) -> dict:
    return {"state": "unknown", "head_sha": head, "error": reason,
            "failing_checks": [], "pending_checks": [], "missing_checks": [], "checks": []}


async def observe_completion_ci(git, repository_url: str, head: str | None,
                                required: CompletionChecks) -> dict:
    if not head or not is_valid_git_oid(head):
        return unavailable(head, "No valid recorded completion commit")
    from src.projects.github import parse_github_repository

    try:
        expected = parse_github_repository(repository_url).full_name
        repository = await git.bind_github_repository(repository_url)
        if repository.full_name != expected:
            raise ValueError("CI repository identity changed")
        entries = await git.acommit_check_runs(expected, head, repository=repository)
        if not isinstance(entries, list):
            return unavailable(head, "Required CI could not be read")
        # Every entry must identify the exact source, even on the config fallback.
        entries = [entry for entry in entries if isinstance(entry, dict)
                   and entry.get("head_sha") == head]
        if required.trusted is not None:
            state, evidence = classify_source_checks(
                entries, head=head, required=required.trusted,
            )
            reported = {check["name"] for check in evidence["checks"]}
            return {"state": state, **evidence,
                    "pending_checks": [check["name"] for check in evidence["checks"]
                                       if check["status"] != "completed"],
                    "missing_checks": sorted(set(required.names) - reported)}
        # Reruns supersede older conclusions for the same check name.
        latest = {}
        for entry in entries:
            name = entry.get("name")
            identity = (str(entry.get("started_at") or entry.get("created_at") or ""),
                        int(entry.get("id") or 0))
            if name and (name not in latest or identity > latest[name][0]):
                latest[name] = (identity, entry)
        checks = [item[1] for item in latest.values()]
        verdict = classify_rollup(checks, required_checks=list(required.names))
        return {"state": verdict.state, "head_sha": head,
                "failing_checks": list(verdict.failing),
                "pending_checks": list(verdict.pending), "missing_checks": list(verdict.missing),
                "checks": [{"name": check["name"], "url": check.get("html_url"),
                            "status": check.get("status"), "conclusion": check.get("conclusion")}
                           for check in checks if check["name"] in verdict.considered]}
    except Exception:
        return unavailable(head, "Required CI repository, policy or check observation is unavailable")


async def verify_completion_ci(db, git, config, task, project, *, commit=None,
                               workspace_path=None) -> dict | None:
    try:
        required = await completion_checks(db, config, task, project)
        if required is None:
            return None
        if workspace_path is None:
            workspace = await db.get_workspace_for_task(task.id)
            workspace_path = workspace.workspace_path if workspace else None
        if not workspace_path:
            return unavailable(None, "Required CI needs the task's published workspace commit")
        branch = task.branch_name.removeprefix("refs/heads/")
        ref = "refs/heads/" + branch
        head = await git.arev_parse(workspace_path, ref)
        if not head or not is_valid_git_oid(head) or (commit and commit != head):
            return unavailable(head, "Close commit differs from the task's source commit")
        repository_url = await task_repository_url(db, task, project)
        repository = await git.bind_github_repository(repository_url)
        published = await git.aremote_branch_head(repository=repository, branch=branch)
        if published != head:
            return unavailable(head, "Task branch is not published at the completion commit")
        observed = await observe_completion_ci(git, repository_url, head, required)
        local = await git.arev_parse(workspace_path, ref)
        published = await git.aremote_branch_head(repository=repository, branch=branch)
        if local != head or published != head:
            return unavailable(head, "Task source changed during CI observation; recheck the new head")
        return observed
    except Exception:
        return unavailable(None, "Required completion CI policy or source is unavailable")
