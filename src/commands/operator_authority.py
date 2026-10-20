"""Request-local setup authority; an operator worker never becomes LOCAL.

The wrapper argument models live here, beside the role constants, so tool
definitions can describe them without importing the handler or database.
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, StrictInt

OPERATOR_CLASSES = frozenset({"standard-high", "deep-high"})
OPERATOR_HARNESSES = frozenset({"claude", "codex"})
OPERATOR_FORBIDDEN = frozenset({
    "ask_prepare_decision", "ask_decide", "ask_retry_operation",
    "review_decide", "review_delegate", "decision_record", "promote_approve",
    "promote_prepare", "promote_request", "promote_hotfix", "delivery_promote",
    "integration_promote_main", "pr_merge", "git_merge", "integration_app_setup",
})
OPERATOR_READS = frozenset({
    "integration_status", "integration_trust_manifest", "integration_app_verify",
    "integration_cutover_plan", "provider_status", "policy_diff",
    "wait_graph",
})
OPERATOR_WRITES = frozenset({
    "workspace_cutover", "edit_project", "integration_cutover", "integration_abort_batch",
    "integration_eject", "integration_settle_batch",
    "integration_retire_origin", "integration_release_owner", "playbook_v2_import",
    "playbook_activate", "set_playbook_enabled", "playbook_delete", "policy_apply",
    "wait_lift",
})
HUMAN_DECISION_COMMANDS = frozenset({
    "edit_project", "integration_cutover", "integration_abort_batch", "integration_eject",
    "integration_settle_batch",
    "integration_retire_origin", "integration_release_owner", "playbook_delete",
})
PROJECT_SETUP_FIELDS = frozenset({
    "project_id", "integration_repository", "integration_repository_id",
    "hierarchical_integration_policy", "hierarchical_integration_mode", "integration_mode",
    "promotion_flow", "expected_integration_generation", "reason",
})


def arguments_sha256(arguments: dict) -> str:
    return hashlib.sha256(json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def is_operator_profile(profile) -> bool:
    """The explicit authored grant is the role marker, never a profile-id prefix."""
    return "operator_execute" in (getattr(profile, "aq_commands", None) or ())


@dataclass(frozen=True)
class OperatorGrant:
    command: str
    args_sha256: str
    project_id: str
    task_id: str
    session_id: str
    claim_epoch: int

    def matches(self, command: str, arguments: dict) -> bool:
        return self.command == command and self.args_sha256 == arguments_sha256(arguments)


operator_grant_var: ContextVar[OperatorGrant | None] = ContextVar("operator_grant", default=None)


def operator_grant(command: str | None = None) -> OperatorGrant | None:
    grant = operator_grant_var.get()
    return grant if grant and (command is None or grant.command == command) else None


def local_or_operator(command: str) -> bool:
    """Local authority, or an operator grant bound to exactly this dispatched command."""
    from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal

    principal = current_principal() or TRUSTED_LOCAL
    return principal.kind is PrincipalKind.LOCAL or operator_grant(command) is not None


def bind_operator_dispatch(principal, name: str, args: dict):
    """Scope one dispatch: (enforce capabilities, grant reset token, refusal or None).

    The wrapper's grant survives only into the exact command and arguments it
    audited; an operator session reaches setup commands through the wrapper alone.
    """
    from src.commands.principal import PrincipalKind

    operator_session = (
        principal.kind is PrincipalKind.SESSION and not principal.policy.derived_from_legacy
        and "operator_execute" in principal.policy.aq_commands
    )
    grant = operator_grant_var.get()
    token = operator_grant_var.set(grant if grant and grant.matches(name, args) else None)
    refusal = None
    if operator_session and name in OPERATOR_FORBIDDEN:
        refusal = "operator cannot perform human gate or delivery actions"
    elif operator_session and name in OPERATOR_READS | OPERATOR_WRITES and not operator_grant():
        refusal = "operator setup commands require operator_execute"
    return operator_session, token, refusal


class OperatorAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    arguments: dict = Field(default_factory=dict)
    decision_id: str | None = None


class OperatorExecuteArgs(OperatorAction):
    project_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    preflight_id: StrictInt | None = None


class OperatorPreflightArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    actions: list[OperatorAction] = Field(min_length=1, max_length=50)
    branches: list[str] = Field(default_factory=list, max_length=50)
    runner_labels: list[str] = Field(default_factory=list, max_length=50)
    app_ids: list[StrictInt] = Field(default_factory=list, max_length=10)
    policy: dict | None = None
    repository_id: str | None = None
