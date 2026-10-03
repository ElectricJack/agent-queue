"""Worker-safe typed contracts for record reads, links, and capabilities (K03)."""

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
from src.commands.contracts.record_scope import RecordScopeArgs
from src.commands.principal import principal_context


class RecordShowArgs(RecordScopeArgs):
    identity: str = Field(min_length=4)
    revision_id: str | None = None


class RecordSearchArgs(RecordScopeArgs):
    query: str = Field(default="", max_length=512)
    category: str | None = None
    include_retired: bool = False
    include_disputed: bool = False
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = None


class RecordCapabilitiesArgs(CommandArgs):
    project_id: str | None = None


class RecordRepairArgs(CommandArgs):
    operation: Literal["backfill-task-mappings", "replay-outbox"]
    dry_run: bool = True
    event_id: str | None = None
    max_batches: int = Field(default=2, ge=1, le=20)


class LinkCreateArgs(RecordScopeArgs):
    identity: str = Field(min_length=4)
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=128)
    if_revision: str | None = None
    if_link_token: str | None = None
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class LinkListArgs(RecordScopeArgs):
    identity: str = Field(min_length=4)
    revision_id: str | None = None


class LinkRemoveArgs(RecordScopeArgs):
    identity: str = Field(min_length=4)
    link_id: str
    idempotency_key: str = Field(min_length=1, max_length=128)
    if_revision: str | None = None
    if_link_token: str | None = None
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)


class RecordValue(CommandValue):
    record_id: str | None = None
    kind: str | None = None
    knowledge_alias: str | None = None
    revision_id: str | None = None
    sequence: int | None = None
    content_sha256: str | None = None
    hash_version: int | None = None
    link_token: str | None = None
    task: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    items: list[dict[str, Any]] | None = None
    next_cursor: str | None = None
    links: list[dict[str, Any]] | None = None
    links_out: list[dict[str, Any]] | None = None
    links_in: list[dict[str, Any]] | None = None
    changed: list[str] | None = None
    capabilities: dict[str, Any] | None = None
    dry_run: bool | None = None
    inventory: dict[str, Any] | None = None
    batches: list[dict[str, Any]] | None = None
    done: bool | None = None
    eligible: bool | None = None


def _record_invoke(name: str, result_model: type[CommandValue]):
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


def register_record_contracts(registry) -> None:
    specs = (
        ("record_show", RecordShowArgs, RecordValue, SideEffectClass.READ),
        ("record_search", RecordSearchArgs, RecordValue, SideEffectClass.READ),
        ("record_capabilities", RecordCapabilitiesArgs, RecordValue, SideEffectClass.READ),
        ("record_repair", RecordRepairArgs, RecordValue, SideEffectClass.UPDATE),
        ("link_create", LinkCreateArgs, RecordValue, SideEffectClass.LINK),
        ("link_list", LinkListArgs, RecordValue, SideEffectClass.READ),
        ("link_remove", LinkRemoveArgs, RecordValue, SideEffectClass.LINK),
    )
    summaries = {
        "record_show": "Read a record by identity, pinned to a revision when knowledge.",
        "record_search": "Search authorized records with a bounded query.",
        "record_capabilities": "Describe what this caller may do with records.",
        "record_repair": "Inspect or apply bounded task mapping backfill or outbox replay.",
        "link_create": "Add, update, or remove typed record links in one batch.",
        "link_list": "List the typed links on a record.",
        "link_remove": "Remove one typed record link.",
    }
    keyed = {
        "link_create": "idempotency_key",
        "link_remove": "idempotency_key",
    }
    for name, args_model, result_model, effect in specs:
        if registry.get(name) is not None:
            continue
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
                        idempotency=IdempotencySpec(mode="keyed", key_field=keyed[name])
                        if name in keyed
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
                _record_invoke(name, result_model),
            )
        )
