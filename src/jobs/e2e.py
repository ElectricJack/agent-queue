"""The finite e2e preset: prepare its isolated world, then run its smoke kit."""

from __future__ import annotations
import asyncio
import os
from pathlib import Path


async def main():
    # The server owns this wrapper; the runner's cwd owns the submitted input.
    # __file__ may be in a different checkout from the live tree or snapshot
    # whose identity the runner records.
    root = Path.cwd()
    inherited = []
    for name in os.listdir("/proc/self/fd"):
        try:
            fd = int(name)
            if fd > 2 and os.get_inheritable(fd):
                inherited.append(fd)
        except OSError:
            continue
    for script, args in (("e2e-env.sh", ["--reset"]), ("e2e-smoke.sh", [])):
        child = await asyncio.create_subprocess_exec(
            str(root / "scripts" / script), *args, cwd=root, pass_fds=tuple(inherited)
        )
        code = await child.wait()
        if code:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
