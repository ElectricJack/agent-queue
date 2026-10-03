"""Operator-only, read-only legacy import inventory dry-run (K06).

``knowledge_import`` runs the offline scanner against operator-supplied roots,
seals the manifest, verifies the seal and returns the hash-pinned
reconciliation report (counts, items, legacy mappings, identities) in memory.
It never writes a row to ``record_import_runs``, ``record_legacy_mappings``
or ``record_import_items`` — those belong to K07's apply path. The feature is
doubly gated: the local operator AND ``knowledge.import_inventory.enabled``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

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


class InventoryRootSpec(BaseModel):
    """One scan root: an absolute path with a stable, path-free identity."""

    model_config = {"frozen": True}
    root_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    source_scope: str = Field(min_length=1)
    source_kind: str = "memory"
    relative_paths: list[str] | None = None


class KnowledgeImportArgs(CommandArgs):
    roots: list[InventoryRootSpec] = Field(min_length=1)
    vector_export: str | None = None
    scope_aliases: dict[str, list[str]] | None = None
    source_installation_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    snapshot_timestamp: str = Field(min_length=1)


class KnowledgeImportValue(CommandValue):
    success: bool | None = None
    outcome: Literal["read", "rejected"] | None = None
    error_code: str | None = None
    error: str | None = None
    source_installation_id: str | None = None
    snapshot_id: str | None = None
    snapshot_timestamp: str | None = None
    manifest_sha256: str | None = None
    manifest_content_base64: str | None = None
    vector_observation: str | None = None
    counts: dict[str, Any] | None = None
    items: list[dict[str, Any]] | None = None
    mappings: list[dict[str, Any]] | None = None
    identities: list[dict[str, Any]] | None = None


def _knowledge_import_invoke(result_model: type[CommandValue]):
    async def invoke(args, principal):
        from src.commands.contracts.builtin import _handler

        with principal_context(principal):
            raw = await _handler().execute(
                "knowledge_import", args.model_dump(exclude_none=True)
            )
        if raw.get("success") is False or raw.get("error"):
            return CommandResult(
                outcome="rejected",
                value=result_model.model_construct(
                    success=False,
                    outcome="rejected",
                    error_code=raw.get("error_code"),
                    error=raw.get("error"),
                ),
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


def register_inventory_contracts(registry) -> None:
    name = "knowledge_import"
    if registry.get(name) is not None:
        return
    registry.register(
        CommandRegistration(
            name,
            CommandContract(
                execution=ExecutionContract(
                    name=name,
                    args_model=KnowledgeImportArgs,
                    result_model=KnowledgeImportValue,
                    capability=name,
                    side_effect=SideEffectClass.READ,
                    retry_safe=True,
                    idempotency=IdempotencySpec(mode="natural"),
                    outcomes=(
                        OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                        OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                    ),
                ),
                presentation=CommandPresentation(
                    title="Knowledge Import (dry-run)",
                    summary=(
                        "Scan, seal and verify a legacy import inventory. "
                        "Read-only by default; nothing is applied or written."
                    ),
                    outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                ),
            ),
            _knowledge_import_invoke(KnowledgeImportValue),
        )
    )
