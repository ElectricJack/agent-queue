"""Durable, content-addressed retention for object-loop receipt artifacts.

``ScoreReceipt`` names durable URIs and SHA-256 digests and refuses local
paths, but nothing else in AQ mints one: a ``matter_render`` job leaves its
capture in ``{data_dir}/runs/<job>/capture``, which the job sweeper reclaims.
This module is that missing producer.  Retention copies bytes out of the
transient run directory into a never-overwritten object file under
``{data_dir}/artifacts/objects`` and names it with its own digest, so
resolution can re-verify the bytes it serves rather than trust a pointer.

The digest is the identity, so retention is idempotent: retaining the same
bytes twice yields the same URI and never rewrites the object.  The URI is
``artifact://sha256/<digest>`` — the ``artifact`` scheme and a named object,
which is exactly what :class:`src.object_loop.contracts.Artifact` accepts, and
distinct from an ``s3://`` or ``https://`` URI this store does not serve.

Two entry points, because a receipt identity is either bytes or a document.
:func:`retain_file` retains a capture artifact as-is; :func:`retain_document`
retains the canonical form of a JSON document, which is how a producer's
declared identity (``candidate_sha256`` and its siblings) becomes an artifact
whose digest *is* that identity.

Nothing here trusts a caller: a source is opened ``O_NOFOLLOW`` and read into a
digest before it is copied, and an object already present is re-hashed before
its identity is reported, so a crashed or edited predecessor can never pass as
retained evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from src.jobs.artifacts import atomic_json, open_artifact, read_json

SCHEME = "artifact"
AUTHORITY = "sha256"
PREFIX = f"{SCHEME}://{AUTHORITY}/"
SIDECAR_VERSION = 1
_CHUNK = 1024 * 1024
_HEX = frozenset("0123456789abcdef")


class ArtifactError(ValueError):
    """Raised when a durable artifact cannot be minted, resolved or verified."""


def canonical_bytes(value) -> bytes:
    """The one canonical JSON form AQ hashes: sorted keys, no insignificant space.

    Matter declares ``candidate_sha256``, ``manifest_sha256`` and
    ``rig_sha256`` this way, so a document minted through :func:`retain_document`
    reproduces the identity its producer claimed instead of a near-miss.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _checked_digest(digest: str) -> str:
    if not isinstance(digest, str) or len(digest) != 64 or set(digest) - _HEX:
        raise ArtifactError("artifact digest must be 64 lowercase sha256 hex characters")
    return digest


def store_root(data_dir) -> Path:
    """The object directory, created 0700 and refused through a symlink."""
    root = Path(data_dir).expanduser().resolve() / "artifacts" / "objects"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ArtifactError("symlink artifact store root")
    return root


def artifact_uri(digest: str) -> str:
    """The durable URI naming *digest*."""
    return f"{PREFIX}{_checked_digest(digest)}"


def parse_uri(uri: str) -> str:
    """Return the digest an ``artifact://sha256/`` URI names.

    Every other form is refused rather than guessed: this store serves its own
    objects and nothing else, so a receipt can only be resolved against bytes AQ
    still holds.
    """
    if not isinstance(uri, str) or not uri.startswith(PREFIX):
        raise ArtifactError(f"{uri!r} is not an {PREFIX}<sha256> URI")
    parsed = urlparse(uri)
    digest = parsed.path.strip("/")
    if (parsed.scheme != SCHEME or parsed.netloc != AUTHORITY or parsed.query
            or parsed.fragment or parsed.params or "/" in digest):
        raise ArtifactError(f"{uri!r} is not an {PREFIX}<sha256> URI")
    return _checked_digest(digest)


def _object_path(data_dir, digest: str) -> Path:
    directory = store_root(data_dir) / _checked_digest(digest)[:2]
    directory.mkdir(exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ArtifactError("symlink artifact fanout directory")
    return directory / digest


def _sidecar_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.json")


def _fsync_dir(directory: Path) -> None:
    handle = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _digest_file(path: Path, *, max_bytes: int | None) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with os.fdopen(open_artifact(path, os.O_RDONLY), "rb") as stream:
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise ArtifactError(f"{path.name} exceeds its retention byte budget")
            digest.update(chunk)
    return digest.hexdigest(), size


def _stream(path: Path):
    with os.fdopen(open_artifact(path, os.O_RDONLY), "rb") as stream:
        while chunk := stream.read(_CHUNK):
            yield chunk


def _write_object(target: Path, chunks) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with os.fdopen(
            open_artifact(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"
        ) as stream:
            for chunk in chunks:
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_dir(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _reported(data_dir, target: Path, digest: str, size: int, **fields) -> dict:
    """Record the sidecar and report a retained object.

    An object already present is re-hashed first: retention is idempotent, but
    it must never certify bytes that no longer hash to the identity they are
    being served under.
    """
    if target.exists():
        if target.is_symlink():
            raise ArtifactError("symlink artifact object")
        found, _ = _digest_file(target, max_bytes=None)
        if found != digest:
            raise ArtifactError(f"retained artifact {digest} no longer hashes to its identity")
    kind = fields.get("kind")
    if not isinstance(kind, str) or not 1 <= len(kind) <= 128:
        raise ArtifactError("artifact kind must be a short label")
    origin = fields.get("origin")
    if origin is not None and not isinstance(origin, dict):
        raise ArtifactError("artifact origin must be a mapping")
    sidecar = {
        "version": SIDECAR_VERSION,
        "sha256": digest,
        "bytes": size,
        "kind": kind,
        "source": fields.get("source") or "",
        "origin": origin or {},
        "retained_at": time.time(),
    }
    atomic_json(_sidecar_path(target), sidecar)
    return {
        "uri": artifact_uri(digest),
        "sha256": digest,
        "bytes": size,
        "kind": kind,
        "path": str(target),
        "origin": sidecar["origin"],
    }


def retain_bytes(data: bytes, *, data_dir, kind: str, origin: dict | None = None) -> dict:
    """Retain in-memory bytes and return their durable identity."""
    if not isinstance(data, (bytes, bytearray)):
        raise ArtifactError("artifact bytes required")
    data = bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    target = _object_path(data_dir, digest)
    if not target.exists():
        _write_object(target, (data,))
    return _reported(
        data_dir, target, digest, len(data), kind=kind, origin=origin, source="bytes"
    )


def retain_file(
    source: str | Path,
    *,
    data_dir,
    kind: str,
    origin: dict | None = None,
    max_bytes: int | None = None,
) -> dict:
    """Copy a regular file into the store and return its durable identity.

    The source is read twice — once to take the digest, once to copy — so a
    multi-hundred-megabyte capture never lands in memory whole.
    """
    path = Path(source)
    if path.is_symlink() or not path.is_file():
        raise ArtifactError(f"{path} is not a retained regular file")
    digest, size = _digest_file(path, max_bytes=max_bytes)
    target = _object_path(data_dir, digest)
    if not target.exists():
        _write_object(target, _stream(path))
    return _reported(
        data_dir, target, digest, size, kind=kind, origin=origin, source=str(path)
    )


def retain_document(
    document: dict, *, data_dir, kind: str, origin: dict | None = None,
    expect_sha256: str | None = None,
) -> dict:
    """Retain the canonical form of a JSON document as a named artifact.

    ``expect_sha256`` is the identity its producer declared.  A document whose
    canonical bytes do not hash to it is refused: minting an artifact beside a
    mismatched claim would produce a receipt that names one candidate and
    carries the evidence of another.
    """
    if not isinstance(document, dict):
        raise ArtifactError("a retained document must be a JSON object")
    data = canonical_bytes(document)
    digest = hashlib.sha256(data).hexdigest()
    if expect_sha256 is not None and _checked_digest(expect_sha256) != digest:
        raise ArtifactError(
            "the document does not hash to the identity it declares "
            f"({digest} != {expect_sha256})"
        )
    return retain_bytes(
        data, data_dir=data_dir, kind=kind,
        origin={"declared_sha256": digest, **(origin or {})},
    )


def resolve(data_dir, uri: str) -> Path:
    """Return the local path holding *uri*, or refuse."""
    target = _object_path(data_dir, parse_uri(uri))
    if target.is_symlink():
        raise ArtifactError("symlink artifact object")
    if not target.is_file():
        raise ArtifactError(f"{uri} is not retained in this store")
    return target


def metadata(data_dir, uri: str) -> dict | None:
    """The recorded sidecar for *uri*, or None when nothing was recorded."""
    return read_json(_sidecar_path(_object_path(data_dir, parse_uri(uri))))


def verify(data_dir, uri: str, sha256: str | None = None) -> dict:
    """Re-hash the retained bytes and prove they are what the URI claims.

    AQ checks identities and evidence; a URI is a claim and this is the check.
    """
    digest = parse_uri(uri)
    if sha256 is not None and _checked_digest(sha256) != digest:
        raise ArtifactError("artifact sha256 does not match the URI it is claimed for")
    target = resolve(data_dir, uri)
    found, size = _digest_file(target, max_bytes=None)
    if found != digest:
        raise ArtifactError(f"{uri} does not hash to its named digest")
    return {
        "uri": uri,
        "sha256": digest,
        "bytes": size,
        "verified": True,
        "path": str(target),
        "kind": (metadata(data_dir, uri) or {}).get("kind"),
    }