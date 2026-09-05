"""Operator adoption path for reviewed Playbook V2 artifact bundles."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from src.api.auth import RequestScope
from src.api.models import get_all_response_models
from src.api.scope import check_command_scope
from src.commands.handler import CommandHandler, PAUSED_PLAYBOOK_COMMANDS
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.commands.playbook_v2_commands import (
    PLAYBOOK_V2_IMPORT_COMMANDS,
    PlaybookV2CommandsMixin,
)
from src.database import Database
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.profiles import shipped_profile_lookup
from src.playbooks.validation import RegisteredEventLookup, RegistryContractLookup
from src.profiles.capabilities import CapabilityPolicy
from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES
from tests.pg_dsn import ensure_worker_postgres_dsn


POSTGRES_TEST_DSN = ensure_worker_postgres_dsn()
REVIEWED_FIXTURES = Path("tests/fixtures/playbooks/v2")
PLAYBOOK_IDS = (
    "default-pipeline",
    "default-assignment-routing",
    "hierarchical-delivery",
    "memory-consolidation",
        "pr-merge-sweep",
    "ci-main-sentinel",
)


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
        database = Database(str(tmp_path / "reviewed-import.db"))
        await database.initialize()
    yield database
    await database.close()


class _Handler(PlaybookV2CommandsMixin):
    def __init__(self, tmp_path: Path, db) -> None:
        self.db = db
        self.config = SimpleNamespace(
            data_dir=str(tmp_path),
            vault_root=str(tmp_path / "vault"),
            compiled_root=str(tmp_path / "compiled"),
            playbooks=SimpleNamespace(
                enabled=True,
                v2_max_artifact_bytes=1_048_576,
            ),
        )
        self._store = ArtifactStore(self.config.compiled_root)

    def _v2_engine(self):
        return SimpleNamespace(services=SimpleNamespace(artifact_store=self._store))

    async def _v2_lookups(self):
        return RegistryContractLookup(), shipped_profile_lookup(), RegisteredEventLookup()


def _copy_bundle(tmp_path: Path, playbook_id: str = "default-pipeline") -> Path:
    relative = Path("reviewed") / playbook_id
    target = tmp_path / "vault" / relative
    shutil.copytree(REVIEWED_FIXTURES / playbook_id, target)
    return relative


def _review_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    marker = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:marker]), text[marker + 5 :]


def _write_review(path: Path, frontmatter: dict, body: str) -> None:
    path.write_text(
        f"---\n{yaml.safe_dump(frontmatter, sort_keys=False)}---\n{body}",
        encoding="utf-8",
    )


async def _import(handler: _Handler, relative: Path) -> dict:
    return await handler._cmd_playbook_v2_import({"path": relative.as_posix()})


def _fail_when_artifact_lock_exits(db, monkeypatch, error: BaseException) -> None:
    """Inject a rollback after the import body, where transaction commit lives."""
    original_lock = db.artifact_hash_lock

    @asynccontextmanager
    async def failing_lock(shas):
        async with original_lock(shas) as conn:
            yield conn
            raise error

    monkeypatch.setattr(db, "artifact_hash_lock", failing_lock)


@pytest.mark.asyncio
async def test_imports_all_reviewed_fixtures_without_activating(db, tmp_path):
    """Dropping the upsert or accidentally activating makes this rehearsal fail."""
    handler = _Handler(tmp_path, db)

    results = []
    for playbook_id in PLAYBOOK_IDS:
        results.append(await _import(handler, _copy_bundle(tmp_path, playbook_id)))

    assert [result["playbook_id"] for result in results] == list(PLAYBOOK_IDS)
    assert all(result["success"] is True for result in results)
    assert all(result["activated"] is False for result in results)
    assert all(result["schema_version"] == 2 for result in results)
    assert all(result["version"] == 1 for result in results)
    assert all(result["scope"] in {"system", "project", "agent_type"} for result in results)
    for result in results:
        assert result["artifact_sha256"].startswith("sha256:")
        assert await db.get_playbook_artifact(result["artifact_sha256"]) is not None
        assert handler._store.exists(result["artifact_sha256"])
    assert await db.list_playbook_activations() == []


@pytest.mark.asyncio
async def test_import_refuses_a_bundle_outside_the_vault(db, tmp_path):
    handler = _Handler(tmp_path, db)
    outside = tmp_path / "outside"
    shutil.copytree(REVIEWED_FIXTURES / "default-pipeline", outside)

    result = await handler._cmd_playbook_v2_import({"path": str(outside)})

    assert result["success"] is False
    assert "inside vault root" in result["error"]
    assert await db.list_playbook_artifacts("default-pipeline") == []


@pytest.mark.asyncio
async def test_import_refuses_a_bundle_file_symlinked_outside_the_vault(db, tmp_path):
    """Resolving only the parent directory would let child symlinks escape."""
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    artifact_path = tmp_path / "vault" / relative / "artifact.json"
    outside = tmp_path / "outside-artifact.json"
    outside.write_bytes(artifact_path.read_bytes())
    artifact_path.unlink()
    artifact_path.symlink_to(outside)

    result = await _import(handler, relative)

    assert result["success"] is False
    assert "artifact.json must be inside vault root" in result["error"]
    assert await db.list_playbook_artifacts("default-pipeline") == []


@pytest.mark.asyncio
async def test_import_does_not_treat_review_decision_as_core_policy(db, tmp_path):
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    review_path = tmp_path / "vault" / relative / "manifest.md"
    review, body = _review_frontmatter(review_path)
    review["decision"] = "rejected"
    _write_review(review_path, review, body)

    result = await _import(handler, relative)

    assert result["success"] is True
    assert result["activated"] is False
    assert len(await db.list_playbook_artifacts("default-pipeline")) == 1


@pytest.mark.asyncio
async def test_import_refuses_duplicate_review_frontmatter_keys(db, tmp_path):
    """Duplicate metadata keys are rejected even when they are not policy gates."""
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    review_path = tmp_path / "vault" / relative / "manifest.md"
    text = review_path.read_text(encoding="utf-8")
    review_path.write_text(text.replace("---\n", "---\nplaybook_id: duplicate\n", 1))

    result = await _import(handler, relative)

    assert result["success"] is False
    assert "duplicate review key 'playbook_id'" in result["error"]
    assert await db.list_playbook_artifacts("default-pipeline") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["artifact_sha256", "source_sha256", "playbook_id"])
async def test_import_refuses_review_metadata_that_does_not_match_bytes(
    db, tmp_path, mismatch
):
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    review_path = tmp_path / "vault" / relative / "manifest.md"
    review, body = _review_frontmatter(review_path)
    review[mismatch] = {
        "artifact_sha256": "sha256:" + "0" * 64,
        "source_sha256": "sha256:" + "1" * 64,
        "playbook_id": "some-other-playbook",
    }[mismatch]
    _write_review(review_path, review, body)

    result = await _import(handler, relative)

    assert result["success"] is False
    assert mismatch in result["error"]
    assert await db.list_playbook_artifacts("default-pipeline") == []


@pytest.mark.asyncio
async def test_import_refuses_noncanonical_artifact_bytes(db, tmp_path):
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    directory = tmp_path / "vault" / relative
    artifact_path = directory / "artifact.json"
    raw = artifact_path.read_bytes() + b"\n"
    artifact_path.write_bytes(raw)
    sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    (directory / "artifact.sha256").write_text(sha + "\n", encoding="utf-8")
    review, body = _review_frontmatter(directory / "manifest.md")
    review["artifact_sha256"] = sha
    _write_review(directory / "manifest.md", review, body)

    result = await _import(handler, relative)

    assert result["success"] is False
    assert "canonical" in result["error"]
    assert await db.list_playbook_artifacts("default-pipeline") == []


@pytest.mark.asyncio
async def test_import_refuses_an_artifact_that_fails_live_validation(db, tmp_path):
    from tests.playbook_v2_helpers import StubContracts

    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    handler._v2_lookups = AsyncMock(
        return_value=(StubContracts(), shipped_profile_lookup(), RegisteredEventLookup())
    )

    result = await _import(handler, relative)

    assert result["success"] is False
    assert result["diagnostics"]
    assert any(row["severity"] in {"error", "question"} for row in result["diagnostics"])
    assert await db.list_playbook_artifacts("default-pipeline") == []


@pytest.mark.asyncio
async def test_failed_database_upsert_removes_a_new_artifact_file(db, tmp_path, monkeypatch):
    """A missing DB reference after a failed import must not leave new bytes behind."""
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    recorded_sha = (
        tmp_path / "vault" / relative / "artifact.sha256"
    ).read_text(encoding="utf-8").strip()
    monkeypatch.setattr(
        db,
        "upsert_playbook_artifact",
        AsyncMock(side_effect=RuntimeError("database write failed")),
    )

    result = await _import(handler, relative)

    assert result["success"] is False
    assert "database write failed" in result["error"]
    assert handler._store.exists(recorded_sha) is False
    assert await db.list_playbook_artifacts("default-pipeline") == []


@pytest.mark.asyncio
async def test_cancelled_database_upsert_removes_a_new_artifact_file(db, tmp_path, monkeypatch):
    """Cancellation is a rollback too, even though it is not an Exception."""
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    recorded_sha = (
        tmp_path / "vault" / relative / "artifact.sha256"
    ).read_text(encoding="utf-8").strip()
    monkeypatch.setattr(
        db,
        "upsert_playbook_artifact",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await _import(handler, relative)

    assert handler._store.exists(recorded_sha) is False
    assert await db.list_playbook_artifacts("default-pipeline") == []


@pytest.mark.asyncio
async def test_failed_transaction_exit_removes_a_new_artifact_file(db, tmp_path, monkeypatch):
    """Commit/__aexit__ failure is still inside the compensating boundary."""
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    recorded_sha = (
        tmp_path / "vault" / relative / "artifact.sha256"
    ).read_text(encoding="utf-8").strip()
    _fail_when_artifact_lock_exits(
        db, monkeypatch, RuntimeError("transaction commit failed")
    )

    result = await _import(handler, relative)

    assert result["success"] is False
    assert "transaction commit failed" in result["error"]
    assert handler._store.exists(recorded_sha) is False
    assert await db.list_playbook_artifacts("default-pipeline") == []


@pytest.mark.asyncio
async def test_cancelled_transaction_exit_removes_a_new_artifact_file(
    db, tmp_path, monkeypatch
):
    """Cancellation during commit rolls back bytes without being swallowed."""
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    recorded_sha = (
        tmp_path / "vault" / relative / "artifact.sha256"
    ).read_text(encoding="utf-8").strip()
    _fail_when_artifact_lock_exits(db, monkeypatch, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _import(handler, relative)

    assert handler._store.exists(recorded_sha) is False
    assert await db.list_playbook_artifacts("default-pipeline") == []


@pytest.mark.asyncio
async def test_failed_transaction_exit_preserves_an_adopted_artifact_file(
    db, tmp_path, monkeypatch
):
    """Compensation must not delete identical bytes that predated this import."""
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    directory = tmp_path / "vault" / relative
    recorded_sha = (directory / "artifact.sha256").read_text(encoding="utf-8").strip()
    stored_path = Path(handler._store.path_for(recorded_sha))
    stored_path.parent.mkdir(parents=True)
    stored_path.write_bytes((directory / "artifact.json").read_bytes())
    _fail_when_artifact_lock_exits(
        db, monkeypatch, RuntimeError("transaction commit failed")
    )

    result = await _import(handler, relative)

    assert result["success"] is False
    assert stored_path.is_file()
    assert await db.list_playbook_artifacts("default-pipeline") == []


def test_import_is_not_available_to_an_ordinary_agent_session():
    scope = RequestScope(
        kind="session", session_id="s1", task_id="t1", project_id="p1"
    )

    assert check_command_scope("playbook_v2_import", {}, scope) == (
        "out of scope: playbook_v2_import"
    )


@pytest.mark.asyncio
async def test_command_refuses_a_non_elevated_principal_even_with_capability(db, tmp_path):
    """MCP/direct dispatch cannot bypass the HTTP session-scope allowlist."""
    handler = _Handler(tmp_path, db)
    relative = _copy_bundle(tmp_path)
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        session_id="s1",
        project_id="p1",
        policy=CapabilityPolicy.from_namespaces(aq_commands=["playbook_v2_import"]),
    )

    with principal_context(principal):
        result = await _import(handler, relative)

    assert result == {
        "success": False,
        "error": "out of scope: playbook_v2_import requires elevated capability scope",
    }
    assert await db.list_playbook_artifacts("default-pipeline") == []


def test_import_is_exposed_on_every_operator_surface_and_pauses_with_playbooks():
    """Removing any registration breaks API, CLI, MCP, or feature-pause parity."""
    name = "playbook_v2_import"
    definitions = {definition["name"] for definition in _ALL_TOOL_DEFINITIONS}

    assert PLAYBOOK_V2_IMPORT_COMMANDS == frozenset({name})
    assert name in definitions
    assert _TOOL_CATEGORIES[name] == "playbook"
    assert name in get_all_response_models()
    assert name in PAUSED_PLAYBOOK_COMMANDS
    assert hasattr(CommandHandler, f"_cmd_{name}")
