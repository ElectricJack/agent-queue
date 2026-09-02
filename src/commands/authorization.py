"""Capability matching, dispatch authorization, and tool-schema filtering.

**One predicate, one answer.** :func:`command_allowed` is called by dispatch
(``CommandHandler.execute``), by tool discovery (``_cmd_load_tools``,
``/api/tools``, MCP registration), and by ``PlaybookServices.node_tools``.
That is what makes the roadmap's "tool discovery and tool-schema publication
use the same capability policy as execution" mechanically true rather than a
convention two call sites are expected to keep: a name that is published is
runnable, and a name that is denied is not published.

**Denials leak nothing.** :func:`denial_result` names only the command. The
principal kind, profile id, namespace and policy fingerprint go to the daemon
log and the ``capability.denied`` counter — operator surfaces, not agent
surfaces.

See ``docs/superpowers/plans/2026-09-01-playbook-v2-phase0-security.md``
§3.6, §4.3, §4.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from src.commands.principal import ExecutionPrincipal
from src.profiles.capabilities import Namespace, classify_capability

#: ``security.capability_enforcement`` values.  See ``SecurityConfig``.
MODE_OFF = "off"
MODE_AUDIT = "audit"
MODE_ENFORCE = "enforce"

#: The error code a denial carries, mapped to HTTP 403 at both surfaces.
CAPABILITY_DENIED = "capability_denied"

_MODES = frozenset({MODE_OFF, MODE_AUDIT, MODE_ENFORCE})


def normalize_mode(value: object) -> str:
    """Coerce a configured enforcement value to a mode this module understands.

    A **non-string** means there is no real ``SecurityConfig`` behind it — a
    test double, or a config object built before this field existed.  That is
    an absent choice, not an operator choice, so it resolves to the shipped
    default rather than to the strictest setting.

    An **unrecognised string** is different: ``SecurityConfig.validate()``
    rejects those, so one reaching here means config validation was bypassed.
    That resolves to ``enforce`` — fail closed on a value nobody vouched for.
    """
    if not isinstance(value, str):
        return MODE_AUDIT
    return value if value in _MODES else MODE_ENFORCE


class CommandResolver(Protocol):
    """Tells the authorizer what kind of name it is looking at."""

    def is_builtin(self, name: str) -> bool: ...

    def is_plugin(self, name: str) -> bool: ...

    def plugin_command_names(self) -> frozenset[str]: ...


class CommandHandlerResolver:
    """:class:`CommandResolver` backed by a live ``CommandHandler``."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def is_builtin(self, name: str) -> bool:
        return getattr(self._handler, f"_cmd_{name}", None) is not None

    def is_plugin(self, name: str) -> bool:
        registry = getattr(self._handler.orchestrator, "plugin_registry", None)
        if registry is None:
            return False
        try:
            return registry.get_command(name) is not None
        except Exception:  # pragma: no cover — a broken registry denies, not crashes
            return False

    def plugin_command_names(self) -> frozenset[str]:
        return self._handler._plugin_command_names()


@dataclass(frozen=True, slots=True)
class AuthzDecision:
    """The outcome of one authorization check."""

    allowed: bool
    namespace: Namespace
    reason: str = ""
    #: True when the policy *would* have denied but the caller's policy was
    #: reconstructed by the legacy adapter and the mode is ``audit``.  The
    #: command runs; a ``capability_denied_shadow`` warning is emitted.
    shadow: bool = False


def resolve_namespace(name: str, resolver: CommandResolver) -> Namespace:
    """Which namespace gates *name*.

    A built-in wins dispatch over a same-named plugin command (tool names are
    globally unique — see ``docs/specs/tiered-tools.md``), so it is checked
    first here too: the gate must consult the namespace of the handler that
    would actually run.
    """
    if resolver.is_builtin(name):
        return "aq_commands"
    if resolver.is_plugin(name):
        return "plugin_tools"
    return classify_capability(name, plugin_command_names=resolver.plugin_command_names())


def required_capability(name: str) -> str:
    """Return the contract-declared capability, retaining legacy behavior."""
    from src.commands.contracts import CONTRACTS
    return CONTRACTS.required_capability(name) or name


def command_allowed(
    name: str, principal: ExecutionPrincipal, *, resolver: CommandResolver
) -> bool:
    """Whether *principal* may run *name* under strict (``enforce``) semantics.

    The publication predicate: discovery surfaces call this so what an agent
    is shown matches what it can run.  Trusted principals (``LOCAL``,
    ``SERVICE``) are allowed everything — that is the explicit trusted path,
    a property of the kind, with no argument or config that grants it to a
    ``SESSION``.
    """
    if not principal.enforced:
        return True
    return principal.policy.allows(resolve_namespace(name, resolver), required_capability(name))


def authorize_command(
    name: str,
    principal: ExecutionPrincipal,
    *,
    resolver: CommandResolver,
    mode: str = MODE_AUDIT,
) -> AuthzDecision:
    """Decide dispatch for *name*, honouring the enforcement mode.

    ============  ============================  ================================
    Mode          Authored ``## Capabilities``   Legacy-adapted *or* unresolved
    ============  ============================  ================================
    ``off``       allow                         allow
    ``audit``     **deny**                      allow + shadow warning
    ``enforce``   **deny**                      **deny**
    ============  ============================  ================================

    The split is deliberate: an operator who wrote the block asked for it, so
    an explicit policy is always enforced.  Only the *adapted* legacy shape
    gets a grace mode, because flipping every un-migrated profile to
    deny-by-default in one commit would strand a running fleet.

    An *unresolved* principal (:attr:`ExecutionPrincipal.unresolved` — a
    missing session or profile row) rides the same grace mode for the same
    reason: it is deny-by-default arriving from an incomplete migration
    rather than from an operator's choice.  Both are fatal under
    ``enforce``, which is the mode the package exit gate is proven with.
    """
    mode = normalize_mode(mode)
    namespace = resolve_namespace(name, resolver)

    if not principal.enforced:
        return AuthzDecision(allowed=True, namespace=namespace, reason="trusted-principal")
    if mode == MODE_OFF:
        return AuthzDecision(allowed=True, namespace=namespace, reason="enforcement-off")
    if principal.policy.allows(namespace, required_capability(name)):
        return AuthzDecision(allowed=True, namespace=namespace)

    if mode == MODE_AUDIT and (principal.policy.derived_from_legacy or principal.unresolved):
        return AuthzDecision(
            allowed=True,
            namespace=namespace,
            reason="unresolved-shadow" if principal.unresolved else "legacy-shadow",
            shadow=True,
        )
    return AuthzDecision(
        allowed=False,
        namespace=namespace,
        reason=f"{name} is not in the caller's {namespace}",
    )


def denial_result(name: str) -> dict:
    """The agent-facing denial payload.  Deliberately says nothing more."""
    return {
        "success": False,
        "error": f"capability denied: {name}",
        "error_code": CAPABILITY_DENIED,
    }


def filter_tool_definitions(
    definitions: Iterable[dict],
    principal: ExecutionPrincipal,
    *,
    resolver: CommandResolver,
) -> list[dict]:
    """Keep only the tool definitions *principal* could actually dispatch.

    Uses :func:`command_allowed`, so a published name is a runnable name.
    """
    return [d for d in definitions if command_allowed(d.get("name", ""), principal, resolver=resolver)]


__all__ = [
    "AuthzDecision",
    "CAPABILITY_DENIED",
    "CommandHandlerResolver",
    "CommandResolver",
    "MODE_AUDIT",
    "MODE_ENFORCE",
    "MODE_OFF",
    "authorize_command",
    "command_allowed",
    "denial_result",
    "filter_tool_definitions",
    "normalize_mode",
    "required_capability",
    "resolve_namespace",
]
