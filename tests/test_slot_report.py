"""The slot-wait report ``aq test`` writes and the development publisher reads."""

from src.resources.slot_report import append_event, read_slot_wait


def test_a_missing_report_means_no_slot_was_ever_requested(tmp_path):
    wait = read_slot_wait(tmp_path / "absent.jsonl", now=100.0)
    assert wait.total_seconds == 0.0
    assert wait.waiting_seconds == 0.0
    assert wait.acquired == 0
    assert not wait.timed_out


def test_a_completed_wait_is_charged_once(tmp_path):
    report = tmp_path / "slot.jsonl"
    append_event(report, "waiting", at=10.0)
    append_event(report, "acquired", at=170.0, waited=160.0, slot=1)
    append_event(report, "released", at=380.0)
    wait = read_slot_wait(report, now=400.0)
    assert wait.total_seconds == 160.0
    assert wait.waiting_seconds == 0.0
    assert wait.acquired == 1


def test_an_open_wait_is_charged_up_to_now(tmp_path):
    report = tmp_path / "slot.jsonl"
    append_event(report, "waiting", at=50.0)
    wait = read_slot_wait(report, now=80.0)
    assert wait.waiting_seconds == 30.0
    assert wait.total_seconds == 30.0
    assert wait.acquired == 0


def test_waits_of_several_invocations_add_up(tmp_path):
    report = tmp_path / "slot.jsonl"
    append_event(report, "acquired", at=0.0, waited=0.0, slot=0)
    append_event(report, "released", at=5.0)
    append_event(report, "waiting", at=6.0)
    append_event(report, "acquired", at=26.0, waited=20.0, slot=0)
    append_event(report, "released", at=30.0)
    append_event(report, "waiting", at=31.0)
    wait = read_slot_wait(report, now=41.0)
    assert wait.total_seconds == 30.0
    assert wait.waiting_seconds == 10.0
    assert wait.acquired == 2


def test_a_slot_timeout_is_recorded(tmp_path):
    report = tmp_path / "slot.jsonl"
    append_event(report, "waiting", at=0.0)
    append_event(report, "slot_timeout", at=600.0, waited=600.0)
    wait = read_slot_wait(report, now=601.0)
    assert wait.timed_out
    assert wait.total_seconds == 600.0
    assert wait.waiting_seconds == 0.0


def test_a_torn_or_foreign_line_is_ignored(tmp_path):
    report = tmp_path / "slot.jsonl"
    append_event(report, "acquired", at=0.0, waited=4.0, slot=0)
    with report.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write('["a", "list"]\n')
        handle.write('{"event": "acquired", "waited": "soon"}\n')
        handle.write('{"event": "waiting", "at"')  # a write still in flight
    wait = read_slot_wait(report, now=10.0)
    assert wait.total_seconds == 4.0
    assert wait.acquired == 1


def test_append_never_raises_on_an_unwritable_path(tmp_path):
    append_event(tmp_path / "missing-dir" / "slot.jsonl", "waiting", at=0.0)
