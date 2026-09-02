"""Atomic, content-addressed storage for immutable Playbook V2 artifacts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from src.playbooks.artifact_ref import ARTIFACT_SCHEMA_GENERATION, ArtifactRef, SHA256_RE
from src.playbooks.definition import (
    PlaybookDefinition,
    artifact_sha256 as definition_artifact_sha256,
    canonical_bytes as definition_canonical_bytes,
)
from src.playbooks.run_state import (
    ArtifactHashCollision,
    ArtifactTooLarge,
    ArtifactVerificationFailed,
)


class ArtifactStore:
    """Store canonical artifact bytes beneath ``<compiled_root>/artifacts``."""

    def __init__(self, compiled_root: str, *, max_artifact_bytes: int = 1_048_576) -> None:
        self._root = Path(compiled_root) / "artifacts"
        self._max_artifact_bytes = max_artifact_bytes

    canonical_bytes = staticmethod(definition_canonical_bytes)

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
        definition: PlaybookDefinition,
        *,
        source_digest: str,
        contract_fingerprint: str,
        profile_fingerprint: str,
        compiler_build: str,
        version: int = 0,
    ) -> ArtifactRef:
        # The profile fingerprint is caller-owned row metadata rather than
        # artifact identity.  Accept it here as part of the locked compile-to-
        # store handoff; PlaybookArtifactQueryMixin persists it separately.
        _ = profile_fingerprint
        definition = PlaybookDefinition.model_validate(definition)
        data = definition_canonical_bytes(definition)
        if len(data) > self._max_artifact_bytes:
            raise ArtifactTooLarge(f"artifact is {len(data)} bytes; limit is {self._max_artifact_bytes}")
        sha = definition_artifact_sha256(definition)
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = Path(self.path_for(sha))
        if path.exists():
            if path.read_bytes() != data:
                raise ArtifactHashCollision(f"{sha} already names different bytes at {path}")
            # Content-addressed storage means an identical artifact is adopted
            # rather than rewritten, which would otherwise leave this file with
            # the mtime of whenever it was first written.  The retention sweep
            # decides orphan candidacy by age (``ORPHAN_FILE_TTL_SECONDS``), so
            # a file being adopted right now must look recent: without this,
            # a put that reuses an old file could race the sweep between the
            # adoption here and the caller's row write.
            try:
                os.utime(path)
            except OSError:  # pragma: no cover - permissions/filesystem
                pass
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
        return ArtifactRef(
            playbook_id=definition.id,
            artifact_sha256=sha,
            schema_generation=ARTIFACT_SCHEMA_GENERATION,
            contract_fingerprint=contract_fingerprint,
            source_digest=source_digest,
            compiler_build=compiler_build,
            version=version,
        )

    def load(self, artifact_sha256: str) -> PlaybookDefinition:
        path = Path(self.path_for(artifact_sha256))
        data = path.read_bytes()
        if self._sha(data) != artifact_sha256:
            raise ArtifactVerificationFailed(f"artifact at {path} does not match {artifact_sha256}")
        return PlaybookDefinition.model_validate_json(data)

    def delete(self, artifact_sha256: str) -> bool:
        path = Path(self.path_for(artifact_sha256))
        if not path.exists():
            return False
        path.unlink()
        return True
