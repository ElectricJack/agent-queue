"""Checkpoint and fresh-prime boundary, without enabling stop/launch services."""

import asyncio
import hashlib
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import insert, select, update

from src.commands import CommandHandler
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import AppConfig
from src.database import Database
from src.integration.admission import observe_admission
from src.database.queries.continuation_queries import (
    checkpoint_continuation,
    continuation_prime_context,
    degrade_checkpoint,
    wait_outcome_envelope,
)
from src.database.queries.wait_queries import register_wait
from src.database.tables import (
    continuations, messages, sessions, tasks, wait_listener_cursors, wait_records, wait_source_events,
)
from src.models import Project, SessionRecord, Task, WorktreeSentinel
from src.prime import PrimeRenderer
from src.prime.budgets import CONTINUATION_COMMENTS_BYTES
from src.prime.hook_envelopes import wrap
from src.profiles.capabilities import CapabilityPolicy
from src.waits.conditions import SleepDecision, WaitError
from src.waits.registry import build_registry
from tests.db_fixtures import lease_dsn
from tests.test_wait_queries import NOW, Policy, owner


@pytest.fixture
async def env(request):
    db = Database(lease_dsn("wait-checkpoints"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    scope = await owner(db)
    await db.create_task(Task(id="source", project_id="p", title="Source", description=""))
    condition = {"kind": "task", "task_id": "source"}
    message_wait = getattr(request, "param", None) == "message"
    if message_wait:
        question = await db.create_message(
            project_id="p", from_kind="session", from_id="s", to_kind="session",
            to_id="supervisor-p", thread_id="interview-q4", body="Question",
        )
        condition = {"kind": "message", "thread_id": question.thread_id,
                     "after_seq": question.created_seq}
    watermark = await db.wait_source_watermark()
    async with db._engine.begin() as conn:
        result = await register_wait(
            conn,
            scope=scope,
            condition=condition,
            idempotency_key="hibernate",
            registry=build_registry(clock=lambda: NOW),
            policy=Policy(),
            decide_sleep=lambda *_: SleepDecision("hibernate", "duration", {}),
            now=NOW,
            source_watermark=watermark,
        )
        await conn.execute(
            insert(continuations).values(
                id="c",
                task_id="owner",
                wait_id=result["wait_id"],
                suspension_generation=1,
                facts={},
                branch="aq/owner",
                prior_status="IN_PROGRESS",
                held_cursor_seq=0,
                resume_state="sleeping",
                created_at=NOW,
            )
        )
    context = SimpleNamespace(db=db, scope=scope, wait_id=result["wait_id"])
    try:
        yield context
    finally:
        if getattr(context, 'legacy_null_tokens', False):
            async with db._engine.begin() as conn:
                await conn.execute(update(sessions).where(sessions.c.instance_token.is_(None)).values(
                    instance_token='restored-test-token'))
                await conn.exec_driver_sql(
                    'ALTER TABLE sessions ALTER COLUMN instance_token SET NOT NULL')
        await db.close()


async def intent(env):
    async with env.db._engine.connect() as conn:
        return dict((await conn.execute(select(continuations))).mappings().one())


async def capture(env, summary="Goal: resume the exact checked tree.", **kwargs):
    values = dict(scope=env.scope, wait_id=env.wait_id, summary=summary, facts={"head": "abc"})
    values.update(kwargs)
    async with env.db._engine.begin() as conn:
        return await checkpoint_continuation(conn, **values)


async def fallback(env, **kwargs):
    values = dict(
        task_id="owner",
        wait_id=env.wait_id,
        continuation_id="c",
        generation=1,
        now=NOW + 120,
        transition_budget_seconds=120,
        facts={"head": "server"},
    )
    values.update(kwargs)
    async with env.db._engine.begin() as conn:
        return await degrade_checkpoint(conn, **values)


@pytest.mark.parametrize(
    "summary",
    ["x" * 8193, "é" * 4097, " ", ""],
    ids=["ascii-overflow", "utf8-overflow", "blank", "empty"],
)
async def test_invalid_summary_never_mutates(env, summary):
    with pytest.raises(WaitError, match="1–8192"):
        await capture(env, summary)
    row = await intent(env)
    assert row["summary"] is None and row["version"] == 1


@pytest.mark.parametrize(
    "summary", ["é" * 4096, "next\r\ncheck the exact tree\r\n"], ids=["byte-boundary", "crlf"]
)
async def test_exact_bytes_hash_and_replay(env, summary):
    first = await capture(env, summary)
    second = await capture(env, summary, facts={"head": "changed"})
    assert first == second
    row = await intent(env)
    assert row["summary"] == summary
    assert row["summary_sha256"] == hashlib.sha256(summary.encode()).hexdigest()
    assert row["facts"] == {"head": "abc"} and row["version"] == 2
    with pytest.raises(WaitError, match="already captured"):
        await capture(env, "replacement")


@pytest.mark.parametrize(
    "kwargs", [dict(generation=2), dict(continuation_id="foreign"), dict(scope=None)]
)
async def test_stale_generation_or_unbound_holder_is_refused(env, kwargs):
    with pytest.raises(WaitError):
        await capture(env, **kwargs)
    assert (await intent(env))["summary"] is None


@pytest.mark.parametrize("mutation", ["epoch", "instance", "paused", "stopped", "in_place"])
async def test_transaction_repeats_holder_and_suspension_fences(env, mutation):
    async with env.db._engine.begin() as conn:
        if mutation == "epoch":
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
        elif mutation == "instance":
            await conn.execute(
                update(sessions).where(sessions.c.id == "s").values(instance_token="new")
            )
        elif mutation == "paused":
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(status="PAUSED"))
        elif mutation == "stopped":
            await conn.execute(update(continuations).values(stopped_at=NOW + 10))
        else:
            await conn.execute(update(wait_records).values(sleep_decision="in_place"))
    with pytest.raises(WaitError):
        await capture(env)
    assert (await intent(env))["summary"] is None


async def test_deadline_fallback_is_facts_only_labelled_and_immutable(env):
    with pytest.raises(WaitError, match="has not expired"):
        await fallback(env, now=NOW + 119)
    await fallback(env)
    await fallback(env, facts={"head": "later"})
    row = await intent(env)
    assert row["summary_degraded"] and row["summary"] is None and row["summary_sha256"] is None
    assert row["facts"] == {"head": "server"} and row["version"] == 2
    with pytest.raises(WaitError, match="deadline already committed"):
        await capture(env)


async def test_checkpoint_deadline_race_has_one_immutable_winner(env):
    results = await asyncio.gather(capture(env), fallback(env), return_exceptions=True)
    row = await intent(env)
    assert row["version"] == 2
    assert (row["summary"] is None) == row["summary_degraded"]
    assert any(isinstance(r, dict) and r["success"] for r in results)
    if row["summary"] is not None:
        assert row["facts"] == {"head": "abc"}


async def test_fallback_preserves_submitted_summary_and_refuses_stale_generation(env):
    await capture(env)
    with pytest.raises(WaitError):
        await fallback(env, generation=2)
    await fallback(env)
    assert not (await intent(env))["summary_degraded"]


@pytest.mark.parametrize("outcome", ["satisfied", "lifted_by_supervisor", "source_unavailable"])
async def test_resolution_during_capture_preserves_checkpoint_for_queued_resume(env, outcome):
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome=outcome))
        await conn.execute(update(continuations).values(resume_state="queued"))
    await capture(env)
    assert (await intent(env))["summary"] is not None


async def test_eviction_transition_budget_uses_intent_time_and_reclaimed_owner_is_refused(env):
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(created_at=NOW + 300))
    with pytest.raises(WaitError, match="has not expired"):
        await fallback(env, now=NOW + 419)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
    with pytest.raises(WaitError, match="owner changed"):
        await fallback(env, now=NOW + 420)
    assert not (await intent(env))["summary_degraded"]


async def test_checkpoint_remains_available_during_flag_rollback(env):
    handler = CommandHandler(
        SimpleNamespace(
            db=env.db,
            bus=SimpleNamespace(emit=AsyncMock()),
            plugin_registry=None,
            wait_v2_handlers={},
        ),
        AppConfig(),
    )
    handler.config.wait_policy = SimpleNamespace(v2_enabled=False)
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["wait_checkpoint"]),
        session_id="s",
        session_instance_token="token",
        task_id="owner",
        project_id="p",
        profile_id="worker-codex",
    )
    with principal_context(principal):
        args = dict(wait_id=env.wait_id, summary="next: inspect result", claim_epoch=1)
        result = await handler.execute("wait_checkpoint", args)
    assert result["success"], result
    facts = (await intent(env))["facts"]
    assert facts["task_id"] == "owner" and facts["last_comment"] is None
    assert facts["open_subtasks"] == []


async def resumed(env, *, degraded=False, outcome="satisfied"):
    if degraded:
        await fallback(env)
    else:
        await capture(env, "resume-summary: run the pending verification")
    await env.db.create_session(
        SessionRecord(
            id="new",
            project_id="p",
            profile_id="worker-codex",
            task_id="owner",
            harness="codex",
            provider="fake",
            name="fresh",
            lifecycle="task",
            state="running",
            work_dir="/tmp/fresh-wait-session",
            epoch="boot",
            instance_token="new-token",
            started_at=NOW + 300,
        )
    )
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(continuations).values(new_session_id="new", resume_state="delivered")
        )
        await conn.execute(
            update(wait_records).values(outcome=outcome, result_ref="task:source:COMPLETED")
        )
    return PrimeRenderer(env.db, AppConfig())


async def mail(env, ident, *, to_kind="session", to_id="wait-s", **kwargs):
    values = dict(
        id=ident,
        project_id="p",
        from_kind="system",
        from_id="test",
        to_kind=to_kind,
        to_id=to_id,
        body=f"held-body-{ident}",
        created_at=NOW + 200,
    )
    values.update(kwargs)
    async with env.db._engine.begin() as conn:
        await conn.execute(insert(messages).values(**values))


@pytest.mark.parametrize("degraded", [False, True])
@pytest.mark.parametrize("outcome", ["satisfied", "cancelled"])
async def test_resume_prime_outcome_summary_ticket_all_comments_and_held_mail(
    env, tmp_path, degraded, outcome
):
    renderer = await resumed(env, degraded=degraded, outcome=outcome)
    for n in range(12):
        await env.db.add_task_comment(
            "owner", f"comment-{n:02}: {'内容' * 800}", author_kind="user", author_id="human"
        )
    await mail(env, "old-session")
    await mail(env, "task-mail", to_kind="task", to_id="owner")
    await mail(env, "profile-mail", to_kind="profile", to_id="worker-codex")
    await mail(env, "foreign", to_id="someone-else")
    await mail(env, "wake", body_kind="wait_outcome")
    doc = await renderer.render_for_task(
        "owner", session_id="new", work_dir=str(tmp_path), mark_messages_delivered=True
    )
    body = doc.to_markdown()
    assert (
        body.index("## Wait outcome")
        < body.index("## Compaction summary")
        < body.index("## Task\n")
    )
    assert body.index("comment-00") < body.index("comment-11") < body.index("Held while waiting")
    assert all(f"comment-{n:02}" in body for n in range(12))
    assert ("summary_degraded" in body) == degraded
    assert "Server facts captured" in body and "task:source:COMPLETED" in body
    assert f"aq wait ack --wait-id {env.wait_id} --continuation-id c --generation 1" in body
    assert "then `aq prime` again" in body
    if outcome == "cancelled":
        assert "The wait was cancelled; you still hold the task claim" in body
        assert "Acknowledge this cancelled outcome before continuing" in body
        assert "do not stop or exit" in body
    assert (
        "held-body-old-session" in body
        and "held-body-task-mail" in body
        and "held-body-profile-mail" in body
    )
    assert "held-body-foreign" not in body and "held-body-wake" not in body
    async with env.db._engine.connect() as conn:
        assert all(row[0] is None for row in await conn.execute(select(messages.c.delivered_at)))
    assert "## Wait outcome" in wrap(body, "codex")


async def test_resume_comments_budget_trims_oldest_preserves_newest_and_pointer(env, tmp_path):
    renderer = await resumed(env)
    await env.db.add_task_context(
        "owner", type="note", label="Pageable context", content="ordinary context " * 1000,
    )
    for n in range(18):
        await env.db.add_task_comment(
            "owner", f"history-{n:02}: {'é' * 3000}", author_kind="user", author_id="human"
        )
    doc = await renderer.render_for_task("owner", session_id="new", work_dir=str(tmp_path))
    section = next(s for s in doc.sections if s.key == "task_context")
    context = section.body
    comments = context[context.index("**Task comments") :]
    assert len(comments.encode()) <= CONTINUATION_COMMENTS_BYTES
    assert "history-17" in comments and "history-00" not in comments
    assert "Older comments truncated" in comments and "aq task comments owner" in comments
    assert "\ufffd" not in comments
    # Aggregate fitting can shrink ordinary context without changing the
    # continuation's separately budgeted, chronological comment history.
    assert section.preview is not None
    reduced = section.preview.render("owner", 0)
    assert "--field prime_context" in reduced
    assert comments in reduced
    assert "ordinary context" not in reduced


async def test_resume_includes_every_page_of_comments_posted_while_asleep(env, tmp_path):
    renderer = await resumed(env)
    for n in range(205):
        await env.db.add_task_comment("owner", f"asleep-page-{n:03}",
                                      author_kind="user", author_id="human")
    body = (await renderer.render_for_task("owner", session_id="new", work_dir=str(tmp_path))).to_markdown()
    assert all(f"asleep-page-{n:03}" in body for n in range(205))
    assert body.index("asleep-page-000") < body.index("asleep-page-204")


async def test_legacy_override_cannot_drop_the_resume_contract(env, tmp_path):
    renderer = await resumed(env)
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "PRIME.md").write_text("Local extra rule. {{task}}")
    body = (
        await renderer.render_for_task("owner", session_id="new", work_dir=str(tmp_path))
    ).to_markdown()
    assert (
        body.index("## Wait outcome")
        < body.index("## Compaction summary")
        < body.index("## Task\n")
    )
    assert body.count("**id:** owner") == 1 and "Local extra rule" in body


async def test_foreign_session_and_project_cannot_read_continuation_or_mail(env):
    await resumed(env)
    async with env.db._engine.connect() as conn:
        assert (
            await continuation_prime_context(conn, task_id="owner", project_id="p", session_id="s")
            is None
        )
        assert (
            await continuation_prime_context(
                conn, task_id="owner", project_id="q", session_id="new"
            )
            is None
        )


def test_in_place_envelope_has_same_keys_without_summary_and_strips_server_context():
    row = {
        "id": "w",
        "outcome": "cancelled",
        "result_ref": None,
        "condition": {"kind": "task", "task_id": "t", "_verified": {"private": "binding"}},
    }
    envelope = wait_outcome_envelope(row)
    assert envelope == {
        "kind": "wait_outcome",
        "wait_id": "w",
        "continuation_generation": None,
        "outcome": "cancelled",
        "condition": {"kind": "task", "task_id": "t"},
        "result_ref": None,
        "decision": None,
        "summary_ref": None,
        "held_messages": [],
        "next_action": "ack_then_continue",
    }
    with pytest.raises(WaitError):
        wait_outcome_envelope({**row, "outcome": "pending"})


@pytest.mark.parametrize(
    "raw", [b"x" * 8193, b"\xff", b"  "], ids=["overflow", "encoding", "blank"]
)
def test_summary_file_refused_before_network(tmp_path, monkeypatch, raw):
    from click.testing import CliRunner
    import src.cli.waits as cli_waits

    calls = []
    monkeypatch.setattr(cli_waits, "_execute", lambda *args: calls.append(args))
    path = tmp_path / "summary"
    path.write_bytes(raw)
    result = CliRunner().invoke(
        cli_waits.wait, ["checkpoint", "--wait-id", "w", "--summary-file", str(path)]
    )
    assert result.exit_code != 0 and "wait.checkpoint_invalid" in result.output
    assert not calls


def test_summary_file_preserves_exact_newlines(tmp_path, monkeypatch):
    from click.testing import CliRunner
    import src.cli.waits as cli_waits

    calls = []

    def execute(_ctx, _name, params):
        calls.append(params)
        return {"success": True, "continuation_id": "c", "generation": 1}

    monkeypatch.setattr(cli_waits, "_execute", execute)
    path = tmp_path / "summary"
    path.write_bytes(b"goal\r\nnext\r\n")
    result = CliRunner().invoke(
        cli_waits.wait, ["checkpoint", "--wait-id", "w", "--summary-file", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["summary"] == "goal\r\nnext\r\n"


async def park(env, **overrides):
    values = dict(task_id='owner', wait_id=env.wait_id, continuation_id='c', generation=1,
                  session_id='s', instance_token='token', claim_epoch=1,
                  saved_head='a' * 40, now=NOW + 20)
    values.update(overrides)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            branch_name='aq/owner'))
        return await env.db.park_task_for_continuation(conn, **values)


@pytest.mark.parametrize("removal", ["delete", "archive"])
@pytest.mark.parametrize("outcome", ["pending", "satisfied"])
async def test_task_removal_tombstones_waits_and_retains_audit(env, removal, outcome):
    from src.database.tables import (
        admission_reservations, escalations, wait_deadlines, wait_edges, wait_priority_projection,
    )
    from src.models import TaskStatus
    from src.wait_runtime.hibernate import HibernationTransaction, locked_suspension
    from src.wait_runtime.resume import ResumeTransaction
    from tests.test_wait_decisions import service_for
    from tests.test_wait_queries import records

    await capture(env)
    await park(env)
    await env.db.update_task("owner", priority=20)
    env.registry = build_registry(clock=lambda: NOW)
    service = service_for(env, [NOW + 30])
    if outcome == "satisfied":
        await env.db.transition_task("source", TaskStatus.COMPLETED)
        assert await service.tick()
    before = (await records(env))[0]
    async with env.db._engine.begin() as conn:
        task, wait, continuation = await locked_suspension(conn, env.wait_id)
        assert (await HibernationTransaction(service).hold(
            conn, task, wait, continuation, "push_failed", service.orch._command_handler,
        ))["held"]
    checkpoint = await intent(env)
    async with env.db._engine.begin() as conn:
        # Cover the third new task FK holder as well as both reservation subjects.
        await conn.execute(insert(wait_priority_projection).values(
            task_id="owner", donor_path=[], graph_version=1, computed_at=NOW))
        for subject in ({"task_id": "owner"}, {"continuation_id": "c"}):
            await conn.execute(insert(admission_reservations).values(
                id=next(iter(subject)), project_id="p", **subject, scope={},
                effective_priority=20, provenance="resume", deadline_at=NOW + 300,
                state="granted", created_at=NOW))
        await conn.execute(insert(admission_reservations).values(
            id="historical", project_id="p", task_id="owner", scope={},
            effective_priority=20, provenance="branch_blocker", deadline_at=NOW,
            state="consumed", created_at=NOW, consumed_by_session_id="s"))
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(status="PAUSED"))
    if removal == "delete":
        await env.db.delete_task("owner")
    else:
        assert await env.db.archive_task("owner", archive_reason="Retire suspended work")
        assert await env.db.get_archived_task("owner")
    assert await env.db.get_task("owner") is None
    row = await intent(env)
    assert row["task_id"] == "owner" and row["resume_state"] == "cancelled"
    for key in ("summary", "summary_sha256", "facts", "saved_head", "suspension_generation"):
        assert row[key] == checkpoint[key]
    wait = (await records(env))[0]
    assert wait["outcome"] == ("cancelled" if outcome == "pending" else outcome)
    if outcome != "pending":
        for key in ("outcome", "resolved_at", "result_ref"):
            assert wait[key] == before[key]
    assert wait["cleared_at"] is not None and not wait["blocking"]
    incident = (await records(env, escalations))[0]
    assert incident["id"] == checkpoint["facts"]["hold_escalation_id"]
    assert incident["state"] == "cancelled" and incident["terminal_outcome"] == "cancelled"
    assert not any(edge["active"] for edge in await records(env, wait_edges))
    assert not any(d["fired_at"] is None and d["cancelled_at"] is None
                   for d in await records(env, wait_deadlines))
    reservations = await records(env, admission_reservations)
    assert {r["id"]: r["state"] for r in reservations} == {
        "task_id": "cancelled", "continuation_id": "cancelled", "historical": "consumed"}
    assert all(r["task_id"] == "owner" for r in reservations if r["id"] != "continuation_id")
    assert not [p for p in await records(env, wait_priority_projection) if p["task_id"] == "owner"]
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.effective_priority).where(
            tasks.c.id == "source")) == 100
    with pytest.raises(WaitError):
        await ResumeTransaction(SimpleNamespace(orch=SimpleNamespace(db=env.db))).reserve("c", 1)


async def test_wait_task_audit_migration_drops_old_fks_idempotently(env):
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[1] / (
        "migrations/versions/a00000000113_continuation_task_audit.py")
    spec = importlib.util.spec_from_file_location("wait_task_audit_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def exercise(conn):
        for table in ("continuations", "admission_reservations"):
            conn.execute(sa.text(f'ALTER TABLE {table} ADD CONSTRAINT old_{table}_task '
                                 'FOREIGN KEY (task_id) REFERENCES tasks(id)'))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
        for table in ("continuations", "admission_reservations"):
            assert not [fk for fk in sa.inspect(conn).get_foreign_keys(table)
                        if fk["referred_table"] == "tasks"]

    async with env.db._engine.begin() as conn:
        await conn.run_sync(exercise)
    await park(env)
    await env.db.delete_task("owner")
    assert (await intent(env))["resume_state"] == "cancelled"


@pytest.mark.parametrize("reason", ["push_failed", "stop_uncertain", "resume_launch_failed"])
async def test_continuation_incident_rejects_forged_and_stale_bindings(env, reason):
    from src.database.tables import escalations
    from src.wait_runtime.hibernate import HibernationTransaction, locked_suspension
    from tests.test_wait_decisions import service_for
    from tests.test_wait_queries import records

    await park(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome="satisfied", resolved_at=NOW + 10))
    env.registry = build_registry(clock=lambda: NOW)
    service = service_for(env, [NOW + 30])
    real_handler = service.orch._command_handler
    handler = SimpleNamespace(wait_escalation_create=AsyncMock(
        wraps=real_handler.wait_escalation_create))
    driver = HibernationTransaction(service, clock=lambda: NOW + 30)
    async with env.db._engine.begin() as conn:
        task, wait, continuation = await locked_suspension(conn, env.wait_id)
        assert (await driver.hold(conn, task, wait, continuation, reason, handler))["held"]
    args = handler.wait_escalation_create.call_args.args[1]
    async with env.db._engine.begin() as conn:
        assert (await real_handler.wait_escalation_create(conn, args))["success"]
    for override in (
        {"task_id": "source"},
        {"source_identity": "continuation:c:2:resume_launch_failed",
         "incident_key": "continuation:c:2:resume_launch_failed"},
        {"source_identity": "continuation:missing:1:resume_launch_failed",
         "incident_key": "continuation:missing:1:resume_launch_failed"},
        {"source_identity": "continuation:c:1:unrecognised",
         "incident_key": "continuation:c:1:unrecognised"},
        {"incident_key": "other"},
    ):
        async with env.db._engine.begin() as conn:
            result = await real_handler.wait_escalation_create(conn, {**args, **override})
        assert result["error_code"] == "invalid_binding", result
    assert (await real_handler.execute("escalation_create", args))["error_code"] == "invalid_binding"
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(resume_state="cancelled"))
        result = await real_handler.wait_escalation_create(conn, args)
    assert result["error_code"] == "invalid_binding"
    assert len(await records(env, escalations)) == 1


async def test_park_fences_old_writer_without_returning_to_frontier(env):
    assert await park(env) == 2
    task = await env.db.get_task('owner')
    session = await env.db.get_session('s')
    assert task.status.value == 'HIBERNATED'
    assert task.assigned_agent_id is None and task.claim_epoch == 2
    assert task.branch_name == 'aq/owner'
    assert session.state == session.desired_state == 'stopped'
    assert session.end_reason == 'hibernated' and session.task_id is None
    row = await intent(env)
    assert row['resume_state'] == 'sleeping' and row['saved_head'] == 'a' * 40
    with pytest.raises(WaitError):
        await park(env)


@pytest.mark.parametrize('change', [dict(generation=2), dict(instance_token='new'),
                                    dict(claim_epoch=2), dict(saved_head=None)])
async def test_park_refuses_changed_identity_and_missing_publication(env, change):
    with pytest.raises(WaitError):
        await park(env, **change)
    assert (await env.db.get_task('owner')).claim_epoch == 1
    assert (await env.db.get_session('s')).task_id == 'owner'
    assert (await intent(env))['stopped_at'] is None


async def test_pause_wins_when_park_commits(env):
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(status='PAUSED'))
    await park(env)
    assert (await env.db.get_task('owner')).status.value == 'PAUSED'
    row = await intent(env)
    assert row['resume_state'] == 'held' and row['hold_reason'] == 'paused'


async def test_outcome_during_stop_queues_only_after_park(env):
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).where(wait_records.c.id == env.wait_id).values(
            outcome='satisfied', resolved_at=NOW + 10, result_ref='task:source'))
    await park(env)
    assert (await env.db.get_task('owner')).status.value == 'RESUME_PENDING'
    assert (await intent(env))['resume_state'] == 'queued'


@pytest.mark.parametrize("env", ["message"], indirect=True)
@pytest.mark.parametrize("recipient", ["task", "session"])
@pytest.mark.parametrize("ceiling", [False, True])
async def test_held_thread_reply_resolves_hibernated_wait_and_queues_resume(
    env, recipient, ceiling, monkeypatch,
):
    from src.waits.holding import message_is_held
    from src.waits.source_sql import source_batch_statement
    from tests.test_wait_dispatch import runtime

    service = runtime(env, registry=build_registry(clock=lambda: NOW))
    service.orch.wait_dispatcher = service._dispatcher
    if ceiling:
        service.orch.config.wait_policy.hibernation_enabled = True
        async with env.db._engine.begin() as conn:
            await conn.execute(update(wait_records).values(sleep_decision="in_place"))
            wait = (await conn.execute(select(wait_records))).mappings().one()
            assert (await service.lifecycle.ceiling(
                conn, {}, wait, now=NOW + 1800, handler=None))["suspended"]
    await capture(env)
    await park(env)
    # Reproduce the live incident's CI/pool flood ahead of the reply. The
    # operation bound is deterministic, rather than a loaded-box latency budget.
    async with env.db._engine.begin() as conn:
        await conn.execute(source_batch_statement([
            dict(source_key="ci_check:repo:sha:unit" if n % 2 else
                 "resource_invalidation:pool:worker", generation=0, revision=str(n),
                 outcome_ref=f"noise:{n}", payload={})
            for n in range(2000)
        ]))
    dispatch = service.listener._dispatch

    async def only_relevant(conn, event):
        assert not event["source_key"].startswith(("ci_check:", "resource_invalidation:pool:"))
        await dispatch(conn, event)

    monkeypatch.setattr(service.listener, "_dispatch", only_relevant)
    lease = AsyncMock(wraps=service.lease)
    monkeypatch.setattr(service, "lease", lease)
    reply = await env.db.create_message(
        project_id="p", from_kind="session", from_id="supervisor-p", to_kind=recipient,
        to_id="owner" if recipient == "task" else "s", thread_id="interview-q4", body="Answer",
    )
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(messages.c.id).where(
            messages.c.id == reply.id, message_is_held())) == reply.id
    assert await service.listener.tick()
    async with env.db._engine.connect() as conn:
        wait = (await conn.execute(select(wait_records))).mappings().one()
        assert await conn.scalar(select(wait_listener_cursors.c.last_seq)) >= await conn.scalar(
            select(wait_source_events.c.seq).where(
                wait_source_events.c.outcome_ref == f"message:{reply.id}"))
    assert lease.await_count <= 20
    assert wait["outcome"] == "satisfied"
    assert wait["result_ref"] == f"message:{reply.id}"
    assert (await intent(env))["resume_state"] == "queued"
    assert (await env.db.get_task("owner")).status.value == "RESUME_PENDING"
    queued = await env.db.get_message(reply.id)
    assert queued.delivered_at is None and queued.read_at is None


async def test_live_service_resumes_parked_outcome_without_orchestrator_cycle(env):
    from tests.test_wait_dispatch import runtime
    from src.models import TaskStatus

    await park(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(facts={'workspace_released_at': NOW + 20}))
    service = runtime(env, registry=build_registry(clock=lambda: NOW))
    service.orch.wait_dispatcher = service._dispatcher
    service.listener.renewal_seconds = 0.01
    delivered = asyncio.Event()

    async def command(name, args):
        assert name == 'wait_resume_dispatch'
        assert args == {'continuation_id': 'c', 'generation': 1}
        row = await intent(env)
        assert row['resume_state'] == 'queued'
        assert (await env.db.get_task('owner')).status is TaskStatus.RESUME_PENDING
        delivered.set()
        return {'success': True}

    async def listen():
        # Drain a real outcome, then simulate a stalled source listener.
        await env.db.transition_task('source', TaskStatus.COMPLETED)
        await service.listener.tick()
        await asyncio.Event().wait()

    service.command = command
    service.listener.run = listen
    service.startup_bridge = AsyncMock()
    runner = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(delivered.wait(), 5)
        async with env.db._engine.connect() as conn:
            wait = (await conn.execute(select(wait_records))).mappings().one()
        assert wait['outcome'] == 'satisfied' and wait['wake_delivered_at'] is None
        assert wait['cleared_at'] is None  # Recovery never acknowledges on the worker's behalf.
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert not service.lifecycle._resume_tasks


@pytest.mark.parametrize('state', ['queued', 'reserved', 'launching', 'held'])
async def test_resume_stall_notice_retains_outcome_and_deduplicates_across_restart(env, state):
    from tests.test_wait_dispatch import runtime

    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
    await park(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(resume_state=state))
    now = [NOW + 309]
    service = runtime(env, clock=lambda: now[0], registry=build_registry())
    async def notices():
        async with env.db._engine.connect() as conn:
            return list((await conn.execute(select(messages).where(
                messages.c.subject == 'Resume stalled: owner',
            ))).mappings())
    before = await intent(env)
    await service.orch._command_handler.wait_service_resume_stalls(service.lifecycle)
    assert not await notices()
    now[0] += 1
    await service.orch._command_handler.wait_service_resume_stalls(service.lifecycle)
    [notice] = await notices()
    assert notice['to_id'] == 'supervisor-p' and notice['to_kind'] == 'session'
    assert f'state {state}' in notice['body'] and '300 seconds' in notice['body']
    replacement = runtime(env, clock=lambda: NOW + 900, registry=build_registry())
    await replacement.orch._command_handler.wait_service_resume_stalls(replacement.lifecycle)
    assert len(await notices()) == 1
    assert await intent(env) == before
    async with env.db._engine.connect() as conn:
        wait = (await conn.execute(select(wait_records))).mappings().one()
    assert wait['outcome'] == 'satisfied' and wait['cleared_at'] is None


async def test_restart_retries_resume_while_startup_bridge_is_stalled(env):
    from tests.test_wait_dispatch import runtime

    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
    await park(env)
    before = await intent(env)
    # Construct a fresh service twice over the same durable RESUME_PENDING row.
    # Each process must retry refusals without waiting for legacy adoption.
    for _ in range(2):
        service = runtime(env, registry=build_registry())
        service.listener.renewal_seconds = 0.01
        bridge_started, attempted = asyncio.Event(), asyncio.Event()

        async def bridge():
            bridge_started.set()
            await asyncio.Event().wait()

        async def dispatch(name, args):
            assert name == 'wait_resume_dispatch'
            assert args == {'continuation_id': 'c', 'generation': 1}
            if service.command.await_count >= 2:
                attempted.set()
            return {'success': False, 'error_code': 'wait.graph_busy'}

        service.startup_bridge = bridge
        service.listener.run = AsyncMock(side_effect=asyncio.Event().wait)
        service.command = AsyncMock(side_effect=dispatch)
        runner = asyncio.create_task(service.run())
        try:
            async with asyncio.timeout(5):
                await bridge_started.wait()
                await attempted.wait()
            assert await intent(env) == before
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        assert not service.lifecycle._resume_tasks


async def test_resume_dispatch_in_flight_is_reused_and_failed_command_is_retried(env):
    from tests.test_wait_dispatch import runtime

    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
    await park(env)
    service = runtime(env, registry=build_registry())
    started, release = asyncio.Event(), asyncio.Event()
    async def dispatch(*args):
        started.set()
        await release.wait()
        return {'success': False, 'error_code': 'wait.unavailable'}
    service.command = AsyncMock(side_effect=dispatch)
    try:
        await service.lifecycle.resume_pending()
        await asyncio.wait_for(started.wait(), 5)
        await service.lifecycle.resume_pending()
        assert service.command.await_count == 1
        release.set()
        await asyncio.gather(*service.lifecycle._resume_tasks.values())
        service.command.side_effect = RuntimeError('temporary dispatch failure')
        await service.lifecycle.resume_pending()
        await asyncio.gather(*service.lifecycle._resume_tasks.values())
        service.command.side_effect = None
        service.command.return_value = {'success': True}
        await service.lifecycle.resume_pending()
        await asyncio.gather(*service.lifecycle._resume_tasks.values())
        assert service.command.await_count == 3
    finally:
        await service.lifecycle.stop()


async def test_refused_resume_reservation_waits_for_its_backoff(env):
    # stark-ember-97: a policy-held resume retook the graph lock every tick.
    from tests.test_wait_dispatch import runtime

    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
    await park(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(facts={'reserve_retry': {
            'refusals': 3, 'due_at': NOW + 40, 'blocked': ['policy_hold']}}))
    now = [NOW + 39]
    service = runtime(env, registry=build_registry(), clock=lambda: now[0])
    service.command = AsyncMock(return_value={'success': True})
    try:
        await service.lifecycle.resume_pending()
        assert service.command.await_count == 0 and not service.lifecycle._resume_tasks
        now[0] = NOW + 40
        await service.lifecycle.resume_pending()
        await asyncio.gather(*service.lifecycle._resume_tasks.values())
        assert service.command.await_args.args == (
            'wait_resume_dispatch', {'continuation_id': 'c', 'generation': 1})
    finally:
        await service.lifecycle.stop()


async def test_resume_policy_refusal_backs_off_longer_and_caps(env):
    from src.wait_runtime.resume import ResumeTransaction

    driver = ResumeTransaction(SimpleNamespace(orch=SimpleNamespace(db=env.db)))
    await park(env)
    for refusals, delay in [(1, 10), (2, 20), (6, 300), (9, 300)]:
        async with env.db._engine.begin() as conn:
            await conn.execute(update(continuations).values(facts={'kept': True, **(
                {'reserve_retry': {'refusals': refusals - 1}} if refusals > 1 else {})}))
            row = dict((await conn.execute(select(continuations))).mappings().one())
            await driver._back_off(conn, row, {'policy_hold'}, NOW)
        assert (await intent(env))['facts'] == {'kept': True, 'reserve_retry': {
            'refusals': refusals, 'due_at': NOW + delay, 'blocked': ['policy_hold']}}


async def test_resume_graph_busy_streak_is_scoped_reset_and_retired(env, caplog):
    from tests.test_wait_dispatch import runtime

    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
    await park(env)
    service = runtime(env, registry=build_registry())
    busy = {'success': False, 'error_code': 'wait.graph_busy'}
    service.command = AsyncMock(side_effect=[busy, busy,
        {'success': False, 'error_code': 'wait.unavailable'}, busy, {'success': True}, busy])
    try:
        for expected in (1, 2, 0, 1, 0, 1):
            await service.lifecycle.resume_pending()
            await asyncio.gather(*service.lifecycle._resume_tasks.values())
            assert service.lifecycle._resume_graph_busy.get(('c', 1), 0) == expected
        records = [r.message for r in caplog.records if 'wait.resume_graph_busy:' in r.message]
        assert len(records) == 4
        assert all('continuation_id=c generation=1' in r for r in records)
        assert 'consecutive_refusals=2' in records[1]
        async with env.db._engine.begin() as conn:
            await conn.execute(update(continuations).values(suspension_generation=2))
        service.command.side_effect = None
        service.command.return_value = busy
        await service.lifecycle.resume_pending()
        await asyncio.gather(*service.lifecycle._resume_tasks.values())
        assert service.lifecycle._resume_graph_busy == {('c', 2): 1}
        async with env.db._engine.begin() as conn:
            await conn.execute(update(continuations).values(resume_state='held'))
        await service.lifecycle.resume_pending()
        assert not service.lifecycle._resume_graph_busy
    finally:
        await service.lifecycle.stop()


async def test_listener_batches_cannot_starve_queued_resume_dispatch(env, monkeypatch):
    from sqlalchemy import text
    from src.integration import delivery_observer
    from src.models import AgentProfile
    from tests.test_wait_dispatch import runtime

    await env.db.create_profile(AgentProfile(id='worker-codex', name='Worker'))
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            profile_id='worker-codex', route_source='override'))
    await park(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(facts={'workspace_released_at': NOW + 20}))
    service = runtime(env, registry=build_registry())
    service.orch._task_control_lock = lambda _: asyncio.Lock()
    service.scheduler.reader = SimpleNamespace(observe_task=AsyncMock(return_value=SimpleNamespace(
        constraints={}, binding={'scope': {}},
    )))
    monkeypatch.setattr(delivery_observer, 'hierarchy_frontier_modes', AsyncMock())
    service.lifecycle.resume.fresh_workspace = AsyncMock(return_value='/tmp/fresh')
    service.orch._launch_session_for_task_locked = AsyncMock()
    service.command = AsyncMock(wraps=service.command)
    # The synthetic dispatch barrier must survive until the test observes the
    # queued lock waiter. Keep its work budget outside the ten-second test
    # deadline so a loaded runner cannot release the fence before that handshake.
    # Source timeout/rollback behavior is covered in test_wait_dispatch.py.
    service.listener.event_budget_seconds = 30
    # Use the production batch size: even a backlog of fast events must release
    # the graph fence after the first event, before the renewal interval elapses.
    for n in range(12):
        await env.db.wait_source_commit(source_key='test:backlog', generation=0,
            revision=str(n), outcome_ref=f'test:{n}', payload={})
    holding, release = asyncio.Event(), asyncio.Event()
    batches = []
    reservation_batches = []

    async def observe(*args, **kwargs):
        reservation_batches.append(len(batches))
        return SimpleNamespace(constraints={}, binding={'scope': {}})

    service.scheduler.reader.observe_task.side_effect = observe

    async def dispatch(conn, event):
        batches.append(event['seq'])
        if len(batches) == 1:
            holding.set()
            await release.wait()

    service.listener._dispatch = dispatch
    listener = asyncio.create_task(service.listener.tick())
    try:
        async with asyncio.timeout(10):
            await holding.wait()
            await service.lifecycle.resume_pending()
            async with env.db._engine.connect() as conn:
                while not await conn.scalar(text("""
                    SELECT EXISTS (SELECT 1 FROM pg_locks
                    WHERE locktype = 'advisory' AND NOT granted
                    AND database = (SELECT oid FROM pg_database
                                    WHERE datname = current_database()))
                """)):
                    await asyncio.sleep(0.01)
            release.set()
            await asyncio.gather(*service.lifecycle._resume_tasks.values())
            await listener
        service.command.assert_awaited_once_with('wait_resume_dispatch', {
            'continuation_id': 'c', 'generation': 1,
        })
        service.orch._launch_session_for_task_locked.assert_awaited_once()
        assert reservation_batches == [1]  # Resume wins immediately after the held batch.
        assert (await intent(env))['resume_state'] == 'reserved'
        assert not service.lifecycle._resume_graph_busy
        # If resume caused a nonblocking drain refusal, the cursor replays the
        # rest of the backlog safely on the listener's next tick.
        await service.listener.tick()
        assert len(batches) >= 12
        assert len(batches) == len(set(batches))
    finally:
        release.set()
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await service.lifecycle.stop()


async def test_slow_resume_does_not_block_another_parked_task_and_shutdown_joins_it(env):
    from tests.test_wait_dispatch import runtime

    second = await owner(env.db, task_id='second', session_id='s2', agent_id='a2')
    async with env.db._engine.begin() as conn:
        receipt = await register_wait(conn, scope=second,
            condition={'kind': 'task', 'task_id': 'source'}, idempotency_key='second',
            registry=build_registry(clock=lambda: NOW), policy=Policy(), now=NOW,
            source_watermark=0, decide_sleep=lambda *_: SleepDecision('hibernate', 'test', {}))
        await conn.execute(insert(continuations).values(
            id='c2', task_id='second', wait_id=receipt['wait_id'], suspension_generation=1,
            facts={}, branch='aq/second', prior_status='IN_PROGRESS', held_cursor_seq=0,
            resume_state='sleeping', created_at=NOW))
        await conn.execute(update(tasks).where(tasks.c.id == 'second').values(branch_name='aq/second'))
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
    await park(env)
    async with env.db._engine.begin() as conn:
        await env.db.park_task_for_continuation(conn, task_id='second', wait_id=receipt['wait_id'],
            continuation_id='c2', generation=1, session_id='s2', instance_token='token',
            claim_epoch=1, saved_head='a' * 40, now=NOW + 20)
    service = runtime(env, registry=build_registry())
    first, other = asyncio.Event(), asyncio.Event()
    async def dispatch(name, args):
        if args['continuation_id'] == 'c':
            first.set()
            await asyncio.Event().wait()
        other.set()
        return {'success': True}
    service.command = dispatch
    try:
        await service.lifecycle.resume_pending()
        await asyncio.wait_for(first.wait(), 5)
        await asyncio.wait_for(other.wait(), 5)
        assert ('c', 1) in service.lifecycle._resume_tasks
    finally:
        await service.lifecycle.stop()
    assert not service.lifecycle._resume_tasks


async def test_slow_hibernation_does_not_block_overdue_writer_or_duplicate_attempts(env):
    from src.database.tables import wait_deadlines
    from tests.test_wait_dispatch import runtime

    second = await owner(env.db, task_id='second', session_id='s2', agent_id='a2')
    async with env.db._engine.begin() as conn:
        receipt = await register_wait(conn, scope=second,
            condition={'kind': 'task', 'task_id': 'source'}, idempotency_key='second',
            registry=build_registry(clock=lambda: NOW), policy=Policy(), now=NOW,
            source_watermark=0, decide_sleep=lambda *_: SleepDecision('hibernate', 'test', {}))
        await conn.execute(insert(continuations).values(
            id='c2', task_id='second', wait_id=receipt['wait_id'], suspension_generation=1,
            facts={}, branch='aq/second', prior_status='IN_PROGRESS', held_cursor_seq=0,
            resume_state='sleeping', created_at=NOW))
        await conn.execute(insert(wait_deadlines).values(
            id='overdue', wait_id=receipt['wait_id'], deadline_type='transition',
            due_at=NOW + 120, generation=1))
    service = runtime(env, clock=lambda: NOW + 10000, registry=build_registry())
    first, other = asyncio.Event(), asyncio.Event()
    calls = []

    async def suspend(name, args):
        assert name == 'wait_sleep_decide'
        calls.append(args['wait_id'])
        if args['wait_id'] == env.wait_id:
            first.set()
            await asyncio.Event().wait()
        other.set()
        return {'success': True}

    service.command = suspend
    try:
        await service.lifecycle.tick()
        await asyncio.wait_for(first.wait(), 5)
        await asyncio.wait_for(other.wait(), 5)
        await service.lifecycle.tick()
        assert calls.count(env.wait_id) == 1
        assert env.wait_id in service.lifecycle._hibernate_tasks
        async with env.db._engine.connect() as conn:
            assert await conn.scalar(select(wait_deadlines.c.fired_at).where(
                wait_deadlines.c.id == 'overdue')) is None
    finally:
        await service.lifecycle.stop()
    assert not service.lifecycle._hibernate_tasks


async def test_maintenance_retries_after_a_transient_failure(env):
    from tests.test_wait_dispatch import runtime

    service = runtime(env, registry=build_registry())
    service.listener.renewal_seconds = 0.01
    resumed = asyncio.Event()
    attempts = 0

    async def tick():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError('temporary')
        resumed.set()

    service.lifecycle.tick = tick
    runner = asyncio.create_task(service.maintain())
    try:
        await asyncio.wait_for(resumed.wait(), 5)
        assert attempts >= 2
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.parametrize('change', ['pause', 'acknowledged', 'cleared'])
async def test_resume_stall_notice_excludes_paused_and_consumed_waits(env, change):
    from tests.test_wait_dispatch import runtime

    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
    await park(env)
    async with env.db._engine.begin() as conn:
        if change == 'pause':
            await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(status='PAUSED'))
        elif change == 'acknowledged':
            await conn.execute(update(continuations).values(resume_state='acknowledged'))
        else:
            await conn.execute(update(wait_records).values(cleared_at=NOW + 20))
    service = runtime(env, clock=lambda: NOW + 1000, registry=build_registry())
    await service.orch._command_handler.wait_service_resume_stalls(service.lifecycle)
    async with env.db._engine.connect() as conn:
        assert not list((await conn.execute(select(messages))).mappings())


async def test_suspension_fence_is_exact_session_bound(env):
    from src.waits.fencing import mutation_fence

    principal = SimpleNamespace(session_id='s', session_instance_token='token')
    async with env.db._engine.connect() as conn:
        assert (await mutation_fence(conn, 'task_close', principal))['error_code'] == 'wait.suspending'
        assert await mutation_fence(conn, 'wait_checkpoint', principal) is None
        foreign = SimpleNamespace(session_id='other', session_instance_token='other')
        assert await mutation_fence(conn, 'task_close', foreign) is None


async def test_begin_suspension_is_idempotent_and_binds_registration_cursor(env):
    from sqlalchemy import delete
    from src.wait_runtime.hibernate import begin_suspension, locked_suspension

    async with env.db._engine.begin() as conn:
        await conn.execute(delete(continuations))
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(branch_name='aq/owner'))
        task = (await conn.execute(select(tasks).where(tasks.c.id == 'owner'))).mappings().one()
        wait = (await conn.execute(select(wait_records))).mappings().one()
        first = await begin_suspension(conn, wait, task, now=NOW, budget=120)
        second = await begin_suspension(conn, wait, task, now=NOW + 10, budget=120)
        assert first == second
        assert first['held_cursor_seq'] == wait['decision_inputs']['delivery']['held_cursor_seq']
        _, _, current = await locked_suspension(conn, env.wait_id)
        assert current['suspension_generation'] == 1


async def suspension_case(env, monkeypatch, failure=None):
    from src.wait_runtime.hibernate import HibernationTransaction

    await capture(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            branch_name='aq/owner', status='WAITING'))
    session = await env.db.get_session('s')
    workspace = SimpleNamespace(id='ws', workspace_path=session.work_dir, is_slot=False)
    monkeypatch.setattr(env.db, 'get_workspace_for_task', AsyncMock(return_value=workspace))
    monkeypatch.setattr(env.db, 'get_project', AsyncMock(return_value=SimpleNamespace(
        integration_repository_id='repo')))
    monkeypatch.setattr(env.db, 'get_repo', AsyncMock(return_value=SimpleNamespace(
        url='remote', default_branch='main')))
    provider = SimpleNamespace(stop=AsyncMock(), confirm_stopped=AsyncMock(return_value=True),
                              confirm_instance_stopped=AsyncMock(
        return_value=failure != 'stop'), process_alive=AsyncMock(return_value=False))
    saved = SimpleNamespace(status='failed' if failure == 'push' else 'pushed',
                            commit='a' * 40, error='push failed')
    save = AsyncMock(return_value=saved)
    if failure == 'push_retry':
        save.side_effect = [SimpleNamespace(status='failed', commit=None, error='temporary'),
                            saved]
    detach = AsyncMock(return_value=failure != 'detach')
    monkeypatch.setattr('src.orchestrator.stranded_work.save_wip_to_task_branch', save)
    monkeypatch.setattr(
        'src.orchestrator.workspace_attachments.detach_workspace_for_integration_handoff', detach)
    monkeypatch.setattr('src.wait_runtime.hibernate.remove_claim_file_if_matches', lambda *a: True)
    orch = SimpleNamespace(
        db=env.db, config=SimpleNamespace(wait_policy=SimpleNamespace(
            transition_budget_seconds=0.02 if failure in {'push', 'stop'} else 120,
            hibernation_enabled=True)),
        _task_control_lock=lambda _: asyncio.Lock(),
        session_providers=SimpleNamespace(create=lambda *a: provider),
        session_reconciler=SimpleNamespace(_process_names=lambda _: ('codex',)),
        git=SimpleNamespace(resolve_commit_identity=lambda _: None,
                            arev_parse=AsyncMock(return_value='a' * 40)), bus=None, _git_mutex=None,
        token_store=SimpleNamespace(revoke_session=AsyncMock()),
        _release_workspace_and_cleanup=AsyncMock(),
    )
    handler = SimpleNamespace(wait_escalation_create=AsyncMock(return_value={'success': True}))
    driver = HibernationTransaction(SimpleNamespace(orch=orch), clock=lambda: NOW + 1)
    return SimpleNamespace(driver=driver, orch=orch, handler=handler, provider=provider,
                           save=save, workspace=workspace)


@pytest.mark.parametrize('failure', [None, 'push', 'stop', 'detach', 'push_retry'])
async def test_suspend_retains_checkout_until_stop_push_and_detach_proven(env, monkeypatch, failure):
    case = await suspension_case(env, monkeypatch, failure)
    driver, orch, handler = case.driver, case.orch, case.handler
    save, workspace = case.save, case.workspace
    result = await driver.advance(env.wait_id, handler=handler)
    if failure == 'push_retry':
        assert result['push_pending']
        assert (await env.db.get_task('owner')).claim_epoch == 1
        orch._release_workspace_and_cleanup.assert_not_awaited()
        orch.token_store.revoke_session.assert_not_awaited()
        assert (await intent(env))['facts']['local_head'] == 'a' * 40
        assert (await driver.advance(env.wait_id, handler=handler))['push_pending']
        assert save.await_count == 1
        driver.clock = lambda: NOW + 5
        result = await driver.advance(env.wait_id, handler=handler)
        assert result['parked'] and save.await_count == 2
        handler.wait_escalation_create.assert_not_awaited()
        return
    if failure:
        assert result['held']
        assert (await env.db.get_task('owner')).claim_epoch == 1
        orch._release_workspace_and_cleanup.assert_not_awaited()
        orch.token_store.revoke_session.assert_not_awaited()
        assert (await intent(env))['stopped_at'] is None
    else:
        assert result['parked']
        assert (await env.db.get_task('owner')).status.value == 'HIBERNATED'
        orch._release_workspace_and_cleanup.assert_awaited_once_with(workspace)
        orch.token_store.revoke_session.assert_awaited_once_with('s')
        assert (await intent(env))['facts']['workspace_released_at'] == NOW + 1


async def expire_stop(env, token):
    async with env.db._engine.begin() as conn:
        if token is None:
            # Reproduce the legacy rows from the incident. Current metadata
            # requires tokens; restore its constraint before returning the lease.
            await conn.exec_driver_sql(
                'ALTER TABLE sessions ALTER COLUMN instance_token DROP NOT NULL')
            env.legacy_null_tokens = True
        wait = (await conn.execute(select(wait_records))).mappings().one()
        await conn.execute(update(wait_records).values(decision_inputs={
            **wait['decision_inputs'], 'delivery': {
                **wait['decision_inputs']['delivery'], 'instance_token': token}}))
        await conn.execute(update(sessions).where(sessions.c.id == 's').values(instance_token=token))
        await conn.execute(update(continuations).values(
            facts={'hibernation': {'stop_started_at': NOW - 120}}))


@pytest.mark.parametrize('token', [None, 'token'])
@pytest.mark.parametrize('proof', ['gone', 'alive', 'unavailable', 'timeout', 'successor'])
async def test_expired_stop_budget_uses_read_only_proof(env, monkeypatch, token, proof):
    case = await suspension_case(env, monkeypatch)
    await expire_stop(env, token)
    if proof == 'alive':
        case.provider.process_alive.return_value = True
    elif proof == 'unavailable':
        case.provider.confirm_instance_stopped.side_effect = RuntimeError('unavailable')
    elif proof == 'timeout':
        monkeypatch.setattr('src.wait_runtime.hibernate.STOP_PROOF_TIMEOUT_SECONDS', 0.01)

        async def stuck_probe(*args):
            await asyncio.Event().wait()

        case.provider.confirm_instance_stopped.side_effect = stuck_probe
    elif proof == 'successor':
        # The instance-specific probe accepts a successor for a known old token;
        # name absence must still be proved before touching the checkout.
        case.provider.confirm_instance_stopped.return_value = token is not None
        case.provider.confirm_stopped.return_value = False
    result = await case.driver.advance(env.wait_id, handler=case.handler)
    case.provider.stop.assert_not_awaited()
    if proof == 'gone':
        assert result['parked']
        case.save.assert_awaited_once()
        assert (await intent(env))['stopped_at'] == NOW + 1
    else:
        assert result['held'] and result['hold_reason'] == 'stop_uncertain'
        case.save.assert_not_awaited()
        case.orch._release_workspace_and_cleanup.assert_not_awaited()
        assert (await env.db.get_task('owner')).claim_epoch == 1


@pytest.mark.parametrize('resolved', [False, True])
@pytest.mark.parametrize('token', [None, 'token'])
async def test_held_stop_recovers_on_later_tick_and_withdraws_incident(
    env, monkeypatch, resolved, token,
):
    from src.database.tables import escalations
    from tests.test_wait_dispatch import runtime

    case = await suspension_case(env, monkeypatch)
    await expire_stop(env, token)
    now = [NOW + 1]
    service = runtime(env, clock=lambda: now[0], registry=build_registry())
    # Exercise the real command boundary, incident creation and withdrawal.
    service.orch.__dict__.update(case.orch.__dict__)
    service.orch.config = AppConfig()
    service.lifecycle.hibernate.clock = lambda: now[0]
    driver, handler = service.lifecycle.hibernate, service.orch._command_handler
    case.provider.process_alive.return_value = True
    assert (await driver.advance(env.wait_id, handler=handler))['held']
    held = await intent(env)
    incident_id = held['facts']['hold_escalation_id']
    if resolved:
        async with env.db._engine.begin() as conn:
            await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=now[0]))
    try:
        case.provider.process_alive.return_value = False
        # Restart the lifecycle to prove that the backoff lives in the database.
        from src.wait_runtime.lifecycle import WaitLifecycle
        service.lifecycle = WaitLifecycle(service, clock=lambda: now[0])
        await service.lifecycle.tick()
        assert not service.lifecycle._hibernate_tasks
        assert (await intent(env))['resume_state'] == 'held'
        now[0] += 5
        await service.lifecycle.tick()
        await asyncio.gather(*service.lifecycle._hibernate_tasks.values())
    finally:
        await service.lifecycle.stop()
    row = await intent(env)
    assert row['stopped_at'] == now[0] and row['hold_reason'] is None
    assert row['resume_state'] == ('queued' if resolved else 'sleeping')
    assert (await env.db.get_task('owner')).status.value == (
        'RESUME_PENDING' if resolved else 'HIBERNATED')
    async with env.db._engine.connect() as conn:
        [incident] = (await conn.execute(select(escalations))).mappings().all()
    assert incident['id'] == incident_id and incident['state'] == 'cancelled'
    assert incident['terminal_outcome'] == 'source_satisfied'
    case.provider.stop.assert_not_awaited()


async def publication_hold(env, monkeypatch):
    from tests.test_wait_dispatch import runtime

    case = await suspension_case(env, monkeypatch, 'push')
    now = [NOW + 200]
    service = runtime(env, clock=lambda: now[0], registry=build_registry())
    service.orch.__dict__.update(case.orch.__dict__)
    service.orch.config = AppConfig()
    handler = service.orch._command_handler
    assert (await service.lifecycle.hibernate.advance(env.wait_id, handler=handler))['held']
    assert (await intent(env))['hold_reason'] == 'push_failed'
    case.provider.stop.reset_mock()
    return case, service, now


@pytest.mark.parametrize('resolved', [False, True])
async def test_publication_hold_recovers_after_restart_with_persisted_backoff(
    env, monkeypatch, resolved,
):
    from src.database.tables import escalations
    from src.wait_runtime.lifecycle import WaitLifecycle

    case, service, now = await publication_hold(env, monkeypatch)
    held = await intent(env)
    if resolved:
        async with env.db._engine.begin() as conn:
            await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=now[0]))
    service.lifecycle = WaitLifecycle(service, clock=lambda: now[0])
    case.save.return_value = SimpleNamespace(status='pushed', commit='a' * 40, error=None)
    try:
        await service.lifecycle.tick()
        assert not service.lifecycle._hibernate_tasks
        assert case.save.await_count == 1
        now[0] = held['facts']['hibernation']['push_retry_at']
        await service.lifecycle.tick()
        await asyncio.gather(*service.lifecycle._hibernate_tasks.values())
    finally:
        await service.lifecycle.stop()
    row = await intent(env)
    assert row['hold_reason'] is None and row['saved_head'] == 'a' * 40
    assert row['resume_state'] == ('queued' if resolved else 'sleeping')
    assert row['suspension_generation'] == held['suspension_generation']
    assert case.save.await_count == 2
    case.provider.stop.assert_not_awaited()
    case.orch._release_workspace_and_cleanup.assert_awaited_once()
    async with env.db._engine.connect() as conn:
        incident = (await conn.execute(select(escalations))).mappings().one()
    assert incident['id'] == held['facts']['hold_escalation_id']
    assert incident['state'] == 'cancelled' and incident['terminal_outcome'] == 'source_satisfied'


@pytest.mark.parametrize('failure', ['push', 'repository', 'live', 'successor', 'paused', 'epoch'])
async def test_publication_retry_retains_ownership_when_proofs_fail(env, monkeypatch, failure):
    case, service, now = await publication_hold(env, monkeypatch)
    held = await intent(env)
    now[0] = held['facts']['hibernation']['push_retry_at']
    if failure == 'live':
        case.provider.process_alive.return_value = True
    elif failure == 'repository':
        monkeypatch.setattr(env.db, 'get_repo', AsyncMock(return_value=None))
    elif failure == 'successor':
        case.provider.confirm_stopped.return_value = False
    elif failure in {'paused', 'epoch'}:
        async with env.db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
                **({'status': 'PAUSED'} if failure == 'paused' else {'claim_epoch': 2})))
    if failure == 'epoch':
        with pytest.raises(WaitError, match='writer identity changed'):
            await service.lifecycle.hibernate.advance(env.wait_id, handler=service.orch._command_handler)
    else:
        result = await service.lifecycle.hibernate.advance(
            env.wait_id, handler=service.orch._command_handler)
        assert result['held']
    row = await intent(env)
    assert row['stopped_at'] is None and row['saved_head'] is None
    assert row['facts']['local_head'] == held['facts']['local_head']
    assert case.save.await_count == (2 if failure == 'push' else 1)
    if failure in {'push', 'repository', 'live', 'successor'}:
        assert row['facts']['hibernation']['push_retry_at'] > now[0]
    case.provider.stop.assert_not_awaited()
    case.orch._release_workspace_and_cleanup.assert_not_awaited()
    case.orch.token_store.revoke_session.assert_not_awaited()


@pytest.mark.parametrize('resolved', [False, True])
async def test_supervisor_requeues_publication_hold_without_releasing_writer(env, monkeypatch, resolved):
    from src.commands.principal import TRUSTED_LOCAL

    case, service, now = await publication_hold(env, monkeypatch)
    held = await intent(env)
    if resolved:
        async with env.db._engine.begin() as conn:
            await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=now[0]))
    args = dict(wait_id=env.wait_id, decision='requeue', continuation_id='c',
                expected_generation=2, note='Publication credentials repaired')
    handler = service.orch._command_handler
    with principal_context(TRUSTED_LOCAL):
        assert (await handler.execute('wait_decide', args))['error_code'] == 'continuation.stale_generation'
        assert await intent(env) == held
        args['expected_generation'] = 1
        result = await handler.execute('wait_decide', args)
        assert result['success'], result
    row = await intent(env)
    assert row['resume_state'] == 'held' and row['hold_reason'] == 'push_failed'
    assert row['facts']['hibernation']['push_retry_at'] == 0
    assert row['facts']['resume_requeue']['note'] == args['note']
    assert (await env.db.get_task('owner')).claim_epoch == 1
    case.orch._release_workspace_and_cleanup.assert_not_awaited()
    case.save.return_value = SimpleNamespace(status='pushed', commit='a' * 40, error=None)
    assert (await service.lifecycle.hibernate.advance(env.wait_id, handler=handler))['parked']
    case.provider.stop.assert_not_awaited()


@pytest.mark.parametrize('resolved', [False, True])
async def test_supervisor_retires_publication_hold_by_continuation_id(env, monkeypatch, resolved):
    import json
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import admission_reservations, escalations, events, wait_deadlines, wait_edges

    case, service, now = await publication_hold(env, monkeypatch)
    held = await intent(env)
    async with env.db._engine.begin() as conn:
        if resolved:
            await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=now[0]))
        await conn.execute(insert(admission_reservations).values(
            id='retiring-seat', project_id='p', continuation_id='c', scope={},
            effective_priority=100, provenance='resume', deadline_at=NOW + 300,
            state='pending', created_at=NOW))
    args = dict(decision='retire', continuation_id='c', expected_generation=1,
                note='Retire this stranded continuation')
    with principal_context(TRUSTED_LOCAL):
        result = await service.orch._command_handler.execute('wait_decide', args)
    assert result['success'], result
    row = await intent(env)
    assert row['resume_state'] == 'cancelled' and row['hold_reason'] == 'supervisor_retire'
    assert row['summary'] == held['summary'] and row['facts'] == held['facts']
    task = await env.db.get_task('owner')
    assert task.status.value == 'PAUSED' and task.claim_epoch == 2 and task.assigned_agent_id == 'a'
    pause = await env.db.get_task_meta('owner', 'manual_pause')
    assert pause['cleanup_pending'] and pause['claim_epoch'] == 1
    assert pause['status'] == 'READY' and pause['retired_status'] == 'WAITING'
    assert pause['sessions'][0]['id'] == 's'
    await env.db.validate_task_pause('owner', pause)
    assert await env.db.recover_orphaned_pause('owner') is None
    session = await env.db.get_session('s')
    assert session.desired_state == 'stopped' and session.task_id == 'owner'
    case.orch._release_workspace_and_cleanup.assert_not_awaited()
    case.orch.token_store.revoke_session.assert_not_awaited()
    async with env.db._engine.connect() as conn:
        wait = (await conn.execute(select(wait_records))).mappings().one()
        assert wait['outcome'] == ('satisfied' if resolved else 'cancelled')
        assert wait['cleared_at'] == now[0] and not wait['blocking']
        assert not await conn.scalar(select(wait_edges.c.id).where(wait_edges.c.active.is_(True)))
        assert not await conn.scalar(select(wait_deadlines.c.id).where(
            wait_deadlines.c.cancelled_at.is_(None), wait_deadlines.c.fired_at.is_(None)))
        assert await conn.scalar(select(admission_reservations.c.state)) == 'cancelled'
        assert await conn.scalar(select(escalations.c.state)) == 'cancelled'
        audit = json.loads(await conn.scalar(select(events.c.payload).where(
            events.c.event_type == 'wait.continuation_retired')))
    assert audit['continuation_id'] == 'c' and audit['generation'] == 1
    assert audit['note'] == args['note']
    # Normal pause cleanup may only release a proved stopped holder. Once it
    # finishes, explicit Resume must not recreate an orphan HIBERNATED task.
    await env.db.update_session('s', state='stopped', last_claim_result='manually_paused')
    await env.db.finish_task_pause('owner', pause)
    await env.db.resume_task('owner')
    assert (await env.db.get_task('owner')).status.value == 'READY'


@pytest.mark.parametrize('change', [
    'generation', 'wait', 'epoch', 'branch', 'instance', 'acknowledged', 'terminal',
])
async def test_retire_refuses_stale_continuation_without_mutation(env, monkeypatch, change):
    from src.commands.principal import TRUSTED_LOCAL

    _, service, _ = await publication_hold(env, monkeypatch)
    args = dict(decision='retire', continuation_id='c', expected_generation=1, note='Retire')
    async with env.db._engine.begin() as conn:
        if change == 'generation':
            args['expected_generation'] = 2
        elif change == 'wait':
            args['wait_id'] = 'foreign'
        elif change == 'instance':
            await conn.execute(update(sessions).where(sessions.c.id == 's').values(
                instance_token='replacement'))
        elif change in {'epoch', 'branch', 'terminal'}:
            await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(**{
                'epoch': {'claim_epoch': 2}, 'branch': {'branch_name': 'aq/replacement'},
                'terminal': {'status': 'COMPLETED'},
            }[change]))
        else:
            await conn.execute(update(continuations).values(resume_state='acknowledged'))
    held = await intent(env)
    before = await env.db.get_task('owner')
    with principal_context(TRUSTED_LOCAL):
        assert not (await service.orch._command_handler.execute('wait_decide', args))['success']
    assert await intent(env) == held
    assert await env.db.get_task('owner') == before
    assert (await env.db.get_session('s')).desired_state == 'running'


async def test_retirement_requires_live_scoped_supervisor_and_task_control_fence(env, monkeypatch):
    from dataclasses import replace

    _, service, _ = await publication_hold(env, monkeypatch)
    handler = service.orch._command_handler
    record = replace(await env.db.get_session('s'), id='supervisor', name='supervisor',
                     task_id=None, agent_id=None, lifecycle='named', profile_id='supervisor')
    await env.db.create_session(record)
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=['wait_decide']),
        session_id=record.id, session_instance_token=record.instance_token,
        project_id='p', profile_id='supervisor', elevated=True)
    args = dict(decision='retire', continuation_id='c', expected_generation=1, note='Retire')
    held = await intent(env)
    for rejected in (replace(principal, elevated=False), replace(principal, project_id='foreign'),
                     replace(principal, session_instance_token='stale')):
        with principal_context(rejected):
            assert not (await handler.execute('wait_decide', args))['success']
        assert await intent(env) == held
    lock = asyncio.Lock()
    service.orch._task_control_lock = lambda _: lock
    with principal_context(principal):
        async with lock:
            retirement = asyncio.create_task(handler.execute('wait_decide', args))
            await asyncio.sleep(0)
            assert not retirement.done()
            assert await intent(env) == held
        result = await retirement
    assert result['success'], result


@pytest.mark.parametrize('writer', ['parked', 'reused_original', 'resumed', 'successor'])
async def test_retirement_after_parking_fences_the_resume_writer(env, monkeypatch, writer):
    from dataclasses import replace
    from src.commands.principal import TRUSTED_LOCAL

    case, service, _ = await publication_hold(env, monkeypatch)
    case.save.return_value = SimpleNamespace(status='pushed', commit='a' * 40, error=None)
    with principal_context(TRUSTED_LOCAL):
        assert (await service.orch._command_handler.execute('wait_decide', dict(
            wait_id=env.wait_id, decision='requeue', continuation_id='c',
            expected_generation=1, note='Retry publication')))['success']
    assert (await service.lifecycle.hibernate.advance(
        env.wait_id, handler=service.orch._command_handler))['parked']
    if writer == 'reused_original':
        # Simulate restored/replaced session history; the lifecycle API rightly
        # forbids reviving a terminal session row.
        async with env.db._engine.begin() as conn:
            await conn.execute(update(sessions).where(sessions.c.id == 's').values(
                task_id='source', instance_token='replacement',
                state='running', desired_state='running'))
    elif writer != 'parked':
        record = replace(await env.db.get_session('s'), id='resume', name='resume',
                         task_id='owner', state='running', desired_state='running',
                         last_claim_epoch=2, instance_token='fresh')
        await env.db.create_session(record)
        async with env.db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
                status='RESUME_PENDING', assigned_agent_id='a'))
            row = await intent(env)
            await conn.execute(update(continuations).values(
                new_session_id='resume', resume_state='delivered', facts={**row['facts'],
                    'resume_delivery': {'claim_epoch': 2,
                                        'instance_token': 'stale' if writer == 'successor' else 'fresh'}}))
    before = await intent(env)
    with principal_context(TRUSTED_LOCAL):
        result = await service.orch._command_handler.execute('wait_decide', dict(
            decision='retire', continuation_id='c', expected_generation=1, note='Retire'))
    assert result['success'] == (writer != 'successor'), result
    if writer == 'successor':
        assert await intent(env) == before
        assert (await env.db.get_session('resume')).desired_state == 'running'
    else:
        assert (await intent(env))['resume_state'] == 'cancelled'
        assert (await env.db.get_task('owner')).status.value == 'PAUSED'
        if writer == 'resumed':
            assert (await env.db.get_session('resume')).desired_state == 'stopped'
        elif writer == 'reused_original':
            replacement = await env.db.get_session('s')
            assert replacement.task_id == 'source' and replacement.desired_state == 'running'


@pytest.mark.parametrize('status', ['COMPLETED', 'FAILED', 'BLOCKED'])
@pytest.mark.parametrize('outcome', ['pending', 'satisfied', 'cancelled'])
@pytest.mark.parametrize('cleared', [False, True])
async def test_terminal_stop_hold_retires_on_one_lifecycle_dispatch(env, status, outcome, cleared):
    from src.wait_runtime.lifecycle import WaitLifecycle
    from tests.test_wait_dispatch import runtime

    await capture(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(status=status))
        await conn.execute(update(continuations).values(
            resume_state='held', hold_reason='stop_uncertain', saved_head='a' * 40,
            facts={'hibernation': {'stop_proof_attempts': 1, 'stop_retry_at': NOW}},
        ))
        await conn.execute(update(wait_records).values(
            outcome=outcome, resolved_at=NOW if outcome != 'pending' else None,
            result_ref='original-result' if outcome != 'pending' else None,
            cleared_at=NOW if cleared else None, blocking=not cleared,
        ))
    before = await env.db.get_task('owner')
    service = runtime(env, clock=lambda: NOW + 100, registry=build_registry())
    service.command = AsyncMock(wraps=service.command)
    service.lifecycle.hibernate.advance = AsyncMock(side_effect=AssertionError('must not stop/save'))
    try:
        await service.lifecycle.tick()
        await asyncio.gather(*service.lifecycle._hibernate_tasks.values())
        service.lifecycle.hibernate.advance.assert_not_awaited()
        # A fresh lifecycle also observes durable retirement, not an in-memory guard.
        service.lifecycle = WaitLifecycle(service, clock=lambda: NOW + 200)
        await service.lifecycle.tick()
        await service.lifecycle.tick()
        assert service.command.await_count == 1
        assert not service.lifecycle._hibernate_tasks
    finally:
        await service.lifecycle.stop()
    row = await intent(env)
    assert row['resume_state'] == 'cancelled' and row['hold_reason'] == 'task_terminal'
    assert row['stopped_at'] is None and 'workspace_released_at' not in row['facts']
    assert row['summary'] == 'Goal: resume the exact checked tree.' and row['saved_head'] == 'a' * 40
    assert await env.db.get_task('owner') == before
    async with env.db._engine.connect() as conn:
        wait = (await conn.execute(select(wait_records))).mappings().one()
        assert wait['cleared_at'] is not None and not wait['blocking']
        if outcome != 'pending':
            assert (wait['outcome'], wait['resolved_at'], wait['result_ref']) == (
                outcome, NOW, 'original-result')
        else:
            assert wait['outcome'] == 'cancelled'


@pytest.mark.parametrize('outcome', ['satisfied', 'cancelled'])
async def test_cleared_wait_retires_lingering_stop_hold_without_changing_live_task(env, outcome):
    from tests.test_wait_dispatch import runtime

    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(
            resume_state='held', hold_reason='stop_uncertain',
        ))
        await conn.execute(update(wait_records).values(
            outcome=outcome, resolved_at=NOW, cleared_at=NOW, blocking=False,
        ))
    before = await env.db.get_task('owner')
    service = runtime(env, clock=lambda: NOW + 10, registry=build_registry())
    service.command = AsyncMock(wraps=service.command)
    try:
        await service.lifecycle.tick()
        await asyncio.gather(*service.lifecycle._hibernate_tasks.values())
        await service.lifecycle.tick()
        assert service.command.await_count == 1
    finally:
        await service.lifecycle.stop()
    assert (await intent(env))['resume_state'] == 'cancelled'
    assert await env.db.get_task('owner') == before


@pytest.mark.parametrize('proof', ['alive', 'unavailable'])
async def test_stop_proof_retries_exhaust_without_releasing_checkout(env, monkeypatch, proof):
    from src.wait_runtime.hibernate import STOP_PROOF_MAX_ATTEMPTS
    from tests.test_wait_dispatch import runtime

    case = await suspension_case(env, monkeypatch)
    await expire_stop(env, 'token')
    case.provider.process_alive.return_value = True
    if proof == 'unavailable':
        case.provider.confirm_instance_stopped.side_effect = RuntimeError('unavailable')
    assert (await case.driver.advance(env.wait_id, handler=case.handler))['held']
    for attempt in range(2, STOP_PROOF_MAX_ATTEMPTS + 1):
        state = (await intent(env))['facts']['hibernation']
        assert state['stop_proof_attempts'] == attempt - 1
        case.driver.clock = lambda: state['stop_retry_at']
        assert (await case.driver.advance(env.wait_id, handler=case.handler))['held']
    service = runtime(env, clock=lambda: NOW + 10000, registry=build_registry())
    service.command = AsyncMock()
    await service.lifecycle.tick()
    assert not service.lifecycle._hibernate_tasks
    case.driver.clock = lambda: NOW + 10000
    probes = case.provider.confirm_instance_stopped.await_count
    assert (await case.driver.advance(env.wait_id, handler=case.handler))['held']
    assert case.provider.confirm_instance_stopped.await_count == probes == STOP_PROOF_MAX_ATTEMPTS
    case.handler.wait_escalation_create.assert_awaited_once()
    case.save.assert_not_awaited()
    case.orch.token_store.revoke_session.assert_not_awaited()
    case.orch._release_workspace_and_cleanup.assert_not_awaited()


async def test_null_token_never_signals_even_with_stop_budget_remaining(env, monkeypatch):
    case = await suspension_case(env, monkeypatch)
    await expire_stop(env, None)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(facts={}))
    case.provider.confirm_instance_stopped.return_value = False
    assert (await case.driver.advance(env.wait_id, handler=case.handler))['held']
    case.provider.stop.assert_not_awaited()


async def test_writer_identity_changed_during_stop_proof_never_saves(env, monkeypatch):
    case = await suspension_case(env, monkeypatch)
    await expire_stop(env, None)

    async def successor(*args):
        await env.db.update_session('s', instance_token='successor-token')
        return True

    case.provider.confirm_instance_stopped.side_effect = successor
    with pytest.raises(WaitError, match='writer identity changed'):
        await case.driver.advance(env.wait_id, handler=case.handler)
    case.save.assert_not_awaited()
    case.orch._release_workspace_and_cleanup.assert_not_awaited()


async def test_failed_stop_signal_with_fresh_death_proof_still_parks(env, monkeypatch):
    case = await suspension_case(env, monkeypatch)
    case.provider.stop.side_effect = OSError('stop response lost')
    assert (await case.driver.advance(env.wait_id, handler=case.handler))['parked']
    case.provider.stop.assert_awaited_once()
    case.save.assert_awaited_once()
    case.handler.wait_escalation_create.assert_not_awaited()


async def bound_resume(env, *, outcome='satisfied'):
    """Park a real claim, reserve a seat, bind an entirely new process."""
    from dataclasses import replace
    from src.database.tables import admission_reservations, agents
    from src.scheduler import AssignAction
    from src.wait_runtime.resume import ResumeTransaction
    from src.models import RepoSourceType, Workspace

    await capture(env)
    await mail(env, 'held-task', to_kind='task', to_id='owner')
    await mail(env, 'held-old', to_id='s')
    await mail(env, 'foreign', to_id='someone-else')
    await mail(env, 'control', to_kind='task', to_id='owner', body_kind='wait_suspend')
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome=outcome, resolved_at=NOW + 10))
    await park(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(assigned_agent_id='a'))
        await conn.execute(update(agents).where(agents.c.id == 'a').values(
            state='BUSY', current_task_id='owner'))
        await conn.execute(update(continuations).values(
            resume_state='reserved', facts={'workspace_released_at': NOW + 20}, launch_attempts=1))
        await conn.execute(insert(admission_reservations).values(
            id='reservation', project_id='p', continuation_id='c', scope={},
            effective_priority=100, provenance='resume', deadline_at=NOW + 300,
            state='granted', created_at=NOW + 30))
    record = SessionRecord(
        id='fresh', task_id='owner', agent_id='a', project_id='p', profile_id='worker-codex',
        harness='codex', provider='fake', name='fresh', lifecycle='task', state='running',
        desired_state='running', work_dir='/tmp/fresh', epoch='boot', instance_token='fresh-token',
        last_claim_epoch=2, started_at=NOW + 30,
    )
    service = SimpleNamespace(orch=SimpleNamespace(db=env.db))
    driver = ResumeTransaction(service, clock=lambda: NOW + 30)
    action = AssignAction('a', 'owner', 'p', 'c', 1)
    await env.db.create_workspace(Workspace(
        id='resume-slot', project_id='p', workspace_path=record.work_dir,
        source_type=RepoSourceType.CLONE, locked_by_task_id='owner', locked_by_agent_id='a'))
    await driver.bind_launch(action, record)
    await env.db.create_session(record)
    scope = replace(env.scope, session_id='fresh', instance_token='fresh-token', claim_epoch=2)
    return driver, action, scope


@pytest.mark.parametrize('outcome', ['satisfied', 'cancelled'])
async def test_resume_ack_requires_exact_delivered_outcome_and_consumes_snapshot(env, outcome):
    from src.waits.resume_ack import acknowledge, mark_delivered
    from src.waits.holding import active_wait_for
    from src.waits.fencing import mutation_fence

    _, _, scope = await bound_resume(env, outcome=outcome)
    args = dict(wait_id=env.wait_id, continuation_id='c', generation=1)
    async with env.db._engine.begin() as conn:
        with pytest.raises(WaitError, match='delivered'):
            await acknowledge(conn, args, scope=scope, now=NOW + 40)
    assert (await env.db.get_task('owner')).status.value == 'RESUME_PENDING'
    assert await active_wait_for(env.db, await env.db.get_session('fresh'))
    principal = SimpleNamespace(session_id='fresh', session_instance_token='fresh-token')
    async with env.db._engine.connect() as conn:
        assert (await mutation_fence(conn, 'task_close', principal))['error_code'] == 'wait.ack_required'
        assert await mutation_fence(conn, 'wait_ack', principal) is None
    await mail(env, 'late', to_kind='task', to_id='owner')
    async with env.db._engine.begin() as conn:
        await mark_delivered(conn, continuation_id='c', session_id='fresh',
                             instance_token='fresh-token', claim_epoch=2, now=NOW + 40)
        first = await acknowledge(conn, args, scope=scope, now=NOW + 41)
    assert {m['id'] for m in first['held_messages']} == {'held-task', 'held-old'}
    assert (await env.db.get_task('owner')).status.value == 'IN_PROGRESS'
    assert await active_wait_for(env.db, await env.db.get_session('fresh')) is None
    async with env.db._engine.begin() as conn:
        assert await acknowledge(conn, args, scope=scope, now=NOW + 50) == first
        remaining = set((await conn.execute(select(messages.c.id).where(
            messages.c.delivered_at.is_(None)))).scalars())
        assert remaining == {'late', 'foreign', 'control'}
        assert await mutation_fence(conn, 'task_close', principal) is None


@pytest.mark.parametrize('outcome', ['satisfied', 'cancelled'])
async def test_resume_prime_stale_context_claim_guides_ack_then_prime(env, outcome):
    from tests.record_helpers import knowledge_config
    from tests.test_wait_dispatch import runtime

    await bound_resume(env, outcome=outcome)
    service = runtime(env, registry=build_registry())
    config = service.orch.config
    config.knowledge = knowledge_config()
    config.knowledge.context.enabled = config.memory.enabled = True
    handler = service.orch._command_handler
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=[
            'prime', 'wait_ack', 'knowledge_show', 'record_search',
        ]),
        session_id='fresh', session_instance_token='fresh-token', task_id='owner',
        project_id='p', profile_id='worker-codex',
    )
    scope = dict(kind='session', session_id='fresh', session_instance_token='fresh-token',
                 task_id='owner', project_id='p', elevated=False)
    with principal_context(principal):
        refused = await handler.execute('prime', {'_scope': scope})
        assert not refused['success'], refused
        assert refused['error_code'] == 'record.stale_claim'
        assert (f'`aq wait ack --wait-id {env.wait_id} --continuation-id c --generation 1` first'
                in refused['error'])
        assert '`aq prime` again' in refused['error']
        assert 'A cancelled wait outcome also requires acknowledgement' in refused['error']
        assert 'you still hold the task claim. Do not stop or exit.' in refused['error']
        assert (await intent(env))['resume_state'] == 'delivered'
        assert (await env.db.get_task('owner')).status.value == 'RESUME_PENDING'
        repeated = await handler.execute('prime', {'_scope': scope})
        assert repeated == refused

        acknowledged = await handler.execute('wait_ack', dict(
            _scope=scope, wait_id=env.wait_id, continuation_id='c', generation=1, claim_epoch=2,
        ))
        assert acknowledged['success'], acknowledged
        result = await handler.execute('prime', {'_scope': scope})
        assert result['success'], result
        assert result['context_state'] == 'prepared'
        assert '## Wait outcome' in result['body']

        # A later, genuinely stale holder still fails without resume guidance.
        async with env.db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
                claim_epoch=3, assigned_agent_id=None))
        stale = await handler.execute('prime', {'_scope': scope})
        assert not stale['success'] and stale['error_code'] == 'record.stale_claim'
        assert 'aq wait ack' not in stale['error']


@pytest.mark.parametrize('change', ['generation', 'session', 'token', 'epoch', 'pause'])
async def test_resume_ack_rejects_stale_or_paused_holder(env, change):
    from dataclasses import replace
    from src.waits.resume_ack import acknowledge, mark_delivered

    _, _, scope = await bound_resume(env)
    args = dict(wait_id=env.wait_id, continuation_id='c', generation=1)
    async with env.db._engine.begin() as conn:
        await mark_delivered(conn, continuation_id='c', session_id='fresh',
                             instance_token='fresh-token', claim_epoch=2, now=NOW + 40)
        if change == 'pause':
            await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(status='PAUSED'))
    if change == 'generation':
        args['generation'] = 2
    elif change == 'session':
        scope = replace(scope, session_id='s')
    elif change == 'token':
        scope = replace(scope, instance_token='other')
    elif change == 'epoch':
        scope = replace(scope, claim_epoch=3)
    async with env.db._engine.begin() as conn:
        with pytest.raises(WaitError):
            await acknowledge(conn, args, scope=scope, now=NOW + 45)
    assert (await intent(env))['resume_state'] == 'delivered'


async def test_ceiling_inherits_anchor_until_hibernation_ack(env):
    from src.waits.decision import ceiling_anchor

    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(
            sleep_decision='in_place', ceiling_anchor_at=NOW - 1000, created_at=NOW - 500))
        assert await ceiling_anchor(conn, env.scope, now=NOW, policy=Policy()) == NOW - 1000
        await conn.execute(update(continuations).values(
            resume_state='acknowledged', acknowledged_at=NOW - 100))
        assert await ceiling_anchor(conn, env.scope, now=NOW, policy=Policy()) == NOW


async def test_ceiling_requests_one_checkpoint_without_cancelling_source(env):
    from sqlalchemy import delete
    from src.wait_runtime.lifecycle import WaitLifecycle
    from src.database.tables import wait_deadlines, wait_edges

    config = AppConfig()
    config.wait_policy.hibernation_enabled = True
    lifecycle = WaitLifecycle(SimpleNamespace(orch=SimpleNamespace(db=env.db, config=config)))
    async with env.db._engine.begin() as conn:
        await conn.execute(delete(continuations))
        await conn.execute(update(wait_records).values(sleep_decision='in_place'))
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            branch_name='aq/owner', status='WAITING'))
        row = (await conn.execute(select(wait_records))).mappings().one()
        first = await lifecycle.ceiling(conn, {}, row, now=NOW + 1800, handler=None)
        row = (await conn.execute(select(wait_records))).mappings().one()
        replay = await lifecycle.ceiling(conn, {}, row, now=NOW + 1801, handler=None)
        assert first['suspended'] and not replay['suspended']
        assert row['outcome'] == 'pending' and row['decision_reason'] == 'ceiling'
        assert list((await conn.execute(select(wait_edges.c.active))).scalars()) == [True]
        deadlines = (await conn.execute(select(wait_deadlines).where(
            wait_deadlines.c.deadline_type == 'transition'))).mappings().all()
        assert len(deadlines) == 1 and deadlines[0]['due_at'] == NOW + 1920
        assert len(list((await conn.execute(select(messages.c.id).where(
            messages.c.body_kind == 'wait_suspend'))).scalars())) == 1


async def test_measurement_medians_exclude_old_and_incomplete_costs(env):
    from src.database.tables import wait_measurements
    from src.database.queries.continuation_queries import measurement_medians

    async with env.db._engine.begin() as conn:
        for i, (age, seconds, complete) in enumerate([
            (1, 10, True), (2, 20, True), (3, 30, True),
            (4, 40, True), (5, 50, True), (6, 60, False), (31 * 86400, 9999, True),
        ]):
            await conn.execute(insert(wait_measurements).values(
                id=str(i), project_id='p', wait_id=env.wait_id, condition_kind='task',
                source_class='task', decision='hibernate', waited_seconds=seconds,
                hibernated=True, evicted=False, teardown_seconds=seconds,
                setup_seconds=10, rebuild_seconds=20 if complete else None,
                recorded_at=NOW - age,
            ))
        result = await measurement_medians(conn, 'p', now=NOW)
        assert result['durations'][('task', 'task')]['median'] == 35
        assert result['durations'][('task', 'task')]['count'] == 6
        assert result['cost'] == {'count': 5, 'median': 60}


@pytest.mark.parametrize('times,priorities,costs,winner', [
    ([NOW, NOW + 1], [100, 1], [1, 100], 'second'),
    ([NOW, NOW], [100, 200], [1, 100], 'second'),
    ([NOW, NOW], [100, 100], [1, 100], 'owner'),
])
async def test_eviction_chooses_least_sunk_then_priority_then_cost(
    env, times, priorities, costs, winner,
):
    from sqlalchemy import delete
    from src.wait_runtime.eviction import candidate, evict
    from src.wait_runtime.lifecycle import WaitLifecycle

    second = await owner(env.db, 'second', 's2', 'a2')
    async with env.db._engine.begin() as conn:
        await conn.execute(delete(continuations))
        registered = await register_wait(
            conn, scope=second, condition={'kind': 'task', 'task_id': 'source'},
            idempotency_key='second-wait', registry=build_registry(clock=lambda: NOW),
            policy=Policy(), decide_sleep=lambda *_: SleepDecision('in_place', 'short'),
            now=NOW, source_watermark=0,
        )
        for i, (task_id, wait_id) in enumerate([('owner', env.wait_id),
                                               ('second', registered['wait_id'])]):
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(
                status='WAITING', branch_name=f'aq/{task_id}', effective_priority=priorities[i]))
            row = (await conn.execute(select(wait_records).where(
                wait_records.c.id == wait_id))).mappings().one()
            await conn.execute(update(wait_records).where(wait_records.c.id == wait_id).values(
                sleep_decision='in_place', created_at=times[i],
                decision_inputs={**row['decision_inputs'], 'resume_cost_minutes': costs[i]}))
        selected = await candidate(conn, 'p', 'worker-codex')
        assert selected['owner_id'] == winner
    config = AppConfig()
    config.wait_policy.hibernation_enabled = True
    lifecycle = WaitLifecycle(SimpleNamespace(orch=SimpleNamespace(db=env.db, config=config)),
                              clock=lambda: NOW + 10)
    args = {'wait_id': selected['id'], 'reason': 'pressure'}
    assert (await evict(lifecycle, args))['suspended']
    assert not (await evict(lifecycle, args))['suspended']
    row = await intent(env)
    assert row['task_id'] == winner and row['stopped_at'] is None
    assert (await env.db.get_task(winner)).status.value == 'WAITING'


async def test_pool_eviction_keeps_the_placed_workspace_kind_through_reselection(env):
    from sqlalchemy import delete

    from src.models import RepoSourceType, Workspace
    from src.scheduler import PlacedStart, PoolKey
    from src.wait_runtime.eviction import candidate, evict, request_evictions
    from src.wait_runtime.lifecycle import WaitLifecycle

    second = await owner(env.db, 'second', 's2', 'a2')
    await env.db.update_session('s2', work_dir='/tmp/native-wait')
    for workspace_id, path, kind in (
        ('ordinary', '/tmp/wait-v2', 'project-repo'),
        ('native', '/tmp/native-wait', 'native'),
    ):
        await env.db.create_workspace(Workspace(
            id=workspace_id, project_id='p', workspace_path=path,
            source_type=RepoSourceType.CLONE, kind_id=kind,
        ))
    async with env.db._engine.begin() as conn:
        await conn.execute(delete(continuations))
        await register_wait(
            conn, scope=second, condition={'kind': 'task', 'task_id': 'source'},
            idempotency_key='native-wait', registry=build_registry(clock=lambda: NOW),
            policy=Policy(), decide_sleep=lambda *_: SleepDecision('in_place', 'short'),
            now=NOW + 1, source_watermark=0,
        )
        await conn.execute(update(tasks).where(tasks.c.id.in_(['owner', 'second'])).values(
            status='WAITING', branch_name='aq/owner'))
        await conn.execute(update(wait_records).where(
            wait_records.c.id == env.wait_id).values(sleep_decision='in_place'))
        assert (await candidate(conn, 'p', 'worker-codex'))['owner_id'] == 'second'
    config = AppConfig()
    config.wait_policy.hibernation_enabled = True
    orch = SimpleNamespace(db=env.db, config=config)
    lifecycle = WaitLifecycle(SimpleNamespace(orch=orch), clock=lambda: NOW + 10)

    async def command(name, args):
        assert name == 'wait_evict'
        return await evict(lifecycle, args)

    orch.wait_service = SimpleNamespace(command=AsyncMock(side_effect=command))
    assert await request_evictions(orch, [PlacedStart(
        PoolKey('worker-codex'), 'p', 1, 'evict', 'project-repo',
    )]) == []
    assert (await intent(env))['task_id'] == 'owner'
    async with env.db._engine.connect() as conn:
        assert (await candidate(conn, 'p', 'worker-codex',
                                workspace_kind='native'))['owner_id'] == 'second'


async def test_completed_eviction_reenters_pool_arbitration_immediately(env):
    from src.wait_runtime.lifecycle import WaitLifecycle

    config = AppConfig()
    config.wait_policy.hibernation_enabled = True
    orch = SimpleNamespace(db=env.db, config=config, _reconcile_pools=AsyncMock())
    lifecycle = WaitLifecycle(SimpleNamespace(orch=orch))
    lifecycle.hibernate.advance = AsyncMock(return_value={'success': True, 'parked': True})
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(decision_reason='evicted'))
    await lifecycle.sleep_decide({'wait_id': env.wait_id}, principal=None, scope=None, handler=None)
    lifecycle.hibernate.advance.assert_awaited_once()
    orch._reconcile_pools.assert_awaited_once()


async def test_parked_mail_remains_held_until_resume_ack(env):
    from src.waits.holding import message_is_held

    await park(env)
    await mail(env, 'parked-task', to_kind='task', to_id='owner')
    await mail(env, 'parked-session', to_id='s')
    await mail(env, 'elsewhere', to_id='other')
    async with env.db._engine.connect() as conn:
        held = set((await conn.execute(select(messages.c.id).where(message_is_held()))).scalars())
    assert held == {'parked-task', 'parked-session'}


async def test_failed_resume_retries_once_then_holds_without_frontier_requeue(env):
    from src.wait_runtime.hibernate import HibernationTransaction

    driver, action, _ = await bound_resume(env)
    await env.db.update_session('fresh', state='stopped', desired_state='stopped')
    # This retry case starts after the failed writer's checkout was released.
    await env.db.update_workspace('resume-slot', locked_by_task_id=None, locked_by_agent_id=None)
    service = driver.service
    service.lifecycle = SimpleNamespace(hibernate=HibernationTransaction(service))
    driver.orch.token_store = SimpleNamespace(revoke_session=AsyncMock())
    handler = SimpleNamespace(wait_escalation_create=AsyncMock(
        return_value={'success': True, 'escalation': {'id': 'incident'}}))
    task = await env.db.get_task('owner')
    assert (await driver.failed(action, task, 'first spawn failed', handler=handler))['retry_queued']
    assert (await env.db.get_task('owner')).status.value == 'RESUME_PENDING'
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(
            resume_state='reserved', launch_attempts=2, new_session_id=None))
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(assigned_agent_id='a'))
    result = await driver.failed(action, task, 'second spawn failed', handler=handler)
    assert result['held'] and result['hold_reason'] == 'resume_launch_failed'
    assert (await intent(env))['launch_attempts'] == 2
    assert (await env.db.get_task('owner')).status.value == 'RESUME_PENDING'
    handler.wait_escalation_create.assert_awaited_once()

    metrics = await env.db.metrics_waits_snapshot(NOW + 600)
    assert metrics['wakes_1h']['launch_failed'] == 2
    assert metrics['wakes_1h']['other'] == 0


@pytest.mark.parametrize('setup_fails', [False, True])
async def test_fresh_resume_builds_new_git_folder_without_ignored_transcript(env, tmp_path, setup_fails):
    from pathlib import Path
    from src.git.manager import GitManager
    from src.models import Workspace, RepoSourceType
    from src.wait_runtime.resume import ResumeTransaction
    from src.scheduler import AssignAction

    git = GitManager()
    base_path = tmp_path / 'base'
    base_path.mkdir()
    await git._arun(['init', '-b', 'main'], cwd=str(base_path))
    await git._arun(['config', 'user.email', 'test@example.test'], cwd=str(base_path))
    await git._arun(['config', 'user.name', 'Test'], cwd=str(base_path))
    (base_path / '.gitignore').write_text('transcript.cache\n')
    await git._arun(['add', '.gitignore'], cwd=str(base_path))
    await git._arun(['commit', '-m', 'base'], cwd=str(base_path))
    head = await git._arun(['rev-parse', 'HEAD'], cwd=str(base_path))
    old = tmp_path / 'old-slot'
    await git._arun(['worktree', 'add', '-b', 'aq/owner', str(old)], cwd=str(base_path))
    (old / 'transcript.cache').write_text('old process transcript')
    await park(env, saved_head=head)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(resume_state='reserved', launch_attempts=1))
    workspace = Workspace(id='slot', project_id='p', workspace_path=str(old),
                          slot_index=0, source_type=RepoSourceType.CLONE,
                          base_workspace_id='base', kind_id='project-repo')
    base = Workspace(id='base', project_id='p', workspace_path=str(base_path),
                     source_type=RepoSourceType.CLONE)
    env.db.get_workspace_for_task = AsyncMock(return_value=workspace)
    env.db.get_workspace = AsyncMock(return_value=base)
    env.db.resolve_workspace_kind = AsyncMock(return_value=SimpleNamespace())
    env.db.update_workspace = AsyncMock()
    slots = SimpleNamespace(
        reprovision_slot=AsyncMock(),
        read_sentinel=Mock(return_value=WorktreeSentinel('slot-0', 0, 'base', 'p')),
        write_sentinel=Mock(),
    )
    if setup_fails:
        slots.reprovision_slot.side_effect = RuntimeError('setup failed')
    orch = SimpleNamespace(db=env.db, git=git, _prepare_workspace_locked=AsyncMock(
        return_value=str(old)), _git_mutex=lambda _: asyncio.Lock(), _worktree_slots=lambda: slots,
        _worktree_base_paths={})
    driver = ResumeTransaction(SimpleNamespace(orch=orch))
    args = (AssignAction('a', 'owner', 'p', 'c', 1),
            await env.db.get_task('owner'), None, await intent(env))
    if setup_fails:
        with pytest.raises(RuntimeError, match='setup failed'):
            await driver.fresh_workspace(*args)
        target = (await intent(env))['facts']['fresh_workspace']['new_path']
    else:
        target = await driver.fresh_workspace(*args)
    assert target != str(old) and Path(target).is_dir()
    assert not (Path(target) / 'transcript.cache').exists()
    assert await git._arun(['rev-parse', 'HEAD'], cwd=target) == head
    assert await git._arun(['branch', '--show-current'], cwd=target) == 'aq/owner'
    env.db.update_workspace.assert_awaited_once_with('slot', workspace_path=target)
    slots.reprovision_slot.assert_awaited_once()


@pytest.fixture
async def cold_restart_checkout(env, tmp_path):
    from src.git.manager import GitManager
    from src.models import RepoConfig, RepoSourceType, Workspace

    git = GitManager()
    origin = tmp_path / 'origin.git'
    await git._arun(['init', '--bare', str(origin)])
    base_path = tmp_path / 'base'
    await git._arun(['clone', str(origin), str(base_path)])
    await git._arun(['config', 'user.email', 'test@example.test'], cwd=str(base_path))
    await git._arun(['config', 'user.name', 'Test'], cwd=str(base_path))
    await git._arun(['switch', '-c', 'main'], cwd=str(base_path))
    (base_path / 'content').write_text('saved work\n')
    await git._arun(['add', 'content'], cwd=str(base_path))
    await git._arun(['commit', '-m', 'base'], cwd=str(base_path))
    await git._arun(['push', 'origin', 'main'], cwd=str(base_path))
    head = await git._arun(['rev-parse', 'HEAD'], cwd=str(base_path))
    old = base_path / '.aq' / 'worktrees' / 'slot-0'
    target = old.parent / 'resume-c-1'
    duplicate = old.parent / 'slot-1'
    await git._arun(['worktree', 'add', '-b', 'aq/owner', str(old)], cwd=str(base_path))
    await git._arun(['push', 'origin', 'aq/owner'], cwd=str(old))
    await git._arun(['switch', '--detach'], cwd=str(old))
    await git._arun(['worktree', 'add', str(target), 'aq/owner'], cwd=str(base_path))
    await git._arun(['worktree', 'add', '--detach', str(duplicate), 'main'], cwd=str(base_path))
    await env.db.create_repo(RepoConfig(id='repo', project_id='p', url=str(origin),
                                      source_type=RepoSourceType.CLONE, default_branch='main'))
    await env.db.update_project('p', integration_repository_id='repo')
    await env.db.create_workspace(Workspace(id='base', project_id='p',
        workspace_path=str(base_path), source_type=RepoSourceType.CLONE))
    for index, path in enumerate((old, duplicate)):
        await env.db.create_workspace(Workspace(id=f'slot-{index}', project_id='p',
            workspace_path=str(path), source_type=RepoSourceType.WORKTREE,
            base_workspace_id='base', slot_index=index, locked_by_task_id='owner',
            locked_by_agent_id='a'))
    await park(env, saved_head=head)
    await env.db.update_session('s', state='stopped', desired_state='stopped')
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(resume_state='held', launch_attempts=1,
            hold_reason='resume_workspace_failed', facts={'fresh_workspace': {
                'workspace_id': 'slot-0', 'old_path': str(old), 'new_path': str(target),
                'attempt': 1}}))
    provider = SimpleNamespace(confirm_stopped=AsyncMock(return_value=True))
    locks = {}
    orch = SimpleNamespace(git=git, config=AppConfig(),
        _git_mutex=lambda path: locks.setdefault(path, asyncio.Lock()),
        _task_control_lock=lambda task: locks.setdefault(('task', task), asyncio.Lock()),
        session_providers=SimpleNamespace(create=lambda *_: provider))
    return SimpleNamespace(env=env, git=git, old=old, target=target, duplicate=duplicate,
        base=base_path, provider=provider, command=SimpleNamespace(db=env.db, orchestrator=orch))


@pytest.mark.parametrize('relocation_recorded', [False, True])
@pytest.mark.parametrize('prior_resume', [False, True])
async def test_cold_restart_releases_staged_branch_and_all_reservations(
    cold_restart_checkout, relocation_recorded, prior_resume,
):
    from src.commands.task_commands import TaskCommandsMixin
    from src.database.tables import admission_reservations
    from src.models import TaskStatus

    case = cold_restart_checkout
    db = case.env.db
    if prior_resume:
        old = case.old.parent / 'resume-prior-1'
        await case.git._arun(['worktree', 'move', str(case.old), str(old)], cwd=str(case.base))
        async with db._engine.begin() as conn:
            current = await intent(case.env)
            await conn.execute(insert(continuations).values(id='prior', task_id='owner',
                wait_id=case.env.wait_id, suspension_generation=0, branch='aq/owner',
                prior_status='IN_PROGRESS', held_cursor_seq=0, resume_state='acknowledged',
                facts={'fresh_workspace': {'workspace_id': 'slot-0', 'attempt': 1,
                    'old_path': str(case.old), 'new_path': str(old)}}))
            stage = {**current['facts']['fresh_workspace'], 'old_path': str(old)}
            await conn.execute(update(continuations).where(continuations.c.id == 'c').values(
                facts={'fresh_workspace': stage}))
        case.old = old
        await db.update_workspace('slot-0', workspace_path=str(old))
    if relocation_recorded:
        await db.update_workspace('slot-0', workspace_path=str(case.target))
        await case.git._arun(['worktree', 'remove', str(case.old)], cwd=str(case.base))
    async with db._engine.begin() as conn:
        await conn.execute(insert(admission_reservations).values(id='reservation',
            project_id='p', task_id='owner', continuation_id='c', state='granted',
            scope={}, provenance='resume', created_at=NOW, effective_priority=100,
            deadline_at=NOW + 300))
    result = await TaskCommandsMixin._cmd_restart_task(case.command, {'task_id': 'owner'})
    assert result['success'], result
    assert (await db.get_task('owner')).status == TaskStatus.READY
    assert all(not ws.locked_by_task_id for ws in await db.list_workspaces('p'))
    assert await case.git._arun(['branch', '--show-current'], cwd=str(case.target)) == ''
    assert (case.target / 'content').read_text() == 'saved work\n'
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(continuations.c.resume_state).where(
            continuations.c.id == 'c')) == 'cancelled'
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(admission_reservations.c.state)) == 'cancelled'
    case.provider.confirm_stopped.assert_awaited_once()
    # Replay must not reattach the branch, recreate reservations or reprobe writers.
    assert (await TaskCommandsMixin._cmd_restart_task(case.command, {'task_id': 'owner'}))['success']
    case.provider.confirm_stopped.assert_awaited_once()
    # This is the Git operation that the next claim previously could not perform.
    await case.git._arun(['switch', 'aq/owner'], cwd=str(case.duplicate))


async def test_cold_restart_waits_for_resume_workspace_transaction(cold_restart_checkout):
    from src.commands.task_commands import TaskCommandsMixin

    case = cold_restart_checkout
    lock = case.command.orchestrator._task_control_lock('owner')
    async with lock:
        restart = asyncio.create_task(TaskCommandsMixin._cmd_restart_task(
            case.command, {'task_id': 'owner'}))
        await asyncio.sleep(0)
        assert not restart.done()
        case.provider.confirm_stopped.assert_not_awaited()
    assert (await restart)['success']


@pytest.mark.parametrize('status', ['WAITING', 'HIBERNATED', 'RESUME_PENDING'])
async def test_cold_restart_readmits_after_continuation_retirement(env, status):
    from src.commands.task_commands import TaskCommandsMixin
    from src.models import TaskStatus

    await park(env)
    await env.db.update_session('s', state='stopped', desired_state='stopped')
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(resume_state='cancelled', facts={}))
        await conn.execute(update(wait_records).values(
            outcome='cancelled', cleared_at=NOW + 30, blocking=False))
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(status=status))
    before = await env.db.get_task('owner')
    command = SimpleNamespace(db=env.db)
    result = await TaskCommandsMixin._cmd_restart_task(command, {'task_id': 'owner'})
    assert result['success'], result
    task = await env.db.get_task('owner')
    assert task.status is TaskStatus.READY and task.assigned_agent_id is None
    assert task.claim_epoch == before.claim_epoch


@pytest.mark.parametrize('blocker', ['task_control', 'task_row', 'recovery_guard'])
async def test_cold_restart_deadline_reports_lock_and_releases_resources(
    env, monkeypatch, caplog, blocker,
):
    from src.commands.task_commands import TaskCommandsMixin
    from src.wait_runtime import restart

    monkeypatch.setattr(restart, 'RESTART_TIMEOUT_SECONDS', 1)
    lock = asyncio.Lock()
    command = SimpleNamespace(db=env.db, orchestrator=SimpleNamespace(
        _task_control_lock=lambda _: lock))
    if blocker == 'recovery_guard':
        async def stalled(*_):
            await asyncio.Event().wait()
        monkeypatch.setattr(env.db, 'guard_restart_recovery_hold', stalled)
    async with env.db._engine.begin() as holder:
        if blocker == 'task_control':
            await lock.acquire()
        elif blocker == 'task_row':
            await holder.execute(select(tasks).where(tasks.c.id == 'owner').with_for_update())
        result = await TaskCommandsMixin._cmd_restart_task(command, {'task_id': 'owner'})
        assert not result['success'] and result['error_code'] == 'task_restart.timeout', result
        phase = 'task-control lock' if blocker == 'task_control' else 'recovery hold guard'
        assert phase in result['error'] and phase in caplog.text
        assert 'task_id=owner' in caplog.text
        if blocker == 'task_control':
            lock.release()
    assert not lock.locked()
    assert env.db._engine.pool.checkedout() == 0
    # Cancellation rolled back and returned the connection; the row is usable.
    async with env.db._engine.begin() as conn:
        await conn.execute(select(tasks).where(tasks.c.id == 'owner').with_for_update(nowait=True))


@pytest.mark.parametrize('blocker', ['repository', 'graph'])
async def test_cold_restart_cleanup_deadline_retains_ownership(
    cold_restart_checkout, monkeypatch, caplog, blocker,
):
    from src.commands.task_commands import TaskCommandsMixin
    from src.wait_runtime import restart
    from src.waits.graph import lock_graph

    case = cold_restart_checkout
    # Allow the real Git proofs, then expire promptly on the selected lock.
    monkeypatch.setattr(restart, 'RESTART_TIMEOUT_SECONDS', 5)
    mutex = case.command.orchestrator._git_mutex(str(case.base))
    async with case.env.db._engine.begin() as holder:
        if blocker == 'repository':
            await mutex.acquire()
        else:
            await lock_graph(holder)
        try:
            result = await TaskCommandsMixin._cmd_restart_task(case.command, {'task_id': 'owner'})
        finally:
            if blocker == 'repository':
                mutex.release()
    assert result['error_code'] == 'task_restart.timeout', result
    phase = 'repository lock/Git' if blocker == 'repository' else 'wait graph lock'
    assert phase in result['error'] and phase in caplog.text
    assert not case.command.orchestrator._task_control_lock('owner').locked()
    assert (await case.env.db.get_workspace('slot-0')).locked_by_task_id == 'owner'
    assert (await intent(case.env))['resume_state'] == 'held'
    assert case.env.db._engine.pool.checkedout() == 0
    # Detach is idempotent; a timeout must leave the next restart able to finish.
    monkeypatch.setattr(restart, 'RESTART_TIMEOUT_SECONDS', 20)
    assert (await TaskCommandsMixin._cmd_restart_task(case.command, {'task_id': 'owner'}))['success']


@pytest.mark.parametrize('drift', ['task', 'workspace', 'session'])
async def test_cold_restart_revalidates_after_external_proofs(cold_restart_checkout, drift):
    from src.commands.task_commands import TaskCommandsMixin
    from src.database.tables import workspaces

    case = cold_restart_checkout
    epoch = (await case.env.db.get_task('owner')).claim_epoch
    table, key, values = {
        'task': (tasks, 'owner', {'claim_epoch': epoch + 1}),
        'workspace': (workspaces, 'slot-0', {'generation': 2}),
        'session': (sessions, 's', {'instance_token': 'replacement'}),
    }[drift]

    async def stopped(_):
        async with case.env.db._engine.begin() as conn:
            await conn.execute(update(table).where(table.c.id == key).values(**values))
        return True

    case.provider.confirm_stopped.side_effect = stopped
    result = await TaskCommandsMixin._cmd_restart_task(case.command, {'task_id': 'owner'})
    assert result['error_code'] == 'continuation.stale_generation', result
    assert (await case.env.db.get_workspace('slot-0')).locked_by_task_id == 'owner'
    assert (await intent(case.env))['resume_state'] == 'held'


@pytest.mark.parametrize('blocker', [
    'live', 'unknown_stop', 'stop_error', 'dirty', 'unpushed', 'head_drift',
    'reused', 'reused_agent', 'pinned',
])
async def test_cold_restart_refuses_without_losing_ownership(cold_restart_checkout, blocker):
    from src.commands.task_commands import TaskCommandsMixin
    from src.database.tables import workspaces

    case = cold_restart_checkout
    db = case.env.db
    if blocker == 'live':
        await db.create_session(SessionRecord(id='live-writer', project_id='p',
            profile_id='worker', harness='codex', provider='fake', name='live-writer', lifecycle='task',
            work_dir=str(case.target), task_id='owner', epoch='boot',
            instance_token='live-token', state='running', desired_state='running',
            started_at=NOW, last_activity=NOW))
    elif blocker == 'unknown_stop':
        case.provider.confirm_stopped.return_value = False
    elif blocker == 'stop_error':
        case.provider.confirm_stopped.side_effect = RuntimeError('provider unavailable')
    elif blocker == 'dirty':
        (case.target / 'unsaved').write_text('retain this')
    elif blocker == 'unpushed':
        (case.target / 'content').write_text('unpublished work')
        await case.git._arun(['commit', '-am', 'unpublished'], cwd=str(case.target))
    elif blocker == 'head_drift':
        async with db._engine.begin() as conn:
            await conn.execute(update(continuations).values(saved_head='f' * 40))
    elif blocker == 'reused':
        await db.create_task(Task(id='other-owner', project_id='p', title='Other', description=''))
        await db.update_workspace('slot-0', locked_by_task_id='other-owner')
    elif blocker == 'reused_agent':
        await db.update_workspace('slot-0', locked_by_task_id=None)
    else:
        async with db._engine.begin() as conn:
            await conn.execute(update(workspaces).where(workspaces.c.id == 'slot-0').values(
                job_pin_count=1))
    before = await db.get_task('owner')
    result = await TaskCommandsMixin._cmd_restart_task(case.command, {'task_id': 'owner'})
    assert not result['success'], result
    assert (await db.get_task('owner')).status == before.status
    assert (await db.get_workspace('slot-1')).locked_by_task_id == 'owner'
    assert (await intent(case.env))['resume_state'] == 'held'
    assert case.target.is_dir()


async def fresh_rig(env, tmp_path):
    """A slot holding the parked branch, beside a peer whose folder is gone.

    The real ``WorktreeSlotManager`` provisions the fresh checkout, so a drift
    in its ``reprovision_slot`` signature fails here rather than in production.
    """
    from collections import defaultdict
    from pathlib import Path

    from src.config import WorktreesConfig
    from src.git.manager import GitManager
    from src.models import KIND_MODE_WORKTREE, RepoSourceType, Workspace, WorkspaceKind
    from src.orchestrator.worktree_manager import WorktreeSlotManager
    from src.wait_runtime.resume import ResumeTransaction
    from tests.git_seed_helpers import clone_seed, init_bare_seed

    git = GitManager()
    origin, base_path = tmp_path / 'origin.git', tmp_path / 'base'
    init_bare_seed(origin)
    clone_seed(origin, base_path)

    async def run(args, cwd=base_path):
        return await git._arun(['-c', 'user.name=T', '-c', 'user.email=t@t.test', *args],
                               cwd=str(cwd))

    (base_path / '.gitignore').write_text('transcript.cache\n')
    await run(['add', '.gitignore'])
    await run(['commit', '-m', 'base'])
    await run(['push', 'origin', 'main'])
    worktrees = base_path / '.aq' / 'worktrees'
    old, peer = worktrees / 'slot-0', worktrees / 'slot-1'
    await run(['worktree', 'add', '--detach', str(peer)])
    shutil.rmtree(peer)  # registration retained: preserved evidence, never pruned
    await run(['worktree', 'add', '-b', 'aq/owner', str(old)])
    (old / 'saved.txt').write_text('saved work\n')
    await run(['add', 'saved.txt'], old)
    await run(['commit', '-m', 'saved work'], old)
    (old / 'transcript.cache').write_text('old process transcript')
    head = await git._arun(['rev-parse', 'HEAD'], cwd=str(old))
    await park(env, saved_head=head)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(resume_state='reserved', launch_attempts=1))
    workspace = Workspace(id='slot', project_id='p', workspace_path=str(old), slot_index=0,
                          source_type=RepoSourceType.WORKTREE, base_workspace_id='base',
                          kind_id='project-repo')
    base = Workspace(id='base', project_id='p', workspace_path=str(base_path),
                     source_type=RepoSourceType.CLONE, kind_id='project-repo')
    env.db.get_workspace_for_task = AsyncMock(return_value=workspace)
    env.db.get_workspace = AsyncMock(return_value=base)
    env.db.resolve_workspace_kind = AsyncMock(return_value=WorkspaceKind(
        project_id='__system__', id='project-repo', is_git_repo=True, mode=KIND_MODE_WORKTREE))
    env.db.update_workspace = AsyncMock()
    locks = defaultdict(asyncio.Lock)
    slots = WorktreeSlotManager(db=env.db, git=git, bus=SimpleNamespace(emit=AsyncMock()),
                                config=WorktreesConfig(enabled=True, setup_timeout_seconds=60),
                                git_mutex=lambda path: locks[path])
    slots.reprovision_slot = AsyncMock(wraps=slots.reprovision_slot)
    orch = SimpleNamespace(db=env.db, git=git, _prepare_workspace_locked=AsyncMock(
        return_value=str(old)), _git_mutex=lambda path: locks[path], _worktree_slots=lambda: slots,
        _worktree_base_paths={})

    async def registered():
        return {Path(e['path']).resolve() for e in await git.aworktree_list(str(base_path))}

    return SimpleNamespace(
        git=git, run=run, base=base_path, old=old, peer=peer, head=head, slots=slots, orch=orch,
        target=worktrees / 'resume-c-1', registered=registered,
        driver=ResumeTransaction(SimpleNamespace(orch=orch), clock=lambda: NOW + 30))


async def resume_fresh(env, rig):
    from src.scheduler import AssignAction

    return await rig.driver.fresh_workspace(AssignAction('a', 'owner', 'p', 'c', 1),
        await env.db.get_task('owner'), None, await intent(env))


async def stage_recovery(env, rig):
    """The state a restart leaves after ``worktree add`` and before reprovision."""
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(facts={'fresh_workspace': {
            'old_path': str(rig.old), 'new_path': str(rig.target), 'workspace_id': 'slot',
            'attempt': 1}}))
    await rig.git._arun(['switch', '--detach'], cwd=str(rig.old))
    await rig.git._arun(['worktree', 'add', str(rig.target), 'aq/owner'], cwd=str(rig.base))


def assert_fresh_checkout(env, rig, target):
    from pathlib import Path

    from src.orchestrator.worktree_manager import WorktreeSlotManager

    assert Path(target) == rig.target
    sentinel = WorktreeSlotManager.read_sentinel(target)
    assert (sentinel.workspace_id, sentinel.base_workspace_id, sentinel.slot) == (
        'slot', 'base', 'slot-0')
    assert (sentinel.task_id, sentinel.branch, sentinel.assigned_at) == (
        'owner', 'aq/owner', NOW + 30)
    assert sentinel.project_id == 'p'
    assert sentinel.setup_hash
    assert rig.slots.reprovision_slot.await_args.kwargs == {'prune': False}
    assert not (Path(target) / 'transcript.cache').exists()
    assert (Path(target) / 'saved.txt').read_text() == 'saved work\n'
    env.db.update_workspace.assert_awaited_once_with('slot', workspace_path=str(target))
    assert not rig.old.exists()


async def assert_resumed_binding(env, rig, target, *, outcome='satisfied'):
    """Bind the new process and prove the sentinel plus durable receipt agree."""
    from src.claim_file import write_claim_file
    from src.database.tables import admission_reservations, workspaces
    from src.models import RepoSourceType, Workspace
    from src.orchestrator.workspace_binding_audit import reconcile_slot_bindings
    from src.scheduler import AssignAction

    del env.db.get_workspace
    await env.db.create_workspace(Workspace(
        id='base', project_id='p', workspace_path=str(rig.base),
        source_type=RepoSourceType.CLONE))
    await env.db.create_workspace(Workspace(
        id='slot', project_id='p', workspace_path=target,
        source_type=RepoSourceType.WORKTREE, slot_index=0, base_workspace_id='base',
        locked_by_task_id='owner', locked_by_agent_id='a'))
    async with env.db._engine.begin() as conn:
        await conn.execute(update(workspaces).where(workspaces.c.id == 'slot').values(generation=103))
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            assigned_agent_id='a', status='RESUME_PENDING'))
        await conn.execute(update(wait_records).values(outcome=outcome, resolved_at=NOW + 25))
        await conn.execute(insert(admission_reservations).values(
            id='reservation', project_id='p', continuation_id='c', scope={},
            effective_priority=100, provenance='resume', deadline_at=NOW + 300,
            state='granted', created_at=NOW + 30))
    record = SessionRecord(
        id='fresh', task_id='owner', agent_id='a', project_id='p', profile_id='worker-codex',
        harness='codex', provider='fake', name='fresh', lifecycle='task', state='running',
        work_dir=target, epoch='boot', instance_token='fresh-token',
        last_claim_epoch=2, started_at=NOW + 30)
    await env.db.set_task_meta('owner', 'claim_workspace', {'session_id': 'predecessor'})
    await rig.driver.bind_launch(AssignAction('a', 'owner', 'p', 'c', 1), record)
    assert await env.db.get_task_meta('owner', 'claim_workspace') == {
        'workspace_id': 'slot', 'generation': 103, 'session_id': 'fresh',
        'instance_token': 'fresh-token', 'claim_epoch': 2}
    await env.db.create_session(record)
    write_claim_file(target, {'task_id': 'owner', 'claim_epoch': 2, 'session_id': 'fresh'})
    assert await reconcile_slot_bindings(env.db, rig.slots, 'p') == []


@pytest.mark.parametrize('outcome', ['satisfied', 'cancelled'])
async def test_fresh_resume_reprovisions_through_real_slot_manager_without_pruning(
    env, tmp_path, outcome,
):
    rig = await fresh_rig(env, tmp_path)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome=outcome, resolved_at=NOW + 25))

    target = await resume_fresh(env, rig)

    assert_fresh_checkout(env, rig, target)
    registered = await rig.registered()
    assert rig.peer.resolve() in registered
    assert rig.target.resolve() in registered and rig.old.resolve() not in registered
    assert await rig.git._arun(['rev-parse', 'HEAD'], cwd=target) == rig.head
    assert await rig.git._arun(['branch', '--show-current'], cwd=target) == 'aq/owner'
    assert (await intent(env))['facts']['fresh_workspace'] == {
        'old_path': str(rig.old), 'new_path': str(rig.target), 'workspace_id': 'slot',
        'attempt': 1}
    await assert_resumed_binding(env, rig, target, outcome=outcome)


@pytest.mark.parametrize('outcome', ['satisfied', 'cancelled'])
async def test_restart_recovery_adopts_added_checkout_and_retires_old_folder(env, tmp_path, outcome):
    rig = await fresh_rig(env, tmp_path)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome=outcome, resolved_at=NOW + 25))
    await stage_recovery(env, rig)

    target = await resume_fresh(env, rig)

    rig.orch._prepare_workspace_locked.assert_not_awaited()
    assert_fresh_checkout(env, rig, target)
    registered = await rig.registered()
    assert rig.peer.resolve() in registered and rig.old.resolve() not in registered
    assert await rig.git._arun(['rev-parse', 'HEAD'], cwd=target) == rig.head
    await assert_resumed_binding(env, rig, target, outcome=outcome)


@pytest.mark.parametrize('drift', ['dirty', 'branch', 'ancestry'])
async def test_restart_recovery_fences_refuse_before_reprovision(env, tmp_path, drift):
    from src.git.manager import GitError

    rig = await fresh_rig(env, tmp_path)
    await stage_recovery(env, rig)
    if drift == 'dirty':
        (rig.target / 'stray.txt').write_text('unsaved\n')
    elif drift == 'branch':
        await rig.run(['switch', '-c', 'aq/other'], rig.target)
    else:
        await rig.run(['commit', '--allow-empty', '-m', 'elsewhere'])
        side = await rig.git._arun(['rev-parse', 'HEAD'], cwd=str(rig.base))
        async with env.db._engine.begin() as conn:
            await conn.execute(update(continuations).values(saved_head=side))

    with pytest.raises(GitError if drift == 'ancestry' else WaitError) as raised:
        await resume_fresh(env, rig)

    if drift != 'ancestry':
        assert raised.value.code == 'continuation.head_drift'
    rig.orch._prepare_workspace_locked.assert_not_awaited()
    rig.slots.reprovision_slot.assert_not_awaited()
    env.db.update_workspace.assert_not_awaited()
    assert rig.old.is_dir() and rig.target.is_dir()
    assert {rig.old.resolve(), rig.target.resolve(), rig.peer.resolve()} <= await rig.registered()


@pytest.mark.parametrize('drift', ['dirty', 'ancestry'])
async def test_fresh_resume_fences_refuse_before_any_git_mutation(env, tmp_path, drift):
    from src.git.manager import GitError

    rig = await fresh_rig(env, tmp_path)
    if drift == 'dirty':
        (rig.old / 'saved.txt').write_text('edited after save\n')
    else:
        async with env.db._engine.begin() as conn:
            await conn.execute(update(continuations).values(saved_head='f' * 40))

    with pytest.raises(WaitError if drift == 'dirty' else GitError):
        await resume_fresh(env, rig)

    rig.slots.reprovision_slot.assert_not_awaited()
    env.db.update_workspace.assert_not_awaited()
    assert not rig.target.exists()
    assert await rig.git._arun(['branch', '--show-current'], cwd=str(rig.old)) == 'aq/owner'
    assert 'fresh_workspace' not in (await intent(env))['facts']


@pytest.mark.parametrize('owner', ['row', 'dirty'])
async def test_retire_checkout_keeps_a_folder_a_row_names_or_that_holds_work(
    env, tmp_path, owner,
):
    from src.models import RepoSourceType, Workspace

    rig = await fresh_rig(env, tmp_path)
    await stage_recovery(env, rig)
    if owner == 'row':
        await env.db.create_workspace(Workspace(id='other', project_id='p',
            workspace_path=str(rig.old), source_type=RepoSourceType.WORKTREE))
    else:
        (rig.old / 'stray.txt').write_text('unsaved\n')

    await rig.driver.retire_checkout(str(rig.old), str(rig.target), SimpleNamespace(
        workspace_path=str(rig.base)))

    assert rig.old.is_dir() and rig.old.resolve() in await rig.registered()
    if owner == 'dirty':
        assert (rig.old / 'stray.txt').read_text() == 'unsaved\n'


async def test_busy_resume_reservation_remains_queued_without_launch_or_hold(env, monkeypatch):
    from src.integration import delivery_observer
    from src.wait_runtime import resume
    from src.wait_runtime.resume import ResumeTransaction
    from src.waits.graph import lock_graph

    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
    await park(env)
    monkeypatch.setattr(delivery_observer, 'hierarchy_frontier_modes', AsyncMock())
    monkeypatch.setattr(resume, 'RESUME_GRAPH_LOCK_TIMEOUT', 0.05)
    orch = SimpleNamespace(db=env.db, _task_control_lock=lambda _: asyncio.Lock())
    driver = ResumeTransaction(SimpleNamespace(orch=orch))
    driver.failed = AsyncMock()
    driver.fresh_workspace = AsyncMock()
    before = await intent(env)
    assert before['resume_state'] == 'queued'

    async with env.db._engine.begin() as holder:
        await lock_graph(holder)
        with pytest.raises(WaitError, match='within 0.05s') as refused:
            await asyncio.wait_for(driver.dispatch(
                {'continuation_id': 'c', 'generation': 1},
                principal=None, scope=None, handler=None,
            ), timeout=5)
        assert refused.value.code == 'wait.graph_busy'
    assert await intent(env) == before
    driver.failed.assert_not_awaited()
    driver.fresh_workspace.assert_not_awaited()


async def test_resume_reserves_common_capacity_only_after_idle_process_stops(env):
    from src.database.tables import admission_reservations
    from src.models import AgentProfile, RepoConfig, RepoSourceType, Workspace
    from src.wait_runtime.resume import ResumeTransaction
    from src.wait_runtime.scheduler import SchedulerWaitService

    await env.db.create_profile(AgentProfile(id='worker', name='Worker', lifecycle='pool',
                                            max_active=1))
    await env.db.create_repo(RepoConfig(id='repo', project_id='p', url='https://example.test/repo',
        source_type=RepoSourceType.CLONE, checkout_base_path='/tmp/resume-admission'))
    await env.db.update_project('p', integration_repository_id='repo')
    await env.db.create_workspace(Workspace(id='available', project_id='p',
        workspace_path='/tmp/resume-available', source_type=RepoSourceType.CLONE,
        kind_id='project-repo'))
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            profile_id='worker', route_source='override'))
    await park(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(facts={'workspace_released_at': NOW + 20}))
    await env.db.create_session(SessionRecord(id='idle', project_id='p', profile_id='worker',
        harness='codex', provider='fake', name='idle', lifecycle='pool', work_dir='/tmp/idle',
        epoch='boot', instance_token='idle-token', state='running', desired_state='running',
        started_at=NOW, last_activity=NOW))
    config = AppConfig()
    config.wait_policy.v2_enabled = config.wait_policy.hibernation_enabled = True
    config.swarm.global_max_active = 1
    orch = SimpleNamespace(db=env.db, config=config, _worktrees_enabled=lambda: False,
                           _delivery_admission=lambda ids: observe_admission(env.db, ids, None),
                           _pool_quarantine_state=lambda *a: (None, None))
    service = SimpleNamespace(orch=orch, scheduler=SchedulerWaitService(orch, clock=lambda: NOW))
    now = [NOW + 30]
    driver = ResumeTransaction(service, clock=lambda: now[0])

    async def requests():
        async with env.db._engine.connect() as conn:
            return list((await conn.execute(select(admission_reservations))).mappings())

    assert await driver.reserve('c', 1) is None
    assert (await env.db.get_session('idle')).desired_state == 'stopped'
    # A blocked reserve durably records the first request, never a grant.
    [request] = await requests()
    assert (request['state'], request['created_at'], request['granted_at']) == (
        'pending', NOW + 30, None)
    # ...and when to look again, so the resume loop stops retaking the graph lock.
    assert (await intent(env))['facts']['reserve_retry'] == {
        'refusals': 1, 'due_at': NOW + 35, 'blocked': ['slot']}
    now[0] = NOW + 60
    assert await driver.reserve('c', 1) is None  # Requested stop is not proof of free capacity.
    assert await requests() == [request]  # An idempotent retry keeps the first boundary.
    assert (await intent(env))['facts']['reserve_retry'] == {
        'refusals': 2, 'due_at': NOW + 70, 'blocked': ['slot']}
    await env.db.update_session('idle', state='stopped')
    now[0] = NOW + 90
    action = await driver.reserve('c', 1)
    assert action is not None and action.continuation_id == 'c'
    assert (await intent(env))['facts']['reserve_retry'] is None
    now[0] = NOW + 120
    assert (await driver.reserve('c', 1)).agent_id == action.agent_id
    reservations = await requests()
    assert len(reservations) == 1 and reservations[0]['state'] == 'granted'
    assert (reservations[0]['id'], reservations[0]['fifo_seq']) == (request['id'], request['fifo_seq'])
    assert (reservations[0]['created_at'], reservations[0]['granted_at']) == (NOW + 30, NOW + 90)
    assert reservations[0]['scope']['profile_id'] == 'worker'
    assert (await env.db.get_task('owner')).status.value == 'RESUME_PENDING'


async def test_resume_hold_requires_new_current_provider_generation(env):
    from src.database.tables import provider_availability
    from src.models import AgentProfile

    driver, _, _ = await bound_resume(env)
    await env.db.create_profile(AgentProfile(id='worker-codex', name='Worker'))
    driver.orch.provider_availability = SimpleNamespace(provider_for_profile=lambda *a, **k: 'fake')
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            profile_id='worker-codex', route_source='override'))
        await conn.execute(update(continuations).values(resume_state='held',
            hold_reason='resume_launch_failed', launch_attempts=2,
            facts={'resume_attempt_started_at': NOW + 30}))
        await conn.execute(insert(provider_availability).values(provider='fake', state='available',
            since=NOW + 60, updated_at=NOW + 60, generation=2))
    event = {'source_key': 'resource_invalidation:provider:fake', 'source_revision': '1',
             'occurred_at': NOW + 50, 'payload': {'identity': 'fake', 'facts': {'generation': 1}}}
    async with env.db._engine.begin() as conn:
        await driver.recovery_event(conn, event)
    assert (await intent(env))['resume_state'] == 'held'
    event.update(source_revision='2', occurred_at=NOW + 60,
                 payload={'identity': 'fake', 'facts': {'generation': 2}})
    async with env.db._engine.begin() as conn:
        await driver.recovery_event(conn, event)
    row = await intent(env)
    assert row['resume_state'] == 'queued' and row['launch_attempts'] == 0
    assert row['hold_reason'] is None


@pytest.mark.parametrize('inherited_pass', [False, True])
@pytest.mark.parametrize('changed_task', ['owner', 'unrelated'])
async def test_resume_refreshes_own_admission_despite_backlog_and_other_graph_changes(
    env, monkeypatch, inherited_pass, changed_task,
):
    from contextlib import nullcontext
    from src.integration import admission
    from src.models import AgentProfile, RepoConfig, RepoSourceType, Workspace
    from src.wait_runtime.resume import ResumeTransaction
    from src.wait_runtime.scheduler import SchedulerWaitService

    await env.db.create_profile(AgentProfile(id='worker', name='Worker', lifecycle='pool'))
    await env.db.create_repo(RepoConfig(id='repo', project_id='p', url='https://example.test/repo',
        source_type=RepoSourceType.CLONE, checkout_base_path='/tmp/resume-admission'))
    await env.db.update_project('p', integration_repository_id='repo')
    await env.db.create_workspace(Workspace(id='available', project_id='p',
        workspace_path='/tmp/resume-available', source_type=RepoSourceType.CLONE,
        kind_id='project-repo'))
    await env.db.create_task(Task(id='unrelated', project_id='p', title='Other', description=''))
    await env.db.update_task('unrelated', status='READY')
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            profile_id='worker', route_source='override'))
    await park(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(facts={'workspace_released_at': NOW + 20}))
    for n in range(20):
        await env.db.wait_source_commit(source_key='message:backlog', generation=0,
            revision=str(n), outcome_ref=f'message:{n}', payload={})
    config = AppConfig()
    config.wait_policy.v2_enabled = config.wait_policy.hibernation_enabled = True
    orch = SimpleNamespace(db=env.db, config=config, _worktrees_enabled=lambda: False,
                           _pool_quarantine_state=lambda *a: (None, None))
    scheduler = SchedulerWaitService(orch, clock=lambda: NOW + 30)
    driver = ResumeTransaction(SimpleNamespace(orch=orch, scheduler=scheduler),
                               clock=lambda: NOW + 30)
    observe = admission.observe_admission
    observations = []

    async def moving_graph(db, ids, *args, **kwargs):
        view = await observe(db, ids, *args, **kwargs)
        observations.append((set(ids), kwargs['cached_only']))
        # Continuous work elsewhere must not spoil this task's snapshot.
        await db.add_dependency(changed_task, 'source')
        return view

    monkeypatch.setattr(admission, 'observe_admission', moving_graph)
    async with scheduler.reader.prepared() if inherited_pass else nullcontext():
        action = await driver.reserve('c', 1)
    assert observations[-1] == ({'owner'}, False)
    if changed_task == 'owner':
        assert action is None
        assert (await intent(env))['resume_state'] == 'queued'
    else:
        assert action is not None and action.continuation_id == 'c'
        assert (await intent(env))['resume_state'] == 'reserved'
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(wait_source_events.c.seq).where(
            wait_source_events.c.source_key == 'message:backlog',
            wait_source_events.c.dispatched_at.is_(None),
        ).limit(1)) is not None  # Admission did not need to drain even one event.


def test_turn_measurement_counts_all_calls_and_streaming_maxima():
    from src.sessions.transcripts import TranscriptEntry
    from src.wait_runtime.measurements import first_turn_tokens

    def entry(key, tokens, *, done=False, ts=NOW):
        return TranscriptEntry(uuid=key, parent_uuid=None, type='assistant', text='',
            model=None, usage={'input_tokens': tokens, 'output_tokens': 1}, ts=ts,
            usage_call_id=key, turn_complete=done)

    entries = [entry('prior', 999, done=True, ts=NOW - 2), entry('a', 10, ts=NOW - 1),
               entry('a', 12), entry('b', 20, done=True), entry('later', 500, done=True)]
    assert first_turn_tokens(entries, since=NOW) == 34
    assert first_turn_tokens(entries[:3], since=NOW) is None


async def test_turn_cost_cache_reuses_missing_usage_and_invalidates_changed_files(
    tmp_path, monkeypatch,
):
    import json
    from collections import OrderedDict

    from src.sessions.transcripts.claude import ClaudeTranscriptReader, slug_work_dir
    from src.wait_runtime import measurements

    session = SimpleNamespace(harness="claude", session_key="key", work_dir="/worker")
    db = SimpleNamespace(get_session=AsyncMock(return_value=session))
    reader = ClaudeTranscriptReader(base_dir=tmp_path)
    path = tmp_path / ".claude" / "projects" / slug_work_dir(session.work_dir) / "key.jsonl"
    path.parent.mkdir(parents=True)
    pending = {"type": "assistant", "uuid": "a", "timestamp": "2026-10-25T00:00:00Z",
               "message": {"id": "a", "content": [{"type": "text", "text": "pending"}],
                           "usage": {"input_tokens": 3}}}
    path.write_text(json.dumps(pending) + "\n")
    read = AsyncMock(wraps=reader.read_new)
    monkeypatch.setattr(reader, "read_new", read)
    monkeypatch.setattr(measurements, "resolve_reader", lambda _harness: reader)
    cache = OrderedDict()
    for _ in range(3):
        assert await measurements.session_turn_tokens(db, "s", since=0, cache=cache) is None
    assert read.await_count == 1

    completed = {**pending, "uuid": "b", "message": {
        **pending["message"], "id": "b", "usage": {"output_tokens": 5}, "stop_reason": "end_turn",
    }}
    with path.open("a") as stream:
        stream.write(json.dumps(completed) + "\n")
    assert await measurements.session_turn_tokens(db, "s", since=0, cache=cache) == 8
    assert read.await_count == 2
    assert await measurements.session_turn_tokens(db, "s", since=0, cache=cache) == 8
    assert read.await_count == 2

    # Replacement/truncation, missing files, and distinct start times cannot
    # reuse a previous completed turn or its cached missing-usage observation.
    path.unlink()
    assert await measurements.session_turn_tokens(db, "s", since=0, cache=cache) is None
    path.write_text(json.dumps(completed) + "\n")
    assert await measurements.session_turn_tokens(db, "s", since=0, cache=cache) == 5
    assert await measurements.session_turn_tokens(db, "s", since=2e9, cache=cache) is None
    assert read.await_count == 4
    monkeypatch.setattr(measurements, "TURN_TOKEN_CACHE_SIZE", 2)
    for since in (1, 2, 3):
        await measurements.session_turn_tokens(db, "s", since=since, cache=cache)
    assert len(cache) == 2


async def test_turn_cost_cache_tolerates_eviction_during_stat(tmp_path, monkeypatch):
    import threading
    from collections import OrderedDict

    from src.wait_runtime import measurements

    path = tmp_path / "transcript"
    path.write_text("pending")
    session = SimpleNamespace(harness="claude", session_key="key", work_dir="/worker")
    db = SimpleNamespace(get_session=AsyncMock(return_value=session))
    reader = SimpleNamespace(resolve_session=lambda _session: path,
                             read_new=AsyncMock(return_value=([], 7)))
    monkeypatch.setattr(measurements, "resolve_reader", lambda _harness: reader)
    cache = OrderedDict()
    assert await measurements.session_turn_tokens(db, "s", since=0, cache=cache) is None
    entered, release = threading.Event(), threading.Event()
    signature = measurements._signature

    def slow_stat(path):
        entered.set()
        assert release.wait(5)
        return signature(path)

    monkeypatch.setattr(measurements, "_signature", slow_stat)
    pending = asyncio.create_task(measurements.session_turn_tokens(db, "s", since=0, cache=cache))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        cache.clear()  # Another wait can evict the entry while this stat runs.
    finally:
        release.set()
    assert await pending is None
    reader.read_new.assert_awaited_once()


async def test_turn_cost_reduction_runs_off_the_event_loop(tmp_path, monkeypatch):
    import threading

    from collections import OrderedDict
    from src.wait_runtime import measurements

    path = tmp_path / "transcript"
    path.write_text("pending")
    session = SimpleNamespace(harness="claude", session_key="key", work_dir="/worker")
    db = SimpleNamespace(get_session=AsyncMock(return_value=session))
    reader = SimpleNamespace(resolve_session=lambda _session: path,
                             read_new=AsyncMock(return_value=([], 7)))
    monkeypatch.setattr(measurements, "resolve_reader", lambda _harness: reader)
    entered, release = threading.Event(), threading.Event()

    def slow_reduce(entries, *, since):
        entered.set()
        assert release.wait(5)
        return 23

    monkeypatch.setattr(measurements, "first_turn_tokens", slow_reduce)
    pending = asyncio.create_task(measurements.session_turn_tokens(
        db, "s", since=0, cache=OrderedDict(),
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert not pending.done()  # The loop can run while reduction is blocked.
    finally:
        release.set()
    assert await pending == 23


async def test_completed_turn_costs_arriving_after_ack_are_recorded(env, monkeypatch):
    from src.database.tables import wait_measurements
    from src.wait_runtime import measurements
    from src.waits.resume_ack import acknowledge, mark_delivered

    _, _, scope = await bound_resume(env)
    current = await intent(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(facts={
            **current['facts'], 'costs': {'setup_seconds': 3},
        }))
        await conn.execute(insert(wait_measurements).values(id='sample', project_id='p',
            condition_kind='task', source_class='task', wait_id=env.wait_id,
            decision='hibernate', waited_seconds=10, hibernated=True, evicted=False,
            recorded_at=NOW + 10))
        await mark_delivered(conn, continuation_id='c', session_id='fresh',
            instance_token='fresh-token', claim_epoch=2, now=NOW + 40)
        await acknowledge(conn, dict(wait_id=env.wait_id, continuation_id='c', generation=1),
                          scope=scope, now=NOW + 41)
    observed = AsyncMock(side_effect=[11, 34])
    monkeypatch.setattr(measurements, 'session_turn_tokens', observed)
    lifecycle = SimpleNamespace(db=env.db, clock=lambda: NOW + 70)
    await measurements.capture_costs(lifecycle, env.wait_id)
    async with env.db._engine.connect() as conn:
        sample = (await conn.execute(select(wait_measurements))).mappings().one()
    assert sample['summary_tokens'] == 11 and sample['resume_tokens'] == 34
    assert sample['setup_seconds'] == 3 and sample['rebuild_seconds'] == 10
    await measurements.capture_costs(lifecycle, env.wait_id)
    assert observed.await_count == 2
    # A later sweep must leave completed cost accounting untouched, even after
    # the normal sampling cooldown expires.
    lifecycle.clock = lambda: NOW + 110
    sample_update = AsyncMock(side_effect=AssertionError("completed costs were written again"))
    monkeypatch.setattr(measurements, 'update_sample', sample_update)
    await measurements.capture_costs(lifecycle, env.wait_id)
    assert observed.await_count == 2
    sample_update.assert_not_awaited()
    assert (await intent(env))['facts']['usage_checked_at'] == NOW + 70


async def test_slow_scheduler_observation_does_not_exhaust_resume_graph_retry(env, monkeypatch):
    from src.commands.principal import ExecutionPrincipal
    from src.integration import admission
    from src.models import AgentProfile, RepoConfig, RepoSourceType, Workspace
    from src.wait_runtime.resume import ResumeTransaction
    from src.wait_runtime.scheduler import SchedulerWaitService
    from src.waits import graph

    await env.db.create_profile(AgentProfile(id='worker', name='Worker', lifecycle='pool'))
    await env.db.create_repo(RepoConfig(id='repo', project_id='p', url='https://example.test/repo',
        source_type=RepoSourceType.CLONE, checkout_base_path='/tmp/resume-admission'))
    await env.db.update_project('p', integration_repository_id='repo')
    await env.db.create_workspace(Workspace(id='available', project_id='p',
        workspace_path='/tmp/resume-available', source_type=RepoSourceType.CLONE,
        kind_id='project-repo'))
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            profile_id='worker', route_source='override'))
    await park(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(facts={'workspace_released_at': NOW + 20}))
    config = AppConfig()
    config.wait_policy.v2_enabled = config.wait_policy.hibernation_enabled = True
    config.swarm.global_max_active = 1
    orch = SimpleNamespace(db=env.db, config=config, _worktrees_enabled=lambda: False,
                           _pool_quarantine_state=lambda *a: (None, None))
    scheduler = SchedulerWaitService(orch, clock=lambda: NOW + 30)
    driver = ResumeTransaction(SimpleNamespace(orch=orch, scheduler=scheduler),
                               clock=lambda: NOW + 30)
    entered, release = asyncio.Event(), asyncio.Event()
    observe = admission.observe_admission

    async def slow(*args, **kwargs):
        if asyncio.current_task().get_name() == 'slow-scheduler':
            entered.set()
            await release.wait()
        return await observe(*args, **kwargs)

    monkeypatch.setattr(admission, 'observe_admission', slow)
    monkeypatch.setattr(graph, 'GRAPH_LOCK_TIMEOUT', 0.05)
    scan = asyncio.create_task(scheduler.sync({'project_id': 'p', 'cycle_id': 'slow'},
        principal=ExecutionPrincipal.service('test')), name='slow-scheduler')
    try:
        await asyncio.wait_for(entered.wait(), 5)
        action = await asyncio.wait_for(driver.reserve('c', 1), 5)
        assert action is not None and action.continuation_id == 'c'
        assert not scan.done()  # Dispatch completed while the observation was still stalled.
        assert (await intent(env))['resume_state'] == 'reserved'
    finally:
        release.set()
        await asyncio.wait_for(scan, 5)


async def test_cold_restart_external_proofs_leave_transactions_closed(
    cold_restart_checkout, monkeypatch,
):
    from src.commands.task_commands import TaskCommandsMixin
    from src.wait_runtime import restart
    from src.waits.graph import lock_graph

    case = cold_restart_checkout

    async def stopped(handle):
        # Neither graph nor task rows can be held while awaiting the provider.
        assert case.env.db._engine.pool.checkedout() == 0
        async with case.env.db._engine.begin() as conn:
            await lock_graph(conn, timeout=0.05)
            for table in (tasks, wait_records, continuations, sessions):
                await conn.execute(select(table).with_for_update(nowait=True))
        return True

    original_ancestor = case.git.ais_ancestor
    original_detach = restart.detach_task_slot

    async def ancestor(*args, **kwargs):
        assert case.env.db._engine.pool.checkedout() == 0
        return await original_ancestor(*args, **kwargs)

    async def detach(*args, **kwargs):
        assert case.env.db._engine.pool.checkedout() == 0
        return await original_detach(*args, **kwargs)

    monkeypatch.setattr(case.git, 'ais_ancestor', ancestor)
    monkeypatch.setattr(restart, 'detach_task_slot', detach)
    case.provider.confirm_stopped.side_effect = stopped
    result = await TaskCommandsMixin._cmd_restart_task(case.command, {'task_id': 'owner'})
    assert result['success'], result
    case.provider.confirm_stopped.assert_awaited_once()


async def workspace_resume(env):
    """A real parked owner and granted resume seat, before spawning a process."""
    from src.database.tables import admission_reservations
    from tests.test_wait_dispatch import runtime

    _, action, _ = await bound_resume(env)
    await env.db.update_session('fresh', state='stopped', desired_state='stopped')
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(
            resume_state='reserved', new_session_id=None, envelope_id=None,
            facts={'workspace_released_at': NOW + 20, 'resume_attempt_started_at': NOW + 30},
        ))
        await conn.execute(update(admission_reservations).values(state='granted'))
    now = [NOW + 30]
    service = runtime(env, clock=lambda: now[0], registry=build_registry())
    service.orch._task_control_lock = lambda _: asyncio.Lock()
    service.orch._launch_session_for_task_locked = AsyncMock()
    service.lifecycle.resume.fresh_workspace = AsyncMock(return_value='/tmp/preserved')
    return service, action, now


@pytest.mark.parametrize('mutation', ['path', 'agent', 'task', 'disabled'])
async def test_resume_launch_receipt_refuses_changed_workspace_binding(env, mutation):
    from dataclasses import replace
    from src.database.tables import admission_reservations, workspaces

    service, action, _ = await workspace_resume(env)
    fields = {
        'path': {'workspace_path': '/tmp/another-checkout'},
        'agent': {'locked_by_agent_id': None},
        'task': {'locked_by_task_id': None},
        'disabled': {'enabled': False},
    }[mutation]
    async with env.db._engine.begin() as conn:
        await conn.execute(update(workspaces).where(workspaces.c.id == 'resume-slot').values(**fields))
    before = await env.db.get_task_meta('owner', 'claim_workspace')
    record = replace(await env.db.get_session('fresh'), id='replacement')
    with pytest.raises(WaitError, match='launch workspace binding changed'):
        await service.lifecycle.resume.bind_launch(action, record)
    assert await env.db.get_task_meta('owner', 'claim_workspace') == before
    assert (await intent(env))['resume_state'] == 'reserved'
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(admission_reservations.c.state)) == 'granted'
        assert await conn.scalar(select(messages.c.id).where(messages.c.to_id == 'replacement')) is None


@pytest.mark.parametrize('error', [WaitError('wait.graph_busy', 'lock timeout'), TimeoutError()])
async def test_workspace_transient_retry_preserves_reservation_and_then_resumes(env, error):
    from src.waits.occupancy import continuation_occupancy
    from src.database.tables import admission_reservations

    service, action, now = await workspace_resume(env)
    driver = service.lifecycle.resume
    driver.fresh_workspace.side_effect = [error, '/tmp/preserved']
    # Occupancy uses the real admission scope, including the profile identity.
    async with env.db._engine.begin() as conn:
        await conn.execute(update(admission_reservations).values(scope={'profile_id': 'worker-codex'}))
    before = await intent(env)
    args = {'continuation_id': 'c', 'generation': 1}
    result = await driver.dispatch(args, principal=None, scope=None,
                                   handler=service.orch._command_handler)
    assert result['retry_queued'] and result['retry_at'] == now[0] + 5
    row = await intent(env)
    assert row['resume_state'] == 'reserved' and row['launch_attempts'] == 1
    assert (row['saved_head'], row['branch'], row['wait_id']) == (
        before['saved_head'], before['branch'], before['wait_id'])
    assert (await env.db.get_task('owner')).assigned_agent_id == action.agent_id
    _, occupancy = await continuation_occupancy(env.db, service.orch.config)
    assert occupancy == {('p', 'worker-codex'): 1}
    assert await driver.reserve('c', 1) == action
    service.command = AsyncMock()
    await service.lifecycle.resume_pending()
    service.command.assert_not_awaited()
    now[0] += 5
    result = await driver.dispatch(args, principal=None, scope=None,
                                   handler=service.orch._command_handler)
    assert result['dispatched'] and driver.fresh_workspace.await_count == 2
    service.orch._launch_session_for_task_locked.assert_awaited_once()
    assert (await intent(env))['launch_attempts'] == 1


@pytest.mark.parametrize('retryable', [True, False])
async def test_workspace_failure_holds_after_budget_or_immediately(env, retryable):
    from src.database.tables import escalations

    service, _, now = await workspace_resume(env)
    driver = service.lifecycle.resume
    driver.fresh_workspace.side_effect = (WaitError('wait.graph_busy', 'lock busy') if retryable
                                         else WaitError('continuation.head_drift', 'dirty checkout'))
    for attempt in range(3 if retryable else 1):
        result = await driver.dispatch({'continuation_id': 'c', 'generation': 1},
            principal=None, scope=None, handler=service.orch._command_handler)
        if attempt < 2 and retryable:
            assert result['retry_queued']
            now[0] = result['retry_at']
    assert result['held'] and result['hold_reason'] == 'resume_workspace_failed'
    row = await intent(env)
    assert row['resume_state'] == 'held' and row['launch_attempts'] == 1
    assert row['facts']['workspace_retry']['failures'] == (3 if retryable else 1)
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(escalations.c.id)) == row['facts']['hold_escalation_id']
    service.orch._launch_session_for_task_locked.assert_not_awaited()


@pytest.mark.parametrize('reason', ['slots_full', 'slot_warming', 'slot_lost_race'])
async def test_workspace_capacity_requeues_without_spending_failure_budget(env, reason):
    from src.database.tables import admission_reservations, escalations
    from src.models import AgentProfile, RepoConfig, RepoSourceType, Workspace
    from src.wait_runtime.resume import ResumeTransaction
    from src.waits.occupancy import continuation_occupancy

    service, action, now = await workspace_resume(env)
    await env.db.delete_workspace('resume-slot')
    await env.db.create_profile(AgentProfile(id='worker-codex', name='Worker'))
    await env.db.create_repo(RepoConfig(id='repo', project_id='p', url='https://example.test/repo',
        source_type=RepoSourceType.CLONE, checkout_base_path='/tmp/resume-admission'))
    await env.db.update_project('p', integration_repository_id='repo')
    await env.db.create_workspace(Workspace(id='available', project_id='p',
        workspace_path='/tmp/resume-available', source_type=RepoSourceType.CLONE,
        kind_id='project-repo'))
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            profile_id='worker-codex', route_source='override'))
        await conn.execute(update(admission_reservations).values(scope={'profile_id': 'worker-codex'}))
    service.orch._worktrees_enabled = lambda: False
    service.orch._pool_quarantine_state = lambda *a: (None, None)
    service.orch._workspace_wait_reasons = {'owner': reason}
    service.orch._prepare_workspace_locked = AsyncMock(return_value=None)

    async def reservation():
        async with env.db._engine.connect() as conn:
            return dict((await conn.execute(select(admission_reservations))).mappings().one())

    before, request = await intent(env), await reservation()
    started = now[0]
    for delay in [5, 10, 20, 40, 60, 60, 60, 60]:
        # Reconstruct the transaction driver to prove backoff is durable.
        driver = ResumeTransaction(service, clock=lambda: now[0])
        service.lifecycle.resume = driver
        result = await driver.dispatch({'continuation_id': 'c', 'generation': 1},
            principal=None, scope=None, handler=service.orch._command_handler)
        assert result['capacity_blocked'] and result['retry_at'] == now[0] + delay
        row = await intent(env)
        assert (row['resume_state'], row['launch_attempts'], row['hold_reason']) == ('queued', 0, None)
        assert 'workspace_retry' not in row['facts']
        assert row['facts']['workspace_capacity']['first_blocked_at'] == started
        assert (row['saved_head'], row['branch'], row['wait_id'], row['suspension_generation']) == (
            before['saved_head'], before['branch'], before['wait_id'], before['suspension_generation'])
        pending = await reservation()
        assert pending['state'] == 'pending' and pending['granted_at'] is None
        assert (pending['id'], pending['created_at'], pending['fifo_seq']) == (
            request['id'], request['created_at'], request['fifo_seq'])
        assert (await env.db.get_task('owner')).assigned_agent_id is None
        assert (await env.db.get_agent(action.agent_id)).current_task_id is None
        assert (await env.db.get_agent(action.agent_id)).state.value == 'RETIRED'
        assert (await continuation_occupancy(env.db, service.orch.config))[1] == {}
        # Both maintenance and direct dispatch respect the persisted delay.
        service.command = AsyncMock()
        await service.lifecycle.resume_pending()
        service.command.assert_not_awaited()
        assert await driver.reserve('c', 1) is None
        assert await intent(env) == row
        now[0] = result['retry_at']

    await service.orch._command_handler.wait_service_resume_stalls(service.lifecycle)
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(escalations.c.id)) is None
        [notice] = (await conn.execute(select(messages).where(
            messages.c.subject == 'Resume stalled: owner'))).mappings().all()
        assert reason in notice['body']
        wait = (await conn.execute(select(wait_records))).mappings().one()
        assert wait['outcome'] == 'satisfied' and wait['cleared_at'] is None
    # A later retry reobserves actual workspace capacity before taking a seat.
    driver.fresh_workspace = AsyncMock(return_value='/tmp/preserved')
    await env.db.update_workspace('available', locked_by_task_id='source', locked_by_agent_id='a')
    result = await driver.dispatch({'continuation_id': 'c', 'generation': 1},
        principal=None, scope=None, handler=service.orch._command_handler)
    assert not result['dispatched']
    driver.fresh_workspace.assert_not_awaited()
    assert (await reservation())['state'] == 'pending'
    assert (await intent(env))['launch_attempts'] == 0
    # Capacity becomes available: fresh admission succeeds, then launch once.
    await env.db.release_workspace('available')
    result = await driver.dispatch({'continuation_id': 'c', 'generation': 1},
        principal=None, scope=None, handler=service.orch._command_handler)
    assert result['dispatched']
    assert (await intent(env))['launch_attempts'] == 1
    assert (await reservation())['granted_at'] == now[0]
    service.orch._launch_session_for_task_locked.assert_awaited_once()


@pytest.mark.parametrize('reason', ['slot_stalled', None])
async def test_missing_workspace_without_capacity_evidence_holds(env, reason):
    service, _, _ = await workspace_resume(env)
    await env.db.delete_workspace('resume-slot')
    service.orch._workspace_wait_reasons = {'owner': reason}
    service.orch._prepare_workspace_locked = AsyncMock(return_value=None)
    driver = type(service.lifecycle.resume)(service)
    result = await driver.dispatch({'continuation_id': 'c', 'generation': 1},
        principal=None, scope=None, handler=service.orch._command_handler)
    assert result['held'] and result['hold_reason'] == 'resume_workspace_failed'


@pytest.mark.parametrize('authority', ['local', 'project', 'global'])
async def test_task_recover_accepts_resume_escalation_id_and_preserves_outcome(env, authority):
    from dataclasses import replace
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import escalations

    service, action, _ = await workspace_resume(env)
    service.orch.wait_service = service
    driver = service.lifecycle.resume
    driver.fresh_workspace.side_effect = RuntimeError('setup needs repair')
    handler = service.orch._command_handler
    assert (await driver.dispatch({'continuation_id': 'c', 'generation': 1},
        principal=None, scope=None, handler=handler))['held']
    held = await intent(env)
    incident_id = held['facts']['hold_escalation_id']
    principal = TRUSTED_LOCAL
    if authority != 'local':
        record = replace(await env.db.get_session('fresh'), id='supervisor', task_id=None,
            agent_id=None, name='supervisor', lifecycle='named', profile_id='supervisor',
            state='running', desired_state='running', project_id='p' if authority == 'project' else None)
        await env.db.create_session(record)
        principal = ExecutionPrincipal(kind=PrincipalKind.SESSION,
            policy=CapabilityPolicy.from_namespaces(aq_commands=['task_recover']),
            session_id=record.id, session_instance_token=record.instance_token,
            project_id=record.project_id, profile_id='supervisor', elevated=True)
    with principal_context(principal):
        result = await handler.execute('task_recover', dict(task_id='owner',
            incident_id=incident_id, decision='retry', reason='Setup repaired'))
        assert result['success'], result
        assert result['incident_id'] == incident_id and result['decision'] == 'retry'
        replay = await handler.execute('task_recover', dict(task_id='owner',
            incident_id=incident_id, decision='retry', reason='Duplicate retry'))
        assert not replay['success']
    row = await intent(env)
    assert row['resume_state'] == 'reserved' and row['hold_reason'] is None
    assert row['facts']['resume_requeue']['incident_id'] == incident_id
    assert row['facts']['resume_requeue']['note'] == 'Setup repaired'
    assert (row['saved_head'], row['branch'], row['wait_id']) == (
        held['saved_head'], held['branch'], held['wait_id'])
    assert (await env.db.get_task('owner')).assigned_agent_id == action.agent_id
    async with env.db._engine.connect() as conn:
        assert await conn.scalar(select(escalations.c.state)) == 'needs_human'
        wait = (await conn.execute(select(wait_records))).mappings().one()
        assert wait['outcome'] == 'satisfied' and wait['cleared_at'] is None


@pytest.mark.parametrize('change', [
    'generation', 'task', 'closed', 'incident', 'hold', 'live', 'scope', 'branch',
])
async def test_task_recover_resume_incident_refuses_changed_binding(env, change):
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import escalations

    service, _, _ = await workspace_resume(env)
    service.orch.wait_service = service
    driver = service.lifecycle.resume
    handler = service.orch._command_handler
    driver.fresh_workspace.side_effect = RuntimeError('setup needs repair')
    assert (await driver.dispatch({'continuation_id': 'c', 'generation': 1},
        principal=None, scope=None, handler=handler))['held']
    held = await intent(env)
    incident_id = held['facts']['hold_escalation_id']
    async with env.db._engine.begin() as conn:
        if change == 'generation':
            await conn.execute(update(continuations).values(suspension_generation=2))
        elif change == 'task':
            await conn.execute(update(escalations).values(task_id='source'))
        elif change == 'closed':
            await conn.execute(update(escalations).values(state='cancelled',
                terminal_outcome='source_satisfied', terminal_evidence={'rule': 'test'},
                terminal_at=NOW + 40))
        elif change == 'incident':
            await conn.execute(update(continuations).values(facts={**held['facts'],
                'hold_escalation_id': 'another-incident'}))
        elif change == 'hold':
            await conn.execute(update(continuations).values(hold_reason='push_failed'))
        elif change == 'live':
            await conn.execute(update(sessions).where(sessions.c.id == 'fresh').values(state='running'))
        elif change == 'branch':
            await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(branch_name='aq/other'))
    principal = TRUSTED_LOCAL
    if change == 'scope':
        principal = ExecutionPrincipal(kind=PrincipalKind.SESSION,
            session_id='foreign-supervisor', project_id='foreign', elevated=True,
            policy=CapabilityPolicy.from_namespaces(aq_commands=['task_recover']))
    before = await intent(env)
    with principal_context(principal):
        result = await handler.execute('task_recover', dict(task_id='owner', incident_id=incident_id,
            decision='retry', reason='Setup repaired'))
    assert not result['success'], result
    assert await intent(env) == before


async def test_supervisor_requeue_generation_fence_audit_and_resumed_incident(env):
    import json
    from dataclasses import replace
    from src.commands.principal import TRUSTED_LOCAL
    from src.database.tables import escalations, events
    from src.waits.resume_ack import mark_delivered

    service, action, now = await workspace_resume(env)
    driver = service.lifecycle.resume
    handler = service.orch._command_handler
    driver.fresh_workspace.side_effect = RuntimeError('repairable setup configuration')
    assert (await driver.dispatch({'continuation_id': 'c', 'generation': 1},
        principal=None, scope=None, handler=handler))['held']
    held = await intent(env)
    args = dict(wait_id=env.wait_id, decision='requeue', continuation_id='c',
                expected_generation=2, note='Setup repaired')
    with principal_context(TRUSTED_LOCAL):
        assert (await handler.execute('wait_decide', args))['error_code'] == 'continuation.stale_generation'
        assert await intent(env) == held
        args['expected_generation'] = 1
        assert (await handler.execute('wait_decide', args))['success']
        assert not (await handler.execute('wait_decide', args))['success']
    row = await intent(env)
    # The workspace hold kept its agent and granted seat, so the retry reuses them.
    assert row['resume_state'] == 'reserved' and row['hold_reason'] is None
    assert row['launch_attempts'] == 1 and row['facts']['workspace_retry'] == {}
    assert row['saved_head'] == held['saved_head'] and row['wait_id'] == held['wait_id']
    async with env.db._engine.connect() as conn:
        incident = (await conn.execute(select(escalations))).mappings().one()
        assert incident['state'] == 'needs_human'
        audit = json.loads(await conn.scalar(select(events.c.payload).where(
            events.c.event_type == 'wait.resume_requeued')))
        assert audit['generation'] == 1 and audit['note'] == 'Setup repaired'
    assert await driver.reserve('c', 1) == action
    record = replace(await env.db.get_session('fresh'), id='repaired',
                     state='running', desired_state='running')
    await driver.bind_launch(action, record)
    await env.db.create_session(record)
    async with env.db._engine.begin() as conn:
        await mark_delivered(conn, continuation_id='c', session_id='repaired',
            instance_token='fresh-token', claim_epoch=2, now=now[0] + 10)
    async with env.db._engine.connect() as conn:
        incident = (await conn.execute(select(escalations))).mappings().one()
        assert incident['state'] == 'cancelled' and incident['terminal_outcome'] == 'source_satisfied'


async def test_requeue_of_a_launch_hold_returns_to_the_capacity_queue(env):
    from src.commands.principal import TRUSTED_LOCAL

    service, _, _ = await workspace_resume(env)
    # A launch hold released its agent and seat, so the repair must re-reserve.
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(
            resume_state='held', hold_reason='resume_launch_failed', launch_attempts=2))
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(assigned_agent_id=None))
    args = dict(wait_id=env.wait_id, decision='requeue', continuation_id='c',
                expected_generation=1, note='Provider repaired')
    with principal_context(TRUSTED_LOCAL):
        assert (await service.orch._command_handler.execute('wait_decide', args))['success']
    row = await intent(env)
    assert (row['resume_state'], row['launch_attempts'], row['hold_reason']) == ('queued', 0, None)


@pytest.mark.parametrize('failure', ['graph_busy', 'cancel_after_write'])
async def test_refused_or_cancelled_requeue_preserves_generation_and_hold(env, monkeypatch, failure):
    from src.commands.principal import TRUSTED_LOCAL
    from src.wait_runtime import resume
    from src.waits.graph import lock_graph

    service, _, _ = await workspace_resume(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(continuations).values(
            resume_state='held', hold_reason='resume_workspace_failed'))
    held = await intent(env)
    args = dict(wait_id=env.wait_id, decision='requeue', continuation_id='c',
                expected_generation=1, note='Setup repaired')
    handler = service.orch._command_handler
    with principal_context(TRUSTED_LOCAL):
        with monkeypatch.context() as patch:
            if failure == 'graph_busy':
                async def short_lock(conn, **kwargs):
                    assert kwargs == {'queued': True}
                    await lock_graph(conn, queued=True, timeout=0.05)
                patch.setattr(resume, 'lock_graph', short_lock)
                async with env.db._engine.begin() as blocker:
                    await lock_graph(blocker)
                    result = await handler.execute('wait_decide', args)
                    assert result['error_code'] == 'wait.graph_busy'
            else:
                # The audit insert follows the continuation UPDATE. Interrupt
                # here to prove both writes belong to one rollback boundary.
                def cancelled_insert(*args, **kwargs):
                    raise asyncio.CancelledError()
                patch.setattr(resume, 'insert', cancelled_insert)
                with pytest.raises(asyncio.CancelledError):
                    await handler.execute('wait_decide', args)
        assert await intent(env) == held
        # A retry uses the exact same expected generation, without a stale error.
        result = await handler.execute('wait_decide', args)
        assert result['success'] and result['generation'] == 1
    repaired = await intent(env)
    assert repaired['suspension_generation'] == held['suspension_generation']
    assert repaired['version'] == held['version'] + 1
    assert repaired['saved_head'] == held['saved_head']


async def test_workspace_retry_recovers_actual_staged_checkout(env, tmp_path):
    from dataclasses import replace
    from src.database.tables import admission_reservations, agents
    from tests.test_wait_dispatch import runtime

    rig = await fresh_rig(env, tmp_path)
    now = [NOW + 30]
    service = runtime(env, clock=lambda: now[0], registry=build_registry())
    driver = service.lifecycle.resume
    rig.orch.config = service.orch.config
    rig.orch._task_control_lock = lambda _: asyncio.Lock()
    rig.orch._launch_session_for_task_locked = AsyncMock()
    driver.orch = rig.orch
    driver.fresh_workspace = rig.driver.fresh_workspace
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == 'owner').values(
            status='RESUME_PENDING', assigned_agent_id='a'))
        await conn.execute(update(agents).where(agents.c.id == 'a').values(
            state='BUSY', current_task_id='owner'))
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 25))
        await conn.execute(update(continuations).values(facts={
            'workspace_released_at': NOW + 20, 'resume_attempt_started_at': now[0]}))
        await conn.execute(insert(admission_reservations).values(
            id='reservation', project_id='p', continuation_id='c', scope={},
            effective_priority=100, provenance='resume', deadline_at=NOW + 300,
            state='granted', created_at=now[0]))
    real_reprovision = rig.slots.reprovision_slot
    rig.slots.reprovision_slot = AsyncMock(side_effect=TimeoutError('setup transport timed out'))
    args = {'continuation_id': 'c', 'generation': 1}
    result = await driver.dispatch(args, principal=None, scope=None,
                                   handler=service.orch._command_handler)
    assert result['retry_queued'] and rig.target.is_dir()
    stage = (await intent(env))['facts']['fresh_workspace']
    env.db.get_workspace_for_task.return_value = replace(
        env.db.get_workspace_for_task.return_value, workspace_path=str(rig.target))
    rig.slots.reprovision_slot = real_reprovision
    now[0] = result['retry_at']
    assert (await driver.dispatch(args, principal=None, scope=None,
                                 handler=service.orch._command_handler))['dispatched']
    assert (await intent(env))['facts']['fresh_workspace'] == stage
    assert await rig.git._arun(['rev-parse', 'HEAD'], cwd=str(rig.target)) == rig.head
    rig.orch._prepare_workspace_locked.assert_awaited_once()
    rig.orch._launch_session_for_task_locked.assert_awaited_once()


async def test_requeue_checks_live_supervisor_project_and_process_fences(env):
    from dataclasses import replace
    from src.commands.principal import TRUSTED_LOCAL

    service, _, _ = await workspace_resume(env)
    driver, handler = service.lifecycle.resume, service.orch._command_handler
    driver.fresh_workspace.side_effect = RuntimeError('setup needs repair')
    await driver.dispatch({'continuation_id': 'c', 'generation': 1},
        principal=None, scope=None, handler=handler)
    record = replace(await env.db.get_session('fresh'), id='supervisor', task_id=None,
                     agent_id=None, name='supervisor', lifecycle='named', profile_id='supervisor',
                     state='running', desired_state='running')
    await env.db.create_session(record)
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=['wait_decide']),
        session_id=record.id, session_instance_token=record.instance_token,
        project_id='p', profile_id='supervisor', elevated=True)
    args = dict(wait_id=env.wait_id, decision='requeue', continuation_id='c',
                expected_generation=1, note='Repair verified')
    held = await intent(env)
    for rejected in (replace(principal, elevated=False),
                     replace(principal, project_id='foreign'),
                     replace(principal, session_instance_token='stale')):
        with principal_context(rejected):
            assert not (await handler.execute('wait_decide', args))['success']
        assert await intent(env) == held
    live = replace(await env.db.get_session('fresh'), id='unexpected', name='unexpected',
                   state='running')
    await env.db.create_session(live)
    with principal_context(TRUSTED_LOCAL):
        assert (await handler.execute('wait_decide', args))['error_code'] == 'continuation.stop_uncertain'
    assert await intent(env) == held
    await env.db.update_session('unexpected', state='stopped')
    with principal_context(principal):
        result = await handler.execute('wait_decide', args)
    assert result['success'], result
    assert (await intent(env))['facts']['resume_requeue']['session_id'] == 'supervisor'
