"""Request-local identity of the live playbook command being invoked.

This is provenance only.  Command authority continues to come exclusively
from :mod:`src.commands.principal`; an invocation snapshot carries no policy
or caller-controlled command arguments.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from src.playbooks.artifact_ref import ArtifactRef
    from src.playbooks.executors.base import StepContext


@dataclass(frozen=True, slots=True)
class PlaybookInvocation:
    """Immutable server snapshot of one actual live command step."""

    run_id: str
    dispatch_id: str
    artifact_ref: ArtifactRef
    rule_id: str
    step_id: str
    attempt: int


_invocation_var: contextvars.ContextVar[PlaybookInvocation | None] = (
    contextvars.ContextVar("_playbook_invocation_var", default=None)
)


def current_invocation() -> PlaybookInvocation | None:
    """Return the live command invocation bound to this async context."""

    return _invocation_var.get()


@contextmanager
def _invocation_context(ctx: StepContext) -> Iterator[PlaybookInvocation]:
    """Bind a snapshot constructed only from the engine's ``StepContext``."""

    invocation = PlaybookInvocation(
        run_id=ctx.run_id,
        dispatch_id=ctx.dispatch_id,
        artifact_ref=ctx.artifact_ref,
        rule_id=ctx.rule_id,
        step_id=ctx.step_id,
        attempt=ctx.attempt,
    )
    token = _invocation_var.set(invocation)
    try:
        yield invocation
    finally:
        _invocation_var.reset(token)


__all__ = ["PlaybookInvocation", "current_invocation"]
