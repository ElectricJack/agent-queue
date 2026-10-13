"""Fail-closed Linux script boundary, called by the existing detached job runner.

No command endpoint, worker poller, database owner or ScriptStep is introduced
here. E2 calls ``launch`` after server-side principal, invocation, input/output
readiness and durable launch-intent checks. Raw streams exist only in memory or
private pipes until E2 encrypts and settles them. Public reprs omit all payloads.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import fcntl
import json
import os
import resource
import signal
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, field_validator

from src.config import ScriptsConfig
from src.playbooks.script_inputs import (
    InputStore, PinnedScript, QuotaLedger, RuntimeManifest, canonical, digest, open_beneath, read_file,
)
from src.playbooks.script_policy import FrozenModel, MIB, ScriptError, ScriptLimits, script_environment
from src.resources.box_lock import BoxLock
from src.resources.semaphore import SlotTimeout

GRACE_SECONDS = 2
PROTOCOL_BYTES = 4 * MIB
REQUEST_BYTES = MIB
POLICY_PATH = Path("/etc/agent-queue/script-sandbox.json")


class SandboxRequest(FrozenModel):
    version: Literal[1] = 1
    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    run_id: str = Field(min_length=1, max_length=128)
    pinned: PinnedScript
    arguments: tuple[str, ...] = Field(default=(), max_length=256, repr=False)
    environment: dict[str, str] = Field(default_factory=dict, repr=False)
    stdin_base64: str = Field(default="", max_length=87384, repr=False)

    @field_validator("arguments")
    @classmethod
    def _arguments(cls, values):
        if any("\0" in value for value in values) or sum(len(v.encode()) for v in values) > 32768:
            raise ValueError("script arguments exceed limits")
        return values

    @property
    def stdin(self) -> bytes:
        try:
            data = base64.b64decode(self.stdin_base64, validate=True)
        except ValueError:
            raise ScriptError("script_stdin_invalid") from None
        if len(data) > 65536:
            raise ScriptError("script_stdin_limit_exceeded")
        return data


class LauncherPolicy(FrozenModel):
    version: Literal[1] = 1
    scripts: dict
    jobs_enabled: bool = False
    daemon_uid: StrictInt = Field(ge=1)
    lock_dir: str
    capacity: StrictInt = Field(ge=1)

    def settings(self) -> ScriptsConfig:
        settings = ScriptsConfig(**self.scripts)
        if settings.validate():
            raise ScriptError("script_host_policy_invalid")
        return settings


@dataclass(frozen=True)
class SandboxResult:
    execution_id: str
    outcome: str
    exit_code: int | None = None
    reason: str | None = None
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    capture_complete: bool = False
    cleanup_complete: bool = False
    started_at: float | None = None
    ended_at: float | None = None
    cgroup: str | None = None
    quota_key: str | None = None

    def to_wire(self) -> dict:
        result = dataclasses.asdict(self)
        result["stdout"] = base64.b64encode(self.stdout).decode()
        result["stderr"] = base64.b64encode(self.stderr).decode()
        return result


def _host_file(path: Path) -> None:
    """Installation files and every ancestor are controlled only by root."""
    for current in [path, *path.parents]:
        info = current.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ScriptError("script_installation_untrusted")


def read_launcher_policy() -> LauncherPolicy:
    _host_file(POLICY_PATH)
    return LauncherPolicy.model_validate_json(read_file(POLICY_PATH.parent, POLICY_PATH.name, MIB))


def verify_runtime(settings: ScriptsConfig) -> RuntimeManifest:
    root, manifest_path = Path(settings.runtime_root), Path(settings.runtime_manifest)
    _host_file(root)
    _host_file(manifest_path)
    manifest = RuntimeManifest.model_validate_json(
        read_file(manifest_path.parent, manifest_path.name, 4 * MIB)
    )
    if sum(item.byte_count for item in manifest.files) > settings.runtime_bytes:
        raise ScriptError("script_runtime_limit_exceeded")
    expected = {item.path for item in manifest.files}
    found = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            _host_file(Path(directory) / name)
        for name in files:
            path = Path(directory) / name
            _host_file(path)
            found.add(path.relative_to(root).as_posix())
            if len(found) > len(expected):
                raise ScriptError("script_runtime_undeclared_file")
    if found != expected:
        raise ScriptError("script_runtime_files_changed")
    if not any(item.path == manifest.probe and item.executable for item in manifest.files):
        raise ScriptError("script_runtime_probe_unavailable")
    for name in ("proc", "dev", "inputs", "scratch"):
        path = root / name
        _host_file(path)
        if not path.is_dir() or any(path.iterdir()):
            raise ScriptError("script_runtime_mountpoint_invalid")
    for item in manifest.files:
        data = read_file(root, item.path, item.byte_count, immutable=True)
        info = (root / item.path).lstat()
        if (digest(data) != item.sha256 or len(data) != item.byte_count
                or bool(info.st_mode & 0o111) != item.executable or info.st_mode & 0o6000):
            raise ScriptError("script_runtime_digest_mismatch")
    return manifest


def _cgroup_files(root: Path) -> None:
    if (not root.is_absolute() or root.resolve() != root
            or not root.is_relative_to("/sys/fs/cgroup")):
        raise ScriptError("script_cgroup_invalid")
    # Root must already be delegated and controller-enabled; this function does
    # not enable delegation or move any host process on the operator's behalf.
    if not {"cpu", "memory", "pids"} <= set((root / "cgroup.subtree_control").read_text().split()):
        raise ScriptError("script_cgroup_not_delegated")
    if (root / "cgroup.procs").read_text().strip() or not os.access(root, os.W_OK):
        raise ScriptError("script_cgroup_not_delegated")
    for name in ("cgroup.kill", "cpu.stat", "cpu.max", "memory.max", "memory.swap.max",
                 "memory.oom.group", "memory.events", "pids.max", "pids.events", "cgroup.events"):
        if not (root / name).exists():
            raise ScriptError("script_cgroup_limit_unavailable")


async def prerequisites(policy: LauncherPolicy) -> dict:
    """Read-only check in the dedicated identity. No fixes or user program."""
    try:
        settings = policy.settings()
        if sys.platform != "linux":
            raise ScriptError("script_linux_required")
        if (os.getuid() != settings.execution_uid or os.geteuid() != settings.execution_uid
                or os.getgid() != settings.execution_gid or os.getuid() in (0, policy.daemon_uid)
                or any(gid != settings.execution_gid for gid in os.getgroups())):
            raise ScriptError("script_execution_identity_unavailable")
        status = Path("/proc/self/status").read_text()
        if any(int(line.split()[1], 16) for line in status.splitlines()
               if line.startswith(("CapPrm:", "CapEff:", "CapAmb:"))):
            raise ScriptError("script_execution_capabilities_present")
        if "NoNewPrivs:" not in status:
            raise ScriptError("script_no_new_privileges_unavailable")
        _host_file(Path(settings.bwrap))
        if Path(settings.bwrap).stat().st_mode & 0o6000:
            raise ScriptError("script_privileged_bwrap_unsupported")
        _host_file(Path(__file__))
        _host_file(Path(__file__).with_name("script_entry.py"))
        _host_file(Path(sys.executable).resolve())
        _cgroup_files(Path(settings.cgroup_root))
        if resource.getrlimit(resource.RLIMIT_NOFILE)[1] < settings.fds:
            raise ScriptError("script_fd_limit_unavailable")
        manifest = await asyncio.to_thread(verify_runtime, settings)
        # No environment or shared sockets are inherited by even the help probe.
        proc = await asyncio.create_subprocess_exec(
            settings.bwrap, "--help", env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, close_fds=True,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 5)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode or not all(flag in output for flag in (
            b"--disable-userns", b"--assert-userns-disabled", b"--size", b"--unshare-cgroup",
            b"--json-status-fd",
        )):
            raise ScriptError("script_namespace_support_unavailable")
        # A supported flag list alone does not prove namespaces are permitted
        # by this host. Use only the immutable operator probe, without inputs.
        proc = await asyncio.create_subprocess_exec(
            *_namespace_argv(settings, Path(settings.runtime_root) / "inputs", settings.scratch_bytes),
            "--chdir", "/scratch", "--", "/" + manifest.probe,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"}, close_fds=True,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            if await asyncio.wait_for(proc.wait(), 5):
                raise ScriptError("script_namespace_support_unavailable")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        # Access must be provisioned for this dedicated UID on the SAME lock
        # directory jobs use. Doctor never creates/chmods lock directories.
        for root in (Path(settings.state_root), Path(policy.lock_dir)):
            if not root.is_dir() or root.is_symlink() or not os.access(root, os.W_OK | os.X_OK):
                raise ScriptError("script_storage_or_lock_access_unavailable")
        for path in Path(policy.lock_dir).glob("*.lock"):
            if path.is_symlink() or not os.access(path, os.R_OK | os.W_OK):
                raise ScriptError("script_box_lock_access_unavailable")
        return {"ready": True, "runtime_sha256": manifest.sha256,
                "execution_uid": os.getuid(), "cgroup_root": settings.cgroup_root}
    except (ScriptError, OSError, ValueError, TypeError, TimeoutError) as exc:
        return {"ready": False, "reason": exc.code if isinstance(exc, ScriptError)
                else "script_prerequisite_unavailable"}


class OwnedCgroup:
    def __init__(self, root: Path, execution_id: str):
        self.path = root / execution_id

    def prepare(self, limits: ScriptLimits):
        _cgroup_files(self.path.parent)
        self.path.mkdir(mode=0o700)  # Existing identity is uncertain, never reused.
        try:
            values = {
                "memory.max": limits.memory_bytes, "memory.swap.max": 0,
                "memory.oom.group": 1, "pids.max": limits.pids,
                "cpu.max": "100000 100000",
            }
            for name, value in values.items():
                (self.path / name).write_text(str(value))
                if (self.path / name).read_text().strip() != str(value):
                    raise ScriptError("script_cgroup_limit_unavailable")
            for name in ("cgroup.kill", "cgroup.events", "cpu.stat", "memory.events", "pids.events"):
                if not (self.path / name).exists():
                    raise ScriptError("script_cgroup_limit_unavailable")
        except BaseException:
            self.path.rmdir()
            raise

    def populated(self) -> bool:
        return bool(self.counters("cgroup.events").get("populated", 1))

    def counters(self, name: str) -> dict[str, int]:
        return {key: int(value) for key, value in
                (line.split() for line in (self.path / name).read_text().splitlines())}

    def limit_hit(self, cpu_seconds: int) -> str | None:
        if self.counters("cpu.stat").get("usage_usec", 0) >= cpu_seconds * 1_000_000:
            return "cpu"
        if self.counters("memory.events").get("max", 0):
            return "memory"
        if self.counters("pids.events").get("max", 0):
            return "pids"
        return None

    async def stop(self) -> bool:
        # cgroup.procs is authoritative even for double-forked descendants;
        # namespace helpers cannot escape because cgroupfs is never mounted.
        try:
            for raw in (self.path / "cgroup.procs").read_text().split():
                try:
                    os.kill(int(raw), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + GRACE_SECONDS
            while self.populated() and time.monotonic() < deadline:
                await asyncio.sleep(0.025)
            if self.populated():
                (self.path / "cgroup.kill").write_text("1")
            deadline = time.monotonic() + GRACE_SECONDS
            while self.populated() and time.monotonic() < deadline:
                await asyncio.sleep(0.025)
            return not self.populated()
        except OSError:
            return False


def _namespace_argv(settings: ScriptsConfig, inputs: Path, scratch_bytes: int) -> list[str]:
    return [settings.bwrap, "--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc",
            "--unshare-uts", "--unshare-cgroup", "--disable-userns", "--assert-userns-disabled",
            "--cap-drop", "ALL", "--die-with-parent", "--new-session", "--clearenv",
            "--ro-bind", settings.runtime_root, "/", "--proc", "/proc", "--dev", "/dev",
            "--ro-bind", str(Path(settings.runtime_root) / "inputs"), "/dev/shm",
            "--remount-ro", "/dev",
            "--ro-bind", str(inputs), "/inputs", "--size", str(scratch_bytes),
            "--tmpfs", "/scratch"]


def _sandbox_argv(settings: ScriptsConfig, request: SandboxRequest, inputs: Path) -> list[str]:
    program = request.pinned.program
    argv = [*_namespace_argv(settings, inputs, program.limits.scratch_bytes),
            "--chdir", "/" + program.cwd]
    environment = script_environment(program.env_allowlist, tuple(settings.env_allowlist),
                                     request.environment)
    for key, value in environment.items():
        argv.extend(["--setenv", key, value])
    argv.extend(["--", "/" + program.executable])
    if program.script is not None:
        argv.append("/inputs/" + program.script)
    elif program.shell is not None:
        # The positional $0 sentinel keeps the first dynamic value in $1. Never
        # interpolate data into the reviewed -c program, even for shell mode.
        argv.extend(["-c", program.shell, "aq-script"])
    return [*argv, *request.arguments]


async def _admit_request(policy: LauncherPolicy, request: SandboxRequest):
    """All failures here precede creation of any user process."""
    settings = policy.settings()
    if not policy.jobs_enabled or not settings.enabled:
        return SandboxResult(request.execution_id, "admission_denied", reason="script_disabled")
    ready = await prerequisites(policy)
    if not ready["ready"]:
        return SandboxResult(request.execution_id, "admission_denied", reason=ready["reason"])
    pinned, limits = request.pinned, request.pinned.program.limits
    limits.narrow(ScriptLimits(**{key: getattr(settings, key) for key in ScriptLimits.model_fields}))
    if pinned.runtime_sha256 != ready["runtime_sha256"]:
        raise ScriptError("script_runtime_digest_mismatch")
    manifest = await asyncio.to_thread(verify_runtime, settings)
    executable = next((item for item in manifest.files if item.path == pinned.program.executable), None)
    if (executable is None or not executable.executable
            or executable.sha256 != pinned.executable_sha256):
        raise ScriptError("script_executable_digest_mismatch")
    script_environment(pinned.program.env_allowlist, tuple(settings.env_allowlist), request.environment)
    stdin = request.stdin
    count = sum(item.byte_count for item in pinned.inputs)
    if count > settings.input_bytes:
        raise ScriptError("script_input_limit_exceeded")
    key = digest(canonical({"execution_id": request.execution_id, "run_id": request.run_id}))
    quota = QuotaLedger(Path(settings.state_root) / "quotas", input_bytes=settings.input_storage_bytes,
                        output_bytes=settings.output_storage_bytes,
                        per_run_bytes=settings.per_run_storage_bytes)
    dynamic_bytes = len(stdin) + sum(len(arg.encode()) for arg in request.arguments)
    dynamic_bytes += len(canonical(request.environment))
    await asyncio.to_thread(quota.reserve, key, request.run_id, input_bytes=count + dynamic_bytes,
                            output_bytes=limits.stdout_bytes + limits.stderr_bytes)
    root = Path(settings.state_root)
    run_lock = open_beneath(root, "run-" + digest(request.run_id.encode()) + ".lock",
                           flags=os.O_RDWR | os.O_CREAT)
    return settings, stdin, key, run_lock


async def execute_in_launcher(
    policy: LauncherPolicy, request: SandboxRequest, cancelled: asyncio.Event,
) -> SandboxResult:
    """One launch, mandatory shared admission, separate bounded drains and cleanup."""
    try:
        admission = await _admit_request(policy, request)
    except (OSError, ValueError, TypeError) as exc:
        return SandboxResult(request.execution_id, "admission_denied", cleanup_complete=True,
                             reason=exc.code if isinstance(exc, ScriptError)
                             else "script_request_or_prerequisite_invalid")
    if isinstance(admission, SandboxResult):
        return admission
    settings, stdin, key, run_lock = admission
    pinned, limits = request.pinned, request.pinned.program.limits
    root = Path(settings.state_root)
    cm = None
    group = OwnedCgroup(Path(settings.cgroup_root), request.execution_id)
    prepared, process, started = False, None, None
    status_fd = None
    cause, reason, clean = "succeeded", None, False
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    counts = {"stdout": 0, "stderr": 0}
    tasks = []
    deadline = time.monotonic() + settings.queue_seconds
    try:
        while True:
            if cancelled.is_set():
                return SandboxResult(request.execution_id, "cancelled", cleanup_complete=True,
                                     quota_key=key)
            if time.monotonic() >= deadline:
                return SandboxResult(request.execution_id, "admission_timed_out",
                                     cleanup_complete=True, quota_key=key)
            try:
                fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)

        def waiting(*_):
            if cancelled.is_set():
                raise InterruptedError

        cm = BoxLock(policy.lock_dir, policy.capacity).acquire(
            exclusive=False, weight=1, timeout=max(0, deadline - time.monotonic()), poll=0.05,
            meta={"run_id": request.run_id, "execution_id": request.execution_id,
                  "priority_band": 2}, on_wait=waiting,
        )
        await asyncio.to_thread(cm.__enter__)
        if cancelled.is_set():
            raise InterruptedError
        intent = open_beneath(root, "intent-" + request.execution_id,
                              flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        os.close(intent)  # Retain forever until E2 settles/retains this identity.
        await asyncio.to_thread(group.prepare, limits)
        prepared = True
        with tempfile.TemporaryDirectory(dir=root, prefix="inputs-") as temporary:
            staged = Path(temporary)
            store = InputStore(Path(settings.input_root))
            for item in pinned.inputs:
                data = await asyncio.to_thread(store.read, item.sha256, item.byte_count)
                path = staged / item.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o444)
            status_fd = os.memfd_create("aq-script-status", flags=os.MFD_CLOEXEC)
            argv = _sandbox_argv(settings, request, staged)
            argv[1:1] = ["--json-status-fd", str(status_fd)]
            entry = {"argv": argv, "cgroup": str(group.path), "fds": limits.fds,
                     "scratch": limits.scratch_bytes, "status_fd": status_fd,
                     "env": {"PATH": "/usr/bin:/bin", "LANG": "C"}}
            entry_fd = os.memfd_create("aq-script-entry", flags=os.MFD_CLOEXEC)
            try:
                os.write(entry_fd, canonical(entry))
                os.lseek(entry_fd, 0, os.SEEK_SET)
                started = time.time()
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", str(Path(__file__).with_name("script_entry.py")),
                    str(entry_fd), env={"PATH": "/usr/bin:/bin", "LANG": "C"}, cwd="/",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, close_fds=True, pass_fds=(entry_fd, status_fd),
                    start_new_session=True,
                )
            finally:
                os.close(entry_fd)

            def stop_for(outcome, detail):
                nonlocal cause, reason
                if cause == "succeeded":
                    cause, reason = outcome, detail

            async def drain(name, stream, ceiling):
                while chunk := await stream.read(65536):
                    counts[name] += len(chunk)
                    buffers[name].extend(chunk[:max(0, ceiling - len(buffers[name]))])
                    if counts[name] > ceiling:
                        stop_for("output_limit_exceeded", name)

            async def supply():
                try:
                    process.stdin.write(stdin)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()

            tasks = [asyncio.create_task(drain("stdout", process.stdout, limits.stdout_bytes)),
                     asyncio.create_task(drain("stderr", process.stderr, limits.stderr_bytes)),
                     asyncio.create_task(supply())]
            end = time.monotonic() + limits.wall_seconds
            waiter = asyncio.create_task(process.wait())
            while not waiter.done():
                if cancelled.is_set():
                    stop_for("cancelled", "cancelled")
                elif time.monotonic() >= end:
                    stop_for("timed_out", "wall")
                else:
                    hit = await asyncio.to_thread(group.limit_hit, limits.cpu_seconds)
                    if hit:
                        stop_for("resource_limit_exceeded", hit)
                if cause != "succeeded":
                    clean = await group.stop()
                    # The shim may still be running just before enrollment.
                    if process.returncode is None:
                        process.terminate()
                    break
                await asyncio.wait({waiter}, timeout=0.02)
            try:
                await asyncio.wait_for(asyncio.shield(waiter), GRACE_SECONDS + 1)
            except TimeoutError:
                process.kill()
                await waiter
            clean = await group.stop()
            await asyncio.wait_for(asyncio.gather(*tasks), GRACE_SECONDS + 1)
            status = [json.loads(line) for line in os.pread(status_fd, 65536, 0).splitlines()]
            hit = await asyncio.to_thread(group.limit_hit, limits.cpu_seconds)
            if hit:
                stop_for("resource_limit_exceeded", hit)
            if not any("exit-code" in record for record in status):
                stop_for("admission_denied", "script_namespace_setup_failed")
            if process.returncode in (-signal.SIGXFSZ, 128 + signal.SIGXFSZ):
                stop_for("resource_limit_exceeded", "scratch")
            elif process.returncode:
                stop_for("nonzero_exit", "exit")
    except SlotTimeout:
        cause, reason, clean = "admission_timed_out", "queue", True
    except InterruptedError:
        cause, reason, clean = "cancelled", "queue", True
    except (OSError, ValueError, RuntimeError, TimeoutError):
        cause, reason = "admission_denied" if process is None else "runtime_error", "script_backend_failed"
    finally:
        if prepared:
            clean = await group.stop()
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if status_fd is not None:
            os.close(status_fd)
        if prepared and clean:
            try:
                group.path.rmdir()
            except OSError:
                clean = False
        if cm is not None:
            await asyncio.to_thread(cm.__exit__, None, None, None)
        os.close(run_lock)
    if prepared and not clean:
        cause, reason = "runtime_error", "script_cleanup_uncertain"
    if not prepared and process is None:
        clean = True
    return SandboxResult(
        request.execution_id, cause, exit_code=process.returncode if process else None, reason=reason,
        stdout=bytes(buffers["stdout"]), stderr=bytes(buffers["stderr"]),
        stdout_bytes=counts["stdout"], stderr_bytes=counts["stderr"],
        capture_complete=clean and cause in {"succeeded", "nonzero_exit"}, cleanup_complete=clean,
        started_at=started, ended_at=time.time(), cgroup=str(group.path) if prepared else None,
        quota_key=key,
    )


async def probe_launcher(settings: ScriptsConfig) -> dict:
    """Daemon-side, read-only doctor transport; never starts the service for it."""
    try:
        _host_file(Path(settings.launcher))
        proc = await asyncio.create_subprocess_exec(
            settings.launcher, "--probe", env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, close_fds=True,
        )
        try:
            async with asyncio.timeout(30):
                data = await _read_bounded(proc.stdout, 65536)
                if await proc.wait() != 0:
                    raise ScriptError("script_launcher_probe_failed")
                result = json.loads(data)
                if not isinstance(result, dict) or type(result.get("ready")) is not bool:
                    raise ScriptError("script_launcher_protocol_invalid")
                return result
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    except (OSError, ValueError, TimeoutError):
        return {"ready": False, "reason": "script_launcher_unavailable"}


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    data = bytearray()
    while chunk := await stream.read(min(65536, limit + 1 - len(data))):
        data.extend(chunk)
        if len(data) > limit:
            raise ScriptError("script_launcher_protocol_invalid")
    return bytes(data)


async def launch(
    settings: ScriptsConfig, request: SandboxRequest, *, jobs_enabled: bool,
    cancelled: asyncio.Event,
) -> SandboxResult:
    """Private transport seam for E2; never persist or log the wire response."""
    if not settings.enabled or not jobs_enabled:
        return SandboxResult(request.execution_id, "admission_denied", reason="script_disabled")
    if settings.validate():
        return SandboxResult(request.execution_id, "admission_denied", reason="script_host_policy_invalid")
    payload = request.model_dump_json().encode() + b"\n"
    if len(payload) > REQUEST_BYTES:
        return SandboxResult(request.execution_id, "admission_denied", reason="script_request_too_large")
    try:
        _host_file(Path(settings.launcher))
        child = await asyncio.create_subprocess_exec(
            settings.launcher, env={"PATH": "/usr/bin:/bin", "LANG": "C"}, cwd="/",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, close_fds=True,
        )
    except (ScriptError, OSError):
        return SandboxResult(request.execution_id, "admission_denied", reason="script_launcher_unavailable")
    try:
        child.stdin.write(payload)
        await child.stdin.drain()
        reader = asyncio.create_task(_read_bounded(child.stdout, PROTOCOL_BYTES))
        cancel_wait = asyncio.create_task(cancelled.wait())
        async with asyncio.timeout(settings.queue_seconds + settings.wall_seconds + 30):
            done, _ = await asyncio.wait({reader, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait in done:
                child.stdin.write(b'{"cancel":true}\n')
                await child.stdin.drain()
            data = await reader
            if await child.wait() != 0:
                raise ScriptError("script_launcher_protocol_invalid")
        body = json.loads(data)
        if not isinstance(body, dict) or body["execution_id"] != request.execution_id:
            raise ScriptError("script_launcher_protocol_invalid")
        for name in ("stdout", "stderr"):
            body[name] = base64.b64decode(body[name], validate=True)
            if len(body[name]) > getattr(request.pinned.program.limits, name + "_bytes"):
                raise ScriptError("script_launcher_protocol_invalid")
            if type(body[name + "_bytes"]) is not int or body[name + "_bytes"] < len(body[name]):
                raise ScriptError("script_launcher_protocol_invalid")
        if (body["outcome"] not in {"succeeded", "nonzero_exit", "cancelled", "timed_out",
                                   "output_limit_exceeded", "resource_limit_exceeded",
                                   "admission_denied", "admission_timed_out", "runtime_error"}
                or any(type(body[name]) is not bool for name in
                       ("capture_complete", "cleanup_complete"))
                or (body["outcome"] == "succeeded"
                    and not (body["capture_complete"] and body["cleanup_complete"]))):
            raise ScriptError("script_launcher_protocol_invalid")
        return SandboxResult(**body)
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        return SandboxResult(request.execution_id, "runtime_error", reason="script_cleanup_uncertain")
    finally:
        child.stdin.close()  # EOF is a cancellation signal if the transport fails.
        pending = [task for task in (locals().get("reader"), locals().get("cancel_wait"))
                   if task is not None]
        for task in pending:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Give the launcher time for TERM, KILL and cgroup-empty verification.
        # A broken transport remains cleanup-uncertain even if this wait ends.
        async def discard():
            while await child.stdout.read(65536):
                pass

        # Continue draining without retention after an invalid/overflowed
        # response; otherwise a full stdout pipe can block even process.wait().
        remainder = asyncio.create_task(discard())
        try:
            await asyncio.wait_for(child.wait(), 2 * GRACE_SECONDS + 5)
        except TimeoutError:
            if child.returncode is None:
                child.kill()
            await asyncio.wait_for(child.wait(), GRACE_SECONDS + 1)
        finally:
            remainder.cancel()
            await asyncio.gather(remainder, return_exceptions=True)
