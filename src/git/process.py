"""Own local command writers until completion, including interrupted startup.

The caller holds the workspace/ref locks. Never return cancellation while an
ordinary child in the command's process group can still write. Unknown cleanup
is a distinct failure so preparation can retain its durable ownership proof.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

CLEANUP_SECONDS = 5.0
PREPARE_CLEANUP_HOLD = "prepare_cleanup_unproved"
_pending: set[asyncio.Task] = set()


class ProcessCleanupError(Exception):
    """The command's writers could not be proved stopped; do not reuse its slot."""


async def _settle(task: asyncio.Task):
    """Wait out repeated caller cancellation without cancelling the owned task."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def _group_has_writer(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    # Orphan zombies may await PID 1 indefinitely. They cannot write; unlike
    # leader wait(), inspect the whole group and fail closed on unknown state.
    found = False
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            # Any process on the box may exit mid-scan: ENOENT at open, ESRCH
            # at read. A vanished process is in no group and cannot write.
            continue
        if int(fields[2]) == pgid:
            found = True
            if fields[0] not in {"Z", "X"}:
                return True
    if not found:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        raise ProcessCleanupError(f"process group {pgid} is not observable")
    return False


async def _stop(process: asyncio.subprocess.Process) -> None:
    try:
        async with asyncio.timeout(CLEANUP_SECONDS):
            if os.name != "posix":
                # A leader kill is not a process-tree proof on this platform.
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise ProcessCleanupError("process-group cleanup is unavailable")
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            while _group_has_writer(process.pid):
                await asyncio.sleep(0.01)
    except (OSError, ValueError, IndexError, TimeoutError) as exc:
        raise ProcessCleanupError(
            f"process-group cleanup unproved for pid {process.pid}"
        ) from exc


async def _stop_spawn(spawn: asyncio.Task) -> None:
    try:
        process = await spawn
    except Exception:
        return  # No child was created; preserve the original interruption.
    await _stop(process)


def _observe_pending(task: asyncio.Task) -> None:
    _pending.discard(task)
    if not task.cancelled():
        task.exception()  # The caller already recorded the unproved cleanup.


async def run_owned_process(
    *argv: str, timeout: float, input: bytes | None = None, **kwargs,
) -> tuple[asyncio.subprocess.Process, bytes, bytes]:
    """Capture output with bounded execution and cancellation-safe group cleanup."""
    process = None
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, start_new_session=os.name == "posix", **kwargs,
    ))
    try:
        # Keep the command deadline inclusive of subprocess creation. Shielding
        # creation retains the handle if cancellation lands during pipe setup.
        async def execute():
            nonlocal process
            process = await asyncio.shield(spawn)
            if input is not None:
                return await process.communicate(input)
            return await process.communicate()

        stdout, stderr = await asyncio.wait_for(execute(), timeout=timeout)
    except BaseException:
        cleanup = asyncio.create_task(_stop_spawn(spawn))
        # Startup itself could be stuck. Continue owning the handle in the
        # background, but return an explicit uncertainty within the margin.
        async def bounded_cleanup():
            done, _ = await asyncio.wait({cleanup}, timeout=CLEANUP_SECONDS)
            if not done:
                _pending.add(cleanup)
                cleanup.add_done_callback(_observe_pending)
                raise ProcessCleanupError("subprocess startup or cleanup did not settle")
            return cleanup.result()

        await _settle(asyncio.create_task(bounded_cleanup()))
        raise
    # Commands must not leave background writers behind even after exit 0.
    if os.name == "posix":
        await _settle(asyncio.create_task(_stop(process)))
    # Cancellation arriving during successful-exit cleanup still propagates.
    if asyncio.current_task().cancelling():
        raise asyncio.CancelledError
    return process, stdout, stderr
