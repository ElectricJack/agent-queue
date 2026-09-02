"""``src/review_keys.py`` — the one place the pipeline's review dedup keys live.

The default pipeline writes ``review:task:<id>`` and ``branch-review:<branch>``
rows; the doctor's ``integration.unreviewed_prs`` looks for the first, and the
session close path flags a finishing task that carries either so the review
rules never review a review.  All three must agree on the strings.
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


def test_default_pipeline_source_uses_these_prefixes():
    """The markdown is the source of truth; the constants must not drift from it."""
    from tests.conftest import DEFAULT_PIPELINE_PATH

    src = DEFAULT_PIPELINE_PATH.read_text(encoding="utf-8")
    assert f'"dedup_key": "{REVIEW_TASK_DEDUP_PREFIX}{{{{event.task_id}}}}"' in src
    assert f'"dedup_key": "{BRANCH_REVIEW_DEDUP_PREFIX}{{{{event.task.branch_name}}}}"' in src


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
