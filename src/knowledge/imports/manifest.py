"""Version 1 offline inventory contracts and deterministic, self-contained seals.

K06 handoff: call ``await inventory.scan_inventory(...)`` with explicit roots and
an operator-produced JSON export, then ``seal_manifest`` with the operator's
source installation, snapshot ID and timestamp. Persist ``manifest.content``
and ``manifest.sha256`` together. ``verify_manifest`` checks that exact pair;
it is an integrity check, not authorization or an approval signature. K06 owns
mapping tables, scope authorization and CLI integration; K07 owns selection,
application, replay receipts and writer fences. Neither lives in this package.

Every item carries legacy mapping identities and artifact hashes. Artifacts
retain exact input bytes (including invalid UTF-8); parsed fields are additional
evidence. Source scope labels, collection names and frontmatter convey no grant.
No source content is converted into a knowledge revision or verified assertion.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

TOOL_VERSION = "knowledge-inventory/1"
Disposition = Literal["candidate", "quarantined", "excluded", "unavailable"]
VectorObservation = Literal["not_observed", "observed", "unavailable"]


def canonical_json(value: Any) -> bytes:
    """Canonical JSON, without text/newline/Unicode normalization."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class Artifact:
    content: bytes

    @property
    def sha256(self) -> str:
        return sha256(self.content)

    def descriptor(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size_bytes": len(self.content),
            "bytes_base64": base64.b64encode(self.content).decode("ascii"),
        }


@dataclass(frozen=True)
class Source:
    source_kind: str
    source_scope: str
    source_key: str
    artifact_sha256: str | None
    metadata: dict[str, Any]

    @property
    def identity(self) -> tuple[str, str, str]:
        """Combine with source_installation_id for K06's permanent mapping key."""
        return self.source_kind, self.source_scope, self.source_key


@dataclass(frozen=True)
class InventoryItem:
    sources: tuple[Source, ...]
    classification: str
    disposition: Disposition
    issues: tuple[str, ...] = ()
    original: str | None = None
    summary: str | None = None

    @property
    def item_key(self) -> str:
        return sha256(canonical_json(sorted(source.identity for source in self.sources)))

    def descriptor(self) -> dict[str, Any]:
        data = {
            "item_key": self.item_key,
            "sources": sorted((asdict(s) for s in self.sources), key=canonical_json),
            "classification": self.classification,
            "disposition": self.disposition,
            "issues": sorted(set(self.issues)),
            "original": self.original,
            "summary": self.summary,
        }
        # Covers all source hashes and parsed evidence; stable item identity is separate.
        return {**data, "source_sha256": sha256(canonical_json(data))}


@dataclass(frozen=True)
class Inventory:
    roots: tuple[dict[str, Any], ...]
    vector_observation: VectorObservation
    items: tuple[InventoryItem, ...]
    artifacts: tuple[Artifact, ...]


@dataclass(frozen=True)
class SealedManifest:
    content: bytes
    sha256: str


def seal_manifest(
    inventory: Inventory,
    *,
    source_installation_id: str,
    snapshot_id: str,
    snapshot_timestamp: str,
) -> SealedManifest:
    """Seal supplied evidence. No clock, random IDs, filesystem writes or providers."""
    if not source_installation_id or not snapshot_id:
        raise ValueError("source installation and snapshot IDs are required")
    timestamp = datetime.fromisoformat(snapshot_timestamp.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("snapshot timestamp requires an explicit timezone")
    items = sorted((item.descriptor() for item in inventory.items), key=lambda i: i["item_key"])
    identities = [source.identity for item in inventory.items for source in item.sources]
    if len(identities) != len(set(identities)):
        raise ValueError("a source identity must appear exactly once")
    artifacts = {artifact.sha256: artifact.descriptor() for artifact in inventory.artifacts}
    data = {
        "manifest_version": 1,
        "tool_version": TOOL_VERSION,
        "source_installation_id": source_installation_id,
        "snapshot_id": snapshot_id,
        "snapshot_timestamp": timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "roots": sorted(inventory.roots, key=canonical_json),
        "vector_observation": inventory.vector_observation,
        "items": items,
        "artifacts": [artifacts[key] for key in sorted(artifacts)],
        "counts": {
            "items": len(items),
            "sources": len(identities),
            "classifications": dict(sorted(Counter(i["classification"] for i in items).items())),
            "dispositions": dict(sorted(Counter(i["disposition"] for i in items).items())),
        },
    }
    content = canonical_json(data)
    verify_manifest(content, sha256(content))
    return SealedManifest(content, sha256(content))


def verify_manifest(content: bytes, expected_sha256: str) -> dict[str, Any]:
    """Verify the seal and internal artifacts, returning a fresh JSON document."""
    if sha256(content) != expected_sha256:
        raise ValueError("manifest hash mismatch")
    data = json.loads(content)
    if (
        type(data.get("manifest_version")) is not int
        or data["manifest_version"] != 1
        or data.get("tool_version") != TOOL_VERSION
        or canonical_json(data) != content
    ):
        raise ValueError("unsupported or noncanonical manifest")
    artifacts = {}
    for artifact in data["artifacts"]:
        raw = base64.b64decode(artifact["bytes_base64"], validate=True)
        if sha256(raw) != artifact["sha256"] or len(raw) != artifact["size_bytes"]:
            raise ValueError("artifact hash/size mismatch")
        if artifact["sha256"] in artifacts:
            raise ValueError("duplicate artifact")
        artifacts[artifact["sha256"]] = raw
    identities = set()
    keys = set()
    if data["vector_observation"] not in ("not_observed", "observed", "unavailable"):
        raise ValueError("invalid vector observation")
    for item in data["items"]:
        if item["disposition"] not in ("candidate", "quarantined", "excluded", "unavailable"):
            raise ValueError("invalid item disposition")
        unsigned = {key: value for key, value in item.items() if key != "source_sha256"}
        if sha256(canonical_json(unsigned)) != item["source_sha256"]:
            raise ValueError("item source hash mismatch")
        source_identities = []
        for source in item["sources"]:
            identity = (source["source_kind"], source["source_scope"], source["source_key"])
            if identity in identities:
                raise ValueError("duplicate source identity")
            identities.add(identity)
            source_identities.append(identity)
            digest = source["artifact_sha256"]
            if digest is not None and digest not in artifacts:
                raise ValueError("source artifact unavailable")
        if item["item_key"] != sha256(canonical_json(sorted(source_identities))):
            raise ValueError("item identity mismatch")
        if not source_identities or item["item_key"] in keys:
            raise ValueError("empty or duplicate item")
        keys.add(item["item_key"])
    expected_counts = {
        "items": len(keys),
        "sources": len(identities),
        "classifications": dict(Counter(i["classification"] for i in data["items"])),
        "dispositions": dict(Counter(i["disposition"] for i in data["items"])),
    }
    if data["counts"] != expected_counts:
        raise ValueError("manifest accounting mismatch")
    return data
