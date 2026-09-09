"""Playbook V2 schema and partial-index contracts at the current baseline."""

import pytest
import sqlalchemy as sa

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration

V2_TABLES = (
    "playbook_artifacts",
    "playbook_activations",
    "playbook_v2_runs",
    "playbook_step_receipts",
    "playbook_waits",
    "playbook_pending_events",
)

V1_TABLES = {
    "playbook_runs",
    "playbook_cutover_events",
    "playbook_migration_acks",
}

#: Every non-primary-key index they create, keyed by its table.
V2_INDEXES = {
    "playbook_artifacts": {
        "idx_playbook_artifacts_playbook",
        "idx_playbook_artifacts_source",
        "idx_playbook_artifacts_created",
    },
    "playbook_activations": {"idx_playbook_activations_health"},
    "playbook_v2_runs": {
        "uq_playbook_v2_runs_dispatch_rule",
        "idx_playbook_v2_runs_playbook",
        "idx_playbook_v2_runs_lifecycle",
        "idx_playbook_v2_runs_artifact",
    },
    "playbook_step_receipts": {
        "idx_playbook_step_receipts_run",
        "idx_playbook_step_receipts_key",
        "idx_playbook_step_receipts_turn",
    },
    "playbook_waits": {
        "uq_playbook_waits_active_step",
        "idx_playbook_waits_match",
        "idx_playbook_waits_deadline",
    },
    "playbook_pending_events": {
        "uq_playbook_pending_events_dedup",
        "idx_playbook_pending_events_playbook",
        "idx_playbook_pending_events_expiry",
    },
}

#: The three partial (filtered) indexes and the predicate each must carry on
#: both dialects.  A partial index that silently loses its ``WHERE`` becomes a
#: total uniqueness constraint and rejects perfectly legal rows.
PARTIAL_INDEXES = {
    "uq_playbook_v2_runs_dispatch_rule": "dispatch_id IS NOT NULL",
    "uq_playbook_waits_active_step": "state = 'active'",
    "uq_playbook_pending_events_dedup": "resolved_at IS NULL AND dedup_key <> ''",
}


def _assert_schema(conn):
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())
    assert set(V2_TABLES) <= tables
    assert not V1_TABLES & tables
    assert not {"reviewed_artifact_sha256", "reviewed_by", "reviewed_at"} & {
        column["name"] for column in inspector.get_columns("playbook_activations")
    }
    for table in ("task_assignment_routes", "workflows"):
        assert any(
            fk["referred_table"] == "playbook_v2_runs"
            and fk["constrained_columns"] == ["playbook_run_id"]
            for fk in inspector.get_foreign_keys(table)
        )
    for table, expected in V2_INDEXES.items():
        found = {index["name"] for index in inspector.get_indexes(table)}
        assert expected <= found, f"{table} missing {expected - found}"
    assert {"dispatch_claim_token", "dispatch_claimed_by", "dispatch_claimed_at"} <= {
        column["name"] for column in inspector.get_columns("playbook_pending_events")
    }
    assert "ck_playbook_pending_events_dispatch_claim" in {
        constraint["name"]
        for constraint in inspector.get_check_constraints("playbook_pending_events")
    }
    columns = {column["name"]: column for column in inspector.get_columns("playbook_step_receipts")}
    assert columns["operator_decision_id"]["nullable"]
    unique = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("playbook_step_receipts")
    }
    assert unique["uq_playbook_step_receipts_boundary"] == (
        "run_id",
        "step_id",
        "iteration",
        "attempt",
        "turn_index",
        "receipt_kind",
    )
    defs = dict(
        conn.execute(
            sa.text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public'")
        ).all()
    )
    for name in PARTIAL_INDEXES:
        assert "WHERE" in defs[name].upper(), f"{name} lost its predicate"
    assert "(playbook_id, dispatch_id, rule_id)" in defs["uq_playbook_v2_runs_dispatch_rule"]


async def test_baseline_playbook_schema():
    database = Database(lease_dsn("playbook-schema"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            await conn.run_sync(_assert_schema)
    finally:
        await database.close()
