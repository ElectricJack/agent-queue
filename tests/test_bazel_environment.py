"""H2 environment boundary and real Linux namespace/process acceptance.

AQ_BAZEL_ISOLATION=1 requires actual namespaces, never mocks/skips on refusal.
The local acceptance runtime is explicitly declared system Python/libraries;
Bazel callers supply H1's locked runtime closure through the same interface.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import sys
from pathlib import Path

import pytest

from tools.bazel import test_environment as isolated


@pytest.fixture
def runtime(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    python = inputs / "python"
    python.touch(mode=0o700)
    return isolated.Runtime(python=python, read_only=(inputs,), bin_dirs=(inputs,))


def test_poisoned_ambient_environment_cannot_cross_boundary(tmp_path, runtime, monkeypatch):
    poison = {
        "HOME": "/operator/home", "PATH": "/operator/bin", "XDG_CONFIG_HOME": "/operator/config",
        "GH_TOKEN": "github-secret", "GITHUB_TOKEN": "github-secret",
        "ANTHROPIC_API_KEY": "provider-secret", "OPENAI_API_KEY": "provider-secret",
        "UNRECOGNIZED_VENDOR_CREDENTIAL": "provider-secret", "AQ_API_TOKEN": "live-token",
        "AQ_SESSION_ID": "live-session", "AQ_TASK_ID": "live-task",
        "CLAUDE_CODE_SESSION_ID": "live-session", "TMUX": "live-tmux",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "operator-helper", "GIT_CONFIG_GLOBAL": "/operator/gitconfig",
        "PYTHONPATH": "/operator/python", "LD_PRELOAD": "/operator/hook.so",
        "DOCKER_HOST": "unix:///var/run/docker.sock", "POSTGRES_TEST_DSN": "live-db",
        "AQ_DB_SCOPE": "worker", "AQ_DATABASE_URL": "worker-refusal-sentinel",
        "BAZEL_SECRET": "not-a-protocol-variable", "TEST_UNRECOGNIZED": "secret",
    }
    for key, value in poison.items():
        monkeypatch.setenv(key, value)
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime) as environment:
        env = environment.child_env({"HYPOTHESIS_PROFILE": "ci"})
        assert all(env.get(key) != value for key, value in poison.items())
        assert env["PATH"] == str(runtime.bin_dirs[0])
        assert env["TZ"] == "UTC"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["HYPOTHESIS_PROFILE"] == "ci"
        for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
                     "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "TMUX_TMPDIR", "AQ_E2E_HOME"):
            path = Path(env[name])
            assert path.is_relative_to(environment.root)
            assert path.stat().st_mode & 0o777 == 0o700
        assert Path(env["AQ_CONFIG_PATH"]).read_text() == "{}\n"
        env["PATH"] = "mutated"
        assert environment.child_env()["PATH"] != "mutated"
        root = environment.root
    assert not root.exists()
    for key, value in poison.items():
        assert os.environ[key] == value  # including the parent's worker sentinels


@pytest.mark.parametrize("key", [
    "HOME", "PATH", "AQ_API_TOKEN", "AQ_DATABASE_URL", "GH_TOKEN", "GITHUB_TOKEN",
    "GEMINI_API_KEY", "TMUX_TMPDIR", "GIT_CONFIG_COUNT", "PYTHONPATH", "LD_PRELOAD",
    "BASH_ENV", "TEST_TMPDIR", "CLAUDE_CODE_SESSION_ID", "LOWER_case",
])
def test_explicit_settings_cannot_override_boundary(tmp_path, runtime, key):
    with pytest.raises(isolated.IsolationError):
        with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime, settings={key: "x"}):
            pytest.fail("unsafe setting admitted")


def test_shards_have_private_state_and_only_declared_bazel_paths(tmp_path, runtime):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    bazel = {
        "RUNFILES_DIR": str(runtime.read_only[0]), "TEST_SHARD_INDEX": "1",
        "TEST_TOTAL_SHARDS": "2", "XML_OUTPUT_FILE": str(outputs / "results.xml"),
    }
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime, bazel=bazel,
                                   output_roots=(outputs,)) as first:
        with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime) as second:
            assert first.root != second.root
            assert first.environ["HOME"] != second.environ["HOME"]
            assert first.environ["TEST_SHARD_INDEX"] == "1"
            assert first.environ["XML_OUTPUT_FILE"] == str(outputs / "results.xml")
    for bad in ({"TEST_SECRET": "x"}, {"RUNFILES_DIR": "/operator/runfiles"},
                {"XML_OUTPUT_FILE": "/operator/output"}):
        with pytest.raises(isolated.IsolationError):
            with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime, bazel=bad):
                pytest.fail("undeclared Bazel input admitted")


def test_missing_runtime_and_overlapping_mounts_fail_closed(tmp_path, runtime):
    with pytest.raises(isolated.IsolationError, match="missing"):
        isolated.Runtime(Path("/nonexistent/python"), runtime.read_only, runtime.bin_dirs).validate()
    with pytest.raises(isolated.IsolationError, match="overlap"):
        with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime,
                                       output_roots=runtime.read_only):
            pytest.fail("overlapping mounts admitted")


async def test_invalid_command_platform_and_budget_never_spawn(tmp_path, runtime, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid launch reached spawn")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime) as environment:
        for command, timeout in ((["python"], 1), (["/usr/bin/python"], 1),
                                  ([str(runtime.python)], 300), ([str(runtime.python)], float("nan"))):
            with pytest.raises(isolated.IsolationError):
                await isolated.run_isolated(environment, command, cwd=environment.root,
                                            timeout=timeout)
        monkeypatch.setattr(sys, "platform", "darwin")
        with pytest.raises(isolated.IsolationError, match="legacy backend"):
            await isolated.run_isolated(environment, [str(runtime.python)], cwd=environment.root)


def test_namespace_refusal_never_runs_target(monkeypatch, capsys, tmp_path):
    config = tmp_path / "launch.json"
    config.write_text("{}")
    monkeypatch.setattr(sys, "argv", ["launcher", "--namespace-init", str(config)])
    def denied(*args):
        raise PermissionError("namespace disabled")
    monkeypatch.setattr(os, "unshare", denied)
    assert isolated.main() == 125
    assert "target was not admitted" in capsys.readouterr().err


@pytest.fixture
def namespace_runtime(tmp_path):
    if os.environ.get("AQ_BAZEL_ISOLATION") != "1":
        pytest.skip("set AQ_BAZEL_ISOLATION=1 for required real namespace acceptance")
    assert sys.platform == "linux", "acceptance requires Linux namespaces"
    # Explicit local acceptance fixture only; production never discovers these.
    # Include the interpreter's shared stdlib and ABI libraries, not /usr or HOME.
    binary_dir = tmp_path / "runtime" / "bin"
    binary_dir.mkdir(parents=True)
    python = binary_dir / "python3.12"
    shutil.copy2(Path(sys.executable).resolve(), python)
    shell = binary_dir / "sh"
    shutil.copy2(Path("/bin/sh").resolve(), shell)
    roots = (binary_dir, Path("/usr/lib/python3.12"), Path("/lib/x86_64-linux-gnu"),
             Path("/lib64/ld-linux-x86-64.so.2"))
    assert all(path.exists() for path in roots), "declare this runner's Python ABI explicitly"
    return isolated.Runtime(python, roots, (python.parent,), shell=shell)


async def test_actual_namespaces_poison_loopback_and_filesystem(
    tmp_path, namespace_runtime, monkeypatch,
):
    # python.parent is only a PATH entry: expose a single executable, not /usr/bin.
    poison_home = tmp_path / "operator"
    poison_home.mkdir()
    (poison_home / ".gitconfig").write_text("secret operator configuration")
    monkeypatch.setenv("HOME", str(poison_home))
    monkeypatch.setenv("GH_TOKEN", "do-not-inherit")
    monkeypatch.setenv("PATH", str(poison_home))
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=namespace_runtime) as env:
        result = env.root / "result.json"
        script = """
import json, os, pathlib, pty, pwd, socket, subprocess
assert 'GH_TOKEN' not in os.environ
assert not pathlib.Path(%r).exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/usr/bin/git').exists()
assert not pathlib.Path('/etc/gitconfig').exists()
assert 'CapEff:\t0000000000000000' in pathlib.Path('/proc/self/status').read_text()
assert pwd.getpwuid(os.getuid()).pw_name == 'test'
assert pwd.getpwuid(os.getuid()).pw_dir == os.environ['HOME']
assert pathlib.Path('/proc/1/environ').read_bytes()
assert socket.gethostbyname('localhost') == '127.0.0.1'
master, slave = pty.openpty()
os.write(slave, b'private terminal')
assert os.read(master, 64) == b'private terminal'
os.close(master); os.close(slave)
assert subprocess.check_output('printf declared-shell', shell=True) == b'declared-shell'
assert socket.if_nameindex() == [(1, 'lo')]
with socket.socket() as server:
    server.bind(('127.0.0.1', 0)); server.listen()
    with socket.create_connection(server.getsockname()), server.accept()[0]: pass
with socket.socket() as outside:
    outside.settimeout(1)
    assert outside.connect_ex(('192.0.2.1', 443)) != 0
pathlib.Path(%r).write_text(json.dumps({'pid': os.getpid(), 'home': os.environ['HOME']}))
""" % (str(poison_home), str(result))
        assert await isolated.run_isolated(env, [str(namespace_runtime.python), "-I", "-c", script],
                                           cwd=env.root, timeout=10) == 0
        assert json.loads(result.read_text())["pid"] == 2


@pytest.mark.parametrize("mode", ["exit", "timeout", "cancel", "sigterm", "sigint", "kill_helper"])
async def test_actual_namespace_reaps_detached_descendants(
    tmp_path, namespace_runtime, mode, monkeypatch,
):
    spawn = asyncio.create_subprocess_exec
    helpers = []

    async def capture_helper(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        helpers.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_helper)
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=namespace_runtime) as env:
        ready = env.root / "ready"
        heartbeat = env.root / "heartbeat"
        script = """
import os, pathlib, signal, time
if os.fork() == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path(%r).touch()
    while True:
        pathlib.Path(%r).write_text(str(time.monotonic_ns()))
        time.sleep(.02)
else:
    while not pathlib.Path(%r).exists(): time.sleep(.01)
    if %r != 'exit':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True: time.sleep(1)
""" % (str(ready), str(heartbeat), str(ready), mode)
        task = asyncio.create_task(isolated.run_isolated(
            env, [str(namespace_runtime.python), "-I", "-c", script], cwd=env.root,
            timeout=1 if mode == "timeout" else 10, grace=.15,
        ))
        if mode in {"cancel", "sigterm", "sigint", "kill_helper"}:
            async with asyncio.timeout(8):
                while not ready.exists() and not task.done():
                    await asyncio.sleep(.02)
            assert ready.exists(), f"namespace setup failed: {await task}"
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                sig = {"sigterm": signal.SIGTERM, "sigint": signal.SIGINT,
                       "kill_helper": signal.SIGKILL}[mode]
                helpers[0].send_signal(sig)
                assert await task == 128 + sig
        else:
            assert await task == (124 if mode == "timeout" else 0)
        assert ready.exists(), "namespace target never started"
        # The private PID namespace guarantees no descendant survives PID 1.
        before = heartbeat.read_text() if heartbeat.exists() else None
        await asyncio.sleep(.15)
        assert (heartbeat.read_text() if heartbeat.exists() else None) == before


@pytest.fixture
def postgres_runtime(tmp_path):
    """Explicit H1 closure for real PG acceptance; never discover ambient PG."""
    from tools.bazel.postgres_cluster import PostgresBinaries

    location = os.environ.get("AQ_BAZEL_POSTGRES_RUNTIME")
    if not location:
        pytest.skip("set AQ_BAZEL_POSTGRES_RUNTIME to H1's locked native runtime")
    native = Path(location).resolve()
    binary = native / "bin"
    binaries = PostgresBinaries(*(binary / name for name in ("initdb", "postgres", "pg_ctl", "psql")))
    runtime = isolated.Runtime(
        Path(sys.executable).resolve(), (native, Path(sys.executable).resolve().parent),
        (binary,), library_dirs=(native / "lib",),
    )
    return runtime, binaries


async def test_real_pg_clusters_are_private_and_preserve_parent(tmp_path, postgres_runtime):
    import asyncpg
    from tools.bazel.postgres_cluster import postgres_cluster

    runtime, binaries = postgres_runtime
    before = {key: os.environ.get(key) for key in
              ("POSTGRES_TEST_DSN", "AQ_DB_SCOPE", "AQ_DATABASE_URL", "AGENT_QUEUE_DB")}
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime) as first_env:
        with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime) as second_env:
            async with postgres_cluster(first_env, binaries, socket_alias_root=Path("/tmp")) as one:
                async with postgres_cluster(second_env, binaries, tcp=True,
                                            socket_alias_root=Path("/tmp")) as two:
                    assert one.port != two.port and one.socket_dir != two.socket_dir
                    assert one.root != two.root and one.dsn != two.dsn
                    assert one.root.is_relative_to(first_env.root)
                    assert two.root.is_relative_to(second_env.root)
                    a = await asyncpg.connect(one.dsn.replace("+asyncpg", ""))
                    b = await asyncpg.connect(two.dsn.replace("+asyncpg", ""))
                    try:
                        await a.execute("CREATE TABLE private_target (value text)")
                        assert await b.fetchval("SELECT to_regclass('private_target')") is None
                        assert await a.fetchval("SHOW listen_addresses") == ""
                        with socket.create_connection(("127.0.0.1", two.port), timeout=1):
                            pass
                        with socket.socket() as connection:
                            assert connection.connect_ex(("127.0.0.1", one.port)) != 0
                        assert one.environ["AQ_TEST_PG_MODE"] == "postgres"
                        assert one.environ["AQ_DB_SCOPE"] == "test"
                    finally:
                        await a.close()
                        await b.close()
                assert two.process.returncode is not None
                assert not two.root.exists() and not two.socket_dir.exists()
                # The other target's shutdown cannot disturb the surviving cluster.
                a = await asyncpg.connect(one.dsn.replace("+asyncpg", ""))
                try:
                    assert await a.fetchval("SELECT to_regclass('private_target')") is not None
                finally:
                    await a.close()
            assert one.process.returncode is not None
            assert not one.root.exists() and not one.socket_dir.exists()
    assert {key: os.environ.get(key) for key in before} == before


async def test_real_pg_cluster_cancellation_kills_connected_backends(tmp_path, postgres_runtime):
    import asyncpg
    from tools.bazel.postgres_cluster import postgres_cluster

    runtime, binaries = postgres_runtime
    ready = asyncio.Event()
    clusters, connections = [], []
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime) as environment:
        async def work():
            async with postgres_cluster(environment, binaries, grace=.2,
                                        socket_alias_root=Path("/tmp")) as cluster:
                clusters.append(cluster)
                connection = await asyncpg.connect(cluster.dsn.replace("+asyncpg", ""))
                connections.append(connection)
                ready.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(work())
        try:
            async with asyncio.timeout(40):
                while not ready.is_set() and not task.done():
                    await asyncio.sleep(.02)
            if task.done():
                await task
            assert ready.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            cluster = clusters[0]
            assert cluster.process.returncode is not None
            assert not cluster.root.exists() and not cluster.socket_dir.exists()
            async with asyncio.timeout(5):
                while not connections[0].is_closed():
                    await asyncio.sleep(.02)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for connection in connections:
                connection.terminate()


async def _pytest_probe(tmp_path, source, *, mode="pure", collect=False):
    """Load real root fixtures in a child with explicit Python/config inputs."""
    repo = Path(__file__).resolve().parents[1]
    packages = Path(pytest.__file__).resolve().parents[1]
    runtime = isolated.Runtime(
        Path(sys.executable), (repo, Path(sys.prefix)), (Path(sys.executable).parent,),
    )
    sample = tmp_path / "test_sample.py"
    sample.write_text(source)
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime) as environment:
        env = environment.child_env()
        env["AQ_TEST_PG_MODE"] = mode
        args = [str(sample), "-p", "tests.conftest", "-p", "pytest_asyncio.plugin",
                "-c", str(repo / "pyproject.toml"), "-o", "addopts=", "-q"]
        if collect:
            args.append("--collect-only")
        script = (f"import sys; sys.path.insert(0, {str(packages)!r}); "
                  f"import pytest; raise SystemExit(pytest.main({args!r}))")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, cwd=repo, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 30)
            return process.returncode, output.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.parametrize("collect", [False, True])
async def test_pure_pytest_without_dsn_runs_real_root_fixtures(tmp_path, collect):
    status, output = await _pytest_probe(
        tmp_path, "def test_pure():\n    assert 2 + 2 == 4\n", collect=collect,
    )
    assert status == 0, output
    assert ("1 test collected" if collect else "1 passed") in output


@pytest.mark.parametrize("source,outcome", [
    ("def test_pg():\n    from tests.db_fixtures import lease_dsn; lease_dsn()\n", "1 failed"),
    ("def test_pg():\n    from tests.pg_dsn import ensure_worker_postgres_dsn; "
     "ensure_worker_postgres_dsn()\n", "1 failed"),
    ("def test_pg(reuse_database):\n    pass\n", "1 error"),
])
async def test_pure_pytest_rejects_accidental_pg_dependency(tmp_path, source, outcome):
    status, output = await _pytest_probe(tmp_path, source)
    assert status == 1, output
    assert "PostgreSQL dependency requested by a pure target" in output, output
    assert outcome in output and "skipped" not in output


async def test_declared_pg_target_requires_dsn_but_collection_does_not(tmp_path):
    source = "def test_pg():\n    pass\n"
    status, output = await _pytest_probe(tmp_path, source, mode="postgres")
    assert status == 4, output
    assert "POSTGRES_TEST_DSN is not set" in output
    status, output = await _pytest_probe(tmp_path, source, mode="postgres", collect=True)
    assert status == 0, output
    assert "1 test collected" in output


async def test_actual_postgres_target_starts_inside_namespace(
    tmp_path, namespace_runtime, postgres_runtime,
):
    from tools.bazel.postgres_cluster import run_postgres_target

    native_runtime, binaries = postgres_runtime
    native = binaries.postgres.parent.parent
    repo = Path(__file__).resolve().parents[1]
    runtime = isolated.Runtime(
        namespace_runtime.python, (*namespace_runtime.read_only, native, repo),
        (native / "bin", namespace_runtime.python.parent),
        library_dirs=native_runtime.library_dirs, shell=namespace_runtime.shell,
    )
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime) as environment:
        result = environment.root / "result.json"
        script = """
import json, os, pathlib, socket, subprocess
assert socket.if_nameindex() == [(1, 'lo')]
assert os.environ['AQ_TEST_PG_MODE'] == 'postgres'
assert os.environ['AQ_DB_SCOPE'] == 'test'
assert not {'AQ_SESSION_ID', 'AQ_API_TOKEN', 'AGENT_QUEUE_DB', 'AQ_DATABASE_URL'} & os.environ.keys()
dsn = os.environ['POSTGRES_TEST_DSN'].replace('+asyncpg', '')
value = subprocess.check_output([%r, '-XAt', '-d', dsn, '-c', 'SELECT version()'], text=True)
assert 'PostgreSQL 18.1' in value
pathlib.Path(%r).write_text(json.dumps({'dsn': dsn, 'version': value}))
""" % (str(binaries.psql), str(result))
        status = await run_postgres_target(
            environment, binaries, [str(runtime.python), "-I", "-c", script],
            cwd=environment.root, timeout=30,
        )
        assert status == 0
        assert "PostgreSQL 18.1" in json.loads(result.read_text())["version"]
        assert not list(environment.root.glob("pg-*"))


def test_e2e_worlds_use_factory_without_ambient_identity(tmp_path, runtime, monkeypatch):
    from scripts.e2e.environment import child_env, world_env

    poison = {
        "HOME": "/operator/home", "PATH": "/operator/bin", "GH_TOKEN": "secret",
        "AQ_API_TOKEN": "live-token", "AQ_SESSION_ID": "live-session",
        "AQ_TASK_ID": "live-task", "CLAUDE_CODE_SESSION_ID": "live-session",
        "AQ_DB_SCOPE": "worker", "AQ_DATABASE_URL": "refusal-sentinel",
        "POSTGRES_TEST_DSN": "postgresql://operator/agent_queue",
        "UNRECOGNIZED_VENDOR_CREDENTIAL": "secret",
    }
    for name, value in poison.items():
        monkeypatch.setenv(name, value)
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime) as target:
        dsn = f"postgresql://test@/aq_test_target?host={target.root}/pg&port=35432"
        first = world_env(target.root / "one", environment=target, postgres_dsn=dsn)
        second = world_env(target.root / "two", environment=target, postgres_dsn=dsn)
        for name in ("HOME", "TMPDIR", "TMUX_TMPDIR", "AQ_CONFIG_PATH", "AQ_E2E_HOME",
                     "AQ_E2E_PORT", "AQ_E2E_TMUX_SOCKET", "E2E_DB_NAME", "E2E_DB_URL"):
            assert first[name] != second[name], name
        assert first["PATH"] == str(runtime.bin_dirs[0])
        assert first["POSTGRES_TEST_DSN"] == dsn
        assert first["E2E_DB_URL"].endswith(f"?host={target.root}/pg&port=35432")
        for name, value in poison.items():
            assert first.get(name) != value
            assert os.environ[name] == value
        # The smoke runner may introduce local session auth only after this fence.
        child = child_env({**first, **{"GH_TOKEN": "secret", "AQ_API_TOKEN": "live-token"}})
        assert "GH_TOKEN" not in child and "AQ_API_TOKEN" not in child
        assert child["E2E_DB_URL"] == first["E2E_DB_URL"]
        with pytest.raises(ValueError, match="H3-owned"):
            world_env(target.root / "missing", environment=target)
        with pytest.raises(ValueError, match="belong"):
            world_env(tmp_path / "outside", environment=target, postgres_dsn=dsn)


def test_world_smoke_import_and_reset_do_not_reintroduce_parent_environment(tmp_path, monkeypatch):
    from scripts.e2e.environment import world_env
    from tests.test_e2e_cli_stateful import World

    monkeypatch.setenv("AQ_API_TOKEN", "operator-token")
    monkeypatch.setenv("AQ_SESSION_ID", "operator-session")
    env = world_env(tmp_path / "world")
    world = World("probe", env)
    world.swarm = {"enabled": True}
    observed = []

    def command(name, args):
        observed.append(world.smoke._cli_env())
        return {"sessions": [], "projects": []}

    monkeypatch.setattr(world.smoke, "api_checked", command)
    monkeypatch.setattr(world.smoke, "fake_script", lambda: None)
    monkeypatch.setattr(world.smoke, "wait_for_restored_readiness", lambda: None)
    world.reset()
    assert observed
    assert all("AQ_API_TOKEN" not in child and "AQ_SESSION_ID" not in child for child in observed)
    assert os.environ["AQ_API_TOKEN"] == "operator-token"


def test_two_concurrent_e2e_children_have_private_state(tmp_path, monkeypatch):
    import select
    import subprocess

    from scripts.e2e.environment import world_env

    monkeypatch.setenv("UNRECOGNIZED_VENDOR_CREDENTIAL", "must-not-cross")
    environments = [world_env(tmp_path / name) for name in ("first", "second")]
    code = """
import json, os, pathlib, socket
assert 'UNRECOGNIZED_VENDOR_CREDENTIAL' not in os.environ
home = pathlib.Path(os.environ['HOME'])
assert not (home / 'state').exists()
(home / 'state').write_text(os.environ['E2E_DB_NAME'])
with socket.socket() as listener:
    listener.bind(('127.0.0.1', int(os.environ['AQ_E2E_PORT'])))
    listener.listen()
    print(json.dumps({'home': str(home), 'state': (home / 'state').read_text()}), flush=True)
    input()
"""
    processes = [subprocess.Popen(
        [sys.executable, "-c", code], env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) for env in environments]
    try:
        for env, process in zip(environments, processes, strict=True):
            assert select.select([process.stdout], [], [], 10)[0], "child did not become ready"
            row = json.loads(process.stdout.readline())
            assert row == {"home": env["HOME"], "state": env["E2E_DB_NAME"]}
        for process in processes:
            _, error = process.communicate("stop\n", timeout=10)
            assert process.returncode == 0, error
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_e2e_shell_preserves_explicit_socket_database_urls(tmp_path):
    import subprocess

    from scripts.e2e.environment import world_env

    env = world_env(tmp_path / "world", postgres_dsn=(
        "postgresql://test@/aq_test_target?host=/private/socket&port=35432"
    ))
    result = subprocess.run(
        ["bash", "-c", 'source scripts/e2e-common.sh; '
         'printf "%s\\n%s\\n" "$E2E_ADMIN_DSN" "$E2E_DB_URL"'],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert result.stdout.splitlines() == [env["E2E_ADMIN_DSN"], env["E2E_DB_URL"]]


def test_e2e_reset_protects_test_database_with_unix_socket_dsn(monkeypatch):
    from scripts.e2e.dbsetup import refuse

    monkeypatch.setenv(
        "POSTGRES_TEST_DSN", "postgresql://test@/aq_e2e_target?host=/private/socket&port=35432",
    )
    assert refuse("aq_e2e_target") == "this is POSTGRES_TEST_DSN's database"
    assert refuse("aq_e2e_other_world") is None


async def test_actual_e2e_shards_use_h3_runtime(tmp_path, namespace_runtime, postgres_runtime):
    """Run all four scenario shards concurrently, each in its own PG namespace."""
    from contextlib import ExitStack
    from tools.bazel.postgres_cluster import run_postgres_target

    _, binaries = postgres_runtime
    native = binaries.postgres.parent.parent
    source = Path(__file__).resolve().parents[1]
    # Declare source inputs without exposing .aq claims, the operator checkout,
    # credentials, editable installs or the host's configuration directory.
    repo = tmp_path / "sources"
    repo.mkdir()
    for directory in ("src", "tests", "scripts", "tools", "migrations", "packages"):
        shutil.copytree(source / directory, repo / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "node_modules"))
    for name in ("pyproject.toml", "alembic.ini"):
        shutil.copy2(source / name, repo / name)
    shutil.copytree(source / "docs/history/legacy-train-policies",
                    repo / "docs/history/legacy-train-policies")
    packages = Path(pytest.__file__).resolve().parents[1]
    (namespace_runtime.python.parent / "python3").symlink_to(namespace_runtime.python.name)
    runtime = isolated.Runtime(
        namespace_runtime.python,
        (*namespace_runtime.read_only, native, repo, source / "tools", packages),
        (native / "bin", namespace_runtime.python.parent),
        library_dirs=(native / "lib",), git_exec_path=native / "libexec/git-core",
        shell=native / "bin/bash", env_executable=native / "bin/env",
    )
    python_paths = (repo, repo / "packages/aq-client", packages)
    with ExitStack() as stack:
        environments = [stack.enter_context(isolated.test_environment(
            test_tmpdir=tmp_path, runtime=runtime,
        )) for _ in range(4)]
        async def shard(environment, selector):
            result = environment.root / "e2e-result.json"
            code = f"""
import json, os, pathlib, sys
sys.path[:0] = {list(map(str, python_paths))!r}
import pytest
assert not {{'AQ_API_TOKEN', 'AQ_SESSION_ID', 'GH_TOKEN'}} & os.environ.keys()
status = pytest.main(['tests/test_e2e_cli_stateful.py', '-m', 'integration',
                 '-k', {selector!r}, '-p', 'pytest_asyncio.plugin', '-o', 'addopts=', '-q'])
pathlib.Path({str(result)!r}).write_text(json.dumps({{'status': status,
    'dsn': os.environ['POSTGRES_TEST_DSN'], 'home': os.environ['HOME']}}))
sys.exit(status)
"""
            status = await run_postgres_target(
                environment, binaries, [str(runtime.python), "-I", "-c", code],
                cwd=repo, timeout=290, python_paths=python_paths, tcp=True,
            )
            assert status == 0
            return json.loads(result.read_text())
        groups = ('claims', 'cli', 'graphs', 'failover')
        tasks = [asyncio.create_task(shard(environment, group + '-'))
                 for environment, group in zip(environments, groups, strict=True)]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert len({row['dsn'] for row in results}) == 4
        assert len({row['home'] for row in results}) == 4
        assert all(row['status'] == 0 for row in results)


def test_e2e_fixture_loads_h3_manifest_without_runtime_or_dsn_fallback(tmp_path, runtime, monkeypatch):
    from dataclasses import asdict
    from scripts.e2e.environment import world_env

    monkeypatch.setenv('AQ_TEST_PG_MODE', 'postgres')
    monkeypatch.delenv('AQ_TEST_ENVIRONMENT', raising=False)
    with pytest.raises(ValueError, match='manifest'):
        world_env(tmp_path / 'refused')
    monkeypatch.setenv('POSTGRES_TEST_DSN', 'postgresql://operator/agent_queue')
    with isolated.test_environment(test_tmpdir=tmp_path, runtime=runtime) as target:
        manifest = target.root / 'environment.json'
        dsn = f'postgresql+asyncpg://test@/aq_target_private?host={target.root}/s&port=35432'
        manifest.write_text(json.dumps({
            'root': str(target.root), 'runtime': asdict(runtime), 'env': target.child_env(),
            'python_paths': [], 'socket_alias_root': None, 'postgres_dsn': dsn,
            'tcp_dsn': 'postgresql+asyncpg://test@127.0.0.1:35432/aq_target_private',
        }, default=str))
        monkeypatch.setenv('AQ_TEST_ENVIRONMENT', str(manifest))
        env = world_env(target.root / 'world')
        assert env['POSTGRES_TEST_DSN'] == dsn
        assert env['PATH'] == str(runtime.bin_dirs[0])
        assert env['AQ_TEST_ENVIRONMENT'] == str(manifest)
        assert env['E2E_ADMIN_DSN'] == 'postgresql://test@127.0.0.1:35432/postgres'
        assert 'PYTHONPATH' not in env
        assert os.environ['POSTGRES_TEST_DSN'] == 'postgresql://operator/agent_queue'
