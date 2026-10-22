"""Reviewed stuck-task watch acceptance over an isolated daemon and PostgreSQL, no LLM.

One disposable world (``scripts/e2e-env.sh --reset`` plus ``e2e-daemon.sh start``)
carries the whole sequence, so each phase builds on durable rows written by the
previous one. Time is substituted only at the declared boundary: the reminder
phase backdates ``last_notified`` in the disposable database, never the clock.
The daemon's own ``timer.15m`` fires no earlier than 15 minutes after start, so
manual ticks are the only runs while the test is inside that window.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from src.database import tables as t
from src.playbooks.private_values import is_reference
from tests.test_e2e_cli_stateful import REPO_ROOT, disposable_world, world_env

FIXTURES = Path("tests/fixtures/playbooks/v2")
KINDS = [("claims.holder_consistency", "holder_inconsistency")] + [
    ("stall.sweep", kind) for kind in (
        "unclaimed_work", "undelivered_blocker", "restore_branch", "worker_relaunch_loop")]
EVIDENCE = (t.messages, t.playbook_state_namespaces, t.playbook_state_entries,
            t.playbook_state_operations, t.playbook_step_receipts, t.observation_snapshots,
            t.playbook_private_values)
# A dry run must leave every one of these exactly as it found them.
WRITES = tuple(table.name for table in EVIDENCE if table is not t.messages)
WATCH_SUBJECTS = ("Stuck-task observation",)
WORLD_BUDGET = 840  # stay inside the daemon's first timer.15m window


class WatchWorld:
    def __init__(self, world, env):
        self.world, self.env, self.smoke = world, env, world.smoke
        self.home = Path(env["AQ_E2E_HOME"])
        self.config = yaml.safe_load((self.home / "config.yaml").read_text())
        self.started = time.monotonic()

    def api(self, command, args):
        return self.smoke.api_checked(command, args)

    def ready(self):
        url = self.env["AQ_E2E_API_URL"] + "/ready"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read() or b"{}")

    def daemon(self, action):
        result = subprocess.run([str(REPO_ROOT / "scripts/e2e-daemon.sh"), action],
                                cwd=REPO_ROOT, env=self.env, check=False, timeout=180,
                                capture_output=True, text=True)
        assert result.returncode == 0, f"{action}: {result.stdout}\n{result.stderr}"

    def install(self, playbook_id):
        bundle = self.home / "vault" / "acceptance" / playbook_id
        shutil.copytree(FIXTURES / playbook_id, bundle)
        imported = self.api("playbook_v2_import", {"path": str(bundle)})
        sha = (bundle / "artifact.sha256").read_text().strip()
        assert sha in json.dumps(imported)
        self.api("playbook_activate", {"playbook_id": playbook_id,
                                       "artifact_sha256": sha, "acknowledge_diff": sha})

    def evidence(self):
        async def read():
            engine = create_async_engine(self.config["database"]["url"])
            try:
                async with engine.connect() as conn:
                    return {table.name: [dict(row) for row in
                                         (await conn.execute(select(table))).mappings()]
                            for table in EVIDENCE}
            finally:
                await engine.dispose()

        return asyncio.run(read())

    def backdate(self, key, playbook, seconds):
        """Move one notification's last delivery into the past; the clock stays real."""
        namespace = self.entry(key, playbook)["namespace_id"]

        async def write():
            engine = create_async_engine(self.config["database"]["url"])
            try:
                async with engine.begin() as conn:
                    result = await conn.execute(
                        update(t.playbook_state_entries)
                        .where(t.playbook_state_entries.c.key == key,
                               t.playbook_state_entries.c.namespace_id == namespace)
                        .values(last_notified=time.time() - seconds))
                    assert result.rowcount == 1
            finally:
                await engine.dispose()

        asyncio.run(write())

    def entry(self, key, playbook="stuck-task-watch", records=None):
        records = records or self.evidence()
        namespaces = {row["namespace_id"] for row in records[t.playbook_state_namespaces.name]
                      if row["playbook_id"] == playbook}
        rows = [row for row in records[t.playbook_state_entries.name]
                if row["key"] == key and row["namespace_id"] in namespaces]
        assert len(rows) <= 1, rows
        return rows[0] if rows else None

    def watch_messages(self, records=None):
        records = records or self.evidence()
        return [row for row in records[t.messages.name]
                if (row["subject"] or "").startswith(WATCH_SUBJECTS)]

    def tick(self, identity, playbook="stuck-task-watch"):
        assert time.monotonic() - self.started < WORLD_BUDGET, "left the timer-free window"
        # Manual runs expose handled failures even when the policy completes.
        # A bounded publisher log cannot prove absence, so the real doctor
        # snapshot and its pages are partial; the watch checks each partition.
        result = self.smoke.api("run_playbook", {"playbook_id": playbook, "event": {
            "type": "timer.15m", "event_id": identity,
            "tick_time": datetime.now(UTC).isoformat()}})
        assert result.get("status") == "completed", result
        assert result["success"] is False
        failures = result["failed_steps"]
        assert any(row["step_id"] == "doctor_scan" for row in failures)
        assert all(row["step_id"] in {"doctor_scan", "doctor_page"}
                   and row["outcome"] == "failure" for row in failures), result
        snapshots = self.evidence()[t.observation_snapshots.name]
        doctor, = [row for row in snapshots if row["run_id"] == result["run_id"]
                   and row["producer"] == "doctor_run"]
        assert doctor["complete"] is False
        assert "bounded_log_tail" in doctor["envelope"]["reasons"]
        assert all(part["kind"] in {"publisher_skip", "publisher_error"}
                   for part in doctor["coverage"] if "bounded_log_tail" in part["reasons"])
        selected = [part for part in doctor["coverage"] if part["project_id"] == "e2e"
                    and (part["check_id"], part["kind"]) in KINDS]
        assert {(part["check_id"], part["kind"]) for part in selected} == set(KINDS)
        assert all(part["complete"] for part in selected), selected
        # Distinct observations must have increasing watermarks.
        time.sleep(0.01)
        return result


def assert_key_ready(watch):
    body = watch.ready()
    assert body["ready"] is True, body
    assert body["checks"]["playbook_private_value_key"]["ok"] is True, body
    playbooks = watch.config["playbooks"]
    assert playbooks["private_value_active_key_id"] in playbooks["private_value_key_env"]
    assert (watch.home / ".env").stat().st_mode & 0o077 == 0


@pytest.mark.integration
# Bound the whole multi-scenario world, including setup/restart and evidence reads.
@pytest.mark.timeout(WORLD_BUDGET)
def test_reviewed_watch_through_disposable_daemon(tmp_path):
    """Import, activate and execute the exact recording over the real daemon API."""
    env = world_env(tmp_path / "aq-e2e")
    fragment = tmp_path / "watch-config.yaml"
    fragment.write_text("swarm:\n  enabled: false\n")
    env["AQ_E2E_EXTRA_CONFIG"] = str(fragment)
    with disposable_world("watch", env) as world, patch.dict("os.environ", env):
        watch = WatchWorld(world, env)
        smoke, api = watch.smoke, watch.api
        # Activation handoff: the protected .env key is loaded and generated
        # invocation has private storage before anything is activated.
        assert_key_ready(watch)
        watch.install("stuck-task-watch")
        task = smoke.create_task("F3 blocked observation")
        api("set_task_status", {"task_id": task, "status": "BLOCKED"})
        other = smoke.create_task("F3 inactive-project observation",
                                  project_id=smoke.OTHER_PROJECT)
        api("set_task_status", {"task_id": other, "status": "BLOCKED"})
        api("pause_project", {"project_id": smoke.OTHER_PROJECT})
        key, other_key = f"task:e2e:blocked:{task}", f"task:other:blocked:{other}"

        # Only the active project notifies; generated reads stored protected state.
        initial = watch.tick("daemon-first")
        first = watch.evidence()
        row = watch.entry(key, records=first)
        message_id = row["message_id"]
        assert row["active"] and row["revision"] == 1 and row["episode"] == 1
        assert is_reference(row["value_reference"])
        assert first[t.playbook_private_values.name]
        assert watch.entry(other_key, records=first) is None
        assert [m["id"] for m in watch.watch_messages(first)] == [message_id]
        assert first[t.playbook_state_operations.name] and first[t.playbook_step_receipts.name]

        # Unchanged ticks and duplicate delivery suppress; replay is the same run.
        for identity in ("daemon-second", "daemon-third"):
            watch.tick(identity)
        replay = watch.tick("daemon-first")
        assert replay["run_id"] == initial["run_id"]
        unchanged = watch.evidence()
        row = watch.entry(key, records=unchanged)
        assert row["revision"] == 3 and row["episode"] == 1 and row["message_id"] == message_id
        assert [m["id"] for m in watch.watch_messages(unchanged)] == [message_id]

        # Dry run writes no state, message, receipt or snapshot rows.
        dry = api("dry_run_playbook", {"playbook_id": "stuck-task-watch", "event": {
            "type": "timer.15m", "event_id": "daemon-dry",
            "tick_time": datetime.now(UTC).isoformat()}})
        assert dry["dry_run"] is True
        after_dry = watch.evidence()
        assert {name: after_dry[name] for name in WRITES} == \
            {name: unchanged[name] for name in WRITES}
        assert watch.watch_messages(after_dry) == watch.watch_messages(unchanged)

        def refreshed(identity, revision):
            # Every complete tick refreshes a present key: one revision, no message.
            watch.tick(identity)
            row = watch.entry(key)
            assert row["revision"] == revision + 1, (identity, row)
            return row

        # A real daemon restart reloads the key ring and keeps the namespace.
        watch.daemon("stop")
        watch.daemon("start")
        assert_key_ready(watch)
        row = refreshed("daemon-after-restart", row["revision"])
        assert row["episode"] == 1 and row["message_id"] == message_id
        assert len(watch.watch_messages()) == 1

        # Reactivated project notifies; a paused project neither notifies nor clears.
        api("resume_project", {"project_id": smoke.OTHER_PROJECT})
        row = refreshed("daemon-other-active", row["revision"])
        notified = watch.entry(other_key)
        assert notified["active"] and notified["episode"] == 1
        assert len(watch.watch_messages()) == 2
        api("pause_project", {"project_id": smoke.OTHER_PROJECT})
        api("set_task_status", {"task_id": other, "status": "DEFINED"})
        row = refreshed("daemon-other-paused", row["revision"])
        held = watch.entry(other_key)
        assert held["active"] and held["revision"] == notified["revision"]
        assert held["message_id"] == notified["message_id"]
        assert len(watch.watch_messages()) == 2
        api("resume_project", {"project_id": smoke.OTHER_PROJECT})
        row = refreshed("daemon-other-resumed", row["revision"])
        assert not watch.entry(other_key)["active"]
        assert len(watch.watch_messages()) == 3
        assert row["message_id"] == message_id

        # Complete clear, then recurrence opens a new episode with one notice.
        api("set_task_status", {"task_id": task, "status": "DEFINED"})
        row = refreshed("daemon-clear", row["revision"])
        assert not row["active"] and row["message_id"] != message_id
        assert len(watch.watch_messages()) == 4
        api("set_task_status", {"task_id": task, "status": "BLOCKED"})
        row = refreshed("daemon-recur", row["revision"])
        assert row["active"] and row["episode"] == 2
        assert len(watch.watch_messages()) == 5

        # Disabled activations do not run; re-enabling keeps suppression.
        api("set_playbook_enabled", {"playbook_id": "stuck-task-watch", "enabled": False})
        refused = smoke.api("run_playbook", {"playbook_id": "stuck-task-watch", "event": {
            "type": "timer.15m", "event_id": "daemon-disabled",
            "tick_time": datetime.now(UTC).isoformat()}})
        assert refused["success"] is False and "No ready V2 artifact" in refused["error"]
        assert watch.entry(key) == row
        api("set_playbook_enabled", {"playbook_id": "stuck-task-watch", "enabled": True})
        row = refreshed("daemon-enabled-again", row["revision"])
        assert row["active"] and row["episode"] == 2
        assert len(watch.watch_messages()) == 5

        # Reminder bundle: silent before 86,400s, one reminder after, replay safe.
        api("set_playbook_enabled", {"playbook_id": "stuck-task-watch", "enabled": False})
        reminder = "stuck-task-watch-reminder"
        watch.install(reminder)
        watch.tick("reminder-first", reminder)
        assert watch.entry(key, reminder)["active"]
        assert len(watch.watch_messages()) == 6
        watch.backdate(key, reminder, 86400 - 300)
        watch.tick("reminder-before-boundary", reminder)
        assert len(watch.watch_messages()) == 6
        watch.backdate(key, reminder, 86400 + 1)
        due = watch.tick("reminder-due", reminder)
        assert len(watch.watch_messages()) == 7
        assert watch.tick("reminder-due", reminder)["run_id"] == due["run_id"]
        watch.tick("reminder-after", reminder)
        assert len(watch.watch_messages()) == 7
        assert watch.entry(key, reminder)["episode"] == 1
