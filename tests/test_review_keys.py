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


@pytest.mark.parametrize(
    ("dedup_key", "expected"),
    [
        ("review:task:abc", "abc"),
        ("review:task:", None),
        ("branch-review:aq/feature", None),
        ("triage-open", None),
        ("", None),
        (None, None),
    ],
)
def test_reviewed_task_id(dedup_key, expected):
    """The reviewed task's id, recoverable from the review's own dedup key.

    ``_cmd_ensure_task`` uses it to refuse a review of a review without
    knowing anything about the pipeline that asked for one.
    """
    from src.review_keys import reviewed_task_id

    assert reviewed_task_id(dedup_key) == expected
