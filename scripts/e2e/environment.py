"""H2 environments for disposable E2E worlds and their child processes.

Legacy callers declare the installed interpreter and standard system tools. A
hermetic caller supplies its H2 Runtime and H3-owned PostgreSQL DSN explicitly;
there is no ambient executable or database fallback on that path.
"""
from __future__ import annotations

import json
import os
import site
import socket
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from tools.bazel.test_environment import Runtime, TestEnvironment, create_test_environment

# Values produced by H2 or by the disposable kit. No arbitrary prefix copying:
# provider credentials, interpreter hooks and live worker identity never pass.
_CHILD_VARIABLES = frozenset("""
HOME PATH LANG LC_ALL TZ TMPDIR TMP TEMP TEST_TMPDIR
XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_RUNTIME_DIR TMUX_TMPDIR
GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM GIT_TERMINAL_PROMPT GIT_EXEC_PATH LD_LIBRARY_PATH SHELL
PYTHONNOUSERSITE PYTEST_DISABLE_PLUGIN_AUTOLOAD PYTHONUNBUFFERED
PYTEST_XDIST_AUTO_NUM_WORKERS AQ_TEST_WORKERS
AQ_CONFIG_PATH AQ_DB_SCOPE POSTGRES_TEST_DSN
AQ_TEST_ENVIRONMENT AQ_TEST_PG_MODE
AQ_E2E_SOCKET_ROOT
AQ_E2E_HOME AQ_E2E_PORT AQ_E2E_API_URL AQ_E2E_DASHBOARD_PORT AQ_E2E_TMUX_SOCKET
AQ_E2E_SESSION_PROVIDER AQ_E2E_CONVERGE_TIMEOUT AQ_E2E_CYCLE_SECONDS
AQ_E2E_CONFIG_POLL_SECONDS AQ_E2E_GRAPH_SWEEP_SECONDS AQ_E2E_POLL_SECONDS
AQ_E2E_TIMINGS_DIR AQ_E2E_EXTRA_CONFIG AQ_E2E_CLI_SOCKET AQ_E2E_FORBID_LLM
AQ_E2E_PYTHONPATH AQ_E2E_PLUGIN_FIXTURE AQ_E2E_STARTUP_TIMEOUT AQ_E2E_STOP_GRACE AQ_E2E_PLAYBOOKS_ENABLED
E2E_PG_HOST E2E_PG_PORT E2E_PG_USER E2E_PG_PASSWORD E2E_DB_NAME E2E_DB_URL E2E_ADMIN_DSN
E2E_CONFIG E2E_VAULT E2E_REPO E2E_OTHER_REPO E2E_LOG E2E_PID_FILE E2E_BIN E2E_FAKE_SCRIPT
E2E_ONBOARDING_ROOT E2E_STATEFUL_WORKSPACE
AQ_API_URL AGENT_QUEUE_DATA AQ_DATABASE_URL AGENT_QUEUE_DB
""".split())


def child_env(source: Mapping[str, str]) -> dict[str, str]:
    """Propagate only the H2/kit contract; callers add fresh local auth explicitly."""
    result = {name: source[name] for name in _CHILD_VARIABLES if name in source}
    if pythonpath := source.get("AQ_E2E_PYTHONPATH"):
        result["PYTHONPATH"] = pythonpath
    return result


def unused_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def legacy_runtime() -> Runtime:
    """Explicit legacy backend tools, never a claim of hermetic execution."""
    python = Path(sys.executable).absolute()
    bins = tuple(dict.fromkeys((python.parent, *(Path(p) for p in os.defpath.split(os.pathsep)))))
    roots = tuple(path for path in bins if not any(
        path != other and path.is_relative_to(other) for other in bins
    ))
    return Runtime(python=python, read_only=roots, bin_dirs=bins)


def database_url(dsn: str, name: str, *, sqlalchemy: bool = False) -> str:
    """Keep Unix socket/port query parameters when choosing a world's database."""
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgresql", "postgresql+asyncpg", "postgres"}:
        raise ValueError("E2E requires a PostgreSQL test DSN")
    return urlunsplit(parsed._replace(
        scheme="postgresql+asyncpg" if sqlalchemy else "postgresql", path="/" + name,
    ))


def world_env(
    home: Path, *, environment: TestEnvironment | None = None,
    postgres_dsn: str | None = None, python_paths: Sequence[Path] = (),
    timings_dir: Path | None = None,
) -> dict[str, str]:
    """Allocate one independent world's environment without changing its parent.

    ``home`` and the private sibling runtime tree belong to the caller's scratch
    directory. They outlive individual shell calls and are removed with that
    directory. Supplied H2 environments remain owned by the target launcher.
    """
    socket_alias_root = None
    manifest = None
    daemon_dsn = None
    if environment is None and os.environ.get("AQ_TEST_PG_MODE") == "pure":
        raise ValueError("E2E requires a declared PostgreSQL target")
    if environment is None and os.environ.get("AQ_TEST_PG_MODE") == "postgres":
        manifest = os.environ.get("AQ_TEST_ENVIRONMENT")
        if not manifest:
            raise ValueError("hermetic E2E requires its H3 environment manifest")
        declaration = json.loads(Path(manifest).read_text())
        fields = declaration["runtime"]
        runtime = Runtime.from_manifest(fields)
        root = Path(declaration["root"])
        environment = TestEnvironment(root, runtime, declaration["env"], (root,))
        python_paths = (*map(Path, declaration["python_paths"]), *python_paths)
        postgres_dsn = postgres_dsn or declaration["postgres_dsn"]
        socket_alias_root = declaration.get("socket_alias_root")
        daemon_dsn = declaration.get("tcp_dsn")
        if not daemon_dsn:
            raise ValueError("E2E daemons require H3's private loopback listener (tcp=True)")
    home = home.absolute()
    home.parent.mkdir(parents=True, exist_ok=True)
    if environment is None:
        runtime = legacy_runtime()
        repo = Path(__file__).resolve().parents[2]
        python_paths = tuple(dict.fromkeys((
            repo, repo / "packages/aq-client", *python_paths,
            *(Path(path) for path in (*site.getsitepackages(), site.getusersitepackages())
              if Path(path).is_dir()),
        )))
        # The legacy pytest substrate has already admitted this disposable DSN.
        postgres_dsn = postgres_dsn or os.environ.get("POSTGRES_TEST_DSN")
        if timings_dir is None and os.environ.get("AQ_E2E_TIMINGS_DIR"):
            timings_dir = Path(os.environ["AQ_E2E_TIMINGS_DIR"]).absolute()
    else:
        if not home.is_relative_to(environment.root):
            raise ValueError("hermetic E2E home must belong to its target environment")
        if not postgres_dsn:
            raise ValueError("hermetic E2E requires its H3-owned PostgreSQL DSN")
        runtime = environment.runtime
        for path in python_paths:
            if not path.is_absolute() or not any(
                path.is_relative_to(root) for root in runtime.read_only
            ):
                raise ValueError("E2E Python dependency path is outside declared inputs")
        if timings_dir is not None and not any(
            timings_dir.is_absolute() and timings_dir.is_relative_to(root)
            for root in environment.writable
        ):
            raise ValueError("E2E timings must belong to declared target output roots")
    root = Path(tempfile.mkdtemp(prefix="e2e-runtime-", dir=home.parent))
    private = create_test_environment(root=root, runtime=runtime)
    env = private.child_env()
    if manifest:
        env.update(AQ_TEST_ENVIRONMENT=manifest, AQ_TEST_PG_MODE="postgres")
    if socket_alias_root:
        env["AQ_E2E_SOCKET_ROOT"] = socket_alias_root
    if timings_dir is not None:
        env["AQ_E2E_TIMINGS_DIR"] = str(timings_dir)
    if python_paths:
        env["AQ_E2E_PYTHONPATH"] = os.pathsep.join(map(str, python_paths))
        env["PYTHONPATH"] = env["AQ_E2E_PYTHONPATH"]
    env.update({
        "AQ_E2E_HOME": str(home),
        "AQ_CONFIG_PATH": str(home / "config.yaml"),
        "AQ_E2E_PORT": str(unused_loopback_port()),
        "AQ_E2E_TMUX_SOCKET": f"aq-e2e-{uuid.uuid4().hex[:12]}",
        "E2E_DB_NAME": f"aq_e2e_cli_{uuid.uuid4().hex[:16]}",
        "AQ_E2E_SESSION_PROVIDER": "fake",
        "AQ_E2E_CONVERGE_TIMEOUT": "90",
        "AQ_E2E_CYCLE_SECONDS": "1",
        "AQ_E2E_CONFIG_POLL_SECONDS": "0.5",
        "AQ_E2E_GRAPH_SWEEP_SECONDS": "1",
        "AQ_E2E_POLL_SECONDS": "0.25",
        "PYTHONUNBUFFERED": "1",
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "1",
        "AQ_TEST_WORKERS": "1",
    })
    env["AQ_E2E_API_URL"] = f"http://127.0.0.1:{env['AQ_E2E_PORT']}"
    env["AQ_API_URL"] = env["AQ_E2E_API_URL"]
    if postgres_dsn:
        from src.database.migration_guard import assert_not_production_database

        assert_not_production_database(postgres_dsn, actor="disposable E2E world")
        env["POSTGRES_TEST_DSN"] = postgres_dsn
        env["E2E_ADMIN_DSN"] = database_url(daemon_dsn or postgres_dsn, "postgres")
        env["E2E_DB_URL"] = database_url(
            daemon_dsn or postgres_dsn, env["E2E_DB_NAME"], sqlalchemy=True,
        )
    return env
