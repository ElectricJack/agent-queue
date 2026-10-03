"""Typed contracts for the consolidated integration operator surface (spec §5.1).

The reconciler leaves four human decisions — answer a gate, authorize a root,
activate a policy, hold a subject — and two diagnostics, ``status`` and
``explain``.  ``integration_authorize_root``, ``integration_status`` and
``integration_flush`` keep their existing contracts; this module adds the
other four.  Every older control is listed in
:mod:`src.commands.integration_legacy` with its replacement or removal gate.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from src.commands.contracts.integration import (
    IntegrationOperationalValue,
    _hierarchy_adapter,
    _operational_contract,
)
from src.commands.contracts.models import CommandArgs, CommandValue, SideEffectClass
from src.commands.contracts.registry import CommandRegistration, ContractRegistry


class IntegrationGateAnswerArgs(CommandArgs):
    """A human's answer to one open integration gate."""

    gate_id: str = Field(min_length=1)
    choice: str = Field(min_length=1)
    #: Injected by the scope gate for a per-project supervisor; checked, not used.
    project_id: str | None = None


class IntegrationGateAnswerValue(CommandValue):
    gate_id: str | None = None
    subject_id: str | None = None
    project_id: str | None = None
    choice: str | None = None
    answered_by: str | None = None
    reason: str | None = None


GATE_ANSWER_OUTCOMES = ("answered", "refused", "not_found")


class IntegrationPolicyActivateArgs(CommandArgs):
    """Select a project's integration mode and, optionally, its policy.

    ``policy`` is the project's whole hierarchical integration policy (the
    pinned route artifacts per boundary) and is written only while the project
    is disabled and drained; ``development`` takes the development policy.
    """

    project_id: str = Field(min_length=1)
    mode: Literal["disabled", "observe", "hierarchy", "train", "development"]
    expected_generation: int | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1)
    policy: dict[str, Any] | None = None
    waiver_id: str | None = Field(default=None, min_length=1)
    interval_seconds: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def mode_inputs(self) -> IntegrationPolicyActivateArgs:
        if self.mode == "development":
            if self.policy is None:
                raise ValueError("development mode requires its policy")
        elif self.expected_generation is None:
            raise ValueError("expected_generation is required (see integration status)")
        if self.interval_seconds is not None and self.mode != "train":
            raise ValueError("interval_seconds is only valid with mode train")
        return self


class IntegrationPolicyActivateValue(IntegrationOperationalValue):
    #: Fields the configure step wrote before the mode change, if any.
    fields: tuple[str, ...] = ()
    configured_generation: int | None = None


POLICY_ACTIVATE_OUTCOMES = (
    "configured", "enabled", "disabled", "draining", "blocked", "busy", "stale", "not_found",
)


class IntegrationHoldArgs(CommandArgs):
    """Hold, or release the hold on, a subject's or task's integration."""

    target: str = Field(min_length=1)
    reason: str | None = Field(default=None, min_length=1)
    release: bool = False
    project_id: str | None = None

    @model_validator(mode="after")
    def holding_names_a_reason(self) -> IntegrationHoldArgs:
        if not self.release and (self.reason is None or not self.reason.strip()):
            raise ValueError("a hold requires a reason")
        return self


class IntegrationHoldValue(CommandValue):
    target: str | None = None
    project_id: str | None = None
    subject_id: str | None = None
    task_ids: tuple[str, ...] = ()
    reason: str | None = None
    held_by: str | None = None
    woken_subjects: int = 0


HOLD_OUTCOMES = ("held", "already_held", "released", "not_held", "not_found", "invalid")


class IntegrationExplainArgs(CommandArgs):
    """Why a subject is where it is: its last recorded decisions."""

    target: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=50)
    project_id: str | None = None


class IntegrationExplainValue(CommandValue):
    target: str | None = None
    project_id: str | None = None
    subjects: tuple[dict[str, Any], ...] = ()


EXPLAIN_OUTCOMES = ("explained", "not_found")


def _surface_contract(name, args_model, outcomes, successes, side_effect, value_model, summary):
    contract = _operational_contract(
        name, args_model, outcomes, successes=frozenset(successes),
        side_effect=side_effect, result_model=value_model,
    )
    return contract.model_copy(update={
        "presentation": contract.presentation.model_copy(update={"summary": summary}),
    })


_SURFACE = (
    ("integration_gate_answer", IntegrationGateAnswerArgs, GATE_ANSWER_OUTCOMES, {"answered"},
     SideEffectClass.UPDATE, IntegrationGateAnswerValue,
     "Answer an open integration gate; only a verified human operator's answer binds."),
    ("integration_policy_activate", IntegrationPolicyActivateArgs, POLICY_ACTIVATE_OUTCOMES,
     {"configured", "enabled", "disabled", "draining"}, SideEffectClass.COMPOSITE,
     IntegrationPolicyActivateValue,
     "Activate a project's integration mode and policy behind its generation fence."),
    ("integration_hold", IntegrationHoldArgs, HOLD_OUTCOMES,
     {"held", "already_held", "released", "not_held"}, SideEffectClass.UPDATE,
     IntegrationHoldValue,
     "Hold or release a subject's integration; the policy may only wait on a hold."),
    ("integration_explain", IntegrationExplainArgs, EXPLAIN_OUTCOMES, {"explained"},
     SideEffectClass.READ, IntegrationExplainValue,
     "The last recorded reconciler decisions for a subject and why."),
)


def _adapter(name, value_model, outcomes):
    async def invoke(args, ctx):
        return await _hierarchy_adapter(name, args, ctx, value_model, set(outcomes))

    return invoke


def register_integration_surface_contracts(registry: ContractRegistry) -> None:
    for name, args_model, outcomes, successes, effect, value_model, summary in _SURFACE:
        if registry.get(name) is None:
            contract = _surface_contract(
                name, args_model, outcomes, successes, effect, value_model, summary
            )
            registry.register(
                CommandRegistration(name, contract, _adapter(name, value_model, outcomes))
            )
