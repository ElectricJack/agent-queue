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
