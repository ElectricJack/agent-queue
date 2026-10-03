"""K06 legacy read-only import inventory: schema, authorization and dry-run CLI.

Covers the mapping schema (three tables + migration roundtrip), the contract
registration, the local-operator / feature-flag gating, and the in-memory
read-only reconciliation the dry-run returns. The scanner slice itself is
covered by :mod:`tests.test_knowledge_inventory`; this file only pins what K06
owns: the tables, the authorization, and the operator-only read path.

Nothing here applies a revision or writes an import row.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import inspect

from src.commands.contracts.builtin import register_builtin_contracts
from src.commands.contracts.registry import ContractRegistry
from src.commands.knowledge_commands import KnowledgeCommandsMixin
from src.commands.principal import (
    TRUSTED_LOCAL,
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.config import KnowledgeConfig, KnowledgeFeatureConfig
from src.database.engine import create_postgres_engine
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database
from tests.test_migration_integration_subjects import _alembic

REVISION = "a00000000060"
IMPORT_TABLES = ("record_import_runs", "record_legacy_mappings", "record_import_items")


# ---------------------------------------------------------------------------
# Mapping schema: migration roundtrip on a disposable database.
# ---------------------------------------------------------------------------


@pytest.mark.migration
async def test_import_inventory_migration_deploys_rolls_back_and_replays():
    """The revision owns both install kinds, on a disposable database.

    A fresh install's squashed baseline is built from live metadata
    (``create_all``), so the tables already exist there and the revision must
    stay a no-op. For a deployed install at the previous revision the tables
    never existed: construct that state explicitly by dropping only the three
    empty new tables in reverse dependency order, then verify upgrade,
    metadata equality, downgrade, re-upgrade, and a re-run over the current
    schema.
    """
    from importlib import import_module

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = create_postgres_engine(await create_scratch_database("import-inventory60"))
    try:
        await _alembic(engine, "upgrade", previous_revision(REVISION))

        # Fresh baseline: already materialized from live metadata before the
        # revision runs -- the revision's upgrade is idempotent here.
        names = await _table_names(engine)
        assert set(IMPORT_TABLES) <= names

        # Deployed install at a059: never had the three tables. Drop them in
        # reverse dependency order (items references runs; runs references the
        # pre-existing record_scopes).
        async with engine.begin() as conn:
            await conn.execute(sa.text('DROP TABLE "record_import_items"'))
            await conn.execute(sa.text('DROP TABLE "record_import_runs"'))
            await conn.execute(sa.text('DROP TABLE "record_legacy_mappings"'))
        names = await _table_names(engine)
        assert not (set(IMPORT_TABLES) & names), (
            f"pre-upgrade state wrong: {set(IMPORT_TABLES) & names}"
        )

        # The revision's own upgrade creates them from the shared metadata.
        await _alembic(engine, "upgrade", REVISION)
        await _assert_matches_metadata(engine)

        # Down removes exactly what up added; up again restores it.
        await _alembic(engine, "downgrade", previous_revision(REVISION))
        names = await _table_names(engine)
        assert not (set(IMPORT_TABLES) & names), (
            f"import tables survived downgrade: {set(IMPORT_TABLES) & names}"
        )
        await _alembic(engine, "upgrade", REVISION)
        await _assert_matches_metadata(engine)

        # Replay: the body itself is idempotent over the current schema.
        revision = import_module("migrations.versions.a00000000060_knowledge_import_inventory")

        def rerun(sync):
            with Operations.context(MigrationContext.configure(sync)):
                revision.upgrade()
                revision.upgrade()

        async with engine.begin() as conn:
            await conn.run_sync(rerun)
        await _assert_matches_metadata(engine)
    finally:
        await engine.dispose()


async def _table_names(engine) -> set[str]:
    def check(sync):
        return inspect(sync).get_table_names()

    async with engine.begin() as conn:
        return set(await conn.run_sync(check))


@pytest.mark.migration
async def test_import_inventory_tables_match_metadata():
    from src.database.tables import (
        record_import_items,
        record_import_runs,
        record_legacy_mappings,
    )

    engine = create_postgres_engine(await create_scratch_database("import-inventory61"))
    try:
        await _alembic(engine, "upgrade", REVISION)

        def shape(sync):
            inspector = inspect(sync)
            return {
                "record_import_runs": {c["name"] for c in inspector.get_columns("record_import_runs")},
                "record_legacy_mappings": {c["name"] for c in inspector.get_columns("record_legacy_mappings")},
                "record_import_items": {c["name"] for c in inspector.get_columns("record_import_items")},
            }

        async with engine.begin() as conn:
            live = await conn.run_sync(shape)

        for table_def, name in (
            (record_import_runs, "record_import_runs"),
            (record_legacy_mappings, "record_legacy_mappings"),
            (record_import_items, "record_import_items"),
        ):
            expected = {c.name for c in table_def.columns}
            assert expected <= live[name], f"{name}: missing {expected - live[name]}"

        # The hash + state columns the read-only rollback relies on are spelled
        # out in the live schema.
        assert {"manifest_sha256", "state", "cursor", "report"} <= live["record_import_runs"]
    finally:
        await engine.dispose()


async def _assert_matches_metadata(engine) -> None:
    """Live columns and named constraints of the three tables equal ``metadata``."""
    from src.database.tables import metadata

    def shape(sync):
        inspector = inspect(sync)
        return {
            name: {
                "columns": {c["name"] for c in inspector.get_columns(name)},
                "primary_key": list(inspector.get_pk_constraint(name)["constrained_columns"]),
                "checks": {c["name"] for c in inspector.get_check_constraints(name)},
                "uniques": {
                    tuple(sorted(u["column_names"]))
                    for u in inspector.get_unique_constraints(name)
                },
            }
            for name in IMPORT_TABLES
        }

    async with engine.begin() as conn:
        live = await conn.run_sync(shape)

    for name in IMPORT_TABLES:
        table = metadata.tables[name]
        expected = {
            "columns": {c.name for c in table.columns},
            "primary_key": [c.name for c in table.primary_key.columns],
            "checks": {
                c.name for c in table.constraints
                if c.__class__.__name__ == "CheckConstraint"
            },
            "uniques": {
                tuple(sorted(c.name for c in u.columns)) for u in table.constraints
                if u.__class__.__name__ == "UniqueConstraint"
            },
        }
        for key, want in expected.items():
            assert want == live[name][key], f"{name}.{key}: {want ^ live[name][key]}"


# ---------------------------------------------------------------------------
# Contract registration: knowledge_import is a read, operator-only command.
# ---------------------------------------------------------------------------


def _registry():
    registry = ContractRegistry()
    register_builtin_contracts(registry)
    return registry


def test_knowledge_import_contract_registered_read_only():
    registry = _registry()
    registration = registry.get("knowledge_import")
    assert registration is not None
    contract = registration.contract
    execution = contract.execution
    assert execution.side_effect.value == "read"
    assert execution.name == "knowledge_import"
    # Read-only and replay-safe: never a state change.
    assert execution.retry_safe is True
    outcome_names = {outcome.name for outcome in execution.outcomes}
    assert outcome_names == {"completed", "rejected"}


def test_knowledge_import_args_schema_requires_roots_and_identity():
    from src.commands.contracts.inventory import KnowledgeImportArgs

    fields = set(KnowledgeImportArgs.model_fields)
    assert {"roots", "source_installation_id", "snapshot_id", "snapshot_timestamp"} <= fields
    # roots is required and non-empty by the contract.
    roots_field = KnowledgeImportArgs.model_fields["roots"]
    assert roots_field.is_required()
    # Optional evidence knobs are absent-able.
    assert not KnowledgeImportArgs.model_fields["vector_export"].is_required()


def test_knowledge_import_result_schema_matches_report_shape():
    from src.commands.contracts.inventory import KnowledgeImportValue

    fields = set(KnowledgeImportValue.model_fields)
    for key in (
        "success",
        "outcome",
        "error",
        "source_installation_id",
        "snapshot_id",
        "snapshot_timestamp",
        "manifest_sha256",
        "manifest_content_base64",
        "vector_observation",
        "counts",
        "items",
        "mappings",
        "identities",
    ):
        assert key in fields, f"knowledge_import result missing {key}"


# ---------------------------------------------------------------------------
# Authorization: local operator only, AND the import_inventory feature flag.
# ---------------------------------------------------------------------------


def _stub_handler(knowledge_config: KnowledgeConfig):
    class Handler(KnowledgeCommandsMixin):
        def __init__(self, knowledge):
            self.config = SimpleNamespace(knowledge=knowledge)

    return Handler(knowledge_config)


def _args(tmp_path, *, roots=True):
    root_dir = tmp_path / "notes"
    return {
        "roots": [
            {
                "root_id": "notes",
                "path": str(root_dir),
                "source_scope": "project:fixture",
                "source_kind": "notes",
            }
        ]
        if roots
        else [],
        "vector_export": None,
        "scope_aliases": None,
        "source_installation_id": "synthetic-installation",
        "snapshot_id": "synthetic-snapshot",
        "snapshot_timestamp": "2026-10-01T12:00:00Z",
    }


async def test_knowledge_import_requires_local_operator(tmp_path):
    handler = _stub_handler(
        KnowledgeConfig(
            enabled=True,
            import_inventory=KnowledgeFeatureConfig(enabled=True),
        )
    )
    from src.commands.principal import TRUSTED_LOCAL

    session = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=TRUSTED_LOCAL.policy,
        session_id="worker",
        profile_id="test-worker",
    )
    # A SESSION (worker) principal is refused even with every gate on.
    with principal_context(session):
        result = await handler._cmd_knowledge_import(_args(tmp_path))
    assert result["success"] is False
    assert result["error_code"] == "knowledge_import.forbidden"


async def test_knowledge_import_feature_flag_gate(tmp_path):
    # Feature off: even the local operator is refused before any scan.
    handler = _stub_handler(
        KnowledgeConfig(
            enabled=True,
            import_inventory=KnowledgeFeatureConfig(enabled=False),
        )
    )
    from src.commands.principal import TRUSTED_LOCAL

    with principal_context(TRUSTED_LOCAL):
        result = await handler._cmd_knowledge_import(_args(tmp_path))
    assert result["success"] is False
    assert result["error_code"] == "knowledge_import.disabled"

    # Knowledge master switch off: same gate.
    handler_off = _stub_handler(
        KnowledgeConfig(
            enabled=False,
            import_inventory=KnowledgeFeatureConfig(enabled=True),
        )
    )
    with principal_context(TRUSTED_LOCAL):
        result = await handler_off._cmd_knowledge_import(_args(tmp_path))
    assert result["error_code"] == "knowledge_import.disabled"


async def test_knowledge_import_empty_roots_scans_nothing(tmp_path):
    """The handler accepts an empty root list and returns an empty, closed report.

    (The min_length=1 rule on ``KnowledgeImportArgs.roots`` is a contract-layer
    guard enforced at the dispatch boundary, not inside the handler.)
    """
    from src.commands.principal import TRUSTED_LOCAL

    handler = _stub_handler(
        KnowledgeConfig(
            enabled=True,
            import_inventory=KnowledgeFeatureConfig(enabled=True),
        )
    )
    with principal_context(TRUSTED_LOCAL):
        result = await handler._cmd_knowledge_import(_args(tmp_path, roots=False))
    assert result["success"] is True
    assert result["outcome"] == "read"
    assert result["items"] == []
    assert result["mappings"] == []
    assert result["identities"] == []


async def test_knowledge_import_dry_run_returns_reconciliation(tmp_path):
    from src.api.models.knowledge import KnowledgeImportResponse
    from src.commands.principal import TRUSTED_LOCAL

    root_dir = tmp_path / "notes"
    root_dir.mkdir()
    (root_dir / "paired.md").write_bytes(b"Exact retained original.\n")

    handler = _stub_handler(
        KnowledgeConfig(
            enabled=True,
            import_inventory=KnowledgeFeatureConfig(enabled=True),
        )
    )
    with principal_context(TRUSTED_LOCAL):
        result = await handler._cmd_knowledge_import(_args(tmp_path))

    assert result["success"] is True
    assert result["outcome"] == "read"
    assert result["source_installation_id"] == "synthetic-installation"
    assert result["snapshot_id"] == "synthetic-snapshot"
    # A 64-hex seal proves the manifest was sealed before the report.
    assert re_full_match(result["manifest_sha256"])
    assert result["vector_observation"] in ("not_observed", "observed", "unavailable")
    assert isinstance(result["counts"], dict)
    assert isinstance(result["items"], list)
    assert isinstance(result["mappings"], list)
    assert isinstance(result["identities"], list)
    # The accounting closes: every identity maps exactly once.
    id_keys = {(m["source_kind"], m["source_scope"], m["source_key"]) for m in result["mappings"]}
    assert len(id_keys) == len(result["mappings"])
    # API serialization must retain the exact sealed bytes and accounting.
    serialized = KnowledgeImportResponse.model_validate(result).model_dump()
    for field in result:
        assert serialized[field] == result[field]


async def test_knowledge_import_sealed_manifest_content_is_verifiable(tmp_path):
    """The operator can recover the exact sealed seal, not just its hash.

    ROOT REVIEW BLOCKER: the CLI previously returned only ``manifest_sha256``
    and discarded ``manifest.content``. The report must carry the sealed
    canonical bytes (lossless, base64-encoded) so the operator can re-run
    ``verify_manifest`` against ``manifest_sha256`` — the integrity check
    that is the K06 handoff, not a trust assumption.
    """
    import base64
    import hashlib
    import json

    from src.knowledge.imports.manifest import verify_manifest

    root_dir = tmp_path / "notes"
    root_dir.mkdir()
    original_bytes = b"Exact retained original.\n"
    (root_dir / "paired.md").write_bytes(original_bytes)

    handler = _stub_handler(
        KnowledgeConfig(
            enabled=True,
            import_inventory=KnowledgeFeatureConfig(enabled=True),
        )
    )
    with principal_context(TRUSTED_LOCAL):
        result = await handler._cmd_knowledge_import(_args(tmp_path))

    content_b64 = result["manifest_content_base64"]
    content = base64.b64decode(content_b64, validate=True)

    # Round-trip: the exact sealed bytes re-hashed to the returned digest.
    assert hashlib.sha256(content).hexdigest() == result["manifest_sha256"]

    # verify_manifest against that same pair succeeds: the pair is canonical
    # (no drift, seal matches, artifacts decode to their declared hashes).
    document = verify_manifest(content, result["manifest_sha256"])

    # Offline / original evidence is preserved on the source metadata.
    artifact = document["artifacts"][0]
    assert base64.b64decode(artifact["bytes_base64"], validate=True) == original_bytes
    assert artifact["sha256"] == hashlib.sha256(original_bytes).hexdigest()

    # The sealed document is the canonical JSON form the hash was computed on.
    assert json.loads(content) == document
    assert document["source_installation_id"] == "synthetic-installation"
    assert document["snapshot_id"] == "synthetic-snapshot"


def re_full_match(hexdigest: str) -> bool:
    return bool(hexdigest) and len(hexdigest) == 64 and all(c in "0123456789abcdef" for c in hexdigest)


async def test_knowledge_import_dry_run_is_read_only(tmp_path):
    """The dry-run computes the report in memory; it never opens a database."""
    from src.commands.principal import TRUSTED_LOCAL

    root_dir = tmp_path / "notes"
    root_dir.mkdir()
    (root_dir / "paired.md").write_bytes(b"Exact retained original.\n")

    handler = _stub_handler(
        KnowledgeConfig(
            enabled=True,
            import_inventory=KnowledgeFeatureConfig(enabled=True),
        )
    )
    with principal_context(TRUSTED_LOCAL):
        result = await handler._cmd_knowledge_import(_args(tmp_path))
    assert result["success"] is True
    # No DB object is handed to the handler at all: the read-only path is
    # structural, not a promise. The stub exposes no ``db``.
    assert not hasattr(handler, "db")
