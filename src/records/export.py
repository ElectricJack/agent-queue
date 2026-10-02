"""Deterministic Markdown and confined, crash-recoverable managed exports.

The filesystem lock serializes exporters across daemon instances. Database
transactions only hydrate/recheck/checkpoint; all filesystem I/O occurs outside
them. A delayed event cannot replace a newer acknowledged or renamed export.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.commands.principal import TRUSTED_LOCAL
from src.database.tables import record_export_state, record_outbox
from src.knowledge.models import content_hash
from src.records.models import RecordError
from src.records.outbox import lease_matches
from src.records.service import RecordService

MAX_EXPORT_BYTES = 2 * 1024 * 1024


def managed_parts(scope_key, alias):
    if not isinstance(alias, str) or not re.fullmatch(r"kn-[0-9a-f]{32}", alias):
        raise RecordError("record.export_path")
    if scope_key == "global":
        return ("system", "knowledge", "records", alias + ".md")
    kind, _, project = scope_key.partition(":")
    if (
        kind != "project"
        or not project
        or project in {".", ".."}
        or any(c in project for c in ("/", "\\", "\0"))
    ):
        raise RecordError("record.export_path")
    return ("projects", project, "knowledge", "records", alias + ".md")


def render_export(installation_id, record, revision, snapshot=None) -> bytes:
    doc = revision["snapshot"] if snapshot is None else snapshot
    if doc is None:
        raise RecordError("record.revision_redacted")
    header = dict(
        format_version=1,
        installation_id=str(installation_id),
        record_id=str(record["record_id"]),
        revision_id=str(revision["revision_id"]),
        sequence=revision["sequence"],
        content_sha256=revision["content_sha256"],
    )
    # JSON scalars are a YAML subset; do not interpolate user text into YAML.
    frontmatter = "\n".join(f"{key}: {json.dumps(value)}" for key, value in header.items())
    metadata = {key: value for key, value in doc.items() if key not in {"title", "body"}}
    return (
        f"---\n{frontmatter}\n---\n\n# {doc['title']}\n\n{doc['body']}\n\n"
        "## Record metadata\n\n```json\n"
        + json.dumps(metadata, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n```\n"
    ).encode("utf-8")


def _directory(root, parts, *, create=False):
    path = Path(root).expanduser().absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for ordinal, component in enumerate((*path.parts[1:], *parts)):
            if component in {"", ".", ".."}:
                raise RecordError("record.export_path")
            if create and ordinal >= len(path.parts) - 1:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_at(fd, name):
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_EXPORT_BYTES:
            raise RecordError("record.export_diverged")
        with os.fdopen(handle, "rb", closefd=False) as stream:
            value = stream.read(MAX_EXPORT_BYTES + 1)
        after = os.fstat(handle)
        if len(value) > MAX_EXPORT_BYTES or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RecordError("record.export_diverged")
        return value
    finally:
        os.close(handle)


def read_managed(root, scope_key, alias):
    parts = managed_parts(scope_key, alias)
    try:
        fd = _directory(root, parts[:-1])
    except FileNotFoundError:
        return None
    try:
        return _read_at(fd, parts[-1])
    finally:
        os.close(fd)


class ManagedFile:
    def __init__(self, root, scope_key, alias):
        self.root = root
        self.parts = managed_parts(scope_key, alias)
        self.fd = _directory(root, self.parts[:-1], create=True)
        self.lock_fd = None
        self.temp_name = None
        try:
            self.lock_fd = os.open(
                f".{alias}.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=self.fd,
            )
            if not stat.S_ISREG(os.fstat(self.lock_fd).st_mode):
                raise RecordError("record.export_path")
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RecordError("record.export_busy") from None
        except BaseException:
            self.close()
            raise

    def read(self):
        return _read_at(self.fd, self.parts[-1])

    def stage(self, value):
        self.temp_name = f".{self.parts[-1]}.{uuid4().hex}.tmp"
        handle = os.open(
            self.temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        with os.fdopen(handle, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())

    def publish(self, expected):
        # Reopen the named parents before publication to catch a replaced
        # directory/symlink; rename itself uses the pinned directory descriptor.
        current_fd = _directory(self.root, self.parts[:-1])
        try:
            current, pinned = os.fstat(current_fd), os.fstat(self.fd)
            if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise RecordError("record.export_path")
            if self.read() != expected:
                raise RecordError("record.export_diverged")
            os.replace(self.temp_name, self.parts[-1], src_dir_fd=self.fd, dst_dir_fd=self.fd)
            self.temp_name = None
            os.fsync(self.fd)
        finally:
            os.close(current_fd)

    def close(self):
        if self.temp_name:
            try:
                os.unlink(self.temp_name, dir_fd=self.fd)
            except FileNotFoundError:
                pass
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        os.close(self.fd)


async def _io(callback, *args):
    # Cancellation cannot release a lock/fd while its filesystem thread is
    # still renaming. If acknowledgment is interrupted, replay hashes bytes.
    task = asyncio.create_task(asyncio.to_thread(callback, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = await task
        if isinstance(result, ManagedFile):
            # Cancellation during acquisition must not strand its file lock.
            await asyncio.to_thread(result.close)
        raise


class RecordExporter:
    def __init__(self, db, config, vault_root, *, clock=None):
        from datetime import UTC, datetime

        self.db = db
        self._config = config
        self._vault_root = vault_root
        self.clock = clock or (lambda: datetime.now(UTC))
        self._request_slots = asyncio.Semaphore(2)

    @property
    def config(self):
        return self._config() if callable(self._config) else self._config

    @property
    def vault_root(self):
        return self._vault_root() if callable(self._vault_root) else self._vault_root

    async def manual(self, *, identity, principal, project_id, revision_id=None):
        try:
            async with asyncio.timeout(5):
                async with self._request_slots:
                    return await self._manual(
                        identity=identity,
                        principal=principal,
                        project_id=project_id,
                        revision_id=revision_id,
                    )
        except TimeoutError:
            raise RecordError("record.retryable") from None

    async def _manual(self, *, identity, principal, project_id, revision_id):
        service = RecordService(self.db, self.config)
        async with self.db.immediate() as conn:
            access = await service._access(conn, principal, "knowledge_export", project_id)
            record = await service.resolve_on(identity, access, conn=conn)
            revision = await service._revision(record, revision_id, conn=conn)
            if content_hash(revision["snapshot"]) != revision["content_sha256"]:
                raise RecordError("record.hash_divergence")
            safe = await service._safe_snapshot(revision["snapshot"], access, conn=conn)
            installation = await self.db.get_record_installation_on(conn=conn)
        value = render_export(installation, record, revision, safe)
        return dict(
            success=True,
            outcome="read",
            record_id=str(record["record_id"]),
            revision_id=str(revision["revision_id"]),
            content=value.decode(),
            export_sha256=hashlib.sha256(value).hexdigest(),
            format_version=1,
        )

    async def _load(self, event, *, conn):
        if not self.config.export.enabled:
            raise RecordError("record.export_disabled")
        service = RecordService(self.db, self.config)
        project = event["scope_key"].removeprefix("project:")
        access = await service._access(conn, TRUSTED_LOCAL, "knowledge_export", project)
        row = (
            (
                await conn.execute(
                    select(record_outbox).where(record_outbox.c.event_id == event["event_id"])
                )
            )
            .mappings()
            .first()
        )
        if not lease_matches(row, event, self.clock()):
            raise RecordError("record.stale_lease")
        record = await service.resolve_on(
            f"record:{event['aggregate_record_id']}", access, conn=conn
        )
        revision = await service._revision(record, conn=conn)
        if revision["revision_id"] != event["revision_id"]:
            return None
        if content_hash(revision["snapshot"]) != revision["content_sha256"]:
            raise RecordError("record.hash_divergence")
        safe = await service._safe_snapshot(revision["snapshot"], access, conn=conn)
        installation = await self.db.get_record_installation_on(conn=conn)
        state = (
            (
                await conn.execute(
                    select(record_export_state).where(
                        record_export_state.c.record_id == record["record_id"],
                        record_export_state.c.destination == "vault",
                    )
                )
            )
            .mappings()
            .first()
        )
        return record, revision, render_export(installation, record, revision, safe), state

    async def _known_bytes(self, existing, record, state):
        if existing is None:
            return True
        digest = hashlib.sha256(existing).hexdigest()
        if state and digest == state["export_sha256"]:
            return True
        # Recover a rename whose checkpoint never committed. Parse only the
        # small server-rendered frontmatter, never user-controlled YAML bodies.
        try:
            head = existing.split(b"---\n", 2)[1].decode("utf-8")
            values = {
                key: json.loads(value)
                for key, value in (line.split(": ", 1) for line in head.splitlines())
            }
            prior_id = UUID(values["revision_id"])
        except (ValueError, TypeError, AttributeError, KeyError, IndexError, UnicodeError):
            return False
        service = RecordService(self.db, self.config)
        async with self.db.immediate() as conn:
            intent = await conn.scalar(
                select(record_outbox.c.event_id)
                .where(
                    record_outbox.c.aggregate_record_id == record["record_id"],
                    record_outbox.c.revision_id == prior_id,
                    record_outbox.c.destination == "export",
                )
                .limit(1)
            )
            if not intent:
                return False
            access = await service._access(
                conn,
                TRUSTED_LOCAL,
                "knowledge_export",
                record["scope_key"].removeprefix("project:"),
            )
            try:
                prior = await service._revision(record, prior_id, conn=conn)
            except RecordError:
                return False
            if state and prior["sequence"] < state["sequence"]:
                return False
            safe = await service._safe_snapshot(prior["snapshot"], access, conn=conn)
            installation = await self.db.get_record_installation_on(conn=conn)
        return render_export(installation, record, prior, safe) == existing

    async def deliver(self, event):
        managed = None
        try:
            async with self.db.immediate() as conn:
                loaded = await self._load(event, conn=conn)
            if loaded is None:
                return {"state": "superseded"}
            record, _, _, _ = loaded
            managed = await _io(
                ManagedFile, self.vault_root, record["scope_key"], record["knowledge_alias"]
            )
            # Rehydrate after acquiring the cross-process per-record lock.
            async with self.db.immediate() as conn:
                loaded = await self._load(event, conn=conn)
            if loaded is None:
                return {"state": "superseded"}
            record, revision, value, state = loaded
            if state and state["diverged_at"] is not None:
                raise RecordError("record.export_diverged")
            existing = await _io(managed.read)
            if existing != value:
                if not await self._known_bytes(existing, record, state):
                    raise RecordError("record.export_diverged")
                await _io(managed.stage, value)
                async with self.db.immediate() as conn:
                    latest = await self._load(event, conn=conn)
                if latest is None:
                    return {"state": "superseded"}
                if latest[2] != value:
                    raise RecordError("record.export_retry")
                await _io(managed.publish, existing)
            async with self.db.immediate() as conn:
                # A crash here leaves exact bytes that the next lease recognizes.
                latest = await self._load(event, conn=conn)
                if latest is None:
                    return {"state": "superseded"}
                values = dict(
                    record_id=record["record_id"],
                    destination="vault",
                    revision_id=revision["revision_id"],
                    sequence=revision["sequence"],
                    export_sha256=hashlib.sha256(value).hexdigest(),
                    exported_at=self.clock(),
                    diverged_at=None,
                )
                await conn.execute(
                    pg_insert(record_export_state)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[
                            record_export_state.c.record_id,
                            record_export_state.c.destination,
                        ],
                        set_=values,
                        where=record_export_state.c.sequence <= revision["sequence"],
                    )
                )
            return {"state": "exported", "export_sha256": values["export_sha256"]}
        except RecordError as exc:
            if exc.code == "record.export_diverged":
                async with self.db.immediate() as conn:
                    await conn.execute(
                        update(record_export_state)
                        .where(
                            record_export_state.c.record_id == event["aggregate_record_id"],
                            record_export_state.c.destination == "vault",
                        )
                        .values(diverged_at=self.clock())
                    )
            raise
        except OSError:
            raise RecordError("record.export_filesystem") from None
        finally:
            if managed is not None:
                await _io(managed.close)
