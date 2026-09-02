"""Side-effect-free preview seam for future dry-run executors."""

from __future__ import annotations

from typing import Any

from src.commands.contracts.models import (
    CommandArgs,
    CommandContract,
    CommandResult,
    CommandValue,
    redact_args,
)


def preview_stub(
    contract: CommandContract[Any, Any], args: CommandArgs
) -> CommandResult[CommandValue]:
    """A non-operational, redacted preview; built-ins register no preview yet."""
    return CommandResult(
        outcome="contract_violation",
        value=contract.execution.result_model.model_construct(),
        summary=str(redact_args(contract, args.model_dump())),
    )
