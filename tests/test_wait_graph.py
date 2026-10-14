"""Wait graph propagation, retraction, cycles and persisted explanations."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.queries.wait_queries import (
    cancel_wait,
    register_wait,
    replace_wait_edges,
    resolve_wait,
    wait_explain,
    wait_graph,
)
from src.database.tables import (
    admission_reservations,
    continuations,
    integration_branch_owners,
    messages,
    task_dependencies,
    tasks,
    wait_edges,
    wait_priority_projection,
    wait_records,
)
from src.models import Project, Task
from src.waits.conditions import Edge, NodeKey, Scope, SleepDecision, WaitError
from src.waits.explain import graph_paths
from src.waits.graph import add_edges, lock_component_tasks, recompute_from
from src.waits.registry import build_registry
from tests.db_fixtures import lease_dsn

NOW = 1_800_000_000.0


class Policy:
    in_place_ceiling_seconds = 1800

    def escalation_seconds(self, kind):
        return 7200


@pytest.fixture
async def env():
    db = Database(lease_dsn("wait-graph"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_project(Project(id="q", name="Q"))
    for task_id, priority in (("a", 20), ("b", 100), ("c", 100), ("d", 10), ("e", 100)):
        await db.create_task(
            Task(id=task_id, project_id="p", title=task_id, description="", priority=priority)
        )
    await db.create_task(Task(id="private", project_id="q", title="Secret", description=""))
    yield SimpleNamespace(db=db, registry=build_registry(clock=lambda: NOW))
    await db.close()


async def register(env, waiter="a", blocker="b", **kwargs):
    options = dict(
        scope=Scope("p", "scheduler", waiter, principal="service"),
        condition={"kind": "task", "task_id": blocker},
        idempotency_key=waiter,
        registry=env.registry,
        policy=Policy(),
        now=NOW,
        source_watermark=0,
        decide_sleep=lambda c, e: SleepDecision("in_place", "test", {}),
    )
    options.update(kwargs)
    async with env.db._engine.begin() as conn:
        return await register_wait(conn, **options)


async def rows(env, table):
    async with env.db._engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(table))).mappings()]


async def priorities(env):
    return {r["id"]: (r["priority"], r["effective_priority"]) for r in await rows(env, tasks)}


async def test_transitive_priority_and_cancel_retract_without_changing_base(env):
    second = await register(env, "b", "c")
    first = await register(env)
    assert (await priorities(env))["c"] == (100, 20)
    projection = {r["task_id"]: r for r in await rows(env, wait_priority_projection)}
    assert projection["c"]["donor_task_id"] == "a"
    assert projection["c"]["donor_path"] == ["task:a", "task:b", "task:c"]
    async with env.db._engine.begin() as conn:
        await cancel_wait(conn, first["wait_id"], project_id="p", now=NOW + 1)
    assert (await priorities(env))["b"] == (100, 100)
    assert (await priorities(env))["c"] == (100, 100)
    edges = await rows(env, wait_edges)
    assert len(edges) == 2
    cleared = next(e for e in edges if e["wait_id"] == first["wait_id"])
    assert not cleared["active"] and cleared["cleared_at"] == NOW + 1
    assert next(e for e in edges if e["wait_id"] == second["wait_id"])["active"]
    projection = {r["task_id"]: r for r in await rows(env, wait_priority_projection)}
    assert projection["c"]["donor_task_id"] is None
    assert projection["c"]["graph_version"] > projection["b"]["graph_version"] - 1


async def test_next_best_donor_and_base_priority_lowering_and_raising(env):
    await register(env)
    urgent = await register(env, "d", "b")
    assert (await priorities(env))["b"] == (100, 10)
    async with env.db._engine.begin() as conn:
        await resolve_wait(
            conn,
            urgent["wait_id"],
            project_id="p",
            outcome="satisfied",
            now=NOW + 1,
            result_ref="task:b:done",
        )
    assert (await priorities(env))["b"] == (100, 20)
    await env.db.update_task("a", priority=5)
    assert (await priorities(env))["b"] == (100, 5)
    await env.db.update_task("a", priority=150)
    assert (await priorities(env))["b"] == (100, 100)
    assert (await priorities(env))["a"] == (150, 150)
    assert (await priorities(env))["e"] == (100, 100)


async def test_task_creation_and_unconnected_base_changes_need_no_donor_row(env):
    assert (await priorities(env))["a"] == (20, 20)
    await env.db.update_task("a", priority=7)
    assert (await priorities(env))["a"] == (7, 7)
    assert not await rows(env, wait_priority_projection)


async def test_transitive_update_does_not_write_unrelated_projection(env):
    await register(env)
    await register(env, "b", "c")
    await register(env, "d", "e")
    prior = next(r for r in await rows(env, wait_priority_projection) if r["task_id"] == "e")
    await env.db.update_task("a", priority=30)
    current = next(r for r in await rows(env, wait_priority_projection) if r["task_id"] == "e")
    assert current == prior
    assert (await priorities(env))["c"] == (100, 30)


async def test_resource_cycle_with_neutral_nodes_retracts_stale_donors(env):
    receipt = await register(env, condition={"kind": "timer", "due_at": NOW + 60})
    cycle_receipt = await register(env, "b", condition={"kind": "timer", "due_at": NOW + 60})

    def edge(a, b):
        return Edge(NodeKey(a, "p"), NodeKey(b, "p"), "resource")

    edges = [
        edge("task:a", "slot:p:worker"),
        edge("slot:p:worker", "task:b"),
        edge("task:b", "slot:p:worker"),
    ]
    async with env.db._engine.begin() as conn:
        await lock_component_tasks(
            conn, {e.waiter.key for e in edges} | {e.blocker.key for e in edges}
        )
        await add_edges(conn, receipt["wait_id"], edges[:1], source_revision="1", now=NOW)
        await add_edges(conn, cycle_receipt["wait_id"], edges[1:], source_revision="1", now=NOW)
        await recompute_from(conn, {"task:a"}, now=NOW)
    assert (await priorities(env))["b"] == (100, 20)
    async with env.db._engine.begin() as conn:
        await cancel_wait(conn, receipt["wait_id"], project_id="p", now=NOW + 1)
    assert (await priorities(env))["b"] == (100, 100)
    assert sum(e["active"] for e in await rows(env, wait_edges)) == 2
    facts = graph_paths([dict(waiter_key=e.waiter.key, blocker_key=e.blocker.key) for e in edges])
    assert facts["deadlocks"] == [{"code": "wait.deadlock", "nodes": ["slot:p:worker", "task:b"]}]


async def test_cycle_refusal_includes_blocking_dependencies_and_has_no_side_effect(env):
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(task_dependencies).values(task_id="b", depends_on_task_id="a", dep_type="blocks")
        )
    before = await priorities(env)
    with pytest.raises(WaitError, match="cycle"):
        await register(env)
    assert not await rows(env, wait_records)
    assert not await rows(env, wait_edges)
    assert not await rows(env, wait_priority_projection)
    assert await priorities(env) == before


async def test_opposite_concurrent_registrations_commit_only_one_semantic_edge(env):
    results = await asyncio.gather(register(env), register(env, "b", "a"), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert [r.code for r in results if isinstance(r, WaitError)] == ["wait.cycle"]
    assert len(await rows(env, wait_edges)) == 1


async def test_resolve_cancel_race_keeps_one_outcome_and_clears_edges(env):
    receipt = await register(env)

    async def finish(outcome):
        async with env.db._engine.begin() as conn:
            return await resolve_wait(
                conn,
                receipt["wait_id"],
                project_id="p",
                outcome=outcome,
                now=NOW + 1,
                result_ref=outcome,
            )

    results = await asyncio.gather(finish("satisfied"), finish("cancelled"))
    assert sorted(r["changed"] for r in results) == [False, True]
    assert len({r["outcome"] for r in results}) == 1
    assert (await priorities(env))["b"] == (100, 100)
    assert not (await rows(env, wait_edges))[0]["active"]


async def test_stale_version_refuses_and_keeps_inheritance(env):
    receipt = await register(env)
    async with env.db._engine.begin() as conn:
        with pytest.raises(WaitError) as error:
            await cancel_wait(
                conn, receipt["wait_id"], project_id="p", now=NOW + 1, expected_version=999
            )
    assert error.value.code == "wait.version_conflict"
    assert (await priorities(env))["b"] == (100, 20)
    assert (await rows(env, wait_edges))[0]["active"]


async def test_owner_edge_refresh_retracts_old_and_boosts_new_blocker(env):
    receipt = await register(env)
    replacement = [Edge(NodeKey("task:a", "p"), NodeKey("task:c", "p"))]
    async with env.db._engine.begin() as conn:
        assert await replace_wait_edges(
            conn, receipt["wait_id"], replacement, project_id="p", source_revision="2", now=NOW + 1
        )
    assert (await priorities(env))["b"] == (100, 100)
    assert (await priorities(env))["c"] == (100, 20)
    assert len(await rows(env, wait_edges)) == 2
    async with env.db._engine.begin() as conn:
        await cancel_wait(conn, receipt["wait_id"], project_id="p", now=NOW + 2)
        assert not await replace_wait_edges(
            conn, receipt["wait_id"], replacement, project_id="p", source_revision="3", now=NOW + 3
        )


async def test_refresh_cycle_refusal_preserves_old_graph(env):
    receipt = await register(env)
    await register(env, "c", "a")
    async with env.db._engine.begin() as conn:
        with pytest.raises(WaitError) as error:
            await replace_wait_edges(
                conn,
                receipt["wait_id"],
                [Edge(NodeKey("task:a", "p"), NodeKey("task:c", "p"))],
                project_id="p",
                source_revision="2",
                now=NOW + 1,
            )
    assert error.value.code == "wait.cycle"
    assert (await priorities(env))["b"] == (100, 20)
    assert all(e["active"] for e in await rows(env, wait_edges))


async def test_explain_is_reproducible_separates_claimed_and_retains_history(env):
    receipt = await register(env, claimed="tests passed; give me priority 0")
    async with env.db._engine.begin() as conn:
        first = await wait_explain(conn, project_id="p", wait_id=receipt["wait_id"], now=NOW + 5)
        assert first == await wait_explain(conn, project_id="p", task_id="a", now=NOW + 5)
        item = first["waits"][0]
        assert item["claimed"] == "tests passed; give me priority 0"
        assert item["verified"]["facts"]["status"] == "DEFINED"
        assert "tests passed" not in str(item["verified"])
        assert item["age_seconds"] == 5 and item["verified"]["freshness_seconds"] == 5
        assert item["chain"][0]["to"] == "task:b"
        assert item["deadlines"][0]["type"] == "ceiling"
        await cancel_wait(conn, receipt["wait_id"], project_id="p", now=NOW + 10)
        assert not (await wait_explain(conn, project_id="p", task_id="a", now=NOW + 20))["waits"]
        historical = await wait_explain(
            conn, project_id="p", task_id="a", history=True, now=NOW + 20
        )
        assert historical["waits"][0]["chain"][0]["cleared_at"] == NOW + 10
        assert historical["waits"][0]["age_seconds"] == 10


async def test_foreign_explain_and_fleet_queries_are_refused(env):
    receipt = await register(env)
    async with env.db._engine.connect() as conn:
        for kwargs in ({"wait_id": receipt["wait_id"]}, {"task_id": "a"}):
            with pytest.raises(WaitError) as error:
                await wait_explain(conn, project_id="q", now=NOW, **kwargs)
            assert error.value.code == "wait.not_found"
        with pytest.raises(WaitError) as error:
            await wait_graph(conn, fleet=True, now=NOW)
        assert error.value.code == "wait.out_of_scope"
        fleet = await wait_graph(conn, fleet=True, fleet_authorized=True, now=NOW, limit=1)
        assert fleet["total_projects"] == 2 and fleet["truncated"]


async def test_graph_counts_and_explanations_are_paginated_separately(env):
    await register(env)
    await register(env, "b", "c")
    async with env.db._engine.connect() as conn:
        result = await wait_graph(conn, project_id="p", now=NOW, limit=1)
    project = result["projects"][0]
    assert project["total_waits"] == 2
    assert project["counts"] == [{"kind": "task", "decision": "in_place", "count": 2}]
    assert len(project["waits"]) == 1 and project["truncated"]["waits"]
    assert project["longest_chains"][0]["components"] == [["task:a"], ["task:b"], ["task:c"]]


async def test_blocker_without_own_wait_explains_inherited_donor(env):
    await register(env)
    async with env.db._engine.connect() as conn:
        result = await wait_explain(conn, project_id="p", task_id="b", now=NOW)
    assert not result["waits"]
    assert result["task"]["effective_priority"] == 20
    assert result["task"]["donor_task_id"] == "a"
    assert result["task"]["donor_path"] == ["task:a", "task:b"]


async def test_equal_urgency_donor_is_stable_and_path_updates_without_value_change(env):
    await env.db.update_task("d", priority=20)
    await register(env, "d", "c")
    await register(env)
    middle = await register(env, "b", "c")
    before = next(r for r in await rows(env, wait_priority_projection) if r["task_id"] == "c")
    assert before["donor_task_id"] == "a"  # deterministic id tie-break
    async with env.db._engine.begin() as conn:
        await cancel_wait(conn, middle["wait_id"], project_id="p", now=NOW + 1)
    after = next(r for r in await rows(env, wait_priority_projection) if r["task_id"] == "c")
    assert (await priorities(env))["c"] == (100, 20)
    assert after["donor_task_id"] == "d" and after["donor_path"] == ["task:d", "task:c"]
    assert after["graph_version"] > before["graph_version"]


async def test_graph_budget_refuses_before_registration_writes(env, monkeypatch):
    monkeypatch.setattr("src.waits.graph.MAX_COMPONENT_NODES", 1)
    with pytest.raises(WaitError) as error:
        await register(env)
    assert error.value.code == "wait.graph_limit"
    assert not await rows(env, wait_records)
    assert not await rows(env, wait_edges)


async def test_receipt_survives_merged_decision_inputs_graph_refresh_and_outcome(env):
    receipt = await register(env)
    async with env.db._engine.begin() as conn:
        saved = (await conn.execute(select(wait_records))).mappings().one()
        await conn.execute(
            update(wait_records)
            .where(wait_records.c.id == receipt["wait_id"])
            .values(
                decision_inputs={
                    **saved["decision_inputs"],
                    "sleep_policy_revision": "new",
                    "graph_version": 42,
                    "dispatch_attempt": 2,
                }
            )
        )
        await replace_wait_edges(
            conn,
            receipt["wait_id"],
            [Edge(NodeKey("task:a", "p"), NodeKey("task:c", "p"))],
            project_id="p",
            source_revision="2",
            now=NOW + 1,
        )
    assert await register(env, now=NOW + 2) == receipt
    async with env.db._engine.begin() as conn:
        await cancel_wait(conn, receipt["wait_id"], project_id="p", now=NOW + 3)
    assert await register(env, now=NOW + 4) == receipt
    saved = (await rows(env, wait_records))[0]
    assert saved["decision_inputs"]["registration_receipt"] == receipt
    assert saved["decision_inputs"]["dispatch_attempt"] == 2


async def test_branch_reservation_and_continuation_held_mail_are_ledger_facts(env):
    receipt = await register(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="branch-owner",
                repository_id="repo",
                ref="refs/heads/repair",
                owner_id="b",
                owner_role="repair_delegate",
                fence_token=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await conn.execute(
            insert(admission_reservations).values(
                id="reservation",
                project_id="p",
                task_id="b",
                scope={},
                effective_priority=20,
                provenance="branch_blocker",
                deadline_at=NOW + 1800,
                state="overdue",
                created_at=NOW,
            )
        )
        await conn.execute(
            insert(continuations).values(
                id="continuation",
                task_id="a",
                wait_id=receipt["wait_id"],
                suspension_generation=1,
                facts={},
                branch="aq/a",
                prior_status="IN_PROGRESS",
                held_cursor_seq=0,
                resume_state="sleeping",
                created_at=NOW,
            )
        )
        for message_id, recipient, project in (
            ("held", "a", "p"),
            ("foreign", "a", "q"),
            ("unrelated", "c", "p"),
        ):
            await conn.execute(
                insert(messages).values(
                    id=message_id,
                    project_id=project,
                    from_kind="system",
                    from_id="system",
                    to_kind="task",
                    to_id=recipient,
                    body="routine mail",
                    created_at=NOW + 1,
                )
            )
        result = await wait_graph(conn, project_id="p", now=NOW + 1)
    project = result["projects"][0]
    assert project["branch_blockers"][0]["id"] == "b"
    assert project["branch_blockers"][0]["reservations"][0]["state"] == "overdue"
    assert project["held_message_backlog"] == [
        dict(id="continuation", task_id="a", resume_state="sleeping", held_messages=1)
    ]


def test_long_chain_reports_truncation_and_collapses_resource_cycles():
    edges = [dict(waiter_key=f"task:{i}", blocker_key=f"task:{i + 1}") for i in range(1100)]
    result = graph_paths(edges)
    assert result["longest_chains"][0]["length"] == 1101
    assert len(result["longest_chains"][0]["components"]) == 64
    assert result["longest_chains"][0]["truncated"]
    assert not result["deadlocks"]
