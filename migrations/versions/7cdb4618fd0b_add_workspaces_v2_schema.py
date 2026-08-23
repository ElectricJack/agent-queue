"""add workspaces_v2 schema + seed system kinds + bind existing workspaces

Revision ID: 7cdb4618fd0b
Revises: e4f2a8b1d6c9
Create Date: 2026-05-07 17:50:19.366738

Schema changes:
1. Create workspace_kinds (composite PK (project_id, id); '__system__' sentinel
   for system rows so PK columns can be NOT NULL on Postgres).
2. Create task_workspace_requirements (composite PK (task_id, kind_id, position)).
3. Add nullable workspaces.kind_id (tightened to NOT NULL in a follow-up migration).

Data migration (idempotent — safe to re-run):
1. Seed __system__ kinds: project-repo, vault, readonly-dir.
2. Bind existing workspaces to kind_id='project-repo' (WHERE kind_id IS NULL).
3. Provision per-project vault workspaces (skip if any vault-kind workspace exists).

See docs/specs/design/workspaces-v2.md §3 + §9.
"""
from typing import Sequence, Union

import os
import time
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import column, select as sa_select, table


# revision identifiers, used by Alembic.
revision: str = '7cdb4618fd0b'
down_revision: Union[str, Sequence[str], None] = 'e4f2a8b1d6c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema + seed data."""
    # ── Schema ────────────────────────────────────────────────────────
    op.create_table('workspace_kinds',
    sa.Column('project_id', sa.Text(), nullable=False),
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('writable', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('lockable', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('is_git_repo', sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column('repo_url', sa.Text(), nullable=True),
    sa.Column('default_lock_mode', sa.Text(), nullable=True),
    sa.Column('auto_attach', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('project_id', 'id')
    )
    op.create_table('task_workspace_requirements',
    sa.Column('task_id', sa.Text(), nullable=False),
    sa.Column('kind_id', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), server_default='0', nullable=False),
    sa.Column('alias', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
    sa.PrimaryKeyConstraint('task_id', 'kind_id', 'position')
    )
    with op.batch_alter_table('task_workspace_requirements', schema=None) as batch_op:
        batch_op.create_index('idx_task_ws_reqs_task_id', ['task_id'], unique=False)

    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind_id', sa.Text(), nullable=True))

    # ── Data migration (spec §9.2, idempotent) ────────────────────────
    bind = op.get_bind()
    now = time.time()

    wk = table(
        "workspace_kinds",
        column("project_id"), column("id"), column("description"),
        column("writable"), column("lockable"), column("is_git_repo"),
        column("repo_url"), column("default_lock_mode"), column("auto_attach"),
        column("created_at"), column("updated_at"),
    )
    ws = table(
        "workspaces",
        column("id"), column("project_id"), column("workspace_path"),
        column("source_type"), column("name"), column("kind_id"),
        column("locked_by_agent_id"), column("locked_by_task_id"),
        column("locked_at"), column("lock_mode"), column("enabled"),
        column("created_at"),
    )
    proj = table("projects", column("id"))

    # Step 1: Seed system kinds — match key (project_id='__system__', id).
    system_kinds = [
        dict(project_id="__system__", id="project-repo",
             description="Default project repository — single writable, "
                         "exclusively-locked clone of the project repo.",
             writable=True, lockable=True, is_git_repo=True,
             repo_url=None, default_lock_mode="exclusive", auto_attach=False,
             created_at=now, updated_at=now),
        dict(project_id="__system__", id="vault",
             description="Project vault — agent memory, notes, knowledge bases. "
                         "Auto-attached to every task; not lockable.",
             writable=True, lockable=False, is_git_repo=False,
             repo_url=None, default_lock_mode=None, auto_attach=True,
             created_at=now, updated_at=now),
        dict(project_id="__system__", id="readonly-dir",
             description="Read-only reference directory — docs, schemas, peer "
                         "projects. Not writable, not lockable.",
             writable=False, lockable=False, is_git_repo=False,
             repo_url=None, default_lock_mode=None, auto_attach=False,
             created_at=now, updated_at=now),
    ]
    existing_kinds = {
        (r[0], r[1])
        for r in bind.execute(sa_select(wk.c.project_id, wk.c.id)).fetchall()
    }
    for k in system_kinds:
        if (k["project_id"], k["id"]) not in existing_kinds:
            bind.execute(wk.insert().values(**k))

    # Step 2: Bind existing workspaces to project-repo (idempotent — WHERE NULL).
    bind.execute(
        ws.update()
          .where(ws.c.kind_id.is_(None))
          .values(kind_id="project-repo")
    )

    # Step 3: Provision per-project vault workspaces — skip if any vault-kind
    # workspace exists for the project (preserves operator-customized paths).
    project_ids = [row[0] for row in bind.execute(sa_select(proj.c.id)).fetchall()]
    existing_vault = {
        row[0]
        for row in bind.execute(
            sa_select(ws.c.project_id).where(ws.c.kind_id == "vault")
        ).fetchall()
    }
    vault_root = os.path.expanduser("~/.agent-queue/vault/projects")
    for pid in project_ids:
        if pid in existing_vault:
            continue
        bind.execute(ws.insert().values(
            id=f"vault-{pid}-{uuid.uuid4().hex[:8]}",
            project_id=pid,
            workspace_path=os.path.join(vault_root, pid),
            source_type="link",
            name="vault",
            kind_id="vault",
            locked_by_agent_id=None,
            locked_by_task_id=None,
            locked_at=None,
            lock_mode=None,
            enabled=True,
            created_at=now,
        ))


def downgrade() -> None:
    """Downgrade schema. Data in dropped tables/column is lost — no data downgrade."""
    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.drop_column('kind_id')

    with op.batch_alter_table('task_workspace_requirements', schema=None) as batch_op:
        batch_op.drop_index('idx_task_ws_reqs_task_id')

    op.drop_table('task_workspace_requirements')
    op.drop_table('workspace_kinds')
