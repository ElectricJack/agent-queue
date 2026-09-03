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
   and no ``API_EXCLUDED`` command (``run_command`` above all) gets a route;
4. the committed ``openapi.json`` — the artifact both clients are generated
   from — still equals the spec that same ``create_app()`` serves;
5. the parts of ``packages/aq-client/`` that depend on the generator rather
   than on the spec (``README.md``, ``pyproject.toml``, the runtime modules)
   are byte-for-byte what the pinned generator writes, so a regeneration
   rewrites nothing the spec did not change.  The generator's post hooks
   format the generated Python with ruff, and formatter output is a function
   of the ruff release, so ruff is pinned exactly alongside the generator and
   the same guards cover both pins.

The generated client is read from the repo checkout (``packages/aq-client``),
not from whatever copy happens to be installed, so the guard always compares
the committed artifacts of *this* revision.
"""

from __future__ import annotations

import importlib
import json
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
    from agent_queue_api_client.api.task import task_show
    from agent_queue_api_client.client import Client
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
            json={"profile_id": "worker", "max": None},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["max_active"] is None
    # Bounds are global; each project's own cap is still reported per project.
    assert body["project_caps"] == [
        {"project_id": "pool-api", "max_concurrent_agents": 2, "effective_max_active": 2}
    ]


def test_codegen_routes_keep_execute_exclusions_off_every_live_router():
    """Plan 3: one route per discovered command; excluded commands get none."""
    from src.api.codegen import API_EXCLUDED, build_category_routers
    from src.mcp_registration import _discover_all_commands
    from src.plugins.internal import collect_internal_tool_definitions
    from src.tools import (
        _ALL_TOOL_DEFINITIONS,
        _CLI_CATEGORY_OVERRIDES,
        _TOOL_CATEGORIES,
        CATEGORIES,
    )

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


def test_committed_openapi_json_matches_the_live_app_surface():
    """The committed ``openapi.json`` is the spec ``create_app()`` actually serves.

    ``openapi.json`` is a build artifact that nothing regenerates per commit,
    so a change to ``src/api/models`` used to land with the spec — and every
    client generated from it — silently behind.  That is how
    ``PoolStatusRow.quarantined_reason`` reached ``main`` without reaching a
    single typed consumer.  Regenerating needs no daemon (see
    ``src.api.spec``), so there is no reason to let it drift.
    """
    from src.api.spec import SPEC_PATH, build_openapi_spec, render_openapi_spec

    live = build_openapi_spec()
    committed = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

    if live == committed:
        # Also pin the on-disk rendering so the file the scripts write and
        # the file in git stay diffable line-for-line.
        assert SPEC_PATH.read_text(encoding="utf-8") == render_openapi_spec(committed), (
            f"{SPEC_PATH.name} is valid but not formatted the way "
            "scripts/regenerate-api-client.sh --offline writes it — regenerate it."
        )
        return

    live_paths = {(m.lower(), p) for p, ms in live["paths"].items() for m in ms}
    committed_paths = {(m.lower(), p) for p, ms in committed["paths"].items() for m in ms}
    live_schemas = set(live.get("components", {}).get("schemas", {}))
    committed_schemas = set(committed.get("components", {}).get("schemas", {}))
    changed_schemas = sorted(
        name
        for name in live_schemas & committed_schemas
        if live["components"]["schemas"][name] != committed["components"]["schemas"][name]
    )

    raise AssertionError(
        "openapi.json is out of sync with the app create_app() serves, so "
        "packages/aq-client and the dashboard's TS client are stale too. "
        "Regenerate with:\n"
        "  ./scripts/regenerate-api-client.sh --offline\n"
        "  ./scripts/regenerate-ts-client.sh --from-file\n"
        f"operations missing from openapi.json: {sorted(live_paths - committed_paths)}\n"
        f"stale operations in openapi.json: {sorted(committed_paths - live_paths)}\n"
        f"schemas missing from openapi.json: {sorted(live_schemas - committed_schemas)}\n"
        f"stale schemas in openapi.json: {sorted(committed_schemas - live_schemas)}\n"
        f"schemas whose definition changed: {changed_schemas}"
    )


def test_generator_version_pin_agrees_between_the_script_and_the_dev_extra():
    """The client generator is pinned exactly, in both places that install it.

    Every file under ``packages/aq-client/`` — the boilerplate ``README.md``
    the generator writes included — is a function of the
    ``openapi-python-client`` version, not just of ``openapi.json``.  With the
    version floating, a box on a different release rewrote files nobody
    touched and the ``git diff --exit-code`` idempotence check in the Package 5
    verification section failed for a reason unrelated to the spec.  So the
    version is pinned in ``scripts/regenerate-api-client.sh`` (which refuses to
    run against anything else) and in the ``dev`` extra (which installs it);
    this pins the two together.
    """
    script = (REPO_ROOT / "scripts" / "regenerate-api-client.sh").read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    script_pin = re.search(r'^GENERATOR_VERSION="([^"]+)"', script, re.MULTILINE)
    assert script_pin, (
        "scripts/regenerate-api-client.sh no longer pins GENERATOR_VERSION — "
        "without it, regeneration is not idempotent across machines."
    )
    extra_pin = re.search(r'"openapi-python-client==([^"]+)"', pyproject)
    assert extra_pin, (
        'pyproject.toml no longer pins "openapi-python-client==<version>" in the dev extra.'
    )
    assert script_pin.group(1) == extra_pin.group(1), (
        f"generator version pins disagree: the script wants {script_pin.group(1)}, "
        f"the dev extra installs {extra_pin.group(1)}."
    )


def _pinned_versions() -> tuple[str, str]:
    """``(generator, ruff)`` versions pinned by ``scripts/regenerate-api-client.sh``."""
    script = (REPO_ROOT / "scripts" / "regenerate-api-client.sh").read_text(encoding="utf-8")
    generator = re.search(r'^GENERATOR_VERSION="([^"]+)"', script, re.MULTILINE)
    ruff = re.search(r'^RUFF_VERSION="([^"]+)"', script, re.MULTILINE)
    assert generator, (
        "scripts/regenerate-api-client.sh no longer pins GENERATOR_VERSION — "
        "without it, regeneration is not idempotent across machines."
    )
    assert ruff, (
        "scripts/regenerate-api-client.sh no longer pins RUFF_VERSION — without it the "
        "generated Python is formatted by whichever ruff is on PATH, and formatter output "
        "is a function of the ruff release."
    )
    return generator.group(1), ruff.group(1)


def test_formatter_version_pin_agrees_between_the_script_and_the_dev_extra():
    """ruff is pinned exactly, in both places that install it, like the generator.

    The generator is only half of what writes ``packages/aq-client/``: its post
    hooks (``scripts/openapi-python-client.yaml``) run ``ruff check --fix-only``
    and ``ruff format`` over the generated package with whichever ``ruff`` is
    first on PATH, and formatter output is a function of the ruff release.
    Generating from the committed ``openapi.json`` with ruff 0.9.1 (the repo's
    pre-commit pin) rewrites 215 of the modules ruff 0.16.5 writes.  With the
    dev extra at ``ruff>=0.4.0`` CI installed the newest release while
    developer boxes carried whatever they had, so a ruff release that changed
    formatting would have rewritten committed generated files on CI, failed
    the boilerplate tests below with a message that says "regenerate", and
    invited a box-dependent ratchet -- the README failure mode of run
    33800115024 again, this time in the Python.

    So ruff is pinned the same way the generator is: ``RUFF_VERSION`` in the
    script (which refuses any other release) and ``ruff==`` in the ``dev``
    extra (which installs it); this pins the two together.  This is a
    deliberate policy: every box that installs the ``dev`` extra runs the same
    ruff.  Upgrading is bumping both and committing the regenerated client in
    the same change.
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    _, script_pin = _pinned_versions()
    extra_pin = re.search(r'"ruff==([^"]+)"', pyproject)
    assert extra_pin, (
        'pyproject.toml no longer pins "ruff==<version>" in the dev extra; a floor '
        "(ruff>=...) installs a different formatter on every box."
    )
    assert script_pin == extra_pin.group(1), (
        f"ruff version pins disagree: the script wants {script_pin}, "
        f"the dev extra installs {extra_pin.group(1)}."
    )


# The part of packages/aq-client/ that is a function of the *generator*, not of
# openapi.json: boilerplate the generator writes verbatim (modulo the project /
# package names and the spec's info block) on every run.
_GENERATOR_BOILERPLATE = (
    "README.md",
    "pyproject.toml",
    "agent_queue_api_client/__init__.py",
    "agent_queue_api_client/client.py",
    "agent_queue_api_client/errors.py",
    "agent_queue_api_client/types.py",
    "agent_queue_api_client/py.typed",
)


# Digests of that boilerplate, recorded by scripts/regenerate-api-client.sh in
# sha256sum(1) format so they can also be checked by hand:
#   cd packages/aq-client && sha256sum -c ../../scripts/aq-client-boilerplate.sha256
# Its first line is a comment naming the tool versions the digests were
# recorded under (sha256sum -c ignores comment lines).
_BOILERPLATE_DIGESTS = REPO_ROOT / "scripts" / "aq-client-boilerplate.sha256"
_PROVENANCE_LINE = re.compile(
    r"^#\s*generated by openapi-python-client (?P<generator>\S+) with ruff (?P<ruff>\S+)\s*$"
)


def _recorded_boilerplate_digests() -> dict[str, str]:
    """Parse the committed sha256sum manifest into ``{path: digest}``."""
    recorded: dict[str, str] = {}
    for line in _BOILERPLATE_DIGESTS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        digest, _, name = line.partition("  ")
        recorded[name.strip()] = digest.strip()
    return recorded


def _recorded_boilerplate_provenance() -> tuple[str, str] | None:
    """``(generator, ruff)`` versions the manifest's header says it was recorded under."""
    for line in _BOILERPLATE_DIGESTS.read_text(encoding="utf-8").splitlines():
        match = _PROVENANCE_LINE.match(line)
        if match:
            return match.group("generator"), match.group("ruff")
    return None


def _pinned_generator() -> tuple[str, str]:
    """``(executable, version)`` for the pinned generator, or a skip without it."""
    import shutil
    import subprocess

    pinned, _ = _pinned_versions()

    generator = shutil.which("openapi-python-client")
    if generator is None:
        pytest.skip(f"openapi-python-client=={pinned} is not installed (dev extra)")
    found = subprocess.run(
        [generator, "--version"], capture_output=True, text=True, check=True
    ).stdout.split()[-1]
    if found != pinned:
        pytest.skip(f"openapi-python-client {found} installed, tree is generated by {pinned}")
    return generator, pinned


def _pinned_formatter() -> str:
    """The pinned ruff version, or a skip when the ruff on PATH is not it.

    The generator's post hooks run the first ``ruff`` on PATH (``shutil.which``,
    then ``subprocess.run(..., shell=True)``), so that is the one to check.
    Any other release formats the generated Python differently, and comparing
    its output against the committed tree would report a "drift" that is
    really the box's formatter.  CI installs the dev extra and never skips
    here; a developer box on another ruff is still covered by
    ``test_generated_client_boilerplate_matches_the_recorded_digests``.
    """
    import shutil
    import subprocess

    _, pinned = _pinned_versions()

    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip(f"ruff=={pinned} is not installed (dev extra)")
    found = subprocess.run([ruff, "--version"], capture_output=True, text=True, check=True)
    found_version = found.stdout.split()[-1]
    if found_version != pinned:
        pytest.skip(f"ruff {found_version} is on PATH, tree is formatted by ruff {pinned}")
    return pinned


def _generate_from_minimal_spec(generator: str, out: Path, *, path: str | None = None) -> Path:
    """Generate a client from an empty spec carrying the real ``info`` block.

    The generator-only boilerplate does not depend on the 274-path spec at all
    — only on the generator version and on ``scripts/openapi-python-client.yaml``
    — so this takes ~0.5s where a full regeneration takes minutes.  ``path``
    replaces ``PATH`` for the run, which is how
    ``test_generated_client_readme_does_not_depend_on_the_ambient_formatter``
    generates with ruff out of the generator's reach.
    """
    import os
    import subprocess

    spec = json.loads((REPO_ROOT / "openapi.json").read_text(encoding="utf-8"))
    minimal = out.parent / f"{out.name}-openapi.json"
    minimal.write_text(
        json.dumps({"openapi": spec["openapi"], "info": spec["info"], "paths": {}}),
        encoding="utf-8",
    )

    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    result = subprocess.run(
        [
            generator,
            "generate",
            "--path",
            str(minimal),
            "--output-path",
            str(out),
            "--config",
            str(REPO_ROOT / "scripts" / "openapi-python-client.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, (
        f"the pinned generator failed on a minimal spec:\n{result.stdout}\n{result.stderr}"
    )
    return out


def test_generated_client_boilerplate_matches_what_the_pinned_generator_writes(tmp_path):
    """Regenerating rewrites nothing the spec did not change.

    The version pin is a *declaration* that the committed tree came from
    ``GENERATOR_VERSION``; nothing checked it against the tree itself.  So a
    tree generated by an older release survived the pin: on 2026-09-02
    ``./scripts/regenerate-api-client.sh --offline`` still rewrote 11 lines of
    ``packages/aq-client/README.md`` (a collapsed call, three blank lines) with
    the pinned 0.29.0 installed and ``openapi.json`` unchanged, which fails the
    ``git diff --exit-code`` idempotence check for a reason unrelated to the
    spec.

    Those files do not depend on the 274-path spec at all — only on the
    generator version, on the ruff release its post hooks format the Python
    with, and on ``scripts/openapi-python-client.yaml`` (the two name
    overrides and those post hooks) — so this generates them from an empty
    spec carrying the real ``info`` block (~0.5s) and compares them
    byte-for-byte, instead of running a full regeneration.  It runs only with
    *both* pinned tools on PATH: on any other ruff the comparison would
    measure the box's formatter, not the tree.
    """
    generator, pinned = _pinned_generator()
    pinned_ruff = _pinned_formatter()
    out = _generate_from_minimal_spec(generator, tmp_path / "generated")

    drifted = [
        name
        for name in _GENERATOR_BOILERPLATE
        if (CLIENT_DIR / name).read_bytes() != (out / name).read_bytes()
    ]
    assert not drifted, (
        "packages/aq-client/ carries boilerplate the pinned "
        f"openapi-python-client {pinned} (post hooks on ruff {pinned_ruff}) does not "
        "write, so regeneration is not idempotent and rewrites these files with "
        f"openapi.json unchanged: {drifted}. Regenerate with exactly those versions "
        "and commit the result:\n"
        "  ./scripts/regenerate-api-client.sh --offline"
    )

    # The digest manifest is what carries this guard to boxes without the
    # generator, so it has to be checked against the generator wherever there
    # is one -- otherwise a stale or hand-written manifest would silently
    # bless whatever the tree happens to hold.
    import hashlib

    recorded = _recorded_boilerplate_digests()
    stale = [
        name
        for name in _GENERATOR_BOILERPLATE
        if recorded.get(name) != hashlib.sha256((out / name).read_bytes()).hexdigest()
    ]
    assert not stale, (
        f"{_BOILERPLATE_DIGESTS.relative_to(REPO_ROOT)} does not record what the pinned "
        f"openapi-python-client {pinned} with ruff {pinned_ruff} writes for: {stale}. "
        "It is written by the regeneration script, so re-run it and commit the result:\n"
        "  ./scripts/regenerate-api-client.sh --offline"
    )


def test_generated_client_readme_does_not_depend_on_the_ambient_formatter(tmp_path):
    """README.md is what the generator writes, not what the local ruff reformats.

    The generator's default post hooks (``ruff check --fix-only .``,
    ``ruff format .``) run over the whole generated project, and a recent ruff
    reformats the Python code blocks *inside* README.md — collapsing a call,
    stripping trailing whitespace, spacing out defs — while an older one, or a
    box with no ruff on PATH (the hook is then skipped with a warning and exit
    code 0), leaves them alone.  That made the committed README a function of
    the box that last regenerated it rather than of the pinned generator: PR
    #388 ratcheted an unformatted README and Linux CI, on ruff 0.16.6,
    reformatted it on every run, so
    ``test_generated_client_boilerplate_matches_what_the_pinned_generator_writes``
    failed deterministically on main.

    Neither the version pin nor a recorded digest can express that — both
    happily bless whichever README the ratcheting box produced.  So generate
    twice, once with ruff reachable and once with it out of ``PATH``, and
    require the same bytes.  ``scripts/openapi-python-client.yaml`` earns that
    by scoping both hooks to the generated Python package, which is the only
    Python in the tree, so the .py output is formatted exactly as before.

    The generated *Python* is still formatted by whichever ruff the hooks
    find, and that is deliberate — it is what the tree was generated with.  A
    ruff release that reformats it will fail the byte-comparison above; that
    one is a real regeneration (``./scripts/regenerate-api-client.sh
    --offline``), where a README difference never is.
    """
    import shutil

    generator, _ = _pinned_generator()
    if shutil.which("ruff") is None:
        pytest.skip("ruff is not installed, so this check would pass vacuously")

    bare_bin = tmp_path / "bin-without-ruff"
    bare_bin.mkdir()

    with_formatter = _generate_from_minimal_spec(generator, tmp_path / "with-ruff")
    without_formatter = _generate_from_minimal_spec(
        generator, tmp_path / "without-ruff", path=str(bare_bin)
    )

    assert (with_formatter / "README.md").read_bytes() == (
        without_formatter / "README.md"
    ).read_bytes(), (
        "packages/aq-client/README.md depends on whether a formatter is on PATH when "
        "the client is generated, so the committed file is a function of the box that "
        "last regenerated it and CI rewrites it. Keep the post_hooks in "
        "scripts/openapi-python-client.yaml scoped to the generated Python package so "
        "that nothing reformats the README."
    )


def test_generated_client_boilerplate_matches_the_recorded_digests():
    """The boilerplate is verifiable without the generator installed.

    ``test_generated_client_boilerplate_matches_what_the_pinned_generator_writes``
    can only run where ``openapi-python-client==GENERATOR_VERSION`` is
    installed; everywhere else it skips, so on a box carrying only the
    ``cli`` extra the guard is silently a no-op.  That is how 7eba1124
    ("docs(client): sync README with pinned generator") landed a ``README.md``
    the pinned generator does not write — the *pre-pin* file, with trailing
    whitespace after commas and a re-expanded ``AuthenticatedClient`` call —
    while both the pin and the tree check were already in its history.  CI
    installs the ``dev`` extra and would have failed; nothing local did.

    So the regeneration script also records the digests of those files, and
    this compares the committed tree against them.  It needs nothing but the
    checkout, which means a hand-edit of a generated file fails on every box.
    The manifest cannot itself go stale unnoticed: the generator test above
    checks it against freshly generated bytes wherever the generator is
    installed.

    The manifest's header names the generator and ruff versions the digests
    were recorded under.  Requiring it to agree with the script's pins means
    bumping a pin without regenerating fails on every box too, with a message
    that says which pin moved, instead of only on CI once the newly pinned
    tool actually writes something different.
    """
    import hashlib

    pinned_generator, pinned_ruff = _pinned_versions()
    provenance = _recorded_boilerplate_provenance()
    assert provenance is not None, (
        f"{_BOILERPLATE_DIGESTS.relative_to(REPO_ROOT)} has no provenance header "
        '("# generated by openapi-python-client <version> with ruff <version>"). '
        "It is written by the regeneration script, so re-run it and commit the result:\n"
        "  ./scripts/regenerate-api-client.sh --offline"
    )
    assert provenance == (pinned_generator, pinned_ruff), (
        f"{_BOILERPLATE_DIGESTS.relative_to(REPO_ROOT)} was recorded under "
        f"openapi-python-client {provenance[0]} with ruff {provenance[1]}, but "
        f"scripts/regenerate-api-client.sh now pins openapi-python-client {pinned_generator} "
        f"with ruff {pinned_ruff}. A pin was bumped without regenerating the client: "
        "install exactly the pinned versions, regenerate, and commit the result with "
        "the pin bump:\n"
        "  ./scripts/regenerate-api-client.sh --offline"
    )

    recorded = _recorded_boilerplate_digests()
    assert set(recorded) == set(_GENERATOR_BOILERPLATE), (
        f"{_BOILERPLATE_DIGESTS.relative_to(REPO_ROOT)} does not cover the boilerplate "
        f"list: missing {sorted(set(_GENERATOR_BOILERPLATE) - set(recorded))}, "
        f"unexpected {sorted(set(recorded) - set(_GENERATOR_BOILERPLATE))}. "
        "Regenerate it:\n  ./scripts/regenerate-api-client.sh --offline"
    )

    drifted = {
        name: hashlib.sha256((CLIENT_DIR / name).read_bytes()).hexdigest()
        for name in _GENERATOR_BOILERPLATE
        if hashlib.sha256((CLIENT_DIR / name).read_bytes()).hexdigest() != recorded[name]
    }
    assert not drifted, (
        "packages/aq-client/ carries boilerplate that is not what the pinned "
        f"openapi-python-client {pinned_generator} with ruff {pinned_ruff} wrote — these "
        f"files no longer match their recorded digests: {sorted(drifted)}. Either they "
        "were hand-edited (restore them) or they were generated by another "
        "openapi-python-client or formatted by another ruff (regenerate with exactly the "
        "pinned versions; the script refuses any other):\n"
        "  ./scripts/regenerate-api-client.sh --offline"
    )
