"""Worker-safe typed contracts for the eight knowledge_* commands (K03)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from src.commands.contracts.models import (
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandResult,
    CommandValue,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    SideEffectClass,
)
from src.commands.contracts.registry import CommandRegistration
from src.commands.principal import principal_context

CATEGORY = Literal[
    "fact", "decision", "policy", "procedure", "incident", "reference", "note"
]


class KnowledgeCreateArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=240)
    body: str
    category: CATEGORY
    summary: str | None = Field(default=None, max_length=4096)
    tags: list[str] = Field(default_factory=list, max_length=32)
    sources: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=128)
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class KnowledgeListArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    category: CATEGORY | None = None
    include_retired: bool = False
    include_disputed: bool = False
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = None


class KnowledgeShowArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    identity: str = Field(min_length=4)
    revision_id: str | None = None


class KnowledgeUpdateArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    identity: str = Field(min_length=4)
    idempotency_key: str = Field(min_length=1, max_length=128)
    if_revision: str | None = None
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    body: str | None = None
    category: CATEGORY | None = None
    summary: str | None = Field(default=None, max_length=4096)
    tags: list[str] | None = Field(default=None, max_length=32)
    sources: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    metadata: dict[str, Any] | None = None
    change_reason: str | None = Field(default=None, max_length=4096)
    valid_from: str | None = None
    valid_until: str | None = None
    recheck_at: str | None = None
    summary_of_revision: str | None = None


class KnowledgeHistoryArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    identity: str = Field(min_length=4)
    before_sequence: int | None = Field(default=None, ge=1)
    limit: int = Field(default=25, ge=1, le=100)


class KnowledgeDiffArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    identity: str = Field(min_length=4)
    from_revision: str
    to_revision: str


class KnowledgeRetireArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    identity: str = Field(min_length=4)
    reason: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=1, max_length=128)
    if_revision: str | None = None
    successor_record_id: str | None = None
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class KnowledgeRestoreArgs(CommandArgs):
    project_id: str = Field(min_length=1)
    identity: str = Field(min_length=4)
    revision_id: str
    reason: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=1, max_length=128)
    if_revision: str | None = None
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class KnowledgeValue(CommandValue):
    record_id: str | None = None
    knowledge_alias: str | None = None
    revision_id: str | None = None
    sequence: int | None = None
    content_sha256: str | None = None
    hash_version: int | None = None
    export_state: str | None = None
    index_state: str | None = None
    snapshot: dict[str, Any] | None = None
    kind: str | None = None
    revisions: list[dict[str, Any]] | None = None
    items: list[dict[str, Any]] | None = None
    next_cursor: str | None = None
    changes: list[dict[str, Any]] | None = None


def _knowledge_invoke(name: str, args_model, result_model: type[CommandValue]):
    async def invoke(args, principal):
        from src.commands.contracts.builtin import _handler

        with principal_context(principal):
            raw = await _handler().execute(name, args.model_dump(exclude_none=True))
        if raw.get("success") is False or raw.get("error"):
            return CommandResult(
                outcome="rejected",
                value=result_model.model_construct(),
                summary=str(raw.get("error") or "rejected"),
            )
        return CommandResult(
            outcome="completed",
            value=result_model(
                **{key: raw[key] for key in result_model.model_fields if key in raw}
            ),
            summary="completed",
        )

    return invoke


def register_knowledge_contracts(registry) -> None:
    specs = (
        ("knowledge_create", KnowledgeCreateArgs, KnowledgeValue, SideEffectClass.CREATE),
        ("knowledge_list", KnowledgeListArgs, KnowledgeValue, SideEffectClass.READ),
        ("knowledge_show", KnowledgeShowArgs, KnowledgeValue, SideEffectClass.READ),
        ("knowledge_update", KnowledgeUpdateArgs, KnowledgeValue, SideEffectClass.UPDATE),
        ("knowledge_history", KnowledgeHistoryArgs, KnowledgeValue, SideEffectClass.READ),
        ("knowledge_diff", KnowledgeDiffArgs, KnowledgeValue, SideEffectClass.READ),
        ("knowledge_retire", KnowledgeRetireArgs, KnowledgeValue, SideEffectClass.RESOLVE),
        ("knowledge_restore", KnowledgeRestoreArgs, KnowledgeValue, SideEffectClass.RESOLVE),
    )
    summaries = {
        "knowledge_create": "Create one active, unverified knowledge finding.",
        "knowledge_list": "List authorized knowledge metadata with a page cursor.",
        "knowledge_show": "Read an authorized knowledge snapshot at an exact revision.",
        "knowledge_update": "Revise editable knowledge fields with a concurrency token.",
        "knowledge_history": "Read the revision history of a knowledge record.",
        "knowledge_diff": "Diff two exact, readable revisions of a knowledge record.",
        "knowledge_retire": "Retire a knowledge finding with an optional successor.",
        "knowledge_restore": "Restore a knowledge finding to a retained revision.",
    }
    for name, args_model, result_model, effect in specs:
        if registry.get(name) is not None:
            continue
        keyed = name not in {
            "knowledge_list", "knowledge_show", "knowledge_history", "knowledge_diff",
        }
        registry.register(
            CommandRegistration(
                name,
                CommandContract(
                    execution=ExecutionContract(
                        name=name,
                        args_model=args_model,
                        result_model=result_model,
                        capability=name,
                        side_effect=effect,
                        retry_safe=True,
                        idempotency=IdempotencySpec(
                            mode="keyed", key_field="idempotency_key"
                        )
                        if keyed
                        else IdempotencySpec(mode="natural"),
                        outcomes=(
                            OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                        ),
                    ),
                    presentation=CommandPresentation(
                        title=name.replace("_", " ").title(),
                        summary=summaries[name],
                        outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                    ),
                ),
                _knowledge_invoke(name, args_model, result_model),
            )
        )
