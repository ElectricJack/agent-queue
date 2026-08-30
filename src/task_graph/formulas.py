"""Formulas — reusable task-graph templates in the vault (swarm-work-model §13).

A formula is a markdown file with YAML frontmatter (``name``, ``description``,
``vars`` declarations, optional single ``extends``) and exactly one fenced
``aq-graph`` block.  Files live at ``vault/formulas/<name>.md`` (system) or
``vault/projects/<pid>/formulas/<name>.md`` (project shadows system by name).

The registry is in-memory and vault-watched, mirroring
:class:`src.sessions.harness_registry.HarnessRegistry`: a file that fails to
parse is logged, remembered in ``registry.errors`` for ``aq doctor``, and —
on a watcher re-parse — leaves the previous good entry in place so a
half-saved edit never takes a formula offline.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from src.profiles.parser import parse_frontmatter
from src.task_graph.models import GraphError, GraphParseError
from src.task_graph.parser import GRAPH_FENCE_LANG, extract_graph_block, parse_graph

if TYPE_CHECKING:  # pragma: no cover
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

__all__ = [
    "FORMULA_PATTERNS",
    "Formula",
    "FormulaError",
    "FormulaRegistry",
    "ResolvedFormula",
    "VarDecl",
    "apply_defaults",
    "chain_sha",
    "derive_formula_id",
    "load_from_vault",
    "merge_documents",
    "merged_var_decls",
    "parse_formula",
    "register_formula_handlers",
    "resolve_chain",
    "resolve_formula",
    "validate_vars",
]

#: Glob patterns handed to the vault watcher.
FORMULA_PATTERNS: list[str] = ["formulas/*.md", "projects/*/formulas/*.md"]

_FENCE_RE = re.compile(
    r"^(`{3,}|~{3,})[ \t]*" + GRAPH_FENCE_LANG + r"[ \t]*\r?$",
    re.MULTILINE,
)

OnReloadHook = Callable[[list[tuple[str | None, str]]], Awaitable[None]]


@dataclass(frozen=True)
class VarDecl:
    name: str
    required: bool = False
    default: str | None = None
    enum: tuple[str, ...] | None = None


@dataclass
class Formula:
    name: str
    description: str
    scope: str  # "system" | "project:<pid>"
    project_id: str | None
    rel_path: str  # vault-relative, e.g. "formulas/base-review.md"
    vars: dict[str, VarDecl]
    extends: str | None
    graph_block: str  # raw text of the aq-graph block
    #: The RAW authored mapping (``yaml.safe_load``/``json.loads`` of
    #: ``graph_block``, mirroring :func:`~src.task_graph.parser.parse_graph`'s
    #: auto format detection) — only the keys the author actually wrote, no
    #: ``TaskGraph.to_dict()`` defaults baked in.  ``parse_graph(graph_block)``
    #: is still run at parse time to validate structure; its ``TaskGraph`` is
    #: not stored, only used for validation, so chain merging (see
    #: ``merge_documents``) can tell "the author set this" from "this field
    #: defaulted" — a distinction ``to_dict()`` erases.
    graph_doc: dict
    content_sha: str  # sha256 of the whole file text


class FormulaError(Exception):
    def __init__(self, errors: list[GraphError]):
        self.errors = errors
        super().__init__("; ".join(f"{e.rule}: {e.detail}" for e in errors) or "invalid formula")


def derive_formula_id(rel_path: str) -> tuple[str | None, str] | None:
    """``(project_id, name)`` from a vault-relative path, or None."""
    parts = rel_path.replace("\\", "/").split("/")
    if (
        len(parts) == 4
        and parts[0] == "projects"
        and parts[2] == "formulas"
        and parts[-1].endswith(".md")
    ):
        return (parts[1], parts[-1][:-3])
    if len(parts) == 2 and parts[0] == "formulas" and parts[-1].endswith(".md"):
        return (None, parts[-1][:-3])
    return None


def _load_raw_document(block: str) -> dict | None:
    """Best-effort raw parse of an ``aq-graph`` *block* to a plain dict.

    Mirrors :func:`~src.task_graph.parser.parse_graph`'s ``fmt="auto"``
    detection (try JSON, then YAML) so ``Formula.graph_doc`` holds exactly
    the keys the author wrote — no ``TaskGraph.to_dict()`` defaults.
    """
    for loader in (json.loads, yaml.safe_load):
        try:
            data = loader(block)
        except (json.JSONDecodeError, yaml.YAMLError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _parse_var_decls(raw, errors: list[GraphError]) -> dict[str, VarDecl]:
    out: dict[str, VarDecl] = {}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        errors.append(GraphError(rule="formula.var_decl", detail="vars must be a mapping"))
        return out
    for name, spec in raw.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            errors.append(
                GraphError(rule="formula.var_decl", detail=f"{name}: declaration must be a mapping")
            )
            continue
        required = bool(spec.get("required", False))
        default = spec.get("default")
        default = None if default is None else str(default)
        enum = spec.get("enum")
        if enum is not None:
            if not isinstance(enum, list) or not all(
                isinstance(v, (str, int, float, bool)) for v in enum
            ):
                errors.append(
                    GraphError(
                        rule="formula.var_decl", detail=f"{name}: enum must be a list of scalars"
                    )
                )
                continue
            enum = tuple(str(v) for v in enum)
        if required and default is not None:
            errors.append(
                GraphError(
                    rule="formula.var_decl",
                    detail=f"{name}: required vars cannot have a default",
                )
            )
            continue
        if enum is not None and default is not None and default not in enum:
            errors.append(
                GraphError(
                    rule="formula.var_decl", detail=f"{name}: default {default!r} not in enum"
                )
            )
            continue
        out[str(name)] = VarDecl(name=str(name), required=required, default=default, enum=enum)
    return out


def parse_formula(text: str, *, rel_path: str) -> Formula:
    """Parse formula markdown *text*.  Raises :class:`FormulaError`."""
    errors: list[GraphError] = []
    ident = derive_formula_id(rel_path)
    stem = ident[1] if ident else os.path.splitext(os.path.basename(rel_path))[0]
    project_id = ident[0] if ident else None

    fm, body = parse_frontmatter(text)
    if body == text or not fm.name:
        raise FormulaError(
            [GraphError(rule="formula.frontmatter", detail="frontmatter with a `name` is required")]
        )

    if fm.name != stem:
        errors.append(
            GraphError(
                rule="formula.name_mismatch",
                detail=f"name {fm.name!r} does not match file stem {stem!r}",
            )
        )

    extends = fm.extra.get("extends")
    extends = str(extends) if extends else None
    if extends == fm.name:
        errors.append(
            GraphError(rule="formula.extends_self", detail="a formula cannot extend itself")
        )

    var_decls = _parse_var_decls(fm.extra.get("vars"), errors)

    fence_count = len(_FENCE_RE.findall(body))
    block = extract_graph_block(body)
    if block is None or fence_count == 0:
        errors.append(
            GraphError(rule="formula.no_graph", detail="exactly one aq-graph block is required")
        )
        block = ""
    elif fence_count > 1:
        errors.append(
            GraphError(rule="formula.multiple_graphs", detail=f"{fence_count} aq-graph blocks")
        )

    graph_doc: dict = {}
    if block:
        try:
            parse_graph(block)  # validate structure only; TaskGraph itself is discarded
        except GraphParseError as exc:
            errors.append(
                GraphError(
                    rule="formula.graph_parse", detail="; ".join(e.detail for e in exc.errors)
                )
            )
        else:
            raw_doc = _load_raw_document(block)
            if isinstance(raw_doc, dict):
                if "vars" in raw_doc:
                    errors.append(
                        GraphError(
                            rule="formula.vars_in_body",
                            detail="declare vars in frontmatter, not in the aq-graph block",
                        )
                    )
                graph_doc = raw_doc

    if errors:
        raise FormulaError(errors)

    return Formula(
        name=fm.name,
        description=str(fm.extra.get("description") or ""),
        scope=f"project:{project_id}" if project_id else "system",
        project_id=project_id,
        rel_path=rel_path.replace(os.sep, "/"),
        vars=var_decls,
        extends=extends,
        graph_block=block,
        graph_doc=graph_doc,
        content_sha=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


class FormulaRegistry:
    """Project-first, system-fallback lookup of :class:`Formula` entries."""

    def __init__(self) -> None:
        self._formulas: dict[tuple[str | None, str], Formula] = {}
        self.errors: dict[str, str] = {}

    # -- mutation ----------------------------------------------------------

    def upsert(self, formula: Formula) -> None:
        self._formulas[(formula.project_id, formula.name)] = formula

    def remove(self, name: str, project_id: str | None = None) -> bool:
        return self._formulas.pop((project_id, name), None) is not None

    def clear(self) -> None:
        self._formulas.clear()
        self.errors.clear()

    # -- lookup --------------------------------------------------------

    def get(self, name: str, project_id: str | None = None) -> Formula | None:
        if project_id is not None:
            formula = self._formulas.get((project_id, name))
            if formula is not None:
                return formula
        return self._formulas.get((None, name))

    def list_for_scope(self, project_id: str | None = None) -> list[Formula]:
        if project_id is None:
            return sorted(
                (f for (pid, _), f in self._formulas.items() if pid is None),
                key=lambda f: f.name,
            )
        project_names = {n for (pid, n) in self._formulas if pid == project_id}
        out: list[Formula] = []
        for (pid, name), formula in self._formulas.items():
            if pid == project_id or (pid is None and name not in project_names):
                out.append(formula)
        return sorted(out, key=lambda f: f.name)

    def list_all(self) -> list[Formula]:
        return sorted(self._formulas.values(), key=lambda f: ((f.project_id or ""), f.name))

    def __len__(self) -> int:
        return len(self._formulas)

    def __contains__(self, key: tuple[str | None, str]) -> bool:
        return key in self._formulas


# ---------------------------------------------------------------------------
# Vault scan + watcher integration
# ---------------------------------------------------------------------------


def _iter_formula_files(vault_root: str):
    """Yield ``(abs_path, rel_path)`` for every formula file in the vault."""
    if not os.path.isdir(vault_root):
        return

    sys_dir = os.path.join(vault_root, "formulas")
    if os.path.isdir(sys_dir):
        for fname in sorted(os.listdir(sys_dir)):
            if fname.startswith(".") or not fname.endswith(".md"):
                continue
            abs_path = os.path.join(sys_dir, fname)
            if os.path.isfile(abs_path):
                yield abs_path, f"formulas/{fname}"

    projects_dir = os.path.join(vault_root, "projects")
    if not os.path.isdir(projects_dir):
        return
    for project_name in sorted(os.listdir(projects_dir)):
        if project_name.startswith("."):
            continue
        pdir = os.path.join(projects_dir, project_name, "formulas")
        if not os.path.isdir(pdir):
            continue
        for fname in sorted(os.listdir(pdir)):
            if fname.startswith(".") or not fname.endswith(".md"):
                continue
            abs_path = os.path.join(pdir, fname)
            if os.path.isfile(abs_path):
                yield abs_path, f"projects/{project_name}/formulas/{fname}"


def load_from_vault(registry: FormulaRegistry, vault_root: str) -> list[str]:
    """Populate *registry* from every formula file under *vault_root*.

    Full reload: existing entries are dropped first.  Returns one error
    string per malformed file (the file is skipped, the load continues).
    """
    errors: list[str] = []
    registry.clear()

    for abs_path, rel_path in _iter_formula_files(vault_root):
        try:
            text = Path(abs_path).read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"{rel_path}: read failed: {exc}"
            errors.append(msg)
            registry.errors[rel_path] = msg
            continue
        try:
            formula = parse_formula(text, rel_path=rel_path)
        except FormulaError as exc:
            msg = f"{rel_path}: {exc}"
            errors.append(msg)
            registry.errors[rel_path] = msg
            logger.warning("Formula registry: skipping %s: %s", rel_path, exc)
            continue
        registry.upsert(formula)

    logger.info("Formula registry loaded: %d entries (%d errors)", len(registry), len(errors))
    return errors


async def _on_formula_changed(
    changes: list["VaultChange"],
    *,
    registry: FormulaRegistry,
) -> None:
    """Watcher callback — reparse changed files, update the registry."""
    for change in changes:
        derived = derive_formula_id(change.rel_path)
        if derived is None:
            continue
        project_id, name = derived

        if change.operation == "deleted":
            registry.remove(name, project_id)
            registry.errors.pop(change.rel_path, None)
            logger.info("Formula registry: removed %s (scope=%s)", name, project_id or "system")
            continue

        try:
            text = Path(change.path).read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"{change.rel_path}: read failed: {exc}"
            registry.errors[change.rel_path] = msg
            logger.error("Formula registry: cannot read %s", change.path, exc_info=True)
            continue

        try:
            formula = parse_formula(text, rel_path=change.rel_path)
        except FormulaError as exc:
            # Keep the previous entry: a file being edited must not take a
            # running formula offline halfway through a save.
            msg = f"{change.rel_path}: {exc}"
            registry.errors[change.rel_path] = msg
            logger.warning(
                "Formula registry: %s parse failed: %s — keeping previous entry",
                change.rel_path,
                exc,
            )
            continue

        registry.upsert(formula)
        registry.errors.pop(change.rel_path, None)
        logger.info(
            "Formula registry: %s %s (scope=%s)", change.operation, name, project_id or "system"
        )


def register_formula_handlers(
    watcher: "VaultWatcher",
    registry: FormulaRegistry,
) -> list[str]:
    """Register vault-watcher handlers for both formula scopes."""

    async def _handler(changes: list["VaultChange"]) -> None:
        await _on_formula_changed(changes, registry=registry)

    handler_ids: list[str] = []
    for pattern in FORMULA_PATTERNS:
        handler_ids.append(
            watcher.register_handler(pattern, _handler, handler_id=f"formula:{pattern}")
        )
    logger.info("Formula registry: registered %d handler(s)", len(handler_ids))
    return handler_ids


# ---------------------------------------------------------------------------
# Chain resolution, document merge, var validation, chain hash (part 2)
# ---------------------------------------------------------------------------


def resolve_chain(
    registry: FormulaRegistry, name: str, *, project_id: str | None
) -> list[Formula]:
    """Resolve the ``extends`` chain for *name*, root first.

    Every hop is looked up with the same *project_id* — project shadows
    system at every level of the chain, not just the leaf.

    Raises :class:`FormulaError` with ``formula.not_found`` (the leaf formula
    itself — the one the caller actually asked for — does not exist in this
    scope), ``formula.extends_missing`` (a later hop's named parent does not
    exist), or ``formula.extends_cycle`` (a formula extends one of its own
    ancestors).
    """
    chain: list[Formula] = []
    seen: list[str] = []
    current: str | None = name
    while current is not None:
        if current in seen:
            raise FormulaError(
                [
                    GraphError(
                        rule="formula.extends_cycle",
                        detail=" -> ".join(seen + [current]),
                    )
                ]
            )
        formula = registry.get(current, project_id)
        if formula is None:
            if not seen:
                raise FormulaError(
                    [
                        GraphError(
                            rule="formula.not_found",
                            detail=(
                                f"no formula named {name!r} in scope "
                                f"{project_id or 'system'}"
                            ),
                        )
                    ]
                )
            parent = seen[-1]
            raise FormulaError(
                [
                    GraphError(
                        rule="formula.extends_missing",
                        detail=f"{current!r} (required by {parent!r})",
                    )
                ]
            )
        seen.append(current)
        chain.append(formula)
        current = formula.extends
    chain.reverse()
    return chain


def _drop_null(mapping: dict) -> dict:
    """Drop keys whose authored value is ``None``.

    A bare YAML ``key:`` with nothing after it parses to ``None``, and raw
    YAML/JSON gives no other way to write "unset this key" that differs from
    "omit this key" — so a null is treated the same as an omission and
    dropped before the merge update, rather than overwriting an inherited
    value with ``None``. Every *other* authored value — including an
    explicit ``[]`` or ``""`` — is a genuine override: a child that writes
    ``labels: []`` really does clear the labels it inherited.
    """
    return {k: v for k, v in mapping.items() if v is not None}


def merge_documents(chain: list[Formula]) -> dict:
    """Merge a resolved ``extends`` chain (root first) into one graph document.

    Each ``Formula.graph_doc`` is the RAW authored mapping (see
    :func:`parse_formula`) — only the keys the author actually wrote, with no
    ``TaskGraph.to_dict()`` defaults baked in — so a child overrides *exactly*
    the keys it authored; an omitted key is inherited from the parent
    unchanged.

    - ``defaults``: merged key-wise, child wins.
    - ``parent``: merged field-wise, child keys override, missing keys
      inherited from the parent formula(s).
    - ``nodes``: merged by ``key``. A child node replaces the fields it
      authors on the same-keyed parent node (field-wise, child wins);
      ``needs`` (a list of strings and/or dicts, either shape), ``labels``,
      ``acceptance`` and ``context`` are REPLACED wholesale, never
      concatenated, when the child authors them. New keys are appended in
      the order the child introduces them.
    - ``spec``: child wins.

    Returns a new dict — never mutates any ``Formula.graph_doc``.
    """
    doc: dict = {"version": 1, "defaults": {}, "parent": {}, "nodes": []}
    index: dict[str, int] = {}
    for formula in chain:
        src = copy.deepcopy(formula.graph_doc)
        if src.get("spec"):
            doc["spec"] = src["spec"]
        doc["defaults"].update(_drop_null(src.get("defaults") or {}))
        doc["parent"].update(_drop_null(src.get("parent") or {}))
        for node in src.get("nodes") or []:
            key = node["key"]
            clean = _drop_null(node)
            if key in index:
                doc["nodes"][index[key]].update(clean)
            else:
                index[key] = len(doc["nodes"])
                doc["nodes"].append(clean)
    if not doc["parent"]:
        doc.pop("parent")
    return doc


def merged_var_decls(chain: list[Formula]) -> dict[str, VarDecl]:
    """Var declarations across the chain (root first) — child redeclares wins."""
    decls: dict[str, VarDecl] = {}
    for formula in chain:
        decls.update(formula.vars)
    return decls


def validate_vars(decls: dict[str, VarDecl], supplied: dict[str, str]) -> list[GraphError]:
    """Validate *supplied* values against declared vars.

    ``formula.var_required`` — declared required and absent from *supplied*.
    ``formula.var_enum`` — a supplied value is not one of the declared enum.
    ``formula.var_unknown`` — supplied but not declared anywhere in the chain.
    """
    errors: list[GraphError] = []
    for name, decl in decls.items():
        if decl.required and name not in supplied:
            errors.append(
                GraphError(rule="formula.var_required", detail=f"{name!r} is required")
            )
            continue
        if name in supplied and decl.enum is not None and str(supplied[name]) not in decl.enum:
            errors.append(
                GraphError(
                    rule="formula.var_enum",
                    detail=f"{name}={supplied[name]!r} not in {decl.enum}",
                )
            )
    for name in supplied:
        if name not in decls:
            errors.append(
                GraphError(rule="formula.var_unknown", detail=f"{name!r} is not declared")
            )
    return errors


def apply_defaults(decls: dict[str, VarDecl], supplied: dict[str, str]) -> dict[str, str]:
    """Effective values: *supplied* (``str()``-coerced) over declared defaults.

    Only declared names appear in the result.
    """
    out: dict[str, str] = {}
    for name, decl in decls.items():
        if name in supplied:
            out[name] = str(supplied[name])
        elif decl.default is not None:
            out[name] = str(decl.default)
    return out


def chain_sha(chain: list[Formula]) -> str:
    """sha256 over ``"\\n".join(content_sha for f in chain)``, root to leaf."""
    return hashlib.sha256(
        "\n".join(formula.content_sha for formula in chain).encode("utf-8")
    ).hexdigest()


@dataclass
class ResolvedFormula:
    leaf: Formula
    chain: list[Formula]
    vars: dict[str, str]  # effective values
    document: dict  # merged, with "vars" injected for substitute_vars
    chain_sha: str
    findings: list[GraphError]  # var validation findings (errors block cooking)


def resolve_formula(
    registry: FormulaRegistry,
    name: str,
    *,
    project_id: str | None,
    supplied_vars: dict[str, str],
) -> ResolvedFormula:
    """Resolve, merge and validate a formula end to end.

    Raises :class:`FormulaError` only for chain problems (missing parent,
    cycle) — var problems are returned as findings, never raised, so a
    caller can report them without losing the rest of the resolution.
    """
    chain = resolve_chain(registry, name, project_id=project_id)
    decls = merged_var_decls(chain)
    findings = validate_vars(decls, supplied_vars)
    effective = apply_defaults(decls, supplied_vars)
    document = merge_documents(chain)
    document["vars"] = dict(effective)
    return ResolvedFormula(
        leaf=chain[-1],
        chain=chain,
        vars=effective,
        document=document,
        chain_sha=chain_sha(chain),
        findings=findings,
    )
