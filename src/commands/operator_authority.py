"""Request-local setup authority; an operator worker never becomes LOCAL."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass

OPERATOR_CLASSES = frozenset({"standard-high", "deep-high"})
OPERATOR_HARNESSES = frozenset({"claude", "codex"})
OPERATOR_FORBIDDEN = frozenset({
    "review_decide", "review_delegate", "decision_record", "promote_approve",
    "promote_prepare", "promote_request", "promote_hotfix", "delivery_promote",
    "integration_promote_main", "pr_merge", "git_merge", "integration_app_setup",
})
OPERATOR_READS = frozenset({
    "integration_status", "integration_trust_manifest", "integration_app_verify",
    "integration_cutover_plan", "provider_status", "policy_diff",
})
OPERATOR_WRITES = frozenset({
    "edit_project", "integration_cutover", "integration_abort_batch", "integration_eject",
    "integration_retire_origin", "integration_release_owner", "playbook_v2_import",
    "playbook_activate", "set_playbook_enabled", "playbook_delete", "policy_apply",
})
HUMAN_DECISION_COMMANDS = frozenset({
    "edit_project", "integration_cutover", "integration_abort_batch", "integration_eject",
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
