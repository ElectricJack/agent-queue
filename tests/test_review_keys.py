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
    PIPELINE_REVIEW_PROFILE_IDS,
    REVIEW_TASK_DEDUP_PREFIX,
    branch_review_dedup_key,
    is_pipeline_review_task,
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


# ---------------------------------------------------------------------------
# The profile-id half of the same structural mark
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    [
        ("reviewer", True),
        ("final-reviewer", True),
        ("worker-standard", False),
        ("triage", False),
        ("", False),
        (None, False),
    ],
)
def test_is_pipeline_review_task_recognises_the_review_profiles(profile_id, expected):
    """A review task created outside ``ensure_task`` carries no dedup key.

    Its profile is still one the pipeline itself pins, so the profile alone
    has to be enough to mark it a review.
    """
    assert is_pipeline_review_task(None, profile_id) is expected


def test_either_mark_alone_is_enough():
    assert is_pipeline_review_task("review:task:t1", "worker-standard") is True
    assert is_pipeline_review_task("spec-ingest:x.md", "reviewer") is True
    assert is_pipeline_review_task("spec-ingest:x.md", "worker-standard") is False


def test_default_pipeline_source_pins_these_profiles():
    """The markdown is the source of truth for the profile ids too."""
    from tests.conftest import DEFAULT_PIPELINE_PATH

    src = DEFAULT_PIPELINE_PATH.read_text(encoding="utf-8")
    for profile_id in PIPELINE_REVIEW_PROFILE_IDS:
        assert f'"profile_id": "{profile_id}"' in src


def test_the_ad_hoc_review_profile_literals_delegate_here():
    """Two call sites grew the same set by hand; they must not drift."""
    from src.orchestrator.git_ops import NO_CODE_PROFILE_IDS

    assert NO_CODE_PROFILE_IDS == PIPELINE_REVIEW_PROFILE_IDS
