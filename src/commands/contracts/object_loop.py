"""V2 playbook contracts for the bounded object evaluation loop."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from src.commands.contracts.models import (
    CommandArgs, CommandContract, CommandPresentation, CommandResult, CommandValue,
    ExecutionContract, IdempotencySpec, OutcomeClass, OutcomeSpec, SideEffectClass,
)
from src.commands.contracts.registry import CommandRegistration
from src.commands.principal import principal_context
from src.object_loop.contracts import Artifact, IDENTIFIER, ScoreReceipt, SHA256


class Reservation(CommandArgs):
    usd: float = Field(ge=0, allow_inf_nan=False)
    calls: int = Field(ge=0)
    bakes: int = Field(ge=0)
    active_seconds: float = Field(ge=0, allow_inf_nan=False)


class Variant(CommandArgs):
    variant_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    title: str = Field(min_length=1, max_length=200)
    hypothesis: str = Field(min_length=1, max_length=2000)
    reservation: Reservation


class ObjectLoopStartArgs(CommandArgs):
    project_id: str
    epic_task_id: str
    object_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    attempt_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    incumbent_sha256: str = Field(pattern=SHA256)
    incumbent_artifact: Artifact | None = None
    reference_sha256: str = Field(pattern=SHA256)
    rig_sha256: str = Field(pattern=SHA256)
    scorer_sha256: str = Field(pattern=SHA256)
    render_profile_sha256: str = Field(pattern=SHA256)
    policy_sha256: str = Field(pattern=SHA256)
    brief_review_id: str = Field(min_length=1)
    brief_review_revision: int = Field(ge=1)
    brief_review_sha256: str = Field(pattern=SHA256)
    mandatory_views: list[str] = Field(min_length=1, max_length=64)
    limits: Reservation
    final_reserve: Reservation
    score_reservation: Reservation
    noise_band: float = Field(ge=0, allow_inf_nan=False)
    max_repair_rounds: int = Field(default=2, ge=0, le=8)
    max_plateau_rounds: int = Field(default=3, ge=1, le=8)
    variants: list[Variant] = Field(min_length=1, max_length=3)


class ObjectLoopReconcileArgs(CommandArgs):
    object_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    project_id: str
    expected_version: int | None = Field(default=None, ge=1)
    next_variants: list[Variant] = Field(default_factory=list, max_length=3)
    stop_reason: str | None = None


class ObjectScoreRecordArgs(CommandArgs):
    object_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    project_id: str
    expected_version: int = Field(ge=1)
    score_task_id: str
    receipts: list[ScoreReceipt] = Field(max_length=3)
    spent: Reservation | None = None
    action: Literal["continue", "checkpoint", "stop"]
    next_variants: list[Variant] = Field(default_factory=list, max_length=3)
    stop_reason: str | None = None
    review_id: str | None = None
    review_revision: int | None = None
    review_sha256: str | None = Field(default=None, pattern=SHA256)


class ObjectCheckpointReadArgs(CommandArgs):
    object_id: str = Field(min_length=1, max_length=128, pattern=IDENTIFIER)
    project_id: str


class ArtifactVerifyArgs(CommandArgs):
    """Resolve a durable artifact URI and prove the bytes behind it."""

    uri: str = Field(min_length=1, max_length=2048)
    sha256: str | None = Field(default=None, pattern=SHA256)


class ArtifactVerifyValue(CommandValue):
    uri: str | None = None
    sha256: str | None = None
    bytes: int | None = None
    kind: str | None = None
    verified: bool | None = None
    path: str | None = None


class ObjectLoopInputsArgs(CommandArgs):
    project_id: str
    limit: int = Field(default=32, ge=1, le=32)


class ObjectLoopInputsValue(CommandValue):
    starts: list[dict[str, Any]] = Field(default_factory=list)
    loops: list[dict[str, Any]] = Field(default_factory=list)


class ObjectLoopValue(CommandValue):
    object_id: str | None = None
    version: int | None = None
    state: dict[str, Any] | None = None
    created: bool | None = None
    outcome: str | None = None
    approved: bool | None = None


def register_object_loop_contracts(registry) -> None:
    loop = "Coordinate a bounded, durable object evaluation round."
    definitions = (
        ("object_loop_start", ObjectLoopStartArgs, ObjectLoopValue, SideEffectClass.CREATE, loop),
        ("object_loop_reconcile", ObjectLoopReconcileArgs, ObjectLoopValue,
         SideEffectClass.COMPOSITE, loop),
        ("object_score_record", ObjectScoreRecordArgs, ObjectLoopValue,
         SideEffectClass.UPDATE, loop),
        ("object_checkpoint_read", ObjectCheckpointReadArgs, ObjectLoopValue,
         SideEffectClass.READ, loop),
        ("object_loop_inputs", ObjectLoopInputsArgs, ObjectLoopInputsValue,
         SideEffectClass.READ, loop),
        ("artifact_verify", ArtifactVerifyArgs, ArtifactVerifyValue, SideEffectClass.READ,
         "Resolve a durable artifact URI and re-hash the bytes it names."),
    )
    for name, args_model, result_model, effect, summary in definitions:
        if registry.get(name) is not None:
            continue

        async def invoke(args, principal, name=name, result_model=result_model):
            from src.commands.contracts.builtin import _handler

            with principal_context(principal):
                raw = await _handler().execute(name, args.model_dump())
            if not raw.get("success"):
                return CommandResult(
                    outcome="rejected", value=result_model.model_construct(),
                    summary=str(raw.get("error") or "rejected"),
                )
            return CommandResult(
                outcome="completed",
                value=result_model(**{k: raw[k] for k in result_model.model_fields if k in raw}),
                summary="completed",
            )

        registry.register(CommandRegistration(
            name,
            CommandContract(
                execution=ExecutionContract(
                    name=name, args_model=args_model, result_model=result_model,
                    capability=name, side_effect=effect, retry_safe=True,
                    idempotency=IdempotencySpec(mode="natural"),
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
            invoke,
        ))
