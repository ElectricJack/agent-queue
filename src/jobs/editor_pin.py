"""The editor pin: one editor build per attempt, copied and content-addressed.

The Matter render preset launches one shared editor build.  That build is
rebuildable at any moment — the matter-engine-cpp object-loop pilot had one
rebuilt under it, and 35.5 then blocked on ``current editor SHA differs from
claim-2 binary`` with zero captures: the render profile the attempt had pinned
names the editor build that produced the baseline, so every later capture of a
*different* build is scored against a profile no start packet pinned and can
only be refused as foreign.

So the bytes are pinned once per attempt and the attempt renders from the copy:

* the first ``matter_render`` submission of an attempt copies the configured
  editor into ``<data_dir>/editor-pins/binaries/<sha256>/<name>`` and records
  the attempt's pin;
* every later submission of the *same* attempt reuses that copy, so a rebuild
  of the shared build is invisible to an attempt in flight;
* a new ``attempt_id`` pins afresh, which is the only supported way to move the
  editor build mid-loop;
* a submission with no ``attempt_id`` still launches a content-addressed copy,
  so one job's own editor cannot move between submission and execution, but it
  has no cross-job continuity and says so in its contract.

The copy is written through a temporary file and renamed into place, and is
verified against the digest of the source it was copied from: a rebuild *during*
the copy is refused rather than pinned half-old, half-new.  A recorded pin whose
binary has gone missing or stopped hashing true is refused as
``jobs.editor_pin_lost`` — silently re-pinning would swap the preset under an
attempt and produce exactly the incomparable scores the pin exists to prevent.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

from src.jobs.artifacts import atomic_json, read_json
from src.jobs.policy import JobError

PIN_DIRNAME = "editor-pins"
ATTEMPT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ATTEMPT_ID = "attempt_id"
SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: How long a pin outlives the attempt that recorded it.  An attempt is bounded
#: by the object's own wall deadline (a day), so two weeks keeps every live
#: attempt's binary while bounding what an editor rebuild leaves behind.
RETENTION_SECONDS = 14 * 86400

_CHUNK = 1024 * 1024


def validate_attempt_id(value: str | None) -> str | None:
    """The attempt a submission belongs to, as an identifier nothing can traverse."""
    if value is None:
        return None
    if not isinstance(value, str) or not ATTEMPT_PATTERN.fullmatch(value):
        raise JobError("jobs.attempt_invalid")
    return value


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pin_root(data_dir: str | Path) -> Path:
    return Path(data_dir) / PIN_DIRNAME


def attempt_record_path(data_dir: str | Path, attempt_id: str) -> Path:
    return pin_root(data_dir) / "attempts" / f"{attempt_id}.json"


def binary_path(data_dir: str | Path, digest: str, name: str) -> Path:
    return pin_root(data_dir) / "binaries" / digest / name


def _store(root: Path, source: Path, digest: str) -> Path:
    """Copy *source* into the content-addressed store and return the copy's path."""
    target = root / "binaries" / digest / source.name
    if target.is_file() and file_digest(target) == digest:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise JobError("jobs.editor_pin_lost")
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".editor-")
    try:
        with os.fdopen(handle, "wb") as out, source.open("rb") as stream:
            while chunk := stream.read(_CHUNK):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if file_digest(Path(temporary)) != digest:
            raise JobError("jobs.editor_pin_changed")
        # The copy is what a capture launches, so it has to stay executable even
        # when the shared build is not (a Windows mount can carry no exec bit).
        os.chmod(temporary, 0o755)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def _recorded(data_dir: str | Path, attempt_id: str) -> dict | None:
    record = read_json(attempt_record_path(data_dir, attempt_id))
    if record is None:
        return None
    if not (
        isinstance(record, dict)
        and isinstance(record.get("sha256"), str)
        and SHA256.fullmatch(record["sha256"])
        and isinstance(record.get("path"), str)
    ):
        raise JobError("jobs.editor_pin_lost")
    return record


def _reusable(data_dir: str | Path, attempt_id: str, record: dict) -> dict:
    """The attempt's own pin, proven still to be the bytes it recorded."""
    path = Path(record["path"])
    root = pin_root(data_dir).resolve()
    try:
        inside = path.resolve().is_relative_to(root)
    except OSError:
        inside = False
    if not inside or not path.is_file() or file_digest(path) != record["sha256"]:
        raise JobError("jobs.editor_pin_lost")
    return {**record, ATTEMPT_ID: attempt_id}


def pin_editor(
    editor: str | Path, data_dir: str | Path, attempt_id: str | None = None
) -> dict:
    """The editor build this submission renders with, copied under the pin store.

    The returned record is what the job contract carries: the digest the render
    profile will name, the copy the capture launches, the shared build it came
    from and, for an attempt, the attempt that fixed it.
    """
    attempt_id = validate_attempt_id(attempt_id)
    root = pin_root(data_dir)
    if attempt_id is not None:
        recorded = _recorded(data_dir, attempt_id)
        if recorded is not None:
            return _reusable(data_dir, attempt_id, recorded)
    source = Path(editor)
    digest = file_digest(source)
    stored = _store(root, source, digest)
    record = {
        "sha256": digest,
        "path": str(stored),
        "source": str(source),
        "pinned_at": time.time(),
        ATTEMPT_ID: attempt_id,
    }
    if attempt_id is not None:
        record_file = attempt_record_path(data_dir, attempt_id)
        record_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(record_file, record)
    return record


def prune_pins(data_dir: str | Path, *, now: float | None = None) -> int:
    """Drop pins nothing can still be using; return how many files went away.

    An attempt's record is kept while it is inside the retention window, and a
    binary is kept while any surviving record names it — so an attempt in
    flight keeps its build even though nothing else refers to the file, and a
    rebuild between attempts does not accumulate copies forever.
    """
    root = pin_root(data_dir)
    if not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    removed, live = 0, set()
    attempts = root / "attempts"
    for record_file in sorted(attempts.glob("*.json")) if attempts.is_dir() else []:
        record = read_json(record_file)
        if record_file.stat().st_mtime >= cutoff and isinstance(record, dict):
            live.add(record.get("sha256"))
            continue
        record_file.unlink(missing_ok=True)
        removed += 1
    binaries = root / "binaries"
    for directory in sorted(binaries.glob("*")) if binaries.is_dir() else []:
        if not directory.is_dir() or directory.name in live:
            continue
        stale = directory.stat().st_mtime < cutoff
        for member in sorted(directory.iterdir()):
            if stale or member.stat().st_mtime < cutoff:
                member.unlink(missing_ok=True)
                removed += 1
        if not any(directory.iterdir()):
            directory.rmdir()
    return removed