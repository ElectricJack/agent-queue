"""Squashed baseline — the whole schema in one revision.

The 117 revisions that preceded this one were collapsed on 2026-09-07, at the
same time SQLite support was removed.  Most of their weight was dual-backend
tax: 71 used ``op.batch_alter_table`` (which exists only because SQLite cannot
``ALTER TABLE``) and 29 branched on ``dialect.name``.  Replaying them cost
about eight seconds per fresh database and made every ``ScriptDirectory``
build parse 117 files.

Tables come from ``src.database.tables.metadata``. PostgreSQL trigger functions
are preserved separately in ``migrations.integration_guards`` because table
metadata does not represent their immutability and monotonicity enforcement.

**Existing databases are not asked to replay this.**  A database stamped at
the pre-squash head is stamped forward to this revision instead — see
``LEGACY_HEAD`` and ``src.database.engine._stamp_legacy_database``.  A database
stamped anywhere *earlier* than that head has an incomplete schema and must be
brought to the pre-squash head on the previous release first; the engine says
so rather than guessing.

Revision ID: a00000000001
Revises:
Create Date: 2026-09-07
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a00000000001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The single head of the pre-squash revision graph.  A database stamped here
#: already has this exact schema, so it is stamped forward rather than rebuilt.
LEGACY_HEAD = "6ad7aebb8c7c"


def upgrade() -> None:
    from migrations.integration_guards import install_integration_guards
    from src.database.tables import metadata

    bind = op.get_bind()
    metadata.create_all(bind)
    install_integration_guards(bind)
    _seed_system_workspace_kinds(bind)


def _seed_system_workspace_kinds(bind) -> None:
    """The three built-in workspace kinds (workspaces-v2 spec §3.2).

    Carried over from revision ``7cdb4618fd0b``.  ``create_all`` builds tables,
    not rows, and the orchestrator cannot acquire a workspace without these —
    so a squash that dropped them would produce a schema that looks complete
    and fails at the first task dispatch.
    """
    from src.database.tables import workspace_kinds

    now = time.time()
    rows = [
        {
            "project_id": "__system__",
            "id": "project-repo",
            "description": (
                "Default project repository — single writable, "
                "exclusively-locked clone of the project repo."
            ),
            "writable": True,
            "lockable": True,
            "is_git_repo": True,
            "repo_url": None,
            "default_lock_mode": "exclusive",
            "auto_attach": False,
            "created_at": now,
            "updated_at": now,
        },
        {
            "project_id": "__system__",
            "id": "vault",
            "description": (
                "Project vault — agent memory, notes, knowledge bases. "
                "Auto-attached to every task; not lockable."
            ),
            "writable": True,
            "lockable": False,
            "is_git_repo": False,
            "repo_url": None,
            "default_lock_mode": None,
            "auto_attach": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "project_id": "__system__",
            "id": "readonly-dir",
            "description": (
                "Read-only reference directory — docs, schemas, peer "
                "projects. Not writable, not lockable."
            ),
            "writable": False,
            "lockable": False,
            "is_git_repo": False,
            "repo_url": None,
            "default_lock_mode": None,
            "auto_attach": False,
            "created_at": now,
            "updated_at": now,
        },
    ]
    existing = {
        (r[0], r[1])
        for r in bind.execute(
            sa.select(workspace_kinds.c.project_id, workspace_kinds.c.id)
        ).fetchall()
    }
    for row in rows:
        if (row["project_id"], row["id"]) not in existing:
            bind.execute(workspace_kinds.insert().values(**row))


def downgrade() -> None:
    """Not supported: this is the baseline."""
    raise NotImplementedError("the squashed baseline has no downgrade")
