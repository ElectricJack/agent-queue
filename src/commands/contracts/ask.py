"""Frozen ask inputs shared by command, tool and HTTP surfaces.

H06 fills the transport controls; their permissions and closed schemas are
reserved here so transport rollout cannot accidentally expose human authority.
"""

from typing import Any

from pydantic import Field

from src.api.models.ask import ASK_ARGS
from src.commands.contracts.models import (
    CommandContract,
    CommandPresentation,
    CommandResult,
    CommandValue,
    EffectSubject,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    ReadClause,
    SideEffectClass,
    UpdateClause,
)
from src.commands.contracts.registry import CommandRegistration
from src.commands.principal import principal_context

GATEWAY_COMMANDS = frozenset({"ask_intake", "ask_button"})
OPERATOR_COMMANDS = frozenset(
    {"ask_reconcile", "ask_cutover", "ask_retry_operation", "ask_repair_delivery"}
)
SUPERVISOR_COMMANDS = frozenset({"ask_apply_input", "ask_discussion_reply", "ask_update_context"})
READ_COMMANDS = frozenset({"ask_list", "ask_show", "ask_history"})


class AskValue(CommandValue):
    receipt: dict[str, Any] = Field(default_factory=dict)


def register_ask_contracts(registry):
    for name, args_model in ASK_ARGS.items():
        if registry.get(name):
            continue
        read = name in READ_COMMANDS
        outcomes = (
            OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
        )

        async def invoke(args, principal, command=name):
            from src.commands.contracts.builtin import _handler

            with principal_context(principal):
                result = await _handler().execute(command, args.model_dump(exclude_none=True))
            ok = result.get("success", False)
            return CommandResult(
                outcome="completed" if ok else "rejected",
                value=AskValue(receipt=result),
                summary="Completed" if ok else str(result.get("error", "Rejected")),
            )

        effects = (
            (ReadClause if read else UpdateClause)(subject=EffectSubject.CONVERSATION_INPUT),
        )
        keyed = "idempotency_key" in args_model.model_fields
        registry.register(
            CommandRegistration(
                name=name,
                invoke=invoke,
                contract=CommandContract(
                    execution=ExecutionContract(
                        name=name,
                        args_model=args_model,
                        result_model=AskValue,
                        outcomes=outcomes,
                        capability=name,
                        side_effect=SideEffectClass.READ if read else SideEffectClass.UPDATE,
                        effects=effects,
                        retry_safe=True,
                        idempotency=IdempotencySpec(
                            mode="keyed" if keyed else "natural",
                            key_field="idempotency_key" if keyed else None,
                        ),
                        sensitive_args=frozenset(
                            {"text", "interpretation", "envelope", "overall_note", "evidence"}
                            & args_model.model_fields.keys()
                        ),
                    ),
                    presentation=CommandPresentation(
                        title=name.replace("_", " ").title(),
                        summary="Scoped human request communication and decision evidence.",
                        outcome_labels={o.name: o.name.title() for o in outcomes},
                        subject_labels={"conversation_input": "the stored human input"},
                    ),
                ),
            )
        )
