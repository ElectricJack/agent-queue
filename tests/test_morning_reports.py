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


# Durable scheduling and read surface (implementation plan 2).


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize(
    "day,clock,zone,expected",
    [
        ("2026-03-08", "02:30", "America/Los_Angeles", "2026-03-08T10:00:00"),
        ("2026-11-01", "01:30", "America/Los_Angeles", "2026-11-01T08:30:00"),
        ("2026-09-25", "07:00", "America/Los_Angeles", "2026-09-25T14:00:00"),
        ("2026-09-25", "07:00", "UTC", "2026-09-25T07:00:00"),
        ("2026-10-04", "02:15", "Australia/Lord_Howe", "2026-10-03T15:30:00"),
    ],
)
def test_daily_boundary_resolves_gaps_first_fold_and_report_zone(day, clock, zone, expected):
    from src.reports.schedule import planned_at

    assert planned_at(datetime.fromisoformat(day).date(), clock, zone) == utc(expected)


def test_morning_config_loads_validates_and_round_trips(tmp_path):
    from dataclasses import asdict

    from src.config import MorningReportsConfig, load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "database:\n  url: postgresql://test:test@localhost/disposable\ndiscord:\n  bot_token: test\n  guild_id: test\nreports:\n  timezone: America/Los_Angeles\n  morning:\n"
        "    enabled: true\n    time: '02:30'\n    project_ids: [p]\n"
        "    destination: 'discord:123456789012345678'\n"
    )
    config = load_config(path).reports
    assert not config.validate()
    assert config.morning.enabled and config.morning.time == "02:30"
    assert asdict(config)["morning"]["max_lookback_hours"] == 72
    for values in (
        {"time": "7:00"},
        {"enabled": "false"},
        {"max_lookback_hours": 73},
        {"author_deadline_minutes": 0},
        {"destination": "../../private"},
        {"project_ids": [""]},
    ):
        assert MorningReportsConfig(**values).validate()
    config.timezone = "invalid/report-zone"
    assert config.validate()


async def durable_result(*, window, healthy=True, quiet=False):
    from src.reports.hourly import hash_brief

    result, _, _ = await build()
    brief = result["brief"]
    brief["window"] = window
    brief["source_cursors"] = {"completions": window["until"], "deliveries": window["until"]}
    brief["source_heads"] = {"p": HEAD}
    brief["coverage"] = {
        "complete": healthy,
        "gaps": [] if healthy else [{"source": "completions", "reason": "read_failed"}],
        "warnings": ["late_arrivals_outside_72h_unsupported"],
        "excluded_sources": [],
    }
    if not healthy:
        brief["source_cursors"].pop("completions")
    if quiet:
        brief["facts"] = []
        for project in brief["projects"]:
            for group in ("landed", "pending", "failures"):
                project[group] = []
    return {
        "brief": brief,
        "brief_hash": hash_brief(brief),
        "would_suppress": quiet and healthy,
        "reason": "no_changes"
        if quiet and healthy
        else "activity"
        if healthy
        else "partial_sources",
    }


@pytest.fixture
def scheduled(db, monkeypatch):
    command = Commands(db)
    command.orchestrator.config.reports.morning.enabled = True
    command.orchestrator.config.reports.morning.project_ids = ["p"]

    async def collect(*args, **kwargs):
        return await durable_result(window=kwargs["window"])

    collector = AsyncMock(side_effect=collect)
    monkeypatch.setattr("src.reports.morning.collect_morning_evidence", collector)
    return command, collector


async def tick(command, now):
    with principal_context(ExecutionPrincipal.service("morning-reports")):
        return await command._cmd_morning_report_tick({"now": now})


async def test_restart_before_inside_and_after_cutoff(scheduled):
    command, collector = scheduled
    assert (await tick(command, utc("2026-09-25T06:59:00")))["reason"] == "before_schedule"
    collector.assert_not_awaited()
    first = await tick(command, utc("2026-09-25T08:59:00"))
    row = await command.db.get_morning_report(first["report_id"])
    assert row["state"] == "ready"
    assert row["window_end"] == utc("2026-09-25T07:00:00")
    assert row["author_deadline"] == utc("2026-09-25T09:14:00")
    assert (await tick(command, utc("2026-09-25T09:13:59")))["state"] == "ready"
    assert (await tick(command, utc("2026-09-25T09:14:00")))["state"] == "final"
    skipped = await tick(command, utc("2026-09-26T23:00:00"))
    assert skipped["state"] == "skipped" and skipped["reason"] == "late_start"
    collector.assert_awaited_once()
    cursors = await command.db.list_morning_reports(limit=10)
    assert len(cursors) == 2


async def test_cutoff_boundary_is_inclusive_and_host_timezone_does_not_drive_tick(scheduled):
    command, _ = scheduled
    command.orchestrator.config.reports.timezone = "America/Los_Angeles"
    first = await tick(command, utc("2026-09-25T16:00:00"))
    row = await command.db.get_morning_report(first["report_id"])
    assert row["local_date"] == "2026-09-25" and row["state"] == "ready"
    assert row["planned_at"] == utc("2026-09-25T14:00:00")
    assert (await tick(command, utc("2026-09-26T16:00:01")))["state"] == "skipped"


async def test_concurrent_ticks_config_edits_and_restarts_reuse_frozen_snapshot(scheduled):
    import asyncio

    command, collector = scheduled
    now = utc("2026-09-25T07:00:00")
    results = await asyncio.gather(*(tick(command, now) for _ in range(4)))
    assert {r["report_id"] for r in results} == {"morning-2026-09-25"}
    collector.assert_awaited_once()
    row = await command.db.get_morning_report(results[0]["report_id"])
    command.orchestrator.config.reports.morning.time = "07:01"
    command.orchestrator.config.reports.morning.max_lookback_hours = 48
    await tick(command, now + 120)
    assert (await command.db.get_morning_report(row["id"]))["brief_hash"] == row["brief_hash"]
    assert (await command.db.get_morning_report(row["id"]))["config_snapshot"]["time"] == "07:00"
    await tick(command, now + 901)
    final = await command.db.get_morning_report(row["id"])
    assert final["report"] == final["fallback"]
    assert await command.db.finalize_morning_fallback(row["id"], now=now + 1000) is None
    collector.assert_awaited_once()


async def test_zone_change_guard_shows_next_due_and_does_not_reserve_twice(scheduled):
    command, _ = scheduled
    first = await tick(command, utc("2026-09-25T07:00:00"))
    command.orchestrator.config.reports.timezone = "Pacific/Kiritimati"
    guarded = await tick(command, utc("2026-09-25T17:00:00"))
    assert guarded["reason"] == "timezone_guard"
    assert guarded["report_id"] is None
    assert guarded["next_due_at"] == utc("2026-09-26T17:00:00")
    rows = await command.db.list_morning_reports()
    assert [r["id"] for r in rows] == [first["report_id"]]


async def test_quiet_day_advances_coverage_without_author_or_delivery(scheduled):
    command, collector = scheduled

    async def collect(*a, **kw):
        return await durable_result(window=kw["window"], quiet=True)

    collector.side_effect = collect
    now = utc("2026-09-25T07:00:00")
    assert (await tick(command, now))["state"] == "suppressed"
    await tick(command, now + 86400)
    assert collector.call_args.kwargs["window"]["since"] == now
    async with command.db._engine.connect() as conn:
        assert not (await conn.execute(select(tables.supervisor_report_requests))).all()
        # Existing fixture digest rows are unchanged: morning coverage has no transport receipt.
        assert len((await conn.execute(select(tables.digest_windows))).all()) == 1


async def test_partial_source_recovery_uses_older_cursor_and_replay_membership(scheduled):
    command, collector = scheduled
    now = utc("2026-09-25T07:00:00")

    async def partial_collect(*a, **kw):
        return await durable_result(window=kw["window"], healthy=False)

    collector.side_effect = partial_collect
    first = await tick(command, now)
    await tick(command, now + 900)
    async with command.db._engine.connect() as conn:
        rows = (await conn.execute(select(tables.morning_report_coverage))).mappings().all()
    by_source = {r["source"]: r["covered_until"] for r in rows}
    assert by_source["completions"] == now - 86400
    assert by_source["deliveries"] == now

    async def healthy_collect(*a, **kw):
        return await durable_result(window=kw["window"])

    collector.side_effect = healthy_collect
    await tick(command, now + 86400)
    kwargs = collector.call_args.kwargs
    assert kwargs["window"]["since"] == now - 86400
    assert kwargs["previous_heads"] == {"p": HEAD}
    assert "completion:c1" in kwargs["reported_keys"]
    await tick(command, now + 86400 + 900)
    third = await tick(command, now + 2 * 86400)
    assert third["state"] == "ready"
    assert collector.call_args.kwargs["window"]["since"] == now + 86400
    assert (await command.db.get_morning_report(first["report_id"]))["coverage"][
        "complete"
    ] is False


async def test_failed_build_and_expired_lease_recover_original_context(scheduled):
    command, collector = scheduled
    now = utc("2026-09-25T07:00:00")
    collector.side_effect = RuntimeError("snapshot unavailable")
    first = await tick(command, now)
    row = await command.db.get_morning_report(first["report_id"])
    assert row["state"] == "failed"
    original = row["build_context"]
    await command.db.claim_morning_build(row["id"], owner="crashed", now=now)
    await tick(command, now + 100)
    assert collector.await_count == 1

    async def healthy_collect(*a, **kw):
        return await durable_result(window=kw["window"])

    collector.side_effect = healthy_collect
    await tick(command, now + 300)
    assert collector.call_args.kwargs["window"] == original["window"]
    assert collector.call_args.kwargs["now"] == now
    assert (await command.db.get_morning_report(row["id"]))["state"] == "ready"


async def test_long_outage_gap_and_skip_do_not_silently_advance_coverage(scheduled):
    command, collector = scheduled
    now = utc("2026-09-25T07:00:00")
    await tick(command, now)
    await tick(command, now + 900)
    await tick(command, now + 86400 + 7201)
    await tick(command, now + 5 * 86400)
    window = collector.call_args.kwargs["window"]
    assert window["since"] == now + 2 * 86400
    assert window["omitted_interval"] == {"since": now, "until": now + 2 * 86400}


async def test_disable_cancels_pending_preserves_final_readable_content(scheduled):
    command, _ = scheduled
    now = utc("2026-09-25T07:00:00")
    first = await tick(command, now)
    await tick(command, now + 900)
    second = await tick(command, now + 86400)
    command.orchestrator.config.reports.morning.enabled = False
    disabled = await tick(command, now + 86400 + 60)
    assert disabled["cancelled"] == 1
    assert (await command.db.get_morning_report(first["report_id"]))["state"] == "final"
    assert (await command.db.get_morning_report(second["report_id"]))["reason"] == "disabled"
    assert (await command._cmd_report_get({"report_id": first["report_id"]}))["success"]


async def test_report_reads_filter_fleet_content_and_reject_foreign_scope(scheduled):
    command, _ = scheduled
    command.orchestrator.config.reports.morning.project_ids = []
    first = await tick(command, utc("2026-09-25T07:00:00"))
    for project_id, allowed in (("p", True), ("q", False)):
        principal = ExecutionPrincipal(
            kind=PrincipalKind.SESSION, policy=DENY_ALL, project_id=project_id
        )
        with principal_context(principal):
            result = await command._cmd_report_get({"report_id": first["report_id"]})
            listed = await command._cmd_report_list({})
        assert result["success"] == allowed
        assert len(listed["reports"]) == int(allowed)
        if allowed:
            content = result["report"]["report"]
            assert content["global_facts"] == []
            assert content["summary"] == "Morning report for p."
            assert "config_snapshot" not in result["report"]
    worker = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL, project_id="p")
    with principal_context(worker):
        assert (await command._cmd_morning_report_tick({}))["error_code"] == "out_of_scope"


async def test_retention_preserves_coverage_and_only_prunes_terminal_reports(scheduled):
    command, _ = scheduled
    now = utc("2026-09-25T07:00:00")
    first = await tick(command, now)
    await tick(command, now + 900)
    assert await command.db.prune_morning_reports(now=now + 91 * 86400) == 1
    assert await command.db.get_morning_report(first["report_id"]) is None
    async with command.db._engine.connect() as conn:
        assert (await conn.execute(select(tables.morning_report_coverage))).all()
        assert not (await conn.execute(select(tables.morning_report_facts))).all()


async def test_fallback_is_bounded_evidence_linked_and_never_invents_manual_checks():
    from src.reports.fallback import MAX_REPORT_BYTES, build_fallback

    result, _, _ = await build()
    report = build_fallback(result["brief"])
    project = report["projects"][0]
    assert project["manual_checks"] == []
    assert {item["refs"][0] for item in project["pending"]} == {"completion:c2"}
    assert all(item["verification_label"] == "agent-reported" for item in project["landed"])
    assert len(json.dumps(report, ensure_ascii=False).encode()) <= MAX_REPORT_BYTES


def test_cli_stored_report_reads_use_registered_commands(monkeypatch):
    monkeypatch.setattr(
        "src.cli.reports._execute",
        lambda ctx, command, params: {"success": True, "command": command, **params},
    )
    runner = CliRunner()
    shown = runner.invoke(cli, ["--json", "report", "show", "morning-2026-09-25"])
    assert shown.exit_code == 0
    assert json.loads(shown.output)["data"]["command"] == "report_get"
    listed = runner.invoke(cli, ["--json", "report", "list", "--limit", "3"])
    assert listed.exit_code == 0
    assert json.loads(listed.output)["data"]["limit"] == 3


async def author_request(scheduled, now):
    command, collector = scheduled
    policy = command.orchestrator.config.reports.morning
    policy.project_ids = []
    policy.full_fleet_visibility = True
    policy.destination = "discord:123456789012345678"
    first = await tick(command, now)
    request = await command.db.get_report_request(f"report-morning-{first['report_id']}")
    return command, collector, request


async def test_author_request_reconciles_once_with_original_snapshot(scheduled):
    now = utc("2026-09-25T07:00:00")
    command, collector, request = await author_request(scheduled, now)
    assert request["state"] == "requested" and request["version"] == 2
    assert request["kind"] == "morning" and request["deadline"] == now + 900
    assert (
        request["brief_hash"]
        == (await command.db.get_morning_report(request["owner_ref"]))["brief_hash"]
    )
    # A fresh command object after a lost tick/event still uses the durable reservation.
    restarted = Commands(command.db)
    restarted.orchestrator = command.orchestrator
    for offset in (1, 30, 60):
        assert (await tick(restarted, now + offset))["state"] == "authoring"
    collector.assert_awaited_once()
    repeated = await command.db.get_report_request(request["id"])
    assert repeated == request
    async with command.db._engine.connect() as conn:
        messages = (
            (
                await conn.execute(
                    select(tables.messages).where(tables.messages.c.body_kind == "report_request")
                )
            )
            .mappings()
            .all()
        )
    assert len(messages) == 1
    assert messages[0]["id"] == f"msg-{request['id']}"
    assert "version 1 JSON" in messages[0]["body"]
    assert messages[0]["to_id"] == "supervisor-global"


async def test_request_recovered_after_reservation_before_message(scheduled, monkeypatch):
    command, _ = scheduled
    policy = command.orchestrator.config.reports.morning
    policy.project_ids = []
    policy.full_fleet_visibility = True
    policy.destination = "discord:123456789012345678"
    now = utc("2026-09-25T07:00:00")
    original = command.db.request_report
    failed = AsyncMock(side_effect=RuntimeError("lost request step"))
    monkeypatch.setattr(command.db, "request_report", failed)
    with pytest.raises(RuntimeError, match="lost request step"):
        await tick(command, now)
    request_id = "report-morning-morning-2026-09-25"
    assert (await command.db.get_report_request(request_id))["state"] == "reserved"
    monkeypatch.setattr(command.db, "request_report", original)
    assert (await tick(command, now + 60))["state"] == "authoring"
    assert (await command.db.get_report_request(request_id))["state"] == "requested"


async def test_structured_author_submit_finalizes_coverage_and_immutable_content(
    scheduled, monkeypatch
):
    from src.commands.principal import TRUSTED_LOCAL
    from src.reports.fallback import build_fallback

    now = utc("2026-09-25T07:00:00")
    command, collector, request = await author_request(scheduled, now)
    monkeypatch.setattr("src.commands.report_commands.time.time", lambda: now + 60)
    report = build_fallback(request["brief"])
    report["summary"] = "@everyone CLI changes are on main; the second PR is pending."
    # The author cannot upgrade agent-reported checks to manual verification.
    report["projects"][0]["landed"][0]["verification_label"] = "manually verified"
    args = dict(
        request_id=request["id"],
        brief_hash=request["brief_hash"],
        expected_version=2,
        text=json.dumps(report),
    )
    with principal_context(TRUSTED_LOCAL):
        result = await command._cmd_report_submit(args)
    assert result["success"] and result["state"] == "submitted"
    row = await command.db.get_morning_report(request["owner_ref"])
    assert row["state"] == "final" and row["reason"] == "authored"
    assert "@everyone" not in row["report"]["summary"]
    assert row["report"]["projects"][0]["landed"][0]["verification_label"] == "agent-reported"
    assert row["report"]["coverage"] == report["coverage"]
    final = row["report"]
    await tick(command, now + 900)
    with principal_context(TRUSTED_LOCAL):
        assert (await command._cmd_report_submit(args))["error_code"] == "report.closed"
    assert (await command.db.get_morning_report(request["owner_ref"]))["report"] == final
    async with command.db._engine.connect() as conn:
        covered = (await conn.execute(select(tables.morning_report_coverage))).mappings().all()
    assert all(row["covered_until"] == now for row in covered)
    collector.assert_awaited_once()


async def test_submit_deadline_race_has_one_immutable_winner(scheduled):
    import asyncio
    from src.reports.fallback import build_fallback

    now = utc("2026-09-25T07:00:00")
    command, _, request = await author_request(scheduled, now)
    report = build_fallback(request["brief"])
    report["summary"] = "Author winner"
    submitted, fallback = await asyncio.gather(
        command.db.submit_morning_report(
            request["id"],
            brief_hash=request["brief_hash"],
            expected_version=2,
            report=report,
            evidence_refs=[],
            source_links=[],
            now=now + 899,
        ),
        command.db.finalize_morning_fallback(request["owner_ref"], now=now + 900),
    )
    assert (submitted is None) != (fallback is None)
    row = await command.db.get_morning_report(request["owner_ref"])
    assert row["reason"] == ("authored" if submitted else "author_deadline")
    assert row["report"] == (report if submitted else row["fallback"])
    frozen_report = row["report"]
    assert await command.db.finalize_morning_fallback(row["id"], now=now + 901) is None
    assert (await command.db.get_morning_report(row["id"]))["report"] == frozen_report


async def test_deadline_fallback_closes_request_and_rejects_late_author(scheduled, monkeypatch):
    from src.commands.principal import TRUSTED_LOCAL
    from src.reports.fallback import build_fallback

    now = utc("2026-09-25T07:00:00")
    command, _, request = await author_request(scheduled, now)
    monkeypatch.setattr("src.commands.report_commands.time.time", lambda: now + 900)
    with principal_context(TRUSTED_LOCAL):
        late = await command._cmd_report_submit(
            dict(
                request_id=request["id"],
                brief_hash=request["brief_hash"],
                expected_version=2,
                text=json.dumps(build_fallback(request["brief"])),
            )
        )
    assert late["error_code"] == "report.closed"
    assert (await tick(command, now + 900))["state"] == "final"
    assert (await command.db.get_report_request(request["id"]))["state"] == "fallback"
    assert await command.db.request_report(request["id"], now=now + 901) is None


@pytest.mark.parametrize("scoped,declared", [(False, False), (True, True)])
async def test_restricted_or_undeclared_destination_never_wakes_global_author(
    scheduled, scoped, declared
):
    command, _ = scheduled
    policy = command.orchestrator.config.reports.morning
    policy.project_ids = ["p"] if scoped else []
    policy.full_fleet_visibility = declared
    policy.destination = "discord:123456789012345678"
    first = await tick(command, utc("2026-09-25T07:00:00"))
    assert first["state"] == "ready"
    async with command.db._engine.connect() as conn:
        assert not (await conn.execute(select(tables.supervisor_report_requests))).all()
        assert not (await conn.execute(select(tables.messages))).all()


async def test_disable_cancels_author_request_but_retains_readable_brief(scheduled):
    now = utc("2026-09-25T07:00:00")
    command, _, request = await author_request(scheduled, now)
    command.orchestrator.config.reports.morning.enabled = False
    disabled = await tick(command, now + 30)
    assert disabled["cancelled"] == 1
    row = await command.db.get_report_request(request["id"])
    assert row["state"] == "cancelled" and row["brief_hash"] == request["brief_hash"]
    assert await command.db.request_report(request["id"], now=now + 60) is None


async def test_morning_report_validation_requires_scoped_grounded_manual_checks():
    import copy
    from src.reports.authoring import validate_morning_report
    from src.reports.fallback import build_fallback

    brief = frozen("golden-brief")
    # Use a frozen known surface for the fixture, unknown projects normally have none.
    brief["surface_map"]["projects"]["p"] = ["CLI"]
    report = build_fallback(brief)
    report["summary"] = "CLI preview is available; another PR remains pending."
    check = dict(
        action="Open the report preview",
        surface="CLI",
        expected_result="Evidence is shown",
        reason="The morning preview changed",
        refs=["completion:c1"],
        prior_verification="Manually verified",
        confidence="medium",
    )
    report["projects"][0]["manual_checks"] = [check]
    validated, refs = validate_morning_report(json.dumps(report), brief)
    assert (
        validated["projects"][0]["manual_checks"][0]["prior_verification"] == "focused tests passed"
    )
    assert "completion:c1" in refs
    for field, value in [
        ("refs", ["completion:c2"]),
        ("refs", ["private:secret"]),
        ("surface", "Unknown app"),
        ("action", "https://attacker.invalid/test"),
    ]:
        broken = copy.deepcopy(report)
        broken["projects"][0]["manual_checks"][0][field] = value
        with pytest.raises(ValueError):
            validate_morning_report(json.dumps(broken), brief)
    broken = copy.deepcopy(report)
    broken["projects"][0]["manual_checks"] = [check] * 11
    with pytest.raises(ValueError, match="at most 10"):
        validate_morning_report(json.dumps(broken), brief)
    for field, value in [("path", "/tmp/report"), ("coverage", {"complete": True})]:
        broken = copy.deepcopy(report)
        broken[field] = value
        with pytest.raises(ValueError):
            validate_morning_report(json.dumps(broken), brief)
    broken = copy.deepcopy(report)
    broken["projects"][0]["id"] = "private-project"
    with pytest.raises(ValueError, match="project"):
        validate_morning_report(json.dumps(broken), brief)
    broken = copy.deepcopy(report)
    broken["summary"] = "x" * 32768
    with pytest.raises(ValueError, match="32 KiB"):
        validate_morning_report(json.dumps(broken), brief)


def test_known_surface_map_is_versioned_and_does_not_guess_for_unknown_repos():
    from src.reports.authoring import surface_map

    mapped = surface_map(
        {
            "agent-queue": {
                "diffstat": " src/cli/reports.py | 5 +++++\n dashboard/src/pages/reports/Page.tsx | 1 +"
            },
            "unknown": {"diffstat": " src/cli/reports.py | 5 +++++"},
        }
    )
    assert mapped == {
        "version": 1,
        "projects": {"agent-queue": ["CLI", "dashboard"], "unknown": []},
    }


@pytest.mark.parametrize("change", ["hash", "version", "disabled", "restricted", "destination"])
async def test_morning_submit_rejects_stale_or_changed_visibility(scheduled, monkeypatch, change):
    from src.commands.principal import TRUSTED_LOCAL
    from src.reports.fallback import build_fallback

    now = utc("2026-09-25T07:00:00")
    command, _, request = await author_request(scheduled, now)
    monkeypatch.setattr("src.commands.report_commands.time.time", lambda: now + 60)
    args = dict(
        request_id=request["id"],
        brief_hash=request["brief_hash"],
        expected_version=2,
        text=json.dumps(build_fallback(request["brief"])),
    )
    policy = command.orchestrator.config.reports.morning
    if change == "hash":
        args["brief_hash"] = "wrong-brief"
    elif change == "version":
        args["expected_version"] = 1
    elif change == "disabled":
        policy.enabled = False
    elif change == "restricted":
        policy.project_ids = ["p"]
    else:
        policy.destination = "discord:123456789012345679"
    with principal_context(TRUSTED_LOCAL):
        assert (await command._cmd_report_submit(args))["error_code"] == "report.closed"
    assert (await command.db.get_morning_report(request["owner_ref"]))["report"] is None


async def test_only_current_global_supervisor_launch_can_read_and_submit_morning(
    scheduled, monkeypatch
):
    from src.reports.fallback import build_fallback

    now = utc("2026-09-25T07:00:00")
    command, _, request = await author_request(scheduled, now)
    monkeypatch.setattr("src.commands.report_commands.time.time", lambda: now + 60)
    monkeypatch.setattr(
        command.db,
        "get_session",
        AsyncMock(
            return_value=SimpleNamespace(
                id="supervisor-global", lifecycle="named", instance_token="current-launch"
            )
        ),
    )
    for session_id, instance_token in [
        ("supervisor-global", "replaced-launch"),
        ("other-supervisor", "current-launch"),
    ]:
        principal = ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            policy=DENY_ALL,
            session_id=session_id,
            session_instance_token=instance_token,
        )
        with principal_context(principal):
            assert (await command._cmd_report_brief({"request_id": request["id"]}))[
                "error_code"
            ] == "out_of_scope"
            assert (await command._cmd_report_submit({"request_id": request["id"]}))[
                "error_code"
            ] == "out_of_scope"
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="supervisor-global",
        session_instance_token="current-launch",
    )
    with principal_context(principal):
        brief = await command._cmd_report_brief({"request_id": request["id"], "limit": 1})
        assert brief["success"] and brief["brief"]["kind"] == "morning"
        assert len(brief["facts"]) == 1 and brief["total_facts"] > 1
        result = await command._cmd_report_submit(
            dict(
                request_id=request["id"],
                brief_hash=brief["brief_hash"],
                expected_version=brief["version"],
                text=json.dumps(build_fallback(request["brief"])),
            )
        )
    assert result["success"]


def test_morning_bundle_is_optional_and_uses_only_the_tick_command():
    from src.playbooks.definition import load_definition_json
    from src.playbooks.required import DEFAULT_SYSTEM_PLAYBOOK_IDS, REQUIRED_SYSTEM_PLAYBOOK_IDS

    bundle = Path("src/prompts/reviewed_playbooks/morning-report")
    artifact = load_definition_json((bundle / "artifact.json").read_text())
    assert artifact.rules[0].trigger.event_type == "timer.1m"
    command_steps = [step for step in artifact.steps.values() if step.type == "command"]
    assert len(command_steps) == 1 and command_steps[0].command == "morning_report_tick"
    assert {step.type for step in artifact.steps.values()} == {"command", "terminal"}
    assert artifact.id not in DEFAULT_SYSTEM_PLAYBOOK_IDS + REQUIRED_SYSTEM_PLAYBOOK_IDS


def morning_dispatcher(command, transport, clock, **kwargs):
    from src.digest import DigestScheduleService

    return DigestScheduleService(
        command.db,
        transport,
        config=command.orchestrator.config,
        lease_owner="morning-test",
        clock=clock,
        include_outbound=True,
        links=getattr(command.orchestrator, "dashboard_links", None),
        **kwargs,
    )


async def morning_delivery(command, report_id):
    rows = await command.db.list_outbound_deliveries(owner_kind="morning", owner_id=report_id)
    assert len(rows) == 1
    return rows[0]


async def test_failed_morning_transport_retries_frozen_report_without_reauthoring(scheduled):
    from src.escalations.transport import SinkTransport, TransportUnavailable
    from src.remote_links import DashboardLink
    from tests.test_report_requests import Clock

    now = utc("2026-09-25T07:00:00")
    command, collector, request = await author_request(scheduled, now)
    command.orchestrator.dashboard_links = SimpleNamespace(
        resolve=AsyncMock(return_value=DashboardLink(url="https://dashboard.example.test"))
    )
    await tick(command, now + 900)
    original = await command.db.get_morning_report(request["owner_ref"])
    outbound = await morning_delivery(command, original["id"])
    assert outbound["state"] == "pending"
    assert f"https://dashboard.example.test/reports/{original['id']}" in outbound["payload"]["text"]
    clock, transport = Clock(now + 900), SinkTransport()
    transport.faults.append(("post_root", TransportUnavailable("SEND_MESSAGES missing")))
    pump = morning_dispatcher(command, transport, clock)
    await pump.tick()  # Morning deliveries continue while hourly digest is disabled.
    retry = await morning_delivery(command, original["id"])
    assert retry["state"] == "retry" and retry["attempt_count"] == 1
    await tick(command, now + 901)
    assert await command.db.get_morning_report(original["id"]) == original
    clock.now = retry["due_at"]
    # A new daemon uses the same frozen payload, marker and destination.
    await morning_dispatcher(command, transport, clock).pump()
    sent = await morning_delivery(command, original["id"])
    assert sent["state"] == "sent" and sent["attempt_count"] == 2
    assert sent["payload_hash"] == outbound["payload_hash"]
    assert sent["marker"] == outbound["marker"]
    assert len(transport.messages) == 1
    message = next(iter(transport.messages.values()))
    assert message.where == "123456789012345678"
    assert len(message.content) <= 1500 and message.content.endswith(outbound["marker"])
    collector.assert_awaited_once()
    async with command.db._engine.connect() as conn:
        assert len((await conn.execute(select(tables.supervisor_report_requests))).all()) == 1
        coverage = (await conn.execute(select(tables.morning_report_coverage))).mappings().all()
    assert all(cursor["covered_until"] == now for cursor in coverage)


@pytest.mark.parametrize("landed", [True, False])
async def test_ambiguous_morning_transport_never_resubmits_or_blind_reposts(scheduled, landed):
    from src.escalations.transport import SinkTransport, TransportAmbiguous
    from tests.test_report_requests import Clock

    now = utc("2026-09-25T07:00:00")
    command, collector, request = await author_request(scheduled, now)
    await tick(command, now + 900)
    clock, transport = Clock(now + 900), SinkTransport()

    async def ambiguous(*, channel_id, content):
        if landed:
            transport.record(channel_id, content)
        raise TransportAmbiguous("ack lost")

    transport.post_root = ambiguous
    await morning_dispatcher(command, transport, clock).pump()
    outbound = await morning_delivery(command, request["owner_ref"])
    assert outbound["state"] == ("sent" if landed else "unknown")
    await tick(command, now + 901)
    clock.now += 3600
    await morning_dispatcher(command, transport, clock).pump()
    assert (await morning_delivery(command, request["owner_ref"]))["attempt_count"] == 1
    assert len(transport.messages) == int(landed)
    collector.assert_awaited_once()


async def test_finalization_crash_before_outbox_reservation_recovers_once(scheduled):
    import asyncio
    from src.escalations.transport import SinkTransport
    from tests.test_report_requests import Clock

    now = utc("2026-09-25T07:00:00")
    command, collector, request = await author_request(scheduled, now)
    finalized = await command.db.finalize_morning_fallback(request["owner_ref"], now=now + 900)
    assert finalized["state"] == "final"
    assert not await command.db.list_outbound_deliveries(
        owner_kind="morning", owner_id=finalized["id"]
    )
    await asyncio.gather(tick(command, now + 901), tick(command, now + 901))
    outbound = await morning_delivery(command, finalized["id"])
    assert outbound["state"] == "pending"
    transport = SinkTransport()
    await morning_dispatcher(command, transport, Clock(now + 901)).pump()
    assert len(transport.messages) == 1
    assert await command.db.get_morning_report(finalized["id"]) == finalized
    collector.assert_awaited_once()


@pytest.mark.parametrize("change", ["disabled", "projects", "visibility", "destination"])
async def test_morning_policy_change_cancels_pending_request_and_queued_wake(scheduled, change):
    now = utc("2026-09-25T07:00:00")
    command, _, request = await author_request(scheduled, now)
    policy = command.orchestrator.config.reports.morning
    if change == "disabled":
        policy.enabled = False
    elif change == "projects":
        policy.project_ids = ["p"]
    elif change == "visibility":
        policy.full_fleet_visibility = False
    else:
        policy.destination = "discord:123456789012345679"
    await tick(command, now + 60)
    closed = await command.db.get_report_request(request["id"])
    assert closed["state"] == "cancelled"
    assert closed["skip_reason"] == ("disabled" if change == "disabled" else "visibility_changed")
    assert not await command.db.request_report(request["id"], now=now + 61)
    async with command.db._engine.connect() as conn:
        message = (
            (
                await conn.execute(
                    select(tables.messages).where(
                        tables.messages.c.id == request["request_message_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
    assert message["delivered_at"] is None and message["archived_at"] == now + 60
    assert (await command.db.get_morning_report(request["owner_ref"]))["state"] == "skipped"


@pytest.mark.parametrize("change", ["disabled", "projects", "visibility", "destination"])
async def test_policy_change_before_delivery_cancels_authored_payload_preserves_content(
    scheduled, change
):
    from src.escalations.transport import SinkTransport
    from src.reports.fallback import build_fallback
    from tests.test_report_requests import Clock

    now = utc("2026-09-25T07:00:00")
    command, _, request = await author_request(scheduled, now)
    report = build_fallback(request["brief"])
    report["summary"] = "Sensitive full-fleet author text."
    await command.db.submit_morning_report(
        request["id"],
        brief_hash=request["brief_hash"],
        expected_version=2,
        report=report,
        evidence_refs=[],
        source_links=[],
        now=now + 60,
    )
    await tick(command, now + 61)
    original = await command.db.get_morning_report(request["owner_ref"])
    outbound = await morning_delivery(command, request["owner_ref"])
    policy = command.orchestrator.config.reports.morning
    if change == "disabled":
        policy.enabled = False
    elif change == "projects":
        policy.project_ids = ["p"]
    elif change == "visibility":
        policy.full_fleet_visibility = False
    else:
        policy.destination = "discord:123456789012345679"
    # The shared pump enforces policy even before the next morning minute tick.
    transport = SinkTransport()
    await morning_dispatcher(command, transport, Clock(now + 62)).pump()
    cancelled = await morning_delivery(command, request["owner_ref"])
    assert cancelled["state"] == "cancelled" and cancelled["attempt_count"] == 0
    assert cancelled["payload_hash"] == outbound["payload_hash"]
    assert await command.db.get_morning_report(request["owner_ref"]) == original
    assert not transport.messages
    policy.enabled = True
    policy.project_ids = []
    policy.full_fleet_visibility = True
    policy.destination = request["destination"]
    await morning_dispatcher(command, transport, Clock(now + 63)).pump()
    assert not transport.messages  # Cancellation survives restoring the old configuration.


async def test_visibility_change_after_claim_is_checked_again_before_transport(
    scheduled, monkeypatch
):
    from src.escalations.transport import SinkTransport
    from tests.test_report_requests import Clock

    now = utc("2026-09-25T07:00:00")
    command, _, request = await author_request(scheduled, now)
    await tick(command, now + 900)
    original = command.db.claim_report_deliveries

    async def narrowed_after_claim(**kwargs):
        rows = await original(**kwargs)
        command.orchestrator.config.reports.morning.project_ids = ["p"]
        return rows

    monkeypatch.setattr(command.db, "claim_report_deliveries", narrowed_after_claim)
    transport = SinkTransport()
    await morning_dispatcher(command, transport, Clock(now + 900)).pump()
    assert not transport.messages
    assert (await morning_delivery(command, request["owner_ref"]))["state"] == "cancelled"


async def test_disable_in_finalization_reservation_gap_leaves_durable_cancellation(scheduled):
    from src.escalations.transport import SinkTransport
    from tests.test_report_requests import Clock

    now = utc("2026-09-25T07:00:00")
    command, _, request = await author_request(scheduled, now)
    await command.db.finalize_morning_fallback(request["owner_ref"], now=now + 900)
    command.orchestrator.config.reports.morning.enabled = False
    await tick(command, now + 901)
    assert (await morning_delivery(command, request["owner_ref"]))["state"] == "cancelled"
    command.orchestrator.config.reports.morning.enabled = True
    transport = SinkTransport()
    await morning_dispatcher(command, transport, Clock(now + 902)).pump()
    assert not transport.messages


async def test_scoped_fallback_delivers_without_supervisor_and_summary_budget_includes_link(
    scheduled,
):
    from src.escalations.transport import SinkTransport
    from src.remote_links import DashboardLink
    from src.reports.delivery import render_summary
    from tests.test_report_requests import Clock

    command, collector = scheduled
    policy = command.orchestrator.config.reports.morning
    policy.destination = "discord:123456789012345678"
    command.orchestrator.dashboard_links = SimpleNamespace(
        resolve=AsyncMock(return_value=DashboardLink(url="https://dashboard.example.test"))
    )
    now = utc("2026-09-25T07:00:00")
    first = await tick(command, now)
    await tick(command, now + 900)
    row = await command.db.get_morning_report(first["report_id"])
    transport = SinkTransport()
    await morning_dispatcher(command, transport, Clock(now + 900)).pump()
    assert (await morning_delivery(command, row["id"]))["state"] == "sent"
    async with command.db._engine.connect() as conn:
        assert not (await conn.execute(select(tables.supervisor_report_requests))).all()
    # Coverage warnings and the server link cannot be truncated by a long author summary.
    row["report"]["summary"] = "@everyone " + "x" * 3000
    row["report"]["coverage"]["complete"] = False
    row["report"]["coverage"]["window"]["omitted_interval"] = {"since": 0, "until": 1}
    text = render_summary(row, url="https://dashboard.example.test", notice="")
    assert "Partial coverage" in text and "Lookback capped" in text
    assert text.endswith(f"https://dashboard.example.test/reports/{row['id']}")
    assert "@everyone" not in text
    # Same marker size the shared primitive appends.
    assert len(text) + 1 + len("aq-out:" + "a" * 16) <= 1500
    collector.assert_awaited_once()


async def test_cancelled_delivery_migration_upgrades_old_constraint_idempotently(db):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = Path("migrations/versions/a00000000033_outbound_cancellation.py")
    spec = importlib.util.spec_from_file_location("outbound_cancel_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    def verify(connection):
        with Operations.context(MigrationContext.configure(connection)):
            from alembic import op

            op.drop_constraint("ck_outbound_deliveries_state", "outbound_deliveries", type_="check")
            op.create_check_constraint(
                "ck_outbound_deliveries_state",
                "outbound_deliveries",
                "state IN ('pending','sending','sent','retry','unknown')",
            )
            revision.upgrade()
            revision.upgrade()
        check = next(
            c
            for c in inspect(connection).get_check_constraints("outbound_deliveries")
            if c["name"] == "ck_outbound_deliveries_state"
        )
        assert "cancelled" in check["sqltext"]

    async with db._engine.begin() as conn:
        await conn.run_sync(verify)


async def test_no_configured_channel_never_reserves_author_or_delivery(scheduled):
    command, _ = scheduled
    policy = command.orchestrator.config.reports.morning
    policy.project_ids = []
    policy.full_fleet_visibility = True
    now = utc("2026-09-25T07:00:00")
    first = await tick(command, now)
    await tick(command, now + 900)
    assert (await command.db.get_morning_report(first["report_id"]))["state"] == "final"
    async with command.db._engine.connect() as conn:
        assert not (await conn.execute(select(tables.supervisor_report_requests))).all()
        assert not (await conn.execute(select(tables.outbound_deliveries))).all()
        assert not (await conn.execute(select(tables.messages))).all()


async def test_project_scoped_read_uses_fallback_instead_of_global_authored_prose(scheduled):
    from src.reports.fallback import build_fallback

    now = utc("2026-09-25T07:00:00")
    command, _, request = await author_request(scheduled, now)
    report = build_fallback(request["brief"])
    report["summary"] = "Private project Q is launching."
    report["projects"][0]["landed"][0]["text"] = "Private project Q launch details."
    await command.db.submit_morning_report(
        request["id"],
        brief_hash=request["brief_hash"],
        expected_version=2,
        report=report,
        evidence_refs=[],
        source_links=[],
        now=now + 60,
    )
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL, project_id="p")
    with principal_context(principal):
        read = await command._cmd_report_get({"report_id": request["owner_ref"]})
    assert read["success"] and read["report"]["is_fallback"]
    assert "Private project Q" not in json.dumps(read)
    assert read["report"]["report"]["projects"][0]["manual_checks"] == []
    assert read["report"]["report"]["summary"] == "Morning report for p."
