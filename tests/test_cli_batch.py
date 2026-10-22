"""``aq batch`` — held spec-ingestion batch CLI and its MCP tools (held batches L6.3)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.cli.app import cli
from src.cli.exceptions import ScopeDeniedError


@pytest.fixture(autouse=True)
def _pg_backend():
    """These transport/rendering tests do not allocate a database."""


DETAIL = {
    "batch_id": "prop-1", "state": "reviewing", "structure_revision": 3,
    "reviewer_task_id": "calm-1.9", "source": "spec:docs/x.md", "lineage": {},
    "project_id": "alpha", "structure_digest": "d" * 64, "source_review_id": "rev-1",
    "source_revision": 1, "source_content_sha256": "s" * 64, "decision_id": "dec-1",
    "members": {"a": "alpha-1"},
    "membership": [{"member_key": "a", "task_id": "alpha-1", "state": "active"}],
    "section_verdicts": {"§2": "faithful"}, "walkthrough_verdict": "fixed",
    "receipts": [{"receipt_id": "r1"}],
}


def _client(execute=None, detail=None):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    if isinstance(execute, Exception):
        client.execute.side_effect = execute
    else:
        client.execute.return_value = execute or {"success": True}
    client.get_ingest_batch.return_value = detail or DETAIL
    return client


def _invoke(argv, client, env=None):
    with patch("src.cli.app._get_client", return_value=client):
        return CliRunner().invoke(cli, ["batch", *argv], env=env, terminal_width=240)


def test_batch_group_lists_every_verb():
    result = CliRunner().invoke(cli, ["batch", "--help"])
    assert result.exit_code == 0, result.output
    for verb in ("show", "validate", "update", "review-record", "release", "retire"):
        assert verb in result.output


def test_show_reads_rest_detail_and_emits_envelope():
    client = _client()
    result = _invoke(["show", "prop-1", "--project", "alpha", "--json"], client)
    assert result.exit_code == 0, result.output
    client.get_ingest_batch.assert_awaited_once_with("alpha", "prop-1")
    client.execute.assert_not_awaited()
    data = json.loads(result.output)["data"]
    assert data["structure_revision"] == 3 and data["receipts"] == [{"receipt_id": "r1"}]


def test_show_defaults_project_from_session_env_and_renders():
    client = _client()
    result = _invoke(["show", "prop-1"], client, env={"AQ_PROJECT_ID": "alpha"})
    assert result.exit_code == 0, result.output
    client.get_ingest_batch.assert_awaited_once_with("alpha", "prop-1")
    assert "reviewing" in result.output and "alpha-1" in result.output
    assert "§2=faithful" in result.output and "receipts: 1" in result.output


def test_show_requires_a_project():
    result = _invoke(["show", "prop-1"], _client(), env={"AQ_PROJECT_ID": ""})
    assert result.exit_code == 2
    assert "--project" in result.output


def test_release_sends_the_reviewed_revision_and_digest():
    client = _client({"success": True, "outcome": "released"})
    result = _invoke([
        "release", "--batch-id", "prop-1", "--expected-revision", "3",
        "--expected-digest", "abc", "--project-id", "alpha",
    ], client)
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_batch_release", {
        "batch_id": "prop-1", "expected_revision": 3, "expected_digest": "abc",
        "project_id": "alpha",
    })


def test_retire_requires_a_reason_and_sends_it():
    missing = _invoke(["retire", "--batch-id", "prop-1", "--expected-revision", "3"], _client())
    assert missing.exit_code == 2 and "--reason" in missing.output
    client = _client({"success": True, "outcome": "retired"})
    result = _invoke([
        "retire", "--batch-id", "prop-1", "--expected-revision", "3", "--reason", "superseded",
    ], client)
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_batch_retire", {
        "batch_id": "prop-1", "expected_revision": 3, "reason": "superseded",
    })


def test_review_record_parses_structured_verdicts():
    client = _client({"success": True, "outcome": "recorded"})
    result = _invoke([
        "review-record", "--batch-id", "prop-1", "--expected-revision", "3",
        "--expected-digest", "abc", "--section-verdicts", '{"§2": "faithful"}',
        "--reverse-coverage-verdict", "faithful", "--walkthrough-verdict", "fixed",
        "--structural-findings", "[]",
    ], client)
    assert result.exit_code == 0, result.output
    name, args = client.execute.await_args.args
    assert name == "task_batch_review_record"
    assert args["section_verdicts"] == {"§2": "faithful"}
    assert args["structural_findings"] == []
    assert args["walkthrough_verdict"] == "fixed"


def test_update_and_validate_target_the_held_batch():
    client = _client()
    ops = '[{"kind": "rewrite", "task_id": "alpha-1", "changes": {"title": "T"}}]'
    result = _invoke([
        "update", "--proposal-id", "prop-1", "--expected-revision", "3", "--operations", ops,
    ], client)
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_batch_update", {
        "proposal_id": "prop-1", "expected_revision": 3, "operations": json.loads(ops),
    })
    client = _client({"success": True, "passed": True})
    result = _invoke([
        "validate", "--project-id", "alpha", "--batch-id", "prop-1", "--expected-revision", "3",
    ], client)
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_batch_validate", {
        "project_id": "alpha", "batch_id": "prop-1", "expected_revision": 3,
    })


def test_scope_refusal_exits_four_without_a_bypass():
    """A worker token's release refusal surfaces as out_of_scope; the CLI never retries."""
    client = _client(ScopeDeniedError("task_batch_release", "out of scope: release"))
    result = _invoke([
        "release", "--batch-id", "prop-1", "--expected-revision", "3",
        "--expected-digest", "abc", "--json",
    ], client)
    assert result.exit_code == 4, result.output
    assert client.execute.await_count == 1
    assert "out_of_scope" in result.output


def test_held_batch_mcp_tools_carry_contract_schemas():
    from src.commands.contracts.builtin import (
        TaskBatchReleaseArgs,
        TaskBatchRetireArgs,
        TaskBatchReviewRecordArgs,
    )
    from src.tools import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES

    tools = {tool["name"]: tool for tool in _ALL_TOOL_DEFINITIONS}
    for name, model in (
        ("task_batch_release", TaskBatchReleaseArgs),
        ("task_batch_retire", TaskBatchRetireArgs),
        ("task_batch_review_record", TaskBatchReviewRecordArgs),
    ):
        assert tools[name]["input_schema"] == model.model_json_schema()
        assert _TOOL_CATEGORIES[name] == "task"
