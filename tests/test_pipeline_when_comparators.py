"""Pipeline `when` — equals / is_null (controller ruling 2)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.orchestrator.core import _eval_pipeline_when
from src.playbooks.pipeline_compiler import _validate_when


def ev(**kw):
    # ``_eval_pipeline_when``'s ``event`` argument is the flat hydrated event
    # payload itself (see ``Orchestrator``'s pipeline dispatch in
    # ``src/orchestrator/core.py``) — a "event.foo" field path strips the
    # literal ``event`` prefix segment and walks straight into this dict, so
    # it must NOT be wrapped in another ``{"event": ...}`` layer.
    return kw


def test_equals_and_is_null():
    when = {
        "all": [
            {"field": "event.created_by_kind", "equals": "session"},
            {"field": "event.parent_task_id", "is_null": True},
        ]
    }
    assert _eval_pipeline_when(when, ev(created_by_kind="session", parent_task_id=None))
    assert not _eval_pipeline_when(when, ev(created_by_kind="human", parent_task_id=None))
    assert not _eval_pipeline_when(when, ev(created_by_kind="session", parent_task_id="p"))


def test_is_null_false():
    assert _eval_pipeline_when({"field": "event.x", "is_null": False}, ev(x=1))
    assert not _eval_pipeline_when({"field": "event.x", "is_null": False}, ev(x=None))


def test_validator_accepts_new_and_rejects_two_comparators():
    # ``_validate_when`` returns a list of structured error records — an
    # empty list means the clause is accepted.
    assert _validate_when({"field": "event.x", "equals": "y"}, "rule") == []
    assert _validate_when({"field": "event.x", "is_null": True}, "rule") == []
    errs = _validate_when({"field": "event.x", "equals": "y", "truthy": True}, "rule")
    assert errs, "two comparators on one leaf must be rejected"


def test_validator_rejects_non_bool_is_null():
    errs = _validate_when({"field": "event.x", "is_null": "yes"}, "rule")
    assert errs, "'is_null' must be a boolean"


def test_default_pipeline_legacy_triage_rule_absent():
    text = Path("src/prompts/default_playbooks/default-pipeline.md").read_text(encoding="utf-8")
    block = re.search(r"```json\n(.*?)\n```", text, re.S).group(1)
    rules = {r["id"]: r for r in json.loads(block)["rules"]}
    assert "worker-filed-triage" not in rules
    assert "task-created-routing" not in rules


def test_any_clause_semantics():
    when = {
        "any": [
            {"field": "event.a", "equals": "x"},
            {"field": "event.b", "truthy": True},
        ]
    }
    assert not _eval_pipeline_when(when, ev(a="no", b=""))
    assert _eval_pipeline_when(when, ev(a="x", b=""))
    assert _eval_pipeline_when(when, ev(a="x", b="yes"))


def test_vacuous_all_and_any(caplog):
    with caplog.at_level("WARNING"):
        assert _eval_pipeline_when({"all": []}, ev())
        assert not _eval_pipeline_when({"any": []}, ev())
    assert "vacuous" in caplog.text
    assert _eval_pipeline_when({"all": "nope"}, ev())
    assert _eval_pipeline_when("not-a-dict", ev())
    assert _validate_when({"all": []}, "rule")
    assert _validate_when({"any": []}, "rule")


def test_boolean_false_comparator_is_not_vacuously_true():
    assert not _eval_pipeline_when(
        {"field": "task.status", "truthy": False}, {"task": {"status": "open"}}
    )
    assert not _eval_pipeline_when(
        {"field": "task.parent", "not_null": False}, {"task": {"parent": "p"}}
    )


def test_dot_path_skips_only_leading_event_prefix():
    assert _eval_pipeline_when({"field": "a.event.b", "equals": 1}, {"a": {"event": {"b": 1}}})


def test_leaf_when_without_field_is_rejected_at_compile():
    errs = _validate_when({"equals": "session"}, "routing")
    assert errs and errs[0]["node"] == "routing"
    assert errs[0]["field"] == "when.field"
