"""Job process identity: boot id, start ticks and a per-launch nonce."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from src.sessions.proctable import read_start_ticks, scan_by_env_marker, kill_marked_sync


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


async def identity() -> dict:
    return {
        "boot_id": await asyncio.to_thread(boot_id),
        "pid": os.getpid(),
        "start_ticks": await read_start_ticks(os.getpid()),
    }


async def processes(nonce: str):
    if not Path("/proc/self/stat").exists():
        raise RuntimeError("jobs.cleanup_blocked")
    return [p for p in await scan_by_env_marker("AQ_JOB_NONCE") if p.marker == nonce]


async def verified(receipt: dict, nonce: str) -> bool:
    if receipt.get("boot_id") != await asyncio.to_thread(boot_id):
        return False
    return any(
        p.pid == receipt.get("pid") and p.start_ticks == receipt.get("start_ticks")
        for p in await processes(nonce)
    )


async def stop_tree(nonce: str, *, grace=10):
    await asyncio.to_thread(kill_marked_sync, "AQ_JOB_NONCE", nonce, grace=grace)


async def require_readable_identity(receipt: dict) -> None:
    """A known living process with unreadable identity cannot prove cleanup."""
    if receipt.get("boot_id") != await asyncio.to_thread(boot_id):
        return

    def check():
        pid = receipt.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise RuntimeError("jobs.cleanup_blocked")
        directory = Path("/proc") / str(pid)
        try:
            raw = (directory / "stat").read_text()
            ticks = int(raw[raw.rfind(")") + 2 :].split()[19])
            if ticks == receipt.get("start_ticks"):
                env = (directory / "environ").read_bytes()
                marker = f"AQ_JOB_NONCE={receipt['nonce']}".encode()
                if marker not in env.split(b"\0"):
                    raise RuntimeError("jobs.cleanup_blocked")
        except FileNotFoundError:
            return  # confirmed gone, including a race with normal exit
        except (OSError, ValueError, IndexError) as exc:
            raise RuntimeError("jobs.cleanup_blocked") from exc

    await asyncio.to_thread(check)
