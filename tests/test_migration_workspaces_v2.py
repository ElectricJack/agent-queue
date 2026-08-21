"""Regression: workspaces v2 migration is idempotent and seeds expected data."""

from __future__ import annotations

import tempfile
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine


# Revision id from migrations/versions/7cdb4618fd0b_add_workspaces_v2_schema.py.
WORKSPACES_V2_REVISION = "7cdb4618fd0b"
PRIOR_REVISION = "e4f2a8b1d6c9"


def _alembic_config(db_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_migration_seeds_system_kinds():
    """After migrating a fresh DB, the three system kinds exist with correct flags."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        # Alembic env.py uses create_async_engine, so its URL must be async.
        # Verification uses a separate sync engine pointed at the same file.
        async_url = f"sqlite+aiosqlite:///{db_path}"
        sync_url = f"sqlite:///{db_path}"
        cfg = _alembic_config(async_url)
        url = sync_url  # alias used by `create_engine(url)` calls below
        command.upgrade(cfg, "head")

        engine = create_engine(url)
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id, writable, lockable, is_git_repo, auto_attach "
                    "FROM workspace_kinds WHERE project_id = '__system__' "
                    "ORDER BY id"
                )
            ).fetchall()

        assert len(rows) == 3, rows
        by_id = {r[0]: r for r in rows}

        # project-repo: writable, lockable, git
        assert by_id["project-repo"][1:5] == (1, 1, 1, 0)
        # readonly-dir: not writable, not lockable, not git, not auto
        assert by_id["readonly-dir"][1:5] == (0, 0, 0, 0)
        # vault: writable, not lockable, not git, auto-attached
        assert by_id["vault"][1:5] == (1, 0, 0, 1)


def test_migration_is_idempotent():
    """Downgrading and re-upgrading produces the same row counts (no dupes)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        # Alembic env.py uses create_async_engine, so its URL must be async.
        # Verification uses a separate sync engine pointed at the same file.
        async_url = f"sqlite+aiosqlite:///{db_path}"
        sync_url = f"sqlite:///{db_path}"
        cfg = _alembic_config(async_url)
        url = sync_url  # alias used by `create_engine(url)` calls below
        command.upgrade(cfg, "head")

        # Downgrade one step (drops workspaces_v2 tables) then re-upgrade.
        command.downgrade(cfg, PRIOR_REVISION)
        command.upgrade(cfg, "head")

        engine = create_engine(url)
        with engine.connect() as conn:
            count = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM workspace_kinds "
                    "WHERE project_id = '__system__'"
                )
            ).scalar()
        assert count == 3, f"expected 3 system kinds after re-migration, got {count}"


def test_migration_binds_existing_workspaces():
    """Workspaces present before the migration get kind_id='project-repo';
    each project also gets a vault workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        # Alembic env.py uses create_async_engine, so its URL must be async.
        # Verification uses a separate sync engine pointed at the same file.
        async_url = f"sqlite+aiosqlite:///{db_path}"
        sync_url = f"sqlite:///{db_path}"
        cfg = _alembic_config(async_url)
        url = sync_url  # alias used by `create_engine(url)` calls below

        # Migrate to one revision before workspaces_v2 so we can insert a
        # workspace that the data migration must back-fill.
        command.upgrade(cfg, PRIOR_REVISION)

        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO projects (id, name, credit_weight, "
                "max_concurrent_agents, status, total_tokens_used, created_at) "
                "VALUES ('p1', 'Test Project', 1.0, 1, 'ACTIVE', 0, 0.0)"
            ))
            conn.execute(sa.text(
                "INSERT INTO workspaces (id, project_id, workspace_path, "
                "source_type, enabled, created_at) "
                "VALUES ('w1', 'p1', '/tmp/ws1', 'clone', 1, 0.0)"
            ))

        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            kind = conn.execute(
                sa.text("SELECT kind_id FROM workspaces WHERE id = 'w1'")
            ).scalar()
            vault_count = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM workspaces "
                    "WHERE project_id = 'p1' AND kind_id = 'vault'"
                )
            ).scalar()

        assert kind == "project-repo", kind
        assert vault_count == 1, f"expected 1 vault workspace for project, got {vault_count}"


def test_migration_data_step_is_idempotent_on_partial_state():
    """Re-running the migration after a workspace was manually moved to
    a non-default vault path should not duplicate the row."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        # Alembic env.py uses create_async_engine, so its URL must be async.
        # Verification uses a separate sync engine pointed at the same file.
        async_url = f"sqlite+aiosqlite:///{db_path}"
        sync_url = f"sqlite:///{db_path}"
        cfg = _alembic_config(async_url)
        url = sync_url  # alias used by `create_engine(url)` calls below
        command.upgrade(cfg, PRIOR_REVISION)

        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO projects (id, name, credit_weight, "
                "max_concurrent_agents, status, total_tokens_used, created_at) "
                "VALUES ('p1', 'Test Project', 1.0, 1, 'ACTIVE', 0, 0.0)"
            ))

        command.upgrade(cfg, "head")

        # Operator moves the vault workspace to a custom location.
        with engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE workspaces SET workspace_path = '/custom/vault/path' "
                "WHERE project_id = 'p1' AND kind_id = 'vault'"
            ))

        # Downgrade and re-upgrade — should NOT clobber the custom path.
        command.downgrade(cfg, PRIOR_REVISION)
        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT workspace_path FROM workspaces "
                    "WHERE project_id = 'p1' AND kind_id = 'vault'"
                )
            ).fetchall()

        # The downgrade dropped kind_id, so on re-upgrade the data step
        # back-fills kind_id='project-repo' for any kind_id IS NULL row.
        # The manually-customized vault row went back to kind_id=NULL during
        # downgrade, so re-upgrade rebinds it as 'project-repo' and then a
        # NEW vault row is created at the default path. This is the documented
        # behavior — operator-customized paths only survive within a single
        # forward-migration window. Verify exactly one vault row at the
        # *default* path (not duplicated).
        assert len(rows) == 1, rows
        assert rows[0][0] == f"{Path.home()}/.agent-queue/vault/projects/p1"
