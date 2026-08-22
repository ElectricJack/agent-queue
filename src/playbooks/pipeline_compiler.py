"""Deterministic compiler for ``kind: pipeline`` playbooks.

Parses YAML frontmatter + a single fenced ```json``` block in the body. The
JSON block IS the node graph — no LLM, byte-exact, instant. Compile-time
validation refuses prompt nodes, LLM (natural-language) transitions, and any
command outside the whitelist. Invalid → previous compiled version stays
active (same policy as the LLM compiler, enforced at the manager layer).
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

PIPELINE_COMMAND_WHITELIST: frozenset[str] = frozenset(
    {
        "create_task",
        "ensure_task",
        "edit_task",
        "add_dependency",
        "gate_create",
        "gate_resolve",
        "list_tasks",
        "get_downstream_tasks",
        "task_batch_commit",
    }
)


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


def _extract_json(body: str) -> tuple[dict | None, str | None]:
    m = _JSON_BLOCK_RE.search(body)
    if not m:
        return None, "No fenced ```json``` block in pipeline body"
    try:
        return json.loads(m.group(1)), None
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON in body: {exc.msg} (line {exc.lineno})"


def _validate_frontmatter(fm: dict) -> list[str]:
    errs: list[str] = []
    if fm.get("kind") != "pipeline":
        errs.append("Pipeline compiler requires frontmatter 'kind: pipeline'")
    if not fm.get("role"):
        errs.append("Pipeline frontmatter requires 'role: <name>'")
    if not fm.get("id"):
        errs.append("Frontmatter requires 'id'")
    if not fm.get("scope"):
        errs.append("Frontmatter requires 'scope' (system|project|agent-type:...)")
    triggers = fm.get("triggers")
    if not triggers or not isinstance(triggers, list):
        errs.append("Frontmatter 'triggers' must be a non-empty list")
    return errs


def _validate_node(nid: str, node: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(node, dict):
        return [f"Node '{nid}': must be an object"]
    if node.get("prompt"):
        errs.append(f"Node '{nid}': pipeline nodes must not have 'prompt'")
    if "transitions" in node:
        errs.append(
            f"Node '{nid}': natural-language 'transitions' not allowed in pipelines; "
            "use 'on_success' / 'on_failure' instead"
        )
    if node.get("terminal"):
        return errs
    cmd = node.get("command")
    if not cmd:
        errs.append(f"Node '{nid}': action node must have 'command'")
    elif cmd not in PIPELINE_COMMAND_WHITELIST:
        errs.append(
            f"Node '{nid}': command '{cmd}' not in pipeline whitelist "
            f"({sorted(PIPELINE_COMMAND_WHITELIST)})"
        )
    if "args" in node and not isinstance(node["args"], dict):
        errs.append(f"Node '{nid}': 'args' must be an object")
    if "on_success" in node and not isinstance(node["on_success"], str):
        errs.append(f"Node '{nid}': 'on_success' must be a node id string")
    if "on_failure" in node and not isinstance(node["on_failure"], str):
        errs.append(f"Node '{nid}': 'on_failure' must be a node id string")
    fe = node.get("for_each")
    if fe is not None:
        if not isinstance(fe, dict) or "source" not in fe or "as" not in fe:
            errs.append(
                f"Node '{nid}': 'for_each' must be an object with 'source' and 'as'"
            )
    out = node.get("output")
    if out is not None and (not isinstance(out, dict) or "as" not in out):
        errs.append(f"Node '{nid}': 'output' must be an object with 'as'")
    return errs


def compile_pipeline(markdown: str, *, existing_version: int = 0) -> CompilationResult:
    """Parse + validate a pipeline playbook markdown file.

    Success → :class:`CompilationResult` with ``playbook`` populated (a
    :class:`CompiledPlaybook` whose ``to_dict()`` carries ``kind: pipeline``
    and ``role`` at the top level plus action nodes in ``nodes``).
    """
    fm, body = _parse_frontmatter(markdown)
    fm_errs = _validate_frontmatter(fm)
    if fm_errs:
        return CompilationResult(success=False, errors=fm_errs)

    raw, err = _extract_json(body)
    if err:
        return CompilationResult(success=False, errors=[err])

    nodes = raw.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return CompilationResult(
            success=False, errors=["Pipeline JSON must have a non-empty 'nodes' object"]
        )
    entry_id = raw.get("entry")
    if not entry_id or entry_id not in nodes:
        return CompilationResult(
            success=False,
            errors=["Pipeline JSON 'entry' must reference an existing node id"],
        )

    errs: list[str] = []
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
                errs.append(f"Node '{nid}' {hop} target '{target}' does not exist")
    if not has_terminal:
        errs.append("Pipeline must have at least one terminal node")
    if errs:
        return CompilationResult(success=False, errors=errs)

    src_hash = hashlib.sha256(markdown.encode()).hexdigest()[:16]
    version = existing_version + 1

    normalized_nodes: dict = {}
    for nid, node in nodes.items():
        out_node: dict = {"kind": "action"}
        if nid == entry_id:
            out_node["entry"] = True
        for k in ("command", "args", "on_success", "on_failure", "output", "for_each", "terminal"):
            if k in node:
                out_node[k] = node[k]
        normalized_nodes[nid] = out_node

    compiled = {
        "id": fm["id"],
        "kind": "pipeline",
        "role": fm["role"],
        "version": version,
        "source_hash": src_hash,
        "triggers": fm["triggers"],
        "scope": fm["scope"],
        "nodes": normalized_nodes,
        "compiled_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        pb = _pipeline_from_dict(compiled)
    except Exception as exc:
        return CompilationResult(success=False, errors=[f"Deserialization failed: {exc}"])

    return CompilationResult(
        success=True, playbook=pb, source_hash=src_hash, raw_json=compiled
    )


def _pipeline_from_dict(data: dict) -> CompiledPlaybook:
    """Build a CompiledPlaybook from pipeline JSON, bypassing prompt validation.

    Pipeline nodes are stored with their action metadata attached via
    ``node.__dict__['action']`` so the runtime executor (Task 9) can dispatch
    to CommandHandler. The returned ``CompiledPlaybook`` also carries
    ``kind='pipeline'`` and ``role`` as instance attributes surfaced through
    a patched ``to_dict``.
    """
    nodes: dict[str, PlaybookNode] = {}
    for nid, nd in data["nodes"].items():
        n = PlaybookNode()
        n.entry = bool(nd.get("entry"))
        n.terminal = bool(nd.get("terminal"))
        # Stash action metadata under a fresh attribute the pipeline runner reads.
        n.__dict__["action"] = {
            "command": nd.get("command"),
            "args": nd.get("args") or {},
            "on_success": nd.get("on_success"),
            "on_failure": nd.get("on_failure"),
            "output": nd.get("output"),
            "for_each": nd.get("for_each"),
        }
        nodes[nid] = n

    pb = CompiledPlaybook(
        id=data["id"],
        version=data["version"],
        source_hash=data["source_hash"],
        triggers=data["triggers"],
        scope=data["scope"],
        nodes=nodes,
        compiled_at=data.get("compiled_at"),
    )
    # Attach top-level pipeline metadata for downstream consumers.
    pb.__dict__["kind"] = "pipeline"
    pb.__dict__["role"] = data["role"]

    # Patch to_dict on this instance to surface pipeline-specific fields plus
    # action-node metadata (the stock to_dict drops non-PlaybookNode fields).
    _orig_to_dict = pb.to_dict

    def _pipeline_to_dict() -> dict[str, Any]:
        d = _orig_to_dict()
        d["kind"] = "pipeline"
        d["role"] = pb.__dict__["role"]
        # Replace nodes with pipeline-shaped dicts (stock to_dict drops action metadata).
        out_nodes: dict[str, dict] = {}
        for nid, node in pb.nodes.items():
            nd: dict = {"kind": "action"}
            if node.entry:
                nd["entry"] = True
            if node.terminal:
                nd["terminal"] = True
            action = node.__dict__.get("action") or {}
            for k in ("command", "args", "on_success", "on_failure", "output", "for_each"):
                v = action.get(k)
                if v is not None and v != {}:
                    nd[k] = v
                elif k == "args" and action.get("command"):
                    nd[k] = action.get("args") or {}
            out_nodes[nid] = nd
        d["nodes"] = out_nodes
        return d

    pb.to_dict = _pipeline_to_dict  # type: ignore[method-assign]
    return pb
