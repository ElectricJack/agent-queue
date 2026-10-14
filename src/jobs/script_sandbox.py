"""Ordinary script processes with bounded capture (historical module name).

E2 calls ``launch`` after principal, invocation, protected-output readiness and
persistent launch-intent checks. E2 owns job recovery and encrypted storage; E3
owns ScriptStep dispatch. This backend provides no filesystem/network isolation.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import fcntl
import json
import os
import signal
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from src.config import ScriptsConfig
from src.playbooks.script_inputs import (
    InputStore, PinnedScript, QuotaLedger, RuntimeManifest, canonical, digest, open_beneath, read_file,
)
from src.playbooks.script_policy import FrozenModel, MIB, ScriptError, ScriptLimits, script_environment
from src.resources.box_lock import BoxLock
from src.resources.semaphore import SlotTimeout

GRACE_SECONDS = 2
BINDING_BYTES = 262144
REQUEST_BYTES = MIB


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


@dataclass(frozen=True)
class SandboxResult:
    execution_id: str
    outcome: str
    exit_code: int | None = None
    reason: str | None = None
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    stdout_json: object = field(default=None, repr=False)
    json_status: str = "not_requested"
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    capture_complete: bool = False
    cleanup_complete: bool = False
    started_at: float | None = None
    ended_at: float | None = None
    quota_key: str | None = None

    def binding_value(self) -> dict:
        """Private binding only; E2 must protect it and attach output references.

        Full streams remain on this result even when omitted from the binding.
        Incomplete capture never supplies a prefix as a successful value.
        """
        value = {
            "execution_id": self.execution_id, "outcome": self.outcome,
            "exit_code": self.exit_code if self.exit_code is None or self.exit_code >= 0 else None,
            "signal": -self.exit_code if self.exit_code is not None and self.exit_code < 0 else None,
            "reason": self.reason, "stdout": None, "stderr": None,
            "stdout_json": self.stdout_json, "json_status": self.json_status,
            "stdout_bytes": self.stdout_bytes, "stderr_bytes": self.stderr_bytes,
            "stdout_sha256": digest(self.stdout), "stderr_sha256": digest(self.stderr),
            "capture_complete": self.capture_complete, "cleanup_complete": self.cleanup_complete,
            "inline_omitted": True, "started_at": self.started_at, "ended_at": self.ended_at,
        }
        if self.capture_complete:
            inline = {**value, "stdout": self.stdout.decode("utf-8", errors="replace"),
                      "stderr": self.stderr.decode("utf-8", errors="replace"), "inline_omitted": False}
            if len(canonical(inline)) <= BINDING_BYTES - 4096:
                return inline
        return value


def _parse_output(result: SandboxResult, request: SandboxRequest) -> SandboxResult:
    program = request.pinned.program
    if program.output != "json" or not result.capture_complete:
        return result

    def reject_constant(_):
        raise ValueError("non-finite JSON")

    try:
        value = json.loads(result.stdout.decode("utf-8"), parse_constant=reject_constant)
        canonical(value)  # Also rejects exponent overflow (e.g. 1e999).
        parsed = dataclasses.replace(result, stdout_json=value, json_status="parsed")
        if len(canonical(parsed.binding_value())) > BINDING_BYTES - 4096:
            parsed = dataclasses.replace(result, json_status="too_large")
    except (ValueError, RecursionError):
        parsed = dataclasses.replace(result, json_status="invalid")
    if program.json_required and parsed.json_status != "parsed" and result.outcome == "succeeded":
        parsed = dataclasses.replace(parsed, outcome="invalid_json", reason=parsed.json_status)
    return parsed


def verify_runtime(settings: ScriptsConfig) -> RuntimeManifest:
    """Verify declared runtime bytes; host libraries and filesystem are unrestricted."""
    root, manifest_path = Path(settings.runtime_root), Path(settings.runtime_manifest)
    manifest = RuntimeManifest.model_validate_json(
        read_file(manifest_path.parent, manifest_path.name, 4 * MIB)
    )
    if sum(item.byte_count for item in manifest.files) > settings.runtime_bytes:
        raise ScriptError("script_runtime_limit_exceeded")
    for item in manifest.files:
        data = read_file(root, item.path, item.byte_count)
        if (digest(data) != item.sha256 or len(data) != item.byte_count
                or (item.executable and not os.access(root / item.path, os.X_OK))):
            raise ScriptError("script_runtime_digest_mismatch")
    return manifest


def _process_argv(settings: ScriptsConfig, request: SandboxRequest, inputs: Path) -> list[str]:
    program = request.pinned.program
    argv = [str(Path(settings.runtime_root) / program.executable)]
    if program.script is not None:
        argv.append(str(inputs / program.script))
    elif program.shell is not None:
        argv.extend(["-c", program.shell, "aq-script"])
    return [*argv, *request.arguments]


def _process_group_running(pgid: int) -> bool:
    """Ignore exited zombies when observing an owned process group's cleanup."""
    proc = Path("/proc")
    if not proc.is_dir():
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        return True
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        fields = stat.rsplit(")", 1)[1].split()
        if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
            return True
    return False


async def _stop_process(process: asyncio.subprocess.Process) -> bool:
    """TERM then KILL the owned process group, including ordinary forked children.

    A program can deliberately escape with setsid; this is not an isolation
    boundary. A descendant retaining a pipe beyond cleanup is reported uncertain.
    """
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # A reaped leader does not prove the process group has finished.
        deadline = time.monotonic() + GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.025)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.wait_for(process.wait(), GRACE_SECONDS + 1)
        # Sending KILL and reaping the leader does not prove that children have
        # exited. Keep the capacity lease until the whole group has stopped.
        deadline = time.monotonic() + GRACE_SECONDS
        while await asyncio.to_thread(_process_group_running, process.pid):
            if time.monotonic() >= deadline:
                raise TimeoutError
            await asyncio.sleep(0.025)
        return True
    except (OSError, TimeoutError):
        # Close pipes if an escaped child retains them; never claim complete
        # capture or successful cleanup in that case.
        process._transport.close()
        return False


async def launch(
    settings: ScriptsConfig, request: SandboxRequest, *, jobs_enabled: bool,
    cancelled: asyncio.Event, lock_dir: str | Path, capacity: int,
) -> SandboxResult:
    """Run once with shared box capacity and at most one active script per run.

    ``lock_dir``/``capacity`` must be the existing job contract's values. Call
    before generic job admission, so capacity is acquired exactly once.
    All returned payloads are private; never log or expose them without A3.
    """
    if not settings.enabled or not jobs_enabled:
        return SandboxResult(request.execution_id, "admission_denied", reason="script_disabled",
                             cleanup_complete=True)
    if settings.validate():
        return SandboxResult(request.execution_id, "admission_denied",
                             reason="script_host_policy_invalid", cleanup_complete=True)
    process = None
    cm = None
    acquired = False
    acquiring = None
    run_lock = None
    key = None
    started = None
    clean = True
    stopped = False
    cause, reason = "succeeded", None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    counts = {"stdout": 0, "stderr": 0}
    tasks = []
    abort_admission = threading.Event()
    limits = request.pinned.program.limits
    temporary = None
    try:
        if len(request.model_dump_json().encode()) > REQUEST_BYTES:
            raise ScriptError("script_request_too_large")
        if not all((settings.runtime_root, settings.runtime_manifest,
                    settings.input_root, settings.state_root)):
            raise ScriptError("script_storage_or_runtime_unconfigured")
        limits.narrow(ScriptLimits(**{key: getattr(settings, key) for key in ScriptLimits.model_fields}))
        runtime = await asyncio.to_thread(verify_runtime, settings)
        pinned = request.pinned
        if runtime.runtime != pinned.program.runtime or runtime.sha256 != pinned.runtime_sha256:
            raise ScriptError("script_runtime_digest_mismatch")
        executable = next((item for item in runtime.files if item.path == pinned.program.executable),
                          None)
        if (executable is None or not executable.executable
                or executable.sha256 != pinned.executable_sha256):
            raise ScriptError("script_executable_digest_mismatch")
        stdin = request.stdin
        root = Path(settings.state_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = tempfile.TemporaryDirectory(dir=root, prefix="script-")
        work = Path(temporary.name)
        inputs, scratch = work / "inputs", work / "scratch"
        inputs.mkdir()
        scratch.mkdir()
        environment = script_environment(pinned.program.env_allowlist, tuple(settings.env_allowlist),
                                         request.environment, home=str(scratch))
        count = sum(item.byte_count for item in pinned.inputs)
        if count > settings.input_bytes:
            raise ScriptError("script_input_limit_exceeded")
        key = digest(canonical({"execution_id": request.execution_id, "run_id": request.run_id}))
        quota = QuotaLedger(root / "quotas", input_bytes=settings.input_storage_bytes,
                            output_bytes=settings.output_storage_bytes,
                            per_run_bytes=settings.per_run_storage_bytes)
        dynamic_bytes = len(stdin) + sum(len(arg.encode()) for arg in request.arguments)
        dynamic_bytes += len(canonical(request.environment))
        await asyncio.to_thread(quota.reserve, key, request.run_id, input_bytes=count + dynamic_bytes,
                                output_bytes=limits.stdout_bytes + limits.stderr_bytes)
        store = InputStore(Path(settings.input_root))
        for item in pinned.inputs:
            data = await asyncio.to_thread(store.read, item.sha256, item.byte_count)
            path = inputs / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o444)
        run_lock = open_beneath(root, "run-" + digest(request.run_id.encode()) + ".lock",
                               flags=os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + settings.queue_seconds
        while True:
            if cancelled.is_set():
                raise InterruptedError
            if time.monotonic() >= deadline:
                raise SlotTimeout
            try:
                fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)

        def waiting(*_):
            if abort_admission.is_set() or cancelled.is_set():
                raise InterruptedError

        cm = BoxLock(lock_dir, capacity).acquire(
            exclusive=False, weight=1, timeout=max(0, deadline - time.monotonic()), poll=0.05,
            meta={"run_id": request.run_id, "execution_id": request.execution_id,
                  "priority_band": 2}, on_wait=waiting,
        )
        # Shield admission: cancelling to_thread alone can leak a later-acquired lock.
        acquiring = asyncio.create_task(asyncio.to_thread(cm.__enter__))
        await asyncio.shield(acquiring)
        acquired = True
        if cancelled.is_set():
            raise InterruptedError
        intent = open_beneath(root, "intent-" + request.execution_id,
                              flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        os.close(intent)  # E2 owns durable recovery/retirement; never replay here.
        started = time.time()
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            *_process_argv(settings, request, inputs), env=environment,
            cwd=inputs if pinned.program.cwd == "inputs" else scratch,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, close_fds=True, start_new_session=True,
        ))
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            raise

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
        while True:
            if cancelled.is_set():
                stop_for("cancelled", "cancelled")
            elif time.monotonic() >= end:
                stop_for("timed_out", "wall")
            for task in tasks:
                if task.done() and not task.cancelled() and task.exception():
                    raise RuntimeError("script_pipe_failed")
            if cause != "succeeded" or process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        clean = await _stop_process(process)
        stopped = True
        await asyncio.wait_for(asyncio.gather(*tasks), GRACE_SECONDS + 1)
        if process.returncode:
            stop_for("nonzero_exit", "exit")
    except SlotTimeout:
        cause, reason = "admission_timed_out", "queue"
    except InterruptedError:
        cause, reason = "cancelled", "queue"
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        if isinstance(exc, TimeoutError) and process is not None:
            clean = False
        cause = "admission_denied" if process is None else "runtime_error"
        reason = exc.code if isinstance(exc, ScriptError) else "script_backend_failed"
    finally:
        abort_admission.set()
        if process is not None and not stopped:
            clean = await _stop_process(process)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if acquiring is not None and not acquired:
            try:
                await acquiring
                acquired = True
            except (InterruptedError, SlotTimeout, OSError, RuntimeError):
                pass
        if acquired:
            await asyncio.to_thread(cm.__exit__, None, None, None)
        if run_lock is not None:
            os.close(run_lock)
        if temporary is not None:
            temporary.cleanup()
    if not clean:
        cause, reason = "runtime_error", "script_cleanup_uncertain"
    result = SandboxResult(
        request.execution_id, cause, exit_code=process.returncode if process else None, reason=reason,
        stdout=bytes(buffers["stdout"]), stderr=bytes(buffers["stderr"]),
        stdout_bytes=counts["stdout"], stderr_bytes=counts["stderr"],
        capture_complete=clean and cause in {"succeeded", "nonzero_exit"}, cleanup_complete=clean,
        started_at=started, ended_at=time.time(), quota_key=key,
    )
    return _parse_output(result, request)
