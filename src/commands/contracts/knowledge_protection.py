"""K05 typed proposal, verification, sharing and operator erasure contracts."""

from typing import Any, Literal

from pydantic import Field

from src.commands.contracts.knowledge import KnowledgeValue, _knowledge_invoke
from src.commands.contracts.models import (
    CommandContract,
    CommandPresentation,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    SideEffectClass,
)
from src.commands.contracts.registry import CommandRegistration
from src.commands.contracts.record_scope import RecordScopeArgs


class ProtectionScope(RecordScopeArgs):
    pass


class ProposalArgs(ProtectionScope):
    snapshot: dict[str, Any]
    identity: str | None = None
    if_revision: str | None = None
    link_operations: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=128)
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class ProposalShowArgs(ProtectionScope):
    proposal_id: str


class ProposalDecideArgs(ProposalShowArgs):
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    if_revision: str | None = None
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=1, max_length=128)


class AuthorityArgs(ProtectionScope):
    identity: str
    if_revision: str | None = None
    reason: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=1, max_length=128)


class AuthorityGrantArgs(AuthorityArgs):
    review: dict[str, Any] | None = None


class VerifyArgs(AuthorityArgs):
    evidence: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    verification: Literal["verified", "disputed"] = "verified"


class ShareArgs(AuthorityArgs):
    target_project_id: str = Field(min_length=1)
    revoke: bool = False
    dry_run: bool = True


class RedactArgs(ProtectionScope):
    identity: str
    if_revision: str | None = None
    revision_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=128)
    reason_code: Literal["sensitive", "privacy", "operator_erasure"]
    dry_run: bool = True


class ProtectionValue(KnowledgeValue):
    proposal_id: str | None = None
    proposal_sha256: str | None = None
    base_revision_id: str | None = None
    state: str | None = None
    authority: dict | None = None
    redaction_id: str | None = None
    cleanup_state: dict | None = None
    affected_records: int | None = None
    affected_revisions: int | None = None
    dry_run: bool | None = None
    shared: bool | None = None


PROTECTION_COMMANDS = (
    (
        "knowledge_propose",
        ProposalArgs,
        SideEffectClass.CREATE,
        "Submit an unverified correction bound to its exact base.",
    ),
    (
        "knowledge_proposal_show",
        ProposalShowArgs,
        SideEffectClass.READ,
        "Read an authorized proposal and its exact hash.",
    ),
    (
        "knowledge_proposal_decide",
        ProposalDecideArgs,
        SideEffectClass.RESOLVE,
        "Accept or reject an exact proposal; supervisor grant required.",
    ),
    (
        "knowledge_verify",
        VerifyArgs,
        SideEffectClass.UPDATE,
        "Verify or dispute an exact revision with named evidence.",
    ),
    (
        "knowledge_authority_grant",
        AuthorityGrantArgs,
        SideEffectClass.UPDATE,
        "Grant policy authority bound to an exact verified revision and review.",
    ),
    (
        "knowledge_authority_revoke",
        AuthorityArgs,
        SideEffectClass.UPDATE,
        "Revoke policy authority without rewriting content history.",
    ),
    (
        "knowledge_share",
        ShareArgs,
        SideEffectClass.UPDATE,
        "Preview or change a global record share; explicit global authority required.",
    ),
    (
        "knowledge_redact",
        RedactArgs,
        SideEffectClass.UPDATE,
        "Preview or permanently erase selected knowledge and derived copies; local operator only.",
    ),
)


def register_protection_contracts(registry):
    for name, model, effect, summary in PROTECTION_COMMANDS:
        registry.register(
            CommandRegistration(
                name,
                CommandContract(
                    execution=ExecutionContract(
                        name=name,
                        args_model=model,
                        result_model=ProtectionValue,
                        capability=name,
                        side_effect=effect,
                        retry_safe=True,
                        idempotency=IdempotencySpec(mode="natural")
                        if effect == SideEffectClass.READ
                        else IdempotencySpec(mode="keyed", key_field="idempotency_key"),
                        outcomes=(
                            OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                        ),
                    ),
                    presentation=CommandPresentation(
                        title=name.replace("_", " ").title(),
                        summary=summary,
                        outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                    ),
                ),
                _knowledge_invoke(name, model, ProtectionValue),
            )
        )
