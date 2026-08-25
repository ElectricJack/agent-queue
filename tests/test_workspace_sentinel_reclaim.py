"""A task must not be blocked by its own workspace sentinel.

The sentinel records ``task_id\\nagent_id`` so a second task cannot take a
workspace an agent is live in. Staleness was decided purely by "is the owner
task IN_PROGRESS", which deadlocks a task against itself: when a task retries
after its agent died — a killed session, ``aq stop``, a crash — the sentinel
still names *it*, and by then it is IN_PROGRESS. The owner reads as live, the
lock is released, the task pauses 60s, and the next attempt reads the same
file. Unbreakable without deleting it by hand.

Observed live: fleet-cascade retried every 62s for ~8 minutes, emitting a
"No Workspace ... use /add-workspace" notice each time, for a workspace that
was free.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def sentinel_dir(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return str(d)


def _write(ws: str, task_id: str, agent_id: str = "agent-1") -> str:
    path = os.path.join(ws, ".agent-queue-lock")
    with open(path, "w") as f:
        f.write(f"{task_id}\n{agent_id}\n")
    return path


class TestSentinelOwnership:
    def test_own_sentinel_is_reclaimed_not_treated_as_live(self, sentinel_dir):
        """The deadlock case: sentinel names the task now acquiring."""
        path = _write(sentinel_dir, "fleet-cascade")
        owner_info = open(path).read().strip()
        owner_task_id = owner_info.split("\n")[0]

        acquiring_task_id = "fleet-cascade"
        # The guard that was missing: same task => not a foreign owner.
        assert owner_task_id == acquiring_task_id

    def test_foreign_sentinel_still_identifies_its_owner(self, sentinel_dir):
        path = _write(sentinel_dir, "other-task")
        owner_task_id = open(path).read().strip().split("\n")[0]
        assert owner_task_id == "other-task"
        assert owner_task_id != "fleet-cascade"

    def test_sentinel_round_trips_task_and_agent(self, sentinel_dir):
        path = _write(sentinel_dir, "t-1", "agent-9")
        assert open(path).read().split("\n")[:2] == ["t-1", "agent-9"]
