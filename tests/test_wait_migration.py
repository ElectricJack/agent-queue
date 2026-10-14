"""Verified-wait DDL on fresh and pre-v2 disposable PostgreSQL schemas."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine

from src.database.tables import metadata
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database

ROOT = Path(__file__).resolve().parent.parent
REVISION = "a00000000097"
TABLES = (
    "wait_records",
    "wait_edges",
    "wait_source_events",
    "wait_listener_cursors",
    "wait_deadlines",
    "wait_escalations",
    "continuations",
    "admission_reservations",
    "wait_measurements",
    "wait_priority_projection",
)
CLAIM_INDEXES = (
    "idx_tasks_claim_frontier",
    "idx_tasks_claim_frontier_by_profile",
    "idx_tasks_claim_frontier_by_affinity",
)
JSONB_COLUMNS = {
    "wait_records": {"condition", "decision_inputs"},
    "wait_source_events": {"payload"},
    "continuations": {"facts"},
    "admission_reservations": {"scope"},
    "wait_priority_projection": {"donor_path"},
}


@pytest.fixture(autouse=True)
def _pg_backend():
    """Pure checks need no database; migration checks own scratch databases."""


@pytest.mark.parametrize("name", TABLES)
def test_wait_schema_registered_with_named_checks_and_jsonb(name):
    table = metadata.tables[name]
    assert all(
        check.name.startswith("ck_")
        for check in table.constraints
        if isinstance(check, sa.CheckConstraint)
    )
    assert {col.name for col in table.c if isinstance(col.type, sa.JSON)} == JSONB_COLUMNS.get(
        name, set()
    )
    assert all(isinstance(table.c[col].type, JSONB) for col in JSONB_COLUMNS.get(name, set()))


async def migrate(engine, target):
    def run(bind):
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = bind
        command.upgrade(config, target)

    async with engine.begin() as conn:
        await conn.run_sync(run)


def assert_schema(bind):
    inspector = sa.inspect(bind)
    for name in TABLES:
        assert inspector.has_table(name)
        table = metadata.tables[name]
        assert {col["name"] for col in inspector.get_columns(name)} == set(table.c.keys())
        assert {c["name"] for c in inspector.get_check_constraints(name)} == {
            c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)
        }
        assert {c["name"] for c in inspector.get_unique_constraints(name)} == {
            c.name for c in table.constraints if isinstance(c, sa.UniqueConstraint)
        }
        assert {
            idx["name"]
            for idx in inspector.get_indexes(name)
            if not idx.get("duplicates_constraint")
        } == {idx.name for idx in table.indexes}
        for col in inspector.get_columns(name):
            if col["name"] in JSONB_COLUMNS.get(name, set()):
                assert isinstance(col["type"], JSONB)
    indexes = {idx["name"]: idx for idx in inspector.get_indexes("tasks")}
    for name in CLAIM_INDEXES:
        assert indexes[name]["column_names"][-2:] == ["effective_priority", "created_at"]
        base = name.replace("idx_tasks_claim", "idx_tasks_base_claim")
        assert indexes[base]["column_names"] == [
            "priority" if col == "effective_priority" else col
            for col in indexes[name]["column_names"]
        ]
        assert indexes[base].get("dialect_options", {}) == indexes[name].get("dialect_options", {})
    assert not compare_metadata(
        MigrationContext.configure(bind, opts={"compare_type": True}), metadata
    )


def assert_unique_semantics(bind):
    waits = metadata.tables["wait_records"]
    base = dict(
        project_id="p",
        owner_kind="task",
        owner_id="t",
        origin="agent",
        condition_kind="task",
        condition={},
        source_key="task:t",
        source_generation=1,
        source_watermark_seq=0,
        verification_state="verified",
        evidence_ref="task:t",
        verified_at=1,
        created_at=1,
        request_hash="hash",
    )

    def rejected(statement):
        with pytest.raises(sa.exc.IntegrityError), bind.begin_nested():
            bind.execute(statement)

    bind.execute(waits.insert().values(id="wait", **base))
    rejected(waits.insert().values(id="duplicate-owner", **base))
    assert bind.execute(sa.select(waits.c.blocking, waits.c.version)).one() == (False, 1)
    bind.execute(waits.update().where(waits.c.id == "wait").values(outcome="satisfied"))
    bind.execute(waits.insert().values(id="next-wait", **base))
    for generation in (1, 2):
        bind.execute(
            waits.insert().values(
                id=f"scheduler-{generation}",
                **(base | {"origin": "scheduler", "source_generation": generation}),
            )
        )
    rejected(waits.insert().values(id="scheduler-duplicate", **(base | {"origin": "scheduler"})))
    rejected(
        waits.insert().values(
            id="invalid-kind", **(base | {"owner_id": "other", "condition_kind": "prose"})
        )
    )

    continuations = metadata.tables["continuations"]
    saved = dict(
        task_id="t",
        wait_id="wait",
        suspension_generation=1,
        facts={},
        branch="aq/t",
        prior_status="IN_PROGRESS",
        held_cursor_seq=0,
        resume_state="sleeping",
    )
    bind.execute(continuations.insert().values(id="continuation", summary="é" * 4096, **saved))
    rejected(
        continuations.insert().values(
            id="active-duplicate", **(saved | {"suspension_generation": 2})
        )
    )
    rejected(continuations.update().values(summary="é" * 4097))

    reservations = metadata.tables["admission_reservations"]
    slot = dict(
        project_id="p",
        scope={},
        effective_priority=20,
        provenance="resume",
        deadline_at=30,
        state="pending",
        created_at=1,
    )
    bind.execute(reservations.insert().values(id="task-reservation", task_id="t", **slot))
    rejected(reservations.insert().values(id="duplicate-task", task_id="t", **slot))
    bind.execute(
        reservations.insert().values(
            id="resume-reservation", continuation_id="continuation", **slot
        )
    )
    rejected(
        reservations.insert().values(id="duplicate-resume", continuation_id="continuation", **slot)
    )
    bind.execute(reservations.update().values(state="consumed"))
    bind.execute(
        reservations.insert().values(id="next-resume", continuation_id="continuation", **slot)
    )
    fifo = list(
        bind.execute(sa.select(reservations.c.fifo_seq).order_by(reservations.c.fifo_seq)).scalars()
    )
    assert len(set(fifo)) == 3 and fifo == sorted(fifo)

    events = metadata.tables["wait_source_events"]
    event = dict(
        source_key="task:t",
        source_generation=1,
        source_revision="r1",
        occurred_at=1,
        committed_at=1,
        outcome_ref="task:t",
        payload_hash="hash",
        payload={},
    )
    seq = bind.execute(
        events.insert().values(id="event", **event).returning(events.c.seq)
    ).scalar_one()
    rejected(events.insert().values(id="duplicate-revision", **event))
    next_seq = bind.execute(
        events.insert()
        .values(id="next-event", **(event | {"source_revision": "r2"}))
        .returning(events.c.seq)
    ).scalar_one()
    assert next_seq > seq


@pytest.mark.migration
@pytest.mark.integration
@pytest.mark.parametrize("legacy", [True, False], ids=["pre-v2", "fresh-baseline"])
async def test_upgrade_twice_repairs_indexes_preserves_data_and_retains_downgrade(legacy):
    engine = create_async_engine(await create_scratch_database("waitmigration"))
    revision = import_module("migrations.versions.a00000000097_wait_records")
    compatibility = import_module("migrations.versions.a00000000098_base_claim_frontier_indexes")
    predecessor = import_module("migrations.versions.a00000000096_workspace_kind_limits")
    try:
        await migrate(engine, previous_revision(previous_revision(REVISION)))
        async with engine.begin() as conn:
            if legacy:
                await conn.execute(
                    sa.text("ALTER TABLE workspace_kinds DROP CONSTRAINT ck_workspace_kinds_max_slots")
                )
                await conn.execute(sa.text("ALTER TABLE workspace_kinds DROP COLUMN max_slots"))
                for name in reversed(TABLES):
                    await conn.execute(sa.text(f'DROP TABLE "{name}"'))
                await conn.execute(sa.text("ALTER TABLE tasks DROP COLUMN effective_priority"))
                for index in metadata.tables["tasks"].indexes:
                    if index.name in CLAIM_INDEXES:
                        columns = [
                            "priority" if col.name == "effective_priority" else col.name
                            for col in index.columns
                        ]
                        await conn.run_sync(
                            lambda bind, idx=index, cols=columns: Operations(
                                MigrationContext.configure(bind)
                            ).create_index(
                                idx.name,
                                "tasks",
                                cols,
                                postgresql_where=idx.dialect_options["postgresql"].get("where"),
                            )
                        )
            await conn.execute(
                sa.text("INSERT INTO projects (id,name,created_at) VALUES ('p','P',0)")
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO tasks (id,project_id,title,description,priority,created_at,updated_at) "
                    "VALUES ('t','p','Keep','Keep',20,0,0)"
                )
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO workspace_kinds (project_id,id,description,created_at,updated_at) "
                    "VALUES ('p','keep-kind','Keep kind',0,0)"
                )
            )
        await migrate(engine, "head")
        await migrate(engine, "head")

        def verify(bind):
            assert_schema(bind)
            assert bind.execute(
                sa.text("SELECT description, max_slots FROM workspace_kinds WHERE id='keep-kind'")
            ).one() == ("Keep kind", None)
            with pytest.raises(sa.exc.IntegrityError), bind.begin_nested():
                bind.execute(sa.text("UPDATE workspace_kinds SET max_slots=0"))
            bind.execute(sa.text("UPDATE workspace_kinds SET max_slots=3"))
            with Operations.context(MigrationContext.configure(bind)):
                predecessor.upgrade()
                predecessor.upgrade()
            assert bind.execute(
                sa.text("SELECT max_slots FROM workspace_kinds WHERE id='keep-kind'")
            ).scalar_one() == 3
            assert bind.execute(
                sa.text("SELECT title, priority, effective_priority FROM tasks")
            ).one() == ("Keep", 20, 20 if legacy else 100)
            bind.execute(sa.text("UPDATE tasks SET effective_priority = 7"))
            oids = dict(
                bind.execute(
                    sa.text("SELECT relname, oid FROM pg_class WHERE relname = ANY(:names)"),
                    {"names": list(TABLES)},
                ).all()
            )
            with Operations.context(MigrationContext.configure(bind)):
                revision.upgrade()
                revision.upgrade()
                compatibility.upgrade()
                compatibility.upgrade()
                # Repair missing compatibility indexes without rewriting rows.
                for name in CLAIM_INDEXES:
                    Operations(MigrationContext.configure(bind)).drop_index(
                        name.replace("idx_tasks_claim", "idx_tasks_base_claim"), table_name="tasks"
                    )
                compatibility.upgrade()
                # Missing indexes on existing tables must also be repaired.
                for name in TABLES:
                    for index in metadata.tables[name].indexes:
                        Operations(MigrationContext.configure(bind)).drop_index(
                            index.name, table_name=name
                        )
                revision.upgrade()
                assert_schema(bind)
                assert_unique_semantics(bind)
                revision.downgrade()
                compatibility.downgrade()
            assert_schema(bind)
            assert bind.execute(sa.text("SELECT effective_priority FROM tasks")).scalar_one() == 7
            assert (
                dict(
                    bind.execute(
                        sa.text("SELECT relname, oid FROM pg_class WHERE relname = ANY(:names)"),
                        {"names": list(TABLES)},
                    ).all()
                )
                == oids
            )
            assert (
                bind.execute(sa.text("SELECT summary FROM continuations")).scalar_one()
                == "é" * 4096
            )

        async with engine.begin() as conn:
            await conn.run_sync(verify)
    finally:
        await engine.dispose()
