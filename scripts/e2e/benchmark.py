"""Measure isolated daemon responsiveness and idle CPU/transaction load.

Run from the owned E2E fixture under the test gate; never against operator data.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import asyncpg
import yaml


def run(world, *, samples=3, idle_seconds=20):
    home = Path(world.env["AQ_E2E_HOME"])
    assert (home / ".aq-e2e").exists()
    config = yaml.safe_load((home / "config.yaml").read_text())
    db_url = config["database"]["url"].replace("postgresql+asyncpg://", "postgresql://")
    assert db_url.rsplit("/", 1)[-1].startswith("aq_e2e_")
    pid = int((home / "daemon.pid").read_text())
    smoke = world.smoke

    async def stats():
        conn = await asyncpg.connect(db_url)
        try:
            row = await conn.fetchrow(
                "SELECT xact_commit, xact_rollback FROM pg_stat_database WHERE datname=current_database()"
            )
            return row["xact_commit"] + row["xact_rollback"]
        finally:
            await conn.close()

    def cpu():
        fields = Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")

    def cycles():
        return int((home / "scheduler-cycles").read_text())

    def live(profile):
        rows = smoke.collection_rows(smoke.api_checked("session_list", {
            "project_id": smoke.PROJECT, "live_only": True,
        }), "sessions")
        return [r for r in rows if r["profile_id"] == profile and r["state"] == "running"]

    def ready(profile, intelligence_class):
        task = smoke.api_checked("create_task", {
            "project_id": smoke.PROJECT, "title": "responsiveness fixture", "description": "benchmark",
        })
        task_id = task.get("task_id") or task.get("created")
        smoke.api_checked("task_route_override", {
            "task_id": task_id, "profile_id": profile, "intelligence_class": intelligence_class,
            "reason": "isolated backend responsiveness measurement",
        })

    with patch.dict(os.environ, world.env):
        # Let startup and registration events settle before measuring idle load.
        time.sleep(6)
        before = {"cpu": cpu(), "transactions": asyncio.run(stats()), "cycles": cycles()}
        started = time.monotonic()
        time.sleep(idle_seconds)
        duration = time.monotonic() - started
        idle = {"seconds": duration, "cpu_seconds": cpu() - before["cpu"],
                "transactions": asyncio.run(stats()) - before["transactions"],
                "cycles": cycles() - before["cycles"]}
        idle["cpu_percent"] = idle["cpu_seconds"] / duration * 100
        transitions = {"routed_task_to_worker_seconds": [], "provider_auto_to_worker_seconds": []}
        for _ in range(samples):
            world.reset()
            started = time.monotonic()
            ready(smoke.POOL_PROFILE, smoke.POOL_CLASS)
            smoke.wait_for(lambda: live(smoke.POOL_PROFILE), what="routed task worker",
                           interval=0.05, timeout=30)
            transitions["routed_task_to_worker_seconds"].append(time.monotonic() - started)
            world.reset()
            smoke.api_checked("provider_set_state", {"provider": smoke.PROVA, "state": "disabled",
                                                     "reason": "isolated responsiveness benchmark"})
            ready(smoke.STD_A, "std-high")
            time.sleep(0.2)
            assert not live(smoke.STD_A), "disabled provider launched a worker"
            started = time.monotonic()
            smoke.api_checked("provider_set_state", {"provider": smoke.PROVA, "state": "auto"})
            smoke.wait_for(lambda: live(smoke.STD_A), what="recovered provider worker",
                           interval=0.05, timeout=30)
            transitions["provider_auto_to_worker_seconds"].append(time.monotonic() - started)
        world.reset()
    result = {"idle": idle, "transitions": transitions}
    print("BACKEND_TIMING " + json.dumps(result), flush=True)
    return result
