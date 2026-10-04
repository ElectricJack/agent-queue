"""The arguments ``provider_reroute`` and ``provider_reroute_undo`` accept.

The registered ``provider_reroute`` contract is the playbook step: a playbook
never names tasks, so it declares ``provider`` and ``dry_run`` only.  An
operator or supervisor also names tasks (D11-D16), and ``provider_reroute_undo``
has no contract at all.  These models declare every argument the two handlers
read.

``check_request_scope`` validates against them before any scope rule reads the
arguments and hands the handler the canonical form; the handler validates with
the same model.  The ids the target-scope check resolves are therefore exactly
the ids the handler acts on: an id spelled ``"B-1,"`` or ``" B-1"`` is
normalised *before* its project is looked up, not after, and an alias such as
``task_ids`` is refused rather than read past the scope check.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class _RerouteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: One task id or a list; commas and whitespace separate ids.
    task_id: list[str] | None = None
    force: bool | None = None

    @field_validator("task_id", mode="before")
    @classmethod
    def _split_task_ids(cls, value: Any) -> list[str] | None:
        # ValueError, not TypeError: pydantic reports only the former as a
        # ValidationError.
        if value is None:
            return None
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list | tuple) or not all(
            isinstance(item, str) for item in values
        ):
            raise ValueError("task_id must be a task id or a list of task ids")
        ids = [part for item in values for part in item.replace(",", " ").split()]
        return ids or None


class ProviderRerouteRequest(_RerouteArgs):
    """``provider_reroute`` from an operator, a supervisor or a playbook step."""

    provider: str | None = None
    to_profile: str | None = None
    include_paused: bool | None = None
    dry_run: bool | None = None


class ProviderRerouteUndoRequest(_RerouteArgs):
    """``provider_reroute_undo``."""

    batch_id: str | None = None
    #: The caller's project, injected by the per-project scope gate because
    #: the command has no contract.  Never a filter: the handler confines a
    #: project-scoped caller by its scope, not by this argument.
    project_id: str | None = None


REROUTE_ARGUMENT_MODELS: Final[dict[str, type[_RerouteArgs]]] = {
    "provider_reroute": ProviderRerouteRequest,
    "provider_reroute_undo": ProviderRerouteUndoRequest,
}


def canonical_reroute_args(command: str, args: dict) -> dict[str, Any]:
    """*args* validated against *command*'s model, without the unset fields.

    Raises :class:`pydantic.ValidationError` for an argument the model does
    not declare or a value it cannot read.
    """
    model = REROUTE_ARGUMENT_MODELS[command]
    return model.model_validate(args).model_dump(exclude_none=True)


def validation_error_text(error: ValidationError) -> str:
    """One line per refused argument: ``task_ids: Extra inputs are not permitted``."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'arguments'}: {item['msg']}"
        for item in error.errors()
    )


__all__ = [
    "REROUTE_ARGUMENT_MODELS",
    "ProviderRerouteRequest",
    "ProviderRerouteUndoRequest",
    "canonical_reroute_args",
    "validation_error_text",
]
