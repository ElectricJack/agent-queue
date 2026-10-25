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
