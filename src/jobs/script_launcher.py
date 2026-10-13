"""Operator-installed launcher; no configurable CLI paths or ambient credentials.

Install the package and Python environment root-owned. A root-owned wrapper may
exec this entry point via a sudo rule restricted to this exact command with no
arguments or ``--probe``. Run as the dedicated UID with no supplementary groups.
The only policy file is /etc/agent-queue/script-sandbox.json, owned by root.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys

from src.jobs.script_sandbox import (
    REQUEST_BYTES, SandboxRequest, SandboxResult, execute_in_launcher, prerequisites,
    read_launcher_policy,
)


async def serve(*, probe: bool = False) -> dict:
    policy = read_launcher_policy()
    if probe:
        return await prerequisites(policy)
    reader = asyncio.StreamReader(limit=REQUEST_BYTES)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    cancelled = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, cancelled.set)
    watcher = None
    try:
        raw = await asyncio.wait_for(reader.readline(), 10)
        if len(raw) > REQUEST_BYTES:
            raise ValueError("script_request_too_large")
        request = SandboxRequest.model_validate_json(raw)

        async def control():
            # EOF/anything besides the initial request cancels. No second
            # request can launch another process on this invocation.
            await reader.read(1)
            cancelled.set()

        watcher = asyncio.create_task(control())
        try:
            return (await execute_in_launcher(policy, request, cancelled)).to_wire()
        except Exception:
            # Unexpected failures must not assert that cleanup succeeded or
            # that no user code ran. E2 retains the identity for recovery.
            return SandboxResult(request.execution_id, "runtime_error",
                                 reason="script_cleanup_uncertain").to_wire()
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        transport.close()


def main() -> None:
    # The privileged transition, if any, has already happened in the fixed
    # operator wrapper. This process must never run as root.
    if os.getuid() == 0 or sys.argv[1:] not in ([], ["--probe"]):
        raise SystemExit(2)
    os.umask(0o077)
    try:
        result = asyncio.run(serve(probe=sys.argv[1:] == ["--probe"]))
    except Exception:
        result = {"ready": False, "reason": "script_launcher_unavailable"}
    # This private pipe may carry raw stream bytes. It must never be connected
    # to logging, a terminal, a public job artifact or a command response.
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
