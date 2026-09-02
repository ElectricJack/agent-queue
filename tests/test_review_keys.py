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


@pytest.mark.parametrize("dedup_key", [object(), 7, ["review:task:abc"]])
def test_non_string_dedup_keys_are_not_reviews(dedup_key):
    """A non-string key must read as "not a review", never as a maybe.

    ``_emit_task_event`` reads the key off whatever task object it is handed,
    including test doubles whose attributes are mocks.  ``MagicMock.startswith``
    returns a truthy mock, which would have flagged ordinary work as a review
    and made both review rules stand down on it.
    """
    assert is_pipeline_review_task(dedup_key) is False


def test_default_pipeline_source_uses_these_prefixes():
    """The markdown is the source of truth; the constants must not drift from it."""
    from tests.conftest import DEFAULT_PIPELINE_PATH

    src = DEFAULT_PIPELINE_PATH.read_text(encoding="utf-8")
    assert f'"dedup_key": "{REVIEW_TASK_DEDUP_PREFIX}{{{{event.task_id}}}}"' in src
    assert f'"dedup_key": "{BRANCH_REVIEW_DEDUP_PREFIX}{{{{event.task.branch_name}}}}"' in src
