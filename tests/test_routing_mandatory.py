"""Mandatory task routing ratchet (spec 2026-09-28-mandatory-task-routing §9.3).

Like ``tests/test_v1_removal.py``, this module holds the line the spec draws:

1. **No route writer outside the allowlist.**  An AST scan of ``src/`` finds
   every write of ``profile_id``, ``route_source`` or ``route`` into a task —
   ``Task(...)`` constructions, the task writers (``create_task``,
   ``update_task_routing``, ``update_task`` and the command handlers), dicts
   ``**``-splatted into them, filing commands dispatched through ``execute``,
   and SQLAlchemy ``update(tasks)`` / ``insert(tasks)`` ``.values(...)``.
   Every write must sit in an allowlisted function, and every allowlisted
   function must still write (a stale entry is a failure, so the list only
   shrinks).
2. **Every filing surface refuses every routing argument** for every
   external principal, with ``routing.choice_forbidden``, and writes nothing
   (spec §5.1, §14 criterion 1).
3. **The ``create_task``, ``ensure_task`` and ``edit_task`` contract
   fingerprints are unchanged**: their argument models keep the legacy
   fields, refused at runtime, so no reviewed bundle goes stale.
"""

from __future__ import annotations

import ast
import json
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

#: The task columns that make up a route (spec §4).
ROUTE_KEYS: frozenset[str] = frozenset({"profile_id", "route_source", "route"})

#: Calls whose keyword arguments (or splatted/positional dicts) become task
#: columns.  Matched on the called name's last component.
TASK_WRITE_CALLS: frozenset[str] = frozenset(
    {
        "Task",
        "create_task",
        "_create_task",
        "_cmd_create_task",
        "_cmd_ensure_task",
        "_cmd_edit_task",
        "update_task",
        "update_task_routing",
    }
)

#: Filing commands whose ``execute(name, {...})`` payload is scanned too.
EXECUTED_FILING_COMMANDS: frozenset[str] = frozenset(
    {"create_task", "ensure_task", "edit_task", "create_task_graph"}
)


@dataclass(frozen=True, order=True)
class RouteWrite:
    """One route key written by one function."""

    path: str
    scope: str
    key: str
    line: int

    @property
    def site(self) -> tuple[str, str]:
        return self.path, self.scope

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.scope} writes {self.key}"


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _task_table_names(tree: ast.Module) -> frozenset[str]:
    """The local names this module binds to ``src.database.tables.tasks``."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("database.tables"):
            for alias in node.names:
                if alias.name == "tasks":
                    names.add(alias.asname or "tasks")
    return frozenset(names)


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.tables = _task_table_names(tree)
        self.scopes: list[str] = []
        self.functions: list[ast.AST] = []
        self.found: list[RouteWrite] = []

    # -- scopes ----------------------------------------------------------

    def _visit_scope(self, node: ast.AST, is_function: bool) -> None:
        self.scopes.append(node.name)  # type: ignore[attr-defined]
        if is_function:
            self.functions.append(node)
        self.generic_visit(node)
        if is_function:
            self.functions.pop()
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, False)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, True)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, True)

    # -- writes ----------------------------------------------------------

    def _record(self, key: str, node: ast.AST) -> None:
        if key in ROUTE_KEYS:
            self.found.append(
                RouteWrite(self.path, ".".join(self.scopes) or "<module>", key, node.lineno)
            )

    def _dict_keys(self, expr: ast.expr, seen: frozenset[str] = frozenset()) -> Iterator[str]:
        """Keys *expr* carries when it is a dict (literal, ``dict()`` or a name).

        *seen* stops ``x = {**x, ...}`` from resolving ``x`` forever.
        """
        if isinstance(expr, ast.Dict):
            for key, value in zip(expr.keys, expr.values, strict=True):
                if key is None:  # ``{**other}``
                    yield from self._dict_keys(value, seen)
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    yield key.value
        elif isinstance(expr, ast.Call) and _call_name(expr.func) == "dict":
            for keyword in expr.keywords:
                if keyword.arg is not None:
                    yield keyword.arg
                else:
                    yield from self._dict_keys(keyword.value, seen)
        elif isinstance(expr, ast.Name) and self.functions and expr.id not in seen:
            yield from self._name_keys(expr.id, self.functions[-1], seen | {expr.id})

    def _name_keys(self, name: str, function: ast.AST, seen: frozenset[str]) -> Iterator[str]:
        """Keys the enclosing function writes into the dict bound to *name*."""
        for node in ast.walk(function):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == name and node.value:
                        if isinstance(node.value, (ast.Dict, ast.Call)):
                            yield from self._dict_keys(node.value, seen)
                    elif (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == name
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                    ):
                        yield target.slice.value
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == name
            ):
                if node.func.attr == "update":
                    for arg in node.args:
                        yield from self._dict_keys(arg, seen)
                    for keyword in node.keywords:
                        if keyword.arg is not None:
                            yield keyword.arg
                elif node.func.attr == "setdefault" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        yield first.value

    def _check_arguments(self, call: ast.Call) -> None:
        for keyword in call.keywords:
            if keyword.arg is not None:
                self._record(keyword.arg, keyword)
            else:
                for key in self._dict_keys(keyword.value):
                    self._record(key, keyword.value)
        for arg in call.args:
            if isinstance(arg, (ast.Dict, ast.Name)) or (
                isinstance(arg, ast.Call) and _call_name(arg.func) == "dict"
            ):
                for key in self._dict_keys(arg):
                    self._record(key, arg)

    def _is_tasks_table(self, expr: ast.expr) -> bool:
        return (isinstance(expr, ast.Name) and expr.id in self.tables) or (
            isinstance(expr, ast.Attribute) and expr.attr == "tasks"
        )

    def _on_tasks_table(self, receiver: ast.expr, seen: frozenset[str] = frozenset()) -> bool:
        """Whether *receiver* is an ``update``/``insert`` statement on tasks.

        A name (``stmt = update(tasks).where(...)``) is resolved through the
        enclosing function's assignments to it.
        """
        if isinstance(receiver, ast.Name):
            if receiver.id in seen or not self.functions:
                return False
            return any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == receiver.id
                    for target in node.targets
                )
                and self._on_tasks_table(node.value, seen | {receiver.id})
                for node in ast.walk(self.functions[-1])
            )
        while isinstance(receiver, ast.Call):
            func = receiver.func
            name = _call_name(func)
            if name in {"update", "insert"}:
                if receiver.args and self._is_tasks_table(receiver.args[0]):
                    return True
                if isinstance(func, ast.Attribute) and self._is_tasks_table(func.value):
                    return True
            if not isinstance(func, ast.Attribute):
                return False
            receiver = func.value
            if isinstance(receiver, ast.Name):
                return self._on_tasks_table(receiver, seen)
        return False

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name in TASK_WRITE_CALLS:
            self._check_arguments(node)
        elif (
            name == "execute"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in EXECUTED_FILING_COMMANDS
        ):
            for arg in node.args[1:]:
                for key in self._dict_keys(arg):
                    self._record(key, arg)
        elif (
            name == "values"
            and isinstance(node.func, ast.Attribute)
            and self._on_tasks_table(node.func.value)
        ):
            self._check_arguments(node)
        self.generic_visit(node)


def scan_source(source: str, path: str) -> list[RouteWrite]:
    """Every route write in one module's *source*."""
    tree = ast.parse(source)
    scanner = _Scanner(path, tree)
    scanner.visit(tree)
    return sorted(set(scanner.found))


def scan_tree(root: Path = SRC) -> list[RouteWrite]:
    """Every route write under ``src/``."""
    found: list[RouteWrite] = []
    for file in sorted(root.rglob("*.py")):
        rel = file.relative_to(REPO).as_posix()
        found.extend(scan_source(file.read_text(encoding="utf-8"), rel))
    return found



# ---------------------------------------------------------------------------
# 1. The ratchet: no route writer outside the allowlist
# ---------------------------------------------------------------------------

#: Every function allowed to write a route, and why.  Keyed by the
#: repo-relative path and the enclosing qualname.  Spec §9.3 names the
#: allowed writers: the router apply, failover, spill and undo, override,
#: the role creators, ``delete_profile`` and the migrations (which live
#: outside ``src/``).  Entries marked "Task 6" / "Task 7" are writers the
#: spec's later tasks rework; the stale-entry check makes them leave this
#: list when they stop writing.
ROUTE_WRITERS: dict[tuple[str, str], str] = {
    # -- the router ---------------------------------------------------------
    ("src/database/queries/routing_queries.py", "RoutingQueryMixin.write_router_route"): (
        "task_route_apply: the bound router's single route write (spec §6.6)"
    ),
    # -- the audited emergency override (spec §7, D2) -------------------------
    ("src/database/queries/routing_queries.py", "RoutingQueryMixin.write_override_route"): (
        "task_route_override: the local operator's or supervisor's pinned override"
    ),
    # -- failover, spill and reroute-undo -------------------------------------
    ("src/database/queries/task_reroute_queries.py", "TaskRerouteQueryMixin.apply_task_reroute"): (
        "provider failover, capacity spill and preferred-work moves (spec §6.8, Task 6)"
    ),
    ("src/database/queries/task_reroute_queries.py", "TaskRerouteQueryMixin.undo_task_reroute"): (
        "reroute undo (spec §6.8, Task 6)"
    ),
    # -- role creators (spec §4, D3) -----------------------------------------
    ("src/commands/task_commands.py", "TaskCommandsMixin._create_task"): (
        "writes route_source/route for every task; a profile only for a role creator's "
        "role profile (_filing_refusal refuses every other)"
    ),
    ("src/database/queries/triage_queries.py", "ensure_triage_task"): (
        "the canonical triage task: role 'triage'"
    ),
    ("src/orchestrator/triage.py", "TriageMixin._reconcile_triage_tasks"): (
        "files the triage role task through ensure_task"
    ),
    # -- route resets and profile deletion -----------------------------------
    ("src/database/queries/profile_queries.py", "ProfileQueryMixin.delete_profile"): (
        "nulls the profile a deleted profile leaves behind (spec §9.2)"
    ),
    ("src/database/queries/task_queries.py", "TaskQueryMixin.reset_task_route"): (
        "sends a queued task back to its router: clears the route (aq task route, "
        "edit_task hint edits)"
    ),
    # -- the persistence layer -------------------------------------------------
    ("src/database/queries/task_queries.py", "TaskQueryMixin._insert_task_row"): (
        "db.create_task persists the Task it is given; every Task(...) is scanned"
    ),
    ("src/database/queries/task_queries.py", "TaskQueryMixin.update_task"): (
        "stores the source a profile write declares, and refuses none (spec §9.2); "
        "update_task(profile_id=...) callers are scanned"
    ),
    ("src/database/queries/task_queries.py", "TaskQueryMixin.update_task_routing"): (
        "the guarded routing write; its callers are scanned"
    ),
    ("src/database/queries/task_queries.py", "TaskQueryMixin._row_to_task"): (
        "decodes a stored row into a Task: a read"
    ),
    # -- in-memory probes, never persisted -----------------------------------
    ("src/commands/claim_commands.py", "ClaimCommandsMixin._pool_claim_routing.matches"): (
        "a throwaway Task to test a pool worker against a class requirement"
    ),
    ("src/commands/profile_commands.py", "ProfileCommandsMixin._cmd_show_effective_profile"): (
        "a throwaway Task for the profile resolver"
    ),
    ("src/orchestrator/pools.py", "PoolsMixin._launch_pool_session_inner"): (
        "a throwaway Task describing a pool worker's requirement"
    ),
}


def test_no_route_writer_outside_the_allowlist() -> None:
    """Spec §9.3: every route write sits in an allowlisted function."""
    stray = [str(write) for write in scan_tree() if write.site not in ROUTE_WRITERS]
    assert not stray, (
        "a task route may only be written by the router, failover/spill/undo, an override, "
        "a role creator or delete_profile (mandatory-routing spec §2, §9.3). File with "
        "hints instead, or add the function to ROUTE_WRITERS with the reason:\n"
        + "\n".join(stray)
    )


def test_allowlist_names_only_live_writers() -> None:
    """A writer that stops writing leaves the allowlist, so it only shrinks."""
    live = {write.site for write in scan_tree()}
    stale = sorted(site for site in ROUTE_WRITERS if site not in live)
    assert not stale, f"ROUTE_WRITERS entries that no longer write a route: {stale}"


SEEDED_ROUTE_WRITES = {
    "Task(profile_id=...)": """
        def f():
            return Task(id='', project_id='p', profile_id='coder')
    """,
    "db.create_task(Task(route_source=...))": """
        async def f(db):
            await db.create_task(Task(id='', route_source='router'))
    """,
    "update_task(profile_id=...)": """
        async def f(db):
            await db.update_task('t', profile_id='coder')
    """,
    "update_task_routing(profile_id=...)": """
        async def f(db):
            await db.update_task_routing(
                't', profile_id='coder', intelligence_class=None, preferred_workspace_id=None
            )
    """,
    "update(tasks).values(profile_id=...)": """
        from sqlalchemy import update
        from src.database.tables import tasks

        async def f(conn):
            await conn.execute(update(tasks).where(tasks.c.id == 't').values(profile_id='x'))
    """,
    "insert(tasks).values({'route': ...})": """
        from sqlalchemy import insert
        from src.database.tables import tasks

        async def f(conn):
            await conn.execute(insert(tasks).values({'id': 't', 'route': {}}))
    """,
    "a statement name and a splatted dict": """
        from src.database.tables import tasks as tasks_table

        async def f(conn):
            stmt = tasks_table.update().where(tasks_table.c.id == 't')
            values = {'title': 'x'}
            values['route_source'] = 'router'
            await conn.execute(stmt.values(**values))
    """,
    "_cmd_create_task({'profile_id': ...})": """
        async def f(handler):
            await handler._cmd_create_task({'title': 't', 'profile_id': 'coder'})
    """,
    "a dict updated before the call": """
        async def f(handler):
            args = {'title': 't'}
            args.update(profile_id='coder')
            await handler._cmd_create_task(args)
    """,
    "execute('create_task', {'profile_id': ...})": """
        async def f(handler):
            await handler.execute('create_task', {'title': 't', 'profile_id': 'coder'})
    """,
    "a nested splat": """
        def f():
            return Task(id='', **{'title': 't', **dict(profile_id='x')})
    """,
}

NOT_ROUTE_WRITES = {
    "a class hint": """
        def f():
            return Task(id='', project_id='p', class_hint='deep-high')
    """,
    "another table": """
        from sqlalchemy import update
        from src.database.tables import task_proposals

        async def f(conn):
            await conn.execute(update(task_proposals).values(profile_id='x'))
    """,
    "a result dict": """
        def f(task):
            return {'profile_id': task.profile_id}
    """,
}


@pytest.mark.parametrize("label", sorted(SEEDED_ROUTE_WRITES))
def test_the_scan_finds_a_seeded_route_write(label: str) -> None:
    """The ratchet fails on a seeded write outside the allowlist."""
    found = scan_source(textwrap.dedent(SEEDED_ROUTE_WRITES[label]), "src/seeded.py")
    assert found, label
    assert all(write.site not in ROUTE_WRITERS for write in found)


@pytest.mark.parametrize("label", sorted(NOT_ROUTE_WRITES))
def test_the_scan_ignores_what_is_not_a_route_write(label: str) -> None:
    assert scan_source(textwrap.dedent(NOT_ROUTE_WRITES[label]), "src/seeded.py") == []


def test_a_seeded_write_in_the_real_tree_fails_the_ratchet(tmp_path) -> None:
    """End to end: a copy of ``src/`` plus one stray writer is caught."""
    import shutil

    tree = tmp_path / "src"
    shutil.copytree(SRC, tree, ignore=shutil.ignore_patterns("__pycache__"))
    (tree / "commands" / "stray_route.py").write_text(
        "from src.models import Task\n\n"
        "def stray():\n    return Task(id='', project_id='p', profile_id='coder')\n",
        encoding="utf-8",
    )
    found = []
    for file in sorted(tree.rglob("*.py")):
        rel = "src/" + file.relative_to(tree).as_posix()
        found.extend(scan_source(file.read_text(encoding="utf-8"), rel))
    stray = [write for write in found if write.site not in ROUTE_WRITERS]
    assert [(w.path, w.scope, w.key) for w in stray] == [
        ("src/commands/stray_route.py", "stray", "profile_id")
    ]


# ---------------------------------------------------------------------------
# 2. Every filing surface refuses every routing argument
# ---------------------------------------------------------------------------

#: A value for each refused argument (spec §5.1): what a caller would send.
REFUSED_VALUES: dict[str, object] = {
    "profile_id": "coder",
    "profile": "coder",
    "provider": "anthropic",
    "model": "claude-opus",
    "harness": "claude",
    "agent_type": "coder",
    "pin": True,
    "provider_intent": "pinned",
    "preferred_provider": "anthropic",
    "default_profile_id": "coder",
}

#: The external principals of spec §5.1.  ``None`` is the local operator: the
#: CLI, MCP and the HTTP API all reach ``execute`` with no bound principal.
PRINCIPALS = ("operator", "service", "playbook", "supervisor", "worker")


def _principal(name: str):
    from src.commands.authorization import required_capability
    from src.commands.principal import ExecutionPrincipal, PrincipalKind
    from src.profiles.capabilities import CapabilityPolicy
    from src.routing.filing import FILING_COMMANDS

    policy = CapabilityPolicy.from_namespaces(
        aq_commands=sorted(
            set(FILING_COMMANDS) | {required_capability(name) for name in FILING_COMMANDS}
        )
    )
    if name == "operator":
        return None
    if name == "service":
        return ExecutionPrincipal.service("ratchet")
    if name == "playbook":
        # A playbook runs under its own profile; ``pipeline`` holds no tools,
        # so a role child with none is no escalation.
        return ExecutionPrincipal(kind=PrincipalKind.PLAYBOOK, policy=policy, profile_id="pipeline")
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=policy,
        session_id=f"{name}-session",
        project_id="p",
        elevated=name == "supervisor",
    )


@pytest.fixture
async def env(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig
    from src.database import Database
    from src.models import AgentProfile, Project, Task, TaskStatus
    from src.orchestrator import Orchestrator
    from src.vault import ensure_default_intelligence_classes
    from tests.db_fixtures import lease_dsn

    db = Database(lease_dsn("routing-mandatory.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    for profile_id, harness in (("coder", "codex"), ("triage", "claude"), ("pipeline", "claude")):
        await db.create_profile(AgentProfile(
            id=profile_id, name=profile_id, harness=harness, default_class="standard-high",
            needs_workspace=False,
        ))
    await db.create_task(Task(
        id="queued", project_id="p", title="Queued", description="d",
        status=TaskStatus.READY, class_hint="standard-high",
    ))
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    config = AppConfig(data_dir=data_dir, database=DatabaseConfig(url=lease_dsn("routing-mandatory.db")))
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch._emit_notify = AsyncMock()
    handler = CommandHandler(orch, config)
    yield handler, db
    await db.close()


async def _row_counts(db) -> dict[str, int]:
    from sqlalchemy import func, select

    from src.database.tables import task_metadata, task_proposals, tasks

    counts = {}
    async with db._engine.connect() as conn:
        for table in (tasks, task_proposals, task_metadata):
            counts[table.name] = (
                await conn.execute(select(func.count()).select_from(table))
            ).scalar_one()
        counts["routed"] = (
            await conn.execute(
                select(func.count()).select_from(tasks).where(tasks.c.profile_id.is_not(None))
            )
        ).scalar_one()
    return counts


def _surface_args(surface: str) -> dict:
    """Arguments that file successfully on *surface*, bar the routing choice."""
    spec = {"tempId": "a", "title": "A", "description": "d"}
    return {
        "create_task": {"project_id": "p", "title": "T", "description": "d"},
        "ensure_task": {"project_id": "p", "dedup_key": "k", "title": "T"},
        "create_task_graph": {
            "project_id": "p", "graph": {"nodes": [{"key": "a", "title": "A"}]},
        },
        "formula_cook": {"project_id": "p", "name": "none"},
        "edit_task": {"task_id": "queued", "title": "Renamed"},
        "task_batch_propose": {"project_id": "p", "source": "s", "tasks": [spec], "edges": []},
        "task_batch_update": {"proposal_id": "none", "payload": {"tasks": [spec], "edges": []}},
        "task_batch_commit": {"proposal_id": "none"},
        "task_route": {"task_id": "queued"},
    }[surface]


async def _execute_as(handler, principal_name: str, command: str, args: dict) -> dict:
    from src.commands.principal import principal_context

    principal = _principal(principal_name)
    if principal is None:
        return await handler.execute(command, dict(args))
    with principal_context(principal):
        return await handler.execute(command, dict(args))


def _assert_refused(result: dict, refused: str) -> None:
    from src.routing.filing import ROUTING_CHOICE_FORBIDDEN

    assert result.get("success") is False, result
    assert result.get("code") == ROUTING_CHOICE_FORBIDDEN, result
    assert refused in result["refused"], result
    assert "intelligence_class" in result["error"], result
    # The one lever that names a profile (spec §5.1, §7).
    assert "aq task route-override" in result["error"], result


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize(
    "surface",
    [
        "create_task",
        "ensure_task",
        "create_task_graph",
        "formula_cook",
        "edit_task",
        "task_batch_propose",
        "task_batch_update",
        "task_batch_commit",
        "task_route",
    ],
)
async def test_every_surface_refuses_every_routing_argument(env, surface, principal) -> None:
    """Spec §14 criterion 1: refused, with routing.choice_forbidden, nothing written."""
    handler, db = env
    before = await _row_counts(db)
    for argument, value in REFUSED_VALUES.items():
        result = await _execute_as(
            handler, principal, surface, {**_surface_args(surface), argument: value}
        )
        _assert_refused(result, argument)
    assert await _row_counts(db) == before


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize("surface", ["task_batch_propose", "task_batch_update"])
async def test_batch_specs_refuse_every_routing_argument(env, surface, principal) -> None:
    handler, db = env
    before = await _row_counts(db)
    for argument, value in REFUSED_VALUES.items():
        args = _surface_args(surface)
        specs = [{**spec, argument: value} for spec in (
            args["tasks"] if surface == "task_batch_propose" else args["payload"]["tasks"]
        )]
        if surface == "task_batch_propose":
            args = {**args, "tasks": specs}
        else:
            args = {**args, "payload": {**args["payload"], "tasks": specs}}
        result = await _execute_as(handler, principal, surface, args)
        _assert_refused(result, f"tasks[0].{argument}")
    assert await _row_counts(db) == before


GRAPH_ROUTE_DOCUMENTS = {
    "node profile": {"nodes": [{"key": "a", "title": "A", "profile": "coder"}]},
    "node pin": {"nodes": [{"key": "a", "title": "A", "pin": True}]},
    "node provider": {"nodes": [{"key": "a", "title": "A", "provider": "anthropic"}]},
    "node model": {"nodes": [{"key": "a", "title": "A", "model": "claude-opus"}]},
    "defaults profile": {"defaults": {"profile": "coder"}, "nodes": [{"key": "a", "title": "A"}]},
    "defaults pin": {"defaults": {"pin": True}, "nodes": [{"key": "a", "title": "A"}]},
    "parent profile": {
        "parent": {"title": "Epic", "profile": "coder"}, "nodes": [{"key": "a", "title": "A"}],
    },
}


@pytest.mark.parametrize("principal", PRINCIPALS)
async def test_a_graph_document_cannot_name_a_route(env, principal) -> None:
    """Spec §5.1: node profile/pin, defaults.profile and parent.profile."""
    from src.routing.filing import ROUTING_CHOICE_FORBIDDEN

    handler, db = env
    before = await _row_counts(db)
    for label, graph in GRAPH_ROUTE_DOCUMENTS.items():
        result = await _execute_as(
            handler, principal, "create_task_graph", {"project_id": "p", "graph": graph}
        )
        assert result.get("code") == ROUTING_CHOICE_FORBIDDEN, (label, result)
        assert result["refused"], (label, result)
        assert any(error["rule"] == "routing_choice_forbidden" for error in result["errors"])
    assert await _row_counts(db) == before


def test_a_formula_document_cannot_name_a_route() -> None:
    """formula_cook answers a parsed formula's route keys with the same refusal."""
    from src.routing.filing import ROUTING_CHOICE_FORBIDDEN, graph_route_refusal
    from src.task_graph import GraphParseError, parse_graph

    with pytest.raises(GraphParseError) as caught:
        parse_graph({"nodes": [{"key": "fix", "title": "Fix", "profile": "{fixer}"}]})
    refusal = graph_route_refusal("formula_cook", caught.value.errors)
    assert refusal is not None
    assert refusal["code"] == ROUTING_CHOICE_FORBIDDEN
    assert refusal["refused"] == ["fix.profile"]


@pytest.mark.parametrize("principal", ["service", "playbook"])
@pytest.mark.parametrize("surface", ["create_task", "ensure_task"])
async def test_a_role_creator_may_name_a_role_profile(env, surface, principal) -> None:
    """The one exception (spec §4, D3): a role profile from SERVICE or PLAYBOOK."""
    handler, db = env
    result = await _execute_as(
        handler, principal, surface, {**_surface_args(surface), "profile_id": "triage"}
    )
    assert result.get("success") is not False and "error" not in result, result
    task_id = result.get("task_id") or result.get("created")
    task = await db.get_task(task_id)
    assert (task.profile_id, task.route_source) == ("triage", "role")


@pytest.mark.parametrize("principal", ["operator", "supervisor", "worker"])
@pytest.mark.parametrize("surface", ["create_task", "ensure_task"])
async def test_only_a_role_creator_may_name_a_role_profile(env, surface, principal) -> None:
    handler, db = env
    before = await _row_counts(db)
    result = await _execute_as(
        handler, principal, surface, {**_surface_args(surface), "profile_id": "triage"}
    )
    _assert_refused(result, "profile_id")
    assert await _row_counts(db) == before


@pytest.mark.parametrize("principal", ["service", "playbook"])
async def test_a_role_creator_may_not_name_a_worker_profile(env, principal) -> None:
    handler, db = env
    before = await _row_counts(db)
    result = await _execute_as(
        handler, principal, "create_task", {**_surface_args("create_task"), "profile_id": "coder"}
    )
    _assert_refused(result, "profile_id")
    assert await _row_counts(db) == before


async def test_an_in_process_caller_is_refused_by_the_handler(env) -> None:
    """A caller that skips ``execute`` meets the same refusal in the handler."""
    handler, db = env
    before = await _row_counts(db)
    for command, args in (
        ("create_task", {**_surface_args("create_task"), "pin": True}),
        ("ensure_task", {**_surface_args("ensure_task"), "provider": "anthropic"}),
        ("edit_task", {**_surface_args("edit_task"), "profile_id": "coder"}),
        ("create_task_graph", {**_surface_args("create_task_graph"), "profile_id": "coder"}),
        ("task_batch_propose", {
            **_surface_args("task_batch_propose"),
            "tasks": [{"tempId": "a", "title": "A", "description": "d", "profile_id": "coder"}],
        }),
    ):
        result = await getattr(handler, f"_cmd_{command}")(args)
        from src.routing.filing import ROUTING_CHOICE_FORBIDDEN

        assert result.get("code") == ROUTING_CHOICE_FORBIDDEN, (command, result)
    assert await _row_counts(db) == before


async def test_inert_routing_values_choose_nothing(env) -> None:
    """``null``, ``false`` and ``""`` are no choice: the call files unrouted."""
    handler, db = env
    result = await handler.execute("create_task", {
        **_surface_args("create_task"),
        "profile_id": None, "pin": False, "provider_intent": "", "intelligence_class": "deep-high",
    })
    assert "error" not in result, result
    task = await db.get_task(result["created"])
    assert (task.profile_id, task.route_source, task.class_hint) == (None, "unrouted", "deep-high")


async def test_a_stored_proposal_naming_a_route_is_refused_at_commit(env) -> None:
    """A proposal written before the refusal cannot commit its route."""
    from src.database.queries import proposal_queries

    handler, db = env
    proposal_id = await proposal_queries.insert_proposal(
        db, project_id="p", source="legacy", payload={
            "tasks": [{"tempId": "a", "title": "A", "description": "d", "profile_id": "coder"}],
            "edges": [],
        },
    )
    await proposal_queries.update_proposal(db, proposal_id, status="ready")
    handler._proposal_approval_error = _no_approval_error
    before = await _row_counts(db)
    result = await handler._cmd_task_batch_commit({"proposal_id": proposal_id})
    _assert_refused(result, "tasks[0].profile_id")
    assert await _row_counts(db) == before
    assert (await proposal_queries.get_proposal(db, proposal_id))["status"] == "ready"


async def _no_approval_error(*_args, **_kwargs):
    return None


# -- edit_task keeps the hints and resets the route ---------------------------


async def test_a_hint_edit_sends_a_queued_task_back_to_its_router(env) -> None:
    from src.models import Task, TaskStatus

    handler, db = env
    await db.create_task(Task(
        id="legacy", project_id="p", title="Legacy", description="d",
        status=TaskStatus.READY, profile_id="coder",
        route_source="legacy", intelligence_class="standard-high",
    ))
    assert (await db.get_task("legacy")).route_source == "legacy"
    result = await handler.execute("edit_task", {"task_id": "legacy", "intelligence_class": "deep-high"})
    assert result.get("fields") == ["intelligence_class"], result
    task = await db.get_task("legacy")
    assert (task.profile_id, task.intelligence_class, task.route_source, task.class_hint) == (
        None, None, "unrouted", "deep-high",
    )
    assert task.provider_intent == "class_only"

    result = await handler.execute("edit_task", {"task_id": "legacy", "task_type": "design"})
    assert "error" not in result, result
    assert (await db.get_task("legacy")).task_type.value == "design"


async def test_a_hint_edit_keeps_a_role_route(env) -> None:
    from src.models import Task, TaskStatus

    handler, db = env
    await db.create_task(Task(
        id="role", project_id="p", title="Triage", description="d",
        status=TaskStatus.READY, profile_id="triage",
        route_source="role", intelligence_class="standard-high",
    ))
    result = await handler.execute("edit_task", {"task_id": "role", "intelligence_class": "deep-high"})
    assert "error" not in result, result
    task = await db.get_task("role")
    assert (task.profile_id, task.route_source, task.class_hint) == ("triage", "role", "deep-high")


async def test_a_claimed_task_keeps_its_route(env) -> None:
    from src.models import Agent, Task, TaskStatus

    handler, db = env
    await db.create_agent(Agent(id="agent", name="Worker", profile_id="coder"))
    await db.create_task(Task(
        id="held", project_id="p", title="Held", description="d",
        status=TaskStatus.ASSIGNED, profile_id="coder",
        route_source="legacy", intelligence_class="standard-high",
        assigned_agent_id="agent",
    ))
    refused = await handler.execute("edit_task", {"task_id": "held", "intelligence_class": "deep-high"})
    assert "claimed" in refused["error"]
    kind = await handler.execute("edit_task", {"task_id": "held", "task_type": "bugfix"})
    assert "error" not in kind, kind
    task = await db.get_task("held")
    assert (task.profile_id, task.route_source) == ("coder", "legacy")


# -- the transports -----------------------------------------------------------


async def test_the_typed_api_route_refuses_what_its_model_does_not_declare(
    env, monkeypatch
) -> None:
    """The HTTP request model drops undeclared keys; the route forwards them."""
    import httpx
    from fastapi import FastAPI

    from src.api import dependencies as deps
    from src.api.codegen import build_category_routers
    from src.routing.filing import ROUTING_CHOICE_FORBIDDEN

    handler, db = env
    monkeypatch.setattr(deps, "_command_handler", handler)
    monkeypatch.setattr(deps, "_orchestrator", handler.orchestrator)
    monkeypatch.setattr(deps, "_require_session_token", False)
    app = FastAPI()
    for router in build_category_routers():
        app.include_router(router)
    before = await _row_counts(db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path, body in (
            ("/api/task/create", {"project_id": "p", "title": "T", "profile_id": "coder"}),
            ("/api/task/create", {"project_id": "p", "title": "T", "model": "claude-opus"}),
            ("/api/task/edit", {"task_id": "queued", "pin": True}),
        ):
            response = await client.post(path, json=body)
            assert response.status_code == 422, (path, response.text)
            assert response.json()["code"] == ROUTING_CHOICE_FORBIDDEN, response.text
    assert await _row_counts(db) == before


async def test_the_mcp_tool_refuses_a_routing_argument(env) -> None:
    from unittest.mock import MagicMock

    from mcp.server import FastMCP

    from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS, register_command_tools
    from src.routing.filing import ROUTING_CHOICE_FORBIDDEN

    handler, db = env
    server = FastMCP(name="ratchet")
    register_command_tools(server, excluded=DEFAULT_EXCLUDED_COMMANDS)
    context = MagicMock()
    context.request_context.lifespan_context = {"command_handler": handler}
    server.get_context = lambda: context
    before = await _row_counts(db)
    result = await server.call_tool(
        "create_task", {"project_id": "p", "title": "T", "harness": "claude"}
    )
    text = json.dumps(result, default=str)
    assert ROUTING_CHOICE_FORBIDDEN in text
    assert await _row_counts(db) == before


async def test_a_playbook_step_through_the_contract_is_rejected(env) -> None:
    """A reviewed step's legacy field reaches the ``rejected`` outcome."""
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import CreateTaskArgs, EditTaskArgs, EnsureTaskArgs

    handler, db = env
    handler_factory = __import__("src.commands.contracts.builtin", fromlist=["_handler"])
    original = handler_factory._handler
    handler_factory._handler = lambda: handler
    try:
        before = await _row_counts(db)
        for name, args in (
            ("create_task", CreateTaskArgs(title="T", project_id="p", profile_id="coder")),
            ("create_task", CreateTaskArgs(title="T", project_id="p", pin=True)),
            ("ensure_task", EnsureTaskArgs(dedup_key="k", title="T", project_id="p",
                                           profile_id="coder")),
            ("edit_task", EditTaskArgs(task_id="queued", profile_id="coder")),
        ):
            result = await CONTRACTS.require(name).invoke(args, _principal("playbook"))
            assert result.outcome == "rejected", (name, result)
        assert await _row_counts(db) == before
    finally:
        handler_factory._handler = original


def test_the_llm_tool_loop_refuses_ahead_of_the_args_model() -> None:
    """An undeclared ``provider`` is a routing choice, not a validation error."""
    import inspect

    from src.playbooks.executors import llm

    source = inspect.getsource(llm)
    refusal = source.index("routing_choice_refusal(name, args, ctx.principal)")
    validation = source.index("args_model.model_validate(args)")
    assert refusal < validation


# ---------------------------------------------------------------------------
# 3. Contract fingerprints are unchanged
# ---------------------------------------------------------------------------

#: Recorded before the refusal landed (solid-dune-67.5).  The argument models
#: keep their legacy routing fields, refused at runtime, so these do not move
#: and no reviewed bundle that calls them goes stale (spec §5.1).
FINGERPRINTS = {
    "create_task": "sha256:6b42134bd02d6111e6dde186aa5ffebfb2aae8ba6842a9c4024a465086359ca2",
    "ensure_task": "sha256:6a4c41c5028e2864029871a47bef0e30bfbe3c074ce40aa6ae1b638e3a35b5f4",
    "edit_task": "sha256:a5c02bc88c11931d870330d03db0b1a7237e539a51a7d65a18149b3dc63b9fb6",
}


@pytest.mark.parametrize("command", sorted(FINGERPRINTS))
def test_filing_contract_fingerprints_are_unchanged(command: str) -> None:
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.models import execution_fingerprint

    contract = CONTRACTS.require(command).contract
    assert execution_fingerprint(contract.execution) == FINGERPRINTS[command]
    assert "rejected" in {spec.name for spec in contract.execution.outcomes}


@pytest.mark.parametrize("command", ["create_task", "ensure_task", "edit_task", "task_route"])
def test_the_tool_definitions_drop_the_routing_arguments(command: str) -> None:
    """The LLM-facing schema, the CLI and the typed route no longer offer them."""
    from src.routing.filing import REFUSED_ROUTING_ARGS
    from src.tools.definitions import _ALL_TOOL_DEFINITIONS

    schema = next(t["input_schema"] for t in _ALL_TOOL_DEFINITIONS if t["name"] == command)
    assert not set(REFUSED_ROUTING_ARGS) & set(schema["properties"])


def test_aq_task_create_has_no_routing_flags() -> None:
    from src.cli.tasks import task_create

    flags = {opt for param in task_create.params for opt in getattr(param, "opts", [])}
    assert not flags & {"-P", "--profile", "--pin", "--provider-intent", "--agent-type"}
    assert "--intelligence-class" in flags


if __name__ == "__main__":  # pragma: no cover - a developer's listing aid
    print(json.dumps([str(write) for write in scan_tree()], indent=1))
