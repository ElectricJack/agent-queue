"""Shared subprocess + NDJSON helpers for CLI-based platforms.

Both :class:`ClaudeCLIPlatform` and :class:`CodexCLIPlatform` use these
to manage subprocess lifecycle, stream stdout line-by-line, parse NDJSON
events, and isolate environment variables.  Keep platform-specific
parsing / classification out of this module — only generic plumbing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Callable

logger = logging.getLogger(__name__)

# Env vars that must be stripped before launching agent subprocesses to
# prevent the SDK / CLI from detecting it's inside an existing Claude session.
_STRIP_ENV_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def isolated_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build an env dict for agent subprocesses.

    Inherits :data:`os.environ` minus :data:`_STRIP_ENV_VARS`, then merges
    *extra* on top.  Use this for every subprocess launched by a platform.
    """
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_VARS}
    if extra:
        env.update(extra)
    return env


def parse_ndjson_line(line: bytes) -> dict | None:
    """Parse one NDJSON line into a dict, returning ``None`` for empty / malformed input.

    Streams from CLI platforms occasionally emit blank lines or malformed
    chunks; we log and skip rather than aborting the whole stream.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as e:
        logger.debug("malformed NDJSON line skipped: %s (line=%r)", e, line[:200])
        return None
    if not isinstance(obj, dict):
        logger.debug("NDJSON line was not an object: %r", obj)
        return None
    return obj


async def run_streaming_subprocess(
    cmd: list[str],
    env: dict[str, str],
    cwd: str,
    on_line: Callable[[bytes], None],
    cancel_event: asyncio.Event,
    *,
    sigterm_grace_seconds: float = 2.0,
) -> int:
    """Run *cmd* and stream stdout lines through *on_line*.

    Returns the subprocess exit code.  When *cancel_event* is set, sends
    SIGTERM, waits up to *sigterm_grace_seconds* for graceful exit, then
    SIGKILL.  *on_line* is called synchronously for each raw line
    (including trailing newline) as it arrives.

    Exceptions raised by *on_line* are logged and the stream continues; do
    not rely on *on_line* aborting the subprocess.

    Stderr is captured and logged; not surfaced to *on_line*.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _read_stdout() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                on_line(line)
            except Exception:
                logger.exception("on_line callback raised; continuing stream")

    async def _read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            logger.debug("subprocess stderr: %s", line.rstrip().decode(errors="replace"))

    async def _watch_cancel() -> None:
        await cancel_event.wait()
        if proc.returncode is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=sigterm_grace_seconds)
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())
    cancel_task = asyncio.create_task(_watch_cancel())

    try:
        await proc.wait()
    finally:
        cancel_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        try:
            await cancel_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("cancel watcher raised unexpectedly")

    return proc.returncode if proc.returncode is not None else -1
