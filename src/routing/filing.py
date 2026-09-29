"""Filing carries hints, never routes (mandatory-routing spec §5.1, §5.2).

Every filing surface refuses an argument that would choose a task's worker
route — a profile, a provider, a model, a harness or a pin — with
``success: false`` and ``code: routing.choice_forbidden``, and writes nothing.
A filer may give two hints instead: an intelligence class and a kind
(``task_type``).  The project's bound routing playbook is the only writer of
the route (``task_route_apply``).

:func:`routing_choice_refusal` runs in the command dispatch path
(``CommandHandler.execute``), ahead of any handler and of the contract's
args-model validation, so the answer is the same for every external
principal and an argument no args model declares (``provider``, ``model``,
``harness``) earns this refusal rather than a generic validation error.

The one exception is a role profile (``ROLE_PROFILE_IDS``) passed as
``profile_id`` to ``create_task`` or ``ensure_task`` by a ``SERVICE`` or
``PLAYBOOK`` principal (spec §4, D3).

``create_project`` and ``edit_project`` (:data:`PROJECT_COMMANDS`) refuse the
same arguments, ``default_profile_id`` above all: a project has no default
profile, only a router binding (§8).

Graph documents are refused where they are parsed (``src/task_graph/parser.py``)
with the rule :data:`GRAPH_RULE`, so an inline graph, a vault spec's
``aq-graph`` block and a formula are all refused the same way.

The contract argument models of ``create_task``, ``ensure_task`` and
``edit_task`` keep their legacy fields on purpose: removing them would change
their execution fingerprints and stale every reviewed bundle that calls them.
A value arriving through them is refused here, which the adapter reports as
the contracts' existing ``rejected`` outcome.

Dependency-free apart from :mod:`src.routing.sources`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.routing.sources import ROLE_PROFILE_IDS

#: The ``code`` of every refusal this module returns.
ROUTING_CHOICE_FORBIDDEN = "routing.choice_forbidden"

#: The graph finding rule for a node, ``defaults`` or ``parent`` key that
#: chooses a route; ``create_task_graph`` and ``formula_cook`` answer it with
#: :data:`ROUTING_CHOICE_FORBIDDEN`.
GRAPH_RULE = "routing_choice_forbidden"

#: Arguments that choose a route, in the spec's order (§5.1).
REFUSED_ROUTING_ARGS: tuple[str, ...] = (
    "profile_id",
    "profile",
    "provider",
    "model",
    "harness",
    "agent_type",
    "pin",
    "provider_intent",
    "preferred_provider",
    "default_profile_id",
)

#: Keys a graph node, the graph's ``defaults`` or its ``parent:`` block may
#: not carry.  ``profile`` and ``pin`` are the graph spellings; the rest are
#: refused so a document cannot reach a route under another name.
REFUSED_GRAPH_KEYS: tuple[str, ...] = (
    "profile",
    "profile_id",
    "pin",
    "provider_intent",
    "provider",
    "model",
    "harness",
    "agent_type",
)

#: Every filing surface this module guards, with the hints it still accepts.
#: The refusal names them, so the caller learns what to pass instead.
FILING_HINTS: dict[str, tuple[str, ...]] = {
    "create_task": ("intelligence_class", "task_type"),
    "ensure_task": ("intelligence_class",),
    "create_task_graph": ("intelligence_class", "a node's intelligence_class and task_type"),
    "formula_cook": ("a node's intelligence_class and task_type",),
    "edit_task": ("intelligence_class", "task_type"),
    "task_batch_propose": ("a task's intelligence_class",),
    "task_batch_update": ("a task's intelligence_class",),
    "task_batch_commit": ("a task's intelligence_class",),
}

FILING_COMMANDS: frozenset[str] = frozenset(FILING_HINTS)

#: Project commands (spec §5.1, §8).  A project has no default profile and
#: files nothing, so they refuse the same arguments with a refusal that names
#: the router binding instead of the task hints.
PROJECT_COMMANDS: frozenset[str] = frozenset({"create_project", "edit_project"})

#: Every command the dispatch path guards.
GUARDED_COMMANDS: frozenset[str] = FILING_COMMANDS | PROJECT_COMMANDS

#: The surfaces on which a role creator may name a role profile (§4, D3).
ROLE_FILING_COMMANDS: frozenset[str] = frozenset({"create_task", "ensure_task"})


def is_routing_choice(value: Any) -> bool:
    """Whether *value* chooses something.

    ``None``, ``False`` and ``""`` do not: a client that always sends
    ``pin: false`` or an unset option as ``null`` has asked for nothing.
    """
    return value is not None and value is not False and value != ""


def _batch_specs(command: str, args: Mapping[str, Any]) -> list[Any]:
    """The per-task specs a batch command carries inline."""
    if command == "task_batch_propose":
        specs = args.get("tasks")
    elif command == "task_batch_update":
        payload = args.get("payload")
        specs = payload.get("tasks") if isinstance(payload, Mapping) else None
    else:
        return []
    return list(specs) if isinstance(specs, list) else []


def refused_spec_keys(specs: Any) -> list[str]:
    """``tasks[i].<key>`` for every routing choice in a batch's task specs."""
    found: list[str] = []
    if not isinstance(specs, list):
        return found
    for index, spec in enumerate(specs):
        if not isinstance(spec, Mapping):
            continue
        found.extend(
            f"tasks[{index}].{key}"
            for key in REFUSED_ROUTING_ARGS
            if key in spec and is_routing_choice(spec[key])
        )
    return found


def refused_arguments(
    command: str, args: Mapping[str, Any], *, role_creator: bool = False
) -> list[str]:
    """Every argument of *args* that chooses a route, in a stable order.

    *role_creator* admits a role ``profile_id`` on ``create_task`` and
    ``ensure_task`` (spec §4, D3); nothing else is ever admitted.
    """
    found: list[str] = []
    for key in REFUSED_ROUTING_ARGS:
        if key not in args or not is_routing_choice(args[key]):
            continue
        if (
            key == "profile_id"
            and role_creator
            and command in ROLE_FILING_COMMANDS
            and args[key] in ROLE_PROFILE_IDS
        ):
            continue
        found.append(key)
    found.extend(refused_spec_keys(_batch_specs(command, args)))
    return found


def choice_forbidden(command: str, refused: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """The refusal for *refused* arguments on *command*."""
    if command in PROJECT_COMMANDS:
        return {
            "success": False,
            "code": ROUTING_CHOICE_FORBIDDEN,
            "error": (
                f"{command} does not accept routing choices ({', '.join(refused)}): a "
                "project has no default profile, and its bound router routes every task. "
                "Bind another router with `aq project set <project> router <playbook-id>`."
            ),
            "refused": list(refused),
            "hints": [],
        }
    hints = FILING_HINTS.get(command, ("intelligence_class", "task_type"))
    return {
        "success": False,
        "code": ROUTING_CHOICE_FORBIDDEN,
        "error": (
            f"{command} does not accept routing choices ({', '.join(refused)}): the "
            "project's router picks every task's profile, provider and model. File with "
            f"hints instead: {', '.join(hints)}."
        ),
        "refused": list(refused),
        "hints": list(hints),
    }


def graph_route_refusal(command: str, findings: Any) -> dict[str, Any] | None:
    """The refusal for a graph document whose findings name a route key.

    *findings* are the parser's ``GraphError`` rows.  Each :data:`GRAPH_RULE`
    finding quotes the key it refused first in its detail (``'defaults.pin'``),
    and a node's key prefixes it, so the refusal names every one.  The full
    finding list rides along under ``errors``, as for any invalid graph.
    """
    refused: list[str] = []
    for finding in findings or ():
        if getattr(finding, "rule", None) != GRAPH_RULE:
            continue
        detail = str(getattr(finding, "detail", "") or "")
        field = detail.split("'")[1] if detail.count("'") >= 2 else detail
        node = getattr(finding, "node", None)
        refused.append(f"{node}.{field}" if node else field)
    if not refused:
        return None
    refusal = choice_forbidden(command, refused)
    refusal["errors"] = [finding.to_dict() for finding in findings]
    return refusal


def routing_choice_refusal(
    command: str, args: Mapping[str, Any], principal: Any = None
) -> dict[str, Any] | None:
    """The dispatch-path refusal for *command*, or ``None`` to let it run.

    *principal* is the request's ``ExecutionPrincipal``; only its kind
    matters, for the role exception.
    """
    if command not in GUARDED_COMMANDS or not isinstance(args, Mapping):
        return None
    kind = str(getattr(principal, "kind", "") or "")
    refused = refused_arguments(command, args, role_creator=kind in {"service", "playbook"})
    return choice_forbidden(command, refused) if refused else None


def without_inert_choices(args: dict[str, Any]) -> dict[str, Any]:
    """*args* minus every routing key whose value chooses nothing.

    Run after :func:`routing_choice_refusal` admitted the call, so a handler
    never reads a ``profile_id: null`` or ``pin: false`` as a request.
    """
    inert = [key for key in REFUSED_ROUTING_ARGS if key in args and not is_routing_choice(args[key])]
    if not inert:
        return args
    return {key: value for key, value in args.items() if key not in inert}


__all__ = [
    "FILING_COMMANDS",
    "FILING_HINTS",
    "GRAPH_RULE",
    "GUARDED_COMMANDS",
    "PROJECT_COMMANDS",
    "REFUSED_GRAPH_KEYS",
    "REFUSED_ROUTING_ARGS",
    "ROLE_FILING_COMMANDS",
    "ROUTING_CHOICE_FORBIDDEN",
    "choice_forbidden",
    "graph_route_refusal",
    "is_routing_choice",
    "refused_arguments",
    "refused_spec_keys",
    "routing_choice_refusal",
    "without_inert_choices",
]
