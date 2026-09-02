"""Immutable artifact identity shared by every Playbook V2 consumer.

Roadmap section 4 ``ArtifactRef``.  Deliberately tiny and dependency-free:
Package 3's two implementation branches both need it, Package 4 pins runs
with it, and Package 5 projects it into ``ArtifactRefDTO`` field-for-field.

The hash is computed from the canonical artifact bytes by
``src.playbooks.definition.canonical_bytes`` (Package 2) and is carried everywhere
in its full ``sha256:<64 lowercase hex>`` form.  Truncation is a display
concern and happens in the dashboard, never here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Canonical hash form used on the wire, in the database and in filenames.
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: ``PlaybookDefinition.schema_version`` this generation of the store accepts.
ARTIFACT_SCHEMA_GENERATION = 2


class ArtifactRefError(ValueError):
    """An artifact reference field is missing or malformed."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Identifies exactly one immutable artifact.

    Field names are the Package 5 wire names (``ArtifactRefDTO``); the
    projection is a copy, not a rename.
    """

    playbook_id: str
    artifact_sha256: str
    schema_generation: int
    contract_fingerprint: str
    source_digest: str
    compiler_build: str
    compiled_at: str | None = None
    version: int = 0

    def __post_init__(self) -> None:
        if not self.playbook_id:
            raise ArtifactRefError("playbook_id is required")
        for name in ("artifact_sha256", "contract_fingerprint", "source_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not SHA256_RE.match(value):
                raise ArtifactRefError(f"{name} must be 'sha256:<64 lowercase hex>', got {value!r}")
        if self.schema_generation != ARTIFACT_SCHEMA_GENERATION:
            raise ArtifactRefError(
                f"schema_generation {self.schema_generation} is not supported "
                f"(this build stores generation {ARTIFACT_SCHEMA_GENERATION})"
            )
        if not self.compiler_build:
            raise ArtifactRefError("compiler_build is required")
        if self.version < 0:
            raise ArtifactRefError("version must be >= 0")

    @property
    def digest(self) -> str:
        """The bare 64-hex digest — the artifact's filename stem."""
        return self.artifact_sha256.split(":", 1)[1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "artifact_sha256": self.artifact_sha256,
            "schema_generation": self.schema_generation,
            "contract_fingerprint": self.contract_fingerprint,
            "source_digest": self.source_digest,
            "compiler_build": self.compiler_build,
            "compiled_at": self.compiled_at,
            "version": self.version,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ArtifactRef:
        """Build from a ``playbook_artifacts`` row mapping."""
        return cls(
            playbook_id=row["playbook_id"],
            artifact_sha256=row["artifact_sha256"],
            schema_generation=int(row["schema_generation"]),
            contract_fingerprint=row["contract_fingerprint"],
            source_digest=row["source_digest"],
            compiler_build=row["compiler_build"],
            compiled_at=row["compiled_at"],
            version=int(row["version"]),
        )
