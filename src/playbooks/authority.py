"""Run-bound effect authority for trusted-execution playbook artifacts.

A run's command effects act for the caller that *started* it, bounded by
what the accepted immutable artifact grants
(:func:`~src.playbooks.definition.granted_aq_commands`) and by the
artifact's scope — never for whoever happens to drive the run later.  An
operator resolving a paused run (``retry`` after a crash, say) authorizes
the control decision only; the effect it lets proceed is re-derived from
the recorded caller, so recovery cannot silently widen a session's or a
project playbook's run to the operator's trusted-local authority, and a
trusted manual start still gets exactly the artifact's grant.

The caller record lives on :attr:`RunSnapshot.caller`.  It is written by the
engine from the authenticated principal at run creation, never from the
event, the context or any authored value, and it carries no secret: no
session instance token, only identity, scope restriction and the
caller's AQ-command grant already intersected with the artifact's.

Legacy artifacts (no ``trusted_execution``) keep the walk principal
unchanged and record nothing, so their snapshot and receipt bytes do not
move.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.playbooks.definition import granted_aq_commands
from src.profiles.capabilities import DENY_ALL, CapabilityPolicy

#: Provenance of a principal that could not be re-derived for an effect.
RUN_CALLER_MISSING = "run-caller-missing"
RUN_CALLER_UNRESOLVED = "run-caller-unresolved"

_IDENTITY_FIELDS = ("session_id", "service_name", "task_id", "project_id", "profile_id")
_TRUSTED_KINDS = frozenset({PrincipalKind.LOCAL, PrincipalKind.SERVICE})


def binds_run_authority(artifact: Any) -> bool:
    """Whether *artifact*'s effects run under run-bound authority."""
    return getattr(artifact, "trusted_execution", None) is not None


def _artifact_policy(artifact: Any) -> CapabilityPolicy:
    granted = sorted(granted_aq_commands(artifact))
    # A command step may name a plugin command; membership is by exact name,
    # so granting the same names in the plugin namespace widens nothing.
    return CapabilityPolicy.from_namespaces(aq_commands=granted, plugin_tools=granted)


def caller_record(principal: Any, artifact: Any) -> dict[str, Any] | None:
    """The server-derived caller identity a gated run persists, or ``None``."""
    if not binds_run_authority(artifact) or not isinstance(principal, ExecutionPrincipal):
        return None
    record: dict[str, Any] = {"kind": principal.kind.value}
    for name in _IDENTITY_FIELDS:
        record[name] = getattr(principal, name)
    record["elevated"] = principal.elevated
    if principal.enforced:
        granted = granted_aq_commands(artifact)
        record["aq_commands"] = sorted(principal.policy.aq_commands & granted)
    return record


def public_caller(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The record an operator surface may show: identity, never a grant list."""
    if record is None:
        return None
    return {key: value for key, value in record.items() if key != "aq_commands"}


def bind(caller: ExecutionPrincipal, artifact: Any, run_id: str, step_id: str) -> ExecutionPrincipal:
    """The PLAYBOOK principal one step's effect runs under.

    A trusted local or service caller has no grant of its own to narrow, so
    the artifact's grant and scope *are* the authority.  Every other caller
    only narrows: its own policy intersected with the artifact's, its own
    project restriction kept, and an unrestricted caller restricted to a
    project artifact's project.
    """
    policy = _artifact_policy(artifact)
    scope = getattr(artifact, "scope", None)
    scope_type = getattr(scope, "type", None)
    if caller.kind in _TRUSTED_KINDS:
        principal = replace(
            caller,
            policy=policy,
            project_id=scope.project_id if scope_type == "project" else None,
            profile_id=scope.agent_type if scope_type == "agent_type" else None,
        )
    else:
        principal = caller.narrow(policy, reason="run-bound")
        if principal.project_id is None and scope_type == "project":
            principal = replace(principal, project_id=scope.project_id)
    return replace(
        principal,
        kind=PrincipalKind.PLAYBOOK,
        session_instance_token=None,
        parent_run_id=run_id,
        parent_step_id=step_id,
    )


def _closed(snapshot: Any, reason: str, record: Mapping[str, Any] | None = None) -> ExecutionPrincipal:
    record = record or {}
    return ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=DENY_ALL,
        project_id=record.get("project_id"),
        profile_id=record.get("profile_id"),
        session_id=record.get("session_id"),
        parent_run_id=snapshot.run_id,
        provenance=(reason,),
    )


def _recorded_policy(record: Mapping[str, Any]) -> CapabilityPolicy:
    granted = sorted(str(name) for name in record.get("aq_commands") or ())
    return CapabilityPolicy.from_namespaces(aq_commands=granted, plugin_tools=granted)


def _handler(services: Any) -> Any:
    """The CommandHandler the run's command steps dispatch through."""
    handler = getattr(services, "handler", None)
    if handler is not None:
        return handler
    from src.commands.contracts import builtin

    return builtin._handler() if builtin.handler_provider_installed() else None


def _matches(principal: ExecutionPrincipal, record: Mapping[str, Any]) -> bool:
    return principal.kind.value == record.get("kind") and all(
        getattr(principal, name) == record.get(name) for name in _IDENTITY_FIELDS
    )


async def _revalidate(record: Mapping[str, Any], snapshot: Any, services: Any) -> ExecutionPrincipal:
    """Re-derive the recorded caller's *current* authority."""
    try:
        kind = PrincipalKind(record.get("kind"))
    except ValueError:
        return _closed(snapshot, RUN_CALLER_UNRESOLVED, record)
    identity = {name: record.get(name) for name in _IDENTITY_FIELDS}
    elevated = bool(record.get("elevated"))
    if kind in _TRUSTED_KINDS:
        return ExecutionPrincipal(kind=kind, policy=DENY_ALL, elevated=elevated, **identity)
    recorded = _recorded_policy(record)
    if kind is PrincipalKind.SESSION:
        derive = getattr(_handler(services), "_principal_from_scope", None)
        if derive is None:
            return _closed(snapshot, RUN_CALLER_UNRESOLVED, record)
        current = await derive({
            "kind": "session",
            "session_id": identity["session_id"],
            "task_id": identity["task_id"],
            "project_id": identity["project_id"],
            "elevated": elevated,
        })
        if not isinstance(current, ExecutionPrincipal) or current.kind is not PrincipalKind.SESSION:
            return _closed(snapshot, RUN_CALLER_UNRESOLVED, record)
        return current.narrow(recorded, reason="run-caller")
    # A playbook caller's authority is itself run-bound; its record is the
    # parent's grant already intersected with this artifact's.
    return ExecutionPrincipal(kind=kind, policy=recorded, elevated=elevated, **identity)


async def effect_principal(
    snapshot: Any, artifact: Any, live: Any, services: Any, step_id: str
) -> Any:
    """The principal a gated step's effect runs under at this attempt.

    The live driver's own principal is used only when it *is* the recorded
    caller (the ordinary walk, or a restart resumed by the same caller);
    any other driver — an operator, the restart service — has its identity
    replaced by the recorded caller's freshly re-derived authority.
    """
    if not binds_run_authority(artifact) or live is None:
        return live
    record = getattr(snapshot, "caller", None)
    if record is None:
        # A run persisted before callers were recorded.  An enforced driver
        # can only narrow; a trusted one would substitute itself.
        if isinstance(live, ExecutionPrincipal) and live.enforced:
            return bind(live, artifact, snapshot.run_id, step_id)
        return _closed(snapshot, RUN_CALLER_MISSING)
    if isinstance(live, ExecutionPrincipal) and _matches(live, record):
        caller = live
        if caller.enforced:
            # Current authority, never more than the run was started with.
            caller = caller.narrow(_recorded_policy(record), reason="run-caller")
    else:
        caller = await _revalidate(record, snapshot, services)
    return bind(caller, artifact, snapshot.run_id, step_id)


__all__ = [
    "RUN_CALLER_MISSING",
    "RUN_CALLER_UNRESOLVED",
    "bind",
    "binds_run_authority",
    "caller_record",
    "effect_principal",
    "public_caller",
]
