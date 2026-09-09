"""Allow an auditable successor for a never-written conflict resolution.

Revision ID: a00000000008
Revises: a00000000007

An earlier resolver could freeze a syntactically valid but absent commit. A
guarded push then refused it, leaving an immutable reservation that cannot be
corrected in place. This revision adds a durable pre-write marker and the two
links needed to retain that malformed reservation as audit evidence while a
fresh conflict intent is reserved under a new writer fence.

Fresh databases receive these fields from ``tables.metadata`` in the squashed
baseline. Every operation is therefore conditional for upgrade compatibility.
"""

import sqlalchemy as sa
from alembic import op


revision = "a00000000008"
down_revision = "a00000000007"
branch_labels = None
depends_on = None

_TABLE = "integration_promotion_intents"
_BINDING = "ck_integration_promotion_intents_resolution_binding"
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("resolution_push_started_at", sa.Float()),
    ("supersedes_intent_id", sa.Text()),
    ("superseded_by_intent_id", sa.Text()),
)
_BINDING_SQL = """
    (resolution_head_sha IS NULL AND resolution_tree_sha IS NULL AND
     resolution_commit_shas IS NULL AND resolution_operation_id IS NULL AND
     resolution_stage_ordinal IS NULL AND resolution_task_id IS NULL AND
     resolution_session_id IS NULL AND resolution_session_instance_token IS NULL AND
     resolution_workspace_id IS NULL AND resolution_fence_owner_id IS NULL AND
     resolution_fence_token IS NULL AND resolution_push_started_at IS NULL AND
     resolution_recovery_evidence IS NULL AND resolution_push_evidence IS NULL) OR
    (resolution_head_sha IS NOT NULL AND resolution_tree_sha IS NOT NULL AND
     resolution_commit_shas IS NOT NULL AND resolution_operation_id IS NOT NULL AND
     resolution_stage_ordinal IS NOT NULL AND resolution_task_id IS NOT NULL AND
     resolution_session_id IS NOT NULL AND resolution_session_instance_token IS NOT NULL AND
     resolution_workspace_id IS NOT NULL AND resolution_fence_owner_id IS NOT NULL AND
     resolution_fence_token IS NOT NULL AND state IN
     ('resolution_reserved', 'committed', 'superseded'))
"""


def upgrade() -> None:
    bind = op.get_bind()
    present = {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}
    for name, type_ in _COLUMNS:
        if name not in present:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))

    constraints = {
        item["name"]: item.get("sqltext", "")
        for item in sa.inspect(bind).get_check_constraints(_TABLE)
        if item.get("name")
    }
    current = constraints.get(_BINDING, "")
    if "resolution_push_started_at" not in current or "'superseded'" not in current:
        if _BINDING in constraints:
            op.drop_constraint(_BINDING, _TABLE, type_="check")
        op.create_check_constraint(_BINDING, _TABLE, sa.text(_BINDING_SQL))


def downgrade() -> None:
    # Retain compatible durable recovery evidence when stepping back.
    pass
