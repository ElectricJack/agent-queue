"""Observe completed cycles in the real daemon for disposable fake sessions.

Cadence comes from production scheduling configuration. Commands, reconciliation
and events still run in the production daemon implementation.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path


async def run_fake_cycles(daemon, orch, shutdown_event, interval=None):
    """Expose completed cycles so negative assertions observe actual scheduling."""
    original = orch.run_one_cycle
    path = Path(os.environ["AQ_E2E_HOME"]) / "scheduler-cycles"
    count = 0

    async def observed_cycle():
        nonlocal count
        await original()
        count += 1
        temporary = path.with_suffix(".tmp")
        temporary.write_text(str(count))
        temporary.replace(path)

    orch.run_one_cycle = observed_cycle
    try:
        if interval is None:
            await daemon(orch, shutdown_event)
        else:
            await daemon(orch, shutdown_event, interval=interval)
    finally:
        orch.run_one_cycle = original


def main() -> None:
    from src import main as daemon

    if os.environ.get("AQ_E2E_SESSION_PROVIDER", "fake") == "fake":
        daemon._run_scheduler_cycles = functools.partial(
            run_fake_cycles, daemon._run_scheduler_cycles,
        )
    daemon.main()


if __name__ == "__main__":
    main()
