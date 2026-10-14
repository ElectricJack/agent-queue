"""Exact published-source CI for ordinary PR completion and supervisor audits."""

from __future__ import annotations

from dataclasses import dataclass

from src.git.ci_gate import classify_rollup
from src.git.manager import is_valid_git_oid
from src.integration.delivery_path import (
    MANAGED_MODES, effective_integration_mode, task_repository_url,
)
from src.integration.models import RequiredCheckSet, validate_policy_producers
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
        validate_policy_producers(policy)
        required = RequiredCheckSet.model_validate(policy["root"]["required_checks"])
        return CompletionChecks(required.names, required)
    names = tuple(config.integration.merge_required_checks)
    if names or config.integration.merge_ci_policy == "required":
        return CompletionChecks(names)
    return None


# Where an unknown verdict came from, and the bounded recovery it allows.
# Unknown always stays fail-closed; these only say which lookup to repair.
STAGE_RECOVERY = {
    "policy": "Operator: fix the project's required_checks/producer policy; "
              "`aq ci completed-status` re-audits it.",
    "source_commit": "Commit on the task branch in the assigned workspace and close with "
                     "that commit.",
    "repository_binding": "Operator: check the task's repository row URL (owner/name and "
                          "casing as GitHub reports it) and that the GitHub credential or App "
                          "installation covers it; workers cannot repair credentials.",
    "published_ref": "Publish the completion commit with `aq git push`, then retry close.",
    "no_change_proof": "Keep the checkout clean on the task branch at the base, or commit "
                       "and publish the change, then retry close.",
    "check_runs": "rate_limited/transient: retry after retry_at. credentials, permission "
                  "or not_found_or_hidden: operator checks the credential's checks:read "
                  "access to this repository.",
    "classification": "Check data or policy was malformed; report the stage to the operator.",
    "currentness": "The source moved while CI was read; publish the new head and retry close.",
}
_LOOKUP_CATEGORIES = frozenset({
    "credentials", "permission", "not_found_or_hidden", "rate_limited", "transient",
    "conflict_or_invalid", "github_operation_unsupported",
})


def unavailable(head: str | None, reason: str, *, stage: str,
                cause: BaseException | None = None, **evidence) -> dict:
    result = {"state": "unknown", "head_sha": head, "error": reason, "stage": stage,
              "next": STAGE_RECOVERY[stage], "failing_checks": [], "pending_checks": [],
              "missing_checks": [], "checks": [], **evidence}
    if cause is not None:
        result["lookup"] = lookup_failure(cause)
    return result


def describe_completion_ci(ci: dict) -> str:
    """One refusal sentence naming the failed stage or the unmet checks."""
    text = f"Required CI is {ci['state']} on {ci.get('head_sha') or 'unknown head'}"
    if ci.get("stage"):
        lookup = ci.get("lookup") or {}
        cause = ", ".join(str(part) for part in (
            lookup.get("category") or lookup.get("code"),
            f"HTTP {lookup['http_status']}" if lookup.get("http_status") else None,
        ) if part)
        text += f" at {ci['stage']}" + (f" ({cause})" if cause else "")
    if ci.get("error"):
        text += f": {ci['error']}"
    for label, key in (("failing", "failing_checks"), ("pending", "pending_checks"),
                       ("missing", "missing_checks")):
        if ci.get(key):
            text += f"; {label}: {', '.join(str(name) for name in ci[key][:10])}"
    return text + "."


def lookup_failure(exc: BaseException) -> dict:
    """A redacted classification of *exc*: types, categories and statuses only.

    Arbitrary exception text can carry tokens, headers or signed URLs, so it is
    never copied; only the safe-by-contract GitHub failure sentence is kept,
    scrubbed, bounded and dropped if it still looks like a URL.
    """
    from src.git.github_contracts import GitHubAccessError
    from src.projects.github import GitHubError, scrub_secrets

    found = {"error_type": type(exc).__name__}
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, GitHubAccessError):
            found["category"] = (current.category if current.category in _LOOKUP_CATEGORIES
                                 else "other")
            if isinstance(current.http_status, int):
                found["http_status"] = current.http_status
            if isinstance(current.retry_at, (int, float)):
                found["retry_at"] = current.retry_at
            detail = scrub_secrets(str(current))[:160]
            if detail and "://" not in detail and "?" not in detail and "***" not in detail:
                found["detail"] = detail
            break
        if isinstance(current, GitHubError):
            found["code"] = current.code.value
            break
        current = current.__cause__ or current.__context__
    return found


def _ignored_checks(entries: list, head: str, required: CompletionChecks) -> dict:
    """Bounded counts of check runs that cannot satisfy the requirement.

    Explains a pending verdict (PR merge SHA, renamed job, other producer)
    without letting any of them count toward green.
    """
    names = set(required.names)
    producer = required.trusted.producer_id if required.trusted is not None else None
    other_heads, not_required, untrusted = set(), set(), set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")[:200]
        if entry.get("head_sha") != head:
            other_heads.add(str(entry.get("head_sha") or "")[:40])
        elif name not in names:
            not_required.add(name)
        elif producer is not None:
            app = entry.get("app") if isinstance(entry.get("app"), dict) else {}
            if str(app.get("id")) != producer:
                untrusted.add((name, str(app.get("id"))[:40]))
    return {"other_head_count": len(other_heads), "other_heads": sorted(other_heads)[:5],
            "not_required": sorted(not_required)[:20],
            "untrusted_producer": [{"name": name, "producer_id": app}
                                   for name, app in sorted(untrusted)[:20]]}


async def observe_completion_ci(git, repository_url: str, head: str | None,
                                required: CompletionChecks) -> dict:
    if not head or not is_valid_git_oid(head):
        return unavailable(head, "No valid recorded completion commit", stage="source_commit")
    from src.projects.github import parse_github_repository

    stage = "repository_binding"
    try:
        expected = parse_github_repository(repository_url).full_name
        repository = await git.bind_github_repository(repository_url)
        if repository.full_name != expected:
            return unavailable(head, "CI repository identity changed", stage=stage)
        stage = "check_runs"
        entries = await git.acommit_check_runs(expected, head, repository=repository,
                                               strict=True)
        if not isinstance(entries, list):
            return unavailable(head, "Required CI could not be read", stage=stage)
        stage = "classification"
        ignored = _ignored_checks(entries, head, required)
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
                    "missing_checks": sorted(set(required.names) - reported),
                    "ignored_checks": ignored}
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
                           for check in checks if check["name"] in verdict.considered],
                "ignored_checks": ignored}
    except Exception as exc:
        return unavailable(head, "Required CI repository, policy or check observation is "
                           "unavailable", stage=stage, cause=exc)


async def verify_completion_ci(db, git, config, task, project, *, commit=None,
                               workspace_path=None) -> dict | None:
    stage, head = "policy", None
    try:
        required = await completion_checks(db, config, task, project)
        if required is None:
            return None
        stage = "source_commit"
        if workspace_path is None:
            workspace = await db.get_workspace_for_task(task.id)
            workspace_path = workspace.workspace_path if workspace else None
        if not workspace_path:
            return unavailable(None, "Required CI needs the task's published workspace commit",
                               stage=stage)
        branch = task.branch_name.removeprefix("refs/heads/")
        ref = "refs/heads/" + branch
        head = await git.arev_parse(workspace_path, ref)
        if not head or not is_valid_git_oid(head) or (commit and commit != head):
            return unavailable(head, "Close commit differs from the task's source commit",
                               stage=stage)
        stage = "repository_binding"
        repository_url = await task_repository_url(db, task, project)
        repository = await git.bind_github_repository(repository_url)
        from src.projects.github import parse_github_repository

        if repository.full_name != parse_github_repository(repository_url).full_name:
            return unavailable(head, "CI repository identity changed", stage=stage)
        stage = "published_ref"
        published = await git.aremote_branch_head(repository=repository, branch=branch)
        # Review/vault work may never publish a source branch or trigger CI.
        # Only exact base equality on a clean assigned checkout proves that;
        # intent or task type must not hide documentation/code commits.
        if published in (None, head):
            repo = await db.get_repo(task.repo_id) if task.repo_id else None
            base = repo.default_branch if repo else project.repo_default_branch
            if base and await _unchanged_source(git, workspace_path, ref, branch, head,
                                               repository, base):
                stage = "no_change_proof"
                local = await git.arev_parse(workspace_path, ref)
                published = await git.aremote_branch_head(repository=repository, branch=branch)
                if (local == head and published in (None, head)
                        and await _unchanged_source(git, workspace_path, ref, branch, head,
                                                    repository, base)):
                    return {"state": "green", "head_sha": head, "base_sha": head,
                            "base_branch": base, "branch": branch, "repository_url": repository_url,
                            "reason": "no_source_changes", "failing_checks": [],
                            "pending_checks": [], "missing_checks": [], "checks": []}
                return unavailable(head, "Task source or base changed during no-change "
                                   "verification", stage=stage)
        if published != head:
            return unavailable(head, "Task branch is not published at the completion commit",
                               stage=stage, published_sha=published)
        observed = await observe_completion_ci(git, repository_url, head, required)
        stage = "currentness"
        local = await git.arev_parse(workspace_path, ref)
        published = await git.aremote_branch_head(repository=repository, branch=branch)
        if local != head or published != head:
            return unavailable(head, "Task source changed during CI observation; recheck the "
                               "new head", stage=stage, local_sha=local, published_sha=published)
        return observed
    except Exception as exc:
        return unavailable(head, "Required completion CI policy or source is unavailable",
                           stage=stage, cause=exc)


async def _unchanged_source(git, workspace, ref, branch, head, repository, base) -> bool:
    try:
        if await git.ahas_uncommitted_changes(workspace, strict=True) is not False:
            return False
        if await git.aget_current_branch(workspace, strict=True) != branch:
            return False
        if await git.arev_parse(workspace, ref) != head:
            return False
        return await git.aremote_branch_head(repository=repository, branch=base) == head
    except Exception:
        return False  # Unavailable no-change proof must not waive configured CI.
