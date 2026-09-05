"""Migration coverage for the Playbook V2 ``blocked`` run lifecycle."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = pytest.mark.migration

ROOT = Path(__file__).resolve().parents[1]
PRIOR_REVISION = "8b4d2f7c1a90"
BLOCKED_REVISION = "c7d8e9f0a1b2"
POSTGRES_DSN = ensure_worker_postgres_dsn()

_ARTIFACT = "sha256:" + "d" * 64
_INSERT_ARTIFACT = sa.text(
    "INSERT INTO playbook_artifacts (artifact_sha256, playbook_id, scope, "
    "scope_identifier, schema_generation, version, source_digest, contract_fingerprint, "
    "profile_fingerprint, compiler_build, path, size_bytes, validation, created_at) VALUES "
    "(:sha, 'p', 'system', '', 2, 1, :sha, :sha, '', 'test', '/tmp/test.json', 2, '{}', 1.0)"
)
_INSERT_BLOCKED_RUN = sa.text(
    "INSERT INTO playbook_v2_runs (run_id, playbook_id, artifact_sha256, rule_id, "
    "lifecycle, mode, snapshot_version, snapshot, snapshot_bytes, event_type, summary, "
    "started_at, updated_at, completed_at) VALUES "
    "('blocked-run', 'p', :sha, 'rule', 'blocked', 'live', 0, '{}', 2, '', '', 1.0, 1.0, 1.0)"
)


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _alembic_pg(dsn: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=dsn),
        capture_output=True,
        text=True,
        check=False,
    )


def test_blocked_lifecycle_sqlite_upgrade_and_guarded_downgrade(tmp_path):
    db_path = tmp_path / "blocked-lifecycle.db"
    config = _config(f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(_INSERT_ARTIFACT, {"sha": _ARTIFACT})
            conn.execute(_INSERT_BLOCKED_RUN, {"sha": _ARTIFACT})
        with pytest.raises(RuntimeError, match="blocked playbook runs exist"):
            command.downgrade(config, PRIOR_REVISION)

        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM playbook_v2_runs WHERE lifecycle = 'blocked'"))
        command.downgrade(config, PRIOR_REVISION)
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(_INSERT_BLOCKED_RUN, {"sha": _ARTIFACT})
        command.upgrade(config, BLOCKED_REVISION)
    finally:
        engine.dispose()


@pytest.mark.integration
async def test_blocked_lifecycle_postgres_upgrade_and_guarded_downgrade():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")
    import asyncpg

    dsn = await create_scratch_database("pbv2blocked")
    result = _alembic_pg(dsn, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute(
            "INSERT INTO playbook_artifacts (artifact_sha256, playbook_id, scope, "
            "scope_identifier, schema_generation, version, source_digest, contract_fingerprint, "
            "profile_fingerprint, compiler_build, path, size_bytes, validation, created_at) "
            "VALUES ($1, 'p', 'system', '', 2, 1, $1, $1, '', 'test', '/tmp/test.json', "
            "2, '{}', 1.0)",
            _ARTIFACT,
        )
        await conn.execute(
            "INSERT INTO playbook_v2_runs (run_id, playbook_id, artifact_sha256, rule_id, "
            "lifecycle, mode, snapshot_version, snapshot, snapshot_bytes, event_type, summary, "
            "started_at, updated_at, completed_at) VALUES ('blocked-run', 'p', $1, 'rule', "
            "'blocked', 'live', 0, '{}', 2, '', '', 1.0, 1.0, 1.0)",
            _ARTIFACT,
        )
    finally:
        await conn.close()

    refused = _alembic_pg(dsn, "downgrade", PRIOR_REVISION)
    assert refused.returncode != 0
    assert "blocked playbook runs exist" in refused.stderr

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == BLOCKED_REVISION
        await conn.execute("DELETE FROM playbook_v2_runs WHERE lifecycle = 'blocked'")
    finally:
        await conn.close()
    downgraded = _alembic_pg(dsn, "downgrade", PRIOR_REVISION)
    assert downgraded.returncode == 0, downgraded.stderr
