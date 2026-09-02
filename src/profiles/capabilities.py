"""Normalized, immutable capability policy for profiles.

Playbook V2 roadmap §4 locks this as a cross-package interface: three
separate namespaces with exact membership, set intersection, subset
validation, canonical serialization, and a deny-by-default empty value.
No namespace accepts ``*``.

The three namespaces answer three different questions:

``harness_tools``
    Names a CLI harness understands directly and can be told about via its
    allowlist flag (``Bash``, ``Read``, …).  Enforcement is best-effort:
    a harness with no ``tools_flag`` cannot be restricted.

``aq_commands``
    ``CommandHandler`` command names.  A tmux session reaches these through
    the ``aq`` CLI (i.e. through ``Bash``), so the harness flag cannot
    restrict them — the server-side check at dispatch is the boundary.

``plugin_tools``
    Plugin commands registered unprefixed into the same flat dispatch
    namespace (``read_file``, ``write_note``, ``vibecop_scan``, …) *and*
    fully-qualified third-party MCP tools (``mcp__github__create_issue``).
    :func:`classify_capability` is what keeps the two apart from
    ``aq_commands``.

Two rules that are easy to get backwards:

- **Empty means none.**  ``CapabilityPolicy()`` denies everything.  Nothing
  here treats "empty" as "unset"; ``None`` on the *profile* is what means
  "not authored", and that is the legacy adapter's trigger.
- **There is no grant-everything value.**  ``from_namespaces`` rejects any
  entry containing a wildcard character, so ``"*"``, ``"**"`` and
  ``"mcp__github__*"`` all fail at construction.

See ``docs/superpowers/plans/2026-09-01-playbook-v2-phase0-security.md``
§3.1–§3.3.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from src.api.scope import AGENT_COMMAND_SET

Namespace = Literal["harness_tools", "aq_commands", "plugin_tools"]

#: The three namespaces, in canonical order.
NAMESPACES: Final[tuple[Namespace, ...]] = ("harness_tools", "aq_commands", "plugin_tools")

#: Characters that make an entry a pattern rather than a name.
WILDCARD_CHARS: Final = "*?"

#: Tool names a CLI harness understands directly.  Single definition —
#: ``src/sessions/spec.py`` imports it rather than keeping its own copy, so
#: the launcher's idea of a harness tool and the classifier's cannot drift.
HARNESS_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "Bash", "Read", "Write", "Edit", "Glob", "Grep", "Skill",
        "WebSearch", "WebFetch", "Task", "TodoWrite", "NotebookEdit",
    }
)

#: Prefix marking a fully-qualified third-party MCP tool.
MCP_TOOL_PREFIX: Final = "mcp__"


class CapabilityPolicyError(ValueError):
    """An authored capability set is not expressible as a policy."""


def classify_capability(
    name: str, *, plugin_command_names: frozenset[str] = frozenset()
) -> Namespace:
    """Decide which namespace *name* belongs to.

    *plugin_command_names* comes from ``orchestrator.plugin_registry``.  When
    no registry is wired it defaults to empty, which classifies plugin
    commands as ``aq_commands`` — the **stricter** reading, since
    ``aq_commands`` is checked against a registry-derived built-in set and an
    unknown plugin name is then denied rather than silently allowed.
    """
    if name in HARNESS_TOOL_NAMES:
        return "harness_tools"
    if name.startswith(MCP_TOOL_PREFIX):
        return "plugin_tools"
    if name in plugin_command_names:
        return "plugin_tools"
    return "aq_commands"


def _normalize(namespace: str, values: Any) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, (str, bytes)):
        raise CapabilityPolicyError(
            f"Capabilities '{namespace}' must be a list of names, not a string"
        )
    out: set[str] = set()
    for entry in values:
        if not isinstance(entry, str):
            raise CapabilityPolicyError(
                f"Capabilities '{namespace}' entries must be strings, got "
                f"{type(entry).__name__}: {entry!r}"
            )
        if not entry.strip():
            raise CapabilityPolicyError(
                f"Capabilities '{namespace}' entries must be non-empty, got {entry!r}"
            )
        if any(ch in entry for ch in WILDCARD_CHARS):
            raise CapabilityPolicyError(
                f"Capabilities '{namespace}' entry {entry!r} contains a wildcard; "
                "wildcard capabilities are prohibited — list every name explicitly"
            )
        out.add(entry)
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    """What one profile may do, split into three exact-match namespaces."""

    harness_tools: frozenset[str] = field(default_factory=frozenset)
    aq_commands: frozenset[str] = field(default_factory=frozenset)
    plugin_tools: frozenset[str] = field(default_factory=frozenset)

    #: True when this policy was reconstructed by the legacy adapter from
    #: ``allowed_tools`` rather than authored as explicit namespaces.  Drives
    #: the audit/enforce split: only *adapted* denials get a grace mode, so
    #: flipping deny-by-default on does not strand an un-migrated fleet.
    #: Not part of :meth:`fingerprint`.
    #:
    #: **Removal package: Package 7**, with the legacy adapter, the
    #: ``## Tools`` block, and ``security.capability_enforcement``.
    derived_from_legacy: bool = False

    # -- construction ------------------------------------------------------

    @classmethod
    def from_namespaces(
        cls,
        *,
        harness_tools: Any = None,
        aq_commands: Any = None,
        plugin_tools: Any = None,
        derived_from_legacy: bool = False,
    ) -> "CapabilityPolicy":
        """Validating constructor.

        Raises :class:`CapabilityPolicyError` on a wildcard, an empty or
        whitespace-only entry, or a non-string entry.
        """
        return cls(
            harness_tools=_normalize("harness_tools", harness_tools),
            aq_commands=_normalize("aq_commands", aq_commands),
            plugin_tools=_normalize("plugin_tools", plugin_tools),
            derived_from_legacy=derived_from_legacy,
        )

    # -- membership --------------------------------------------------------

    def allows(self, namespace: Namespace, name: str) -> bool:
        """Exact membership.  No prefix matching, no globbing, no case folding."""
        return name in getattr(self, namespace)

    def allows_harness_tool(self, name: str) -> bool:
        return self.allows("harness_tools", name)

    def allows_aq_command(self, name: str) -> bool:
        return self.allows("aq_commands", name)

    def allows_plugin_tool(self, name: str) -> bool:
        return self.allows("plugin_tools", name)

    # -- algebra -----------------------------------------------------------

    def intersect(self, other: "CapabilityPolicy") -> "CapabilityPolicy":
        """Per-namespace intersection — the only policy transform there is.

        There is deliberately no union: narrowing must be the only direction
        a policy can move at runtime.
        """
        return CapabilityPolicy(
            harness_tools=self.harness_tools & other.harness_tools,
            aq_commands=self.aq_commands & other.aq_commands,
            plugin_tools=self.plugin_tools & other.plugin_tools,
            derived_from_legacy=self.derived_from_legacy or other.derived_from_legacy,
        )

    def is_subset_of(self, other: "CapabilityPolicy") -> bool:
        """True when every namespace of *self* is contained in *other*'s."""
        return all(getattr(self, ns) <= getattr(other, ns) for ns in NAMESPACES)

    # -- serialization -----------------------------------------------------

    def to_canonical(self) -> dict[str, list[str]]:
        """Sorted, stable dict form — the input to :meth:`fingerprint`."""
        return {ns: sorted(getattr(self, ns)) for ns in NAMESPACES}

    def fingerprint(self) -> str:
        """``sha256:<hex>`` over the canonical form.

        Order-independent and independent of :attr:`derived_from_legacy`, so
        two policies with the same effective grant fingerprint identically.
        """
        blob = json.dumps(self.to_canonical(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def is_empty(self) -> bool:
        return not (self.harness_tools or self.aq_commands or self.plugin_tools)


#: Deny everything.  The value a fail-closed path returns.
DENY_ALL: Final[CapabilityPolicy] = CapabilityPolicy()


#: What the legacy adapter falls back to for ``aq_commands`` (rules R1/R2 in
#: :func:`capability_policy_for`).  It is exactly the server-owned session
#: allowlist ``check_request_scope`` already admits, so neither rule grants a
#: name that was not reachable before Package 0.
AGENT_COMMAND_FALLBACK: Final[frozenset[str]] = AGENT_COMMAND_SET


def _agent_command_set() -> frozenset[str]:
    return AGENT_COMMAND_FALLBACK


def capability_policy_for(
    profile: Any, *, plugin_command_names: frozenset[str] = frozenset()
) -> CapabilityPolicy:
    """Resolve one profile's effective policy.

    An ``AgentProfile`` that authored ``harness_tools`` / ``aq_commands`` /
    ``plugin_tools`` (any of them non-``None``) is taken at its word, and an
    explicitly empty namespace means *none*.

    Otherwise the **legacy adapter** builds a policy from ``allowed_tools``
    with two compatibility rules that exist to avoid *removing* rights, never
    to add any:

    R1 — empty ``allowed_tools``
        Today that means "emit no ``--allowedTools`` flag", i.e. the CLI's own
        defaults.  The adapter yields :data:`HARNESS_TOOL_NAMES` — the same
        effective grant, since the launcher could never express more.

    R2 — no AQ names declared
        A legacy profile listing only harness tools has, today, exactly
        ``AGENT_COMMAND_SET`` at the API boundary, so the adapter yields that.

    Both set ``derived_from_legacy=True``, which routes them through the audit
    path instead of hard denial.  The *effective* grant is always the
    intersection of this gate and ``check_request_scope``, which this package
    leaves untouched — so a legacy profile can never come out of Package 0
    with more reach than it had going in.

    ``None`` (no profile at all) is :data:`DENY_ALL`: fail closed.
    """
    if profile is None:
        return DENY_ALL

    authored = {ns: getattr(profile, ns, None) for ns in NAMESPACES}
    if any(v is not None for v in authored.values()):
        return CapabilityPolicy.from_namespaces(
            harness_tools=authored["harness_tools"] or [],
            aq_commands=authored["aq_commands"] or [],
            plugin_tools=authored["plugin_tools"] or [],
        )

    declared = list(getattr(profile, "allowed_tools", None) or [])
    if not declared:
        # R1
        return CapabilityPolicy(
            harness_tools=HARNESS_TOOL_NAMES,
            aq_commands=_agent_command_set(),
            plugin_tools=frozenset(),
            derived_from_legacy=True,
        )

    buckets: dict[Namespace, set[str]] = {ns: set() for ns in NAMESPACES}
    for name in declared:
        if not isinstance(name, str) or not name.strip():
            continue
        if any(ch in name for ch in WILDCARD_CHARS):
            raise CapabilityPolicyError(
                f"profile {getattr(profile, 'id', '?')!r} allowed_tools entry {name!r} "
                "contains a wildcard; wildcard capabilities are prohibited"
            )
        buckets[classify_capability(name, plugin_command_names=plugin_command_names)].add(name)

    if not buckets["aq_commands"]:
        # R2
        buckets["aq_commands"] = set(_agent_command_set())

    return CapabilityPolicy(
        harness_tools=frozenset(buckets["harness_tools"]),
        aq_commands=frozenset(buckets["aq_commands"]),
        plugin_tools=frozenset(buckets["plugin_tools"]),
        derived_from_legacy=True,
    )


__all__ = [
    "AGENT_COMMAND_FALLBACK",
    "CapabilityPolicy",
    "CapabilityPolicyError",
    "DENY_ALL",
    "HARNESS_TOOL_NAMES",
    "MCP_TOOL_PREFIX",
    "NAMESPACES",
    "Namespace",
    "WILDCARD_CHARS",
    "capability_policy_for",
    "classify_capability",
]
