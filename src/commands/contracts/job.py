"""Typed public contracts for finite managed jobs."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from src.agent_waits import AgentWaitRecord
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


class JobSubmitArgs(CommandArgs):
    project_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    claim_epoch: int | None = Field(default=None, ge=0, strict=True)
    preset: str = Field(min_length=1)
    argv: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=1, max_length=200)
    wait: bool = False


class JobGetArgs(CommandArgs):
    job_id: str = Field(min_length=1)


class JobListArgs(CommandArgs):
    project_id: str | None = None
    task_id: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class JobResultArgs(JobGetArgs):
    max_bytes: int = Field(default=8192, ge=0, le=8192)


class JobLogsArgs(JobGetArgs):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=65536, ge=1, le=1048576)


class JobRetainArgs(JobGetArgs):
    """Retain a completed capture under durable artifact identities.

    Bounded on purpose: the capture's per-view PNGs and its receipt are what a
    ``ScoreReceipt`` names, while the identity planes and the adapter's own
    transcript stay behind ``aq job logs`` unless they are asked for.
    """

    # Scope fields the session gate injects; a worker cannot nominate others.
    # Declared, not honoured: ``job_id`` is the only identity this command acts
    # on, and ``_job_for_scope`` fences it to the caller's own job.
    project_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    views: list[str] = Field(default_factory=list, max_length=64)
    include_channels: bool = False


class JobValue(CommandValue):
    job: dict[str, Any]
    wait: AgentWaitRecord | None = None
    next_step: str | None = None


class JobListValue(CommandValue):
    jobs: list[dict[str, Any]]


class JobResultValue(CommandValue):
    result: dict[str, Any] | None


class JobLogsValue(CommandValue):
    chunks: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    next: int
    seen: int


class JobRetainValue(CommandValue):
    job_id: str
    candidate_sha256: str | None = None
    candidate_artifact: dict[str, Any]
    rig_sha256: str | None = None
    render_profile: dict[str, Any] | None = None
    render_profile_sha256: str | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    captures: list[dict[str, Any]] = Field(default_factory=list)
    next_step: str | None = None


def register_job_contracts(registry):
    for name, args_model, result_model, effect in (
        ("job_submit", JobSubmitArgs, JobValue, SideEffectClass.CREATE),
        ("job_get", JobGetArgs, JobValue, SideEffectClass.READ),
        ("job_list", JobListArgs, JobListValue, SideEffectClass.READ),
        ("job_cancel", JobGetArgs, JobValue, SideEffectClass.RESOLVE),
        ("job_result", JobResultArgs, JobResultValue, SideEffectClass.READ),
        ("job_logs", JobLogsArgs, JobLogsValue, SideEffectClass.READ),
        ("job_retain", JobRetainArgs, JobRetainValue, SideEffectClass.CREATE),
    ):
        if registry.get(name) is not None:
            continue

        async def invoke(args, principal, name=name, result_model=result_model):
            from src.commands.contracts.builtin import _handler

            with principal_context(principal):
                raw = await _handler().execute(name, args.model_dump(exclude_none=True))
            if not raw.get("success"):
                return CommandResult(
                    outcome="rejected",
                    value=result_model.model_construct(),
                    summary=str(raw.get("error") or "rejected"),
                )
            return CommandResult(
                outcome="completed",
                value=result_model(**{k: raw[k] for k in result_model.model_fields if k in raw}),
                summary="completed",
            )

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
                        idempotency=IdempotencySpec(mode="keyed", key_field="idempotency_key")
                        if name == "job_submit"
                        else IdempotencySpec(mode="natural"),
                        outcomes=(
                            OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                        ),
                    ),
                    presentation=CommandPresentation(
                        title=name.replace("_", " ").title(),
                        summary={
                            "job_submit": "Submit a finite preset, optionally with an atomic durable wait.",
                            "job_get": "Read a scoped managed job.",
                            "job_list": "List this owner's managed jobs.",
                            "job_cancel": "Cancel a job and verify cleanup before releasing its pin.",
                            "job_result": "Read a job's immutable result and bounded excerpt.",
                            "job_logs": "Read retained output ranges with explicit gaps.",
                            "job_retain": (
                                "Retain a completed capture as durable artifact identities, "
                                "with its candidate artifact and render profile."
                            ),
                        }[name],
                        outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                    ),
                ),
                invoke,
            )
        )
