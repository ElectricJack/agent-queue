"""Immutable execution identity and request-local principal context.

Playbook V2 roadmap §4 locks :class:`ExecutionPrincipal` as the
server-derived identity carried through direct API execution, orchestrator
calls, playbook runs, and agent-task delegation.

Two invariants make this useful as a security boundary:

**Server-derived, never client-supplied.** A principal is never parsed from
a request body. ``CommandHandler.execute`` builds it from the middleware's
``_scope`` and strips ``_principal`` / ``_policy`` / ``_profile_id`` /
``_capabilities`` off the args, as do both HTTP dispatch surfaces.

**Narrowing only.** :meth:`ExecutionPrincipal.narrow` intersects; there is
no widening method, and ``enforced`` is a computed property of the kind, not
a settable field — so no argument, header, or config turns a ``SESSION``
principal into a trusted one.

``TRUSTED_LOCAL`` and :meth:`ExecutionPrincipal.service` are the two
*explicit* trusted principals: the loopback CLI is the operator, and
daemon-internal callers are the server itself. Elevation is deliberately
**not** one of them — ``RequestScope.elevated`` answers "which project may
this token touch", capability answers "which commands may this profile
run", and conflating them is how one stolen supervisor token becomes
unbounded.

See ``docs/superpowers/plans/2026-09-01-playbook-v2-phase0-security.md``
§3.4–§3.5.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Iterator

from src.profiles.capabilities import DENY_ALL, CapabilityPolicy


class PrincipalKind(StrEnum):
    LOCAL = "local"  # loopback CLI, no bearer token — the trusted operator
    SERVICE = "service"  # daemon-internal: cascade, reconciler, timers
    SESSION = "session"  # an agent session bearer token
    PLAYBOOK = "playbook"  # a playbook run step (carries run/step identity)


#: Kinds whose capability policy is actually enforced at dispatch.
ENFORCED_KINDS: frozenset[PrincipalKind] = frozenset(
    {PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}
)

#: Arg keys the server owns.  Stripped at both HTTP surfaces and again in
#: ``CommandHandler.execute`` — two independent layers, because one of them
#: being bypassed is exactly the interesting failure.
SERVER_OWNED_ARG_KEYS: tuple[str, ...] = (
    "_scope",
    "_principal",
    "_policy",
    "_profile_id",
    "_capabilities",
)


@dataclass(frozen=True, slots=True)
class ExecutionPrincipal:
    """Who is running a command, and what they may run."""

    kind: PrincipalKind
    policy: CapabilityPolicy
    session_id: str | None = None
    service_name: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    profile_id: str | None = None
    elevated: bool = False
    parent_run_id: str | None = None
    parent_step_id: str | None = None
    #: How the effective policy was narrowed, one entry per narrowing.
    provenance: tuple[str, ...] = ()

    @property
    def enforced(self) -> bool:
        """Whether this principal's policy gates dispatch.

        Computed, never passed in: the trusted bypass is a property of the
        kind and cannot be requested.
        """
        return self.kind in ENFORCED_KINDS

    def narrow(self, policy: CapabilityPolicy, *, reason: str) -> "ExecutionPrincipal":
        """Intersect this principal's policy with *policy*.

        The only policy transform on the type.  Passing a broader policy
        returns the same (narrow) grant — there is no widening path.
        """
        return replace(
            self,
            policy=self.policy.intersect(policy),
            provenance=self.provenance + (reason,),
        )

    @classmethod
    def service(cls, name: str) -> "ExecutionPrincipal":
        """A daemon-internal caller: the cascade, a reconciler, a timer."""
        return cls(kind=PrincipalKind.SERVICE, policy=DENY_ALL, service_name=name)

    def describe(self) -> str:
        """Operator-facing one-liner for logs.  Never returned to an agent."""
        who = self.session_id or self.service_name or self.profile_id or "-"
        return f"{self.kind.value}:{who}"


#: The loopback CLI operator.  Not enforced.
TRUSTED_LOCAL: ExecutionPrincipal = ExecutionPrincipal(
    kind=PrincipalKind.LOCAL, policy=DENY_ALL
)


_principal_var: contextvars.ContextVar[ExecutionPrincipal | None] = contextvars.ContextVar(
    "_principal_var", default=None
)


def current_principal() -> ExecutionPrincipal | None:
    """The principal bound to this request, or ``None`` outside one."""
    return _principal_var.get()


@contextmanager
def principal_context(principal: ExecutionPrincipal) -> Iterator[ExecutionPrincipal]:
    """Bind *principal* for the duration of the block.

    Save/restore rather than set/clear, matching the ``_current_scope_var``
    discipline in ``src/commands/handler.py``: a command can dispatch another
    one inside its own body (``task_close --claim-next`` calls
    ``_cmd_task_claim``; the playbook runner re-enters ``execute`` outright),
    and an unconditional clear would strip the outer command's identity the
    moment the inner one returned.
    """
    token = _principal_var.set(principal)
    try:
        yield principal
    finally:
        _principal_var.reset(token)


def check_delegation(parent: CapabilityPolicy, child: CapabilityPolicy) -> str:
    """Empty string when ``child ⊆ parent`` in every namespace; else the reason.

    Namespaces are reported one at a time so the message names *which* kind
    of capability was escalated, not just that something was.
    """
    from src.profiles.capabilities import NAMESPACES

    labels = {
        "harness_tools": "harness tool",
        "aq_commands": "aq_command",
        "plugin_tools": "plugin_tool",
    }
    for ns in NAMESPACES:
        extra = sorted(getattr(child, ns) - getattr(parent, ns))
        if extra:
            return (
                f"child has {len(extra)} {labels[ns]}(s) not in parent's "
                f"allowlist: {extra[:5]}"
            )
    return ""


__all__ = [
    "ENFORCED_KINDS",
    "ExecutionPrincipal",
    "PrincipalKind",
    "SERVER_OWNED_ARG_KEYS",
    "TRUSTED_LOCAL",
    "check_delegation",
    "current_principal",
    "principal_context",
]
