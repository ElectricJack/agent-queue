"""Deterministic compiler for ``kind: pipeline`` playbooks.

Parses YAML frontmatter + a single fenced ```json``` block in the body. The
JSON block IS the node graph — no LLM, byte-exact, instant. Compile-time
validation refuses prompt nodes, LLM (natural-language) transitions, and any
command outside the whitelist. Invalid → previous compiled version stays
active (same policy as the LLM compiler, enforced at the manager layer).

Pipeline metadata (``kind: pipeline``, ``role``, per-node ``action`` payload)
lands on real optional fields of :class:`CompiledPlaybook` and
:class:`PlaybookNode` — no monkey-patches, no ``__dict__`` stashes — so the
data round-trips cleanly through :class:`CompiledPlaybookStore`.

**Frontmatter ``scope`` here is not authoritative.** This module reads it
only to validate that the author declared one; the installed value is
overwritten by
:func:`~src.playbooks.compiler.apply_source_authority` at the manager /
install layer, which derives it from the vault path (Playbook V2 Package 0
§3.8) — ``compile_pipeline`` is a pure ``markdown -> CompilationResult``
function with no path argument, so it cannot make that call itself. A file
under ``projects/acme/playbooks/`` cannot declare ``scope: system``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import yaml

from src.playbooks.compiler import CompilationResult
from src.playbooks.models import CompiledPlaybook, PlaybookNode

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

# V1's executable surface stays frozen while V2 adds contracts for reviewed
# LLM tool loops. A new contract must not silently become callable from a
# legacy embedded action graph.
PIPELINE_COMMAND_WHITELIST: frozenset[str] = frozenset(
    {
        "add_dependency",
        "create_task",
        "edit_task",
        "ensure_task",
        "gate_create",
        "gate_resolve",
        "get_downstream_tasks",
        "list_tasks",
        "stop_task",
        "task_batch_commit",
        "task_route",
    }
)


def _err(node: str | None, field: str | None, message: str) -> dict[str, Any]:
    """Build a structured compile-error record."""
    return {"node": node, "field": field, "message": message}


def _errors_result(records: list[dict[str, Any]]) -> CompilationResult:
    """Build a failed :class:`CompilationResult` from structured records.

    ``errors`` (list of strings) is derived from the records so existing
    callers that log/join them keep working; ``structured_errors`` carries
    the full ``{node, field, message}`` shape for Phase 6's ``playbook_validate``.
    """
    strings: list[str] = []
    for r in records:
        loc = r.get("node") or ""
        fld = r.get("field") or ""
        prefix = ""
        if loc and fld:
            prefix = f"Node '{loc}' [{fld}]: "
        elif loc:
            prefix = f"Node '{loc}': "
        elif fld:
            prefix = f"[{fld}] "
        strings.append(f"{prefix}{r['message']}")
    return CompilationResult(success=False, errors=strings, structured_errors=records)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, text
    return meta, parts[2]


def _extract_json(body: str) -> tuple[dict | None, dict[str, Any] | None]:
    m = _JSON_BLOCK_RE.search(body)
    if not m:
        return None, _err(None, "body", "No fenced ```json``` block in pipeline body")
    try:
        return json.loads(m.group(1)), None
    except json.JSONDecodeError as exc:
        return None, _err(None, "body", f"Invalid JSON in body: {exc.msg} (line {exc.lineno})")


def _validate_frontmatter(fm: dict) -> list[dict[str, Any]]:
    errs: list[dict[str, Any]] = []
    if fm.get("kind") != "pipeline":
        errs.append(_err(None, "kind", "Pipeline compiler requires frontmatter 'kind: pipeline'"))
    if not fm.get("role"):
        errs.append(_err(None, "role", "Pipeline frontmatter requires 'role: <name>'"))
    if not fm.get("id"):
        errs.append(_err(None, "id", "Frontmatter requires 'id'"))
    if not fm.get("scope"):
        errs.append(
            _err(None, "scope", "Frontmatter requires 'scope' (system|project|agent-type:...)")
        )
    triggers = fm.get("triggers")
    if not triggers or not isinstance(triggers, list):
        errs.append(_err(None, "triggers", "Frontmatter 'triggers' must be a non-empty list"))
    return errs


def _validate_node(nid: str, node: Any) -> list[dict[str, Any]]:
    errs: list[dict[str, Any]] = []
    if not isinstance(node, dict):
        return [_err(nid, None, "must be an object")]
    if "entry" in node:
        # Top-level 'entry' pointer is the single source of truth for the
        # entry node — nodes must not claim entry status themselves.
        errs.append(
            _err(nid, "entry", "pipeline nodes must not set 'entry'; use top-level 'entry' pointer")
        )
    if node.get("prompt"):
        errs.append(_err(nid, "prompt", "pipeline nodes must not have 'prompt'"))
    if "transitions" in node:
        errs.append(
            _err(
                nid,
                "transitions",
                "natural-language 'transitions' not allowed in pipelines; use 'on_success' / 'on_failure' instead",
            )
        )
    if node.get("terminal"):
        return errs
    cmd = node.get("command")
    if not cmd:
        errs.append(_err(nid, "command", "action node must have 'command'"))
    elif cmd not in PIPELINE_COMMAND_WHITELIST:
        errs.append(
            _err(
                nid,
                "command",
                f"command '{cmd}' not in pipeline whitelist ({sorted(PIPELINE_COMMAND_WHITELIST)})",
            )
        )
    if "args" in node and not isinstance(node["args"], dict):
        errs.append(_err(nid, "args", "'args' must be an object"))
    if "on_success" in node and not isinstance(node["on_success"], str):
        errs.append(_err(nid, "on_success", "'on_success' must be a node id string"))
    if "on_failure" in node and not isinstance(node["on_failure"], str):
        errs.append(_err(nid, "on_failure", "'on_failure' must be a node id string"))
    fe = node.get("for_each")
    if fe is not None:
        if not isinstance(fe, dict) or "source" not in fe or "as" not in fe:
            errs.append(
                _err(nid, "for_each", "'for_each' must be an object with 'source' and 'as'")
            )
    out = node.get("output")
    if out is not None and (not isinstance(out, dict) or "as" not in out):
        errs.append(_err(nid, "output", "'output' must be an object with 'as'"))
    return errs


def _reachable(nodes: dict[str, dict], entry_id: str) -> set[str]:
    """Forward BFS from ``entry_id`` over on_success/on_failure edges."""
    visited: set[str] = set()
    queue = [entry_id]
    while queue:
        nid = queue.pop(0)
        if nid in visited or nid not in nodes:
            continue
        visited.add(nid)
        node = nodes[nid]
        if not isinstance(node, dict):
            continue
        for hop in ("on_success", "on_failure"):
            tgt = node.get(hop)
            if isinstance(tgt, str) and tgt not in visited:
                queue.append(tgt)
    return visited


def _reaches_terminal(nodes: dict[str, dict]) -> set[str]:
    """Reverse BFS from terminal nodes over on_success/on_failure edges."""
    terminals = [nid for nid, nd in nodes.items() if isinstance(nd, dict) and nd.get("terminal")]
    if not terminals:
        return set()
    reverse: dict[str, set[str]] = {nid: set() for nid in nodes}
    for nid, nd in nodes.items():
        if not isinstance(nd, dict):
            continue
        for hop in ("on_success", "on_failure"):
            tgt = nd.get(hop)
            if isinstance(tgt, str) and tgt in reverse:
                reverse[tgt].add(nid)
    visited: set[str] = set()
    queue = list(terminals)
    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        for pred in reverse.get(cur, ()):
            if pred not in visited:
                queue.append(pred)
    return visited


def _normalize_nodes(nodes: dict, entry_id: str, prefix: str = "") -> dict[str, PlaybookNode]:
    """Compile a flat node dict into normalized PlaybookNode objects.

    When *prefix* is non-empty, node IDs are rewritten to ``{prefix}-{nid}``
    and internal ``on_success``/``on_failure`` references are updated to match.
    This allows multiple rules to coexist in a single CompiledPlaybook without
    ID collisions.
    """

    def _remap(val: Any) -> Any:
        """Prefix a node reference if a prefix is active."""
        if prefix and isinstance(val, str):
            return f"{prefix}-{val}"
        return val

    normalized: dict[str, PlaybookNode] = {}
    for nid, node in nodes.items():
        out_nid = f"{prefix}-{nid}" if prefix else nid
        n = PlaybookNode()
        n.entry = nid == entry_id
        n.terminal = bool(node.get("terminal"))
        if not n.terminal:
            n.action = {
                "command": node.get("command"),
                "args": node.get("args") or {},
                "on_success": _remap(node.get("on_success")),
                "on_failure": _remap(node.get("on_failure")),
                "output": node.get("output"),
                "for_each": node.get("for_each"),
            }
        normalized[out_nid] = n
    return normalized


def _validate_and_check_graph(nodes: dict, entry_id: str) -> list[dict[str, Any]]:
    """Validate nodes dict and perform graph-level reachability checks."""
    errs: list[dict[str, Any]] = []
    has_terminal = False
    for nid, node in nodes.items():
        errs.extend(_validate_node(nid, node))
        if isinstance(node, dict) and node.get("terminal"):
            has_terminal = True
        if not isinstance(node, dict):
            continue
        for hop in ("on_success", "on_failure"):
            target = node.get(hop)
            if target and isinstance(target, str) and target not in nodes:
                errs.append(_err(nid, hop, f"{hop} target '{target}' does not exist"))
    if not has_terminal:
        errs.append(_err(None, "nodes", "Pipeline must have at least one terminal node"))

    if errs:
        return errs

    reachable = _reachable(nodes, entry_id)
    unreachable = set(nodes.keys()) - reachable
    if unreachable:
        errs.append(
            _err(
                None,
                "nodes",
                f"Unreachable nodes (not reachable from entry '{entry_id}'): {sorted(unreachable)}",
            )
        )

    can_reach_terminal = _reaches_terminal(nodes)
    trapped = sorted(reachable - can_reach_terminal)
    if trapped:
        errs.append(
            _err(
                None,
                "nodes",
                f"Nodes reachable from entry but with no path to a terminal: {trapped} "
                "— on_success/on_failure cycle has no exit",
            )
        )

    return errs


def compile_pipeline(markdown: str, *, existing_version: int = 0) -> CompilationResult:
    """Parse + validate a pipeline playbook markdown file.

    Supports two JSON formats:

    **Single-graph** (legacy)::

        {"entry": "node-id", "nodes": {...}}

    **Multi-rule** (new — multiple triggers in one file)::

        {
          "rules": [
            {"id": "rule-id", "on": "event.type", "entry": "node-id", "nodes": {...}},
            ...
          ]
        }

    Multi-rule pipelines compile all rules into a single merged node graph
    with rule-prefixed node IDs (``{rule-id}-{node-id}``).  A
    ``pipeline_rules`` dict maps each ``on`` event type to its prefixed entry
    node ID so the orchestrator can select the correct subgraph at dispatch
    time.

    Success → :class:`CompilationResult` with ``playbook`` populated (a
    :class:`CompiledPlaybook` whose ``kind`` is ``"pipeline"`` and whose
    nodes each carry an ``action`` payload dict).
    """
    fm, body = _parse_frontmatter(markdown)
    fm_errs = _validate_frontmatter(fm)
    if fm_errs:
        return _errors_result(fm_errs)

    raw, err = _extract_json(body)
    if err:
        return _errors_result([err])

    src_hash = hashlib.sha256(markdown.encode()).hexdigest()[:16]
    version = existing_version + 1

    # --- Multi-rule format ---
    if "rules" in raw and isinstance(raw.get("rules"), list):
        return _compile_multi_rule(raw["rules"], fm, src_hash, version, markdown)

    # --- Single-graph format (legacy) ---
    nodes = raw.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return _errors_result(
            [_err(None, "nodes", "Pipeline JSON must have a non-empty 'nodes' object")]
        )
    entry_id = raw.get("entry")
    if not entry_id or entry_id not in nodes:
        return _errors_result(
            [_err(None, "entry", "Pipeline JSON 'entry' must reference an existing node id")]
        )

    errs = _validate_and_check_graph(nodes, entry_id)
    if errs:
        return _errors_result(errs)

    normalized_nodes = _normalize_nodes(nodes, entry_id)

    try:
        pb = CompiledPlaybook(
            id=fm["id"],
            version=version,
            source_hash=src_hash,
            triggers=fm["triggers"],
            scope=fm["scope"],
            nodes=normalized_nodes,
            compiled_at=datetime.now(timezone.utc).isoformat(),
            kind="pipeline",
            role=fm["role"],
        )
    except Exception as exc:  # pragma: no cover - defensive
        return _errors_result([_err(None, None, f"Deserialization failed: {exc}")])

    return CompilationResult(
        success=True,
        playbook=pb,
        source_hash=src_hash,
        raw_json=pb.to_dict(),
    )


def _validate_when(when: Any, rule_id: str) -> list[dict[str, Any]]:
    """Reject vacuous ``when`` shapes at compile time.

    ``{"all": []}`` silently evaluates True → the rule fires on every
    event. ``{"any": []}`` silently evaluates False → the rule never
    fires. Both are almost certainly author bugs. Nested clauses are
    validated recursively.
    """
    if not isinstance(when, dict):
        return []
    errs: list[dict[str, Any]] = []
    if "all" in when:
        clauses = when.get("all")
        if isinstance(clauses, list) and not clauses:
            errs.append(
                _err(
                    rule_id,
                    "when.all",
                    "'when.all' must not be empty — vacuous True fires the rule on every event",
                )
            )
        elif isinstance(clauses, list):
            for c in clauses:
                errs.extend(_validate_when(c, rule_id))
    if "any" in when:
        clauses = when.get("any")
        if isinstance(clauses, list) and not clauses:
            errs.append(
                _err(
                    rule_id,
                    "when.any",
                    "'when.any' must not be empty — silently False disables the rule",
                )
            )
        elif isinstance(clauses, list):
            for c in clauses:
                errs.extend(_validate_when(c, rule_id))
    comparator_keys = ("truthy", "not_null", "equals", "is_null")
    if "field" in when:
        comparators = [k for k in comparator_keys if k in when]
        if len(comparators) != 1:
            errs.append(
                _err(
                    rule_id,
                    "when",
                    "leaf 'when' clause must have exactly one of "
                    f"truthy|not_null|equals|is_null; got {comparators or 'none'}",
                )
            )
        elif "is_null" in when and not isinstance(when["is_null"], bool):
            errs.append(_err(rule_id, "when.is_null", "'is_null' must be a boolean"))
    elif any(key in when for key in comparator_keys):
        errs.append(_err(rule_id, "when.field", "leaf 'when' clause requires a 'field'"))
    return errs


def _compile_multi_rule(
    rules: list,
    fm: dict,
    src_hash: str,
    version: int,
    markdown: str,
) -> CompilationResult:
    """Compile a multi-rule pipeline into a single merged CompiledPlaybook."""
    if not rules:
        return _errors_result([_err(None, "rules", "Pipeline 'rules' array must not be empty")])

    errs: list[dict[str, Any]] = []
    all_nodes: dict[str, PlaybookNode] = {}
    # event_type → list of rule metas ({entry, when?}) — multiple rules
    # may share a trigger and are dispatched sequentially in author order.
    pipeline_rules: dict[str, list[dict[str, Any]]] = {}
    collected_triggers: list[str] = []

    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errs.append(_err(None, f"rules[{i}]", "each rule must be an object"))
            continue

        rule_id = rule.get("id")
        if not rule_id:
            errs.append(_err(None, f"rules[{i}].id", "rule must have an 'id' field"))
            continue

        on_event = rule.get("on")
        if not on_event:
            errs.append(_err(rule_id, "on", "rule must have an 'on' event type"))
            continue

        nodes = rule.get("nodes")
        if not isinstance(nodes, dict) or not nodes:
            errs.append(_err(rule_id, "nodes", "rule must have a non-empty 'nodes' object"))
            continue

        entry_id = rule.get("entry")
        if not entry_id or entry_id not in nodes:
            errs.append(_err(rule_id, "entry", "'entry' must reference an existing node id"))
            continue

        rule_errs = _validate_and_check_graph(nodes, entry_id)
        if rule_errs:
            # Prefix error node refs with rule_id for clarity
            for e in rule_errs:
                e["node"] = f"{rule_id}/{e['node']}" if e.get("node") else rule_id
            errs.extend(rule_errs)
            continue

        # Compile and merge with prefixed IDs
        normalized = _normalize_nodes(nodes, entry_id, prefix=rule_id)
        all_nodes.update(normalized)
        prefixed_entry = f"{rule_id}-{entry_id}"
        rule_meta: dict[str, Any] = {"entry": prefixed_entry}
        # Preserve optional ``when`` condition for orchestrator-level guard.
        if "when" in rule:
            when_errs = _validate_when(rule["when"], rule_id)
            if when_errs:
                errs.extend(when_errs)
                continue
            rule_meta["when"] = rule["when"]
        pipeline_rules.setdefault(on_event, []).append(rule_meta)
        collected_triggers.append(on_event)

    if errs:
        return _errors_result(errs)

    if not all_nodes:
        return _errors_result([_err(None, "rules", "No valid rules were compiled")])

    # The frontmatter triggers must include all rule event types.
    # If not specified per-rule, we auto-derive from the rules.
    fm_triggers = fm.get("triggers") or []
    all_trigger_types = set(collected_triggers)
    fm_trigger_types = set(
        t if isinstance(t, str) else t.get("event_type", "") for t in fm_triggers
    )
    missing = all_trigger_types - fm_trigger_types
    if missing:
        errs.append(
            _err(
                None,
                "triggers",
                f"Frontmatter 'triggers' must include all rule 'on' values; "
                f"missing: {sorted(missing)}",
            )
        )
        return _errors_result(errs)

    try:
        pb = CompiledPlaybook(
            id=fm["id"],
            version=version,
            source_hash=src_hash,
            triggers=fm_triggers,
            scope=fm["scope"],
            nodes=all_nodes,
            compiled_at=datetime.now(timezone.utc).isoformat(),
            kind="pipeline",
            role=fm["role"],
            pipeline_rules=pipeline_rules,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return _errors_result([_err(None, None, f"Deserialization failed: {exc}")])

    return CompilationResult(
        success=True,
        playbook=pb,
        source_hash=src_hash,
        raw_json=pb.to_dict(),
    )
