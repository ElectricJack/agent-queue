"""Work-graph WG-1/WG-2: the ``tasks.is_blocked`` projection.

Covers docs/specs/implementation/work-graph.md §11:

* the per-dep-type satisfaction truth table (§3.1),
* gate open/resolved/expired,
* dynamic ``waits-for`` re-block on child creation,
* the conditional terminal-failure edge and its auto-close disposal,
* mixed multi-edge tasks,
* a property test comparing incremental recompute against brute-force full
  evaluation over random DAGs,
* the shadow-mode parity assertion against the legacy scan,
* label filters and the ``hold:*`` ready-frontier convention.
"""

from __future__ import annotations

import random
import time
import uuid

import pytest
from sqlalchemy import insert, select, text

from src.database import SQLiteDatabaseAdapter
from src.database.queries.blocked_state import blocked_predicate
from src.database.tables import gates, task_dependencies, task_gates, tasks as tasks_t
from src.models import DepType, Project, Task, TaskStatus
from src.state_machine import CyclicDependencyError, validate_dag_with_new_edge, validate_waits_for


PROJECT = "p-wg"


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabaseAdapter(str(tmp_path / "wg.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT, name="work-graph"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(
            id=tid,
            project_id=PROJECT,
            title=tid,
            description=tid,
            status=status,
            **kw,
        )
    )
    return tid


async def blocked(db, tid) -> bool:
    task = await db.get_task(tid)
    return task.is_blocked


async def mkgate(db, gate_id, status="open", waiters=()):
    """Insert a gate row directly — the gate query layer is WG-3's."""
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(gates).values(
                id=gate_id,
                project_id=PROJECT,
                gate_type="human",
                title=gate_id,
                question="",
                status=status,
                created_at=time.time(),
            )
        )
        for tid in waiters:
            await conn.execute(insert(task_gates).values(task_id=tid, gate_id=gate_id))
        await db.recompute_blocked(set(waiters), conn=conn)


# ── §3.1 satisfaction truth table ────────────────────────────────────────


class TestSatisfactionTruthTable:
    @pytest.mark.parametrize(
        "dep_status,expect_blocked",
        [
            (TaskStatus.DEFINED, True),
            (TaskStatus.READY, True),
            (TaskStatus.IN_PROGRESS, True),
            (TaskStatus.FAILED, True),
            (TaskStatus.BLOCKED, True),
            (TaskStatus.COMPLETED, False),
        ],
    )
    async def test_blocks_edge(self, db, dep_status, expect_blocked):
        await mktask(db, "dep", status=dep_status)
        await mktask(db, "t")
        await db.add_dependency("t", "dep", DepType.BLOCKS.value)
        assert await blocked(db, "t") is expect_blocked

    @pytest.mark.parametrize(
        "parent_status,expect_blocked",
        [
            # Withholding container statuses.
            (TaskStatus.DEFINED, True),
            # Every other status counts as "released".
            (TaskStatus.READY, False),
            (TaskStatus.IN_PROGRESS, False),
            (TaskStatus.COMPLETED, False),
            (TaskStatus.FAILED, False),
            (TaskStatus.BLOCKED, False),
        ],
    )
    async def test_parent_child_edge(self, db, parent_status, expect_blocked):
        # Written directly (not via ``add_dependency``/``set_parent``) so this
        # exercises the satisfaction truth table in isolation from the
        # HierarchyQueryMixin write-path guards (e.g. ``container_closed``
        # for a COMPLETED parent — see test_hierarchy_queries.py).
        await mktask(db, "parent", status=parent_status)
        await mktask(db, "child")
        async with db._engine.begin() as conn:
            await conn.execute(
                insert(task_dependencies).values(
                    task_id="child",
                    depends_on_task_id="parent",
                    dep_type=DepType.PARENT_CHILD.value,
                )
            )
            await db.recompute_blocked({"child"}, conn=conn)
        assert await blocked(db, "child") is expect_blocked

    async def test_waits_for_is_vacuously_satisfied_without_children(self, db):
        await mktask(db, "container", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "finalize")
        await db.add_dependency("finalize", "container", DepType.WAITS_FOR.value)
        assert await blocked(db, "finalize") is False

    async def test_waits_for_blocks_while_a_child_is_open(self, db):
        await mktask(db, "container", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "finalize")
        await mktask(db, "worker", status=TaskStatus.IN_PROGRESS)
        await db.add_dependency("finalize", "container", DepType.WAITS_FOR.value)
        await db.add_dependency("worker", "container", DepType.PARENT_CHILD.value)
        assert await blocked(db, "finalize") is True

        await db.transition_task("worker", TaskStatus.COMPLETED)
        assert await blocked(db, "finalize") is False

    async def test_waits_for_re_blocks_when_a_child_appears(self, db):
        """The dynamic fan-in rule: a late child re-blocks a cleared waiter."""
        await mktask(db, "container", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "finalize")
        await db.add_dependency("finalize", "container", DepType.WAITS_FOR.value)
        assert await blocked(db, "finalize") is False

        await mktask(db, "late", status=TaskStatus.READY)
        await db.add_dependency("late", "container", DepType.PARENT_CHILD.value)
        assert await blocked(db, "finalize") is True

    @pytest.mark.parametrize(
        "dep_status,retry_count,max_retries,expect_blocked",
        [
            # Terminal failure satisfies the conditional edge.
            (TaskStatus.BLOCKED, 0, 3, False),
            (TaskStatus.FAILED, 3, 3, False),
            (TaskStatus.FAILED, 4, 3, False),
            # A transiently FAILED task still has retries left.
            (TaskStatus.FAILED, 1, 3, True),
            # Success means the contingency will never be needed — the edge
            # stays unsatisfied (the auto-close cascade disposes of it).
            (TaskStatus.COMPLETED, 0, 3, True),
            (TaskStatus.IN_PROGRESS, 0, 3, True),
        ],
    )
    async def test_conditional_blocks_edge(
        self, db, dep_status, retry_count, max_retries, expect_blocked
    ):
        await mktask(db, "dep", status=dep_status, retry_count=retry_count, max_retries=max_retries)
        await mktask(db, "contingency")
        await db.add_dependency("contingency", "dep", DepType.CONDITIONAL_BLOCKS.value)
        assert await blocked(db, "contingency") is expect_blocked

    @pytest.mark.parametrize("dep_type", ["discovered-from", "related", "duplicates", "supersedes"])
    async def test_non_blocking_edges_never_block(self, db, dep_type):
        await mktask(db, "origin", status=TaskStatus.DEFINED)
        await mktask(db, "t")
        await db.add_dependency("t", "origin", dep_type)
        assert await blocked(db, "t") is False

    async def test_mixed_multi_edge_task(self, db):
        """Any unsatisfied blocking edge blocks; provenance edges never do."""
        await mktask(db, "a", status=TaskStatus.COMPLETED)
        await mktask(db, "b", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "plan", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "t")
        await db.add_dependency("t", "a", DepType.BLOCKS.value)
        await db.add_dependency("t", "plan", DepType.PARENT_CHILD.value)
        await db.add_dependency("t", "plan", DepType.DISCOVERED_FROM.value)
        assert await blocked(db, "t") is False

        await db.add_dependency("t", "b", DepType.BLOCKS.value)
        assert await blocked(db, "t") is True

        await db.transition_task("b", TaskStatus.COMPLETED)
        assert await blocked(db, "t") is False

    async def test_one_pair_can_carry_two_edge_types(self, db):
        await mktask(db, "plan", status=TaskStatus.DEFINED)
        await mktask(db, "child")
        await db.add_dependency("child", "plan", DepType.PARENT_CHILD.value)
        await db.add_dependency("child", "plan", DepType.DISCOVERED_FROM.value)
        assert set(await db.get_typed_dependencies("child")) == {
            ("plan", "parent-child"),
            ("plan", "discovered-from"),
        }


# ── Gates (WG-3 owns the query layer; the predicate is WG-1's) ───────────


class TestGateClause:
    @pytest.mark.parametrize(
        "status,expect_blocked",
        [("open", True), ("expired", True), ("resolved", False)],
    )
    async def test_gate_status(self, db, status, expect_blocked):
        await mktask(db, "t")
        await mkgate(db, "gate-1", status=status, waiters=["t"])
        assert await blocked(db, "t") is expect_blocked

    async def test_blocked_recovery_needs_a_graph_blocker(self, db):
        """``tasks_with_graph_blockers`` distinguishes graph- from failure-BLOCKED."""
        await mktask(db, "failure", status=TaskStatus.BLOCKED)
        await mktask(db, "dep", status=TaskStatus.COMPLETED)
        await mktask(db, "graph", status=TaskStatus.BLOCKED)
        await db.add_dependency("graph", "dep", DepType.BLOCKS.value)
        await mktask(db, "gated", status=TaskStatus.BLOCKED)
        await mkgate(db, "gate-2", status="resolved", waiters=["gated"])

        found = await db.tasks_with_graph_blockers(["failure", "graph", "gated"])
        assert found == {"graph", "gated"}


# ── Recompute mechanics ──────────────────────────────────────────────────


class TestRecomputeMechanics:
    async def test_new_task_starts_unblocked(self, db):
        await mktask(db, "t")
        assert await blocked(db, "t") is False

    async def test_removing_the_last_edge_unblocks(self, db):
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "t")
        await db.add_dependency("t", "dep")
        assert await blocked(db, "t") is True
        await db.remove_dependency("t", "dep")
        assert await blocked(db, "t") is False

    async def test_remove_dependency_without_type_removes_every_kind(self, db):
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "t")
        await db.add_dependency("t", "dep", DepType.BLOCKS.value)
        await db.add_dependency("t", "dep", DepType.RELATED.value)
        await db.remove_dependency("t", "dep")
        assert await db.get_typed_dependencies("t") == []

    async def test_remove_dependency_can_target_one_kind(self, db):
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "t")
        await db.add_dependency("t", "dep", DepType.BLOCKS.value)
        await db.add_dependency("t", "dep", DepType.RELATED.value)
        await db.remove_dependency("t", "dep", DepType.BLOCKS.value)
        assert await db.get_typed_dependencies("t") == [("dep", "related")]
        assert await blocked(db, "t") is False

    async def test_deleting_a_dependency_unblocks_its_dependents(self, db):
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "t")
        await db.add_dependency("t", "dep")
        assert await blocked(db, "t") is True
        await db.delete_task("dep")
        assert await blocked(db, "t") is False

    async def test_deleting_a_child_unblocks_the_fan_in_waiter(self, db):
        await mktask(db, "container", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "finalize")
        await mktask(db, "worker", status=TaskStatus.READY)
        await db.add_dependency("finalize", "container", DepType.WAITS_FOR.value)
        await db.add_dependency("worker", "container", DepType.PARENT_CHILD.value)
        assert await blocked(db, "finalize") is True
        await db.delete_task("worker")
        assert await blocked(db, "finalize") is False

    async def test_remove_all_dependencies_on_recomputes_former_dependents(self, db):
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "a")
        await mktask(db, "b")
        await db.add_dependency("a", "dep")
        await db.add_dependency("b", "dep")
        await db.remove_all_dependencies_on("dep")
        assert await blocked(db, "a") is False
        assert await blocked(db, "b") is False

    async def test_transition_task_returns_flipped_ids(self, db):
        await mktask(db, "dep", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "t")
        await db.add_dependency("t", "dep")
        flipped = await db.transition_task("dep", TaskStatus.COMPLETED)
        assert flipped == {"t"}

    async def test_flips_are_logged_as_events(self, db):
        await mktask(db, "dep", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "t")
        await db.add_dependency("t", "dep")  # t becomes blocked
        await db.transition_task("dep", TaskStatus.COMPLETED)  # t unblocks

        events = await db.get_recent_events(limit=50, task_id="t")
        types = [e["event_type"] for e in events]
        assert "task.blocked" in types
        assert "task.unblocked" in types

    async def test_recompute_does_not_touch_updated_at(self, db):
        """``updated_at`` means time-in-current-state; a projection refresh
        is not a state change."""
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "t")
        before = await db.get_task_updated_at("t")
        await db.add_dependency("t", "dep")
        assert await db.get_task_updated_at("t") == before

    async def test_archive_carries_is_blocked_across(self, db):
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "t", status=TaskStatus.COMPLETED)
        await db.add_dependency("t", "dep")
        assert await blocked(db, "t") is True
        assert await db.archive_task("t") is True
        archived = await db.get_archived_task("t")
        assert archived["is_blocked"] is True


# ── Projection inputs other than `status` ────────────────────────────────


class TestProjectionInputWrites:
    """The ``conditional-blocks`` clause reads ``retry_count >= max_retries``,
    so a write to either column is a projection input just like ``status``.

    Both reproductions below left ``is_blocked`` stale when only ``status``
    triggered a recompute.
    """

    async def _primary_and_contingency(self, db):
        """A transiently-FAILED primary with a contingency waiting on its
        *terminal* failure.  The contingency is blocked: retries remain."""
        await mktask(db, "primary", status=TaskStatus.FAILED, retry_count=0, max_retries=3)
        await mktask(db, "contingency")
        await db.add_dependency("contingency", "primary", DepType.CONDITIONAL_BLOCKS.value)
        assert await blocked(db, "contingency") is True

    async def test_same_status_retry_bump_recomputes(self, db):
        """``transition_task`` returns early when the status does not move;
        the ``retry_count`` it carried still has to recompute."""
        await self._primary_and_contingency(db)

        flipped = await db.transition_task("primary", TaskStatus.FAILED, retry_count=3)

        assert flipped == {"contingency"}
        assert await blocked(db, "contingency") is False
        assert (await db.evaluate_blocked(["contingency"]))["contingency"] is False

    async def test_same_status_write_without_a_projection_input_is_cheap(self, db):
        """A same-status write of an unrelated column flips nothing."""
        await self._primary_and_contingency(db)
        flipped = await db.transition_task("primary", TaskStatus.FAILED, branch_name="aq/x")
        assert flipped == set()
        assert await blocked(db, "contingency") is True

    async def test_update_task_max_retries_recomputes(self, db):
        """``update_task(primary, max_retries=10)`` turns a terminal failure
        back into a transient one — the contingency must re-block."""
        await mktask(db, "primary", status=TaskStatus.FAILED, retry_count=3, max_retries=3)
        await mktask(db, "contingency")
        await db.add_dependency("contingency", "primary", DepType.CONDITIONAL_BLOCKS.value)
        assert await blocked(db, "contingency") is False

        await db.update_task("primary", max_retries=10)

        assert await blocked(db, "contingency") is True
        assert (await db.evaluate_blocked(["contingency"]))["contingency"] is True

    async def test_update_task_retry_count_recomputes(self, db):
        await self._primary_and_contingency(db)
        await db.update_task("primary", retry_count=3)
        assert await blocked(db, "contingency") is False


# ── Conditional disposal ─────────────────────────────────────────────────


class TestConditionalDisposal:
    async def test_dead_conditional_is_detected(self, db):
        await mktask(db, "primary", status=TaskStatus.COMPLETED)
        await mktask(db, "contingency")
        await db.add_dependency("contingency", "primary", DepType.CONDITIONAL_BLOCKS.value)
        assert await db.find_dead_conditional_tasks() == [("contingency", PROJECT)]

    async def test_live_conditional_is_not_dead(self, db):
        await mktask(db, "primary", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "contingency")
        await db.add_dependency("contingency", "primary", DepType.CONDITIONAL_BLOCKS.value)
        assert await db.find_dead_conditional_tasks() == []

    async def test_other_blockers_keep_the_contingency_alive(self, db):
        await mktask(db, "primary", status=TaskStatus.COMPLETED)
        await mktask(db, "other", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "contingency")
        await db.add_dependency("contingency", "primary", DepType.CONDITIONAL_BLOCKS.value)
        await db.add_dependency("contingency", "other", DepType.BLOCKS.value)
        assert await db.find_dead_conditional_tasks() == []

    @pytest.mark.parametrize("seed", [11, 12, 13])
    async def test_the_two_predicates_agree_without_conditional_edges(self, db, seed):
        """``_blocked_ignoring_conditional`` is ``blocked_predicate`` minus one
        clause; on a graph with no ``conditional-blocks`` edges the two must
        select exactly the same rows."""
        from src.database.queries.blocked_state import _blocked_ignoring_conditional

        rng = random.Random(seed)
        statuses = list(TaskStatus)
        non_conditional = [d.value for d in DepType if d is not DepType.CONDITIONAL_BLOCKS]

        ids = [f"m{i:02d}" for i in range(12)]
        for tid in ids:
            await mktask(db, tid, status=rng.choice(statuses))
        for _ in range(20):
            i, j = sorted(rng.sample(range(len(ids)), 2))
            try:
                await db.add_dependency(ids[j], ids[i], rng.choice(non_conditional))
            except Exception:
                pass  # duplicate (pair, type) — fine
        await mkgate(db, "gate-m", status=rng.choice(["open", "resolved"]), waiters=[ids[0]])

        async with db._engine.begin() as conn:
            with_cond = {
                r[0] for r in (await conn.execute(select(tasks_t.c.id).where(blocked_predicate())))
            }
            without_cond = {
                r[0]
                for r in (
                    await conn.execute(select(tasks_t.c.id).where(_blocked_ignoring_conditional()))
                )
            }
        assert with_cond == without_cond

    @pytest.mark.parametrize("seed", [21, 22, 23, 24])
    async def test_ignoring_conditional_is_a_subset_on_conditional_graphs(self, db, seed):
        """The stronger guard: on graphs that *do* carry ``conditional-blocks``
        edges, dropping that one clause can only ever remove blocked rows.

        A strict subset is expected (that is the point of the helper); a row
        blocked without the conditional clause but not with it would mean the
        two expressions disagree on a shared clause.
        """
        from src.database.queries.blocked_state import _blocked_ignoring_conditional

        rng = random.Random(seed)
        statuses = list(TaskStatus)
        all_types = [d.value for d in DepType]

        ids = [f"c{i:02d}" for i in range(14)]
        for tid in ids:
            await mktask(
                db,
                tid,
                status=rng.choice(statuses),
                retry_count=rng.choice([0, 3]),
                max_retries=3,
            )
        saw_conditional = False
        for _ in range(28):
            i, j = sorted(rng.sample(range(len(ids)), 2))
            dep_type = rng.choice(all_types)
            try:
                await db.add_dependency(ids[j], ids[i], dep_type)
            except Exception:
                continue  # duplicate (pair, type) — fine
            saw_conditional |= dep_type == DepType.CONDITIONAL_BLOCKS.value
        # Force at least one conditional edge so the test is never vacuous.
        if not saw_conditional:
            await db.add_dependency(ids[-1], ids[0], DepType.CONDITIONAL_BLOCKS.value)
        await mkgate(db, "gate-c", status=rng.choice(["open", "resolved"]), waiters=[ids[1]])

        async with db._engine.begin() as conn:
            with_cond = {
                r[0] for r in (await conn.execute(select(tasks_t.c.id).where(blocked_predicate())))
            }
            without_cond = {
                r[0]
                for r in (
                    await conn.execute(select(tasks_t.c.id).where(_blocked_ignoring_conditional()))
                )
            }
        assert without_cond <= with_cond

    def test_the_shared_clauses_are_aliased_independently(self):
        """Two calls of the same clause factory inside one statement compile to
        distinct anonymous aliases, so no ``EXISTS`` term is dropped."""
        from sqlalchemy import or_, select as sa_select

        from src.database.queries.blocked_state import _blocks_unsat

        sql = str(sa_select(tasks_t.c.id).where(or_(_blocks_unsat(), _blocks_unsat())).compile())
        assert sql.count("EXISTS") == 2
        # `_blocked_ignoring_conditional` shares four clauses with
        # `blocked_predicate`; both must still carry every term, including
        # the manual-pause dependency guard.
        from src.database.queries.blocked_state import _blocked_ignoring_conditional

        assert (
            str(sa_select(tasks_t.c.id).where(blocked_predicate()).compile()).count("EXISTS") == 7
        )
        assert (
            str(sa_select(tasks_t.c.id).where(_blocked_ignoring_conditional()).compile()).count(
                "EXISTS"
            )
            == 6
        )


class TestWaveDriver:
    async def test_extra_waves_are_applied(self, db):
        """``recompute_blocked_waves`` runs one wave per supplied seed set,
        so a status change the first wave could not have seen still lands."""
        await mktask(db, "dep", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "t")
        await db.add_dependency("t", "dep")
        assert await blocked(db, "t") is True

        async with db._engine.begin() as conn:
            # Two mutations in one transaction: the status change happens
            # after the first wave's seed set was chosen.
            await conn.execute(
                tasks_t.update()
                .where(tasks_t.c.id == "dep")
                .values(status=TaskStatus.COMPLETED.value)
            )
            flipped = await db.recompute_blocked_waves(
                {"nonexistent"}, conn=conn, extra_waves=[{"dep"}]
            )
        assert flipped == {"t"}
        assert await blocked(db, "t") is False

    async def test_started_tasks_are_left_alone(self, db):
        await mktask(db, "primary", status=TaskStatus.COMPLETED)
        await mktask(db, "contingency", status=TaskStatus.IN_PROGRESS)
        await db.add_dependency("contingency", "primary", DepType.CONDITIONAL_BLOCKS.value)
        assert await db.find_dead_conditional_tasks() == []


# ── Labels and the ready frontier ────────────────────────────────────────


class TestLabelsAndFrontier:
    async def test_frontier_is_ready_and_unblocked(self, db):
        await mktask(db, "ready", status=TaskStatus.READY)
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "blocked-ready", status=TaskStatus.READY)
        await db.add_dependency("blocked-ready", "dep")
        await mktask(db, "defined", status=TaskStatus.DEFINED)

        frontier = {t.id for t in await db.get_ready_frontier(PROJECT)}
        assert frontier == {"ready", "dep"}

    async def test_hold_label_withholds_from_the_frontier(self, db):
        await mktask(db, "a", status=TaskStatus.READY)
        await mktask(db, "b", status=TaskStatus.READY)
        await db.add_task_label("b", "hold:alice")

        frontier = {t.id for t in await db.get_ready_frontier(PROJECT)}
        assert frontier == {"a"}
        # ...but a held task still *exists* in listings.
        assert {t.id for t in await db.list_tasks(project_id=PROJECT)} == {"a", "b"}

    async def test_all_of_and_any_of_label_filters(self, db):
        await mktask(db, "a", status=TaskStatus.READY)
        await mktask(db, "b", status=TaskStatus.READY)
        await mktask(db, "c", status=TaskStatus.READY)
        await db.add_task_label("a", "x")
        await db.add_task_label("a", "y")
        await db.add_task_label("b", "x")
        await db.add_task_label("c", "z")

        assert {t.id for t in await db.list_tasks(project_id=PROJECT, labels=["x"])} == {"a", "b"}
        assert {t.id for t in await db.list_tasks(project_id=PROJECT, labels=["x", "y"])} == {"a"}
        assert {t.id for t in await db.list_tasks(project_id=PROJECT, any_label=["y", "z"])} == {
            "a",
            "c",
        }
        assert {t.id for t in await db.get_ready_frontier(PROJECT, labels=["x"])} == {"a", "b"}

    async def test_deleting_a_task_removes_its_labels(self, db):
        await mktask(db, "a", status=TaskStatus.READY)
        await db.add_task_label("a", "x")
        await db.delete_task("a")
        assert await db.get_task_labels("a") == []


# ── Cycle rules (design §11) ─────────────────────────────────────────────


class TestCycleRules:
    def test_blocking_edges_enter_the_dfs(self):
        deps = {"b": {"a"}}
        for dep_type in ("blocks", "parent-child", "waits-for", "conditional-blocks"):
            with pytest.raises(CyclicDependencyError):
                validate_dag_with_new_edge(deps, "a", "b", dep_type)

    def test_non_blocking_edges_are_exempt(self):
        deps = {"b": {"a"}}
        for dep_type in ("discovered-from", "related", "duplicates", "supersedes"):
            validate_dag_with_new_edge(deps, "a", "b", dep_type)  # no raise

    def test_self_edges_are_rejected_for_every_type(self):
        for dep_type in ("blocks", "related", "discovered-from"):
            with pytest.raises(CyclicDependencyError):
                validate_dag_with_new_edge({}, "a", "a", dep_type)

    def test_waits_for_rejects_a_descendant_waiter(self):
        # grandchild -> child -> container
        pc = {"child": {"container"}, "grandchild": {"child"}}
        with pytest.raises(CyclicDependencyError):
            validate_waits_for(pc, "grandchild", "container")
        with pytest.raises(CyclicDependencyError):
            validate_waits_for(pc, "child", "container")

    def test_waits_for_allows_an_outside_waiter(self):
        pc = {"child": {"container"}}
        validate_waits_for(pc, "finalize", "container")  # no raise

    def test_waits_for_rejects_self(self):
        with pytest.raises(CyclicDependencyError):
            validate_waits_for({}, "x", "x")

    def test_waits_for_terminates_on_a_parent_child_cycle(self):
        """Malformed data must not hang the ancestry walk."""
        pc = {"a": {"b"}, "b": {"a"}}
        validate_waits_for(pc, "a", "outside")  # no raise, no hang


# ── Property test: incremental recompute == brute force ──────────────────


class TestRecomputeProperty:
    @pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
    async def test_random_dag_mutations_match_full_evaluation(self, db, seed):
        """After every random mutation, the persisted projection must equal a
        full brute-force evaluation of the predicate over all tasks."""
        rng = random.Random(seed)
        statuses = list(TaskStatus)
        dep_types = [d.value for d in DepType]

        n = 25
        ids = [f"n{i:02d}" for i in range(n)]
        for i, tid in enumerate(ids):
            await mktask(db, tid, status=rng.choice(statuses), max_retries=3)

        edges: list[tuple[str, str, str]] = []
        for _ in range(60):
            action = rng.random()
            if action < 0.55 or not edges:
                # Add an edge, respecting the DAG order (i < j only).
                i, j = sorted(rng.sample(range(n), 2))
                edge = (ids[j], ids[i], rng.choice(dep_types))
                if edge in edges:
                    continue
                await db.add_dependency(*edge)
                edges.append(edge)
            elif action < 0.8:
                # Remove an edge.
                edge = edges.pop(rng.randrange(len(edges)))
                await db.remove_dependency(edge[0], edge[1], edge[2])
            else:
                # Change a status.
                tid = rng.choice(ids)
                from src.database.queries.task_queries import ManualPauseActive

                previous = await db.get_task(tid)
                try:
                    await db.transition_task(tid, rng.choice(statuses))
                except ManualPauseActive:
                    # Random automatic transitions cannot override a manual hold.
                    assert previous.status == TaskStatus.PAUSED
                    assert (await db.get_task(tid)).status == TaskStatus.PAUSED

            persisted = await db.get_blocked_map(ids)
            brute = await db.evaluate_blocked(ids)
            assert persisted == brute, f"drift after mutation (seed={seed})"

    async def test_recompute_all_repairs_drift(self, db):
        """A hand-corrupted row is repaired by the full recompute."""
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "t")
        await db.add_dependency("t", "dep")
        async with db._engine.begin() as conn:
            await conn.execute(tasks_t.update().where(tasks_t.c.id == "t").values(is_blocked=0))
        assert await db.recompute_all_blocked() == {"t"}
        assert await blocked(db, "t") is True


# ── The migration's SQL predicate matches the Python one ─────────────────


class TestMigrationPredicateParity:
    async def test_raw_sql_backfill_agrees_with_the_orm_predicate(self, db):
        """The Alembic revision embeds the predicate as literal SQL; it must
        produce the same answer as ``blocked_predicate()``."""
        from migrations.versions.a1c7f3e08b42_work_graph_is_blocked_backfill import (
            _BACKFILL_IS_BLOCKED,
        )

        await mktask(db, "done", status=TaskStatus.COMPLETED)
        await mktask(db, "open", status=TaskStatus.IN_PROGRESS)
        await mktask(db, "plan", status=TaskStatus.DEFINED)
        await mktask(db, "hard-fail", status=TaskStatus.FAILED, retry_count=3, max_retries=3)
        await mktask(db, "b1")
        await mktask(db, "b2")
        await mktask(db, "child")
        await mktask(db, "finalize")
        await mktask(db, "contingency")
        await db.add_dependency("b1", "done", DepType.BLOCKS.value)
        await db.add_dependency("b2", "open", DepType.BLOCKS.value)
        await db.add_dependency("child", "plan", DepType.PARENT_CHILD.value)
        await db.add_dependency("finalize", "plan", DepType.WAITS_FOR.value)
        await db.add_dependency("contingency", "hard-fail", DepType.CONDITIONAL_BLOCKS.value)
        await mkgate(db, "gate-x", status="open", waiters=["b1"])

        expected = await db.evaluate_blocked()

        # Zero the column, then let the migration's literal SQL rebuild it.
        async with db._engine.begin() as conn:
            await conn.execute(tasks_t.update().values(is_blocked=0))
            await conn.execute(text(_BACKFILL_IS_BLOCKED))
            rows = (await conn.execute(select(tasks_t.c.id, tasks_t.c.is_blocked))).fetchall()

        assert {r[0]: bool(r[1]) for r in rows} == expected

    async def test_predicate_expression_is_reusable_in_a_select(self, db):
        """``blocked_predicate()`` correlates against ``tasks`` and can be
        used outside the UPDATE it was written for."""
        await mktask(db, "dep", status=TaskStatus.READY)
        await mktask(db, "t")
        await db.add_dependency("t", "dep")
        async with db._engine.begin() as conn:
            rows = (await conn.execute(select(tasks_t.c.id).where(blocked_predicate()))).fetchall()
        assert {r[0] for r in rows} == {"t"}


def test_unique_ids_helper():
    """Guard against accidental id collisions in the property test."""
    assert len({uuid.uuid4().hex for _ in range(100)}) == 100
