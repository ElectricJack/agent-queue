"""Data models for compiled playbook graphs.

Defines the Python dataclasses that represent a compiled playbook — the JSON
artifact produced by LLM compilation of a playbook markdown file.  The runtime
executor operates on these models, never on the source markdown directly.

The companion JSON Schema (``playbook_schema.json``) is auto-generated from
these dataclasses via :func:`generate_json_schema`.  Both representations are
kept in sync: the dataclasses are the source of truth, the JSON Schema is
derived.

See ``docs/specs/design/playbooks.md`` Section 5 for the full specification.

Typical usage::

    import json
    from src.playbooks.models import CompiledPlaybook

    with open("compiled/code-quality-gate.json") as f:
        data = json.load(f)
    playbook = CompiledPlaybook.from_dict(data)

    # Validate
    errors = playbook.validate()
    if errors:
        raise ValueError(f"Invalid playbook: {errors}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PlaybookScope(Enum):
    """Where a playbook applies.

    - ``SYSTEM``: fires for all events across all projects.
    - ``PROJECT``: fires only for events scoped to a specific project.
    - ``AGENT_TYPE``: fires only when the originating agent matches a type.

    Values are stored as lowercase strings in the compiled JSON.  The
    ``agent-type:`` prefix variant (e.g. ``"agent-type:coding"``) is handled
    by :meth:`CompiledPlaybook.parse_scope`.
    """

    SYSTEM = "system"
    PROJECT = "project"
    AGENT_TYPE = "agent-type"


class PlaybookRunStatus(Enum):
    """Lifecycle state of a single playbook execution (run).

    See ``docs/specs/design/playbooks.md`` Section 6 — Run Persistence.
    """

    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


# ---------------------------------------------------------------------------
# Trigger (event type + optional payload filter)
# ---------------------------------------------------------------------------


@dataclass
class PlaybookTrigger:
    """An event trigger for a playbook — event type with optional payload filter.

    Triggers can be defined in playbook frontmatter as either:

    - **String shorthand**: ``"task.completed"`` — matches all events of that type.
    - **Structured dict**: ``{"event_type": "playbook.run.completed", "filter":
      {"playbook_id": "code-quality-gate"}}`` — matches only when all filter
      key/value pairs match the event payload.

    See ``docs/specs/design/playbooks.md`` Section 10 (Composability) for the
    full specification of event payload filtering.

    The filter uses AND semantics: every key/value pair must match the
    corresponding field in the event payload.  This aligns with the
    :class:`~src.event_bus.EventBus` ``filter`` parameter on ``subscribe()``.

    Examples
    --------
    >>> t1 = PlaybookTrigger(event_type="task.completed")
    >>> t2 = PlaybookTrigger(
    ...     event_type="playbook.run.completed",
    ...     filter={"playbook_id": "code-quality-gate"},
    ... )
    """

    event_type: str
    filter: dict[str, Any] | None = None

    def __eq__(self, other: object) -> bool:
        """Support equality with both PlaybookTrigger and plain strings.

        String comparison returns ``True`` when the trigger has no filter and
        the event types match.  This preserves backward compatibility with
        code that compares triggers to strings (e.g. ``trigger == "git.commit"``).
        """
        if isinstance(other, str):
            return self.event_type == other and self.filter is None
        if isinstance(other, PlaybookTrigger):
            return self.event_type == other.event_type and self.filter == other.filter
        return NotImplemented

    def __hash__(self) -> int:
        filter_key = tuple(sorted(self.filter.items())) if self.filter else None
        return hash((self.event_type, filter_key))

    def __str__(self) -> str:
        return self.event_type

    def __repr__(self) -> str:
        if self.filter:
            return f"PlaybookTrigger({self.event_type!r}, filter={self.filter!r})"
        return f"PlaybookTrigger({self.event_type!r})"

    def to_value(self) -> str | dict[str, Any]:
        """Serialize to JSON-compatible value (string shorthand or dict).

        Returns the string shorthand when no filter is present, or a dict
        with ``event_type`` and ``filter`` keys when a filter is set.  This
        matches the compiled JSON format defined in the spec.
        """
        if self.filter is None:
            return self.event_type
        return {"event_type": self.event_type, "filter": self.filter}

    @classmethod
    def from_value(cls, val: str | dict[str, Any] | PlaybookTrigger) -> PlaybookTrigger:
        """Create from a string, dict, or existing PlaybookTrigger.

        Accepts all three forms that can appear in compiled JSON or in-memory:

        - ``"task.completed"`` → ``PlaybookTrigger(event_type="task.completed")``
        - ``{"event_type": "...", "filter": {...}}`` → full structured trigger
        - ``PlaybookTrigger(...)`` → returned as-is

        Raises
        ------
        TypeError
            If *val* is not a supported type.
        ValueError
            If a dict is missing the required ``event_type`` key.
        """
        if isinstance(val, PlaybookTrigger):
            return val
        if isinstance(val, str):
            return cls(event_type=val)
        if isinstance(val, dict):
            if "event_type" not in val:
                raise ValueError(
                    f"Structured trigger dict must have 'event_type' key, got: {val!r}"
                )
            return cls(event_type=val["event_type"], filter=val.get("filter"))
        raise TypeError(f"Cannot create PlaybookTrigger from {type(val).__name__}: {val!r}")


# ---------------------------------------------------------------------------
# LLM configuration override
# ---------------------------------------------------------------------------


@dataclass
class LlmConfig:
    """Optional LLM provider/model override for a playbook or individual node.

    Allows cost control: transition evaluation can use a fast/cheap model while
    complex reasoning nodes use the most capable model.  When omitted, the
    system default chat provider is used.

    Fields:

    * ``provider`` — chat provider name (e.g. ``"anthropic"``, ``"gemini"``).
    * ``model`` — model identifier (e.g. ``"claude-sonnet-4-20250514"``).
    * ``max_tokens`` — maximum response tokens per LLM call.
    * ``temperature`` — sampling temperature (0.0–1.0).
    """

    provider: str = ""  # e.g. "anthropic", "google", "openai"
    model: str = ""  # e.g. "claude-sonnet-4-20250514", "gemini-2.0-flash"
    max_tokens: int | None = None  # e.g. 1024, 4096
    temperature: float | None = None  # e.g. 0.0, 0.7

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.provider:
            d["provider"] = self.provider
        if self.model:
            d["model"] = self.model
        if self.max_tokens is not None:
            d["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            d["temperature"] = self.temperature
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LlmConfig:
        return cls(
            provider=data.get("provider", ""),
            model=data.get("model", ""),
            max_tokens=data.get("max_tokens"),
            temperature=data.get("temperature"),
        )


# ---------------------------------------------------------------------------
# Transition (edge)
# ---------------------------------------------------------------------------


@dataclass
class PlaybookTransition:
    """A conditional edge between two nodes in a playbook graph.

    Exactly one of ``when`` or ``otherwise`` should be set:

    - *Natural language* ``when``: a string the LLM evaluates given the
      current context (e.g. ``"findings exist"``).
    - *Structured* ``when``: a dict expressing a deterministic check that the
      executor can evaluate without an LLM call (e.g.
      ``{"function": "has_tool_output", "contains": "no findings"}``).
    - ``otherwise``: marks this as the default/fallback transition when no
      other ``when`` condition matches.
    """

    goto: str  # Target node ID
    when: str | dict[str, Any] | None = None
    otherwise: bool = False

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"goto": self.goto}
        if self.when is not None:
            d["when"] = self.when
        if self.otherwise:
            d["otherwise"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaybookTransition:
        return cls(
            goto=data["goto"],
            when=data.get("when"),
            otherwise=data.get("otherwise", False),
        )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


@dataclass
class PlaybookNode:
    """A single step in a playbook graph — a focused LLM decision point.

    Nodes are mutually exclusive between three "kinds":

    1. **Action node** — has a ``prompt`` and either ``transitions`` or
       ``goto`` to determine the next step.
    2. **Terminal node** — ``terminal=True``, execution ends here.
    3. **Human-gate node** — ``wait_for_human=True``, execution pauses for
       review before continuing.

    Invariants enforced by :meth:`CompiledPlaybook.validate`:

    - Non-terminal nodes must have a ``prompt``.
    - ``transitions`` and ``goto`` are mutually exclusive.
    - A node without ``transitions``, ``goto``, or ``terminal`` is invalid.
    - Exactly one node in the playbook should have ``entry=True``.
    """

    prompt: str = ""
    entry: bool = False
    terminal: bool = False
    transitions: list[PlaybookTransition] = field(default_factory=list)
    goto: str | None = None
    wait_for_human: bool = False
    timeout_seconds: int | None = None
    pause_timeout_seconds: int | None = None  # Override pause timeout for this node (spec §9)
    on_timeout: str | None = None  # Node ID to transition to on pause timeout (spec §9)
    llm_config: LlmConfig | None = None
    transition_llm_config: LlmConfig | None = None
    for_each: dict[str, Any] | None = None
    output: dict[str, Any] | None = None

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.entry:
            d["entry"] = True
        if self.prompt:
            d["prompt"] = self.prompt
        if self.transitions:
            d["transitions"] = [t.to_dict() for t in self.transitions]
        if self.goto is not None:
            d["goto"] = self.goto
        if self.terminal:
            d["terminal"] = True
        if self.wait_for_human:
            d["wait_for_human"] = True
        if self.timeout_seconds is not None:
            d["timeout_seconds"] = self.timeout_seconds
        if self.pause_timeout_seconds is not None:
            d["pause_timeout_seconds"] = self.pause_timeout_seconds
        if self.on_timeout is not None:
            d["on_timeout"] = self.on_timeout
        if self.llm_config is not None:
            d["llm_config"] = self.llm_config.to_dict()
        if self.transition_llm_config is not None:
            d["transition_llm_config"] = self.transition_llm_config.to_dict()
        if self.for_each is not None:
            d["for_each"] = self.for_each
        if self.output is not None:
            d["output"] = self.output
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaybookNode:
        transitions = [PlaybookTransition.from_dict(t) for t in data.get("transitions", [])]
        llm_cfg = LlmConfig.from_dict(data["llm_config"]) if "llm_config" in data else None
        trans_cfg = (
            LlmConfig.from_dict(data["transition_llm_config"])
            if "transition_llm_config" in data
            else None
        )
        return cls(
            prompt=data.get("prompt", ""),
            entry=data.get("entry", False),
            terminal=data.get("terminal", False),
            transitions=transitions,
            goto=data.get("goto"),
            wait_for_human=data.get("wait_for_human", False),
            timeout_seconds=data.get("timeout_seconds"),
            pause_timeout_seconds=data.get("pause_timeout_seconds"),
            on_timeout=data.get("on_timeout"),
            llm_config=llm_cfg,
            transition_llm_config=trans_cfg,
            for_each=data.get("for_each"),
            output=data.get("output"),
        )


# ---------------------------------------------------------------------------
# Compiled Playbook (top-level)
# ---------------------------------------------------------------------------


@dataclass
class CompiledPlaybook:
    """The complete compiled representation of a playbook graph.

    This is the runtime artifact produced by LLM compilation of a playbook
    markdown file.  The executor loads and walks this graph — it never reads
    the source markdown directly.

    Top-level fields fall into three groups:

    **Identity & provenance** — ``id``, ``version``, ``source_hash``, ``compiled_at``
    **Trigger & scope** — ``triggers``, ``scope``, ``cooldown_seconds``
    **Graph** — ``nodes`` (the directed graph of LLM decision points)
    **Budget & config** — ``max_tokens``, ``llm_config``, ``transition_llm_config``
    """

    id: str
    version: int
    source_hash: str
    triggers: list[str | PlaybookTrigger]
    scope: str  # "system", "project", or "agent-type:{type}"
    nodes: dict[str, PlaybookNode] = field(default_factory=dict)
    cooldown_seconds: int | None = None
    max_tokens: int | None = None
    pause_timeout_seconds: int | None = None  # Default pause timeout for all nodes (spec §9)
    llm_config: LlmConfig | None = None
    transition_llm_config: LlmConfig | None = None
    compiled_at: str | None = None

    def __post_init__(self) -> None:
        """Normalize trigger entries to :class:`PlaybookTrigger` objects.

        Accepts a mixed list of strings and dicts (both valid in compiled
        JSON) and converts all entries to ``PlaybookTrigger`` instances.
        Existing ``PlaybookTrigger`` instances are passed through unchanged.
        """
        self.triggers = [PlaybookTrigger.from_value(t) for t in self.triggers]

    @property
    def trigger_event_types(self) -> list[str]:
        """Return a sorted list of unique event types across all triggers.

        Convenience accessor for code that only needs the event type strings
        (e.g. trigger map indexing) without filter details.
        """
        return sorted({t.event_type for t in self.triggers})

    # -- scope helpers -------------------------------------------------------

    def parse_scope(self) -> tuple[PlaybookScope, str | None]:
        """Parse the scope string into enum + optional identifier.

        Returns
        -------
        tuple[PlaybookScope, str | None]
            Scope enum and the type identifier for ``agent-type:`` scopes,
            or ``None`` for system/project scopes.

        Examples
        --------
        >>> pb.scope = "system"
        >>> pb.parse_scope()
        (PlaybookScope.SYSTEM, None)
        >>> pb.scope = "agent-type:coding"
        >>> pb.parse_scope()
        (PlaybookScope.AGENT_TYPE, 'coding')
        """
        if self.scope.startswith("agent-type:"):
            return PlaybookScope.AGENT_TYPE, self.scope.split(":", 1)[1]
        try:
            return PlaybookScope(self.scope), None
        except ValueError:
            return PlaybookScope.SYSTEM, None

    # -- graph helpers -------------------------------------------------------

    def entry_node_id(self) -> str | None:
        """Return the ID of the entry node, or ``None`` if not found."""
        for node_id, node in self.nodes.items():
            if node.entry:
                return node_id
        return None

    def terminal_node_ids(self) -> list[str]:
        """Return IDs of all terminal nodes."""
        return [nid for nid, node in self.nodes.items() if node.terminal]

    def reachable_node_ids(self, from_node: str | None = None) -> set[str]:
        """Return the set of node IDs reachable from *from_node* (default: entry).

        Uses breadth-first traversal.  Useful for detecting unreachable nodes
        during validation.
        """
        start = from_node or self.entry_node_id()
        if start is None or start not in self.nodes:
            return set()
        visited: set[str] = set()
        queue = [start]
        while queue:
            nid = queue.pop(0)
            if nid in visited or nid not in self.nodes:
                continue
            visited.add(nid)
            node = self.nodes[nid]
            for t in node.transitions:
                if t.goto not in visited:
                    queue.append(t.goto)
            if node.goto is not None and node.goto not in visited:
                queue.append(node.goto)
        return visited

    def nodes_reaching_terminal(self) -> set[str]:
        """Return the set of node IDs that have at least one path to a terminal node.

        Uses reverse BFS: builds a reverse adjacency graph and walks backwards
        from all terminal nodes.  Nodes *not* in this set are trapped in cycles
        that never reach completion.

        This is the complement to :meth:`reachable_node_ids` — that method
        checks forward reachability from entry, this one checks backward
        reachability *to* terminals.
        """
        terminal_ids = self.terminal_node_ids()
        if not terminal_ids:
            return set()

        # Build reverse adjacency: for each edge A→B, record B→A
        reverse_adj: dict[str, set[str]] = {nid: set() for nid in self.nodes}
        for nid, node in self.nodes.items():
            for t in node.transitions:
                if t.goto in reverse_adj:
                    reverse_adj[t.goto].add(nid)
            if node.goto is not None and node.goto in reverse_adj:
                reverse_adj[node.goto].add(nid)

        # BFS backwards from all terminal nodes
        visited: set[str] = set()
        queue = list(terminal_ids)
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for predecessor in reverse_adj.get(current, set()):
                if predecessor not in visited:
                    queue.append(predecessor)

        return visited

    # -- validation ----------------------------------------------------------

    def validate(self) -> list[str]:
        """Validate the compiled playbook structure.

        Returns a list of human-readable error strings.  An empty list means
        the playbook is valid.  Checks performed:

        1. Required top-level fields are present and non-empty.
        2. Exactly one entry node exists.
        3. At least one terminal node exists.
        4. Non-terminal nodes have a ``prompt``.
        5. ``transitions`` and ``goto`` are mutually exclusive on each node.
        6. Non-terminal nodes have at least one exit path (transitions, goto,
           or wait_for_human).
        7. All transition ``goto`` targets reference existing nodes.
        8. All node ``goto`` targets reference existing nodes.
        9. All nodes are reachable from the entry node.
        10. All reachable nodes have a path to at least one terminal node
            (detects cycles without exit conditions).
        11. Each node has at most one ``otherwise`` fallback transition.
        """
        errors: list[str] = []

        # 1. Required top-level fields
        if not self.id:
            errors.append("Missing required field: id")
        if not self.triggers:
            errors.append("Missing required field: triggers (must be non-empty list)")
        else:
            for i, trigger in enumerate(self.triggers):
                if not trigger.event_type:
                    errors.append(f"Trigger[{i}]: event_type must be non-empty")
        if not self.scope:
            errors.append("Missing required field: scope")
        if not self.source_hash:
            errors.append("Missing required field: source_hash")
        if not self.nodes:
            errors.append("Playbook has no nodes")
            return errors  # Can't validate further without nodes

        # 2. Exactly one entry node
        entry_nodes = [nid for nid, n in self.nodes.items() if n.entry]
        if len(entry_nodes) == 0:
            errors.append("No entry node found (exactly one node must have entry=true)")
        elif len(entry_nodes) > 1:
            errors.append(f"Multiple entry nodes found: {entry_nodes}")

        # 3. At least one terminal node
        terminal_nodes = self.terminal_node_ids()
        if not terminal_nodes:
            errors.append("No terminal node found (at least one node must have terminal=true)")

        # 4-6. Per-node validation
        for nid, node in self.nodes.items():
            # 4. Non-terminal nodes need a prompt
            if not node.terminal and not node.prompt:
                errors.append(f"Node '{nid}': non-terminal node must have a prompt")

            # 5. transitions and goto are mutually exclusive
            if node.transitions and node.goto is not None:
                errors.append(f"Node '{nid}': 'transitions' and 'goto' are mutually exclusive")

            # 6. Non-terminal nodes need an exit path
            if not node.terminal and not node.transitions and node.goto is None:
                # wait_for_human nodes still need an exit path for after resume
                if not node.wait_for_human:
                    errors.append(
                        f"Node '{nid}': non-terminal node must have "
                        "'transitions', 'goto', or 'terminal'"
                    )

            # 7. Transition goto targets exist
            otherwise_count = 0
            for i, t in enumerate(node.transitions):
                if t.goto not in self.nodes:
                    errors.append(
                        f"Node '{nid}' transition[{i}]: goto target '{t.goto}' does not exist"
                    )
                # Transitions need either 'when' or 'otherwise'
                if t.when is None and not t.otherwise:
                    errors.append(
                        f"Node '{nid}' transition[{i}]: must have either 'when' or 'otherwise'"
                    )
                if t.otherwise:
                    otherwise_count += 1

            # 11. At most one otherwise transition per node
            if otherwise_count > 1:
                errors.append(
                    f"Node '{nid}': has {otherwise_count} 'otherwise' transitions "
                    "(at most one fallback allowed)"
                )

            # 8. Node goto target exists
            if node.goto is not None and node.goto not in self.nodes:
                errors.append(f"Node '{nid}': goto target '{node.goto}' does not exist")

        # 9. Reachability from entry
        if entry_nodes:
            reachable = self.reachable_node_ids(entry_nodes[0])
            unreachable = set(self.nodes.keys()) - reachable
            if unreachable:
                errors.append(
                    f"Unreachable nodes (not reachable from entry): {sorted(unreachable)}"
                )

        # 10. Cycles without exits — every reachable node must have a path to
        #     at least one terminal node.  Nodes that are reachable from entry
        #     but cannot reach any terminal are trapped in cycles.
        if entry_nodes and terminal_nodes:
            can_reach_terminal = self.nodes_reaching_terminal()
            reachable = self.reachable_node_ids(entry_nodes[0])
            trapped: set[str] = set()
            for nid in reachable - can_reach_terminal:
                node = self.nodes.get(nid)
                # wait_for_human nodes without explicit exit paths are handled
                # by the executor at resume time — don't flag them.
                if node and node.wait_for_human and not node.transitions and node.goto is None:
                    continue
                trapped.add(nid)
            if trapped:
                errors.append(
                    f"Nodes in cycles without exit to terminal: {sorted(trapped)} "
                    "— these nodes are reachable from entry but cannot reach "
                    "any terminal node"
                )

        return errors

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for ``json.dumps()``."""
        d: dict[str, Any] = {
            "id": self.id,
            "version": self.version,
            "source_hash": self.source_hash,
            "triggers": [t.to_value() for t in self.triggers],
            "scope": self.scope,
            "nodes": {nid: node.to_dict() for nid, node in self.nodes.items()},
        }
        if self.cooldown_seconds is not None:
            d["cooldown_seconds"] = self.cooldown_seconds
        if self.max_tokens is not None:
            d["max_tokens"] = self.max_tokens
        if self.pause_timeout_seconds is not None:
            d["pause_timeout_seconds"] = self.pause_timeout_seconds
        if self.llm_config is not None:
            d["llm_config"] = self.llm_config.to_dict()
        if self.transition_llm_config is not None:
            d["transition_llm_config"] = self.transition_llm_config.to_dict()
        if self.compiled_at is not None:
            d["compiled_at"] = self.compiled_at
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompiledPlaybook:
        """Deserialize from a plain dict (e.g. parsed JSON)."""
        nodes = {nid: PlaybookNode.from_dict(nd) for nid, nd in data.get("nodes", {}).items()}
        llm_cfg = LlmConfig.from_dict(data["llm_config"]) if "llm_config" in data else None
        trans_cfg = (
            LlmConfig.from_dict(data["transition_llm_config"])
            if "transition_llm_config" in data
            else None
        )
        return cls(
            id=data["id"],
            version=data.get("version", 1),
            source_hash=data.get("source_hash", ""),
            triggers=data.get("triggers", []),
            scope=data.get("scope", "system"),
            nodes=nodes,
            cooldown_seconds=data.get("cooldown_seconds"),
            max_tokens=data.get("max_tokens"),
            pause_timeout_seconds=data.get("pause_timeout_seconds"),
            llm_config=llm_cfg,
            transition_llm_config=trans_cfg,
            compiled_at=data.get("compiled_at"),
        )

    @classmethod
    def from_json(cls, json_string: str) -> tuple[CompiledPlaybook, list[str]]:
        """Parse from a JSON string with duplicate key detection.

        Unlike ``from_dict(json.loads(s))``, this method detects duplicate
        keys in the JSON source — which Python's ``json.loads`` silently
        discards (keeping only the last value).  Duplicate node names are
        reported as parse-level errors in the returned list.

        Returns
        -------
        tuple[CompiledPlaybook, list[str]]
            The parsed playbook and a list of parse-level error strings
            (empty if no issues).  Call :meth:`validate` separately for
            structural graph validation.
        """
        import json as json_mod

        duplicate_keys: list[str] = []

        def _check_pairs(pairs: list[tuple[str, Any]]) -> dict:
            seen: set[str] = set()
            for key, _ in pairs:
                if key in seen:
                    duplicate_keys.append(key)
                seen.add(key)
            return dict(pairs)

        data = json_mod.loads(json_string, object_pairs_hook=_check_pairs)
        pb = cls.from_dict(data)

        errors: list[str] = []
        if duplicate_keys:
            unique_dups = sorted(set(duplicate_keys))
            errors.append(
                f"Duplicate node/key names in JSON: {unique_dups} "
                "(last definition wins, earlier definitions are lost)"
            )

        return pb, errors


# ---------------------------------------------------------------------------
# Playbook Run (execution record)
# ---------------------------------------------------------------------------


@dataclass
class NodeTraceEntry:
    """One entry in a playbook run's node trace — records the path taken.

    Captures timing and outcome for each node visited during execution,
    providing the data needed for dashboard visualization and debugging.
    """

    node_id: str
    started_at: float = 0.0
    completed_at: float | None = None
    status: str = "running"  # running, completed, failed, skipped

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "node_id": self.node_id,
            "started_at": self.started_at,
            "status": self.status,
        }
        if self.completed_at is not None:
            d["completed_at"] = self.completed_at
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeTraceEntry:
        return cls(
            node_id=data["node_id"],
            started_at=data.get("started_at", 0.0),
            completed_at=data.get("completed_at"),
            status=data.get("status", "running"),
        )


@dataclass
class PlaybookRun:
    """A single execution record of a playbook.

    Captures the full lifecycle of one playbook invocation: which playbook
    triggered, the event that started it, current execution state, conversation
    history (for pause/resume), and the path taken through the graph.

    For paused runs (human-in-the-loop), the full conversation history is
    persisted so the run can resume exactly where it left off, even across
    process restarts.

    See ``docs/specs/design/playbooks.md`` Section 6 — Run Persistence.
    """

    run_id: str
    playbook_id: str
    playbook_version: int
    trigger_event: dict[str, Any] = field(default_factory=dict)
    status: PlaybookRunStatus = PlaybookRunStatus.RUNNING
    current_node: str | None = None
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    node_trace: list[NodeTraceEntry] = field(default_factory=list)
    tokens_used: int = 0
    started_at: float = 0.0
    completed_at: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "run_id": self.run_id,
            "playbook_id": self.playbook_id,
            "playbook_version": self.playbook_version,
            "trigger_event": self.trigger_event,
            "status": self.status.value,
            "tokens_used": self.tokens_used,
            "started_at": self.started_at,
            "node_trace": [e.to_dict() for e in self.node_trace],
        }
        if self.current_node is not None:
            d["current_node"] = self.current_node
        if self.conversation_history:
            d["conversation_history"] = self.conversation_history
        if self.completed_at is not None:
            d["completed_at"] = self.completed_at
        if self.error is not None:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaybookRun:
        return cls(
            run_id=data["run_id"],
            playbook_id=data["playbook_id"],
            playbook_version=data.get("playbook_version", 1),
            trigger_event=data.get("trigger_event", {}),
            status=PlaybookRunStatus(data.get("status", "running")),
            current_node=data.get("current_node"),
            conversation_history=data.get("conversation_history", []),
            node_trace=[NodeTraceEntry.from_dict(e) for e in data.get("node_trace", [])],
            tokens_used=data.get("tokens_used", 0),
            started_at=data.get("started_at", 0.0),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
        )


# ---------------------------------------------------------------------------
# JSON Schema generation
# ---------------------------------------------------------------------------


def generate_json_schema() -> dict[str, Any]:
    """Generate a JSON Schema (draft 2020-12) for the compiled playbook format.

    The schema is derived from the dataclass definitions above so the two
    representations stay in sync.  The generated schema is suitable for:

    - Validating LLM compilation output before accepting it
    - Providing to the LLM as the target schema during compilation
    - Documenting the compiled format for external consumers

    Returns
    -------
    dict
        A JSON-Schema-compatible dict ready for ``json.dumps()``.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://agent-queue.dev/schemas/compiled-playbook.json",
        "title": "Compiled Playbook",
        "description": (
            "Runtime artifact produced by LLM compilation of a playbook markdown file. "
            "Defines a directed graph of LLM decision points (nodes) connected by "
            "conditional transitions (edges). See docs/specs/design/playbooks.md §5."
        ),
        "type": "object",
        "required": ["id", "version", "source_hash", "triggers", "scope", "nodes"],
        "additionalProperties": False,
        "properties": {
            "id": {
                "type": "string",
                "description": "Unique playbook identifier.",
                "minLength": 1,
            },
            "version": {
                "type": "integer",
                "description": "Auto-incremented on each recompilation.",
                "minimum": 1,
            },
            "source_hash": {
                "type": "string",
                "description": "Hash of source markdown for change detection.",
                "minLength": 1,
            },
            "triggers": {
                "type": "array",
                "description": (
                    "Event triggers that start this playbook. Each item is either "
                    "a string (event type, matches all events of that type) or an "
                    "object with 'event_type' and optional 'filter' for payload "
                    "filtering (see spec §10 Composability)."
                ),
                "items": {
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {
                            "type": "object",
                            "required": ["event_type"],
                            "additionalProperties": False,
                            "properties": {
                                "event_type": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "The event type to trigger on.",
                                },
                                "filter": {
                                    "type": "object",
                                    "description": (
                                        "Payload filter — all key/value pairs must "
                                        "match fields in the event data."
                                    ),
                                },
                            },
                        },
                    ]
                },
                "minItems": 1,
            },
            "scope": {
                "type": "string",
                "description": (
                    "Where this playbook applies: 'system', 'project', or 'agent-type:{type}'."
                ),
                "pattern": r"^(system|project|agent-type:.+)$",
            },
            "cooldown_seconds": {
                "type": "integer",
                "description": "Minimum seconds between executions.",
                "minimum": 0,
            },
            "max_tokens": {
                "type": "integer",
                "description": ("Token budget for the entire run. Run fails if exceeded."),
                "minimum": 1,
            },
            "compiled_at": {
                "type": "string",
                "description": "ISO-8601 UTC timestamp of when this version was compiled.",
            },
            "llm_config": {"$ref": "#/$defs/llm_config"},
            "transition_llm_config": {
                "$ref": "#/$defs/llm_config",
                "description": (
                    "Default LLM config for transition classification calls. "
                    "Allows routing transitions to a cheaper/faster model "
                    "while keeping node execution on a capable model."
                ),
            },
            "nodes": {
                "type": "object",
                "description": (
                    "Map of node ID to node definition. Must contain exactly one "
                    "entry node and at least one terminal node."
                ),
                "minProperties": 1,
                "additionalProperties": {"$ref": "#/$defs/node"},
            },
        },
        "$defs": {
            "llm_config": {
                "type": "object",
                "description": "LLM provider/model override.",
                "additionalProperties": False,
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": ("Chat provider name (e.g. 'anthropic', 'google')."),
                    },
                    "model": {
                        "type": "string",
                        "description": ("Model identifier (e.g. 'claude-sonnet-4-20250514')."),
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum response tokens per LLM call.",
                        "minimum": 1,
                    },
                    "temperature": {
                        "type": "number",
                        "description": "Sampling temperature (0.0-1.0).",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
            },
            "node": {
                "type": "object",
                "description": (
                    "A single step in the playbook graph. Either an action node "
                    "(has prompt + transitions/goto), a terminal node (terminal=true), "
                    "or a human-gate node (wait_for_human=true)."
                ),
                "additionalProperties": False,
                "properties": {
                    "entry": {
                        "type": "boolean",
                        "description": (
                            "If true, this is the starting node. Exactly one per playbook."
                        ),
                        "default": False,
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Focused instruction for the LLM at this step. "
                            "Required for non-terminal nodes."
                        ),
                    },
                    "transitions": {
                        "type": "array",
                        "description": (
                            "Conditional edges. Mutually exclusive with 'goto'. "
                            "Evaluated by a separate LLM call or structured check."
                        ),
                        "items": {"$ref": "#/$defs/transition"},
                        "minItems": 1,
                    },
                    "goto": {
                        "type": "string",
                        "description": (
                            "Unconditional next node ID. Mutually exclusive with 'transitions'."
                        ),
                    },
                    "terminal": {
                        "type": "boolean",
                        "description": "If true, execution ends at this node.",
                        "default": False,
                    },
                    "wait_for_human": {
                        "type": "boolean",
                        "description": ("If true, pause execution and surface for human review."),
                        "default": False,
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": ("Max time for this node's LLM call before failing."),
                        "minimum": 1,
                    },
                    "llm_config": {"$ref": "#/$defs/llm_config"},
                    "transition_llm_config": {
                        "$ref": "#/$defs/llm_config",
                        "description": (
                            "LLM config override for transition classification "
                            "calls from this node. Falls back to playbook-level "
                            "transition_llm_config, then node llm_config, then "
                            "playbook llm_config."
                        ),
                    },
                    "for_each": {
                        "type": "object",
                        "description": (
                            "Iterate over an array from a prior node's output. "
                            "The node's prompt executes once per item with "
                            "{{as_variable}} available in the template. Results "
                            "are optionally collected into a named array."
                        ),
                        "required": ["source", "as"],
                        "additionalProperties": False,
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": (
                                    "Dot-path to an array in node_outputs "
                                    "(e.g. 'discover_projects.active_projects')."
                                ),
                            },
                            "as": {
                                "type": "string",
                                "description": (
                                    "Variable name for the current item, "
                                    "available in the prompt via {{name}} or "
                                    "{{name.field}}."
                                ),
                            },
                            "collect": {
                                "type": "string",
                                "description": (
                                    "Name to store all iteration results as an "
                                    "array in node_outputs for downstream nodes."
                                ),
                            },
                            "filter": {
                                "type": "string",
                                "description": (
                                    "Expression to filter items. Only items where "
                                    "the expression is truthy are included. Use "
                                    "'item.field == value' syntax."
                                ),
                            },
                        },
                    },
                    "output": {
                        "type": "object",
                        "description": (
                            "Extract structured data from tool results and store "
                            "in node_outputs for downstream nodes. Without this, "
                            "the node's text response is stored."
                        ),
                        "additionalProperties": False,
                        "properties": {
                            "extract": {
                                "type": "string",
                                "description": (
                                    "JSON key to extract from the last tool result "
                                    "(e.g. 'findings', 'projects'). Dot-paths "
                                    "supported."
                                ),
                            },
                            "as": {
                                "type": "string",
                                "description": (
                                    "Name to store under in node_outputs. "
                                    "Defaults to the node ID if omitted."
                                ),
                            },
                        },
                    },
                },
                # We can't easily express "prompt required if not terminal" in
                # JSON Schema without oneOf/if-then-else.  Use a simple rule:
                # terminal nodes don't need prompt, all others do.
                "if": {
                    "not": {
                        "properties": {"terminal": {"const": True}},
                        "required": ["terminal"],
                    }
                },
                "then": {"required": ["prompt"]},
            },
            "transition": {
                "type": "object",
                "description": (
                    "A conditional edge connecting two nodes. Must have either "
                    "'when' (condition) or 'otherwise' (default fallback)."
                ),
                "required": ["goto"],
                "additionalProperties": False,
                "properties": {
                    "when": {
                        "description": (
                            "Natural language condition (string) OR structured "
                            "function-call expression (object) evaluated to "
                            "determine if this transition should be followed."
                        ),
                        "oneOf": [
                            {"type": "string", "minLength": 1},
                            {
                                "type": "object",
                                "description": (
                                    "Structured expression for deterministic "
                                    "evaluation without an LLM call."
                                ),
                            },
                        ],
                    },
                    "goto": {
                        "type": "string",
                        "description": "Target node ID.",
                        "minLength": 1,
                    },
                    "otherwise": {
                        "type": "boolean",
                        "description": (
                            "If true, this is the default/fallback transition "
                            "when no other 'when' condition matches."
                        ),
                        "const": True,
                    },
                },
                # Must have either 'when' or 'otherwise'
                "oneOf": [
                    {"required": ["when", "goto"]},
                    {"required": ["otherwise", "goto"]},
                ],
            },
        },
    }
