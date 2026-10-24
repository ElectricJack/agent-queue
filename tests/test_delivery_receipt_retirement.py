"""Retirement ratchet: delivery receipts are historical audit, never authority.

Spec 2026-10-12 fast-smooth-integration §3.3 and the receipt consumer inventory
in ``docs/specs/design/strict-git-containment.md``. The pinned sets below only
shrink: a new reader or writer of ``task_delivery_receipts`` fails here until
the inventory names it and its owner.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
TABLE = "task_delivery_receipts"
WRITE_CALLS = {"insert", "update", "delete", "pg_insert"}
RAW_WRITE = re.compile(rf"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+{TABLE}\b", re.IGNORECASE)

# The only statements that still write a receipt row. None grants delivery:
# each records audit after the fact or rewrites history at operator cutover.
WRITERS = {
    "src/integration/truth_reconciler.py::GitTruthReconciler._repair",
    "src/integration/cutover.py::_Activation.write_on",
    "src/integration/records.py::ParentEpisodeRecords.record_disposition",
}

# Every module that still names the table, with its inventory owner. Delivery
# proof goes through the Git-first reader; these are schema, audit, history,
# operator cutover or transition work their owners have yet to retire.
READERS = {
    "src/database/tables.py": "schema",
    "src/database/legacy_sqlite_import.py": "offline historical import",
    "src/database/queries/blocked_state.py": "explain: failed-landing diagnostics",
    "src/database/queries/integration_delivery_queries.py": "public receipt listing",
    "src/database/queries/integration_train_queries.py": "dispatch: legacy root selection",
    "src/integration/cancelled_collection_recovery.py": "cutover: historical recovery",
    "src/integration/child_delivery.py": "watchdog: stale-child diagnosis",
    "src/integration/cutover.py": "cutover: inventory and retagging",
    "src/integration/epic_delivery.py": "dashboard: shadow display",
    "src/integration/failed_verification_recovery.py": "cutover: historical recovery",
    "src/integration/legacy_backfill.py": "cutover: historical attestation",
    "src/integration/observe.py": "close_merge: parent episode protocol",
    "src/integration/parent_repair_heads.py": "fixers: resolution identity",
    "src/integration/parent_source.py": "close_merge: parent episode protocol",
    "src/integration/records.py": "close_merge: disposition audit",
    "src/integration/release.py": "dispatch: terminal batch release",
    "src/integration/repair.py": "fixers: conflict-resolution identity",
    "src/integration/root_pull_requests.py": "dispatch: root diagnostics",
    "src/integration/stranded_children.py": "watchdog: recovery selection",
    "src/integration/suspended_parent_recovery.py": "cutover: historical recovery",
    "src/integration/task_deletion.py": "audit: deletion snapshot",
    "src/integration/truth_reconciler.py": "audit after Git verification",
}

# Admission projects Git-first containment only (strict-git-containment.md,
# "Admission consumer disposition"); none of these may read a receipt again.
ADMISSION = (
    "src/database/queries/hierarchy_queries.py",
    "src/database/queries/claim_queries.py",
    "src/task_graph/readiness.py",
    "src/integration/hierarchy.py",
    "src/integration/removal_guard.py",
    "src/integration/source_repairs.py",
    "src/integration/drain_owners.py",
    "src/doctor/stall_checks.py",
)


@functools.cache
def _sources() -> tuple[tuple[str, ast.Module], ...]:
    return tuple((path.relative_to(ROOT).as_posix(), ast.parse(path.read_text()))
                 for path in sorted((ROOT / "src").rglob("*.py")))


def _names_table(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == TABLE) or (
        isinstance(node, ast.Attribute) and node.attr == TABLE)


def _writes(tree: ast.AST) -> set[str]:
    found: set[str] = set()

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in WRITE_CALLS and node.args and _names_table(node.args[0]):
                found.add(".".join(scope))
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and RAW_WRITE.search(node.value)):
            found.add(".".join(scope))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, ())
    return found


def test_only_pinned_audit_statements_write_receipts():
    writers = {f"{path}::{scope}" for path, tree in _sources() for scope in _writes(tree)}
    assert writers == WRITERS


def test_receipt_readers_only_shrink_and_admission_reads_none():
    readers = {path for path, tree in _sources()
               if any(_names_table(node) for node in ast.walk(tree))}
    assert readers == set(READERS)
    assert not readers.intersection(ADMISSION)


def test_retired_receipt_authority_is_absent():
    from src.database.base import DatabaseBackend
    from src.integration import child_delivery, records

    assert not hasattr(DatabaseBackend, "finalize_integration_promotion")
    assert not hasattr(child_delivery.ChildDelivery, "_reissue_receipt")
    for name in ("record_receipt", "record_receipt_on", "DeliveryProof", "ReceiptProjection"):
        assert not hasattr(records, name) and not any(
            hasattr(cls, name) for cls in vars(records).values() if isinstance(cls, type))


def test_record_receipt_primitive_has_no_binding():
    bound = sorted(
        path for path, tree in _sources()
        if path != "src/integration/runtime_contracts.py"
        and any(isinstance(node, ast.Attribute) and node.attr == "RECORD_RECEIPT"
                for node in ast.walk(tree)))
    assert bound == []
