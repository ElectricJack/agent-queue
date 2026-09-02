"""Atomic, content-addressed storage for immutable Playbook V2 artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from src.playbooks.artifact_ref import ARTIFACT_SCHEMA_GENERATION, ArtifactRef, SHA256_RE

if TYPE_CHECKING:
    from src.playbooks.definition import PlaybookDefinition as PlaybookDefinitionT
else:  # pragma: no cover - Package 2 has not merged on every Package 3 branch.
    PlaybookDefinitionT = Any


class ArtifactStoreError(RuntimeError):
    """Base error for artifact storage failures."""


class ArtifactTooLarge(ArtifactStoreError):
    """The canonical artifact exceeds the configured byte limit."""


class ArtifactHashCollision(ArtifactStoreError):
    """A hash-named file contains bytes different from its claimed content."""


class ArtifactVerificationFailed(ArtifactStoreError):
    """An artifact's file bytes do not match its SHA-256 identity."""


class ArtifactStore:
    """Store canonical artifact bytes beneath ``<compiled_root>/artifacts``."""

    def __init__(self, compiled_root: str, *, max_artifact_bytes: int = 1_048_576) -> None:
        self._root = Path(compiled_root) / "artifacts"
        self._max_artifact_bytes = max_artifact_bytes

    @staticmethod
    def canonical_bytes(definition: PlaybookDefinitionT) -> bytes:
        """Use Package 2's canonicalizer when available; retain its exact fallback."""
        try:
            from src.playbooks.definition import canonical_bytes
        except ImportError:
            if hasattr(definition, "model_dump"):
                payload = definition.model_dump(mode="json", exclude_none=True)
            else:
                payload = definition
            return json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        return canonical_bytes(definition)

    @staticmethod
    def _sha(data: bytes) -> str:
        return f"sha256:{hashlib.sha256(data).hexdigest()}"

    def path_for(self, artifact_sha256: str) -> str:
        if not SHA256_RE.fullmatch(artifact_sha256):
            raise ValueError(f"invalid artifact SHA-256: {artifact_sha256!r}")
        return str(self._root / f"{artifact_sha256[7:]}.json")

    def exists(self, artifact_sha256: str) -> bool:
        return Path(self.path_for(artifact_sha256)).is_file()

    def put(
        self,
        definition: PlaybookDefinitionT,
        *,
        source_digest: str,
        contract_fingerprint: str,
        compiler_build: str,
        version: int = 0,
    ) -> ArtifactRef:
        data = self.canonical_bytes(definition)
        if len(data) > self._max_artifact_bytes:
            raise ArtifactTooLarge(f"artifact is {len(data)} bytes; limit is {self._max_artifact_bytes}")
        sha = self._sha(data)
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = Path(self.path_for(sha))
        if path.exists():
            if path.read_bytes() != data:
                raise ArtifactHashCollision(f"{sha} already names different bytes at {path}")
        else:
            tmp = self._root / f"{sha[7:]}.json.tmp-{os.getpid()}-{uuid4().hex}"
            try:
                fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, path)
                directory_fd = os.open(self._root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                tmp.unlink(missing_ok=True)
        if self._sha(path.read_bytes()) != sha:
            path.unlink(missing_ok=True)
            raise ArtifactVerificationFailed(f"artifact at {path} does not match {sha}")
        playbook_id = getattr(definition, "id", None) or definition["id"]
        return ArtifactRef(
            playbook_id=playbook_id,
            artifact_sha256=sha,
            schema_generation=ARTIFACT_SCHEMA_GENERATION,
            contract_fingerprint=contract_fingerprint,
            source_digest=source_digest,
            compiler_build=compiler_build,
            version=version,
        )

    def load(self, artifact_sha256: str) -> PlaybookDefinitionT:
        path = Path(self.path_for(artifact_sha256))
        data = path.read_bytes()
        if self._sha(data) != artifact_sha256:
            raise ArtifactVerificationFailed(f"artifact at {path} does not match {artifact_sha256}")
        try:
            from src.playbooks.definition import PlaybookDefinition
        except ImportError:
            return json.loads(data)
        return PlaybookDefinition.model_validate_json(data)

    def delete(self, artifact_sha256: str) -> bool:
        path = Path(self.path_for(artifact_sha256))
        if not path.exists():
            return False
        path.unlink()
        return True
