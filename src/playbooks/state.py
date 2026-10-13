"""Invocation-owned durable state; protected values and immutable pre-scan pages."""
from __future__ import annotations

import contextvars
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass

from src.commands.principal import PrincipalKind, current_principal
from src.database.queries import playbook_state_queries as queries
from src.observations.snapshots import SnapshotService
from src.playbooks.invocation import current_invocation
from src.playbooks.private_values import PrivateOwner, contains_reference


class StateError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def encoded(value) -> bytes:
    from src.commands.contracts.generated import validate_json

    try:
        validate_json(value)
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode()
    except (TypeError, ValueError, RecursionError):
        raise StateError("state.invalid_json") from None


def digest(value) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


@dataclass(frozen=True)
class Namespace:
    scope_kind: str
    scope_owner: str
    playbook_id: str
    state_schema_version: int = 1

    def __post_init__(self):
        if (self.scope_kind not in {"system", "project", "profile"}
                or not isinstance(self.scope_owner, str) or not self.scope_owner.strip()
                or not isinstance(self.playbook_id, str) or not self.playbook_id.strip()
                or len(self.scope_owner) > 256 or len(self.playbook_id) > 256
                or (self.scope_kind == "system" and self.scope_owner != "system")
                or type(self.state_schema_version) is not int
                or not 1 <= self.state_schema_version <= 2_147_483_647):
            raise StateError("state.invalid_namespace")

    def as_dict(self):
        return dict(scope_kind=self.scope_kind, scope_owner=self.scope_owner,
                    playbook_id=self.playbook_id, state_schema_version=self.state_schema_version)

    @property
    def identifier(self):
        return digest(self.as_dict())

    @property
    def owner(self):
        return PrivateOwner("namespace", self.identifier, self.scope_kind, self.scope_owner)


@dataclass(frozen=True)
class Limits:
    value_bytes: int = 256 * 1024
    keys: int = 10_000
    namespace_bytes: int = 16 * 1024 * 1024
    journal_bytes: int = 256 * 1024 * 1024


# A readback handle is never authority. The command establishes membership after
# verifying its invocation, pinned artifact, source and current capability.
_membership = contextvars.ContextVar("_state_namespace_membership", default=None)


@contextmanager
def namespace_access(namespace, principal):
    token = _membership.set((namespace.owner, principal, current_invocation()))
    try:
        yield
    finally:
        _membership.reset(token)


def authorized_private_namespace(owner, principal, invocation):
    return _membership.get() == (owner, principal, invocation)


async def resolve_namespace(handler, command: str) -> Namespace:
    """Only a live PLAYBOOK invocation can choose an ordinary state namespace."""
    from src.playbooks.authoring import _parse_frontmatter
    from src.playbooks.definition import artifact_sha256, scope_from_v1
    from src.playbooks.features import execution_metadata_from_source

    principal, invocation = current_principal(), current_invocation()
    if (principal is None or principal.kind is not PrincipalKind.PLAYBOOK
            or invocation is None
            or not principal.policy.allows_aq_command(command)
            or command not in invocation.aq_commands):
        raise StateError("unauthorized")
    try:
        store = handler._v2_engine().services.artifact_store
        definition = store.load(invocation.artifact_ref.artifact_sha256)
        source = store.load_source(invocation.artifact_ref.artifact_sha256)
        frontmatter, _ = _parse_frontmatter(source)
        metadata = execution_metadata_from_source(frontmatter, definition)
        if (definition.id != invocation.artifact_ref.playbook_id
                or artifact_sha256(definition) != invocation.artifact_ref.artifact_sha256
                or definition.source_hash != invocation.artifact_ref.source_digest
                or definition.id != frontmatter.get("id")
                or definition.scope != scope_from_v1(frontmatter.get("scope", ""))
                or metadata != definition.trusted_execution or metadata is None
                or metadata.state_schema_version is None
                or invocation.step_id not in definition.steps):
            raise StateError("state.invalid_pin")
        scope = definition.scope
        scope_kind = "profile" if scope.type == "agent_type" else scope.type
        owner = (scope.project_id if scope_kind == "project" else
                 scope.agent_type if scope_kind == "profile" else "system")
        namespace = Namespace(scope_kind, owner, definition.id, metadata.state_schema_version)
    except StateError:
        raise
    except Exception:
        raise StateError("state.invalid_pin") from None
    if ((namespace.scope_kind == "project" and principal.project_id != namespace.scope_owner)
            or (namespace.scope_kind != "project" and principal.project_id is not None)
            or (namespace.scope_kind == "profile" and principal.profile_id != namespace.scope_owner)):
        raise StateError("unauthorized")
    return namespace


NOTIFICATION_FIELDS = (
    "episode", "active", "source_partition", "first_seen", "last_seen", "last_notified",
    "last_observation_id", "last_observed_at", "message_id", "replay_floor",
)


def project_entry(row, *, key, record_space, now):
    result = dict(key=key, record_space=record_space, exists=False, revision=0,
                  tombstone=False, expired=False, expires_at=None, value=None)
    if row is None:
        return result
    expired = row["expires_at"] is not None and row["expires_at"] <= now
    result.update(revision=row["revision"], tombstone=row["tombstone"], expired=expired,
                  exists=not row["tombstone"] and not expired, expires_at=row["expires_at"],
                  created_at=row["created_at"], updated_at=row["updated_at"])
    if record_space == "notification":
        result.update({field: row[field] for field in NOTIFICATION_FIELDS})
    return result


class StateService:
    def __init__(self, engine, private_values, *, limits=Limits()):
        self.engine, self.private, self.limits = engine, private_values, limits

    async def _read(self, reference, namespace, principal, conn=None):
        with namespace_access(namespace, principal):
            return await self.private.read(reference, owner=namespace.owner,
                                           principal=principal, conn=conn)

    async def get(self, namespace, principal, *, key, record_space="kv"):
        async with self.engine.connect() as conn:
            row = await queries.entry_row(conn, namespace, record_space, key)
            now = await queries.transaction_time(conn)
            result = project_entry(row, key=key, record_space=record_space, now=now)
            if result["exists"] and row["value_reference"] is not None:
                result["value"] = await self._read(row["value_reference"], namespace, principal, conn)
        return {"success": True, **result}

    async def replay(self, conn, namespace, principal, operation_key, request):
        row = await queries.operation_row(conn, namespace, operation_key)
        if row is None:
            return None
        if row["request_digest"] != digest(request):
            raise StateError("idempotency_conflict")
        return await self._read(row["result_reference"], namespace, principal, conn)

    async def mutate(self, namespace, principal, *, action, key, expected_revision,
                     operation_key, value=None, ttl_seconds=None):
        if action not in {"set", "delete"}:
            raise StateError("state.invalid_arguments")
        payload_bytes = len(encoded(value)) if action == "set" else 0
        if contains_reference(value):
            raise StateError("state.reference_input_forbidden")
        if payload_bytes > self.limits.value_bytes:
            raise StateError("state.value_oversize")
        request = dict(action=action, key=key, expected_revision=expected_revision,
                       value=value, ttl_seconds=ttl_seconds)
        self.private.require_ready()
        async with queries.locked_namespace(self.engine, namespace) as (conn, locked):
            replay = await self.replay(conn, namespace, principal, operation_key, request)
            if replay is not None:
                return replay
            row = await queries.entry_row(conn, namespace, "kv", key, lock=True)
            revision = row["revision"] if row else 0
            if revision != expected_revision:
                raise StateError("conflict")
            now = await queries.transaction_time(conn)
            batch = self.private.batch(namespace.owner)
            values = dict(record_space="kv", key=key, revision=revision + 1,
                          value_reference=batch.protect(value) if action == "set" else None,
                          value_bytes=payload_bytes, tombstone=action == "delete",
                          expires_at=now + ttl_seconds if ttl_seconds is not None else None,
                          created_at=row["created_at"] if row else now, updated_at=now)
            result = {"success": True, **project_entry(values, key=key, record_space="kv", now=now),
                      "namespace_revision": locked["revision"] + 1}
            # Mutation receipts carry metadata, not a second copy of a possibly 256 KiB value.
            result.pop("value")
            result_reference = batch.protect(result)
            await queries.write_entry(conn, namespace, values)
            await queries.commit_operation(
                conn, namespace, locked, operation_key=operation_key, request_digest=digest(request),
                result_reference=result_reference, result=result,
                journal_bytes=(len(encoded(request)) + len(encoded(result))
                               + len(encoded(operation_key))),
                value_bytes=locked["value_bytes"] + payload_bytes - (row["value_bytes"] if row else 0),
                key_count=locked["key_count"] + (0 if row else 1), limits=self.limits, now=now,
            )
            await batch.persist(conn)
        return result

    async def list(self, namespace, principal, *, producer="playbook_state_list", **args):
        request = {"namespace": namespace.as_dict(), "prefix": args.get("prefix", ""),
                   "record_space": args.get("record_space", "kv"),
                   "page_size": args.get("page_size", 500)}
        snapshots = SnapshotService(self.engine, self.private)
        scope = (namespace.scope_kind, namespace.scope_owner)
        if args.get("snapshot_id"):
            return await snapshots.read(producer=producer, request=request, principal=principal,
                                        scope=scope, snapshot_id=args["snapshot_id"],
                                        page_token=args["page_token"])
        await self.private.check_ready()
        rows, revision, now = await queries.snapshot_rows(
            self.engine, namespace, record_space=request["record_space"], prefix=request["prefix"])
        if len(rows) > self.limits.keys:
            raise StateError("state.quota_exceeded")
        entries, identities = [], []
        for row in rows:
            result = project_entry(row, key=row["key"], record_space=row["record_space"], now=now)
            identities.append({key: result[key] for key in (
                "record_space", "key", "revision", "exists", "tombstone", "expired",
                "active", "source_partition") if key in result})
            if result["exists"] and row["value_reference"] is not None:
                result["value"] = await self._read(row["value_reference"], namespace, principal)
            entries.append(result)
        return await snapshots.materialize(
            producer=producer, request=request, principal=principal, scope=scope,
            collections={"entries": entries}, identities=identities,
            coverage=[{"source": producer, "complete": True, "observed": len(rows)}],
            complete=True, observed_at=now, page_size=request["page_size"],
            metadata={"namespace": namespace.as_dict(), "namespace_revision": revision},
        )

    async def reset(self, namespace, principal, *, expected_revision, reason, operation_key):
        from sqlalchemy import func, select, update
        from src.database import tables as t

        if principal.kind not in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}:
            raise StateError("operator_only")
        request = dict(action="reset", expected_revision=expected_revision, reason=reason)
        self.private.require_ready()
        async with queries.locked_namespace(self.engine, namespace, provision=True) as (conn, locked):
            replay = await self.replay(conn, namespace, principal, operation_key, request)
            if replay is not None:
                return replay
            if locked["revision"] != expected_revision:
                raise StateError("conflict")
            now = await queries.transaction_time(conn)
            replay_floor = max(locked["replay_floor"] or now, now)
            table = t.playbook_state_entries
            # Namespace lock already excludes writers. Entry locks retain the same D2 order.
            await conn.execute(select(table.c.key).where(
                table.c.namespace_id == namespace.identifier).with_for_update())
            await conn.execute(update(table).where(table.c.namespace_id == namespace.identifier)
                               .values(revision=table.c.revision + 1, tombstone=True, active=False,
                                       value_reference=None, value_bytes=0, expires_at=None,
                                       replay_floor=func.greatest(table.c.replay_floor, replay_floor),
                                       updated_at=now))
            result = dict(success=True, revision=locked["revision"] + 1,
                          namespace_revision=locked["revision"] + 1, reset_count=locked["key_count"],
                          warning="Suppression reset: future observations may deliberately re-alert.",
                          reason=reason, replay_floor=replay_floor)
            batch = self.private.batch(namespace.owner)
            await queries.commit_operation(
                conn, namespace, locked, operation_key=operation_key, request_digest=digest(request),
                result_reference=batch.protect(result), result=result,
                journal_bytes=(len(encoded(request)) + len(encoded(result))
                               + len(encoded(operation_key))),
                value_bytes=0,
                key_count=locked["key_count"], limits=self.limits, now=now, replay_floor=replay_floor,
            )
            await batch.persist(conn)
        return result
