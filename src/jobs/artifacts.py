"""Fsync/rename receipts and bounded output with logical cursors.

Tail blocks carry hashes in the manifest. A crash during overwrite invalidates
that block explicitly instead of serving new bytes at an old logical offset.
No artifact is opened through a symlink; job ids are canonical UUIDs.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

HEAD_BYTES = 1024 * 1024
TAIL_BYTES = 63 * 1024 * 1024
BLOCK_BYTES = 64 * 1024


def job_directory(data_dir: Path, job_id: str) -> Path:
    if str(uuid.UUID(job_id)) != job_id:
        raise ValueError("invalid job id")
    runs = data_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    if runs.is_symlink():
        raise ValueError("symlink run root")
    path = runs / job_id
    path.mkdir(exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("symlink job directory")
    return path


def open_artifact(path: Path, flags: int, mode: int = 0o600) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, mode)


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = open_artifact(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict | None:
    try:
        fd = open_artifact(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r") as stream:
        return json.load(stream)


class OutputStore:
    def __init__(
        self, directory: Path, *, head_bytes=HEAD_BYTES, tail_bytes=TAIL_BYTES, readonly=False
    ):
        if head_bytes < 1 or tail_bytes < 1:
            raise ValueError("output capacities must be positive")
        self.directory, self.head_cap, self.tail_cap = directory, head_bytes, tail_bytes
        self.block = min(BLOCK_BYTES, tail_bytes)
        self.manifest = read_json(directory / "manifest.json") or {
            "version": 1,
            "seen": 0,
            "head": 0,
            "tail_size": 0,
            "hashes": {},
        }
        flags = os.O_RDONLY if readonly else os.O_RDWR | os.O_CREAT
        self._head = open_artifact(directory / "output.head", flags)
        try:
            self._tail = open_artifact(directory / "output.tail", flags)
        except OSError:
            os.close(self._head)
            raise
        self.failed = False

    @staticmethod
    def _write(fd, data, offset):
        while data:
            size = os.pwrite(fd, data, offset)
            if not size:
                raise OSError("short output write")
            offset += size
            data = data[size:]

    def append(self, data: bytes) -> None:
        for offset in range(0, len(data), self.block):
            self._append(data[offset : offset + self.block])

    def _append(self, data: bytes) -> None:
        start = self.manifest["seen"]
        remaining = self.head_cap - self.manifest["head"]
        if remaining:
            head = data[:remaining]
            self._write(self._head, head, self.manifest["head"])
            self.manifest["head"] += len(head)
            os.fsync(self._head)
            data = data[len(head) :]
            start += len(head)
        offset = 0
        touched = set()
        while offset < len(data):
            physical = (start + offset - self.head_cap) % self.tail_cap
            count = min(len(data) - offset, self.tail_cap - physical)
            self._write(self._tail, data[offset : offset + count], physical)
            touched.update(range(physical // self.block, (physical + count - 1) // self.block + 1))
            self.manifest["tail_size"] = max(self.manifest["tail_size"], physical + count)
            offset += count
        if data:
            os.fsync(self._tail)
        for index in touched:
            raw = os.pread(
                self._tail,
                min(self.block, self.manifest["tail_size"] - index * self.block),
                index * self.block,
            )
            self.manifest["hashes"][str(index)] = hashlib.sha256(raw).hexdigest()
        self.manifest["seen"] = start + len(data)
        atomic_json(self.directory / "manifest.json", self.manifest)

    def read(self, after: int = 0, limit: int = 65536) -> dict:
        if after < 0 or not 1 <= limit <= 1024 * 1024:
            raise ValueError("invalid output cursor/limit")
        chunks, gaps = [], []
        seen = self.manifest["seen"]
        cursor = min(after, seen)
        remaining = limit

        def add(data):
            nonlocal cursor, remaining
            if chunks and chunks[-1]["offset"] + len(chunks[-1]["data"]) == cursor:
                chunks[-1]["data"] += data
            else:
                chunks.append({"offset": cursor, "data": data})
            cursor += len(data)
            remaining -= len(data)

        if cursor < self.manifest["head"]:
            count = min(remaining, self.manifest["head"] - cursor)
            raw = os.pread(self._head, count, cursor)
            if len(raw) != count:
                self.failed = True
                gaps.append({"after": cursor, "next": cursor + count})
                cursor += count
            else:
                add(raw)
        tail_start = max(self.head_cap, seen - self.tail_cap)
        if remaining and cursor < seen and cursor < tail_start:
            gaps.append({"after": cursor, "next": tail_start})
            cursor = tail_start
        while remaining and cursor < seen:
            physical = (cursor - self.head_cap) % self.tail_cap
            index, inside = divmod(physical, self.block)
            count = min(self.block, self.manifest["tail_size"] - index * self.block)
            raw = os.pread(self._tail, count, index * self.block)
            available = min(count - inside, seen - cursor, remaining)
            if available <= 0:
                self.failed = True
                gaps.append({"after": cursor, "next": seen})
                cursor = seen
                break
            if hashlib.sha256(raw).hexdigest() != self.manifest["hashes"].get(str(index)):
                self.failed = True
                gaps.append({"after": cursor, "next": cursor + available})
                cursor += available
            else:
                add(raw[inside : inside + available])
        return {"chunks": chunks, "gaps": gaps, "next": cursor, "seen": seen}

    def stats(self) -> dict:
        seen = self.manifest["seen"]
        kept = min(seen, self.head_cap) + min(max(0, seen - self.head_cap), self.tail_cap)
        return {"output_bytes_seen": seen, "output_bytes_retained": kept, "truncated": kept < seen}

    def close(self):
        os.close(self._head)
        os.close(self._tail)
