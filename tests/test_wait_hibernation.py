"""Checkpoint and fresh-prime boundary, without enabling stop/launch services."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.commands import CommandHandler
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import AppConfig
from src.database import Database
from src.database.queries.continuation_queries import (
    checkpoint_continuation,
    continuation_prime_context,
    degrade_checkpoint,
    wait_outcome_envelope,
)
from src.database.queries.wait_queries import register_wait
from src.database.tables import continuations, messages, sessions, tasks, wait_records
from src.models import Project, SessionRecord, Task
from src.prime import PrimeRenderer
from src.prime.budgets import CONTINUATION_COMMENTS_BYTES
from src.prime.hook_envelopes import wrap
from src.profiles.capabilities import CapabilityPolicy
from src.waits.conditions import SleepDecision, WaitError
from src.waits.registry import build_registry
from tests.db_fixtures import lease_dsn
from tests.test_wait_queries import NOW, Policy, owner


@pytest.fixture
async def env():
    db = Database(lease_dsn("wait-checkpoints"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    scope = await owner(db)
    await db.create_task(Task(id="source", project_id="p", title="Source", description=""))
    async with db._engine.begin() as conn:
        result = await register_wait(
            conn,
            scope=scope,
            condition={"kind": "task", "task_id": "source"},
            idempotency_key="hibernate",
            registry=build_registry(clock=lambda: NOW),
            policy=Policy(),
            decide_sleep=lambda *_: SleepDecision("hibernate", "duration", {}),
            now=NOW,
            source_watermark=0,
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
    yield SimpleNamespace(db=db, scope=scope, wait_id=result["wait_id"])
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


async def test_command_is_concrete_but_activation_remains_required(env):
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
        assert (await handler.execute("wait_checkpoint", args))["error_code"] == "wait.disabled"
        handler.config.wait_policy.v2_enabled = True
        result = await handler.execute("wait_checkpoint", args)
    assert result["success"], result
    facts = (await intent(env))["facts"]
    assert facts["task_id"] == "owner" and facts["last_comment"] is None
    assert facts["open_subtasks"] == []


async def resumed(env, *, degraded=False):
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
            update(wait_records).values(outcome="satisfied", result_ref="task:source:COMPLETED")
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
async def test_resume_prime_outcome_summary_ticket_all_comments_and_held_mail(
    env, tmp_path, degraded
):
    renderer = await resumed(env, degraded=degraded)
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
    for n in range(18):
        await env.db.add_task_comment(
            "owner", f"history-{n:02}: {'é' * 3000}", author_kind="user", author_id="human"
        )
    doc = await renderer.render_for_task("owner", session_id="new", work_dir=str(tmp_path))
    context = next(s.body for s in doc.sections if s.key == "task_context")
    comments = context[context.index("**Task comments") :]
    assert len(comments.encode()) <= CONTINUATION_COMMENTS_BYTES
    assert "history-17" in comments and "history-00" not in comments
    assert "Older comments truncated" in comments and "aq task comments owner" in comments
    assert "\ufffd" not in comments


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


@pytest.mark.parametrize('failure', [None, 'push', 'stop', 'detach', 'push_retry'])
async def test_suspend_retains_checkout_until_stop_push_and_detach_proven(env, monkeypatch, failure):
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
    provider = SimpleNamespace(stop=AsyncMock(), confirm_instance_stopped=AsyncMock(
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


async def bound_resume(env):
    """Park a real claim, reserve a seat, bind an entirely new process."""
    from dataclasses import replace
    from src.database.tables import admission_reservations, agents
    from src.scheduler import AssignAction
    from src.wait_runtime.resume import ResumeTransaction

    await capture(env)
    await mail(env, 'held-task', to_kind='task', to_id='owner')
    await mail(env, 'held-old', to_id='s')
    await mail(env, 'foreign', to_id='someone-else')
    await mail(env, 'control', to_kind='task', to_id='owner', body_kind='wait_suspend')
    async with env.db._engine.begin() as conn:
        await conn.execute(update(wait_records).values(outcome='satisfied', resolved_at=NOW + 10))
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
    await driver.bind_launch(action, record)
    await env.db.create_session(record)
    scope = replace(env.scope, session_id='fresh', instance_token='fresh-token', claim_epoch=2)
    return driver, action, scope


async def test_resume_ack_requires_exact_delivered_outcome_and_consumes_snapshot(env):
    from src.waits.resume_ack import acknowledge, mark_delivered
    from src.waits.holding import active_wait_for
    from src.waits.fencing import mutation_fence

    _, _, scope = await bound_resume(env)
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


async def test_fresh_resume_builds_new_git_folder_without_ignored_transcript(env, tmp_path):
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
    slots = SimpleNamespace(reprovision_slot=AsyncMock())
    orch = SimpleNamespace(db=env.db, git=git, _prepare_workspace_locked=AsyncMock(
        return_value=str(old)), _git_mutex=lambda _: asyncio.Lock(), _worktree_slots=lambda: slots)
    driver = ResumeTransaction(SimpleNamespace(orch=orch))
    target = await driver.fresh_workspace(AssignAction('a', 'owner', 'p', 'c', 1),
        await env.db.get_task('owner'), None, await intent(env))
    assert target != str(old) and Path(target).is_dir()
    assert not (Path(target) / 'transcript.cache').exists()
    assert await git._arun(['rev-parse', 'HEAD'], cwd=target) == head
    assert await git._arun(['branch', '--show-current'], cwd=target) == 'aq/owner'
    env.db.update_workspace.assert_awaited_once_with('slot', workspace_path=target)
    slots.reprovision_slot.assert_awaited_once()


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
                           _pool_quarantine_state=lambda *a: (None, None))
    service = SimpleNamespace(orch=orch, scheduler=SchedulerWaitService(orch, clock=lambda: NOW))
    driver = ResumeTransaction(service, clock=lambda: NOW + 30)
    assert await driver.reserve('c', 1) is None
    assert (await env.db.get_session('idle')).desired_state == 'stopped'
    assert await driver.reserve('c', 1) is None  # Requested stop is not proof of free capacity.
    await env.db.update_session('idle', state='stopped')
    action = await driver.reserve('c', 1)
    assert action is not None and action.continuation_id == 'c'
    assert (await driver.reserve('c', 1)).agent_id == action.agent_id
    async with env.db._engine.connect() as conn:
        reservations = list((await conn.execute(select(admission_reservations))).mappings())
    assert len(reservations) == 1 and reservations[0]['state'] == 'granted'
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
