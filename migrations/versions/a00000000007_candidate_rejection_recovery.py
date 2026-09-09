"""Retain rejected candidate-repair pushes and permit one replacement.

Revision ID: a00000000007
Revises: a00000000006

A pushed candidate repair is external evidence even when its immutable Git
lineage fails validation.  The former one-row-per-member uniqueness made that
evidence an unrecoverable deadlock: it could neither be accepted nor replaced.
Rejected rows now remain immutable audit records, while a partial unique index
permits one current reservation for the frozen member.
"""

import sqlalchemy as sa
from alembic import op


revision = "a00000000007"
down_revision = "a00000000006"
branch_labels = None
depends_on = None

_TABLE = "integration_candidate_resolutions"
_INDEX = "uq_integration_candidate_resolutions_current_member"


def _columns(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}


def _constraints(bind) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints(_TABLE)
        if constraint.get("name")
    }


def _unique_constraints(bind) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(bind).get_unique_constraints(_TABLE)
        if constraint.get("name")
    }


def _indexes(bind) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if "rejection_evidence" not in _columns(bind):
        op.add_column(_TABLE, sa.Column("rejection_evidence", sa.JSON(), nullable=True))
    checks = _constraints(bind)
    for name in (
        "ck_integration_candidate_resolutions_state",
        "ck_integration_candidate_resolutions_push",
        "ck_integration_candidate_resolutions_rejection",
    ):
        if name in checks:
            op.drop_constraint(name, _TABLE, type_="check")
    op.create_check_constraint(
        "ck_integration_candidate_resolutions_state",
        _TABLE,
        "state IN ('reserved', 'pushed', 'accepted', 'rejected')",
    )
    op.create_check_constraint(
        "ck_integration_candidate_resolutions_push",
        _TABLE,
        "(state = 'reserved' AND push_evidence IS NULL) OR "
        "(state IN ('pushed', 'accepted', 'rejected') AND push_evidence IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_integration_candidate_resolutions_rejection",
        _TABLE,
        "(state = 'rejected' AND rejection_evidence IS NOT NULL) OR "
        "(state <> 'rejected' AND rejection_evidence IS NULL)",
    )
    if "uq_integration_candidate_resolutions_member" in _unique_constraints(bind):
        op.drop_constraint("uq_integration_candidate_resolutions_member", _TABLE, type_="unique")
    if _INDEX not in _indexes(bind):
        op.create_index(
            _INDEX,
            _TABLE,
            ["batch_id", "revision", "member_ordinal"],
            unique=True,
            postgresql_where=sa.text("state IN ('reserved', 'pushed', 'accepted')"),
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION integration_candidate_resolution_is_monotone()
        RETURNS trigger AS $$ BEGIN
            IF ROW(NEW.batch_id, NEW.revision, NEW.member_ordinal, NEW.operation_id,
              NEW.operation_episode_id, NEW.stage_ordinal, NEW.stage_deadline_at,
              NEW.project_id, NEW.repair_task_id, NEW.repair_session_id,
              NEW.repair_session_instance_token, NEW.repair_workspace_id,
              NEW.repair_workspace_path, NEW.repository_id, NEW.branch,
              NEW.target_branch, NEW.target_kind, NEW.fence_owner_id,
              NEW.fence_token, NEW.partial_head_sha, NEW.source_base_sha,
              NEW.source_head_sha, NEW.resolved_head_sha, NEW.resolved_tree_sha,
              NEW.repair_commit_shas::text) IS DISTINCT FROM
              ROW(OLD.batch_id, OLD.revision, OLD.member_ordinal, OLD.operation_id,
              OLD.operation_episode_id, OLD.stage_ordinal, OLD.stage_deadline_at,
              OLD.project_id, OLD.repair_task_id, OLD.repair_session_id,
              OLD.repair_session_instance_token, OLD.repair_workspace_id,
              OLD.repair_workspace_path, OLD.repository_id, OLD.branch,
              OLD.target_branch, OLD.target_kind, OLD.fence_owner_id,
              OLD.fence_token, OLD.partial_head_sha, OLD.source_base_sha,
              OLD.source_head_sha, OLD.resolved_head_sha, OLD.resolved_tree_sha,
              OLD.repair_commit_shas::text)
            THEN RAISE EXCEPTION 'candidate resolution identity is immutable'; END IF;
            IF OLD.handoff_owner_id IS NOT NULL AND
              ROW(NEW.handoff_owner_id, NEW.handoff_fence_token) IS DISTINCT FROM
              ROW(OLD.handoff_owner_id, OLD.handoff_fence_token)
            THEN RAISE EXCEPTION 'candidate resolution handoff is immutable'; END IF;
            IF (NEW.handoff_owner_id IS NULL) <> (NEW.handoff_fence_token IS NULL)
            THEN RAISE EXCEPTION 'candidate resolution handoff is incomplete'; END IF;
            IF OLD.push_evidence IS NOT NULL AND
              NEW.push_evidence::text IS DISTINCT FROM OLD.push_evidence::text
            THEN RAISE EXCEPTION 'candidate resolution push evidence is immutable'; END IF;
            IF OLD.rejection_evidence IS NOT NULL AND
              NEW.rejection_evidence::text IS DISTINCT FROM OLD.rejection_evidence::text
            THEN RAISE EXCEPTION 'candidate resolution rejection evidence is immutable'; END IF;
            IF (CASE NEW.state WHEN 'reserved' THEN 0 WHEN 'pushed' THEN 1 ELSE 2 END)
              NOT IN ((CASE OLD.state WHEN 'reserved' THEN 0 WHEN 'pushed' THEN 1 ELSE 2 END),
              (CASE OLD.state WHEN 'reserved' THEN 0 WHEN 'pushed' THEN 1 ELSE 2 END) + 1)
            THEN RAISE EXCEPTION 'candidate resolution transition is not adjacent'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    # Rejected evidence must not be discarded by a downgrade.
    raise NotImplementedError("candidate rejection evidence is durable")
