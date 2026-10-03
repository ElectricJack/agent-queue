"""Tests for the SQLite→PostgreSQL migration table ordering.

The migration copies data table-by-table from ``_ORDERED_TABLES``.  A table
missing from that list is silently dropped data for anyone switching an
existing SQLite install to PostgreSQL, so these tests pin the list against
``tables.metadata`` and against the FK graph.
"""

from __future__ import annotations

import pytest

from src.database.legacy_sqlite_import import _DEFERRED_COLS, _EXCLUDED_TABLES, _ORDERED_TABLES
from src.database.tables import metadata
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn()

_REPORT_REQUEST_ROW = {
    "id": "report-hourly-window",
    "kind": "hourly",
    "owner_ref": "window",
    "destination": "discord:channel",
    "visibility": {"project_ids": ["x"], "full_fleet": False},
    "brief": {"title": "Hourly activity", "counts": {"completed": 2}},
    "brief_hash": "brief-hash",
    "fallback_text": "Two tasks completed.",
    "author_session_id": "supervisor-session",
    "state": "submitted",
    "deadline": 3600.0,
    "version": 3,
    "request_message_id": "msg-report-hourly-window",
    "submitted_text": "Completed two tasks with verified results.",
    "submitted_hash": "submitted-hash",
    "evidence_refs": ["task:p", "task:c"],
    "source_links": ["https://example.com/results"],
    "submitted_at": 120.0,
    "skip_reason": None,
    "created_at": 0.0,
    "updated_at": 120.0,
}

# Durable state must travel together: results keep their receipts and pins,
# reports keep their evidence and coverage, and waits keep their result pointers.
_DURABLE_ROWS = {
    "jobs": {
        "id": "job", "project_id": "x", "task_id": "p",
        "owner_kind": "task", "owner_id": "p", "claim_epoch": 1,
        "idempotency_key": "validation", "request_hash": "request",
        "preset": "test", "preset_version": 1, "argv": ["pytest", "tests/test_x.py"],
        "contract": {"timeout": 60}, "workspace_id": "workspace",
        "workspace_generation": 2, "input_mode": "live", "job_class": "shared",
        "weight": 1, "priority_band": 1, "state": "lost", "submitted_at": 0.0,
        "queue_deadline": 60.0, "run_timeout": 60.0, "runner_nonce": "nonce",
        "cleanup_blocked": True, "result_version": 1, "result_ref": "job:job:result",
        "result": {"outcome": "lost", "reason": "missing receipt"},
    },
    "job_outbox": {
        "key": "job:job:terminal", "job_id": "job", "created_at": 30.0,
        "payload": {"result_ref": "job:job:result"}, "delivered_at": None,
    },
    "job_workspace_pins": {
        "job_id": "job", "workspace_id": "workspace", "generation": 2,
        "created_at": 0.0,
    },
    "agent_waits": {
        "id": "wait", "project_id": "x", "owner_kind": "task", "owner_id": "p",
        "session_id": "session", "session_instance_token": "instance", "claim_epoch": 1,
        "kind": "job", "match": {"job_id": "job"}, "state": "satisfied",
        "created_at": 0.0, "deadline_at": 60.0, "resolved_at": 30.0,
        "result_ref": "job:job:result", "digest": {"outcome": "lost"},
        "idempotency_key": "wait-validation", "result_message_id": "wait-result",
    },
    "morning_reports": {
        "id": "morning", "schedule_id": "daily", "local_date": "2026-09-26",
        "config_snapshot": {"project_ids": ["x"]}, "scope_key": "scope",
        "timezone": "UTC", "planned_at": 120.0, "window_start": 0.0,
        "window_end": 120.0, "build_context": {"source_heads": {"x": "sha"}},
        "state": "final", "report": {"summary": "Verified work"},
        "coverage": {"complete": True}, "author_deadline": 180.0,
        "created_at": 120.0, "finalized_at": 150.0,
    },
    "morning_report_coverage": {
        "schedule_id": "daily", "scope_key": "scope", "source": "git",
        "covered_until": 120.0, "head_sha": "sha", "report_id": "morning",
    },
    "morning_report_facts": {
        "report_id": "morning", "fact_key": "git:sha", "source": "git",
        "record_id": "sha", "project_id": "x", "at": 100.0,
    },
    "outbound_deliveries": {
        "id": "delivery", "owner_kind": "morning", "owner_id": "morning",
        "dedup_key": "morning:morning", "destination": {"channel_id": "channel"},
        "payload": {"text": "Verified work"}, "payload_hash": "hash", "marker": "marker",
        "state": "sent", "due_at": 150.0, "attempt_count": 1,
        "external_receipt_id": "receipt", "receipt_confirmed_at": 151.0,
        "created_at": 150.0, "updated_at": 151.0,
    },
}

# The legacy import still ships, but creating and copying a source schema is
# migration work, not part of the default developer/CI run.
pytestmark = pytest.mark.migration


def test_ordered_tables_covers_every_table() -> None:
    """Every schema table is imported or has a documented exclusion."""
    listed = {t.name for t in _ORDERED_TABLES}
    defined = {t.name for t in metadata.tables.values()}

    assert not defined - listed - _EXCLUDED_TABLES, (
        "tables missing from _ORDERED_TABLES and _EXCLUDED_TABLES (their data "
        "would be silently dropped by the SQLite→Postgres migration): "
        f"{sorted(defined - listed - _EXCLUDED_TABLES)}"
    )
    assert not listed - defined, f"_ORDERED_TABLES lists unknown tables: {sorted(listed - defined)}"
    assert not _EXCLUDED_TABLES - defined, (
        f"_EXCLUDED_TABLES lists unknown tables: {sorted(_EXCLUDED_TABLES - defined)}"
    )
    assert not listed & _EXCLUDED_TABLES, (
        f"tables cannot be both imported and excluded: {sorted(listed & _EXCLUDED_TABLES)}"
    )
    assert set(_ORDERED_TABLES) == {
        table for table in metadata.tables.values() if table.name not in _EXCLUDED_TABLES
    }


def test_ordered_tables_has_no_duplicates() -> None:
    names = [t.name for t in _ORDERED_TABLES]
    assert len(names) == len(set(names)), "duplicate entries in _ORDERED_TABLES"


def test_insertion_order_is_fk_safe() -> None:
    """Each table's FK targets are inserted earlier, or the column is deferred."""
    position = {t.name: i for i, t in enumerate(_ORDERED_TABLES)}
    # epic_dependencies uses soft task references, so the FK walk cannot check it.
    assert position["epic_dependencies"] > position["tasks"]

    violations = []
    for table in _ORDERED_TABLES:
        deferred = _DEFERRED_COLS.get(table.name, frozenset())
        for fk in table.foreign_keys:
            if fk.parent.name in deferred:
                continue
            target = fk.column.table.name
            if position[target] >= position[table.name]:
                violations.append(f"{table.name}.{fk.parent.name} -> {target}")

    assert not violations, (
        "FK targets inserted at or after the referencing table; either reorder "
        f"_ORDERED_TABLES or add the column to _DEFERRED_COLS: {sorted(violations)}"
    )


@pytest.mark.parametrize("table_name", sorted(_DEFERRED_COLS))
def test_deferred_columns_exist_and_are_nullable(table_name: str) -> None:
    """Deferred columns must exist and accept NULL on the first insert pass."""
    table = metadata.tables[table_name]
    for col_name in _DEFERRED_COLS[table_name]:
        assert col_name in table.c, f"{table_name}.{col_name} is not a column"
        assert table.c[col_name].nullable, (
            f"{table_name}.{col_name} is NOT NULL and cannot be deferred"
        )


def test_deferred_tables_have_a_primary_key() -> None:
    """The fixup pass updates rows by PK, so every deferred table needs one."""
    for table_name in _DEFERRED_COLS:
        table = metadata.tables[table_name]
        assert list(table.primary_key.columns), f"{table_name} has no primary key"


async def _empty_pg_adapter():
    """The worker's Postgres database, truncated to the empty state the
    migration requires of its target.  Caller closes it."""
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.records.schema import install_record_guards_v1

    adapter = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
    await adapter.initialize()
    await adapter.reset_for_tests()
    # Reset removes the seed too; restore the immutable installation identity
    # a freshly migrated operator target retains before any legacy import.
    async with adapter._engine.begin() as conn:
        await conn.run_sync(install_record_guards_v1)
    return adapter


async def _seeded_source(tmp_path) -> str:
    """A synthetic legacy SQLite source with rows across the deferred-FK tables:
    a self-FK parent pointer (tasks) and the agents⇄tasks circular FK."""
    from sqlalchemy import JSON, MetaData, insert, text
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.database.tables import supervisor_report_requests

    # The legacy source predates PostgreSQL-only tables and timestamp defaults.
    # Keep production metadata intact while recreating its importable columns.
    source_metadata = MetaData()
    for table in metadata.tables.values():
        if table.name not in _EXCLUDED_TABLES:
            table.to_metadata(source_metadata)
    source_metadata.tables["task_context"].c.created_at.server_default = None
    # PostgreSQL columns such as tasks.route (revision 40) became JSONB;
    # legacy SQLite stored JSON, and SQLAlchemy 2.0 cannot render JSONB on
    # SQLite. Keep each column's SQL NULL versus JSON null semantics.
    for table in source_metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON(none_as_null=column.type.none_as_null)
    # Archived route provenance was introduced by PostgreSQL revision 47;
    # legacy sources lack it and the importer must leave the new column NULL.
    archived = source_metadata.tables["archived_tasks"]
    archived._columns.remove(archived.c.route)
    ledger = source_metadata.tables["token_ledger"]
    ledger.indexes = {
        index for index in ledger.indexes
        if index.name not in {
            "idx_token_ledger_task_attempt", "uq_token_ledger_call", "idx_token_ledger_call_id",
        }
    }
    for name in ("session_id", "attempt_id", "call_id", "model_source"):
        ledger._columns.remove(ledger.c[name])

    path = str(tmp_path / "source.db")
    source = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with source.begin() as conn:
        await conn.run_sync(source_metadata.create_all)
        await conn.execute(text("INSERT INTO projects (id, name, created_at) VALUES ('x','x',0)"))
        await conn.execute(
            text("INSERT INTO agent_profiles (id, name, created_at, updated_at) "
                 "VALUES ('worker','Worker',0,0)")
        )
        await conn.execute(
            text(
                "INSERT INTO tasks (id, project_id, parent_task_id, title, description, "
                "status, created_at, updated_at) VALUES "
                "('p','x',NULL,'p','p','IN_PROGRESS',0,0), ('c','x','p','c','c','READY',0,0)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, name, profile_id, current_task_id, created_at) "
                "VALUES ('a','a','worker','p',0)"
            )
        )
        await conn.execute(text(
            "INSERT INTO token_ledger (id, project_id, agent_id, task_id, tokens_used, timestamp) "
            "VALUES ('legacy-call', 'x', 'a', 'p', 12, 0)"
        ))
        await conn.execute(text(
            "INSERT INTO archived_tasks "
            "(id, project_id, title, description, status, created_at, updated_at, archived_at) "
            "VALUES ('legacy-archive', 'x', 'old', 'old', 'COMPLETED', 0, 0, 0)"
        ))
        await conn.execute(
            text(
                "INSERT INTO epic_dependencies "
                "(dependent_task_id, dependency_task_id, declared_at) "
                "VALUES ('c','p',0)"
            )
        )
        await conn.execute(insert(supervisor_report_requests), _REPORT_REQUEST_ROW)
        await conn.execute(
            insert(source_metadata.tables["workspaces"]),
            {"id": "workspace", "project_id": "x", "workspace_path": "/workspace",
             "generation": 2, "job_pin_count": 1, "created_at": 0.0},
        )
        for table_name, row in _DURABLE_ROWS.items():
            await conn.execute(insert(source_metadata.tables[table_name]), row)
        # playbook_activations -> playbook_artifacts is a plain (non-deferred)
        # FK, so the copy only works if both tables are in _ORDERED_TABLES and
        # the artifact is inserted first.
        await conn.execute(
            text(
                "INSERT INTO playbook_artifacts (artifact_sha256, playbook_id, scope, "
                "source_digest, contract_fingerprint, compiler_build, path, created_at) "
                "VALUES ('sha','pb','system','src','contract','build','/pb.json',0)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO playbook_activations (activation_id, playbook_id, scope, "
                "health, active_artifact_sha256, updated_at) "
                "VALUES ('act','pb','system','ready','sha',0)"
            )
        )
        for i in (1, 2, 3):
            await conn.execute(
                text(
                    "INSERT INTO hierarchy_migration_rejects "
                    "(id, run_id, task_id, source, reason, detail, created_at) "
                    f"VALUES ({i}, 'run', 't{i}', 'edge', 'cycle', '', 0)"
                )
            )
    await source.dispose()
    return path


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_copies_rows_and_restores_deferred_fks(tmp_path) -> None:
    """Rows land, and the NULLed-on-insert deferred columns are restored.

    ``tasks.parent_task_id`` (self-FK) and ``agents.current_task_id``
    (agents⇄tasks circular FK) are inserted as NULL in pass one; pass two
    must put the source values back.
    """
    from sqlalchemy import select, text

    from src.database.legacy_sqlite_import import migrate_sqlite_to_postgres
    from src.database.tables import record_installation, supervisor_report_requests
    from src.records.schema import RECORD_TABLE_NAMES

    path = await _seeded_source(tmp_path)
    target = await _empty_pg_adapter()
    try:
        async with target._engine.connect() as conn:
            installation_id = await conn.scalar(select(record_installation.c.installation_id))
            assert installation_id is not None
        counts = await migrate_sqlite_to_postgres(path, POSTGRES_DSN)
        assert set(counts) == {table.name for table in _ORDERED_TABLES}
        assert counts["tasks"] == 2 and counts["agents"] == 1
        assert counts["epic_dependencies"] == 1
        assert counts["supervisor_report_requests"] == 1
        async with target._engine.connect() as conn:
            assert await conn.scalar(select(record_installation.c.installation_id)) == installation_id
            for table_name in RECORD_TABLE_NAMES:
                if table_name != "record_installation":
                    assert not (await conn.execute(select(metadata.tables[table_name]))).first()
            ledger = (await conn.execute(select(metadata.tables["token_ledger"]))).mappings().one()
            assert ledger["tokens_used"] == 12
            assert all(ledger[name] is None for name in (
                "session_id", "attempt_id", "call_id", "model_source",
            ))
            archived = (
                await conn.execute(select(metadata.tables["archived_tasks"]))
            ).mappings().one()
            assert archived["id"] == "legacy-archive" and archived["route"] is None
            for table_name, expected in _DURABLE_ROWS.items():
                assert counts[table_name] == 1
                actual = (
                    await conn.execute(select(metadata.tables[table_name]))
                ).mappings().one()
                assert {key: actual[key] for key in expected} == expected, table_name
            assert (
                await conn.execute(text("SELECT job_pin_count FROM workspaces"))
            ).scalar() == 1
            report = (
                await conn.execute(select(supervisor_report_requests))
            ).mappings().one()
            assert dict(report) == _REPORT_REQUEST_ROW
            assert (
                await conn.execute(text("SELECT parent_task_id FROM tasks WHERE id='c'"))
            ).scalar() == "p"
            assert (
                await conn.execute(text("SELECT current_task_id FROM agents WHERE id='a'"))
            ).scalar() == "p"
            assert (
                await conn.execute(
                    text(
                        "SELECT dependency_task_id FROM epic_dependencies "
                        "WHERE dependent_task_id='c'"
                    )
                )
            ).scalar() == "p"
            assert (
                await conn.execute(
                    text(
                        "SELECT active_artifact_sha256 FROM playbook_activations "
                        "WHERE activation_id='act'"
                    )
                )
            ).scalar() == "sha"
        await target.reset_for_tests()
    finally:
        await target.close()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_resets_postgres_sequences(tmp_path) -> None:
    """After copying explicit integer PKs, a plain insert must not collide.

    The copied rows carry ids 1..3; without ``setval`` the sequence would
    hand out 1 again and the next default-id insert would violate the PK.
    """
    from sqlalchemy import text

    from src.database.legacy_sqlite_import import migrate_sqlite_to_postgres

    path = await _seeded_source(tmp_path)
    target = await _empty_pg_adapter()
    try:
        await migrate_sqlite_to_postgres(path, POSTGRES_DSN)
        async with target._engine.begin() as conn:
            new_id = (
                await conn.execute(
                    text(
                        "INSERT INTO hierarchy_migration_rejects "
                        "(run_id, task_id, source, reason, detail, created_at) "
                        "VALUES ('run', 't4', 'edge', 'cycle', '', 0) RETURNING id"
                    )
                )
            ).scalar()
        assert new_id == 4
        await target.reset_for_tests()
    finally:
        await target.close()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
@pytest.mark.parametrize("existing_table", ["projects", "record_scopes"])
async def test_migrate_sqlite_to_postgres_rejects_nonempty_target_without_copying(
    tmp_path, existing_table,
) -> None:
    from sqlalchemy import text

    from src.database.legacy_sqlite_import import migrate_sqlite_to_postgres

    path = await _seeded_source(tmp_path)
    target = await _empty_pg_adapter()
    try:
        async with target._engine.begin() as conn:
            if existing_table == "projects":
                await conn.execute(
                    text("INSERT INTO projects (id, name, created_at) VALUES ('existing','e',0)")
                )
            else:
                await conn.execute(text(
                    "INSERT INTO record_scopes (scope_key, scope_kind) VALUES ('global','global')"
                ))
        with pytest.raises(RuntimeError, match="already contains data"):
            await migrate_sqlite_to_postgres(path, POSTGRES_DSN)
        async with target._engine.connect() as conn:
            rows = (await conn.execute(text("SELECT id FROM projects"))).scalars().all()
            assert rows == (["existing"] if existing_table == "projects" else [])
            if existing_table == "record_scopes":
                assert (
                    await conn.execute(text("SELECT scope_key FROM record_scopes"))
                ).scalars().all() == ["global"]
            assert (
                await conn.execute(text("SELECT COUNT(*) FROM tasks"))
            ).scalar() == 0
        await target.reset_for_tests()
    finally:
        await target.close()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_migrate_sqlite_to_postgres_reports_per_table_progress_in_order(tmp_path) -> None:
    from src.database.legacy_sqlite_import import migrate_sqlite_to_postgres

    path = await _seeded_source(tmp_path)
    target = await _empty_pg_adapter()
    try:
        calls: list[tuple[str, int]] = []
        counts = await migrate_sqlite_to_postgres(
            path, POSTGRES_DSN, progress_cb=lambda name, n: calls.append((name, n))
        )
        assert [name for name, _ in calls] == [table.name for table in _ORDERED_TABLES]
        assert dict(calls) == counts
        await target.reset_for_tests()
    finally:
        await target.close()
