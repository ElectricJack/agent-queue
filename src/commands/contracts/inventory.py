"""Operator-only legacy inventory and explicit resumable import contracts.

Dry-run is the default. Apply pins an explicitly selected sealed manifest,
backup receipt and idempotency key before bounded transactions; resume and
cancel use that durable run. Inventory and apply have separate default-off flags.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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
    operation: Literal["dry-run", "apply", "resume", "cancel"] = "dry-run"
    roots: list[InventoryRootSpec] = Field(default_factory=list)
    vector_export: str | None = None
    scope_aliases: dict[str, list[str]] | None = None
    source_installation_id: str | None = None
    snapshot_id: str | None = None
    snapshot_timestamp: str | None = None
    project_id: str | None = None
    global_scope: bool = False
    manifest_content_base64: str | None = None
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    selected_item_ids: list[str] = Field(default_factory=list)
    expected_revisions: dict[str, str] = Field(default_factory=dict)
    expected_source_hashes: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    backup_receipt: str | None = Field(default=None, min_length=1)
    run_id: str | None = None
    limit: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def selection(self):
        if self.operation == "dry-run":
            if not self.roots or not all((self.source_installation_id, self.snapshot_id,
                                          self.snapshot_timestamp)):
                raise ValueError("Dry-run requires roots and snapshot identity")
        else:
            if bool(self.project_id) == self.global_scope or not self.manifest_sha256:
                raise ValueError("Choose one scope and pin the manifest hash")
            if self.operation == "apply":
                if not all((self.manifest_content_base64, self.selected_item_ids,
                            self.idempotency_key, self.backup_receipt)):
                    raise ValueError("Apply requires manifest, explicit selection, key and backup")
            elif not self.run_id:
                raise ValueError("Resume/cancel requires run_id")
        return self


class KnowledgeImportValue(CommandValue):
    success: bool | None = None
    outcome: Literal["read", "rejected", "applied", "replayed"] | None = None
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
    run_id: str | None = None
    state: str | None = None
    replay: bool = False


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
                    side_effect=SideEffectClass.COMPOSITE,
                    retry_safe=True,
                    idempotency=IdempotencySpec(mode="natural"),
                    outcomes=(
                        OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                        OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                    ),
                ),
                presentation=CommandPresentation(
                    title="Knowledge Import",
                    summary=(
                        "Scan, seal and verify a legacy import inventory. "
                        "Dry-run by default; apply/resume require explicit sealed selection."
                    ),
                    outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                ),
            ),
            _knowledge_import_invoke(KnowledgeImportValue),
        )
    )
