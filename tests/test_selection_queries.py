"""Selection history, appended observations and promotion fences on PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.database import Database
from src.models import Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn, seed_task_session_attempt


@pytest.fixture
async def db():
    database = Database(lease_dsn("test-selection-queries"))
    await database.initialize()
    try:
        await database.create_project(Project(id="p", name="Project"))
        await seed_task_session_attempt(database, project_id="p", task_id="t", agent_id=None)
        yield database
    finally:
        await database.close()


def selection_values(**overrides) -> dict:
    values = {
        "project_id": "p",
        "task_id": "t",
        "session_id": "session-unowned-t",
        "claim_epoch": 1,
        "mode": "shadow",
        "workspace": "/w",
        "base_ref": "origin/main",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "dirty_fingerprint": "dirty-hash",
        "snapshot_fingerprint": "snapshot-hash",
        "snapshot_complete": True,
        "incomplete_reason": None,
        "catalogue_digest": "catalogue-hash",
        "rules_digest": "rules-hash",
        "policy_digest": "policy-hash",
        "question_schema_version": 1,
        "static_engine": "pytest-impacted@0.30.0",
        "marker_policy": "default",
        "cache_key": "cache-hash",
        "jev_requested_model": "jev-1.13.0",
        "jev_returned_model": "jev-1.13.0",
        "jev_status": "ok",
        "fallback_reason": None,
        "full_required": False,
        "jev_used_for_omission": False,
        "promotion_id": None,
        "area_decisions": {"database": {"choice": "unknown", "confidence": 0.9}},
        "mandatory_modules": ["tests/test_selection_queries.py"],
        "static_modules": ["tests/test_database.py"],
        "jev_modules": ["tests/test_models.py"],
        "fallback_modules": ["tests/test_selection_queries.py", "tests/test_database.py"],
        "final_modules": ["tests/test_selection_queries.py", "tests/test_models.py"],
        "reasons": {"tests/test_selection_queries.py": ["mandatory_changed_test"]},
        "pending_obligations": [{"command": "aq test -m migration", "state": "pending"}],
        "argv": ["tests/test_selection_queries.py", "tests/test_models.py"],
        "elapsed_ms": {"snapshot": 15.25, "jev": 80},
        "usage": {"input_tokens": 42, "output_tokens": 5},
    }
    values.update(overrides)
    return values


def promotion_values(**overrides) -> dict:
    values = {
        "project_id": "p",
        "model": "jev-1.13.0",
        "question_schema_version": 1,
        "catalogue_digest": "catalogue-hash",
        "rules_digest": "rules-hash",
        "policy_digest": "policy-hash",
        "evidence": {"held_out_red_commits": 30, "recall": 1.0},
        "promoted_by": "operator",
        "promoted_at": 10.0,
    }
    values.update(overrides)
    return values


async def observe(db, selection_id, *, observed_at=10.0, kind="execution"):
    return await db.append_test_selection_observation(
        selection_id=selection_id,
        kind=kind,
        source="worker",
        exit_code=1,
        duration_ms=123,
        executed_modules=["tests/test_selection_queries.py"],
        failed_node_ids=["tests/test_selection_queries.py::test_example"],
        payload={"run_id": "run-1"},
        observed_at=observed_at,
    )


async def test_insert_get_and_json_round_trip_without_mutating_input(db):
    values = selection_values()
    row = await db.insert_test_selection(values)
    assert row["id"].startswith("tsel-")
    assert isinstance(row["created_at"], float)
    assert {key: row[key] for key in values} == values
    assert "id" not in values and "created_at" not in values
    assert await db.get_test_selection(row["id"]) == row
    assert await db.get_test_selection("unknown") is None


async def test_insert_never_updates_existing_record(db):
    values = selection_values(id="tsel-fixed", created_at=1.0)
    first = await db.insert_test_selection(values)
    second = await db.insert_test_selection(selection_values(mode="plan_only"))
    assert second["id"] != first["id"]
    with pytest.raises(IntegrityError):
        await db.insert_test_selection({**values, "mode": "enforce"})
    assert await db.get_test_selection(first["id"]) == first
    assert not hasattr(db, "update_test_selection")
    assert not hasattr(db, "update_test_selection_observation")


async def test_list_scopes_filters_limits_and_before_cursor(db):
    await db.create_project(Project(id="q", name="Other project"))
    old = await db.insert_test_selection(selection_values(created_at=1.0))
    recent = await db.insert_test_selection(selection_values(created_at=3.0))
    taskless = await db.insert_test_selection(selection_values(task_id=None, created_at=2.0))
    await db.insert_test_selection(selection_values(project_id="q", task_id=None, created_at=4.0))
    assert await db.list_test_selections(project_id="p") == [recent, taskless, old]
    assert await db.list_test_selections(project_id="p", task_id="t") == [recent, old]
    assert await db.list_test_selections(project_id="p", limit=1) == [recent]
    assert await db.list_test_selections(project_id="p", before=2.0) == [old]
    assert await db.list_test_selections(project_id="p", limit=0) == []


async def test_observations_append_in_time_order_and_require_selection(db):
    selection = await db.insert_test_selection(selection_values())
    later = await observe(db, selection["id"], observed_at=20.0, kind="ci")
    earlier = await observe(db, selection["id"], observed_at=10.0, kind="replay")
    assert later["id"].startswith("tsobs-")
    assert later["id"] != earlier["id"]
    assert later["failed_node_ids"] == ["tests/test_selection_queries.py::test_example"]
    assert later["payload"] == {"run_id": "run-1"}
    assert later["exit_code"] == 1 and later["duration_ms"] == 123
    assert await db.list_test_selection_observations(selection["id"]) == [earlier, later]
    assert await db.get_test_selection(selection["id"]) == selection
    assert await db.list_test_selection_observations("unknown") == []
    with pytest.raises(LookupError):
        await observe(db, "unknown")


async def test_promotion_one_active_per_project_and_revoke_once(db):
    from src.database.queries.test_selection_queries import PromotionActive

    assert await db.active_test_selection_promotion(project_id="p") is None
    values = promotion_values()
    first = await db.insert_test_selection_promotion(values)
    assert first["id"].startswith("tsprom-")
    assert "id" not in values
    assert {key: first[key] for key in values} == values
    assert first["revoked_at"] is None
    assert await db.active_test_selection_promotion(project_id="p") == first
    with pytest.raises(PromotionActive):
        await db.insert_test_selection_promotion(promotion_values())
    await db.create_project(Project(id="q", name="Other project"))
    other = await db.insert_test_selection_promotion(promotion_values(project_id="q"))
    assert await db.revoke_test_selection_promotion(first["id"], now=11.0, reason="critical miss")
    assert not await db.revoke_test_selection_promotion(first["id"], now=12.0, reason="again")
    assert not await db.revoke_test_selection_promotion("unknown", now=12.0, reason="absent")
    from src.database.tables import test_selection_promotions

    async with db._engine.connect() as conn:
        revoked = (
            await conn.execute(
                select(test_selection_promotions).where(test_selection_promotions.c.id == first["id"])
            )
        ).mappings().one()
    assert revoked["revoked_at"] == 11.0 and revoked["revoke_reason"] == "critical miss"
    assert {key: revoked[key] for key in first if key not in {"revoked_at", "revoke_reason"}} == {
        key: first[key] for key in first if key not in {"revoked_at", "revoke_reason"}
    }
    assert await db.active_test_selection_promotion(project_id="p") is None
    assert await db.active_test_selection_promotion(project_id="q") == other
    new = await db.insert_test_selection_promotion(promotion_values(promoted_at=13.0))
    assert new["id"] != first["id"]
    assert await db.active_test_selection_promotion(project_id="p") == new


async def test_concurrent_promotions_have_one_winner(db):
    from src.database.queries.test_selection_queries import PromotionActive

    results = await asyncio.gather(
        db.insert_test_selection_promotion(promotion_values()),
        db.insert_test_selection_promotion(promotion_values()),
        return_exceptions=True,
    )
    winners = [result for result in results if isinstance(result, dict)]
    assert len(winners) == 1
    assert sum(isinstance(result, PromotionActive) for result in results) == 1
    assert await db.active_test_selection_promotion(project_id="p") == winners[0]


async def test_unrelated_promotion_integrity_errors_are_not_active_conflicts(db):
    with pytest.raises(IntegrityError):
        await db.insert_test_selection_promotion(promotion_values(project_id="unknown"))
    first = await db.insert_test_selection_promotion(promotion_values())
    await db.revoke_test_selection_promotion(first["id"], now=11.0, reason="rollback")
    with pytest.raises(IntegrityError):
        await db.insert_test_selection_promotion(promotion_values(id=first["id"]))


async def test_retention_deletes_only_older_selections_and_cascades_observations(db):
    old = await db.insert_test_selection(selection_values(created_at=1.0))
    retained = await db.insert_test_selection(selection_values(created_at=2.0))
    await observe(db, old["id"])
    kept_observation = await observe(db, retained["id"])
    assert await db.delete_test_selections_older_than(older_than=2.0) == 1
    assert await db.get_test_selection(old["id"]) is None
    assert await db.get_test_selection(retained["id"]) == retained
    assert await db.list_test_selection_observations(old["id"]) == []
    assert await db.list_test_selection_observations(retained["id"]) == [kept_observation]
    assert await db.delete_test_selections_older_than(older_than=2.0) == 0


@pytest.mark.parametrize("removal", ["archive_task", "delete_task"])
async def test_task_removal_retains_immutable_selection_history(db, removal):
    await db.create_task(
        Task(
            id="terminal",
            project_id="p",
            title="Completed work",
            description="",
            status=TaskStatus.COMPLETED,
        )
    )
    selection = await db.insert_test_selection(selection_values(task_id="terminal"))
    observation = await observe(db, selection["id"])
    await getattr(db, removal)("terminal")
    assert await db.get_task("terminal") is None
    assert await db.get_test_selection(selection["id"]) == selection
    assert await db.list_test_selection_observations(selection["id"]) == [observation]
    assert await db.list_test_selections(project_id="p", task_id="terminal") == [selection]


@pytest.mark.parametrize("field", ["mode", "jev_status", "marker_policy"])
async def test_selection_checks_reject_invalid_values(db, field):
    with pytest.raises(IntegrityError):
        await db.insert_test_selection(selection_values(**{field: "invalid-value"}))
    assert await db.list_test_selections(project_id="p") == []


async def test_observation_kind_check_rejects_invalid_value(db):
    selection = await db.insert_test_selection(selection_values())
    with pytest.raises(IntegrityError):
        await observe(db, selection["id"], kind="invalid-value")
    assert await db.list_test_selection_observations(selection["id"]) == []
