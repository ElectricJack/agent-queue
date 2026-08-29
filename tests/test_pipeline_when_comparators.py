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
    when = {"all": [{"field": "event.created_by_kind", "equals": "session"},
                    {"field": "event.parent_task_id", "is_null": True}]}
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


def test_default_pipeline_triage_rule_present():
    text = Path("src/prompts/default_playbooks/default-pipeline.md").read_text(encoding="utf-8")
    block = re.search(r"```json\n(.*?)\n```", text, re.S).group(1)
    rules = {r["id"]: r for r in json.loads(block)["rules"]}
    rule = rules["worker-filed-triage"]
    assert rule["on"] == "task.created"
    assert rule["nodes"]["route"]["command"] == "task_route"
