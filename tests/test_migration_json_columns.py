# tests/test_migration_json_columns.py
"""No new column may be typed plain ``json``; declare ``JSONB``.

PostgreSQL's ``json`` has no equality operator.  A query that compares whole
rows of a table carrying one -- ``SELECT DISTINCT t.*``, ``UNION``, ``GROUP BY``
over the row -- fails at plan time with ``could not identify an equality
operator for type json``, whatever the data.  Revision ``a00000000039`` added
``tasks.route`` as ``json``; the scheduler's ``SELECT DISTINCT tasks.*`` then
failed every cycle and no pool started a session until the column was retyped
(outage 2026-09-28, revision ``a00000000040``).

The columns below predate this check.  None is compared as a whole row today
(audited 2026-09-28), and some hold digest-bearing snapshots whose key order
and duplicate keys ``jsonb`` would not keep, so retyping one is a decision for
its owner, not a sweep.  The lists only shrink: retyping a column means
removing it here.

Like :mod:`tests.test_migration_boolean_defaults` this is a pure source and
metadata scan, so it runs in the default suite on every box.
"""

from __future__ import annotations

import ast
import pathlib
import re

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from src.database.tables import metadata

ROOT = pathlib.Path(__file__).resolve().parent.parent
REVISIONS = sorted((ROOT / "migrations" / "versions").glob("*.py"))

#: ``table -> columns`` still typed ``json`` in :mod:`src.database.tables`.
GRANDFATHERED_COLUMNS: dict[str, tuple[str, ...]] = {
    "agent_waits": ("match", "digest"),
    "collaboration_threads": ("final_result",),
    "dashboard_state_documents": ("value",),
    "digest_windows": ("activity_cursor", "payload"),
    "doc_review_comments": ("heading_path",),
    "doc_review_revisions": ("playbook",),
    "escalation_actions": ("parameters", "result"),
    "escalation_deliveries": ("payload",),
    "escalations": ("choices", "terminal_evidence"),
    "integration_batch_members": ("review_evidence",),
    "integration_batches": ("policy_snapshot", "artifact_snapshot"),
    "integration_candidate_member_results": ("conflict_evidence",),
    "integration_candidate_resolutions": (
        "repair_commit_shas",
        "push_evidence",
        "rejection_evidence",
    ),
    "integration_check_evidence": ("checks",),
    "integration_delegate_releases": ("cleanup",),
    "integration_legacy_suppression": ("policy_snapshot",),
    "integration_outbox": ("payload", "destination_manifest"),
    "integration_owner_recoveries": ("evidence",),
    "integration_promotion_intents": (
        "review_evidence",
        "authors",
        "provenance",
        "commit_metadata",
        "conflict_diagnostics",
        "resolution_commit_shas",
        "resolution_recovery_evidence",
        "resolution_push_evidence",
        "remote_evidence",
    ),
    "integration_repair_operations": ("policy_snapshot", "artifact_snapshot"),
    "integration_repair_stages": (
        "policy",
        "current_subject",
        "success_subject",
        "retained_handoff",
        "dossier",
    ),
    "integration_review_evidence": ("evidence",),
    "integration_rollout_transitions": ("old_legacy_policy", "new_legacy_policy"),
    "integration_root_intent_members": ("result_evidence",),
    "job_outbox": ("payload",),
    "jobs": ("argv", "contract", "measurements", "result"),
    "morning_reports": (
        "config_snapshot",
        "build_context",
        "brief",
        "source_cursors",
        "source_heads",
        "fallback",
        "report",
        "coverage",
    ),
    "outbound_deliveries": ("destination", "payload"),
    "project_onboarding_requests": ("created_resources", "result", "error"),
    "projects": ("hierarchical_integration_policy",),
    "provider_availability": ("evidence",),
    "provider_availability_transitions": ("detail",),
    "supervisor_conversations": ("audience",),
    "supervisor_report_requests": ("visibility", "brief", "evidence_refs", "source_links"),
    "task_delivery_receipts": ("review_evidence", "verification_evidence", "resolution_evidence"),
    "test_selection_observations": ("executed_modules", "failed_node_ids", "payload"),
    "test_selection_promotions": ("evidence",),
    "test_selections": (
        "area_decisions",
        "mandatory_modules",
        "static_modules",
        "jev_modules",
        "fallback_modules",
        "final_modules",
        "reasons",
        "pending_obligations",
        "argv",
        "elapsed_ms",
        "usage",
    ),
}

#: ``(revision file, column)`` pairs that declare a ``json`` column.
#: ``a00000000039``'s ``route`` is retyped by ``a00000000040``.
GRANDFATHERED_REVISION_COLUMNS = frozenset(
    {
        ("a00000000006_legacy_resolution_recovery_evidence.py", "resolution_recovery_evidence"),
        ("a00000000007_candidate_rejection_recovery.py", "rejection_evidence"),
        ("a0000000000c_development_integration.py", "manifest"),
        ("a0000000000c_development_integration.py", "evidence"),
        ("a00000000036_review_playbook_pin.py", "playbook"),
        ("a00000000039_task_route_source.py", "route"),
    }
)

_FIX = "declare it sqlalchemy.dialects.postgresql.JSONB, not sa.JSON"
#: Raw DDL that names ``json`` (and not ``jsonb``) as a column type.
_RAW_JSON_DDL = re.compile(r"(?is)\b(?:add\s+column|create\s+table|type)\b[^;]*?\bjson\b(?!b)")


def _plain_json_columns() -> set[str]:
    return {
        f"{table.name}.{column.name}"
        for table in metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, sa.JSON) and not isinstance(column.type, JSONB)
    }


def _grandfathered() -> set[str]:
    return {f"{t}.{c}" for t, columns in GRANDFATHERED_COLUMNS.items() for c in columns}


def _name_of(node: ast.AST) -> str | None:
    """Trailing identifier of a Name/Attribute/Call, e.g. ``sa.JSON()`` -> JSON."""
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _revision_json_columns(path: pathlib.Path) -> list[tuple[str, int]]:
    """``(column or call, line)`` for each ``json`` column a revision declares."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if not isinstance(node, ast.Call):
            continue
        call = _name_of(node.func)
        if call == "Column":
            types = node.args[1:] + [kw.value for kw in node.keywords if kw.arg == "type_"]
            if any(_name_of(t) == "JSON" for t in types):
                first = node.args[0] if node.args else None
                name = first.value if isinstance(first, ast.Constant) else "<column>"
                found.append((name, node.lineno))
        elif call == "alter_column":
            if any(kw.arg == "type_" and _name_of(kw.value) == "JSON" for kw in node.keywords):
                found.append(("<alter_column>", node.lineno))
        elif call in {"execute", "exec_driver_sql", "text"}:
            for arg in node.args:
                for const in ast.walk(arg):
                    if (
                        isinstance(const, ast.Constant)
                        and isinstance(const.value, str)
                        and _RAW_JSON_DDL.search(const.value)
                    ):
                        found.append(("<raw DDL>", node.lineno))
    return found


def test_no_new_plain_json_columns_in_metadata():
    new = sorted(_plain_json_columns() - _grandfathered())
    assert not new, f"new plain json columns {new}: {_FIX} (see this module's docstring)"


def test_grandfathered_metadata_columns_are_still_json():
    stale = sorted(_grandfathered() - _plain_json_columns())
    assert not stale, f"{stale} are no longer plain json; remove them from GRANDFATHERED_COLUMNS"


def test_revisions_declare_no_new_plain_json_columns():
    new, seen = [], set()
    for path in REVISIONS:
        for column, line in _revision_json_columns(path):
            seen.add((path.name, column))
            if (path.name, column) not in GRANDFATHERED_REVISION_COLUMNS:
                new.append(f"{path.relative_to(ROOT)}:{line} ({column})")
    assert not new, f"revisions declare plain json columns {new}: {_FIX}"
    assert GRANDFATHERED_REVISION_COLUMNS <= seen, sorted(GRANDFATHERED_REVISION_COLUMNS - seen)


def test_the_scan_catches_every_json_spelling(tmp_path):
    revision = tmp_path / "a99999999999_example.py"
    revision.write_text(
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        "from sqlalchemy import JSON\n"
        "from sqlalchemy.dialects import postgresql\n"
        "def upgrade():\n"
        "    op.add_column('t', sa.Column('a', sa.JSON(), nullable=True))\n"
        "    op.add_column('t', sa.Column('b', JSON, nullable=True))\n"
        "    op.add_column('t', sa.Column('c', type_=sa.types.JSON()))\n"
        "    op.alter_column('t', 'd', type_=postgresql.JSON())\n"
        "    op.execute('ALTER TABLE t ADD COLUMN e json')\n"
        "    op.add_column('t', sa.Column('ok', postgresql.JSONB(), nullable=True))\n"
        "    op.execute('ALTER TABLE t ALTER COLUMN ok TYPE jsonb USING ok::jsonb')\n"
    )
    found = sorted(_revision_json_columns(revision), key=lambda hit: hit[1])
    assert [column for column, _ in found] == [
        "a",
        "b",
        "c",
        "<alter_column>",
        "<raw DDL>",
    ]
