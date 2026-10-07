"""PR-backed promotion intent commands, registered independently of flow tooling."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from src.commands.contracts.models import CommandArgs, CommandValue, SideEffectClass
from src.commands.contracts.registry import CommandRegistration
from src.git.manager import is_valid_git_oid


class PromoteProjectArgs(CommandArgs):
    project_id: str = Field(
        min_length=1, description="Project owning the configured promotion flow."
    )
    task_id: str | None = None
    session_id: str | None = None


class PromoteRequestArgs(PromoteProjectArgs):
    step_id: str = Field(min_length=1, description="Configured promotion step id.")
    source_sha: str | None = Field(
        default=None, description="Exact commit to pin; defaults to the source branch tip."
    )
    version: str | None = Field(
        default=None,
        min_length=1,
        description="Version at the pinned source; required for custom tags.",
    )
    notes_reviewed: bool = Field(
        default=False, description="Acknowledge reading the notes at the pinned source."
    )

    @field_validator("source_sha")
    @classmethod
    def exact_source(cls, value):
        if value is not None and not is_valid_git_oid(value):
            raise ValueError("source_sha must be an exact lowercase Git OID")
        return value


class PromoteIntentArgs(PromoteProjectArgs):
    request_id: str = Field(
        min_length=1, description="Promotion request identity returned by request."
    )


class PromoteReadArgs(PromoteProjectArgs):
    step_id: str | None = Field(default=None, description="Restrict cached results to this step.")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum cached intents to return.")


class PromoteValue(CommandValue):
    project_id: str | None = None
    request_id: str | None = None
    batch_id: str | None = None
    task_id: str | None = None
    intent: str | None = None
    pr_url: str | None = None
    promotion: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    flow: list[dict[str, Any]] | None = None
    promotions: list[dict[str, Any]] = Field(default_factory=list)
    evidence_source: str | None = None
    retry_at: float | None = None


REFUSALS = (
    "not_found",
    "unavailable",
    "rate_limited",
    "promotion_flow_empty",
    "promotion_flow_invalid",
    "step_not_found",
    "promotion_flow_changed",
    "promotion_source_not_on_chain",
    "promotion_not_fast_forward",
    "promotion_in_progress",
    "tag_exists",
    "backmerge_pending",
    "promotion_ref_conflict",
    "version_mismatch",
    "step_not_versioned",
    "notes_not_reviewed",
    "promotion_pr_identity_mismatch",
    "promotion_requester_identity_missing",
    "promotion_intent_invalid",
    "approval_not_required",
    "promotion_not_open",
    "promotion_review_invalid",
    "promotion_publish_started",
)


def register_promote_contracts(registry):
    from src.commands.contracts.integration import _hierarchy_adapter, _operational_contract

    for name, args_model, successes, read in (
        ("promote_request", PromoteRequestArgs, ("requested", "already_requested"), False),
        ("promote_approve", PromoteIntentArgs, ("approved", "already_approved"), False),
        ("promote_cancel", PromoteIntentArgs, ("cancelled", "already_cancelled"), False),
        ("promote_status", PromoteReadArgs, ("status",), True),
        ("promote_list", PromoteReadArgs, ("listed",), True),
    ):
        outcomes = successes + REFUSALS
        contract = _operational_contract(
            name,
            args_model,
            outcomes,
            successes=frozenset(successes),
            side_effect=SideEffectClass.READ if read else SideEffectClass.COMPOSITE,
            result_model=PromoteValue,
        )
        summaries = {
            "promote_request": "Open an idempotent promotion intent and a PR pinned to its source commit.",
            "promote_approve": "Post a pinned GitHub approval using the authenticated human gh login.",
            "promote_cancel": "Close an unpublished promotion PR and abort its intent.",
            "promote_status": "Read promotion intents and cached check and PR review evidence.",
            "promote_list": "List promotion history using the local evidence cache.",
        }
        contract = contract.model_copy(
            update={
                "presentation": contract.presentation.model_copy(
                    update={"summary": summaries[name]}
                )
            }
        )

        async def adapter(args, ctx, _name=name, _outcomes=outcomes):
            return await _hierarchy_adapter(_name, args, ctx, PromoteValue, set(_outcomes))

        if registry.get(name) is None:
            registry.register(CommandRegistration(name, contract, adapter))
