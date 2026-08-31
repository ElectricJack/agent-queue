"""``formula_list`` / ``formula_show`` / ``formula_cook`` (swarm-work-model §13).

A formula is a reusable task-graph template registered in
``self.orchestrator.formula_registry`` (:class:`~src.task_graph.formulas.FormulaRegistry`,
loaded from the vault and kept current by the vault watcher — see
``src/task_graph/formulas.py`` and the wiring in
``src/orchestrator/core.py``).

Three commands:

- ``_cmd_formula_list`` — enumerate formulas visible to a project (or the
  system scope alone), never writes.
- ``_cmd_formula_show`` — resolve a formula's ``extends`` chain, substitute
  its vars and validate the result exactly like ``create_task_graph``
  would, without ever writing.  With ``as_cooked``, instead render back the
  snapshot a previous cook actually wrote, ignoring the registry entirely
  (a file can change after the cook; the snapshot is what was really run).
- ``_cmd_formula_cook`` — the write path: resolve, validate, then
  ``create_graph`` in one transaction with a :class:`FormulaProvenance`
  attached, emitting ``formula.cooked`` after a real (non-dry-run) commit.

Resolution order for both ``_cmd_formula_show`` and ``_cmd_formula_cook``:
``resolve_formula`` → var findings are errors → ``parse_graph`` →
``validate_graph`` → errors → (cook only) ``_validate_graph_parent`` →
``create_graph``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.task_graph import (
    GraphParseError,
    create_graph,
    parse_graph,
    split_findings,
    validate_graph,
)
from src.task_graph.creator import FormulaProvenance
from src.task_graph.formulas import (
    FormulaError,
    VarDecl,
    merged_var_decls,
    resolve_formula,
)

logger = logging.getLogger(__name__)


def _var_decl_dict(decl: VarDecl) -> dict[str, Any]:
    return {"required": decl.required, "default": decl.default, "enum": decl.enum}


def _decls_dict(decls: dict[str, VarDecl]) -> dict[str, dict[str, Any]]:
    return {name: _var_decl_dict(decl) for name, decl in decls.items()}


class FormulaCommandsMixin:
    """``_cmd_formula_*`` — see module docstring."""

    # -- shared helpers ------------------------------------------------

    def _formula_registry(self):
        reg = getattr(self.orchestrator, "formula_registry", None)
        if reg is None:
            raise RuntimeError("formula registry not loaded")
        return reg

    def _formula_scope_project(self, args: dict) -> str | None:
        """The project a formula-reading command is pinned to.

        A non-elevated session scope is always pinned to its own project,
        regardless of what the caller passed; every other caller uses the
        explicit ``project_id`` or the handler's active project.
        """
        scope = self._current_scope or {}
        if scope.get("kind") == "session" and not scope.get("elevated"):
            return scope.get("project_id")
        return args.get("project_id") or self._active_project_id

    @staticmethod
    def _formula_vars_arg(args: dict) -> tuple[dict, dict | None]:
        """``(vars, error)`` — *error* is set when ``args["vars"]`` is present and not a mapping."""
        raw = args.get("vars")
        if raw is None:
            return {}, None
        if not isinstance(raw, dict):
            return {}, {"success": False, "error": "vars must be an object of string values"}
        return raw, None

    async def _resolve_formula_graph(self, name: str, project_id: str | None, supplied: dict):
        """resolve → parse → validate.

        Returns ``(resolved, graph, errors, warnings)``.  ``graph`` is
        ``None`` and ``errors``/``warnings`` are empty lists when
        resolution raised (caller checks that separately); ``graph`` is
        ``None`` and ``errors`` holds the var findings when the supplied
        vars fail validation — parsing/validating a document with unresolved
        required vars would only pile on spurious ``unknown_var`` findings.
        """
        resolved = resolve_formula(
            self._formula_registry(), name, project_id=project_id, supplied_vars=supplied or {}
        )
        var_errors = [f for f in resolved.findings if f.is_error]
        if var_errors:
            return resolved, None, var_errors, []
        try:
            graph = parse_graph(resolved.document)
        except GraphParseError as exc:
            return resolved, None, list(exc.errors), []
        findings = await validate_graph(
            graph,
            project_id=project_id,
            db=self.db,
            vault_root=getattr(self.config, "vault_root", None),
        )
        errors, warnings = split_findings(findings)
        return resolved, graph, errors, warnings

    # -- formula_list ----------------------------------------------------

    async def _cmd_formula_list(self, args: dict) -> dict:
        project_id = self._formula_scope_project(args)
        registry = self._formula_registry()
        formulas = registry.list_for_scope(project_id)
        return {
            "success": True,
            "formulas": [
                {
                    "name": f.name,
                    "description": f.description,
                    "scope": f.scope,
                    "extends": f.extends,
                    "vars": _decls_dict(f.vars),
                    "path": f.rel_path,
                }
                for f in formulas
            ],
        }

    # -- formula_show ------------------------------------------------------

    async def _cmd_formula_show(self, args: dict) -> dict:
        as_cooked = args.get("as_cooked")
        if as_cooked:
            return await self._formula_show_as_cooked(as_cooked)

        name = args.get("name")
        if not name:
            return {"success": False, "error": "name or as_cooked is required"}
        project_id = self._formula_scope_project(args)
        supplied, vars_error = self._formula_vars_arg(args)
        if vars_error is not None:
            return vars_error

        try:
            resolved, graph, errors, warnings = await self._resolve_formula_graph(
                name, project_id, supplied
            )
        except FormulaError as exc:
            return {"success": False, "error": str(exc)}

        return {
            "success": not errors,
            "name": resolved.leaf.name,
            "scope": resolved.leaf.scope,
            "path": resolved.leaf.rel_path,
            "chain": [f.name for f in resolved.chain],
            "chain_sha": resolved.chain_sha,
            "vars": {
                "declared": _decls_dict(merged_var_decls(resolved.chain)),
                "effective": resolved.vars,
            },
            # ``graph`` is not yet substituted/validated when var errors (or a
            # structural parse failure) blocked resolution before validation
            # ran — the raw merged document is the best available picture.
            "graph": graph.to_dict() if graph is not None else resolved.document,
            "errors": [e.to_dict() for e in errors],
            "warnings": [w.to_dict() for w in warnings],
        }

    async def _formula_show_as_cooked(self, container_id: str) -> dict:
        """Render the ``formula_snapshot`` a previous cook wrote.

        No registry access, no validation, no writes — this renders exactly
        what was cooked, even if the vault file has since changed.  A
        non-elevated session scope may only read a snapshot on a container
        in its own project.  ``_assert_task_in_scope`` (claim mixin) is not
        enough here on its own: it short-circuits whenever the scope pins a
        ``task_id`` (valid for commands whose target arrives as
        ``args["task_id"]``, which ``as_cooked`` is not — the container
        comes in as ``args["as_cooked"]`` so a pinned-task token would sail
        straight through and read another project's cooked graph), so the
        project fence below runs unconditionally for a non-elevated session
        scope, regardless of whether ``task_id`` is pinned.  When several
        ``formula_snapshot`` rows exist (the container was cooked more than
        once), the row's ``content`` carries a ``cooked_at`` timestamp
        (``write_plan``, controller ruling P3-4 — ``task_context`` has no
        timestamp column and its ``id`` is random hex, so row order alone is
        not a reliable "latest" signal); the newest row wins, ties broken by
        ``chain_sha`` then ``id``.
        """
        container = await self.db.get_task(container_id)

        scope = self._current_scope or {}
        if scope.get("kind") == "session" and not scope.get("elevated"):
            scope_project_id = scope.get("project_id")
            if scope_project_id is not None and (
                container is None or container.project_id != scope_project_id
            ):
                return {
                    "success": False,
                    "result": "out_of_scope",
                    "error": (
                        f"container '{container_id}' is outside this session's "
                        f"scope ('{scope_project_id}')"
                    ),
                }

        contexts = await self.db.get_task_contexts(container_id)
        snapshots = [c for c in contexts if c["type"] == "formula_snapshot"]
        if not snapshots:
            return {"success": False, "error": f"no formula snapshot on {container_id}"}

        def _sort_key(row: dict) -> tuple:
            payload = json.loads(row["content"])
            return (payload.get("cooked_at", 0.0), payload.get("chain_sha", ""), row["id"])

        snapshot_row = max(snapshots, key=_sort_key)
        payload = json.loads(snapshot_row["content"])
        # Rows written before the {cooked_at, chain_sha, document} envelope
        # hold the bare document; render those as-is rather than raising.
        snapshot = payload.get("document", payload) if isinstance(payload, dict) else payload

        name = await self.db.get_task_meta(container_id, "formula")
        scope = await self.db.get_task_meta(container_id, "formula_scope")
        path = await self.db.get_task_meta(container_id, "formula_path")
        chain_sha = await self.db.get_task_meta(container_id, "formula_chain_sha")
        raw_vars = await self.db.get_task_meta(container_id, "formula_vars")
        effective = json.loads(raw_vars) if raw_vars else {}

        return {
            "success": True,
            "as_cooked": container_id,
            "name": name,
            "scope": scope,
            "path": path,
            "chain": None,
            "chain_sha": chain_sha,
            "vars": {"declared": None, "effective": effective},
            "graph": snapshot,
            "errors": [],
            "warnings": [],
        }

    # -- formula_cook --------------------------------------------------

    async def _cmd_formula_cook(self, args: dict) -> dict:
        scope = self._current_scope or {}
        if scope.get("kind") == "session" and not scope.get("elevated"):
            return {"success": False, "error": "formula_cook is not available to agent sessions"}

        name = args.get("name")
        if not name:
            return {"success": False, "error": "name is required"}
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"success": False, "error": "project_id is required (no active project set)"}
        project = await self.db.get_project(project_id)
        if not project:
            return {"success": False, "error": f"Project '{project_id}' not found"}
        supplied, vars_error = self._formula_vars_arg(args)
        if vars_error is not None:
            return vars_error
        parent_id = args.get("parent_id")
        dry_run = bool(args.get("dry_run", False))

        try:
            resolved, graph, errors, warnings = await self._resolve_formula_graph(
                name, project_id, supplied
            )
        except FormulaError as exc:
            return {"success": False, "error": str(exc)}

        if errors:
            return {
                "success": False,
                "error": (
                    f"graph validation failed with {len(errors)} error(s) — nothing was created"
                ),
                "errors": [e.to_dict() for e in errors],
                "warnings": [w.to_dict() for w in warnings],
            }

        parent_error, _parent = await self._validate_graph_parent(project_id, parent_id)
        if parent_error is not None:
            return {**parent_error, "success": False}

        provenance = FormulaProvenance(
            name=resolved.leaf.name,
            scope=resolved.leaf.scope,
            path=resolved.leaf.rel_path,
            vars=resolved.vars,
            chain_sha=resolved.chain_sha,
            snapshot=graph.to_dict(),
        )

        report = await create_graph(
            self,
            graph,
            project_id=project_id,
            dry_run=dry_run,
            parent_id=parent_id,
            provenance=provenance,
        )

        if not dry_run:
            container = await self.db.get_task(report["parent_id"])
            if container is not None:
                await self._emit_task_graph_change("task.updated", container)
            event = {
                "container_id": report["parent_id"],
                "project_id": project_id,
                "formula": resolved.leaf.name,
                "scope": resolved.leaf.scope,
                "chain_sha": resolved.chain_sha,
                "node_count": len(graph.nodes),
            }
            if parent_id:
                event["parent_id"] = parent_id
            await self.orchestrator.bus.emit("formula.cooked", event)

        report["success"] = True
        report["container_id"] = report["parent_id"]
        report["project_id"] = project_id
        report["warnings"] = [w.to_dict() for w in warnings]
        return report
