"""PR-backed promotion intent commands, registered independently of flow tooling."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

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
    from_task: str | None = Field(default=None, description="Completed hotfix task to promote.")

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


class PromotePrepareArgs(PromoteProjectArgs):
    step_id: str = Field(min_length=1)
    version: str | None = Field(default=None, min_length=1)
    bump: Literal["minor", "patch"] | None = None
    from_task: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def one_version_choice(self):
        if (self.version is None) == (self.bump is None):
            raise ValueError("Choose exactly one of --version or --bump")
        return self


class PromotionNotesInputArgs(PromoteProjectArgs):
    step_id: str = Field(min_length=1)
    source_sha: str | None = None

    _exact_source = field_validator("source_sha")(PromoteRequestArgs.exact_source.__func__)
class PromoteHotfixArgs(PromoteProjectArgs):
    step_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str | None = None
    from_task: str | None = None
    version: str | None = None


class PromotionPolicyInputArgs(PromoteRequestArgs):
    pass


class BackmergeSourceArgs(PromoteProjectArgs):
    step_id: str = Field(min_length=1)


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
    version: str | None = None
    notes_input: dict[str, Any] | None = None
    draft: str | None = None
    notes: str | None = None
    policy: dict[str, Any] | None = None
    backmerges: list[dict[str, Any]] = Field(default_factory=list)


REFUSALS = (
    "backmerge_ledger_invalid", "hotfix_patch_required",
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
    "promotion_source_red",
    "promotion_source_pending",
    "promotion_source_unavailable",
    "promotion_source_untrusted",
    "promotion_train_required",
    "prepare_in_progress",
    "version_not_increasing",
    "notes_stale",
    "notes_range_invalid",
    "notes_range_too_large",
    "notes_source_missing",
    "promotion_body_too_large",
)


def register_promote_contracts(registry):
    from src.commands.contracts.integration import _hierarchy_adapter, _operational_contract

    for name, args_model, successes, read in (
        ("integration_promotion_policy_input", PromotionPolicyInputArgs, ("policy_input",), True),
        ("promote_prepare", PromotePrepareArgs, ("prepared",), False),
        ("integration_promotion_notes_input", PromotionNotesInputArgs, ("notes_input",), True),
        ("promote_request", PromoteRequestArgs, ("requested", "already_requested"), False),
        ("promote_hotfix", PromoteHotfixArgs, ("hotfix_filed",), False),
        ("integration_backmerge_source", BackmergeSourceArgs, ("backmerges_authored",), False),
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
            "integration_promotion_policy_input": "Read pinned source and outstanding promotion facts for reviewed policy.",
            "promote_prepare": "File ordinary release preparation on the repository default branch.",
            "integration_promotion_notes_input": "Assemble immutable promotion notes from full Git history.",
            "promote_hotfix": "File a hotfix based on a promotion target, through ordinary task routing.",
            "integration_backmerge_source": "Author gated back-merge sources and fast-forward intents down the chain.",
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
