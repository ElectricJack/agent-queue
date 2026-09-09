"""Digest eligibility, aggregation and rendering (implementation spec §8).

Pure tests: no database, no clock, no transport.  Every row of the §8
eligibility table has a case here, plus the content rules -- one message under
1,200 characters, no mentions, no repeated highlights, and visibility applied
before anything is rendered.
"""

import pytest

from src.digest import (
    ActiveTask,
    DigestInputs,
    DigestWindow,
    WorkFact,
    build_digest,
    evaluate_eligibility,
    render_digest,
)
from src.digest.eligibility import (
    ALL_FILTERED,
    ALREADY_REPORTED,
    IDLE_ONLY,
    NO_ACTIVITY,
    highlight_key,
)
from src.digest.facts import KIND_COMPLETED, KIND_PROGRESS, KIND_STARTED

NOW = 1_000_000.0
HOUR = 3600.0
WINDOW = DigestWindow(since=NOW - HOUR, until=NOW)


def fact(key="f1", kind=KIND_COMPLETED, *, project_id="p", task_id="t1", at=NOW - 60,
         detail="did the thing", category="work", title="Task one"):
    return WorkFact(
        key=key, kind=kind, category=category, project_id=project_id,
        task_id=task_id, title=title, at=at, detail=detail,
    )


def inputs(**kwargs):
    kwargs.setdefault("window", WINDOW)
    return DigestInputs(**kwargs)


# --- the eligibility table -------------------------------------------------


@pytest.mark.parametrize("kind", [KIND_COMPLETED, KIND_STARTED, KIND_PROGRESS])
def test_recorded_work_in_a_selected_category_sends(kind):
    result = build_digest(inputs(facts=(fact(kind=kind),)))
    assert result.send is True
    assert result.reason == "activity"


def test_a_live_attempt_alone_sends_a_short_active_count():
    active = ActiveTask(task_id="t9", project_id="p", title="Long job", started_at=NOW - 4 * HOUR)
    result = build_digest(inputs(active=(active,)), dashboard_url="https://dash/x")
    assert result.send is True
    assert result.reason == "active_execution"
    assert result.active_count == 1
    assert "1 active" in result.text
    # No highlight is invented for a task that merely kept running.
    assert "Long job" not in result.text


def test_idle_queued_paused_or_waiting_work_alone_is_silent():
    result = build_digest(inputs(idle_tasks=7))
    assert result.send is False
    assert result.reason == IDLE_ONLY
    assert result.text == ""


def test_in_progress_container_with_no_live_execution_is_silent():
    # A container is IN_PROGRESS but owns no attempt, so it arrives as an idle
    # task with no fact and no active entry.
    result = build_digest(inputs(idle_tasks=1))
    assert (result.send, result.reason) == (False, IDLE_ONLY)


def test_no_activity_at_all_is_silent():
    result = build_digest(inputs())
    assert (result.send, result.reason) == (False, NO_ACTIVITY)


def test_activity_filtered_out_by_project_is_silent():
    result = build_digest(inputs(facts=(fact(project_id="other"),)),
                          project_ids=frozenset({"p"}))
    assert (result.send, result.reason) == (False, ALL_FILTERED)


def test_activity_filtered_out_by_category_is_silent():
    result = build_digest(inputs(facts=(fact(category="vcs"),)),
                          categories=frozenset({"work"}))
    assert (result.send, result.reason) == (False, ALL_FILTERED)


def test_open_escalations_alone_never_trigger_a_digest():
    result = build_digest(inputs(open_escalations=3))
    assert result.send is False
    assert result.reason == IDLE_ONLY


# --- noise that is not work ------------------------------------------------


def test_heartbeat_and_metrics_noise_produce_no_facts_and_no_digest():
    # The layer only ever sees facts the query built from durable work rows;
    # a window of pure heartbeat/layout/metrics churn produces none of them.
    result = build_digest(inputs(idle_tasks=12, active=()))
    assert result.send is False


def test_stale_or_waiting_work_is_not_active():
    # Staleness is decided by the collector; an empty ``active`` tuple with
    # idle tasks must not be rescued into an "active" digest here.
    assert evaluate_eligibility(inputs(idle_tasks=3)).send is False


# --- deduplication ---------------------------------------------------------


def test_facts_reported_in_an_earlier_window_are_not_reported_again():
    first = build_digest(inputs(facts=(fact(),)))
    second = build_digest(
        inputs(facts=(fact(),), reported_keys=first.reported_keys)
    )
    assert first.send is True
    assert (second.send, second.reason) == (False, ALREADY_REPORTED)


def test_identical_highlight_text_is_never_reposted_as_new_progress():
    first = build_digest(inputs(facts=(fact(key="c1", kind=KIND_PROGRESS),)))
    repeat = fact(key="c2", kind=KIND_PROGRESS, at=NOW - 10)
    second = build_digest(
        inputs(facts=(repeat,), reported_highlights=first.reported_highlights)
    )
    assert (second.send, second.reason) == (False, ALREADY_REPORTED)


def test_the_same_wording_about_a_different_task_still_reports():
    first = build_digest(inputs(facts=(fact(key="c1", kind=KIND_PROGRESS),)))
    other = fact(key="c2", kind=KIND_PROGRESS, task_id="t2")
    second = build_digest(
        inputs(facts=(other,), reported_highlights=first.reported_highlights)
    )
    assert second.send is True


def test_one_completion_recorded_twice_is_one_highlight():
    facts = (
        fact(key="completion:a", at=NOW - 90),
        fact(key="completion:b", at=NOW - 60),
    )
    result = build_digest(inputs(facts=facts))
    assert result.completed_count == 1
    assert result.text.count("did the thing") == 1


def test_late_arriving_events_are_reported_once_in_the_next_window():
    late = fact(key="completion:late", at=NOW - HOUR - 30)
    first = build_digest(inputs(facts=()))
    assert first.send is False
    # Next window's lookback picks the row up; it is new, so it is reported.
    second = build_digest(
        inputs(
            window=DigestWindow(since=NOW, until=NOW + HOUR),
            facts=(late,),
            reported_keys=first.reported_keys,
        )
    )
    assert second.send is True
    # And the window after that stays quiet about it.
    third = build_digest(
        inputs(
            window=DigestWindow(since=NOW + HOUR, until=NOW + 2 * HOUR),
            facts=(late,),
            reported_keys=second.reported_keys,
        )
    )
    assert third.send is False


# --- rendering -------------------------------------------------------------


def test_message_stays_under_the_limit_and_folds_the_rest_into_a_count():
    facts = tuple(
        fact(key=f"c{i}", task_id=f"t{i}", detail=f"finished chunk {i} " + "x" * 200, at=NOW - i)
        for i in range(20)
    )
    result = build_digest(
        inputs(facts=facts), dashboard_url="https://dash/tasks"
    )
    assert result.send is True
    assert len(result.text) <= 1200
    assert "\n" in result.text and result.text.count("\n") < 10
    assert "more" in result.text
    assert "https://dash/tasks" in result.text
    assert result.completed_count == 20
    assert "20 completed" in result.text


def test_highlights_are_capped_at_three_and_prefer_completions():
    facts = (
        fact(key="s1", kind=KIND_STARTED, task_id="t1", detail="started one"),
        fact(key="s2", kind=KIND_STARTED, task_id="t2", detail="started two"),
        fact(key="p1", kind=KIND_PROGRESS, task_id="t3", detail="note three"),
        fact(key="c1", kind=KIND_COMPLETED, task_id="t4", detail="shipped four"),
    )
    result = build_digest(inputs(facts=facts))
    assert "shipped four" in result.text
    assert "+1 more" in result.text


def test_no_mentions_survive_even_from_user_authored_text():
    facts = (
        fact(detail="@everyone please look, cc <@1234> and <@&99>"),
    )
    result = build_digest(inputs(facts=facts))
    assert "@everyone" not in result.text
    assert "<@1234>" not in result.text
    assert "<@&99>" not in result.text
    assert "\u200b" in result.text


def test_raw_tracebacks_never_reach_the_message():
    detail = "boom\nTraceback (most recent call last):\n  File x, line 1\nValueError: no"
    result = build_digest(inputs(facts=(fact(detail=detail),)))
    assert "Traceback" not in result.text
    assert "ValueError" not in result.text


def test_open_escalation_count_and_dashboard_link_are_appended():
    result = build_digest(
        inputs(facts=(fact(),), open_escalations=2), dashboard_url="https://dash/x"
    )
    assert "2 open escalations" in result.text
    assert "https://dash/x" in result.text


def test_highlights_are_grouped_by_project_name():
    facts = (
        fact(key="a", task_id="t1", project_id="p1", detail="alpha done"),
        fact(key="b", task_id="t2", project_id="p2", detail="beta done"),
    )
    result = build_digest(
        inputs(facts=facts, project_names={"p1": "Alpha", "p2": "Beta"})
    )
    assert "Alpha: completed — alpha done" in result.text
    assert result.text.index("Alpha") < result.text.index("Beta")


def test_a_catchup_window_is_labelled_and_not_split():
    window = DigestWindow(since=NOW - 24 * HOUR, until=NOW, catchup=True)
    result = build_digest(inputs(window=window, facts=(fact(),)))
    assert "catch-up, last 24h" in result.text
    assert len(result.text) <= 1200


def test_rendering_a_suppressed_window_is_a_programming_error():
    verdict = evaluate_eligibility(inputs())
    with pytest.raises(ValueError):
        render_digest(verdict, WINDOW)


# --- preview / delivery parity --------------------------------------------


def test_preview_and_delivery_agree_byte_for_byte():
    payload = inputs(facts=(fact(), fact(key="s1", kind=KIND_STARTED, task_id="t2")))
    preview = build_digest(payload, dashboard_url="https://dash/x")
    delivery = build_digest(payload, dashboard_url="https://dash/x")
    assert preview.text == delivery.text
    assert preview.output_hash == delivery.output_hash


def test_unknown_categories_are_rejected_rather_than_silently_ignored():
    with pytest.raises(ValueError):
        build_digest(inputs(facts=(fact(),)), categories=frozenset({"gossip"}))


def test_highlight_key_is_task_scoped():
    assert highlight_key(fact(task_id="a")) != highlight_key(fact(task_id="b"))


def test_completed_work_with_nothing_running_still_sends_with_a_zero_active_count():
    result = build_digest(inputs(facts=(fact(),), idle_tasks=4))
    assert result.send is True
    assert result.active_count == 0
    assert "0 active" in result.text
