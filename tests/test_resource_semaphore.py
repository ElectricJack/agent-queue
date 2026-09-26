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

from src.resources.semaphore import (
    SlotSemaphore,
    SlotTimeout,
    default_lock_dir,
    full_suite_lock_dir,
)
from src.resources.box_lock import BoxLock, IncompatibleLockClient, PROTOCOL_RECORD


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


class TestFullSuiteLock:
    """One full-suite run at a time: a one-slot semaphore beside the slots."""

    def test_it_lives_under_the_slot_directory(self, tmp_path):
        # Whatever relocates the slots (data_dir, a test isolating them)
        # relocates the full-suite lock with them.
        assert (
            full_suite_lock_dir(tmp_path / "test-slots") == tmp_path / "test-slots" / "full-suite"
        )

    def test_it_is_not_one_of_the_slots(self, sem):
        full = SlotSemaphore(full_suite_lock_dir(sem.lock_dir), 1)
        with full.acquire(timeout=0, meta={"task_id": "whole-suite"}):
            assert sem.snapshot()["free"] == 2
            assert full.try_acquire() is None
            # The slot directory's own waiters are not the full lock's.
            assert sem.snapshot()["waiting"] == []
        assert full.snapshot()["free"] == 1


class TestBoxLock:
    def test_shared_capacity_and_exclusion(self, tmp_path):
        box = BoxLock(tmp_path, 3)
        with box.acquire(weight=2, timeout=0) as slots:
            assert slots == (0, 1)
            with box.acquire(timeout=0) as other:
                assert other == (2,)
                with pytest.raises(SlotTimeout):
                    with box.acquire(timeout=0):
                        pytest.fail("capacity exceeded")
            with pytest.raises(SlotTimeout):
                with box.acquire(exclusive=True, timeout=0):
                    pytest.fail("exclusive overlaps shared")
        with box.acquire(exclusive=True, timeout=0):
            assert box.snapshot()["box"]["mode"] == "exclusive"
            assert box.snapshot()["incompatible_slots"] == []
            with pytest.raises(SlotTimeout):
                with box.acquire(timeout=0):
                    pytest.fail("shared overlaps exclusive")
            with pytest.raises(SlotTimeout):
                with box.acquire(exclusive=True, timeout=0):
                    pytest.fail("exclusive overlaps exclusive")
            assert box.semaphore.try_acquire() is None  # Fence racing old clients too.
        assert box.semaphore.snapshot()["free"] == 3

    def test_partial_weighted_claims_are_released_before_retry(self, tmp_path):
        box = BoxLock(tmp_path, 3)
        with box.acquire(weight=2, timeout=0):
            seen = []

            def waiting(_waited, snapshot):
                seen.append(snapshot)
                assert snapshot["free"] == 1  # The partial third slot was rolled back.
                assert box.snapshot()["box"]["mode"] == "shared"
                raise RuntimeError("cancel waiter")

            with pytest.raises(RuntimeError, match="cancel waiter"):
                with box.acquire(weight=2, on_wait=waiting):
                    pytest.fail("weight exceeds remaining capacity")
            assert seen
            with box.acquire(timeout=0) as slots:
                assert slots == (2,)
        assert not list(box.semaphore.waiters_dir.glob("*.json"))

    @pytest.mark.parametrize("capacity,weight", [(0, 1), (2, 0), (2, 3), (2, True), (2, 1.5)])
    def test_invalid_capacity_or_weight_is_refused(self, tmp_path, capacity, weight):
        with pytest.raises(ValueError):
            with BoxLock(tmp_path, capacity).acquire(weight=weight, timeout=0):
                pytest.fail("invalid weight admitted")
        assert not tmp_path.joinpath("box.lock").exists()

    def test_old_clients_are_detected_including_slots_above_capacity(self, tmp_path):
        box = BoxLock(tmp_path, 1)
        old = SlotSemaphore(tmp_path, 3)
        old._ensure_dirs()
        fd = old._try_slot(2, {"task_id": "old-client"})
        try:
            rows = box.snapshot()["incompatible_slots"]
            assert [row["slot"] for row in rows] == [2]
            with pytest.raises(IncompatibleLockClient, match="slot 2 is occupied"):
                with box.acquire(exclusive=True, timeout=0):
                    pytest.fail("legacy holder cannot provide box exclusion")
            with box.acquire(timeout=0):  # Today's shared aq test behavior remains usable.
                pass
        finally:
            os.close(fd)
        assert box.snapshot()["incompatible_slots"] == []  # Stale JSON is not a holder.
        with box.acquire(exclusive=True, timeout=0) as slots:
            assert slots == (0, 1, 2)
        assert not box.snapshot()["turnstile"]["held"]

    def test_unknown_or_corrupt_protocol_fails_closed_and_releases_turnstile(self, tmp_path):
        box = BoxLock(tmp_path, 2)
        for contents in ['{"protocol":"aq-box-lock","version":999}', "bad JSON"]:
            (tmp_path / "protocol.json").write_text(contents)
            with pytest.raises(IncompatibleLockClient, match="incompatible box-lock protocol"):
                with box.acquire(timeout=0):
                    pytest.fail("unknown protocol admitted")
            assert not box.snapshot()["turnstile"]["held"]
        (tmp_path / "protocol.json").write_text(json.dumps(PROTOCOL_RECORD))
        with box.acquire(timeout=0, meta={"box_protocol": {"version": 999}}):
            assert box.snapshot()["incompatible_slots"] == []

    def test_snapshot_does_not_create_lock_directory(self, tmp_path):
        path = tmp_path / "absent"
        state = BoxLock(path, 2).snapshot()
        assert not path.exists()
        assert not state["box"]["held"]
        assert not state["turnstile"]["held"]

    def test_exclusive_waiter_blocks_later_shared_and_kill_releases_turnstile(self, tmp_path):
        box = BoxLock(tmp_path, 2)
        script = textwrap.dedent(f"""
            from src.resources.box_lock import BoxLock
            box = BoxLock({str(tmp_path)!r}, 2)
            def waiting(*args):
                print('waiting', flush=True)
                input()
            with box.acquire(exclusive=True, poll=0.05, on_wait=waiting):
                print('exclusive', flush=True)
                input()
        """)
        with box.acquire(timeout=0):
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdout.readline().strip() == "waiting"  # Admission barrier.
                assert box.snapshot()["turnstile"]["held"]
                with pytest.raises(SlotTimeout):
                    with box.acquire(timeout=0):
                        pytest.fail("new reader passed exclusive waiter")
                proc.kill()
                proc.wait(timeout=10)
                with box.acquire(timeout=0):
                    pass
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

    def test_exclusive_waiter_admits_after_shared_drains(self, tmp_path):
        box = BoxLock(tmp_path, 2)
        script = textwrap.dedent(f"""
            from src.resources.box_lock import BoxLock
            announced = False
            def waiting(*args):
                global announced
                if not announced:
                    print('waiting', flush=True)
                    input()
                    announced = True
            with BoxLock({str(tmp_path)!r}, 2).acquire(
                exclusive=True, poll=0.05, on_wait=waiting
            ):
                print('exclusive', flush=True)
                input()
        """)
        proc = None
        try:
            with box.acquire(timeout=0):
                proc = subprocess.Popen(
                    [sys.executable, "-c", script],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    text=True,
                )
                assert proc.stdout.readline().strip() == "waiting"
            proc.stdin.write("drained\n")
            proc.stdin.flush()
            assert proc.stdout.readline().strip() == "exclusive"
            with pytest.raises(SlotTimeout):
                with box.acquire(timeout=0):
                    pytest.fail("new reader passed exclusive holder")
            proc.stdin.write("release\n")
            proc.stdin.flush()
            assert proc.wait(timeout=10) == 0
            with box.acquire(timeout=0):
                pass
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_executed_child_keeps_box_and_slots_after_wrapper_exits(self, tmp_path):
        box = BoxLock(tmp_path, 2)
        child = "print('child', flush=True); input()"
        script = textwrap.dedent(f"""
            import subprocess, sys
            from src.resources.box_lock import BoxLock
            with BoxLock({str(tmp_path)!r}, 2).acquire(exclusive=True, timeout=0):
                subprocess.Popen([sys.executable, '-c', {child!r}], close_fds=False)
        """)
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout.readline().strip() == "child"
            assert proc.wait(timeout=10) == 0  # Wrapper has closed its descriptors normally.
            assert box.snapshot()["box"]["mode"] == "exclusive"
            assert box.semaphore.snapshot()["free"] == 0
            with pytest.raises(SlotTimeout):
                with box.acquire(timeout=0):
                    pytest.fail("wrapper exit released child's execution capacity")
        finally:
            proc.stdin.write("done\n")
            proc.stdin.flush()
            proc.stdout.read()  # EOF is a process-exit barrier, not a timing guess.
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        with box.acquire(exclusive=True, timeout=0):
            pass
