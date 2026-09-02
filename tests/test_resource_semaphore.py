"""Resource gating layer 2 — the box-wide ``aq test`` slot semaphore.

The behaviours worth protecting: a slot is exclusive, a crashed holder's
slot comes back without a reaper, waiting is observable, and a full box
times out with a retryable error instead of blocking forever.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from src.resources.semaphore import SlotTimeout, SlotSemaphore, default_lock_dir


@pytest.fixture
def sem(tmp_path):
    return SlotSemaphore(tmp_path / "test-slots", slots=2)


class TestAcquireRelease:
    def test_a_slot_is_handed_out_and_given_back(self, sem):
        with sem.acquire(timeout=0) as slot:
            assert slot == 0
            assert sem.snapshot()["free"] == 1
        assert sem.snapshot()["free"] == 2

    def test_slots_are_handed_out_in_order(self, sem):
        with sem.acquire(timeout=0) as first:
            fd_pair = sem.try_acquire()
            assert fd_pair is not None
            second, fd = fd_pair
            try:
                assert (first, second) == (0, 1)
                assert sem.snapshot()["free"] == 0
            finally:
                os.close(fd)

    def test_a_full_semaphore_refuses_a_nonblocking_acquire(self, sem):
        held = [sem.try_acquire() for _ in range(2)]
        try:
            assert all(h is not None for h in held)
            assert sem.try_acquire() is None
        finally:
            for pair in held:
                if pair:
                    os.close(pair[1])

    def test_holder_metadata_is_recorded(self, sem):
        with sem.acquire(timeout=0, meta={"task_id": "prime-ember"}):
            holder = sem.snapshot()["slots"][0]["holder"]
            assert holder["task_id"] == "prime-ember"
            assert holder["pid"] == os.getpid()

    def test_the_lock_is_released_even_when_the_body_raises(self, sem):
        with pytest.raises(RuntimeError):
            with sem.acquire(timeout=0):
                raise RuntimeError("boom")
        assert sem.snapshot()["free"] == 2


class TestCrashRelease:
    """A killed holder must not leave the box permanently one slot short."""

    def _holder_script(self, lock_dir) -> str:
        return textwrap.dedent(
            f"""
            import sys, time
            sys.path.insert(0, {os.getcwd()!r})
            from src.resources.semaphore import SlotSemaphore
            sem = SlotSemaphore({str(lock_dir)!r}, 2)
            with sem.acquire(timeout=0, meta={{"task_id": "doomed"}}):
                print("held", flush=True)
                time.sleep(120)
            """
        )

    def test_sigkill_returns_the_slot(self, tmp_path):
        lock_dir = tmp_path / "test-slots"
        sem = SlotSemaphore(lock_dir, 2)
        proc = subprocess.Popen(
            [sys.executable, "-c", self._holder_script(lock_dir)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout.readline().strip() == "held"
            assert sem.snapshot()["free"] == 1
            proc.kill()
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:  # pragma: no cover - defensive
                proc.kill()
        # No reaper ran; the kernel dropped the flock when the process died.
        # The stale JSON record is still on disk, which is exactly the case
        # snapshot() must not be fooled by.
        state = sem.snapshot()
        assert state["free"] == 2
        assert json.loads((lock_dir / "slot-0.lock").read_text())["task_id"] == "doomed"

    def test_a_stale_record_does_not_block_a_new_acquire(self, sem, tmp_path):
        stale = sem.slot_path(0)
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({"pid": 999999, "task_id": "ghost", "since": 0}))
        with sem.acquire(timeout=0) as slot:
            assert slot == 0


class TestWaiting:
    def test_waiting_is_visible_to_an_observer(self, sem):
        held = sem.try_acquire({"task_id": "holder"})
        assert held is not None
        second = sem.try_acquire({"task_id": "holder2"})
        assert second is not None
        seen: list[dict] = []

        def _release_after_first_poll(waited, snapshot):
            seen.append(snapshot)
            # The waiter's own record must be visible while it blocks —
            # that is what keeps a queued agent from looking hung.
            assert any(w.get("task_id") == "waiter" for w in snapshot["waiting"])
            os.close(held[1])

        with sem.acquire(
            timeout=30, poll=0.05, meta={"task_id": "waiter"}, on_wait=_release_after_first_poll
        ) as slot:
            assert slot == 0
        os.close(second[1])
        assert seen

    def test_the_waiter_record_is_cleaned_up(self, sem):
        held = sem.try_acquire()
        second = sem.try_acquire()
        try:
            with pytest.raises(SlotTimeout):
                with sem.acquire(timeout=0.15, poll=0.05, meta={"task_id": "waiter"}):
                    pass  # pragma: no cover - never entered
        finally:
            for pair in (held, second):
                if pair:
                    os.close(pair[1])
        assert list(sem.waiters_dir.glob("*.json")) == []

    def test_a_dead_waiter_is_swept_from_the_snapshot(self, sem):
        sem._ensure_dirs()
        (sem.waiters_dir / "999999.json").write_text(
            json.dumps({"pid": 999999, "since": time.time()})
        )
        assert sem.snapshot()["waiting"] == []

    def test_timeout_names_the_lock_dir(self, sem):
        held = [sem.try_acquire() for _ in range(2)]
        try:
            with pytest.raises(SlotTimeout, match="2 slot"):
                with sem.acquire(timeout=0, poll=0.01):
                    pass  # pragma: no cover - never entered
        finally:
            for pair in held:
                if pair:
                    os.close(pair[1])


class TestLockDirResolution:
    def test_lock_dir_hangs_off_data_dir(self):
        class _Cfg:
            data_dir = "/var/lib/aq"

        assert default_lock_dir(_Cfg()) == __import__("pathlib").Path(
            "/var/lib/aq/locks/test-slots"
        )

    def test_no_config_falls_back_to_the_home_install(self):
        # aq test has to work in a worktree whose config the CLI could not
        # load; the only requirement is that every agent agrees on the path.
        assert str(default_lock_dir(None)).endswith(".agent-queue/locks/test-slots")
