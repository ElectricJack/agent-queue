"""The current schema admits blocked Playbook V2 runs."""

import pytest
import sqlalchemy as sa

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration

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


async def test_baseline_admits_blocked_lifecycle():
    database = Database(lease_dsn("blocked-lifecycle"))
    await database.initialize()
    try:
        async with database._engine.begin() as conn:
            await conn.execute(_INSERT_ARTIFACT, {"sha": _ARTIFACT})
            await conn.execute(_INSERT_BLOCKED_RUN, {"sha": _ARTIFACT})
            assert (
                await conn.execute(
                    sa.text("SELECT lifecycle FROM playbook_v2_runs WHERE run_id='blocked-run'")
                )
            ).scalar_one() == "blocked"
    finally:
        await database.close()
