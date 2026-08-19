"""substrate: overhaul schema

Revision ID: 93a8a9e48fb8
Revises: e252a41eb210
Create Date: 2026-08-19 16:09:25.161326

Wave 0 of the framework overhaul: **DDL only**, landed once so the five
parallel Wave 1/2 lanes never touch the Alembic chain again.  See
``docs/analysis/execution-plan.md`` §1.1 and §2.

Deliberate deviation from the specs: work-graph §2 asks for four separate
revisions and worktree-execution §3 for its own.  Their *DDL* is merged
here to keep the chain single-headed across parallel agents.  Each spec's
remaining data/behavior steps keep their own later revisions.

Changes, by owning spec:

* work-graph §2.1–2.4 — ``task_dependencies.dep_type`` (+ widened PK,
  check constraint, composite indexes replacing the two single-column
  ones), ``tasks.is_blocked`` / ``archived_tasks.is_blocked``,
  ``idx_tasks_project_status_blocked``, ``idx_tasks_parent``, and the
  ``gates`` / ``task_gates`` / ``task_labels`` tables.
* worktree-execution §3 — ``workspace_kinds.mode`` / ``worktree_setup``,
  ``workspaces.slot_index`` / ``base_workspace_id`` + the partial unique
  index, and the ``merge_slots`` table.
* session-runtime §2 — the ``sessions`` table.
* supervisor-agent §3 — the ``messages`` table and six ``agent_profiles``
  named-session columns.
* aq-surface §4 — the ``api_session_tokens`` table.
* trust-and-ops §6.1 — ``token_ledger.model`` / ``input_tokens`` /
  ``output_tokens``.

The **only** data step is ``UPDATE workspace_kinds SET mode =
'exclusive-clone'`` (worktree-execution §3.1): the shipped column default
is ``'worktree'``, so without this every existing install would silently
change its git provisioning strategy on upgrade.  It runs in the same
transaction as the column add and, being before any new kind row can
exist, needs no guard.  The ``is_blocked`` backfill is **not** here — it
ships with the work-graph lane so that lane can iterate on its predicate.

Dialect notes:

* SQLite cannot alter a primary key, so the ``task_dependencies`` widen
  uses ``batch_alter_table(recreate="always", copy_from=...)`` — a full
  table copy.  ``copy_from`` spells the *old* schema explicitly rather
  than relying on reflection, so the unnamed ``task_id !=
  depends_on_task_id`` check and both foreign keys survive the rebuild.
  PostgreSQL takes plain ``add_column`` / ``drop_constraint`` /
  ``create_primary_key`` / ``create_check_constraint``.
* The ``workspaces`` partial unique index is written by hand
  (``sqlite_where`` / ``postgresql_where``) — autogenerate handles
  partial indexes poorly.
* Every NOT NULL column add carries a ``server_default`` so neither
  backend needs a separate backfill pass.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "93a8a9e48fb8"
down_revision: Union[str, Sequence[str], None] = "e252a41eb210"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Kept in sync with src.database.tables.TASK_DEP_TYPES.  Inlined rather than
# imported so the revision stays valid if the tuple later changes.
_DEP_TYPE_CHECK = (
    "dep_type IN ('blocks', 'parent-child', 'waits-for', 'conditional-blocks', "
    "'discovered-from', 'related', 'duplicates', 'supersedes')"
)

# PostgreSQL's auto-generated name for the unnamed PK created in the
# baseline revision (311e98c39ffa).
_PG_TASK_DEPS_PK = "task_dependencies_pkey"

_PARTIAL_SLOT_WHERE = "base_workspace_id IS NOT NULL AND slot_index IS NOT NULL"


def _task_dependencies_old() -> sa.Table:
    """The pre-revision ``task_dependencies`` schema, for SQLite batch copy."""
    return sa.Table(
        "task_dependencies",
        sa.MetaData(),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("depends_on_task_id", sa.Text(), nullable=False),
        sa.CheckConstraint("task_id != depends_on_task_id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["depends_on_task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("task_id", "depends_on_task_id"),
    )


def _task_dependencies_new() -> sa.Table:
    """The post-revision ``task_dependencies`` schema, for SQLite batch copy."""
    return sa.Table(
        "task_dependencies",
        sa.MetaData(),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("depends_on_task_id", sa.Text(), nullable=False),
        sa.Column("dep_type", sa.Text(), server_default="blocks", nullable=False),
        sa.CheckConstraint("task_id != depends_on_task_id"),
        sa.CheckConstraint(_DEP_TYPE_CHECK, name="ck_task_deps_dep_type"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["depends_on_task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("task_id", "depends_on_task_id", "dep_type"),
    )


def upgrade() -> None:
    """Upgrade schema (DDL only, plus the single workspace_kinds data step)."""
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # ── New tables ────────────────────────────────────────────────────
    op.create_table(
        "gates",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("gate_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), server_default="", nullable=False),
        sa.Column("await_id", sa.Text(), nullable=True),
        sa.Column("timeout_at", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "gate_type IN ('human', 'timer', 'pr-merged', 'ci-run', 'event', 'task')",
            name="ck_gates_type",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'expired')",
            name="ck_gates_status",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_gates_project_status", "gates", ["project_id", "status"], unique=False)
    op.create_index("idx_gates_status_type", "gates", ["status", "gate_type"], unique=False)

    op.create_table(
        "task_gates",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("gate_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["gate_id"], ["gates.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("task_id", "gate_id"),
    )
    op.create_index("idx_task_gates_gate", "task_gates", ["gate_id"], unique=False)

    op.create_table(
        "task_labels",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("task_id", "label"),
    )
    op.create_index("idx_task_labels_label", "task_labels", ["label"], unique=False)

    op.create_table(
        "merge_slots",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("holder_task_id", sa.Text(), nullable=True),
        sa.Column("acquired_at", sa.Float(), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("project_id"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("profile_id", sa.Text(), nullable=False),
        sa.Column("harness", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("lifecycle", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="starting", nullable=False),
        sa.Column("session_key", sa.Text(), nullable=True),
        sa.Column("work_dir", sa.Text(), nullable=False),
        sa.Column("epoch", sa.Text(), nullable=False),
        sa.Column("instance_token", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("last_activity", sa.Float(), nullable=True),
        sa.Column("restarts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quarantined_at", sa.Float(), nullable=True),
        sa.Column("sleep_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_sessions_name", "sessions", ["name"], unique=False)
    op.create_index("idx_sessions_state", "sessions", ["state"], unique=False)
    op.create_index("idx_sessions_task_id", "sessions", ["task_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("from_kind", sa.Text(), nullable=False),
        sa.Column("from_id", sa.Text(), nullable=False),
        sa.Column("to_kind", sa.Text(), nullable=False),
        sa.Column("to_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("delivered_at", sa.Float(), nullable=True),
        sa.Column("read_at", sa.Float(), nullable=True),
        sa.Column("archive_after_inject", sa.Integer(), server_default="0", nullable=False),
        sa.Column("archived_at", sa.Float(), nullable=True),
        sa.Column("reply_to_id", sa.Text(), nullable=True),
        sa.Column("via", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "from_kind IN ('session','user','system')",
            name="ck_messages_from_kind",
        ),
        sa.CheckConstraint(
            "to_kind IN ('session','task','profile','user')",
            name="ck_messages_to_kind",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["reply_to_id"], ["messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_messages_pending", "messages", ["to_kind", "to_id", "delivered_at"], unique=False
    )
    op.create_index(
        "idx_messages_project_created", "messages", ["project_id", "created_at"], unique=False
    )
    op.create_index("idx_messages_thread", "messages", ["thread_id"], unique=False)

    op.create_table(
        "api_session_tokens",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("project_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(
        "idx_api_session_tokens_expires", "api_session_tokens", ["expires_at"], unique=False
    )
    op.create_index(
        "idx_api_session_tokens_session", "api_session_tokens", ["session_id"], unique=False
    )

    # ── task_dependencies: typed edges + widened PK (work-graph §2.1) ──
    # Drop the old single-column indexes first so the SQLite table copy
    # below does not have to carry them.
    op.drop_index("idx_task_deps_depends_on", table_name="task_dependencies")
    op.drop_index("idx_task_deps_task_id", table_name="task_dependencies")

    if is_sqlite:
        # SQLite cannot ALTER a primary key — full table copy.
        with op.batch_alter_table(
            "task_dependencies",
            schema=None,
            recreate="always",
            copy_from=_task_dependencies_old(),
        ) as batch_op:
            batch_op.add_column(
                sa.Column("dep_type", sa.Text(), server_default="blocks", nullable=False)
            )
            batch_op.create_primary_key(
                "pk_task_dependencies", ["task_id", "depends_on_task_id", "dep_type"]
            )
            batch_op.create_check_constraint("ck_task_deps_dep_type", _DEP_TYPE_CHECK)
    else:
        op.add_column(
            "task_dependencies",
            sa.Column("dep_type", sa.Text(), server_default="blocks", nullable=False),
        )
        op.drop_constraint(_PG_TASK_DEPS_PK, "task_dependencies", type_="primary")
        op.create_primary_key(
            _PG_TASK_DEPS_PK,
            "task_dependencies",
            ["task_id", "depends_on_task_id", "dep_type"],
        )
        op.create_check_constraint(
            "ck_task_deps_dep_type", "task_dependencies", sa.text(_DEP_TYPE_CHECK)
        )

    op.create_index(
        "idx_task_deps_task_type", "task_dependencies", ["task_id", "dep_type"], unique=False
    )
    op.create_index(
        "idx_task_deps_depson_type",
        "task_dependencies",
        ["depends_on_task_id", "dep_type"],
        unique=False,
    )

    # ── Column adds (work-graph §2.2, supervisor-agent §3.2,
    #    trust-and-ops §6.1, worktree-execution §3.1–3.2) ───────────────
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_blocked", sa.Integer(), server_default="0", nullable=False)
        )
    op.create_index("idx_tasks_parent", "tasks", ["parent_task_id"], unique=False)
    op.create_index(
        "idx_tasks_project_status_blocked",
        "tasks",
        ["project_id", "status", "is_blocked"],
        unique=False,
    )

    with op.batch_alter_table("archived_tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_blocked", sa.Integer(), server_default="0", nullable=False)
        )

    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("harness", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("lifecycle", sa.Text(), server_default="task", nullable=False)
        )
        batch_op.add_column(sa.Column("mode", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("wake_mode", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("idle_timeout", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_session_age", sa.Integer(), nullable=True))

    with op.batch_alter_table("token_ledger", schema=None) as batch_op:
        batch_op.add_column(sa.Column("model", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))

    with op.batch_alter_table("workspace_kinds", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mode", sa.Text(), server_default="worktree", nullable=False))
        batch_op.add_column(
            sa.Column("worktree_setup", sa.Text(), server_default="[]", nullable=False)
        )

    # The one permitted data step (worktree-execution §3.1): every kind row
    # that exists at migration time keeps clone behavior, so no install
    # changes its git provisioning strategy on upgrade.  New rows get the
    # shipped 'worktree' default.  Runs before any new row can exist.
    op.execute(sa.text("UPDATE workspace_kinds SET mode = 'exclusive-clone'"))

    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(sa.Column("slot_index", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("base_workspace_id", sa.Text(), nullable=True))

    # Partial unique index — hand-written; autogenerate renders these badly.
    op.create_index(
        "uq_workspaces_base_slot",
        "workspaces",
        ["base_workspace_id", "slot_index"],
        unique=True,
        sqlite_where=sa.text(_PARTIAL_SLOT_WHERE),
        postgresql_where=sa.text(_PARTIAL_SLOT_WHERE),
    )


def downgrade() -> None:
    """Downgrade schema.

    Data in dropped tables/columns is lost (there is nowhere to put it).
    ``workspace_kinds.mode`` disappears with the column, so a subsequent
    re-upgrade re-runs the ``exclusive-clone`` backfill — which is the
    correct outcome for a rolled-back install.
    """
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    op.drop_index("uq_workspaces_base_slot", table_name="workspaces")
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_column("base_workspace_id")
        batch_op.drop_column("slot_index")

    with op.batch_alter_table("workspace_kinds", schema=None) as batch_op:
        batch_op.drop_column("worktree_setup")
        batch_op.drop_column("mode")

    with op.batch_alter_table("token_ledger", schema=None) as batch_op:
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("input_tokens")
        batch_op.drop_column("model")

    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.drop_column("max_session_age")
        batch_op.drop_column("idle_timeout")
        batch_op.drop_column("wake_mode")
        batch_op.drop_column("mode")
        batch_op.drop_column("lifecycle")
        batch_op.drop_column("harness")

    with op.batch_alter_table("archived_tasks", schema=None) as batch_op:
        batch_op.drop_column("is_blocked")

    op.drop_index("idx_tasks_project_status_blocked", table_name="tasks")
    op.drop_index("idx_tasks_parent", table_name="tasks")
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("is_blocked")

    # task_dependencies: narrow the PK back and drop dep_type.  Rows whose
    # dep_type is not 'blocks' would collide on the narrowed PK, so they are
    # removed first (they cannot exist before the work-graph lane lands).
    op.drop_index("idx_task_deps_depson_type", table_name="task_dependencies")
    op.drop_index("idx_task_deps_task_type", table_name="task_dependencies")
    op.execute(sa.text("DELETE FROM task_dependencies WHERE dep_type != 'blocks'"))

    if is_sqlite:
        with op.batch_alter_table(
            "task_dependencies",
            schema=None,
            recreate="always",
            copy_from=_task_dependencies_new(),
        ) as batch_op:
            batch_op.drop_constraint("ck_task_deps_dep_type", type_="check")
            batch_op.create_primary_key("pk_task_dependencies", ["task_id", "depends_on_task_id"])
            batch_op.drop_column("dep_type")
    else:
        op.drop_constraint("ck_task_deps_dep_type", "task_dependencies", type_="check")
        op.drop_constraint(_PG_TASK_DEPS_PK, "task_dependencies", type_="primary")
        op.create_primary_key(
            _PG_TASK_DEPS_PK, "task_dependencies", ["task_id", "depends_on_task_id"]
        )
        op.drop_column("task_dependencies", "dep_type")

    op.create_index(
        "idx_task_deps_depends_on", "task_dependencies", ["depends_on_task_id"], unique=False
    )
    op.create_index("idx_task_deps_task_id", "task_dependencies", ["task_id"], unique=False)

    op.drop_index("idx_api_session_tokens_session", table_name="api_session_tokens")
    op.drop_index("idx_api_session_tokens_expires", table_name="api_session_tokens")
    op.drop_table("api_session_tokens")

    op.drop_index("idx_messages_thread", table_name="messages")
    op.drop_index("idx_messages_project_created", table_name="messages")
    op.drop_index("idx_messages_pending", table_name="messages")
    op.drop_table("messages")

    op.drop_index("idx_sessions_task_id", table_name="sessions")
    op.drop_index("idx_sessions_state", table_name="sessions")
    op.drop_index("idx_sessions_name", table_name="sessions")
    op.drop_table("sessions")

    op.drop_table("merge_slots")

    op.drop_index("idx_task_labels_label", table_name="task_labels")
    op.drop_table("task_labels")

    op.drop_index("idx_task_gates_gate", table_name="task_gates")
    op.drop_table("task_gates")

    op.drop_index("idx_gates_status_type", table_name="gates")
    op.drop_index("idx_gates_project_status", table_name="gates")
    op.drop_table("gates")
