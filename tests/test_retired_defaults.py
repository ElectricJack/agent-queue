"""Operator opt-out for shipped default profiles — ``src/profiles/retired_defaults.py``.

``vault.ensure_default_profiles()`` runs on every daemon start and is
write-if-absent, so "no ``vault/agent-types/<id>/``" used to mean only *fresh
install*.  An operator who deleted ``worker-standard`` because their fleet had
moved to the provider-explicit ladder got it back at the next restart.  The
tombstone file covered here is what makes the deletion stick.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.profiles.retired_defaults import (
    RETIRED_DEFAULTS_VERSION,
    is_retired,
    retire_default,
    retired_default_ids,
    retired_defaults_path,
    unretire_default,
)
from src.vault import ensure_default_profiles


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


def _read(data_dir: str) -> dict:
    return json.loads(Path(retired_defaults_path(data_dir)).read_text(encoding="utf-8"))


# --- the record ------------------------------------------------------------


def test_no_file_means_nothing_is_retired(data_dir):
    assert retired_default_ids(data_dir) == set()
    assert is_retired(data_dir, "worker-standard-medium-claude") is False


def test_retiring_writes_a_versioned_record(data_dir):
    assert retire_default(data_dir, "worker-fast-medium-claude", "fleet runs -high") is True

    payload = _read(data_dir)
    assert payload["version"] == RETIRED_DEFAULTS_VERSION
    entry = payload["retired"]["worker-fast-medium-claude"]
    assert entry["reason"] == "fleet runs -high"
    assert isinstance(entry["retired_at"], float)
    assert is_retired(data_dir, "worker-fast-medium-claude") is True


def test_retiring_is_idempotent(data_dir):
    assert retire_default(data_dir, "worker-deep-high-claude") is True
    assert retire_default(data_dir, "worker-deep-high-claude") is False
    assert retired_default_ids(data_dir) == {"worker-deep-high-claude"}


def test_retiring_several_ids_accumulates(data_dir):
    retire_default(data_dir, "worker-fast-medium-claude")
    retire_default(data_dir, "worker-deep-high-claude")
    assert retired_default_ids(data_dir) == {
        "worker-fast-medium-claude",
        "worker-deep-high-claude",
    }


def test_a_reason_is_optional(data_dir):
    retire_default(data_dir, "planner")
    assert "reason" not in _read(data_dir)["retired"]["planner"]


def test_unretiring_removes_just_that_id(data_dir):
    retire_default(data_dir, "planner")
    retire_default(data_dir, "reviewer")

    assert unretire_default(data_dir, "planner") is True
    assert retired_default_ids(data_dir) == {"reviewer"}
    # Nothing to remove the second time.
    assert unretire_default(data_dir, "planner") is False


def test_blank_ids_are_ignored(data_dir):
    assert retire_default(data_dir, "   ") is False
    assert unretire_default(data_dir, "") is False
    assert retired_default_ids(data_dir) == set()


def test_ids_are_stripped_before_use(data_dir):
    retire_default(data_dir, "  planner  ")
    assert retired_default_ids(data_dir) == {"planner"}


def test_a_corrupt_record_retires_nothing(data_dir):
    path = Path(retired_defaults_path(data_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    # Failing open is deliberate: a corrupt opt-out file must not be able to
    # suppress seeding silently.  Worst case the operator deletes it again.
    assert retired_default_ids(data_dir) == set()


@pytest.mark.parametrize("payload", ['["planner"]', '{"retired": []}', '{}'])
def test_a_record_of_the_wrong_shape_retires_nothing(data_dir, payload):
    path = Path(retired_defaults_path(data_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    assert retired_default_ids(data_dir) == set()


def test_a_corrupt_record_is_replaced_by_the_next_retire(data_dir):
    path = Path(retired_defaults_path(data_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert retire_default(data_dir, "planner") is True
    assert _read(data_dir)["retired"].keys() == {"planner"}


# --- seeding honours it ----------------------------------------------------


def test_seeding_skips_a_retired_default(data_dir):
    first = ensure_default_profiles(data_dir)
    assert "worker-standard-medium-claude" in first["created"]
    assert first["retired"] == []

    vault_copy = (
        Path(data_dir) / "vault" / "agent-types"
        / "worker-standard-medium-claude" / "profile.md"
    )
    vault_copy.unlink()
    retire_default(data_dir, "worker-standard-medium-claude", "moved to -high")

    second = ensure_default_profiles(data_dir)
    assert second["created"] == []
    assert second["retired"] == ["worker-standard-medium-claude"]
    assert not vault_copy.exists()
    # Every other shipped default is still seeded, not collateral damage.
    assert "worker-deep-high-claude" in second["skipped"]


def test_seeding_restores_a_default_once_it_is_unretired(data_dir):
    ensure_default_profiles(data_dir)
    vault_copy = (
        Path(data_dir) / "vault" / "agent-types"
        / "worker-fast-medium-claude" / "profile.md"
    )
    vault_copy.unlink()
    retire_default(data_dir, "worker-fast-medium-claude")
    ensure_default_profiles(data_dir)
    assert not vault_copy.exists()

    unretire_default(data_dir, "worker-fast-medium-claude")
    result = ensure_default_profiles(data_dir)
    assert result["created"] == ["worker-fast-medium-claude"]
    assert vault_copy.is_file()


def test_a_retired_id_that_still_has_a_vault_copy_is_reported_as_skipped(data_dir):
    """Present wins: the tombstone only ever suppresses a *write*."""
    ensure_default_profiles(data_dir)
    retire_default(data_dir, "worker-deep-high-claude")

    result = ensure_default_profiles(data_dir)
    assert "worker-deep-high-claude" in result["skipped"]
    assert result["retired"] == []


def test_the_tombstone_file_is_not_mistaken_for_a_profile(data_dir):
    retire_default(data_dir, "planner")
    result = ensure_default_profiles(data_dir)
    # A dotfile sitting in agent-types/ must never appear as a profile id.
    assert ".retired-defaults" not in result["created"] + result["skipped"]


# --- command surface -------------------------------------------------------


@pytest.fixture
async def handler(tmp_path):
    from src.commands.handler import CommandHandler
    from src.config import AppConfig
    from src.orchestrator import Orchestrator

    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    await orch.initialize()
    yield CommandHandler(orch, config)
    await orch.db.close()


async def test_deleting_a_shipped_default_survives_the_next_startup(handler):
    data_dir = handler.config.data_dir
    profile_id = "worker-fast-medium-claude"
    vault_copy = Path(data_dir) / "vault" / "agent-types" / profile_id / "profile.md"
    assert vault_copy.is_file()

    result = await handler.execute(
        "delete_profile", {"profile_id": profile_id, "reason": "runs -high only"}
    )
    assert result["deleted"] == profile_id
    assert result["retired"] is True
    assert "profile-reseed" in result["note"]
    assert not vault_copy.exists()

    # The next daemon start re-runs seeding; the deletion must hold.
    seeded = ensure_default_profiles(data_dir)
    assert seeded["retired"] == [profile_id]
    assert not vault_copy.exists()


async def test_deleting_a_non_shipped_profile_writes_no_tombstone(handler):
    from src.models import AgentProfile

    await handler.orchestrator.db.create_profile(
        AgentProfile(id="my-own", name="Mine", harness="claude")
    )
    result = await handler.execute("delete_profile", {"profile_id": "my-own"})
    assert result["deleted"] == "my-own"
    assert "retired" not in result
    assert retired_default_ids(handler.config.data_dir) == set()


async def test_reseeding_clears_the_tombstone(handler):
    data_dir = handler.config.data_dir
    profile_id = "worker-deep-high-claude"
    await handler.execute("delete_profile", {"profile_id": profile_id})
    assert is_retired(data_dir, profile_id) is True

    reseeded = await handler.execute("profile_reseed", {"profile_id": profile_id})
    assert reseeded["success"] is True
    assert reseeded["created"] is True
    assert reseeded["unretired"] is True
    assert is_retired(data_dir, profile_id) is False

    # And seeding now leaves it alone because the file is back.
    assert ensure_default_profiles(data_dir)["retired"] == []


async def test_reseeding_a_never_retired_profile_reports_no_unretire(handler):
    result = await handler.execute("profile_reseed", {"profile_id": "reviewer"})
    assert result["unretired"] is False
