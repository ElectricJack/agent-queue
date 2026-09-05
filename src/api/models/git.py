"""Response models for git commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class CheckoutBranchResponse(BaseModel):
    project_id: str
    branch: str = ""
    status: str = ""
    warning: str | None = None


class CommitChangesResponse(BaseModel):
    project_id: str
    commit_message: str | None = None
    status: str = ""
    message: str | None = None
    warning: str | None = None


class CreateBranchResponse(BaseModel):
    project_id: str
    branch: str = ""
    status: str = ""


class CreateGithubRepoResponse(BaseModel):
    created: bool = False
    repo_url: str = ""
    name: str = ""


class GenerateReadmeResponse(BaseModel):
    project_id: str
    readme_path: str = ""
    committed: bool = False
    pushed: bool = False
    status: str | None = None
    message: str | None = None


class GetGitStatusResponse(BaseModel):
    project_id: str
    project_name: str = ""
    repos: list[dict[str, Any]] = []


class GitBranchResponse(BaseModel):
    model_config = {"extra": "allow"}
    project_id: str
    created: str | None = None
    message: str | None = None
    current_branch: str | None = None
    branches: list[str] | None = None


class GitChangedFilesResponse(BaseModel):
    project_id: str
    base_branch: str = ""
    files: list[str] = []
    count: int = 0


class GitCheckoutResponse(BaseModel):
    project_id: str
    old_branch: str = ""
    new_branch: str = ""
    message: str = ""


class GitCommitResponse(BaseModel):
    project_id: str
    committed: bool = False
    commit_message: str | None = None
    message: str | None = None


class GitCreateBranchResponse(BaseModel):
    project_id: str
    created_branch: str = ""


class GitCreatePrResponse(BaseModel):
    project_id: str
    pr_url: str = ""
    branch: str = ""
    base: str = ""


class GitDiffResponse(BaseModel):
    project_id: str
    base_branch: str = ""
    diff: str = ""


class GitLogResponse(BaseModel):
    project_id: str
    branch: str = ""
    log: str = ""


class GitMergeResponse(BaseModel):
    project_id: str
    merged: bool = False
    branch: str | None = None
    into: str = ""
    message: str | None = None


class GitPullResponse(BaseModel):
    project_id: str
    pulled: bool = False


class GitPushResponse(BaseModel):
    project_id: str
    pushed: str = ""


class MergeBranchResponse(BaseModel):
    project_id: str
    branch: str = ""
    target: str = ""
    status: str = ""
    message: str | None = None
    warning: str | None = None


class PushBranchResponse(BaseModel):
    project_id: str
    branch: str = ""
    status: str = ""


class GitRemoteUrlResponse(BaseModel):
    project_id: str
    remote: str = "origin"
    url: str | None = None
    message: str | None = None


class PrMergeCiVerdict(BaseModel):
    """What the PR head's status checks said, and what the policy did about it.

    Present on every ``pr_merge`` response unless
    ``integration.merge_ci_policy`` is ``off`` (in which case CI is never
    consulted).  Under ``warn`` the merge happens regardless and this block
    is the record of what landed; under ``required`` a ``blocked`` verdict
    is why ``success`` is false.
    """

    #: The effective ``integration.merge_ci_policy``: ``warn`` or ``required``.
    policy: str = ""
    #: ``green`` / ``red`` / ``pending`` / ``unknown`` (see ``src/git/ci_gate.py``).
    state: str = ""
    #: One line naming the checks behind the verdict.
    summary: str = ""
    failing: list[str] = []
    pending: list[str] = []
    #: Required check names the rollup does not mention at all.
    missing: list[str] = []
    #: True when the merge was refused because of this verdict.
    blocked: bool = False
    #: True when ``force`` overrode a ``required`` refusal.
    forced: bool = False
    message: str = ""


class PrMergeResponse(BaseModel):
    """Response model for the ``pr_merge`` command."""

    success: bool = False
    pr_url: str = ""
    sha: str | None = None
    error: str | None = None
    ci: PrMergeCiVerdict | None = None


class CiBaselineStatusResponse(BaseModel):
    success: bool = True
    project_id: str = ""
    ref: str = ""
    head_sha: str | None = None
    state: str = "unknown"
    failing_checks: list[str] = []
    pending_checks: list[str] = []
    failing_tests: list[str] = []
    run_url: str | None = None
    signature: str | None = None
    attempt: int = 0
    prior_attempts: list[str] = []
    escalated: bool = False
    dedup_key: str | None = None
    title: str | None = None
    description: str | None = None
    escalation_key: str | None = None
    escalation_title: str | None = None
    escalation_question: str | None = None
    error: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "ci_baseline_status": CiBaselineStatusResponse,
    "checkout_branch": CheckoutBranchResponse,
    "commit_changes": CommitChangesResponse,
    "create_branch": CreateBranchResponse,
    "create_github_repo": CreateGithubRepoResponse,
    "generate_readme": GenerateReadmeResponse,
    "get_git_status": GetGitStatusResponse,
    "git_branch": GitBranchResponse,
    "git_changed_files": GitChangedFilesResponse,
    "git_checkout": GitCheckoutResponse,
    "git_commit": GitCommitResponse,
    "git_create_branch": GitCreateBranchResponse,
    "git_create_pr": GitCreatePrResponse,
    "git_diff": GitDiffResponse,
    "git_log": GitLogResponse,
    "git_merge": GitMergeResponse,
    "git_pull": GitPullResponse,
    "git_push": GitPushResponse,
    "merge_branch": MergeBranchResponse,
    "push_branch": PushBranchResponse,
    "git_remote_url": GitRemoteUrlResponse,
    "pr_merge": PrMergeResponse,
}
