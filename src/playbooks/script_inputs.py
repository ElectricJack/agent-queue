"""Proposal-time immutable inputs and conservative, process-safe quota reservations.

Source reads happen only here, at proposal time. Execution takes opaque digests
and validates bytes again; no source path is opened by a running script backend.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from src.git.manager import GitManager
from src.playbooks.script_policy import (
    Digest, FrozenModel, Identifier, MIB, ScriptError, ScriptProgram, relative_path,
)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def open_beneath(root: Path, relative: str, *, flags: int = os.O_RDONLY) -> int:
    """Walk with directory FDs and O_NOFOLLOW, including every root ancestor."""
    relative_path(relative)
    absolute = root.absolute()
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in [*absolute.parts[1:], *relative.split("/")[:-1]]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nxt
        return os.open(relative.split("/")[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
    except FileNotFoundError:
        raise ScriptError("script_path_missing") from None
    except OSError:
        raise ScriptError("script_path_unavailable") from None
    finally:
        os.close(directory)


def read_file(root: Path, relative: str, limit: int, *, immutable: bool = False) -> bytes:
    fd = open_beneath(root, relative)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_nlink != 1
                or (immutable and info.st_mode & 0o222)):
            raise ScriptError("script_input_invalid")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ScriptError("script_input_limit_exceeded")
        return data


class InputPin(FrozenModel):
    path: str
    sha256: Digest
    byte_count: StrictInt = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _path(cls, value):
        return relative_path(value)


class RuntimeFile(InputPin):
    executable: bool = False


class RuntimeManifest(FrozenModel):
    version: Literal[1] = 1
    runtime: Identifier
    # This fixed, reviewed executable must terminate successfully with no args.
    # Doctor runs it in disposable namespaces, never runs a source program.
    probe: str = "usr/bin/true"
    files: tuple[RuntimeFile, ...] = Field(min_length=1, max_length=16384)

    @model_validator(mode="after")
    def _unique(self):
        relative_path(self.probe)
        names = [file.path for file in self.files]
        if len(set(names)) != len(names):
            raise ValueError("duplicate runtime path")
        # Runtime roots must be flattened by the operator: no symlinks, device
        # files, sockets or setuid programs are accepted in a manifest/tree.
        if any(name.split("/")[0] in {"proc", "dev", "sys", "scratch", "inputs", "home"}
               for name in names):
            raise ValueError("runtime file shadows a sandbox mount")
        return self

    @property
    def sha256(self) -> str:
        return digest(canonical(self.model_dump(mode="json")))


class PinnedScript(FrozenModel):
    version: Literal[1] = 1
    program: ScriptProgram
    program_sha256: Digest
    source_revision: str
    inputs: tuple[InputPin, ...] = ()
    runtime_sha256: Digest
    executable_sha256: Digest

    @model_validator(mode="after")
    def _integrity(self):
        if self.program_sha256 != digest(canonical(self.program.model_dump(mode="json"))):
            raise ValueError("program digest mismatch")
        expected = set(self.program.supporting_files)
        if self.program.script is not None:
            expected.add(self.program.script)
        paths = [item.path for item in self.inputs]
        if set(paths) != expected or len(paths) != len(set(paths)):
            raise ValueError("script input manifest does not match declared sources")
        if len(self.source_revision) > 128 or not self.source_revision:
            raise ValueError("source revision required")
        return self


class InputStore:
    """Append-only content-addressed blobs; overwrite/tamper is never repaired.

    Directories belong to the daemon, not the execution identity. File modes
    protect against accidental edits; cryptographic verification is mandatory.
    """

    def __init__(self, root: Path, *, quota_bytes: int = 2 * 1024**3):
        self.root = root
        self.quota_bytes = quota_bytes

    def put(self, data: bytes) -> str:
        key = digest(data)
        self.root.mkdir(mode=0o755, parents=True, exist_ok=True)
        fd = open_beneath(self.root, "store.lock", flags=os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if (self.root / key).exists() or (self.root / key).is_symlink():
                if self.read(key, len(data)) != data:
                    raise ScriptError("script_input_digest_mismatch")
                return key
            used = 0
            for path in self.root.iterdir():
                if re.fullmatch(r"[0-9a-f]{64}", path.name):
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        raise ScriptError("script_input_invalid")
                    used += info.st_size
            if used + len(data) > self.quota_bytes:
                raise ScriptError("script_input_storage_quota")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o444)
                    os.fsync(stream.fileno())
                temporary.rename(self.root / key)
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return key
        finally:
            os.close(fd)

    def read(self, key: str, size: int) -> bytes:
        if re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise ScriptError("script_input_digest_invalid")
        data = read_file(self.root, key, size, immutable=True)
        if len(data) != size or digest(data) != key:
            raise ScriptError("script_input_digest_mismatch")
        return data


async def pin_script_inputs(
    program: ScriptProgram, *, store: InputStore, source_root: Path,
    source_revision: str, runtime: RuntimeManifest, max_bytes: int = 64 * MIB,
) -> PinnedScript:
    """Server-side proposal seam; repository revision must already be authorized.

    ``source_root`` is the approved repository or the caller's scoped vault
    directory, never an authored host path. Runtime identity comes from the
    operator's manifest, not compiler-provided digests.
    """
    import asyncio

    if runtime.runtime != program.runtime:
        raise ScriptError("script_runtime_unavailable")
    executable = next((item for item in runtime.files if item.path == program.executable), None)
    if executable is None or not executable.executable:
        raise ScriptError("script_executable_unavailable")
    if program.source_kind == "repository" and not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ScriptError("script_revision_not_pinned")
    names = sorted(set(program.supporting_files) | ({program.script} if program.script else set()))
    manager = GitManager()
    total, pins = 0, []
    for name in names:
        if program.source_kind == "repository":
            raw = await manager._arun(
                ["--no-replace-objects", "ls-tree", "-z", "-l", source_revision,
                 "--", f":(literal){name}"],
                cwd=str(source_root),
            )
            records = [item for item in raw.split("\0") if item]
            if len(records) != 1:
                raise ScriptError("script_source_unavailable")
            metadata, found = records[0].split("\t", 1)
            mode, kind, oid, size = metadata.split()
            if found != name or mode not in {"100644", "100755"} or kind != "blob":
                raise ScriptError("script_source_not_regular")
            if int(size) > max_bytes - total:
                raise ScriptError("script_input_limit_exceeded")
            data = await manager.aread_blob(str(source_root), oid, max_bytes=max_bytes - total)
        else:
            data = await asyncio.to_thread(read_file, source_root, name, max_bytes - total)
        total += len(data)
        key = await asyncio.to_thread(store.put, data)
        pins.append(InputPin(path=name, sha256=key, byte_count=len(data)))
    return PinnedScript(
        program=program, program_sha256=digest(canonical(program.model_dump(mode="json"))),
        source_revision=source_revision, inputs=tuple(pins),
        runtime_sha256=runtime.sha256, executable_sha256=executable.sha256,
    )


class QuotaLedger:
    """Reserve before launch and retain until protected output is safely retired.

    One host file/lock serializes logical input/output reservations. Crashes leak
    reservations conservatively: no automatic expiry can erase unsettled evidence.
    E2 owns releasing them only after its durable storage/retention decision.
    """

    def __init__(self, root: Path, *, input_bytes: int, output_bytes: int, per_run_bytes: int):
        self.root = root
        self.input_bytes = input_bytes
        self.output_bytes = output_bytes
        self.per_run_bytes = per_run_bytes

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = open_beneath(self.root, "quota.lock", flags=os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                rows = json.loads(read_file(self.root, "quota.json", 16 * MIB))
            except ScriptError as exc:
                if exc.code != "script_path_missing":
                    raise
                rows = {}
            yield rows
            payload = canonical(rows)
            if len(payload) > 16 * MIB:
                raise ScriptError("script_quota_ledger_full")
            from src.jobs.artifacts import atomic_json

            atomic_json(self.root / "quota.json", rows)
        finally:
            os.close(fd)

    def reserve(self, key: str, run_id: str, *, input_bytes: int, output_bytes: int) -> None:
        if (not re.fullmatch(r"[0-9a-f]{64}", key) or not run_id or len(run_id) > 128
                or any(type(size) is not int or size < 0 for size in (input_bytes, output_bytes))):
            raise ScriptError("script_quota_request_invalid")
        record = {"run_id": run_id, "input": input_bytes, "output": output_bytes}
        with self._locked() as rows:
            if key in rows:
                if rows[key] != record:
                    raise ScriptError("script_quota_identity_conflict")
                return
            if (sum(row["input"] for row in rows.values()) + input_bytes > self.input_bytes
                    or sum(row["output"] for row in rows.values()) + output_bytes > self.output_bytes
                    or sum(row["input"] + row["output"] for row in rows.values()
                           if row["run_id"] == run_id) + input_bytes + output_bytes
                    > self.per_run_bytes):
                raise ScriptError("script_storage_quota")
            rows[key] = record

    def release(self, key: str) -> None:
        with self._locked() as rows:
            rows.pop(key, None)
