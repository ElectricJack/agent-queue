"""Frozen source evidence, no-write previews, late facts and honest git proofs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from sqlalchemy import event, insert, select, update

from src.cli.app import cli
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.commands.report_commands import ReportCommandsMixin
from src.config import AppConfig
from src.database import Database
from src.database import tables
from src.git.manager import GitManager
from src.models import Project, Task
from src.profiles.capabilities import DENY_ALL
from src.reports.git import MAX_COMMITS, read_git_evidence
from src.reports.hourly import MAX_BRIEF_BYTES
from src.reports.morning import collect_morning_evidence, preview_until, report_window
from tests.db_fixtures import lease_dsn

FIXTURES = Path(__file__).parent / "fixtures/reports/morning"
HEAD = "a" * 40
OLD = "b" * 40
WINDOW = report_window(86400, 172800)


def frozen(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def snapshot():
    return {
        "projects": [{"id": "p", "name": "Project P", "repo_default_branch": "main"}],
        "sources": {
            name: frozen(name)
            for name in (
                "completions",
                "deliveries",
                "incidents",
                "escalations",
                "attempts",
                "providers",
                "reroutes",
                "digests",
                "repos",
                "workspaces",
            )
        },
        "gaps": [],
        "excluded_sources": [],
    }


async def build(data=None, *, observed=None, **kwargs):
    db = SimpleNamespace(collect_morning_report_sources=AsyncMock(return_value=data or snapshot()))
    git = SimpleNamespace()
    from unittest.mock import patch

    with patch(
        "src.reports.morning.read_git_evidence",
        AsyncMock(return_value=observed or frozen("git")[0]),
    ) as read:
        result = await collect_morning_evidence(db, git, window=WINDOW, now=172801, **kwargs)
    return result, db, read


async def test_frozen_sources_separate_landed_pending_and_agent_verification():
    result, db, read = await build()
    brief = result["brief"]
    assert brief == json.loads((FIXTURES / "golden-brief.json").read_text())
    by_key = {fact["key"]: fact for fact in brief["facts"]}
    assert by_key["completion:c1"]["detail"]["shipment"] == "landed"
    assert by_key["completion:c2"]["detail"]["shipment"] == "pending"
    assert by_key["completion:c1"]["detail"]["verification_label"] == "agent-reported"
    assert "delivery:d1" in brief["projects"][0]["landed"]
    assert "completion:c2" in brief["projects"][0]["pending"]
    assert "attempt:attempt-1" in brief["projects"][0]["failures"]
    # Hourly digest membership cannot consume a morning fact.
    assert "completion:c1" in by_key
    assert read.call_args.kwargs["checkout"] == "/configured/base"
    assert read.call_args.kwargs["proof_commits"] == (HEAD,)
    db.collect_morning_report_sources.assert_awaited_once_with(
        since=-86400, until=172800, project_ids=None
    )


async def test_replayed_fact_is_late_and_morning_membership_deduplicates_it():
    data = snapshot()
    data["sources"]["completions"][0]["completed_at"] = 80000
    first, _, _ = await build(data)
    assert next(f for f in first["brief"]["facts"] if f["key"] == "completion:c1")["late"]
    second, _, _ = await build(data, reported_keys=frozenset({"completion:c1"}))
    assert "completion:c1" not in {f["key"] for f in second["brief"]["facts"]}
    # Critical outstanding failures remain visible even when reported before.
    third, _, _ = await build(data, reported_keys=frozenset({"escalation:e1"}))
    assert "escalation:e1" in {f["key"] for f in third["brief"]["facts"]}


async def test_partial_source_never_suppresses_or_advances_its_cursor():
    data = snapshot()
    data["sources"] = {"repos": frozen("repos")}
    data["gaps"] = [{"source": "completions", "reason": "read_failed"}]
    observed = frozen("git")[0]
    observed["commits"] = []
    result, _, _ = await build(data, observed=observed)
    assert result["reason"] == "partial_sources"
    assert result["would_suppress"] is False
    assert "completions" not in result["brief"]["source_cursors"]


async def test_git_range_preserves_old_commit_dates_and_defers_future_commits():
    observed = frozen("git")[0]
    observed["commits"][0]["at"] = -1000000
    result, _, _ = await build(observed=observed, previous_heads={"p": OLD})
    fact = next(f for f in result["brief"]["facts"] if f["source"] == "git")
    assert fact["detail"]["time_basis"] == "range_observation"
    assert not fact["late"]
    observed["commits"][0]["at"] = WINDOW["until"]
    result, _, _ = await build(observed=observed, previous_heads={"p": OLD})
    assert not any(f["source"] == "git" for f in result["brief"]["facts"])
    assert result["brief"]["source_heads"] == {}
    assert not result["brief"]["coverage"]["complete"]


async def test_no_change_healthy_day_suppresses_with_no_model_dependency():
    data = snapshot()
    data["sources"] = {"repos": frozen("repos")}
    observed = frozen("git")[0]
    observed["commits"] = []
    result, _, _ = await build(data, observed=observed, previous_heads={"p": HEAD})
    assert result["would_suppress"] is True
    assert result["reason"] == "no_changes"
    repeated, _, _ = await build(data, observed=observed, previous_heads={"p": HEAD})
    assert result["brief_hash"] == repeated["brief_hash"]


async def test_oversized_brief_has_no_dangling_refs_or_advanceable_cursors():
    data = snapshot()
    data["sources"]["completions"] = [
        {**data["sources"]["completions"][0], "id": f"c{i}", "changes": "x" * 1500}
        for i in range(100)
    ]
    result, _, _ = await build(data)
    brief = result["brief"]
    assert len(json.dumps(brief, ensure_ascii=False).encode()) <= MAX_BRIEF_BYTES
    assert brief["omitted"]["facts"] > 0
    keys = {fact["key"] for fact in brief["facts"]}
    assert set(brief["projects"][0]["landed"]) <= keys
    assert not brief["coverage"]["complete"]
    assert brief["source_cursors"] == {}


async def test_truncated_completion_commits_cannot_prove_landing():
    data = snapshot()
    data["sources"]["completions"][0]["commits"] = json.dumps([HEAD] * 201)
    result, _, _ = await build(data)
    fact = next(f for f in result["brief"]["facts"] if f["key"] == "completion:c1")
    assert fact["detail"]["shipment"] == "unknown"
    assert "completions" not in result["brief"]["source_cursors"]


@pytest.mark.parametrize(
    "zone,day,expected",
    [
        ("America/Los_Angeles", "2026-03-08T15:00:00", "2026-03-08T14:00:00"),
        ("America/Los_Angeles", "2026-11-01T16:00:00", "2026-11-01T15:00:00"),
        ("UTC", "2026-09-25T06:00:00", "2026-09-24T07:00:00"),
    ],
)
def test_preview_uses_report_zone_and_latest_default_boundary(zone, day, expected):
    def epoch(value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()

    assert preview_until(epoch(day), zone) == epoch(expected)


def test_long_outage_is_explicitly_capped_and_bad_windows_are_refused():
    window = report_window(0, 500000)
    assert window["omitted_interval"] == {"since": 0, "until": 240800}
    for since, until in ((100, 100), (200, 100), (float("nan"), 100)):
        with pytest.raises(ValueError):
            report_window(since, until)


class Commands(ReportCommandsMixin):
    def __init__(self, db, git=None):
        self.db = db
        self.orchestrator = SimpleNamespace(config=AppConfig(), git=git)


async def test_project_principal_rejects_foreign_selection_before_any_read():
    command = Commands(SimpleNamespace())
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL, project_id="p")
    with principal_context(principal):
        rejected = await command._cmd_morning_report_preview({"project_ids": ["q"]})
    assert rejected["error_code"] == "out_of_scope"


def test_cli_preview_requires_dry_run_and_forwards_window(monkeypatch):
    def execute(ctx, command, params):
        return {"success": True, "command": command, **params}

    monkeypatch.setattr("src.cli.reports._execute", execute)
    runner = CliRunner()
    assert runner.invoke(cli, ["report", "morning"]).exit_code == 2
    result = runner.invoke(
        cli,
        [
            "--json",
            "report",
            "morning",
            "--dry-run",
            "--since",
            "0",
            "--until",
            "172800",
            "--project-id",
            "p",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["command"] == "morning_report_preview"
    assert payload["since"] == 0 and payload["project_ids"] == ["p"]


@pytest.fixture
async def db():
    database = Database(lease_dsn("morning_reports"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project P"))
    await database.create_project(Project(id="q", name="Private Project Q"))
    for task_id in ("t1", "t2", "t3"):
        await database.create_task(Task(id=task_id, project_id="p", title=task_id, description=""))
    async with database._engine.begin() as conn:
        for name, table in (
            ("completions", tables.task_completion_records),
            ("deliveries", tables.development_deliveries),
            ("escalations", tables.escalations),
            ("attempts", tables.task_session_attempts),
            ("providers", tables.provider_availability_transitions),
            ("reroutes", tables.task_reroutes),
            ("digests", tables.digest_windows),
            ("repos", tables.repos),
        ):
            rows = [
                {key: value for key, value in row.items() if key in table.c} for row in frozen(name)
            ]
            await conn.execute(insert(table), rows)
        incident = frozen("incidents")[0]
        await conn.execute(
            insert(tables.task_metadata).values(
                task_id="t3", key="supervisor_recovery_incident", value=incident["value"]
            )
        )
        await conn.execute(
            update(tables.tasks)
            .where(tables.tasks.c.id == "t3")
            .values(status="FAILED", updated_at=incident["updated_at"])
        )
    yield database
    await database.close()


async def test_db_frozen_sources_use_one_read_only_snapshot_and_half_open_bounds(db):
    statements = []
    connections = set()

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        connections.add(id(conn))
        assert conn.get_execution_options()["isolation_level"] == "REPEATABLE READ"

    event.listen(db._engine.sync_engine, "before_cursor_execute", record)
    try:
        result = await db.collect_morning_report_sources(
            since=90000, until=90008, project_ids=("p",)
        )
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", record)
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert len(connections) == 1
    assert all(
        sql.startswith(("SELECT", "SET TRANSACTION", "SAVEPOINT", "RELEASE SAVEPOINT"))
        for sql in statements
    )
    for source in ("completions", "deliveries", "incidents", "escalations", "attempts", "reroutes"):
        assert result["sources"][source], source
    assert result["excluded_sources"] == ["providers", "digests"]
    assert [p["id"] for p in result["projects"]] == ["p"]
    all_sources = await db.collect_morning_report_sources(since=90000, until=90008)
    assert all_sources["sources"]["providers"]
    assert not all_sources["sources"]["digests"]  # end boundary is exclusive


async def test_preview_executes_no_database_writes_and_never_wakes_an_author(db, monkeypatch):
    before = {}
    watched = (
        tables.supervisor_report_requests,
        tables.messages,
        tables.digest_windows,
        tables.task_completion_records,
        tables.development_deliveries,
    )
    async with db._engine.connect() as conn:
        for table in watched:
            before[table.name] = (await conn.execute(select(table))).mappings().all()
    monkeypatch.setattr(
        "src.reports.morning.read_git_evidence", AsyncMock(return_value=frozen("git")[0])
    )
    command = Commands(db)
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL, project_id="p")
    with principal_context(principal):
        first = await command._cmd_morning_report_preview({"since": 86400, "until": 172800})
        second = await command._cmd_morning_report_preview({"since": 86400, "until": 172800})
    assert first["success"] and first["brief_hash"] == second["brief_hash"]
    assert "Private Project Q" not in json.dumps(first)
    async with db._engine.connect() as conn:
        for table in watched:
            assert (await conn.execute(select(table))).mappings().all() == before[table.name]


async def test_source_failure_savepoint_preserves_other_sources(db):
    def fail_completion(conn, cursor, statement, parameters, context, executemany):
        if "FROM task_completion_records" in statement:
            return "SELECT missing_report_source_column", ()
        return statement, parameters

    event.listen(db._engine.sync_engine, "before_cursor_execute", fail_completion, retval=True)
    try:
        result = await db.collect_morning_report_sources(
            since=90000, until=90008, project_ids=("p",)
        )
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", fail_completion)
    assert result["sources"]["completions"] == []
    assert result["gaps"] == [{"source": "completions", "reason": "read_failed"}]
    assert result["sources"]["deliveries"] and result["sources"]["attempts"]


async def git_command(git, checkout, *args):
    result = await git.arun_git_result(list(args), cwd=str(checkout))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
async def repo(tmp_path):
    git = GitManager()
    await git_command(git, tmp_path, "init", "--initial-branch=main")
    await git_command(git, tmp_path, "config", "user.name", "Morning test")
    await git_command(git, tmp_path, "config", "user.email", "morning@example.test")
    (tmp_path / "old.txt").write_text("old\n")
    await git_command(git, tmp_path, "add", "old.txt")
    await git_command(git, tmp_path, "commit", "-m", "old")
    old = await git_command(git, tmp_path, "rev-parse", "HEAD")
    (tmp_path / "new.txt").write_text("new\n")
    await git_command(git, tmp_path, "add", "new.txt")
    await git_command(git, tmp_path, "commit", "-m", "new")
    head = await git_command(git, tmp_path, "rev-parse", "HEAD")
    return git, tmp_path, old, head


async def test_git_range_freezes_hashes_diffstat_and_pr_ancestry_without_writes(repo):
    git, path, old, head = repo
    status = await git_command(git, path, "status", "--porcelain")
    result = await read_git_evidence(
        git,
        checkout=str(path),
        default_branch="main",
        since=0,
        until=2e10,
        now=2e10,
        previous_head=old,
        proof_commits=(head,),
    )
    assert result["head"] == head
    assert [c["sha"] for c in result["commits"]] == [head]
    assert "new.txt" in result["diffstat"]
    assert result["ancestry"][head] == "landed" and not result["gaps"]
    assert await git_command(git, path, "status", "--porcelain") == status
    assert await git_command(git, path, "rev-parse", "HEAD") == head


async def test_git_force_push_missing_objects_and_expression_inputs_are_discontinuities(repo):
    git, path, old, head = repo
    for previous in (HEAD, "HEAD~1; touch injected"):
        result = await read_git_evidence(
            git,
            checkout=str(path),
            default_branch="main",
            since=0,
            until=2e10,
            now=2e10,
            previous_head=previous,
        )
        assert "history_discontinuity" in result["gaps"]
        assert not result["commits"]
    await git_command(git, path, "update-ref", "refs/heads/main", old)
    result = await read_git_evidence(
        git,
        checkout=str(path),
        default_branch="main",
        since=0,
        until=2e10,
        now=2e10,
        previous_head=head,
    )
    assert "history_discontinuity" in result["gaps"]
    assert not (path / "injected").exists()


async def test_stale_remote_tracking_refs_are_visible_and_no_fetch_is_run(repo):
    git, path, old, head = repo
    await git_command(git, path, "update-ref", "refs/remotes/origin/main", head)
    result = await read_git_evidence(
        git,
        checkout=str(path),
        default_branch="main",
        since=0,
        until=2e10,
        now=2e10,
        previous_head=old,
    )
    assert result["ref"] == "refs/remotes/origin/main"
    assert "stale_remote_tracking_head" in result["gaps"]
    assert result["fetched_at"] is None


async def test_git_failure_is_a_gap_and_command_operands_are_hashes():
    calls = []

    async def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "check-ref-format":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout=HEAD, stderr="")
        if args[0] == "merge-base":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise RuntimeError("read failed")

    result = await read_git_evidence(
        SimpleNamespace(arun_git_result=run),
        checkout="/daemon/base",
        default_branch="main",
        since=0,
        until=100,
        now=100,
        previous_head=OLD,
    )
    assert "git_read_failed" in result["gaps"]
    log = next(args for args, _ in calls if args[0] == "log")
    assert f"{HEAD}..{HEAD}" in log and log[-1] == "--"
    assert f"--max-count={MAX_COMMITS + 1}" in log
    assert all(kwargs["cwd"] == "/daemon/base" for _, kwargs in calls)
