"""``src/review_keys.py`` — the one place the pipeline's review dedup keys live.

The default pipeline writes ``review:task:<id>`` and ``branch-review:<branch>``
rows; the doctor's ``integration.unreviewed_prs`` looks for the first, and
``Orchestrator._emit_task_event`` flags a finishing task that carries either so
the review rules never review a review.  All three must agree on the strings.
"""

from __future__ import annotations

import pytest

from src.doctor.integration_checks import _review_dedup_key
from src.review_keys import (
    BRANCH_REVIEW_DEDUP_PREFIX,
    REVIEW_PROFILE_IDS,
    REVIEW_TASK_DEDUP_PREFIX,
    branch_review_dedup_key,
    is_pipeline_review_task,
    is_review_completion,
    is_review_role,
    review_task_dedup_key,
)


def test_keys_match_the_shipped_pipeline():
    assert review_task_dedup_key("t1") == "review:task:t1"
    assert branch_review_dedup_key("aq/x") == "branch-review:aq/x"
    assert review_task_dedup_key("t1").startswith(REVIEW_TASK_DEDUP_PREFIX)
    assert branch_review_dedup_key("aq/x").startswith(BRANCH_REVIEW_DEDUP_PREFIX)


def test_doctor_uses_the_same_key():
    assert _review_dedup_key("t1") == review_task_dedup_key("t1")


@pytest.mark.parametrize(
    ("dedup_key", "expected"),
    [
        ("review:task:abc", True),
        ("branch-review:aq/feature", True),
        ("spec-ingest:docs/specs/x.md", False),
        ("triage-open", False),
        ("", False),
        (None, False),
        ("review:", False),
        ("REVIEW:TASK:abc", False),
    ],
)
def test_is_pipeline_review_task(dedup_key, expected):
    assert is_pipeline_review_task(dedup_key) is expected


@pytest.mark.parametrize("dedup_key", [object(), 7, ["review:task:abc"]])
def test_non_string_dedup_keys_are_not_reviews(dedup_key):
    """A non-string key must read as "not a review", never as a maybe.

    ``_emit_task_event`` reads the key off whatever task object it is handed,
    including test doubles whose attributes are mocks.  ``MagicMock.startswith``
    returns a truthy mock, which would have flagged ordinary work as a review
    and made both review rules stand down on it.
    """
    assert is_pipeline_review_task(dedup_key) is False


def _reviewed_pipeline_artifact() -> dict:
    """The reviewed V2 artifact for `default-pipeline`.

    Since Package 6 the shipped Markdown is prose and no longer contains the
    literal `"dedup_key": ...` JSON these constants used to be checked against.
    The artifact is the stronger pin anyway: it is what an activation executes.
    """
    import json
    from pathlib import Path as _Path

    return json.loads(
        (
            _Path(__file__).parent
            / "fixtures" / "playbooks" / "v2" / "default-pipeline" / "artifact.json"
        ).read_text(encoding="utf-8")
    )


def _dedup_key_prefix(artifact: dict, step_id: str) -> str:
    parts = artifact["steps"][step_id]["inputs"]["dedup_key"]["parts"]
    assert parts[0]["type"] == "literal", parts
    return parts[0]["value"]


def test_reviewed_pipeline_artifact_uses_these_prefixes():
    """The reviewed artifact is the source of truth; the constants must not drift."""
    artifact = _reviewed_pipeline_artifact()
    assert (
        _dedup_key_prefix(artifact, "per-task-review--create-review")
        == REVIEW_TASK_DEDUP_PREFIX
    )
    assert (
        _dedup_key_prefix(artifact, "per-branch-final-review--ensure-final")
        == BRANCH_REVIEW_DEDUP_PREFIX
    )


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    [
        ("reviewer", True),
        ("final-reviewer", True),
        ("worker", False),
        ("spec-ingest", False),
        ("Reviewer", False),
        ("", False),
        (None, False),
    ],
)
def test_is_review_role(profile_id, expected):
    assert is_review_role(profile_id) is expected


def test_git_ops_no_code_fallback_shares_the_role_set():
    """``_task_produces_no_code``'s unresolved-profile fallback must not drift.

    If the two lists diverge, a task can be waved through git verification as
    no-code while still being announced as reviewable — or the reverse.
    """
    from src.orchestrator.git_ops import NO_CODE_PROFILE_IDS

    assert NO_CODE_PROFILE_IDS is REVIEW_PROFILE_IDS


@pytest.mark.parametrize(
    ("dedup_key", "profile_id", "expected"),
    [
        # Either signal alone is enough — that is the whole point of the OR.
        ("review:task:abc", "worker", True),
        ("branch-review:aq/x", None, True),
        (None, "reviewer", True),
        ("some-project-key", "final-reviewer", True),
        # Both present (the shipped configuration).
        ("review:task:abc", "reviewer", True),
        # Neither: an ordinary worker that shipped code, which is exactly what
        # the review rules exist for.
        (None, "worker", False),
        ("spec-ingest:docs/specs/x.md", "spec-ingest", False),
        ("", "", False),
    ],
)
def test_is_review_completion(dedup_key, profile_id, expected):
    assert is_review_completion(dedup_key, profile_id) is expected


def test_shipped_review_profiles_exist_with_these_ids():
    """The role guard is id-based, so a rename of either profile disarms it."""
    from pathlib import Path

    defaults = Path(__file__).parent.parent / "src" / "profiles" / "defaults"
    for profile_id in REVIEW_PROFILE_IDS:
        assert (defaults / profile_id / "profile.md").is_file(), (
            f"REVIEW_PROFILE_IDS names '{profile_id}' but no such shipped profile exists"
        )


def test_reviewed_pipeline_artifact_pins_the_review_profiles():
    """The reviewed artifact is the source of truth for the profile ids too."""
    artifact = _reviewed_pipeline_artifact()
    pinned = {
        step["inputs"]["profile_id"]["value"]
        for step in artifact["steps"].values()
        if isinstance(step.get("inputs"), dict) and "profile_id" in step["inputs"]
    }
    assert set(REVIEW_PROFILE_IDS) <= pinned


@pytest.mark.parametrize(
    ("dedup_key", "expected"),
    [
        ("review:task:abc", {"task_id": "abc", "review_task": True}),
        ("branch-review:aq/x", {"task_id": "abc", "review_task": True}),
        ("spec-ingest:x", {"task_id": "abc"}),
        (None, {"task_id": "abc"}),
    ],
)
def test_flag_review_task_event_only_narrows(dedup_key, expected):
    """The dispatch path sets ``review_task`` from the row; it never clears it."""
    from src.review_keys import flag_review_task_event

    event = {"task_id": "abc"}
    assert flag_review_task_event(event, dedup_key) is event
    assert event == expected

    # An emitter that already flagged the task keeps its flag either way.
    flagged = {"task_id": "abc", "review_task": True}
    flag_review_task_event(flagged, dedup_key)
    assert flagged["review_task"] is True


@pytest.mark.parametrize(
    ("dedup_key", "expected"),
    [
        ("review:task:abc", "abc"),
        ("review:task:sound-horizon-77.18.2", "sound-horizon-77.18.2"),
        ("review:task:", None),
        ("branch-review:aq/x", None),
        ("spec-ingest:x", None),
        ("", None),
        (None, None),
    ],
)
def test_reviewed_task_id(dedup_key, expected):
    """``reviewed_task_id`` inverts ``review_task_dedup_key``; anything else is ``None``."""
    from src.review_keys import reviewed_task_id

    assert reviewed_task_id(dedup_key) == expected
