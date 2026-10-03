"""Additive K13 tables, separate from context/citation and prompt-budget schema."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

EXTRACTION_TABLE_NAMES = (
    "knowledge_extraction_jobs",
    "knowledge_extraction_inputs",
    "knowledge_capture_checkpoints",
    "knowledge_feature_budgets",
    "knowledge_budget_reservations",
)


def register_extraction_schema(metadata):
    """Register once on core metadata; importing this module alone has no effects."""
    if EXTRACTION_TABLE_NAMES[0] in metadata.tables:
        return
    jobs = Table(
        "knowledge_extraction_jobs",
        metadata,
        Column("job_id", UUID, nullable=False),
        Column("scope_key", Text, nullable=False),
        Column("source_identity", Text, nullable=False),
        Column("source_sha256", Text, nullable=False),
        Column("extractor_version", Text, nullable=False),
        Column("policy_version", Text, nullable=False),
        Column("state", Text, nullable=False, server_default="pending"),
        Column("attempts", Integer, nullable=False, server_default="0"),
        Column("lease_token", UUID),
        Column("lease_until", DateTime(timezone=True)),
        Column("available_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("result_artifact_id", Text),
        Column("error_code", Text),
        Column("budget_reservation_id", UUID),
        PrimaryKeyConstraint("job_id", name="pk_knowledge_extraction_jobs"),
        ForeignKeyConstraint(
            ["scope_key"],
            ["record_scopes.scope_key"],
            name="fk_knowledge_extraction_jobs_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "scope_key",
            "source_identity",
            "source_sha256",
            "extractor_version",
            "policy_version",
            name="uq_knowledge_extraction_jobs_source",
        ),
        UniqueConstraint("job_id", "scope_key", name="uq_knowledge_extraction_jobs_scope"),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_knowledge_extraction_jobs_hash"
        ),
        CheckConstraint(
            "length(btrim(source_identity)) > 0 AND "
            "length(btrim(extractor_version)) > 0 AND length(btrim(policy_version)) > 0",
            name="ck_knowledge_extraction_jobs_identity",
        ),
        CheckConstraint(
            "state IN ('pending','leased','succeeded','retry','quarantined','cancelled')",
            name="ck_knowledge_extraction_jobs_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_knowledge_extraction_jobs_attempts"),
        CheckConstraint(
            "(state = 'leased' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) "
            "OR (state <> 'leased' AND lease_token IS NULL AND lease_until IS NULL)",
            name="ck_knowledge_extraction_jobs_lease",
        ),
    )
    Index(
        "idx_knowledge_extraction_jobs_due",
        jobs.c.scope_key,
        jobs.c.available_at,
        jobs.c.job_id,
        postgresql_where=jobs.c.state.in_(("pending", "retry", "leased")),
    )
    Table(
        "knowledge_extraction_inputs",
        metadata,
        Column("job_id", UUID, nullable=False),
        Column("input_ordinal", Integer, nullable=False),
        Column("event_id", BigInteger, nullable=False),
        Column("attempt_id", Text),
        Column("artifact_id", Text, nullable=False),
        Column("source_scope", Text, nullable=False),
        Column("actor_id", Text, nullable=False),
        PrimaryKeyConstraint("job_id", "input_ordinal", name="pk_knowledge_extraction_inputs"),
        ForeignKeyConstraint(
            ["job_id", "source_scope"],
            ["knowledge_extraction_jobs.job_id", "knowledge_extraction_jobs.scope_key"],
            name="fk_knowledge_extraction_inputs_job_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "input_ordinal BETWEEN 0 AND 7 AND event_id >= 0",
            name="ck_knowledge_extraction_inputs_position",
        ),
        CheckConstraint(
            "length(btrim(artifact_id)) > 0 AND length(btrim(actor_id)) > 0",
            name="ck_knowledge_extraction_inputs_receipt",
        ),
    )
    Table(
        "knowledge_capture_checkpoints",
        metadata,
        Column("consumer_id", Text, nullable=False),
        Column("scope_key", Text, nullable=False),
        Column("last_event_id", BigInteger, nullable=False),
        Column(
            "last_reconcile_at", DateTime(timezone=True), nullable=False, server_default=func.now()
        ),
        PrimaryKeyConstraint("consumer_id", "scope_key", name="pk_knowledge_capture_checkpoints"),
        ForeignKeyConstraint(
            ["scope_key"],
            ["record_scopes.scope_key"],
            name="fk_knowledge_capture_checkpoints_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint("last_event_id >= 0", name="ck_knowledge_capture_checkpoints_cursor"),
        CheckConstraint(
            "length(btrim(consumer_id)) > 0", name="ck_knowledge_capture_checkpoints_consumer"
        ),
    )
    Table(
        "knowledge_feature_budgets",
        metadata,
        Column("scope_key", Text, nullable=False),
        Column("feature", Text, nullable=False),
        Column("period_start", DateTime(timezone=True), nullable=False),
        Column("limit_microusd", BigInteger, nullable=False, server_default="0"),
        Column("reserved_microusd", BigInteger, nullable=False, server_default="0"),
        Column("spent_microusd", BigInteger, nullable=False, server_default="0"),
        Column("token_limit", BigInteger, nullable=False, server_default="0"),
        Column("reserved_tokens", BigInteger, nullable=False, server_default="0"),
        Column("spent_tokens", BigInteger, nullable=False, server_default="0"),
        Column("circuit_open", Boolean, nullable=False, server_default=false()),
        PrimaryKeyConstraint(
            "scope_key", "feature", "period_start", name="pk_knowledge_feature_budgets"
        ),
        ForeignKeyConstraint(
            ["scope_key"],
            ["record_scopes.scope_key"],
            name="fk_knowledge_feature_budgets_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "limit_microusd >= 0 AND reserved_microusd >= 0 AND spent_microusd >= 0 "
            "AND token_limit >= 0 AND reserved_tokens >= 0 AND spent_tokens >= 0",
            name="ck_knowledge_feature_budgets_counters",
        ),
        CheckConstraint(
            "period_start = date_trunc('day', period_start AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
            name="ck_knowledge_feature_budgets_utc_day",
        ),
        CheckConstraint("length(btrim(feature)) > 0", name="ck_knowledge_feature_budgets_feature"),
    )
    Table(
        "knowledge_budget_reservations",
        metadata,
        Column("reservation_id", UUID, nullable=False),
        Column("job_id", UUID, nullable=False),
        Column("scope_key", Text, nullable=False),
        Column("feature", Text, nullable=False),
        Column("period_start", DateTime(timezone=True), nullable=False),
        Column("estimated_microusd", BigInteger, nullable=False),
        Column("estimated_tokens", BigInteger, nullable=False),
        Column("actual_microusd", BigInteger),
        Column("actual_tokens", BigInteger),
        Column("state", Text, nullable=False, server_default="reserved"),
        Column("provider_operation_id", Text),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        PrimaryKeyConstraint("reservation_id", name="pk_knowledge_budget_reservations"),
        UniqueConstraint("job_id", name="uq_knowledge_budget_reservations_job"),
        UniqueConstraint("reservation_id", "job_id", name="uq_knowledge_budget_reservations_owner"),
        ForeignKeyConstraint(
            ["job_id", "scope_key"],
            ["knowledge_extraction_jobs.job_id", "knowledge_extraction_jobs.scope_key"],
            name="fk_knowledge_budget_reservations_job_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scope_key", "feature", "period_start"],
            [
                "knowledge_feature_budgets.scope_key",
                "knowledge_feature_budgets.feature",
                "knowledge_feature_budgets.period_start",
            ],
            name="fk_knowledge_budget_reservations_budget",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "estimated_microusd >= 0 AND estimated_tokens BETWEEN 0 AND 8000 "
            "AND (actual_microusd IS NULL OR actual_microusd >= 0) "
            "AND (actual_tokens IS NULL OR actual_tokens >= 0)",
            name="ck_knowledge_budget_reservations_amounts",
        ),
        CheckConstraint(
            "state IN ('reserved','settled','unknown','released')",
            name="ck_knowledge_budget_reservations_state",
        ),
        CheckConstraint(
            "(state = 'settled' AND actual_microusd IS NOT NULL "
            "AND actual_tokens IS NOT NULL AND provider_operation_id IS NOT NULL) "
            "OR (state <> 'settled' AND actual_microusd IS NULL AND actual_tokens IS NULL)",
            name="ck_knowledge_budget_reservations_actuals",
        ),
        CheckConstraint(
            "state <> 'unknown' OR provider_operation_id IS NOT NULL",
            name="ck_knowledge_budget_reservations_unknown",
        ),
    )
    # The nullable reverse pointer must refer to this job's own reservation.
    # use_alter handles the cycle during metadata create/drop on a fresh baseline.
    jobs.append_constraint(
        ForeignKeyConstraint(
            ["budget_reservation_id", "job_id"],
            [
                "knowledge_budget_reservations.reservation_id",
                "knowledge_budget_reservations.job_id",
            ],
            name="fk_knowledge_extraction_jobs_reservation",
            use_alter=True,
            ondelete="RESTRICT",
        )
    )
