"""Credential-free child environments and fail-closed Linux test isolation.

B1's runner supplies absolute declared runtime/runfiles paths; H3/H4 reuse
``TestEnvironment.child_env`` for disposable PG/daemon children. This module
never changes the parent environment or imports production ``scrub_env``.
The standalone helper is launched with the declared Python in isolated mode.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
import fcntl
import json
import math
import os
import re
import signal
import socket
import struct
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

# Enumerated Bazel protocol, deliberately not TEST_*/RUNFILES_* prefix copying.
BAZEL_VARIABLES = frozenset({
    "TEST_TARGET", "TEST_WORKSPACE", "TEST_BINARY", "TEST_SIZE", "TEST_TIMEOUT",
    "TEST_RANDOM_SEED", "TEST_SHARD_INDEX", "TEST_TOTAL_SHARDS", "TEST_RUN_NUMBER",
    "TEST_SHARD_STATUS_FILE", "XML_OUTPUT_FILE", "TEST_UNDECLARED_OUTPUTS_DIR",
    "TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR", "TEST_WARNINGS_OUTPUT_FILE",
    "TEST_LOGSPLITTER_OUTPUT_FILE", "TEST_PREMATURE_EXIT_FILE", "TEST_INFRASTRUCTURE_FAILURE_FILE",
    "RUNFILES_DIR", "RUNFILES_MANIFEST_FILE", "JAVA_RUNFILES", "PYTHON_RUNFILES", "TEST_SRCDIR",
})
_RUNFILES = frozenset({
    "RUNFILES_DIR", "RUNFILES_MANIFEST_FILE", "JAVA_RUNFILES", "PYTHON_RUNFILES", "TEST_SRCDIR",
})
_OUTPUTS = frozenset({
    "TEST_SHARD_STATUS_FILE", "XML_OUTPUT_FILE", "TEST_UNDECLARED_OUTPUTS_DIR",
    "TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR", "TEST_WARNINGS_OUTPUT_FILE",
    "TEST_LOGSPLITTER_OUTPUT_FILE", "TEST_PREMATURE_EXIT_FILE", "TEST_INFRASTRUCTURE_FAILURE_FILE",
})
# Explicit target settings cannot override the boundary or inject interpreter
# startup hooks. Disposable authentication/DSNs are created *inside* the target.
_RESERVED_PREFIXES = (
    "AQ_", "AGENT_QUEUE_", "GIT_", "TMUX", "XDG_", "PYTHON", "PYTEST", "LD_", "DYLD_",
    "BASH", "ENV", "SHELLOPTS", "SSH_", "DOCKER_", "CONTAINER_", "TEST_", "RUNFILES_",
    "CLAUDE", "CODEX", "ANTHROPIC", "OPENAI", "GOOGLE", "GEMINI", "AWS_", "AZURE_",
)
_SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL|AUTH|DSN|SESSION")


class IsolationError(RuntimeError):
    """The declared isolation contract could not be established; never fallback."""


def _absolute(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or ":" in str(path):
        raise IsolationError("declared paths must be absolute, normalized and colon-free")
    return path


def _within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


@dataclass(frozen=True)
class Runtime:
    """Complete declared runtime closure; no ambient executable/library discovery.

    Include the Python standard library, native shared libraries and ELF loader
    in read_only, as well as runfiles and all symlink destinations they use.
    Paths retain their absolute spelling inside the namespace.
    """

    python: Path
    read_only: tuple[Path, ...]
    bin_dirs: tuple[Path, ...]
    library_dirs: tuple[Path, ...] = ()
    git_exec_path: Path | None = None
    shell: Path | None = None
    env_executable: Path | None = None

    def validate(self) -> None:
        if not self.bin_dirs:
            raise IsolationError("a declared toolchain PATH is required")
        for root in self.read_only:
            root = _absolute(root)
            if root == Path("/") or not root.exists():
                raise IsolationError("invalid declared input root")
        paths = (self.python, *self.bin_dirs, *self.library_dirs)
        if self.git_exec_path is not None:
            paths += (self.git_exec_path,)
        if self.shell is not None:
            paths += (self.shell,)
        if self.env_executable is not None:
            paths += (self.env_executable,)
        for path in paths:
            if not _within(_absolute(path), self.read_only) or not path.exists():
                raise IsolationError("runtime path is missing from declared inputs")
        if not self.python.is_file() or not os.access(self.python, os.X_OK):
            raise IsolationError("declared Python is not executable")


@dataclass(frozen=True)
class TestEnvironment:
    __test__ = False
    root: Path
    runtime: Runtime
    environ: dict[str, str]
    writable: tuple[Path, ...]

    def child_env(self, settings: Mapping[str, str] | None = None) -> dict[str, str]:
        """Fresh copy for children, with only explicitly declared safe settings.

        Local service credentials/DSNs are added by H3/H4 after creation inside
        the isolated target; they must never be sourced from the caller's env.
        """
        result = dict(self.environ)
        for name, value in (settings or {}).items():
            if (not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
                    or name in result or name in {"HOME", "PATH", "TMPDIR", "TMP", "TEMP"}
                    or name.startswith(_RESERVED_PREFIXES) or _SECRET_NAME.search(name)
                    or "://" in value or "\0" in value):
                raise IsolationError(f"reserved or unsafe target setting: {name}")
            result[name] = value
        return result


@contextmanager
def test_environment(
    *,
    test_tmpdir: Path,
    runtime: Runtime,
    bazel: Mapping[str, str] | None = None,
    output_roots: Sequence[Path] = (),
    settings: Mapping[str, str] | None = None,
):
    """Allocate a private home/config/tmp tree under the supplied TEST_TMPDIR.

    ``bazel`` is an explicitly selected mapping, not an inherited environment.
    Output paths must belong to separately declared writable Bazel output roots.
    No host temporary/config directory is implicitly mounted into the child.
    """
    test_tmpdir = _absolute(test_tmpdir)
    if not test_tmpdir.is_dir():
        raise IsolationError("TEST_TMPDIR must be an existing directory")
    with tempfile.TemporaryDirectory(prefix="aq-test-", dir=test_tmpdir) as temporary:
        yield create_test_environment(
            root=Path(temporary), runtime=runtime, bazel=bazel,
            output_roots=output_roots, settings=settings,
        )


def create_test_environment(
    *,
    root: Path,
    runtime: Runtime,
    bazel: Mapping[str, str] | None = None,
    output_roots: Sequence[Path] = (),
    settings: Mapping[str, str] | None = None,
) -> TestEnvironment:
    """Populate an empty caller-owned directory using the H2 positive allowlist.

    The caller owns its lifetime. E2E worlds use this when a daemon must survive
    several independent script calls; ordinary targets use ``test_environment``.
    """
    runtime.validate()
    root = _absolute(root)
    if not root.is_dir() or any(root.iterdir()):
        raise IsolationError("environment root must be an empty private directory")
    root.chmod(0o700)
    outputs = tuple(_absolute(Path(p)) for p in output_roots)
    for path in outputs:
        if not path.is_dir() or path == Path("/"):
            raise IsolationError("output roots must be existing private directories")
    names = {
        "HOME": "home", "XDG_CONFIG_HOME": "config", "XDG_CACHE_HOME": "cache",
        "XDG_DATA_HOME": "data", "XDG_STATE_HOME": "state", "XDG_RUNTIME_DIR": "run",
        "TMPDIR": "tmp", "TMUX_TMPDIR": "tmux", "AQ_E2E_HOME": "e2e",
    }
    env = {key: str(root / directory) for key, directory in names.items()}
    for value in env.values():
        Path(value).mkdir(mode=0o700)
    config = root / "config" / "aq.yaml"
    config.write_text("{}\n", encoding="utf-8")
    config.chmod(0o600)
    env.update({
        "TEST_TMPDIR": str(root), "TMP": env["TMPDIR"], "TEMP": env["TMPDIR"],
        "AQ_CONFIG_PATH": str(config), "PATH": os.pathsep.join(map(str, runtime.bin_dirs)),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    if runtime.library_dirs:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(map(str, runtime.library_dirs))
    if runtime.git_exec_path is not None:
        env["GIT_EXEC_PATH"] = str(runtime.git_exec_path)
    if runtime.shell is not None:
        env["SHELL"] = str(runtime.shell)
    for name, value in (bazel or {}).items():
        if name not in BAZEL_VARIABLES or "\0" in value:
            raise IsolationError(f"undeclared Bazel variable: {name}")
        if name in _RUNFILES and not _within(_absolute(Path(value)), runtime.read_only):
            raise IsolationError("runfiles path is outside declared inputs")
        if name in _OUTPUTS and not _within(_absolute(Path(value)), (root, *outputs)):
            raise IsolationError("Bazel output path is outside declared output roots")
        env[name] = value
    environment = TestEnvironment(root, runtime, env, (root, *outputs))
    # Prevent an input mount from exposing the parent TMPDIR or shadowing
    # the private state (and vice versa). Sibling targets share no writes.
    roots = (*runtime.read_only, *environment.writable)
    for i, first in enumerate(roots):
        for second in roots[i + 1:]:
            if _within(first, (second,)) or _within(second, (first,)):
                raise IsolationError("declared input/output roots must not overlap")
    return TestEnvironment(root, runtime, environment.child_env(settings), environment.writable)


def _syscall(name: str, *args) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if getattr(libc, name)(*args) == -1:
        raise OSError(ctypes.get_errno(), f"{name} failed")


def _mount(
    source: str | None, target: Path, kind: str | None, flags: int, data: str | None = None,
) -> None:
    _syscall("mount", source.encode() if source else None, os.fsencode(target),
             kind.encode() if kind else None, ctypes.c_ulong(flags),
             data.encode() if data else None)


def _bind(source: Path, target: Path, *, readonly: bool) -> None:
    # Nonrecursive bind: nested host mounts are never imported into the target.
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        target.mkdir(exist_ok=True)
    else:
        target.touch()
    _mount(str(source), target, None, 4096)  # MS_BIND
    _mount(None, target, None, 4096 | 32 | 2 | (1 if readonly else 0))  # REMOUNT,NOSUID,RO


def _network_self_test() -> None:
    """Bring up the sole loopback device; prove local TCP and absent external routes."""
    if socket.if_nameindex() != [(1, "lo")]:
        raise IsolationError("network namespace contains an external interface")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        request = struct.pack("16sH22x", b"lo", 1)  # IFF_UP
        fcntl.ioctl(control, 0x8914, request)  # SIOCSIFFLAGS
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=1), listener.accept()[0]:
            pass
    with socket.socket() as external:
        external.settimeout(1)
        result = external.connect_ex(("192.0.2.1", 443))
        if result not in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
            raise IsolationError("external network isolation self-test failed")


def _enter_namespaces(config: dict) -> int:
    """Return child PID in the outer helper, or zero in private PID 1."""
    uid, gid = os.getuid(), os.getgid()
    os.unshare(os.CLONE_NEWUSER)
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"1000 {uid} 1\n")
    Path("/proc/self/gid_map").write_text(f"1000 {gid} 1\n")
    os.unshare(os.CLONE_NEWNS | os.CLONE_NEWNET | os.CLONE_NEWPID | os.CLONE_NEWIPC)
    _mount(None, Path("/"), None, (1 << 18) | 16384)  # MS_PRIVATE|MS_REC
    _network_self_test()
    jail = Path(config["jail"])
    for source in config["read_only"]:
        _bind(Path(source), jail / source.lstrip("/"), readonly=True)
    for source in config["writable"]:
        _bind(Path(source), jail / source.lstrip("/"), readonly=False)
    for device in ("null", "zero", "random", "urandom"):
        _bind(Path("/dev") / device, jail / "dev" / device, readonly=False)
    if config["shell"]:
        _bind(Path(config["shell"]), jail / "bin" / "sh", readonly=True)
    if config["env_executable"]:
        _bind(Path(config["env_executable"]), jail / "usr" / "bin" / "env", readonly=True)
    # Native PG/git/tmux need an account lookup and private PTYs. Generate these
    # inputs rather than exposing the host's passwd, NSS, hosts or terminal state.
    etc = jail / "etc"
    etc.mkdir()
    (etc / "passwd").write_text(f"test:x:1000:1000:Test:{config['env']['HOME']}:/bin/false\n")
    (etc / "group").write_text("test:x:1000:\n")
    (etc / "nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files\n")
    (etc / "hosts").write_text("127.0.0.1 localhost\n::1 localhost\n")
    (jail / "dev" / "pts").mkdir()
    _mount("devpts", jail / "dev" / "pts", "devpts", 2 | 8,
           "newinstance,ptmxmode=0666,mode=0600")
    (jail / "dev" / "ptmx").symlink_to("pts/ptmx")
    (jail / "dev" / "shm").mkdir(mode=0o700)
    for name, destination in (("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"),
                              ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2")):
        (jail / "dev" / name).symlink_to(destination)
    (jail / "proc").mkdir()
    (jail / "tmp").mkdir(exist_ok=True, mode=0o700)
    # Install before fork so termination during setup cannot strand namespace init.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(128 + signal.SIGINT))
    child = os.fork()
    if child:
        return child
    # Closing the helper kills PID 1, and the kernel kills *all* its descendants,
    # including setsid/double-fork daemons. The pipe also closes the fork/prctl race.
    _syscall("prctl", 1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG
    os.close(config["parent_alive_write_fd"])
    if os.read(config["parent_alive_fd"], 1) != b"1":
        os._exit(125)
    os.close(config["parent_alive_fd"])
    _mount("proc", jail / "proc", "proc", 2 | 4 | 8)  # NOSUID|NODEV|NOEXEC
    os.chroot(jail)
    os.chdir(config["cwd"])
    os.umask(0o077)
    _syscall("prctl", 38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS
    # Mount setup is finished. PID 1 must have the same unprivileged capability
    # set as its children, including for the daemon's same-user /proc proofs.
    header = (ctypes.c_uint32 * 2)(0x20080522, 0)  # LINUX_CAPABILITY_VERSION_3
    capabilities = (ctypes.c_uint32 * 6)()
    _syscall("capset", ctypes.byref(header), ctypes.byref(capabilities))
    return 0


async def _stop(process: asyncio.subprocess.Process, grace: float) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _target(config: dict) -> int:
    loop = asyncio.get_running_loop()
    interrupted = loop.create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: (
            interrupted.set_result(s) if not interrupted.done() else None))
    process = await asyncio.create_subprocess_exec(
        *config["command"], env=config["env"], close_fds=True,
    )
    finished = asyncio.create_task(process.wait())
    try:
        done, _ = await asyncio.wait((finished, interrupted), return_when=asyncio.FIRST_COMPLETED)
        if interrupted in done:
            sig = interrupted.result()
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass
            await _stop(process, config["grace"])
            return 128 + sig
        return finished.result()
    finally:
        finished.cancel()
        interrupted.cancel()


def _helper(config: dict) -> int:
    read_fd, write_fd = os.pipe()
    config["parent_alive_fd"] = read_fd
    config["parent_alive_write_fd"] = write_fd
    try:
        child = _enter_namespaces(config)
    except BaseException:
        for descriptor in (read_fd, write_fd):
            with suppress(OSError):
                os.close(descriptor)
        raise
    if child:
        os.close(read_fd)
        os.write(write_fd, b"1")
        os.close(write_fd)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda s, _frame: os.kill(child, s))
        try:
            _, status = os.waitpid(child, 0)
            result = os.waitstatus_to_exitcode(status)
            return result if result >= 0 else 128 - result
        except BaseException:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child, 0)
            raise
    try:
        result = asyncio.run(_target(config))
    except BaseException:
        # Never unwind to outer launcher code from private PID 1.
        import traceback
        traceback.print_exc()
        result = 125
    os._exit(result if result >= 0 else 128 - result)


async def run_isolated(
    environment: TestEnvironment,
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 290,
    grace: float = 2,
) -> int:
    """Run one target; return exit status (124 deadline, 125 isolation failure).

    The deadline includes namespace setup and execution, with at most two grace
    periods reserved for teardown. Caller owns shared-box resource admission.
    Cancellation terminates and reaps the helper before releasing temporary state.
    All target output streams directly to the caller; no unbounded capture.
    """
    if sys.platform != "linux":
        raise IsolationError("Linux namespaces required; select the legacy backend explicitly")
    if (not math.isfinite(timeout) or not math.isfinite(grace)
            or timeout <= 0 or grace <= 0 or timeout + 2 * grace > 300):
        raise IsolationError("target setup/execution/cleanup budget must fit 300 seconds")
    roots = (*environment.runtime.read_only, *environment.writable)
    if not command or not _within(_absolute(Path(command[0])), roots):
        raise IsolationError("target executable must be a declared absolute path")
    if not _within(_absolute(cwd), roots):
        raise IsolationError("working directory must be declared")
    # Sibling scratch is never exposed to the target, including the launch plan.
    with tempfile.TemporaryDirectory(prefix="aq-launch-", dir=environment.root.parent) as temporary:
        private = Path(temporary)
        jail = private / "root"
        jail.mkdir(mode=0o700)
        config = {
            "jail": str(jail), "cwd": str(cwd), "command": list(command),
            "env": environment.child_env(), "grace": grace,
            "read_only": list(map(str, environment.runtime.read_only)),
            "writable": list(map(str, environment.writable)),
            "shell": str(environment.runtime.shell) if environment.runtime.shell else None,
            "env_executable": str(environment.runtime.env_executable)
            if environment.runtime.env_executable else None,
        }
        plan = private / "launch.json"
        plan.write_text(json.dumps(config), encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            str(environment.runtime.python), "-I", str(Path(__file__).resolve()),
            "--namespace-init", str(plan), env=environment.child_env(), close_fds=True,
            start_new_session=True, stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            result = await asyncio.wait_for(process.wait(), timeout=timeout)
            return result if result >= 0 else 128 - result
        except TimeoutError:
            await _stop(process, 2 * grace)
            return 124
        except BaseException:
            await _stop(process, 2 * grace)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace-init", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.namespace_init.read_text(encoding="utf-8"))
        return _helper(config)
    except (OSError, IsolationError) as exc:
        # Do not print env, arguments, configuration contents or credential values.
        print(f"hermetic isolation unavailable ({type(exc).__name__}); "
              "target was not admitted; use an isolation-capable Linux runner", file=sys.stderr)
        return 125


if __name__ == "__main__":
    sys.exit(main())
