"""Typed contracts for the §4 supervisor-authored digest boundary.

Only the reconciliation command needs a contract: the playbook calls it, and a
playbook step is validated against the registry before it runs.  ``digest_facts``
and ``digest_post`` are the *author* surface the supervisor drives from its own
session, checked by the handler against the live principal rather than by a
contract, exactly as ``report_brief`` and ``report_submit`` are.
"""

from __future__ import annotations

from typing import Any

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
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.commands.principal import principal_context


class DigestRequestArgs(CommandArgs):
    now: float | None = None


class DigestRequestValue(CommandValue):
    requested: int = 0
    cancelled: int = 0


#: A refusal still answers with the shape the executor round-trips; an empty
#: ``model_construct()`` would surface as ``contract_violation`` and hide the
#: handler's reason (the mistake ``morning_report_tick`` already paid for).
_REJECTED_VALUES: dict[type[CommandValue], Any] = {
    DigestRequestValue: lambda code: {"requested": 0, "cancelled": 0},
}


async def _invoke_digest_request(args: DigestRequestArgs, principal: Any):
    from src.commands.contracts.builtin import _handler

    with principal_context(principal):
        raw = await _handler().execute("digest_request", args.model_dump(exclude_none=True))
    if raw.get("success") is False or raw.get("error"):
        return CommandResult(
            outcome="rejected",
            value=DigestRequestValue(**_REJECTED_VALUES[DigestRequestValue](
                str(raw.get("error_code") or "rejected")
            )),
            summary=str(raw.get("error") or "rejected"),
        )
    return CommandResult(
        outcome="completed",
        value=DigestRequestValue(
            requested=int(raw.get("requested") or 0),
            cancelled=int(raw.get("cancelled") or 0),
        ),
        summary="completed",
    )


def register_digest_contracts(registry: ContractRegistry) -> None:
    if registry.get("digest_request") is None:
        registry.register(
            CommandRegistration(
                name="digest_request",
                contract=CommandContract(
                    execution=ExecutionContract(
                        name="digest_request",
                        args_model=DigestRequestArgs,
                        result_model=DigestRequestValue,
                        outcomes=(
                            OutcomeSpec(name="completed", classification=OutcomeClass.SUCCESS),
                            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
                        ),
                        capability="digest_request",
                        side_effect=SideEffectClass.CREATE,
                        idempotency=IdempotencySpec(mode="natural"),
                        retry_safe=True,
                    ),
                    presentation=CommandPresentation(
                        title="Digest Request",
                        summary=(
                            "Queue one supervisor author turn per reserved digest window, "
                            "releasing held windows when supervisor authoring is off."
                        ),
                        outcome_labels={"completed": "Completed", "rejected": "Rejected"},
                    ),
                ),
                invoke=_invoke_digest_request,
            )
        )


__all__ = [
    "DigestRequestArgs",
    "DigestRequestValue",
    "register_digest_contracts",
]