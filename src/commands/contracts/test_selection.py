"""Typed contracts for selection records, evidence and local omission policy."""

from __future__ import annotations

from typing import Any, Literal

from src.commands.contracts.models import (
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandResult,
    CommandValue,
    CreateClause,
    EffectSubject,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    ReadClause,
    SideEffectClass,
    UpdateClause,
)
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.commands.principal import principal_context


class TestSelectArgs(CommandArgs):
    task_id: str | None = None
    claim_epoch: int | None = None
    mode: Literal["plan_only", "shadow", "enforce"] = "shadow"
    base_ref: str | None = None
    targets: list[str] = []
    narrowing_flags: list[str] = []
    jev: bool = True
    marker_policy: Literal["default", "all"] = "default"
    acceptance_commands: list[str] = []
    workspace: str | None = None
    project_id: str | None = None


class TestSelectValue(CommandValue):
    selection_id: str
    mode: str
    recorded: bool
    full_required: bool
    full_suite_authorized: bool
    final_modules: list[str]
    ordered: list[str]
    fallback_modules: list[str]
    mandatory_modules: list[str]
    static_modules: list[str]
    jev_modules: list[str] | None
    jev_status: str
    fallback_reason: str | None
    jev_used_for_omission: bool
    reasons: dict[str, list[str]]
    argv: list[list[str]]
    pending_obligations: list[dict[str, Any]]
    record: dict[str, Any]


class TestSelectionRecheckArgs(CommandArgs):
    selection_id: str


class TestSelectionRecheckValue(CommandValue):
    stale: bool
    fingerprint: str
    recorded_fingerprint: str


class TestSelectionObserveArgs(CommandArgs):
    selection_id: str
    exit_code: int
    duration_ms: int
    executed_modules: list[str]
    failed_node_ids: list[str] = []
    payload: dict[str, Any] = {}


class TestSelectionObserveValue(CommandValue):
    observation_id: str


class TestSelectionShowArgs(CommandArgs):
    selection_id: str


class TestSelectionShowValue(CommandValue):
    selection: dict[str, Any]
    observations: list[dict[str, Any]]


class TestSelectionListArgs(CommandArgs):
    project_id: str
    task_id: str | None = None
    limit: int = 50
    before: float | None = None


class TestSelectionListValue(CommandValue):
    selections: list[dict[str, Any]]


class TestSelectionPolicyArgs(CommandArgs):
    project_id: str


class TestSelectionPolicyValue(CommandValue):
    config: dict[str, Any]
    promotion: dict[str, Any] | None
    latest_digests: dict[str, str] | None


class TestSelectionPromoteArgs(CommandArgs):
    project_id: str
    model: str
    question_schema_version: int
    catalogue_digest: str
    rules_digest: str
    policy_digest: str
    evidence: dict[str, Any]


class TestSelectionPromoteValue(CommandValue):
    promotion: dict[str, Any]


class TestSelectionRevokeArgs(CommandArgs):
    promotion_id: str
    reason: str


class TestSelectionRevokeValue(CommandValue):
    revoked: bool


def _adapter(name, value_type, success_outcome):
    async def invoke(args, ctx):
        from src.commands.contracts.builtin import _handler

        # Preserve explicit workspace=null: even that is a spoofed worker workspace.
        values = args.model_dump(exclude_none=True)
        if name == "test_select" and "workspace" in args.model_fields_set:
            values["workspace"] = args.workspace
        with principal_context(ctx):
            raw = await _handler().execute(name, values)
        if raw.get("error") or raw.get("success") is False:
            return CommandResult(
                outcome="rejected",
                value=value_type.model_construct(),
                summary=str(raw.get("error") or "rejected"),
            )
        outcome = success_outcome(raw)
        return CommandResult(
            outcome=outcome,
            value=value_type(**{k: raw[k] for k in value_type.model_fields}),
            summary=outcome,
        )

    return invoke


def register_test_selection_contracts(registry: ContractRegistry) -> None:
    definitions = (
        (
            "test_select",
            TestSelectArgs,
            TestSelectValue,
            ("selected", "full_required"),
            SideEffectClass.CREATE,
            CreateClause(subject=EffectSubject.TEST_SELECTION),
            "Record a scoped smart test selection proposal.",
            lambda raw: "full_required" if raw["full_required"] else "selected",
        ),
        (
            "test_selection_recheck",
            TestSelectionRecheckArgs,
            TestSelectionRecheckValue,
            ("read",),
            SideEffectClass.READ,
            ReadClause(subject=EffectSubject.TEST_SELECTION),
            "Check whether a recorded selection snapshot is stale.",
            lambda raw: "read",
        ),
        (
            "test_selection_observe",
            TestSelectionObserveArgs,
            TestSelectionObserveValue,
            ("observed",),
            SideEffectClass.CREATE,
            CreateClause(subject=EffectSubject.TEST_SELECTION),
            "Append execution evidence to a visible selection.",
            lambda raw: "observed",
        ),
        (
            "test_selection_show",
            TestSelectionShowArgs,
            TestSelectionShowValue,
            ("read",),
            SideEffectClass.READ,
            ReadClause(subject=EffectSubject.TEST_SELECTION),
            "Read a selection and its appended observations.",
            lambda raw: "read",
        ),
        (
            "test_selection_list",
            TestSelectionListArgs,
            TestSelectionListValue,
            ("listed",),
            SideEffectClass.READ,
            ReadClause(subject=EffectSubject.TEST_SELECTION),
            "List recorded selections in one project.",
            lambda raw: "listed",
        ),
        (
            "test_selection_policy_show",
            TestSelectionPolicyArgs,
            TestSelectionPolicyValue,
            ("read",),
            SideEffectClass.READ,
            ReadClause(subject=EffectSubject.TEST_SELECTION_PROMOTION),
            "Read selection settings, active promotion and latest digests.",
            lambda raw: "read",
        ),
        (
            "test_selection_promote",
            TestSelectionPromoteArgs,
            TestSelectionPromoteValue,
            ("promoted",),
            SideEffectClass.CREATE,
            CreateClause(subject=EffectSubject.TEST_SELECTION_PROMOTION),
            "Locally promote an evaluated omission-policy identity.",
            lambda raw: "promoted",
        ),
        (
            "test_selection_revoke",
            TestSelectionRevokeArgs,
            TestSelectionRevokeValue,
            ("revoked", "noop"),
            SideEffectClass.UPDATE,
            UpdateClause(subject=EffectSubject.TEST_SELECTION_PROMOTION),
            "Locally revoke an omission-policy promotion once.",
            lambda raw: "revoked" if raw["revoked"] else "noop",
        ),
    )
    for name, args, value, successes, side_effect, clause, summary, outcome in definitions:
        outcomes = tuple(
            OutcomeSpec(name=n, classification=OutcomeClass.SUCCESS) for n in successes
        ) + (OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),)
        if registry.get(name) is None:
            registry.register(
                CommandRegistration(
                    name,
                    CommandContract(
                        execution=ExecutionContract(
                            name=name,
                            args_model=args,
                            result_model=value,
                            outcomes=outcomes,
                            capability=name,
                            side_effect=side_effect,
                            effects=(clause,),
                            idempotency=IdempotencySpec(
                                mode="natural"
                                if side_effect in {SideEffectClass.READ, SideEffectClass.UPDATE}
                                else "none"
                            ),
                            retry_safe=side_effect
                            in {SideEffectClass.READ, SideEffectClass.UPDATE},
                        ),
                        presentation=CommandPresentation(
                            title=name.replace("_", " ").title(),
                            summary=summary,
                            outcome_labels={
                                o.name: o.name.replace("_", " ").title() for o in outcomes
                            },
                            subject_labels={
                                clause.subject.value: "the "
                                + clause.subject.value.replace("_", " ")
                            },
                        ),
                    ),
                    _adapter(name, value, outcome),
                )
            )
