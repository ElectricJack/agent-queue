"""Pure evidence and request-packing checks for smart test selection."""

from __future__ import annotations

import json

import pytest

from src.test_selection import questions as q
from src.test_selection.catalogue import load_catalogue, load_rules
from src.test_selection.mandatory import mandatory_set
from src.test_selection.snapshot import ChangedPath, ChangeSnapshot
from src.test_selection.static_impact import StaticResult
from tests.selection_fixture_repo import build_fixture_repo


@pytest.fixture(autouse=True)
def _pg_backend():
    """These checks use only immutable inputs and pure functions."""


@pytest.fixture
def world(tmp_path):
    repo = build_fixture_repo(tmp_path / "repo")
    catalogue = load_catalogue(repo / "tests/selection_catalogue.json")
    return catalogue, load_rules(repo / "tests/selection_rules.yaml", catalogue)


def snap(*changes: ChangedPath, complete: bool = True) -> ChangeSnapshot:
    return ChangeSnapshot("/w", "origin/main", "b" * 40, "h" * 40, changes, "d", complete, None, None, 0.0)


STATIC_OK = StaticResult(frozenset({"tests/test_a.py"}), True, "fixed", None, 0, 1)


def state_for(world, *changes: ChangedPath, excerpt_lines: int = 40):
    catalogue, rules = world
    snapshot = snap(*changes)
    return q.build_state(
        snapshot,
        mandatory=mandatory_set(snapshot, catalogue, rules),
        static=STATIC_OK,
        catalogue=catalogue,
        excerpt_lines=excerpt_lines,
    )


def test_questions_are_one_choice_per_area_with_description_in_instructions(world):
    catalogue, _ = world
    built = q.build_questions(catalogue)
    assert [item.area_id for item in built] == sorted(catalogue.areas)
    alpha = next(item for item in built if item.area_id == "alpha")
    assert alpha.key == "area_alpha"
    assert alpha.body == {
        "type": "choice",
        "instructions": {"area": catalogue.areas["alpha"].description, "question": q.QUESTION_TEXT},
        "criteria": q.CRITERIA,
    }
    assert len({item.key for item in built}) == len(built)
    assert q.question_key("Docs Guards/2") == "area_docs_guards_2"


def test_state_separates_sanitized_evidence_from_code_derived_facts(world):
    change = ChangedPath(
        "src/pkg/a.py",
        "modified",
        hunks=("def alpha():",),
        excerpt="-    return 1\n+    return 2  # AKIAIOSFODNN7EXAMPLE\n+    url = 'https://u:p@h/x'\n",
    )
    state = state_for(world, change)
    assert state.evidence_complete
    [entry] = state.changes
    assert entry["path"] == "src/pkg/a.py" and entry["symbols"] == ["def alpha():"]
    assert "AKIAIOSFODNN7EXAMPLE" not in entry["excerpt"]
    assert "u:p@h" not in entry["excerpt"]
    assert entry["excerpt"].count("[redacted]") == 2
    assert "return 1" in entry["excerpt"]
    assert state.facts == {
        "static_impact_areas": ["alpha"],
        "mandatory_areas": ["alpha"],
        "changed_tests": [],
        "static_complete": True,
    }
    json.dumps(state.to_json())


def test_excerpts_are_bounded_and_hunk_context_is_sanitized(world):
    lines = ["+line " + str(index) for index in range(100)]
    lines[1] = "+ " + "x" * 450
    state = state_for(
        world,
        ChangedPath("src/pkg/a.py", "modified", hunks=("https://u:p@h/x",), excerpt="\n".join(lines)),
        excerpt_lines=10,
    )
    assert not state.evidence_complete
    [entry] = state.changes
    assert len(entry["excerpt"].splitlines()) == 10
    assert max(map(len, entry["excerpt"].splitlines())) <= 400
    assert "u:p@h" not in json.dumps(state.to_json())


def test_sanitize_excerpt_drops_nuls_before_secret_detection():
    excerpt = "+x\0y\n+sk-\0abcdefghijklmnopqrstuvwxyz\n" + "z" * 401
    safe = q.sanitize_excerpt(excerpt)
    assert "\0" not in safe
    assert safe.splitlines()[0] == "+xy"
    assert safe.splitlines()[1] == "[redacted]"
    assert len(safe.splitlines()[2]) == 400
    assert safe.splitlines()[2].endswith("…")


def test_incomplete_snapshot_or_static_result_marks_evidence_incomplete(world):
    catalogue, rules = world
    snapshot = snap(ChangedPath("src/pkg/a.py", "modified", excerpt="+x"), complete=False)
    mandatory = mandatory_set(snapshot, catalogue, rules)
    assert not q.build_state(snapshot, mandatory=mandatory, static=STATIC_OK, catalogue=catalogue).evidence_complete
    complete = snap(ChangedPath("src/pkg/a.py", "modified", excerpt="+x"))
    off = StaticResult(frozenset(), False, "unavailable", "static_unavailable", 0, 0)
    assert not q.build_state(complete, mandatory=mandatory, static=off, catalogue=catalogue).evidence_complete


def test_changed_tests_fact_excludes_deleted_tests(world):
    state = state_for(
        world,
        ChangedPath("tests/test_c.py", "deleted"),
        ChangedPath("tests/test_a.py", "modified"),
    )
    assert state.facts["changed_tests"] == ["tests/test_a.py"]


def test_token_estimate_is_conservative_and_deterministic():
    assert q.estimate_tokens({"a": "x" * 100}) >= 50
    assert q.estimate_tokens({"a": 1}) == q.estimate_tokens({"a": 1})


def test_packing_batches_all_questions_without_trimming_state(world):
    catalogue, _ = world
    state = state_for(world, ChangedPath("src/pkg/a.py", "modified", excerpt="+x\n"))
    built = q.build_questions(catalogue)
    one = q.pack_requests(state, built)
    assert one.complete and len(one.requests) == 1
    assert set(one.requests[0].questions) == {item.key for item in built}

    per_question = max(q.estimate_tokens(item.body) for item in built)
    state_tokens = q.estimate_tokens(state.to_json())
    batched = q.pack_requests(
        state, built, max_total_tokens=state_tokens + 2 * per_question + 1, max_requests=4
    )
    assert batched.complete and len(batched.requests) >= 2
    assert sorted(key for req in batched.requests for key in req.questions) == sorted(
        item.key for item in built
    )
    assert all(req.state == state.to_json() for req in batched.requests)
    assert all(
        req.estimated_tokens <= state_tokens + 2 * per_question + 1 for req in batched.requests
    )


def test_overflow_and_request_budget_exhaustion_fall_back(world):
    catalogue, _ = world
    state = state_for(
        world, ChangedPath("src/pkg/a.py", "modified", excerpt="+x\n" * 5000), excerpt_lines=5000
    )
    built = q.build_questions(catalogue)
    overflow = q.pack_requests(state, built, max_state_plus_question_tokens=100)
    assert (overflow.complete, overflow.reason, overflow.requests) == (False, "packing_overflow", ())
    one_question_tokens = max(
        q.estimate_tokens({"state": state.to_json(), "questions": {item.key: item.body}})
        for item in built
    )
    budget = q.pack_requests(
        state, built, max_total_tokens=one_question_tokens, max_requests=1
    )
    assert (budget.complete, budget.reason, budget.requests) == (False, "fallback_budget", ())
