"""Package 6 T-3 — migration inventory commands, waivers, and their storage.

Child plan §3.6 (command surface) and §4.2 (the waiver's security properties).
The acknowledgement is the one mechanism in Package 6 that can move the fleet
past a real problem, so every property that keeps it narrow is pinned here:
attribution comes from the server, the justification has a floor, editing the
source invalidates the waiver, and no shipped agent profile can call it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from src.commands.playbook_migration_commands import (
    REASON_TOO_SHORT_ERROR,
    PlaybookMigrationCommandsMixin,
)
from src.database import Database
from src.database.queries.playbook_migration_queries import MIN_ACK_REASON_LENGTH
from src.vault import ensure_default_agent_type_playbooks, ensure_default_playbooks
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()

_GOOD_REASON = "superseded by the router; not migrating"


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "playbook-migration.db"))
        await database.initialize()
    yield database
    await database.close()


class _Config:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.vault_root = os.path.join(data_dir, "vault")
        self.compiled_root = os.path.join(data_dir, "compiled")


class _Handler(PlaybookMigrationCommandsMixin):
    """Just the mixin — the surface under test owns no other collaborator."""

    def __init__(self, config, db) -> None:
        self.config = config
        self.db = db

    def _migration_store(self):
        # The compiled V1 tree is irrelevant to the command surface; the
        # inventory's own suite covers it.
        return None


@pytest.fixture
def handler(tmp_path, db):
    data_dir = str(tmp_path / "aq")
    os.makedirs(data_dir, exist_ok=True)
    ensure_default_playbooks(data_dir)
    ensure_default_agent_type_playbooks(data_dir)
    return _Handler(_Config(data_dir), db)


def _ack(handler, playbook_id: str, reason: str):
    return handler._cmd_playbook_migration_acknowledge(
        {"playbook_id": playbook_id, "reason": reason}
    )


def _source_path(handler, name: str = "default-pipeline.md") -> Path:
    return Path(handler.config.vault_root) / "system" / "playbooks" / name


# ---------------------------------------------------------------------------
# Inventory command
# ---------------------------------------------------------------------------


async def test_inventory_command_reports_every_shipped_playbook(handler):
    result = await handler._cmd_playbook_migration_inventory({})
    assert result["success"] is True
    ids = {entry["playbook_id"] for entry in result["entries"]}
    assert {
        "default-pipeline",
        "default-assignment-routing",
        "memory-consolidation",
        "coding-reflection",
    } <= ids
    assert sum(result["counts"].values()) == len(result["entries"])
    assert result["blocking"] >= 1


async def test_inventory_filter_keeps_fleet_wide_counts(handler):
    unfiltered = await handler._cmd_playbook_migration_inventory({})
    filtered = await handler._cmd_playbook_migration_inventory({"disposition": "question_required"})
    assert filtered["filtered_by"] == "question_required"
    assert all(e["disposition"] == "question_required" for e in filtered["entries"])
    assert filtered["counts"] == unfiltered["counts"]


async def test_inventory_rejects_an_unknown_disposition(handler):
    result = await handler._cmd_playbook_migration_inventory({"disposition": "probably-fine"})
    assert result["success"] is False
    assert "probably-fine" in result["error"]


# ---------------------------------------------------------------------------
# Acknowledgement
# ---------------------------------------------------------------------------


async def test_acknowledge_requires_a_reason(handler):
    for reason in ("", "too short", "x" * (MIN_ACK_REASON_LENGTH - 1)):
        result = await _ack(handler, "default-pipeline", reason)
        assert result["success"] is False
        assert result["error"] == REASON_TOO_SHORT_ERROR
        assert str(MIN_ACK_REASON_LENGTH) in result["error"]

    result = await _ack(handler, "default-pipeline", _GOOD_REASON)
    assert result["success"] is True


async def test_acknowledge_refuses_an_unknown_playbook(handler):
    result = await _ack(handler, "not-installed", _GOOD_REASON)
    assert result["success"] is False
    assert "not-installed" in result["error"]


async def test_acknowledged_by_is_server_derived(handler):
    """A request may not name its own author (§4.2)."""
    result = await handler._cmd_playbook_migration_acknowledge(
        {
            "playbook_id": "default-pipeline",
            "reason": _GOOD_REASON,
            "acknowledged_by": "root",
        }
    )
    assert result["success"] is True
    assert result["acknowledgement"]["acknowledged_by"] != "root"

    rows = await handler.db.list_playbook_migration_acks()
    assert [row["acknowledged_by"] for row in rows] != ["root"]


async def test_acknowledge_moves_the_entry_to_disabled(handler):
    await _ack(handler, "default-pipeline", _GOOD_REASON)
    inventory = await handler._cmd_playbook_migration_inventory({})
    entry = next(e for e in inventory["entries"] if e["playbook_id"] == "default-pipeline")
    assert entry["disposition"] == "disabled"
    assert entry["acknowledged_by"]
    assert "default-pipeline" not in {
        e["playbook_id"]
        for e in inventory["entries"]
        if e["disposition"] in ("question_required", "invalid")
    }


async def test_editing_the_source_invalidates_the_waiver(handler):
    await _ack(handler, "default-pipeline", _GOOD_REASON)
    path = _source_path(handler)
    path.write_text(path.read_text() + "\n<!-- an edit -->\n", encoding="utf-8")

    inventory = await handler._cmd_playbook_migration_inventory({})
    entry = next(e for e in inventory["entries"] if e["playbook_id"] == "default-pipeline")
    assert entry["disposition"] == "question_required"
    assert entry["acknowledged_by"] is None


async def test_unacknowledge_restores_the_computed_disposition(handler):
    await _ack(handler, "default-pipeline", _GOOD_REASON)
    removed = await handler._cmd_playbook_migration_unacknowledge({"playbook_id": "default-pipeline"})
    assert removed == {"success": True, "playbook_id": "default-pipeline", "removed": 1}

    inventory = await handler._cmd_playbook_migration_inventory({})
    entry = next(e for e in inventory["entries"] if e["playbook_id"] == "default-pipeline")
    assert entry["disposition"] == "question_required"

    again = await handler._cmd_playbook_migration_unacknowledge({"playbook_id": "default-pipeline"})
    assert again["success"] is False


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


async def test_upsert_replaces_rather_than_duplicating(db):
    digest = "sha256:" + hashlib.sha256(b"x").hexdigest()
    for reason in ("the first justification", "the second justification"):
        await db.upsert_playbook_migration_ack(
            playbook_id="pb",
            scope="system",
            scope_identifier="",
            source_sha256=digest,
            reason=reason,
            acknowledged_by="operator",
        )
    rows = await db.list_playbook_migration_acks()
    assert len(rows) == 1
    assert rows[0]["reason"] == "the second justification"


async def test_storage_enforces_the_reason_floor(db):
    with pytest.raises(ValueError):
        await db.upsert_playbook_migration_ack(
            playbook_id="pb",
            scope="system",
            source_sha256="sha256:" + "a" * 64,
            reason="short",
            acknowledged_by="operator",
        )
    assert await db.list_playbook_migration_acks() == []


async def test_scope_identifier_is_part_of_the_key(db):
    digest = "sha256:" + "a" * 64
    for identifier in ("alpha", "beta"):
        await db.upsert_playbook_migration_ack(
            playbook_id="pb",
            scope="project",
            scope_identifier=identifier,
            source_sha256=digest,
            reason="a sufficiently long justification",
            acknowledged_by="operator",
        )
    rows = await db.list_playbook_migration_acks()
    assert {row["scope_identifier"] for row in rows} == {"alpha", "beta"}
    assert await db.delete_playbook_migration_ack(
        playbook_id="pb", scope="project", scope_identifier="alpha"
    )
    rows = await db.list_playbook_migration_acks()
    assert [row["scope_identifier"] for row in rows] == ["beta"]


async def test_delete_reports_a_miss(db):
    assert (
        await db.delete_playbook_migration_ack(playbook_id="nope", scope="system") is False
    )


# ---------------------------------------------------------------------------
# The waivers are operator-only
# ---------------------------------------------------------------------------


def test_write_commands_absent_from_every_shipped_profile():
    """A worker that can waive a broken playbook can disable its own reviewer."""
    import src.profiles as profiles_pkg

    defaults = Path(os.path.dirname(profiles_pkg.__file__)) / "defaults"
    forbidden = (
        "playbook_migration_acknowledge",
        "playbook_migration_unacknowledge",
    )
    offenders = []
    for profile in sorted(defaults.glob("*/profile.md")):
        text = profile.read_text(encoding="utf-8")
        offenders += [
            f"{profile.parent.name}: {name}" for name in forbidden if name in text
        ]
    assert not offenders, offenders


def test_commands_are_registered_on_the_command_handler():
    from src.commands.handler import CommandHandler
    from src.tools.definitions import _TOOL_CATEGORIES

    for name in (
        "playbook_migration_inventory",
        "playbook_migration_acknowledge",
        "playbook_migration_unacknowledge",
        "playbook_cutover_report",
    ):
        assert callable(getattr(CommandHandler, f"_cmd_{name}", None)), name
        assert _TOOL_CATEGORIES.get(name) == "playbook", name


def test_response_models_are_registered():
    from src.api.models.playbook_migration import RESPONSE_MODELS

    for name in (
        "playbook_migration_inventory",
        "playbook_migration_acknowledge",
        "playbook_migration_unacknowledge",
        "playbook_cutover_report",
    ):
        assert name in RESPONSE_MODELS, name
