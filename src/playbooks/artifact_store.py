"""Atomic, content-addressed storage for immutable Playbook V2 artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from src.playbooks.artifact_ref import ARTIFACT_SCHEMA_GENERATION, SHA256_RE, ArtifactRef
from src.playbooks.definition import (
    PlaybookDefinition,
    load_definition_json,
)
from src.playbooks.definition import (
    artifact_sha256 as definition_artifact_sha256,
)
from src.playbooks.definition import (
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
        self._layout_root = Path(compiled_root) / "layouts"
        self._max_artifact_bytes = max_artifact_bytes

    canonical_bytes = staticmethod(definition_canonical_bytes)

    @staticmethod
    def _sha(data: bytes) -> str:
        return f"sha256:{hashlib.sha256(data).hexdigest()}"

    def path_for(self, artifact_sha256: str) -> str:
        if not SHA256_RE.fullmatch(artifact_sha256):
            raise ValueError(f"invalid artifact SHA-256: {artifact_sha256!r}")
        return str(self._root / f"{artifact_sha256[7:]}.json")

    def layout_path_for(self, artifact_sha256: str) -> str:
        if not SHA256_RE.fullmatch(artifact_sha256):
            raise ValueError(f"invalid artifact SHA-256: {artifact_sha256!r}")
        return str(self._layout_root / f"{artifact_sha256[7:]}.json")

    def load_layout(self, artifact_sha256: str) -> dict[str, dict[str, int]]:
        """Return mutable presentation coordinates for one immutable artifact."""
        path = Path(self.layout_path_for(artifact_sha256))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        positions = payload.get("positions")
        if not isinstance(positions, dict):
            return {}
        clean: dict[str, dict[str, int]] = {}
        for step_id, position in positions.items():
            if not isinstance(step_id, str) or not isinstance(position, dict):
                continue
            x, y = position.get("x"), position.get("y")
            if isinstance(x, bool) or isinstance(y, bool):
                continue
            if isinstance(x, int) and isinstance(y, int):
                clean[step_id] = {"x": x, "y": y}
        return clean

    def save_layout(
        self, artifact_sha256: str, positions: dict[str, dict[str, int]]
    ) -> None:
        """Atomically replace presentation coordinates for one stored artifact."""
        if not self.exists(artifact_sha256):
            raise FileNotFoundError(f"artifact {artifact_sha256} is not stored")
        self._layout_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = Path(self.layout_path_for(artifact_sha256))
        data = json.dumps(
            {"artifact_sha256": artifact_sha256, "positions": positions},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        tmp = self._layout_root / f"{path.name}.tmp-{os.getpid()}-{uuid4().hex}"
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            directory_fd = os.open(self._layout_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            tmp.unlink(missing_ok=True)

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
        # Two attempts, because every read of ``path`` here races the retention
        # sweep, which removes a collected artifact by renaming it away.  The
        # adopt branch below is the one that can lose: it decides the file is
        # already there and then reads it again to verify.  A single retry is
        # enough — the second pass finds no file and writes the bytes itself,
        # and a sweep cannot collect a hash it has already collected.
        for attempt in (0, 1):
            stored = self._read_if_present(path)
            if stored is None:
                self._write_atomically(path, data)
            elif stored != data:
                raise ArtifactHashCollision(f"{sha} already names different bytes at {path}")
            else:
                # Content-addressed storage means an identical artifact is
                # adopted rather than rewritten, which would otherwise leave
                # this file with the mtime of whenever it was first written.
                # The retention sweep decides orphan candidacy by age
                # (``ORPHAN_FILE_TTL_SECONDS``), so a file being adopted right
                # now must look recent: without this, a put that reuses an old
                # file could race the sweep between the adoption here and the
                # caller's row write.
                try:
                    os.utime(path)
                except OSError:  # pragma: no cover - permissions/filesystem
                    pass
            written = self._read_if_present(path)
            if written is None:
                if attempt == 0:
                    continue
                raise ArtifactVerificationFailed(f"artifact at {path} vanished while storing")
            if self._sha(written) != sha:
                path.unlink(missing_ok=True)
                raise ArtifactVerificationFailed(f"artifact at {path} does not match {sha}")
            break
        return ArtifactRef(
            playbook_id=definition.id,
            artifact_sha256=sha,
            schema_generation=ARTIFACT_SCHEMA_GENERATION,
            contract_fingerprint=contract_fingerprint,
            source_digest=source_digest,
            compiler_build=compiler_build,
            version=version,
        )

    @staticmethod
    def _read_if_present(path: Path) -> bytes | None:
        """The file's bytes, or ``None`` when it is not there right now."""
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def _write_atomically(self, path: Path, data: bytes) -> None:
        """Publish ``data`` at ``path`` by rename, durably and without a partial file."""
        tmp = self._root / f"{path.stem}.json.tmp-{os.getpid()}-{uuid4().hex}"
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

    def load(self, artifact_sha256: str) -> PlaybookDefinition:
        """Verify the bytes, then parse them through the strict Package 2 loader.

        The hash check alone does not make stored text safe to parse loosely:
        a file whose bytes hash to what the caller asked for can still carry a
        duplicate object key, and ``model_validate_json`` silently keeps the
        last one.  ``load_definition_json`` is the one parse §7.1 defines, so
        an artifact that ``aq playbook v2 validate`` rejects cannot be loaded
        here as though it were well-formed.
        """
        path = Path(self.path_for(artifact_sha256))
        data = path.read_bytes()
        if self._sha(data) != artifact_sha256:
            raise ArtifactVerificationFailed(f"artifact at {path} does not match {artifact_sha256}")
        return load_definition_json(data.decode("utf-8"))

    def delete(self, artifact_sha256: str) -> bool:
        path = Path(self.path_for(artifact_sha256))
        if not path.exists():
            return False
        path.unlink()
        Path(self.layout_path_for(artifact_sha256)).unlink(missing_ok=True)
        return True
