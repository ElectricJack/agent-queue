"""Read-only dry-run inventory: scan, seal, verify, and reconcile.

K06 produces a hash-pinned reconciliation report without touching any
``record_*`` table. ``record_import_runs``, ``record_legacy_mappings`` and
``record_import_items`` are owned by K07's apply path; K06 only computes what
those tables would hold and returns them for operator review.

The dry-run never initializes the legacy plugin, never uses live search/get,
never mutates retrieval counters, and never applies a revision. The sealed
manifest is the single source of truth for the report: every identity in the
report is one identity in the manifest, and the manifest's ``counts`` is the
identity that the accounting must close on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .inventory import RootSpec, scan_inventory
from .legacy_memory import ScanLimits
from .manifest import seal_manifest, verify_manifest


@dataclass(frozen=True)
class DryRunReport:
    """Hash-pinned reconciliation report for a sealed legacy inventory."""

    source_installation_id: str
    snapshot_id: str
    snapshot_timestamp: str
    manifest_sha256: str
    vector_observation: str
    counts: dict[str, Any]
    items: tuple[dict[str, Any], ...]
    mappings: tuple[dict[str, Any], ...]
    identities: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": True,
            "outcome": "read",
            "source_installation_id": self.source_installation_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_timestamp": self.snapshot_timestamp,
            "manifest_sha256": self.manifest_sha256,
            "vector_observation": self.vector_observation,
            "counts": dict(self.counts),
            "items": list(self.items),
            "mappings": list(self.mappings),
            "identities": list(self.identities),
        }


def _mapping_row(source_installation_id: str, identity: tuple[str, str, str]) -> dict[str, Any]:
    kind, scope, key = identity
    return {
        "source_installation_id": source_installation_id,
        "source_kind": kind,
        "source_scope": scope,
        "source_key": key,
        "record_id": None,
        "latest_source_sha256": None,
        "latest_revision_id": None,
        "ownership": "legacy",
        "decision_reason": None,
    }


def _identity_row(source_installation_id: str, source: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_installation_id": source_installation_id,
        "source_kind": source["source_kind"],
        "source_scope": source["source_scope"],
        "source_key": source["source_key"],
        "artifact_sha256": source.get("artifact_sha256"),
    }


def _item_row(item: dict[str, Any]) -> dict[str, Any]:
    """Mirror a ``record_import_items`` row for this manifest item (K07 applies)."""
    return {
        "item_key": item["item_key"],
        "source_sha256": item["source_sha256"],
        "disposition": item["disposition"],
        "record_id": None,
        "revision_id": None,
        "error_code": None,
        "classification": item["classification"],
        "issues": list(item.get("issues") or ()),
        "original_present": item.get("original") is not None,
        "summary_present": item.get("summary") is not None,
        "sources": len(item.get("sources") or ()),
    }


async def run_dry_run(
    *,
    roots: Sequence[RootSpec],
    vector_export: Path | None,
    scope_aliases: Mapping[str, Sequence[str]] | None,
    source_installation_id: str,
    snapshot_id: str,
    snapshot_timestamp: str,
    limits: ScanLimits | None = None,
) -> DryRunReport:
    """Scan the supplied roots, seal the manifest, and reconcile in-memory.

    The report's ``counts`` closes on the manifest's accounting, and each
    item/mapping row mirrors what a K07 apply would persist. ``manifest_sha256``
    is the seal; ``verify_manifest`` runs before the report is built so a
    corrupted seal never reaches the operator.
    """
    inventory = await scan_inventory(
        roots,
        vector_export=vector_export,
        scope_aliases=scope_aliases,
        limits=limits,
    )
    manifest = seal_manifest(
        inventory,
        source_installation_id=source_installation_id,
        snapshot_id=snapshot_id,
        snapshot_timestamp=snapshot_timestamp,
    )
    document = verify_manifest(manifest.content, manifest.sha256)

    items = tuple(_item_row(item) for item in document["items"])
    identities = tuple(
        _identity_row(source_installation_id, source)
        for item in document["items"]
        for source in item.get("sources") or ()
    )
    # Map each source identity once (the seal enforces exactly once), preserving
    # the manifest's deterministic item order.
    mappings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in identities:
        identity = (row["source_kind"], row["source_scope"], row["source_key"])
        if identity in seen:
            continue
        seen.add(identity)
        mappings.append(_mapping_row(source_installation_id, identity))

    counts = dict(document["counts"])
    return DryRunReport(
        source_installation_id=source_installation_id,
        snapshot_id=snapshot_id,
        snapshot_timestamp=snapshot_timestamp,
        manifest_sha256=manifest.sha256,
        vector_observation=document["vector_observation"],
        counts=counts,
        items=items,
        mappings=tuple(mappings),
        identities=identities,
    )
