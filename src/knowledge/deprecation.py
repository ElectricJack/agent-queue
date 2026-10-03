"""Content-free compatibility diagnostics and fail-closed G7 evidence checks.

These mechanisms never activate flags, load plugins, delete sources or uninstall
adapters. A seal checks integrity; an evidence receipt is an operator attestation,
not a permission token. Actual removal needs separate operator authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import (
    record_compatibility_usage,
    record_import_items,
    record_import_runs,
    record_legacy_mappings,
)
from src.knowledge.imports.manifest import canonical_json, sha256, verify_manifest


async def record_usage_on(conn, *, scope_key, operation, outcome):
    """Count an attempt before dispatch; crashes cannot masquerade as success."""
    table = record_compatibility_usage
    await conn.execute(
        insert(table)
        .values(scope_key=scope_key, operation=operation, outcome=outcome, calls=1)
        .on_conflict_do_update(
            index_elements=[table.c.scope_key, table.c.operation, table.c.outcome],
            set_={
                "calls": table.c.calls + 1,
                # Transactions begun earlier can reach this row after a newer
                # caller. Preserve observation order across that lock wait.
                "last_seen_at": func.greatest(table.c.last_seen_at, func.clock_timestamp()),
            },
        )
    )


async def record_usage(db, *, scope_key, operation, outcome):
    async with db.immediate() as conn:
        await record_usage_on(conn, scope_key=scope_key, operation=operation, outcome=outcome)


async def compatibility_report(db):
    """Operator diagnostics, deliberately without a claim of complete coverage."""
    table = record_compatibility_usage
    async with db.immediate() as conn:
        rows = (
            (
                await conn.execute(
                    select(
                        table.c.operation, table.c.outcome, func.sum(table.c.calls).label("calls")
                    )
                    .group_by(table.c.operation, table.c.outcome)
                    .order_by(table.c.operation, table.c.outcome)
                )
            )
            .mappings()
            .all()
        )
        first = await conn.scalar(select(func.min(table.c.first_seen_at)))
        last = await conn.scalar(select(func.max(table.c.last_seen_at)))
        ownership = (
            (
                await conn.execute(
                    select(
                        record_legacy_mappings.c.ownership, func.count().label("sources")
                    ).group_by(record_legacy_mappings.c.ownership)
                )
            )
            .mappings()
            .all()
        )
    return {
        "attempts": [{**dict(row), "calls": int(row["calls"])} for row in rows],
        "first_seen_at": first.isoformat() if first else None,
        "last_seen_at": last.isoformat() if last else None,
        "ownership": {row["ownership"]: row["sources"] for row in ownership},
        "coverage": "managed core compatibility paths only; external adapters unattested",
        "unsafe_writes": None,
        "g7_decision": "required",
        "removal_authorized": False,
    }


async def reconcile_manifest(db, *, content, manifest_sha256):
    """Compare every sealed source with its current permanent ownership fence.

    This internal operator-evidence reader does not return content or source
    identities. Every candidate must match the exact source descriptor hash and
    map to a retained revision. Exclusions require a stored reason. Quarantine,
    missing vector inventory and changed/unmapped sources keep G7 closed.
    """
    document = verify_manifest(content, manifest_sha256)
    counts = {"managed": 0, "excluded": 0, "unresolved": 0}
    async with db.immediate() as conn:
        for item in document["items"]:
            for source in item["sources"]:
                table = record_legacy_mappings
                receipt = await conn.scalar(
                    select(record_import_items.c.item_key)
                    .join(
                        record_import_runs,
                        record_import_runs.c.run_id == record_import_items.c.run_id,
                    )
                    .where(
                        record_import_runs.c.source_installation_id
                        == document["source_installation_id"],
                        record_import_items.c.item_key == item["item_key"],
                        record_import_items.c.source_sha256 == item["source_sha256"],
                        record_import_items.c.disposition == item["disposition"],
                        record_import_items.c.error_code.is_(None),
                    )
                    .limit(1)
                )
                mapping = (
                    (
                        await conn.execute(
                            select(table).where(
                                table.c.source_installation_id
                                == document["source_installation_id"],
                                table.c.source_kind == source["source_kind"],
                                table.c.source_scope == source["source_scope"],
                                table.c.source_key == source["source_key"],
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    item["disposition"] == "candidate"
                    and receipt
                    and mapping
                    and mapping["ownership"] == "managed"
                    and mapping["record_id"]
                    and mapping["latest_revision_id"]
                    and mapping["latest_source_sha256"] == sha256(canonical_json(source))
                ):
                    revision = await db.get_knowledge_revision_on(
                        mapping["record_id"], revision_id=mapping["latest_revision_id"], conn=conn
                    )
                    record = await db.get_record_on(record_id=mapping["record_id"], conn=conn)
                    retained = (
                        revision
                        and revision["snapshot"] is not None
                        and record
                        and record["scope_key"] == source["source_scope"]
                    )
                    outcome = "managed" if retained else "unresolved"
                elif (
                    item["disposition"] == "excluded"
                    and receipt
                    and mapping
                    and mapping["ownership"] == "excluded"
                    and mapping["decision_reason"]
                ):
                    outcome = "excluded"
                else:
                    outcome = "unresolved"
                counts[outcome] += 1
    return {
        "manifest_sha256": manifest_sha256,
        "sources": document["counts"]["sources"],
        **counts,
        "vector_observed": document["vector_observation"] == "observed",
        "complete": counts["unresolved"] == 0 and document["vector_observation"] == "observed",
    }


@dataclass(frozen=True)
class DeprecationEvidence:
    """Explicit attestations retained by the operator outside core configuration."""

    announced_at: datetime | None = None
    release_receipts: tuple[str, ...] = ()
    restore_receipt: str = ""
    replacement_acceptance_receipt: str = ""
    compatibility_coverage_receipt: str = ""
    unsafe_compatibility_writes: int | None = None
    g7_approved_by: str = ""
    g7_decision_receipt: str = ""


def adapter_removal_guard(reconciliation, evidence: DeprecationEvidence, *, now=None):
    """Report missing conditions; never perform removal or grant permission."""
    now = now or datetime.now(UTC)
    blockers = []
    if not reconciliation.get("complete"):
        blockers.append("complete_reconciliation_required")
    if (
        evidence.announced_at is None
        or evidence.announced_at.tzinfo is None
        or now.tzinfo is None
        or now - evidence.announced_at < timedelta(days=30)
    ):
        blockers.append("announced_30_day_window_required")
    if len({receipt.strip() for receipt in evidence.release_receipts if receipt.strip()}) < 2:
        blockers.append("two_release_receipts_required")
    for field in (
        "restore_receipt",
        "replacement_acceptance_receipt",
        "compatibility_coverage_receipt",
        "g7_approved_by",
        "g7_decision_receipt",
    ):
        if not getattr(evidence, field).strip():
            blockers.append(f"{field}_required")
    if (
        type(evidence.unsafe_compatibility_writes) is not int
        or evidence.unsafe_compatibility_writes != 0
    ):
        blockers.append("zero_unsafe_compatibility_writes_required")
    return {
        "success": True,
        "g7_evidence_complete": not blockers,
        "blockers": blockers,
        "removal_authorized": False,
        "next_step": "separate operator authorization for uninstall and deletion",
    }
