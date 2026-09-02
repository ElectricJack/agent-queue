"""Package 5 fixture guard — ``tests/fixtures/playbooks/v2/``.

The child plan (``docs/superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md``
§10) specifies three fixtures that the graph-projection, artifact-diff and
run-overlay suites all build on.  The projectors themselves land with Packages
1/2/4 (``definition.py``, ``explanation.py``, ``engine.py``); the fixture data
does not depend on them, so it ships here with the structural properties §10
calls "load-bearing for a test" asserted directly.

Nothing below reaches past the frozen §4 DTO module, so this file stays green
while the upstream packages are still in flight — and turns any later edit that
quietly drops one of those properties into a failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.api.models.playbook_v2 import ReceiptDTO

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "playbooks" / "v2"
V5 = FIXTURE_DIR / "review-pipeline.artifact.json"
V6 = FIXTURE_DIR / "review-pipeline.v6.artifact.json"
RECEIPTS = FIXTURE_DIR / "review-pipeline.receipts.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture
def v5() -> dict:
    return _load(V5)


@pytest.fixture
def v6() -> dict:
    return _load(V6)


@pytest.fixture
def receipts() -> list[dict]:
    return _load(RECEIPTS)["receipts"]


class TestBaseArtifact:
    """§10.1 — the properties the projection suite selects on."""

    def test_two_rules_on_two_distinct_events(self, v5):
        events = [r["trigger"]["event_type"] for r in v5["rules"]]
        assert events == ["task.completed", "spec.approved"]

    def test_every_step_kind_appears(self, v5):
        kinds = {s["type"] for s in v5["steps"].values()}
        assert kinds == {
            "command",
            "llm",
            "agent_task",
            "decision",
            "wait",
            "foreach",
            "terminal",
        }

    def test_every_step_declares_its_owning_rule(self, v5):
        rule_ids = {r["id"] for r in v5["rules"]}
        assert all(s["rule"] in rule_ids for s in v5["steps"].values())

    def test_no_transition_crosses_a_rule_boundary(self, v5):
        """§5.1: a rule owns a closed subgraph — no cross-cluster edge."""
        for step_id, step in v5["steps"].items():
            for target in step.get("transitions", {}).values():
                assert v5["steps"][target]["rule"] == step["rule"], step_id

    def test_convergence_two_steps_target_await_approval(self, v5):
        sources = {
            step_id
            for step_id, step in v5["steps"].items()
            if "await-approval" in step.get("transitions", {}).values()
        }
        assert sources == {"classify-risk", "escalate"}

    def test_loop_back_edge_returns_to_the_entry_step(self, v5):
        assert v5["steps"]["await-approval"]["transitions"]["revise"] == "ensure-review-task"

    def test_decision_case_and_default_share_a_target(self, v5):
        """Two distinct edges with the same ``(source, target)`` pair — the V1
        dedupe regression §5.1 names.  They must stay independently selectable."""
        check = v5["steps"]["check-gate"]
        assert check["cases"][0]["goto"] == "for-each-task"
        assert check["default"] == "for-each-task"

    def test_three_terminals_live_in_the_review_rule(self, v5):
        terminals = {
            step_id
            for step_id, step in v5["steps"].items()
            if step["type"] == "terminal" and step["rule"] == "review-on-task-completed"
        }
        assert terminals == {"review-unavailable", "cancelled-end", "done"}

    def test_llm_step_maps_every_reserved_outcome(self, v5):
        transitions = v5["steps"]["classify-risk"]["transitions"]
        assert {
            "invalid_output",
            "budget_exceeded",
            "provider_error",
            "timed_out",
            "cancelled",
        } <= set(transitions)

    def test_one_of_every_value_kind_is_exercised(self, v5):
        kinds: set[str] = set()

        def walk(node) -> None:
            if isinstance(node, dict):
                if isinstance(node.get("type"), str):
                    kinds.add(node["type"])
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(v5["steps"])
        assert {
            "literal",
            "event_ref",
            "binding_ref",
            "loop_ref",
            "template",
            "comparison",
        } <= kinds

    def test_every_transition_target_exists(self, v5):
        for step_id, step in v5["steps"].items():
            for target in step.get("transitions", {}).values():
                assert target in v5["steps"], f"{step_id} -> {target}"

    def test_each_rule_entry_step_exists_and_belongs_to_it(self, v5):
        for rule in v5["rules"]:
            entry = v5["steps"][rule["entry_step"]]
            assert entry["rule"] == rule["id"]


class TestV6Companion:
    """§10.2 — drives ``executable_change=True``/``presentation_change_count=1``."""

    def test_it_is_the_next_version_of_the_same_playbook(self, v5, v6):
        assert v6["id"] == v5["id"]
        assert v6["version"] == v5["version"] + 1
        assert v6["source_hash"] != v5["source_hash"]

    def test_exactly_one_step_title_is_reworded_and_nothing_else(self, v5, v6):
        reworded = [
            step_id
            for step_id, step in v6["steps"].items()
            if step["title"] != v5["steps"][step_id]["title"]
        ]
        assert reworded == ["classify-risk"]
        before = dict(v5["steps"]["classify-risk"], title=None)
        after = dict(v6["steps"]["classify-risk"], title=None)
        assert before == after, "the reworded step must be presentation-only"

    def test_the_ensure_task_title_template_changed_executably(self, v5, v6):
        before = v5["steps"]["ensure-review-task"]["inputs"]["title"]
        after = v6["steps"]["ensure-review-task"]["inputs"]["title"]
        assert before != after

    def test_one_decision_case_was_added(self, v5, v6):
        assert len(v6["steps"]["check-gate"]["cases"]) == (
            len(v5["steps"]["check-gate"]["cases"]) + 1
        )

    def test_the_step_set_is_otherwise_unchanged(self, v5, v6):
        assert set(v6["steps"]) == set(v5["steps"])


class TestReceipts:
    """§10.2 — loop-iteration, multi-attempt and ``truncated=False`` overlay."""

    def test_there_are_eleven_and_they_all_satisfy_the_frozen_dto(self, receipts):
        assert len(receipts) == 11
        for raw in receipts:
            ReceiptDTO.model_validate(raw)

    def test_the_envelope_is_not_truncated(self):
        assert _load(RECEIPTS)["truncated"] is False

    def test_receipt_ids_are_unique(self, receipts):
        assert len({r["receipt_id"] for r in receipts}) == len(receipts)

    def test_the_first_step_was_retried_once(self, receipts):
        attempts = [
            (r["attempt"], r["outcome"])
            for r in receipts
            if r["step_id"] == "ensure-review-task"
        ]
        assert attempts == [(1, "rejected"), (2, "created")]

    def test_a_retried_attempt_selects_no_edge(self, receipts):
        retried = next(
            r for r in receipts if r["step_id"] == "ensure-review-task" and r["attempt"] == 1
        )
        assert retried["selected_edge_id"] is None
        assert retried["error"]

    def test_five_loop_iterations_indexed_zero_to_four(self, receipts):
        gates = sorted(
            (r for r in receipts if r["step_id"] == "open-gate"),
            key=lambda r: r["iteration_index"],
        )
        assert [r["iteration_index"] for r in gates] == [0, 1, 2, 3, 4]

    def test_exactly_one_iteration_failed_and_the_rest_still_ran(self, receipts):
        gates = [r for r in receipts if r["step_id"] == "open-gate"]
        failed = [r for r in gates if r["outcome"] == "rejected"]
        assert len(failed) == 1
        assert failed[0]["iteration_index"] == 3
        assert len(gates) == 5, "failure_policy: collect keeps the loop going"

    def test_iteration_index_is_set_only_inside_the_foreach_body(self, receipts):
        for raw in receipts:
            if raw["step_id"] == "open-gate":
                assert raw["iteration_index"] is not None
            else:
                assert raw["iteration_index"] is None

    def test_only_the_llm_receipt_carries_token_usage(self, receipts):
        with_usage = {r["step_id"] for r in receipts if r["token_usage"]}
        assert with_usage == {"classify-risk"}

    def test_only_the_wait_receipt_carries_wait_facts(self, receipts):
        with_wait = {r["step_id"] for r in receipts if r["wait"]}
        assert with_wait == {"await-approval"}

    def test_selected_edge_ids_use_the_content_derived_form(self, receipts):
        """§5.1: ``f'{rule_id}::{step_id}::{outcome}'``."""
        for raw in receipts:
            edge_id = raw["selected_edge_id"]
            if edge_id is None:
                continue
            assert edge_id == f"{raw['rule_id']}::{raw['step_id']}::{raw['outcome']}"

    def test_every_selected_edge_joins_a_declared_transition(self, v5, receipts):
        """The overlay's edge ids must join the projected graph's edge ids, and
        the projection derives those from the artifact's declared transitions."""
        declared = {
            f"{step['rule']}::{step_id}::{outcome}"
            for step_id, step in v5["steps"].items()
            for outcome in step.get("transitions", {})
        }
        for raw in receipts:
            if raw["selected_edge_id"] is not None:
                assert raw["selected_edge_id"] in declared

    def test_a_terminal_receipt_selects_no_edge(self, receipts):
        done = next(r for r in receipts if r["step_id"] == "done")
        assert done["step_kind"] == "terminal"
        assert done["selected_edge_id"] is None

    def test_every_receipt_names_a_step_that_exists_in_the_artifact(self, v5, receipts):
        for raw in receipts:
            step = v5["steps"][raw["step_id"]]
            assert step["rule"] == raw["rule_id"]
            assert step["type"] == raw["step_kind"]
