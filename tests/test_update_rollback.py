"""A tagged fixture proves A -> B -> A keeps the schema and durable rows."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

from src.database.schema_key import PROJECT_ROOT
from tests.pg_dsn import create_scratch_database

pytestmark = pytest.mark.migration


@pytest.fixture
def releases(tmp_path):
    repository = tmp_path / "releases"
    repository.mkdir()
    shutil.copytree(
        PROJECT_ROOT / "src",
        repository / "src",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
        ),
    )
    migrations = repository / "migrations"
    versions = migrations / "versions"
    versions.mkdir(parents=True)
    for file in ("__init__.py", "env.py", "integration_guards.py"):
        shutil.copyfile(PROJECT_ROOT / "migrations" / file, migrations / file)
    (versions / "__init__.py").touch()
    (repository / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s/migrations\n")
    (versions / "a00000000001_squashed_baseline.py").write_text(
        "from alembic import op\nfrom src.database.tables import metadata\n"
        "revision = 'fixture_a'\ndown_revision = None\nLEGACY_HEAD = 'legacy_fixture'\n"
        "def upgrade():\n    metadata.create_all(op.get_bind())\n"
        "def downgrade():\n    raise RuntimeError('never downgrade during rollback')\n"
    )

    def git(*args):
        subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.email", "fixture@example.test")
    git("config", "user.name", "Rollback fixture")
    git("add", ".")
    git("commit", "-m", "release A with schema-ahead support")
    git("tag", "-a", "v0.1.0", "-m", "release A")
    (versions / "fixture_b.py").write_text(
        "from alembic import op\nimport sqlalchemy as sa\n"
        "revision = 'fixture_b'\ndown_revision = 'fixture_a'\n"
        "def upgrade():\n"
        "    op.add_column('tasks', sa.Column('rollback_probe', sa.Text(), nullable=True))\n"
        "def downgrade():\n    raise RuntimeError('never downgrade during rollback')\n"
    )
    git("add", ".")
    git("commit", "-m", "release B adds nullable column")
    git("tag", "-a", "v0.2.0", "-m", "release B")
    return repository


async def test_script_rolls_back_previous_tag_without_losing_durable_rows(releases, tmp_path):
    import asyncpg

    dsn = await create_scratch_database("rollback_drill")
    seed = tmp_path / "seed.sql"
    seed.write_text("""
INSERT INTO projects (id, name, created_at) VALUES ('drill', 'Rollback drill', 1);
INSERT INTO tasks (id, project_id, title, description, status, created_at, updated_at, rollback_probe)
VALUES ('keep-task', 'drill', 'Keep task', 'Preserved description', 'READY', 1, 1, 'from newer release');
INSERT INTO events (event_type, project_id, task_id, payload, timestamp)
VALUES ('task.created', 'drill', 'keep-task', '{"keep": true}', 1);
INSERT INTO agent_waits (id, project_id, owner_kind, owner_id, session_id,
session_instance_token, claim_epoch, kind, match, created_at, deadline_at, idempotency_key)
VALUES ('keep-wait', 'drill', 'task', 'keep-task', 'session', 'instance', 1,
'timer', '{"until": 100}', 1, 100, 'keep-wait');
INSERT INTO operator_decisions (id, project_id, object_kind, object_id, effect, operator,
decision, source, source_ref, recorded_by, created_at, idempotency_key)
VALUES ('keep-approval', 'drill', 'task', 'keep-task', 'note', 'fixture',
'approved', 'cli', 'fixture', 'fixture', 1, 'keep-approval');
""")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "rollback_drill.py"),
        "--repository",
        str(releases),
        "--previous-tag",
        "v0.1.0",
        "--current-tag",
        "v0.2.0",
        "--seed-sql",
        str(seed),
        "--max-revisions",
        "1",
        env={**os.environ, "AQ_ROLLBACK_DRILL_DSN": dsn},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    assert process.returncode == 0, (stdout + stderr).decode()
    result = json.loads(stdout)
    assert result["success"] and result["database_preserved"]
    assert result["previous"] != result["current"]
    for table in ("tasks", "events", "agent_waits", "operator_decisions"):
        assert result["tables"][table]["rows"] == 1
    connection = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == "fixture_b"
        assert await connection.fetchval("SELECT rollback_probe FROM tasks") == "from newer release"
    finally:
        await connection.close()


async def test_script_refuses_nonempty_database_without_changing_rows(releases):
    import asyncpg

    from scripts.rollback_drill import drill

    dsn = await create_scratch_database("rollback_nonempty")
    connection = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await connection.execute("CREATE TABLE keep (id int); INSERT INTO keep VALUES (1)")
        with pytest.raises(ValueError, match="empty scratch"):
            await drill(releases, "v0.1.0", "v0.2.0", dsn)
        assert await connection.fetchval("SELECT id FROM keep") == 1
    finally:
        await connection.close()
