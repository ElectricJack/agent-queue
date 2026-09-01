"""Live API ↔ generated-client conformance guards (api-cli plan 1-3, API-2).

The dashboard talks to the daemon through the generated
``agent_queue_api_client`` package (``packages/aq-client/``), which is a
committed snapshot of the daemon's OpenAPI surface.  Nothing else verifies
that snapshot against the application ``create_app()`` actually serves, so a
router/category/schema change can strand dashboard callers silently.  These
tests are the executable conformance guard:

1. every operation served by a real ``create_app()`` app exists in the
   generated client, and vice versa (exact bidirectional match);
2. the generated request/response models round-trip a real typed route over
   ASGI, including the documented 422 error shape;
3. codegen routers cover every discovered non-excluded command exactly once,
   and no ``API_EXCLUDED`` command (``run_command`` above all) gets a route.

The generated client is read from the repo checkout (``packages/aq-client``),
not from whatever copy happens to be installed, so the guard always compares
the committed artifacts of *this* revision.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = REPO_ROOT / "packages" / "aq-client"
CLIENT_PKG_DIR = CLIENT_DIR / "agent_queue_api_client"


def _import_repo_client():
    """Import ``agent_queue_api_client`` from the repo checkout.

    An installed (possibly stale) copy may already be importable or even
    imported; drop it so the committed package is what gets exercised.
    """
    loaded = sys.modules.get("agent_queue_api_client")
    if loaded is not None and Path(loaded.__file__).parent == CLIENT_PKG_DIR:
        return loaded
    for name in [n for n in sys.modules if n.split(".")[0] == "agent_queue_api_client"]:
        del sys.modules[name]
    if str(CLIENT_DIR) not in sys.path:
        sys.path.insert(0, str(CLIENT_DIR))
    pkg = importlib.import_module("agent_queue_api_client")
    assert Path(pkg.__file__).parent == CLIENT_PKG_DIR
    return pkg


def _generated_client_operations() -> dict[tuple[str, str], str]:
    """Parse every generated api module's ``_get_kwargs`` request line.

    Returns ``{(method, path_template): module_name}``.  The path template
    keeps its ``{param}`` placeholders, which is exactly the OpenAPI form.
    """
    ops: dict[tuple[str, str], str] = {}
    for mod_path in sorted((CLIENT_PKG_DIR / "api").glob("*/*.py")):
        if mod_path.name == "__init__.py":
            continue
        source = mod_path.read_text(encoding="utf-8")
        method = re.search(r'"method":\s*"(\w+)"', source)
        url = re.search(r'"url":\s*"([^"]+)"', source)
        assert method and url, f"unparseable generated module: {mod_path}"
        key = (method.group(1).lower(), url.group(1))
        assert key not in ops, f"duplicate generated operation {key}"
        ops[key] = mod_path.stem
    return ops


@pytest.fixture
async def live_app(tmp_path):
    """A real ``create_app()`` application over a SQLite-backed orchestrator."""
    from src.api import dependencies as deps
    from src.api.app import create_app
    from src.config import AppConfig, DiscordConfig
    from src.database import Database
    from src.event_bus import EventBus
    from src.orchestrator import Orchestrator

    db = Database(str(tmp_path / "contract.db"))
    await db.initialize()
    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "contract.db"),
        data_dir=str(tmp_path / "d"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch.bus = EventBus()
    saved = (
        deps._orchestrator,
        deps._command_handler,
        deps._token_store,
        deps._require_session_token,
    )
    try:
        yield create_app(orch, config), db
    finally:
        (
            deps._orchestrator,
            deps._command_handler,
            deps._token_store,
            deps._require_session_token,
        ) = saved
        await db.close()


async def test_live_openapi_operations_match_generated_python_client(live_app):
    """Plan 1 / API-2: exact bidirectional live-surface ↔ client match."""
    from src.api.codegen import API_EXCLUDED

    app, _db = live_app
    spec = app.openapi()

    live_ops: dict[tuple[str, str], str] = {}
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            live_ops[(method.lower(), path)] = op["operationId"]

    generated_ops = _generated_client_operations()

    missing_from_client = sorted(set(live_ops) - set(generated_ops))
    stale_in_client = sorted(set(generated_ops) - set(live_ops))
    assert not missing_from_client and not stale_in_client, (
        "generated client is out of sync with the live app — regenerate with "
        "scripts/regenerate-api-client.sh.\n"
        f"live operations missing from packages/aq-client: {missing_from_client}\n"
        f"stale client operations no longer served: {stale_in_client}"
    )

    # Excluded commands must not exist as typed operations.  The sets match
    # bidirectionally above, so proving them absent from the live surface
    # proves them absent from the client too.
    live_operation_ids = set(live_ops.values())
    for cmd in API_EXCLUDED:
        assert cmd not in live_operation_ids, f"API_EXCLUDED command {cmd} has a live route"


async def test_generated_client_models_round_trip_real_typed_route(live_app):
    """Plan 2: generated models serialize/parse against a real ASGI route."""
    _import_repo_client()
    from agent_queue_api_client.client import Client
    from agent_queue_api_client.api.task import task_show
    from agent_queue_api_client.models.task_show_request import TaskShowRequest
    from agent_queue_api_client.models.task_show_response import TaskShowResponse
    from agent_queue_api_client.models.task_show_response_422 import TaskShowResponse422

    from src.models import Project, Task, TaskStatus

    app, db = live_app
    await db.create_project(Project(id="p1", name="p1"))
    await db.create_task(Task(
        id="t-contract", project_id="p1", title="Contract task",
        description="round trip", status=TaskStatus.DEFINED,
    ))

    client = Client(base_url="http://test", raise_on_unexpected_status=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as http:
        client.set_async_httpx_client(http)

        parsed = await task_show.asyncio(
            client=client, body=TaskShowRequest(task_id="t-contract"),
        )
        assert isinstance(parsed, TaskShowResponse), parsed
        assert parsed.id == "t-contract"
        assert parsed.title == "Contract task"

        err = await task_show.asyncio(
            client=client, body=TaskShowRequest(task_id="no-such-task"),
        )
        assert isinstance(err, TaskShowResponse422), err
        assert "no-such-task" in str(err.error)


async def test_pool_scale_typed_route_preserves_explicit_null_max(live_app):
    """``max: null`` is a meaningful unbounded-pool request, not omission."""
    from src.models import AgentProfile, Project

    app, db = live_app
    await db.create_project(Project(id="pool-api", name="pool-api", max_concurrent_agents=2))
    await db.upsert_profile(
        AgentProfile(id="worker", name="Worker", lifecycle="pool", min_active=1, max_active=2)
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/pool/scale",
            json={"project_id": "pool-api", "profile_id": "worker", "max": None},
        )

    assert response.status_code == 200, response.text
    assert response.json()["max_active"] is None
    assert response.json()["effective_max_active"] == 2


def test_codegen_routes_keep_execute_exclusions_off_every_live_router():
    """Plan 3: one route per discovered command; excluded commands get none."""
    from src.api.codegen import API_EXCLUDED, build_category_routers
    from src.mcp_registration import _discover_all_commands
    from src.plugins.internal import collect_internal_tool_definitions
    from src.tools import CATEGORIES, _ALL_TOOL_DEFINITIONS, _CLI_CATEGORY_OVERRIDES, _TOOL_CATEGORIES

    routed: list[str] = []
    for router in build_category_routers():
        for route in router.routes:
            routed.append(route.operation_id)

    # Every generated route is unique.
    assert len(routed) == len(set(routed)), "duplicate operation ids in codegen routers"
    routed_set = set(routed)

    # Rebuild the discovery + category resolution the way
    # build_category_routers does: explicit tool definitions, discovered
    # ``_cmd_*`` methods, then internal plugin tools (which only supply a
    # category when nothing else claimed the name).
    discovered = {t["name"] for t in _ALL_TOOL_DEFINITIONS}
    discovered.update(_discover_all_commands())
    plugin_categories: dict[str, str] = {}
    for category, tool_defs in collect_internal_tool_definitions():
        for defn in tool_defs:
            name = defn["name"]
            discovered.add(name)
            if name not in _TOOL_CATEGORIES and name not in _CLI_CATEGORY_OVERRIDES:
                plugin_categories.setdefault(name, category)

    by_category: dict[str, set[str]] = {}
    for name in discovered:
        cat = (
            _TOOL_CATEGORIES.get(name)
            or _CLI_CATEGORY_OVERRIDES.get(name)
            or plugin_categories.get(name)
        )
        if cat:
            by_category.setdefault(cat, set()).add(name)

    # Routers are only built for categories registered in CATEGORIES, so a
    # plugin inventing its own category silently loses its typed routes.
    # Today that set is exactly aq-vibecop's three commands — they stay
    # reachable through /api/execute and the CLI only.  Any NEW command
    # dropped this way must show up here and fail loudly.
    unrouted_categories = set(by_category) - set(CATEGORIES)
    dropped = sorted(
        name for cat in unrouted_categories for name in by_category[cat]
    )
    assert dropped == ["vibecop_check", "vibecop_scan", "vibecop_status"], (
        "commands silently dropped from the typed API surface changed: "
        f"{dropped} (categories without a CATEGORIES entry: {sorted(unrouted_categories)})"
    )

    expected = {
        name
        for cat, names in by_category.items()
        if cat in CATEGORIES
        for name in names
    } - API_EXCLUDED
    missing = sorted(expected - routed_set)
    extra = sorted(routed_set - expected)
    assert not missing, f"discovered commands without a typed route: {missing}"
    assert not extra, f"typed routes for undiscovered commands: {extra}"

    assert "run_command" in API_EXCLUDED
    for cmd in API_EXCLUDED:
        assert cmd not in routed_set, f"API_EXCLUDED command {cmd} got a typed route"
