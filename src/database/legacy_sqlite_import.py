"""Migrate data from a SQLite database to PostgreSQL.

Used by the setup wizard when a user switches from SQLite to PostgreSQL
and wants to carry their existing data over.

Usage::

    await migrate_sqlite_to_postgres(
        "/home/user/.agent-queue/agent-queue.db",
        "postgresql://user:pass@localhost:5432/agent_queue",
    )
"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import Integer, inspect, insert, null, or_, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.asyncio import AsyncEngine

from src.database.engine import create_postgres_engine
from src.database.tables import (
    agent_profiles,
    agent_questions,
    agent_waits,
    agents,
    api_session_tokens,
    archived_tasks,
    chat_analyzer_suggestions,
    conversation_backfill_cursors,
    conversation_inputs,
    conversation_intake_gaps,
    dashboard_state_documents,
    digest_windows,
    doc_review_comments,
    doc_review_dispatches,
    doc_review_revisions,
    doc_reviews,
    epic_dependencies,
    escalation_actions,
    escalation_deliveries,
    escalation_messages,
    escalations,
    events,
    gates,
    hierarchy_migration_rejects,
    integration_attestation_publications,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_delegate_releases,
    integration_owner_recoveries,
    integration_candidate_member_results,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_child_dispositions,
    integration_cleanup_items,
    integration_episode_receipt_acceptances,
    integration_history_waiver_consumptions,
    integration_history_waivers,
    integration_legacy_deliveries,
    integration_legacy_gate_applicability,
    integration_legacy_suppression,
    integration_operation_artifact_pins,
    integration_outbox,
    integration_outbox_artifact_pins,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verification_evidence,
    integration_parent_verifications,
    integration_promotion_intents,
    integration_release_results,
    integration_repair_operations,
    integration_repair_stage_evidence,
    integration_repair_stages,
    integration_review_evidence,
    integration_rollout_transitions,
    integration_root_intent_members,
    job_outbox,
    job_workspace_pins,
    jobs,
    layout_dirty,
    layout_jobs,
    layout_reflow_requests,
    layout_tidy_request_pairs,
    layout_tidy_requests,
    merge_slots,
    message_discord_receipts,
    messages,
    metrics_samples,
    morning_report_coverage,
    morning_report_facts,
    morning_reports,
    outbound_deliveries,
    playbook_activations,
    playbook_artifacts,
    playbook_pending_events,
    playbook_step_receipts,
    playbook_v2_runs,
    playbook_waits,
    plugin_data,
    plugins,
    project_constraints,
    project_integration_leases,
    project_integration_schedules,
    project_layout_meta,
    project_onboarding_requests,
    projects,
    provider_availability,
    provider_availability_transitions,
    provider_usage_snapshots,
    rate_limits,
    repos,
    sessions,
    subagent_events,
    supervisor_conversations,
    supervisor_report_requests,
    system_config,
    task_assignment_routes,
    task_branch_origins,
    task_comments,
    task_completion_records,
    task_context,
    task_criteria,
    task_delivery_receipts,
    task_dependencies,
    task_gates,
    task_integration_checkpoints,
    task_labels,
    task_layout_cells,
    task_layouts,
    task_metadata,
    task_proposals,
    task_reroutes,
    task_results,
    task_session_attempts,
    task_subtasks,
    task_tools,
    task_workspace_requirements,
    tasks,
    token_ledger,
    transcript_checkpoints,
    workflows,
    workspace_kinds,
    workspaces,
)

logger = logging.getLogger(__name__)

# Tables in FK-safe insertion order.
#
# This list and _EXCLUDED_TABLES must cover every table in ``tables.metadata`` —
# an unlisted table is silently dropped data for anyone migrating a SQLite
# install to PostgreSQL. ``tests/test_migrate_sqlite_to_pg.py`` checks coverage.
#
# Circular and self-referential FKs are handled by inserting the offending
# columns as NULL (see ``_DEFERRED_COLS``) and restoring them afterwards.
# If a future table should not be copied, name it here with a reason so its
# omission is explicit and reviewable.
_EXCLUDED_TABLES: frozenset[str] = frozenset(
    {
        # PostgreSQL-era tables added after legacy SQLite databases stopped
        # existing (SQLite removal 1cae290cb, 2026-09-07 predates commit
        # fca6f0eb7, 2026-09-25): a legacy SQLite file can never contain
        # them, so there is nothing to import and copying them would only
        # fail on columns SQLite never had (JSON, partial unique indexes).
        "test_selections",
        "test_selection_observations",
        "test_selection_promotions",
        # Collaboration threads shipped after SQLite removal (revisions
        # a00000000033+): a legacy SQLite file can never contain them.
        "collaboration_threads",
        "collaboration_members",
        "collaboration_messages",
        # Revisions 44-46 were added after SQLite removal. Legacy files have
        # neither PR snapshots, object evaluation state, nor review images.
        "pull_request_inbox_snapshot",
        "object_loops",
        "doc_review_attachments",
        # Revision 47's monotonic benchmark measurements were never in SQLite.
        "benchmark_stage_spans",
        # Exact source-CI recovery shipped in PostgreSQL revision 48.
        "integration_source_ci",
        # Exact per-root operator authorizations shipped in revision 54.
        "integration_root_authorizations",
        # Reconciler subjects and their journal shipped in revision 57.
        "integration_subjects",
        "integration_subject_journal",
        # Shared operator decisions shipped in PostgreSQL revision 77.
        "operator_decisions",
        # Session-owned recurring prompts shipped in PostgreSQL revision 86;
        # legacy SQLite files predate schedules and their delivery diagnostics.
        "agent_cron",
        # Branch deletion audits (revision 88) and explicit branch retirement
        # requests (revision 89) shipped after SQLite removal.
        "branch_deletion_audit",
        "branch_retirements",
        # Durable record/knowledge tables shipped after SQLite removal in
        # revision 55. Legacy files have no record identities, revisions,
        # informational links or outbox state; backfill/import is explicit.
        "record_installation",
        "record_scopes",
        "records",
        "knowledge_records",
        "knowledge_revisions",
        "knowledge_revision_payloads",
        "knowledge_search",
        "record_link_heads",
        "record_link_versions",
        "task_record_link_state",
        "record_source_artifacts",
        "record_requests",
        "record_outbox",
        "record_consumer_receipts",
        "record_export_state",
        "record_backfill_state",
        # K05 protection state shipped in PostgreSQL revision 59. Legacy
        # SQLite has no proposals, authority grants, shares or erasure ledger;
        # these tables depend on the excluded record/revision domain above.
        "knowledge_proposals",
        "knowledge_authority_grants",
        "knowledge_global_shares",
        "knowledge_redactions",
        "knowledge_redaction_targets",
        # K06 sealed knowledge-import inventory shipped in revision 60, not
        # in the legacy SQLite format. Its runs depend on record_scopes.
        "record_import_runs",
        "record_legacy_mappings",
        "record_import_items",
        # K08 prepared context, observed delivery and citations shipped in
        # revision 63, not in the legacy SQLite format.
        "knowledge_context_bundles",
        "knowledge_context_deliveries",
        "knowledge_citations",
        # K12 derived provider index receipts shipped in revision 66. A legacy
        # SQLite file predates every record revision, so it has no index state;
        # the derived index is rebuilt by reindexing authorized records.
        "record_index_state",
        # K13 durable extraction jobs, receipts and budgets shipped in revision 64.
        "knowledge_extraction_jobs",
        "knowledge_extraction_inputs",
        "knowledge_capture_checkpoints",
        "knowledge_feature_budgets",
        "knowledge_budget_reservations",
        # K14 compatibility attempt counters shipped in PostgreSQL revision
        # 67, after SQLite removal; legacy files have no managed-source usage.
        "record_compatibility_usage",
        # Per-API-call transcript usage maxima shipped in revision 56.
        "transcript_usage_calls",
    }
)

# Nullable columns added after the legacy source format was retired. Missing
# provenance stays unknown; all other missing source columns still fail closed.
_POSTGRES_ONLY_COLUMNS = {
    "archived_tasks": frozenset({"route"}),
    "token_ledger": frozenset({"session_id", "attempt_id", "call_id", "model_source"}),
}

# Non-null PostgreSQL-era columns whose target default supplies missing values.
# Omit them from both the source read and target insert when absent, rather
# than inserting NULL. Existing source identities must travel unchanged.
_POSTGRES_DEFAULT_COLUMNS = {
    "tasks": frozenset({"legacy_completion_id"}),
    "archived_tasks": frozenset({"legacy_completion_id"}),
}


_ORDERED_TABLES = [
    # No FK dependencies
    system_config,
    agent_profiles,
    plugins,
    rate_limits,
    provider_usage_snapshots,
    provider_availability,
    provider_availability_transitions,
    events,
    project_onboarding_requests,
    workspace_kinds,
    playbook_artifacts,
    api_session_tokens,
    chat_analyzer_suggestions,
    archived_tasks,
    task_completion_records,
    task_comments,
    task_session_attempts,
    conversation_backfill_cursors,
    conversation_intake_gaps,
    # No FK to tasks: checklist rows survive archive like task_comments.
    task_subtasks,
    # Soft-referenced audit of retired integration delegates; no FKs.
    integration_delegate_releases,
    # Soft-referenced audit of integration owner recoveries; no FKs.
    integration_owner_recoveries,
    # Document reviews: soft task references, no FK to tasks; revisions and
    # comments reference doc_reviews.
    doc_reviews,
    doc_review_revisions,
    doc_review_comments,
    doc_review_dispatches,
    dashboard_state_documents,
    agent_questions,
    subagent_events,
    metrics_samples,
    transcript_checkpoints,
    playbook_pending_events,
    layout_dirty,
    layout_jobs,
    layout_reflow_requests,
    layout_tidy_requests,
    task_layout_cells,
    digest_windows,
    # Durable report authoring history; owner/session/message references are soft.
    supervisor_report_requests,
    # Durable morning report history, coverage cursors and cited evidence.
    # FK → morning_reports for facts; coverage uses a soft report reference.
    morning_reports,
    morning_report_coverage,
    morning_report_facts,
    # Frozen outbound payloads and delivery receipts; owner references are soft.
    outbound_deliveries,
    # Durable managed job contracts/results; owner/workspace references are soft.
    jobs,
    # FK → jobs; terminal receipt intents survive until delivery.
    job_outbox,
    # FK → playbook_artifacts
    playbook_activations,
    playbook_v2_runs,
    # FK → playbook_v2_runs
    playbook_step_receipts,
    playbook_waits,
    # FK → agent_profiles
    projects,
    # FK → projects
    layout_tidy_request_pairs,
    repos,
    project_layout_meta,
    gates,
    merge_slots,
    project_constraints,
    # FK → projects; durable conditions and result pointers survive restarts.
    agent_waits,
    # FK → projects, playbook_v2_runs
    workflows,
    # FK → projects (reply_to_id is a self-FK — deferred)
    messages,
    message_discord_receipts,
    # FK -> projects
    escalations,
    # FK -> escalations, messages
    escalation_messages,
    # FK -> escalations, escalation_messages
    escalation_actions,
    # FK -> escalations, escalation_messages
    escalation_deliveries,
    # FK -> messages (conversation_inputs also -> supervisor_conversations)
    supervisor_conversations,
    conversation_inputs,
    # FK → repos (current_task_id deferred)
    agents,
    # FK → projects, repos, agents, agent_profiles, workflows
    # (preferred_workspace_id and parent_task_id deferred)
    tasks,
    # Soft references to tasks on both columns; copy after the task rows.
    epic_dependencies,
    # FK → projects, playbook_v2_runs, tasks
    task_assignment_routes,
    # FK → projects, agents, tasks
    workspaces,
    # FK → jobs, workspaces; pins survive until verified process cleanup.
    job_workspace_pins,
    # FK → projects, tasks
    task_layouts,
    # FK → tasks
    task_criteria,
    task_dependencies,
    task_context,
    task_metadata,
    task_tools,
    task_labels,
    # No FKs.
    hierarchy_migration_rejects,
    task_workspace_requirements,
    # FK → gates, tasks
    task_gates,
    # FK → projects
    task_proposals,
    # FK → projects, tasks
    sessions,
    # FK → projects, agents, tasks
    token_ledger,
    task_results,
    task_reroutes,
    # hooks and hook_runs tables removed (playbooks spec §13 Phase 3)
    # FK → plugins
    plugin_data,
    # --- Hierarchical integration trains (topologically sorted by FK; no
    # --- pre-existing table references any of these and none is self-referential)
    # No FK dependencies
    integration_batches,
    integration_legacy_deliveries,
    integration_branch_owners,
    integration_candidate_revisions,
    integration_check_evidence,
    integration_outbox,
    integration_repair_stages,
    integration_review_evidence,
    project_integration_leases,
    project_integration_schedules,
    task_branch_origins,
    # FK → projects
    integration_history_waivers,
    integration_legacy_suppression,
    # FK → repos, tasks
    integration_parent_episodes,
    # FK → integration_review_evidence
    integration_batch_members,
    # FK → integration_candidate_revisions
    integration_candidate_publications,
    integration_promotion_intents,
    # FK → integration_outbox, playbook_artifacts
    integration_outbox_artifact_pins,
    # FK → integration_batches
    integration_release_results,
    # FK → integration_parent_episodes, tasks
    integration_repair_operations,
    # FK → integration_check_evidence, integration_repair_stages
    integration_repair_stage_evidence,
    # FK → integration_history_waivers, projects
    integration_rollout_transitions,
    # FK → integration_candidate_revisions, integration_check_evidence,
    #      integration_repair_operations, projects
    integration_attestation_publications,
    # FK → integration_batch_members, integration_candidate_revisions
    integration_candidate_member_results,
    # FK → integration_parent_episodes, integration_repair_operations
    integration_child_dispositions,
    # FK → integration_history_waivers, integration_rollout_transitions, projects
    integration_history_waiver_consumptions,
    # FK → gates, integration_history_waivers, integration_rollout_transitions, projects
    integration_legacy_gate_applicability,
    # FK → integration_repair_operations, playbook_artifacts
    integration_operation_artifact_pins,
    # FK → integration_parent_episodes, integration_repair_operations, tasks
    integration_parent_verifications,
    # FK → integration_candidate_member_results, integration_repair_stages,
    #      sessions, tasks, workspaces
    integration_candidate_resolutions,
    # FK → integration_parent_verifications, integration_repair_operations
    integration_parent_operation_completions,
    # FK → integration_check_evidence, integration_parent_verifications
    integration_parent_verification_evidence,
    # FK → integration_batch_members, integration_candidate_member_results,
    #      integration_promotion_intents, integration_review_evidence
    integration_root_intent_members,
    # FK → integration_batch_members, integration_candidate_member_results,
    #      integration_parent_episodes, integration_repair_operations
    task_delivery_receipts,
    # FK → integration_candidate_resolutions, integration_candidate_revisions
    integration_candidate_ref_mutations,
    # FK → integration_batches, projects, repos, task_delivery_receipts
    integration_cleanup_items,
    # FK → integration_parent_episodes, integration_parent_verifications,
    #      integration_repair_operations, task_delivery_receipts
    integration_episode_receipt_acceptances,
    # FK → integration_parent_episodes, integration_parent_operation_completions,
    #      integration_parent_verifications
    task_integration_checkpoints,
]

# Columns NULLed out on first insert because they point at a table that is
# inserted later (or at the same table), then restored by
# ``_fixup_deferred_columns``.  Keyed by table name.
_DEFERRED_COLS: dict[str, frozenset[str]] = {
    # agents ⇄ tasks circular FK
    "agents": frozenset({"current_task_id"}),
    # tasks → workspaces (inserted later) and tasks → tasks (self-FK)
    "tasks": frozenset({"preferred_workspace_id", "parent_task_id"}),
    # messages → messages (self-FK)
    "messages": frozenset({"reply_to_id"}),
}


def _read_only_sqlite_engine(path: str) -> AsyncEngine:
    """A minimal aiosqlite engine, for reading a legacy database only.

    The daemon no longer has a SQLite backend, so this module owns the three
    lines it needs rather than depending on an engine factory that no longer
    exists.  ``aiosqlite`` is an optional dependency (``pip install
    agent-queue[sqlite-import]``) precisely because this is the only caller.
    """
    try:
        import aiosqlite  # noqa: F401
    except ImportError:  # pragma: no cover - depends on the install
        raise ImportError(
            "Reading a legacy SQLite database needs aiosqlite. "
            'Install it with: pip install "agent-queue[sqlite-import]"'
        ) from None
    return create_async_engine(f"sqlite+aiosqlite:///{path}", future=True)


async def migrate_sqlite_to_postgres(
    sqlite_path: str,
    pg_dsn: str,
    *,
    progress_cb: Callable[[str, int], None] | None = None,
) -> dict[str, int]:
    """Copy all data from a SQLite database into PostgreSQL.

    Args:
        sqlite_path: Path to the SQLite database file.
        pg_dsn: PostgreSQL connection DSN.
        progress_cb: Optional callback ``(table_name, row_count)`` called
            after each table is migrated.

    Returns:
        Dict mapping table name to number of rows migrated.

    Raises:
        RuntimeError: If the PostgreSQL database already contains data.
    """
    sqlite_engine = _read_only_sqlite_engine(sqlite_path)
    pg_engine = create_postgres_engine(pg_dsn)

    try:
        await _check_pg_empty(pg_engine)
        counts = await _copy_tables(sqlite_engine, pg_engine, progress_cb)
        await _fixup_deferred_columns(sqlite_engine, pg_engine)
        await _reset_sequences(pg_engine)
        return counts
    finally:
        await sqlite_engine.dispose()
        await pg_engine.dispose()


async def _check_pg_empty(engine: AsyncEngine) -> None:
    """Refuse user data while preserving the schema's installation identity seed."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' "
                "AND tablename NOT IN ('alembic_version', 'record_installation')"
            )
        )
        for row in result:
            count_result = await conn.execute(
                text(f"SELECT COUNT(*) FROM {row[0]}")  # noqa: S608
            )
            if count_result.scalar() > 0:
                raise RuntimeError(
                    f"PostgreSQL table '{row[0]}' already contains data. "
                    "Aborting migration to avoid duplicates. "
                    "Drop the tables or use a fresh database."
                )


async def _copy_tables(
    src: AsyncEngine,
    dst: AsyncEngine,
    progress_cb: Callable[[str, int], None] | None,
) -> dict[str, int]:
    """Copy rows from each table in FK-safe order.

    SQLite databases may contain orphaned FK references (e.g. events pointing
    to archived/deleted tasks) because SQLite does not enforce FKs by default.
    We disable FK trigger checks during the bulk copy via PostgreSQL's
    ``session_replication_role = replica`` to handle this gracefully.
    """
    counts: dict[str, int] = {}

    async with dst.begin() as dst_conn:
        # Disable FK trigger checks for the duration of the bulk copy
        await dst_conn.execute(text("SET session_replication_role = replica"))

        for table in _ORDERED_TABLES:
            async with src.connect() as src_conn:
                projection = list(table.c)
                optional = _POSTGRES_ONLY_COLUMNS.get(table.name, frozenset())
                defaulted = _POSTGRES_DEFAULT_COLUMNS.get(table.name, frozenset())
                if optional or defaulted:
                    present = await src_conn.run_sync(
                        lambda conn: {column["name"] for column in inspect(conn).get_columns(table.name)}
                    )
                    projection = [
                        null().label(column.name)
                        if column.name in optional and column.name not in present else column
                        for column in table.c
                        if column.name not in defaulted or column.name in present
                    ]
                result = await src_conn.execute(select(*projection).select_from(table))
                rows = result.mappings().fetchall()

            if not rows:
                counts[table.name] = 0
                if progress_cb:
                    progress_cb(table.name, 0)
                continue

            # NULL out columns whose FK target is not inserted yet
            deferred = _DEFERRED_COLS.get(table.name)
            if deferred:
                rows = [{k: (None if k in deferred else v) for k, v in row.items()} for row in rows]

            await dst_conn.execute(insert(table), [dict(r) for r in rows])

            counts[table.name] = len(rows)
            if progress_cb:
                progress_cb(table.name, len(rows))
            logger.info("Migrated %d rows from %s", len(rows), table.name)

        # Re-enable FK trigger checks
        await dst_conn.execute(text("SET session_replication_role = DEFAULT"))

    return counts


async def _fixup_deferred_columns(src: AsyncEngine, dst: AsyncEngine) -> None:
    """Restore the ``_DEFERRED_COLS`` values that were NULLed during insert."""
    for table in _ORDERED_TABLES:
        names = _DEFERRED_COLS.get(table.name)
        if not names:
            continue

        pk_cols = list(table.primary_key.columns)
        deferred_cols = [table.c[name] for name in sorted(names)]

        async with src.connect() as src_conn:
            result = await src_conn.execute(
                select(*pk_cols, *deferred_cols).where(
                    or_(*[col.is_not(None) for col in deferred_cols])
                )
            )
            rows = result.fetchall()

        if not rows:
            continue

        async with dst.begin() as dst_conn:
            for row in rows:
                pk_values = row[: len(pk_cols)]
                stmt = update(table)
                for col, value in zip(pk_cols, pk_values):
                    stmt = stmt.where(col == value)
                stmt = stmt.values(dict(zip([c.name for c in deferred_cols], row[len(pk_cols) :])))
                await dst_conn.execute(stmt)

        logger.info(
            "Restored deferred columns %s for %d rows in %s",
            sorted(names),
            len(rows),
            table.name,
        )


async def _reset_sequences(engine: AsyncEngine) -> None:
    """Reset PostgreSQL sequences for tables with auto-increment integer PKs."""
    async with engine.begin() as conn:
        for table in _ORDERED_TABLES:
            # Find columns that are autoincrement Integer PKs
            for col in table.columns:
                if col.primary_key and isinstance(col.type, Integer) and col.autoincrement:
                    seq_name = f"{table.name}_{col.name}_seq"
                    max_val = await conn.execute(
                        text(f"SELECT COALESCE(MAX({col.name}), 0) FROM {table.name}")
                    )
                    max_id = max_val.scalar()
                    if max_id and max_id > 0:
                        await conn.execute(text(f"SELECT setval('{seq_name}', {max_id})"))
                        logger.info("Reset sequence %s to %d", seq_name, max_id)
