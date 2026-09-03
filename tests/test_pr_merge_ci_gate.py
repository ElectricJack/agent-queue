"""The CI gate on ``pr_merge`` — src/git/ci_gate.py + _check_ci_before_merge.

Regression cover for the 2026-09-03 finding behind task ``nimble-ember-15``:
PR #341 merged with ``Tests (default)`` FAILURE and landed the
``packages/aq-client/README.md`` regression that same run had caught, because
``main`` has no required status check and ``gh pr merge`` was called blind.

The rollup shapes below are the real ones ``gh pr view --json
statusCheckRollup`` returns — including #341's own ``FAILURE, CANCELLED``
pair for a single check name, which the fold has to read as red rather than
as an ambiguous mix.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig, IntegrationConfig
from src.database import Database
from src.git import ci_gate
from src.models import Project, RepoSourceType, Workspace
from src.orchestrator import Orchestrator

PR = "https://github.com/o/r/pull/341"


def check_run(name: str, conclusion: str | None, status: str = "COMPLETED") -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "workflowName": "Tests",
    }


def status_context(context: str, state: str) -> dict:
    return {"__typename": "StatusContext", "context": context, "state": state}


# ---------------------------------------------------------------------------
# classify_rollup
# ---------------------------------------------------------------------------


def test_all_checks_passing_is_green():
    verdict = ci_gate.classify_rollup(
        [check_run("Tests (default)", "SUCCESS"), check_run("Tests (slow)", "SUCCESS")]
    )
    assert verdict.state == ci_gate.GREEN
    assert verdict.is_green is True
    assert verdict.failing == ()


def test_pr_341_rollup_is_red():
    """The exact shape recorded on #341: one failure, one superseded run."""
    verdict = ci_gate.classify_rollup(
        [
            check_run("Tests (default)", "FAILURE"),
            check_run("Tests (default)", "CANCELLED"),
            check_run("Tests (migration-and-slow)", "SUCCESS"),
            check_run("Tests (postgres-integration)", "SUCCESS"),
        ]
    )
    assert verdict.state == ci_gate.RED
    assert verdict.failing == ("Tests (default)",)
    assert "Tests (default)" in verdict.summary()


def test_a_cancelled_run_alone_is_pending_not_red():
    """A run the concurrency group superseded says nothing about the code."""
    verdict = ci_gate.classify_rollup([check_run("Tests (default)", "CANCELLED")])
    assert verdict.state == ci_gate.PENDING
    assert verdict.pending == ("Tests (default)",)
    assert verdict.failing == ()


def test_a_cancelled_duplicate_does_not_mask_a_success():
    verdict = ci_gate.classify_rollup(
        [check_run("Tests (default)", "SUCCESS"), check_run("Tests (default)", "CANCELLED")]
    )
    assert verdict.state == ci_gate.GREEN


def test_in_progress_check_is_pending():
    verdict = ci_gate.classify_rollup(
        [check_run("Tests (default)", None, status="IN_PROGRESS")]
    )
    assert verdict.state == ci_gate.PENDING
    assert verdict.pending == ("Tests (default)",)


def test_skipped_and_neutral_count_as_satisfied():
    verdict = ci_gate.classify_rollup(
        [check_run("Docs", "SKIPPED"), check_run("Lint", "NEUTRAL")]
    )
    assert verdict.state == ci_gate.GREEN


def test_status_context_entries_are_judged_too():
    verdict = ci_gate.classify_rollup(
        [status_context("ci/legacy", "ERROR"), check_run("Tests (default)", "SUCCESS")]
    )
    assert verdict.state == ci_gate.RED
    assert verdict.failing == ("ci/legacy",)


def test_required_checks_filter_ignores_everything_else():
    verdict = ci_gate.classify_rollup(
        [check_run("Tests (default)", "SUCCESS"), check_run("Flaky nightly", "FAILURE")],
        required_checks=["Tests (default)"],
    )
    assert verdict.state == ci_gate.GREEN
    assert verdict.considered == ("Tests (default)",)


def test_a_required_check_the_rollup_never_mentions_blocks():
    verdict = ci_gate.classify_rollup(
        [check_run("Docs", "SUCCESS")], required_checks=["Tests (default)"]
    )
    assert verdict.state == ci_gate.PENDING
    assert verdict.missing == ("Tests (default)",)
    assert "not reported" in verdict.summary()


def test_empty_rollup_is_pending_not_green():
    assert ci_gate.classify_rollup([]).state == ci_gate.PENDING


def test_unreadable_rollup_is_unknown():
    verdict = ci_gate.classify_rollup(None)
    assert verdict.state == ci_gate.UNKNOWN
    assert verdict.is_green is False
    assert "could not be read" in verdict.summary()


def test_nameless_entry_is_ignored_rather_than_counted_as_a_check():
    verdict = ci_gate.classify_rollup(
        [{"__typename": "Future", "conclusion": "FAILURE"}, check_run("Tests", "SUCCESS")]
    )
    assert verdict.state == ci_gate.GREEN
    assert verdict.considered == ("Tests",)


def test_an_unrecognised_state_is_never_read_as_a_pass():
    verdict = ci_gate.classify_rollup([check_run("Tests", "SOMETHING_NEW")])
    assert verdict.state == ci_gate.PENDING


# ---------------------------------------------------------------------------
# GitManager.apr_check_rollup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apr_check_rollup_asks_gh_and_returns_entries(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    seen = {}

    async def fake(cmd, cwd, timeout):
        seen["cmd"] = cmd
        r = MagicMock()
        r.returncode = 0
        r.stdout = '{"statusCheckRollup": [{"name": "Tests", "conclusion": "SUCCESS"}]}'
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake)
    entries = await gm.apr_check_rollup("/cwd", PR)
    assert entries == [{"name": "Tests", "conclusion": "SUCCESS"}]
    assert seen["cmd"][:4] == ["gh", "pr", "view", PR]
    assert "statusCheckRollup" in seen["cmd"]


@pytest.mark.asyncio
async def test_apr_check_rollup_returns_none_when_gh_fails(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()

    async def fake(cmd, cwd, timeout):
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "gh: not authenticated"
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake)
    assert await gm.apr_check_rollup("/cwd", PR) is None


@pytest.mark.asyncio
async def test_apr_check_rollup_returns_none_on_malformed_json(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()

    async def fake(cmd, cwd, timeout):
        r = MagicMock()
        r.returncode = 0
        r.stdout = "not json"
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake)
    assert await gm.apr_check_rollup("/cwd", PR) is None


@pytest.mark.asyncio
async def test_apr_check_rollup_maps_null_to_an_empty_list(monkeypatch):
    """"No checks at all" is an answer, not a failure to read one."""
    from src.git.manager import GitManager

    gm = GitManager()

    async def fake(cmd, cwd, timeout):
        r = MagicMock()
        r.returncode = 0
        r.stdout = '{"statusCheckRollup": null}'
        return r

    monkeypatch.setattr(gm, "_arun_subprocess", fake)
    assert await gm.apr_check_rollup("/cwd", PR) == []


# ---------------------------------------------------------------------------
# _cmd_pr_merge honours integration.merge_ci_policy
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "cg.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="P1"))
    await d.create_workspace(
        Workspace(
            id="w1",
            project_id="p1",
            workspace_path=str(tmp_path / "repo"),
            source_type=RepoSourceType.CLONE,
        )
    )
    yield d
    await d.close()


def _config(tmp_path, **integration) -> AppConfig:
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "cg.db"),
        data_dir=str(tmp_path / "d"),
        integration=IntegrationConfig(**integration),
    )


def _handler(db, config, rollup) -> CommandHandler:
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    o.git.apr_check_rollup = AsyncMock(return_value=rollup)
    o.git.amerge_pr = AsyncMock(return_value={"success": True, "sha": "s" * 40, "error": None})
    o.git.avalidate_pr_for_merge = AsyncMock(return_value=MagicMock(head_oid="h" * 40))
    o.git.apr_base_ref = AsyncMock(return_value=None)
    return CommandHandler(o, config)


RED_ROLLUP = [check_run("Tests (default)", "FAILURE"), check_run("Tests (default)", "CANCELLED")]
GREEN_ROLLUP = [check_run("Tests (default)", "SUCCESS")]


async def _merge(handler, **extra):
    return await handler.execute("pr_merge", {"project_id": "p1", "pr_url": PR, **extra})


@pytest.mark.asyncio
async def test_policy_off_never_asks_about_ci(db, tmp_path):
    handler = _handler(db, _config(tmp_path, merge_ci_policy="off"), RED_ROLLUP)
    result = await _merge(handler)
    assert result["success"] is True
    assert "ci" not in result
    handler.orchestrator.git.apr_check_rollup.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_warn_merges_red_but_reports_it(db, tmp_path):
    handler = _handler(db, _config(tmp_path, merge_ci_policy="warn"), RED_ROLLUP)
    result = await _merge(handler)
    assert result["success"] is True
    assert result["ci"]["state"] == ci_gate.RED
    assert result["ci"]["blocked"] is False
    assert result["ci"]["failing"] == ["Tests (default)"]
    assert "merged anyway" in result["ci"]["message"]
    handler.orchestrator.git.amerge_pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_warn_is_the_shipped_default(db, tmp_path):
    """A fresh config probes CI and reports, without changing what merges."""
    handler = _handler(db, _config(tmp_path), RED_ROLLUP)
    result = await _merge(handler)
    assert result["success"] is True
    assert result["ci"]["policy"] == "warn"
    assert result["ci"]["state"] == ci_gate.RED


@pytest.mark.asyncio
async def test_policy_required_refuses_a_red_pr(db, tmp_path):
    handler = _handler(db, _config(tmp_path, merge_ci_policy="required"), RED_ROLLUP)
    result = await _merge(handler)
    assert result["success"] is False
    assert result["ci"]["blocked"] is True
    assert "Tests (default)" in result["error"]
    assert "force=true" in result["error"]
    handler.orchestrator.git.amerge_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_required_refuses_while_checks_are_still_running(db, tmp_path):
    rollup = [check_run("Tests (default)", None, status="IN_PROGRESS")]
    handler = _handler(db, _config(tmp_path, merge_ci_policy="required"), rollup)
    result = await _merge(handler)
    assert result["success"] is False
    assert "still running" in result["error"]
    handler.orchestrator.git.amerge_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_required_fails_closed_when_ci_cannot_be_read(db, tmp_path):
    handler = _handler(db, _config(tmp_path, merge_ci_policy="required"), None)
    result = await _merge(handler)
    assert result["success"] is False
    assert result["ci"]["state"] == ci_gate.UNKNOWN
    handler.orchestrator.git.amerge_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_required_merges_a_green_pr(db, tmp_path):
    handler = _handler(db, _config(tmp_path, merge_ci_policy="required"), GREEN_ROLLUP)
    result = await _merge(handler)
    assert result["success"] is True
    assert result["ci"]["state"] == ci_gate.GREEN
    handler.orchestrator.git.amerge_pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_overrides_the_required_policy_and_is_recorded(db, tmp_path):
    handler = _handler(db, _config(tmp_path, merge_ci_policy="required"), RED_ROLLUP)
    result = await _merge(handler, force=True)
    assert result["success"] is True
    assert result["ci"]["forced"] is True
    assert result["ci"]["blocked"] is False
    handler.orchestrator.git.amerge_pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_never_overrides_reserved_pr_delivery_content(db, tmp_path):
    handler = _handler(db, _config(tmp_path, merge_ci_policy="required"), RED_ROLLUP)
    handler.orchestrator.git.avalidate_pr_for_merge = AsyncMock(
        side_effect=ValueError("PR changes reserved daemon bookkeeping paths: .aq/claim.json")
    )

    result = await _merge(handler, force=True)

    assert result["success"] is False
    assert ".aq/claim.json" in result["error"]
    handler.orchestrator.git.apr_check_rollup.assert_not_awaited()
    handler.orchestrator.git.amerge_pr.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_checks_narrows_what_blocks(db, tmp_path):
    rollup = [check_run("Tests (default)", "SUCCESS"), check_run("Flaky nightly", "FAILURE")]
    config = _config(
        tmp_path, merge_ci_policy="required", merge_required_checks=["Tests (default)"]
    )
    handler = _handler(db, config, rollup)
    result = await _merge(handler)
    assert result["success"] is True
    assert result["ci"]["state"] == ci_gate.GREEN


@pytest.mark.asyncio
async def test_a_probe_that_raises_never_breaks_the_merge_under_warn(db, tmp_path):
    handler = _handler(db, _config(tmp_path, merge_ci_policy="warn"), None)
    handler.orchestrator.git.apr_check_rollup = AsyncMock(side_effect=RuntimeError("boom"))
    result = await _merge(handler)
    assert result["success"] is True
    assert result["ci"]["state"] == ci_gate.UNKNOWN


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_integration_config_defaults():
    integ = IntegrationConfig()
    assert integ.merge_ci_policy == "warn"
    assert integ.merge_required_checks == []
    assert integ.validate() == []


def test_integration_config_rejects_an_unknown_policy():
    errors = IntegrationConfig(merge_ci_policy="maybe").validate()
    assert any(e.field == "merge_ci_policy" for e in errors)


def test_integration_config_rejects_a_non_list_of_check_names():
    errors = IntegrationConfig(merge_required_checks=[""]).validate()
    assert any(e.field == "merge_required_checks" for e in errors)


def test_loader_reads_the_merge_ci_policy(tmp_path):
    from src.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "discord:\n"
        "  bot_token: t\n"
        "  guild_id: '1'\n"
        f"database_path: {tmp_path / 'x.db'}\n"
        "integration:\n"
        "  merge_ci_policy: required\n"
        "  merge_required_checks:\n"
        "    - Tests (default)\n"
    )
    config = load_config(str(path))
    assert config.integration.merge_ci_policy == "required"
    assert config.integration.merge_required_checks == ["Tests (default)"]


def test_loader_accepts_a_single_check_name_as_a_bare_string(tmp_path):
    from src.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "discord:\n"
        "  bot_token: t\n"
        "  guild_id: '1'\n"
        f"database_path: {tmp_path / 'x.db'}\n"
        "integration:\n"
        "  merge_required_checks: Tests (default)\n"
    )
    config = load_config(str(path))
    assert config.integration.merge_required_checks == ["Tests (default)"]
