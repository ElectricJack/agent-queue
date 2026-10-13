"""Immutable observation transport. Tokens locate pages; they grant no authority.

Producers pass a bounded, completed scan, compact public identities and coverage.
Every payload (including oversize inspection artifacts) uses A3 encryption and
commits with its manifest. No continuation has a producer callback to invoke.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import time
from dataclasses import dataclass
from typing import Any

from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.playbooks.invocation import current_invocation
from src.playbooks.private_values import PrivateOwner, PrivateValueError, PrivateValueService

TTL = 1800
PAGE_BYTES = 192 * 1024
RESULT_BYTES = 262_144
MAX_IDENTITIES = 10_000
ENVELOPE_LIMIT = RESULT_BYTES - 4096  # typed value defaults and page receipt fields
PAGING_KEYS = frozenset({"pagination", "snapshot_id", "page_token", "page_size"})


class SnapshotError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def encoded(value: Any) -> bytes:
    # Match the command/expression envelope measurement, including JSON spacing
    # and ASCII escaping. Compact UTF-8 alone would undercount some responses.
    try:
        return json.dumps(value, sort_keys=True, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise SnapshotError("snapshot.invalid_json") from None


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def failure(code: str) -> dict:
    return {"success": False, "complete": False, "execution_status": "incomplete",
            "error": code, "error_code": code, "reasons": [code],
            "snapshot_id": None, "page_tokens": [], "identity_manifest": []}


def requested(args: dict) -> bool:
    return any(args.get(key) is not None for key in PAGING_KEYS)


def validate_paging(args: dict) -> None:
    if args.get("pagination") != "snapshot":
        raise SnapshotError("snapshot.invalid_arguments")
    if bool(args.get("snapshot_id")) != bool(args.get("page_token")):
        raise SnapshotError("snapshot.invalid_arguments")
    for key in ("snapshot_id", "page_token"):
        if args.get(key) is not None and (
            not isinstance(args[key], str) or not args[key] or len(args[key]) > 128
        ):
            raise SnapshotError("snapshot.invalid_arguments")
    size = args.get("page_size", 500)
    if type(size) is not int or not 1 <= size <= 500:
        raise SnapshotError("snapshot.invalid_arguments")


@dataclass(frozen=True)
class Limits:
    per_run: int = 64 * 1024 * 1024
    per_scope: int = 256 * 1024 * 1024
    host: int = 2 * 1024 * 1024 * 1024


def authority(principal: ExecutionPrincipal, producer: str, scope: tuple[str, str]) -> str:
    """Fingerprint the current effective grant and identity, never authored args."""
    if principal is None or principal.unresolved:
        raise SnapshotError("snapshot.unauthorized")
    invocation = current_invocation()
    if principal.enforced:
        if not principal.policy.allows_aq_command(producer):
            raise SnapshotError("snapshot.unauthorized")
        if principal.project_id and scope != ("project", principal.project_id):
            raise SnapshotError("snapshot.unauthorized")
        if scope[0] == "profile" and principal.profile_id != scope[1]:
            raise SnapshotError("snapshot.unauthorized")
        if principal.kind is PrincipalKind.PLAYBOOK and invocation is None:
            raise SnapshotError("snapshot.unauthorized")
    return digest({
        "kind": principal.kind.value, "policy": principal.policy.fingerprint(),
        "session": principal.session_id, "instance": principal.session_instance_token,
        "service": principal.service_name, "task": principal.task_id,
        "project": principal.project_id, "profile": principal.profile_id,
        "elevated": principal.elevated, "scope": scope,
        "run": invocation.run_id if invocation else None,
    })


def verify_authority(row, principal, *, now: float) -> None:
    scope = (row["scope_kind"], row["scope_owner"])
    actual = authority(principal, row["producer"], scope)
    if not hmac.compare_digest(row["authority_fingerprint"], actual):
        raise SnapshotError("snapshot.unauthorized")
    if row["expires_at"] <= now:
        raise SnapshotError("snapshot.expired")


class SnapshotService:
    def __init__(self, engine, private_values: PrivateValueService, *, limits=Limits()):
        self.engine = engine
        self.private = private_values
        self.limits = limits

    async def materialize(
        self, *, producer: str, request: dict, principal: ExecutionPrincipal,
        scope: tuple[str, str], collections: dict[str, list[dict]],
        identities: list[dict], coverage: list[dict], complete: bool,
        observed_at: float, metadata: dict | None = None, page_size: int = 500,
        producer_version: int = 1, interval_started_at: float | None = None,
    ) -> dict:
        from src.database.queries.observation_snapshot_queries import store_snapshot

        if not 1 <= page_size <= 500 or not math.isfinite(observed_at):
            raise SnapshotError("snapshot.invalid_arguments")
        fingerprint = authority(principal, producer, scope)
        self.private.require_ready()
        if len(identities) > MAX_IDENTITIES or len(encoded(identities)) > RESULT_BYTES:
            raise SnapshotError("snapshot.manifest_oversize")
        if sum(map(len, collections.values())) > MAX_IDENTITIES + 500:
            raise SnapshotError("snapshot.too_many_rows")
        snapshot_id = secrets.token_hex(16)
        invocation = current_invocation()
        # Every persisted payload has a snapshot owner, even outside a playbook.
        owner = PrivateOwner("snapshot", snapshot_id, *scope)
        batch = self.private.batch(owner)
        materialized_at = time.time()
        expires_at = materialized_at + TTL
        pages, tokens, artifacts = [], [], []
        payload_bytes = 0

        def save(kind, items, *, artifact=False):
            nonlocal payload_bytes
            payload = {kind: items}
            size = len(encoded(payload))
            payload_bytes += size
            if payload_bytes > min(self.limits.per_scope, self.limits.host,
                                   self.limits.per_run if invocation else self.limits.host):
                raise SnapshotError("snapshot.quota_exceeded")
            token = secrets.token_urlsafe(32)
            ref = batch.protect(payload, expires_at=expires_at)
            pages.append({"snapshot_id": snapshot_id, "ordinal": len(pages), "kind": kind,
                          "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                          "private_reference": ref, "content_digest": digest(payload),
                          "byte_count": size, "row_count": len(items), "artifact": artifact})
            descriptor = {"page_token": token, "kind": kind, "row_count": len(items)}
            if artifact:
                artifacts.append({**descriptor, "reference": ref,
                                  "owner": {"kind": "snapshot", "identifier": snapshot_id,
                                            "scope_kind": scope[0], "scope_owner": scope[1]},
                                  "byte_count": size})
            else:
                tokens.append(descriptor)

        for kind, rows in collections.items():
            items = []
            for row in rows:
                if len(encoded({kind: [row]})) > PAGE_BYTES:
                    if items:
                        save(kind, items)
                        items = []
                    save(kind, [row], artifact=True)
                else:
                    if items and (len(items) >= page_size
                                  or len(encoded({kind: [*items, row]})) > PAGE_BYTES):
                        save(kind, items)
                        items = []
                    items.append(row)
            if items:
                save(kind, items)

        complete = bool(complete and not artifacts)
        response = {**(metadata or {}), "success": complete, "complete": complete,
                    "execution_status": "succeeded" if complete else "incomplete",
                    "snapshot_id": snapshot_id, "producer": producer,
                    "producer_version": producer_version, "observed_at": observed_at,
                    "materialized_at": materialized_at, "expires_at": expires_at,
                    "interval_started_at": interval_started_at or materialized_at,
                    "interval_finished_at": materialized_at,
                    "coverage": coverage, "identity_manifest": identities,
                    "page_tokens": tokens, "artifacts": artifacts,
                    "page_count": len(tokens), **{kind: [] for kind in collections}}
        if artifacts:
            response.update(error_code="snapshot.row_oversize", error="snapshot.row_oversize",
                            reasons=["snapshot.row_oversize"])
        if len(encoded(response)) > ENVELOPE_LIMIT:
            raise SnapshotError("snapshot.manifest_oversize")
        # Only compact safe metadata accompanies pages; identities/tokens are a
        # separate first response, never multiplied by the number of pages.
        envelope = {key: value for key, value in response.items()
                    if key not in {"identity_manifest", "page_tokens", "artifacts"}}
        for page in pages:
            if not page["artifact"] and len(encoded(envelope)) + page["byte_count"] > ENVELOPE_LIMIT:
                raise SnapshotError("snapshot.envelope_oversize")
        row = {"snapshot_id": snapshot_id, "producer": producer,
               "producer_version": producer_version, "authority_fingerprint": fingerprint,
               "scope_kind": scope[0], "scope_owner": scope[1],
               "run_id": invocation.run_id if invocation else None,
               "request_hash": digest(request), "observed_at": observed_at,
               "materialized_at": materialized_at, "expires_at": expires_at,
               "manifest": identities, "coverage": coverage, "envelope": envelope,
               "complete": complete, "page_count": len(pages),
               "content_digest": digest([p["content_digest"] for p in pages]),
               "byte_count": payload_bytes + len(encoded(response))}
        await store_snapshot(self.engine, row, pages, batch, self.limits)
        return response

    async def read(self, *, producer, request, principal, scope, snapshot_id, page_token,
                   producer_version=1) -> dict:
        from src.database.queries.observation_snapshot_queries import load_page

        row, page = await load_page(self.engine, snapshot_id, page_token, self.limits)
        verify_authority(row, principal, now=time.time())
        if (row["producer"] != producer or row["producer_version"] != producer_version
                or (row["scope_kind"], row["scope_owner"]) != scope
                or not hmac.compare_digest(row["request_hash"], digest(request))):
            raise SnapshotError("snapshot.token_mismatch")
        if page["artifact"]:
            raise SnapshotError("snapshot.row_oversize")
        payload = await self.private.read(
            page["private_reference"],
            owner=PrivateOwner("snapshot", snapshot_id, *scope), principal=principal,
        )
        if digest(payload) != page["content_digest"]:
            raise SnapshotError("snapshot.integrity_failed")
        result = {**row["envelope"], **payload, "page_kind": page["kind"],
                  "page_index": page["ordinal"], "page_tokens": [], "identity_manifest": []}
        if len(encoded(result)) > RESULT_BYTES:
            raise SnapshotError("snapshot.envelope_oversize")
        return result

    async def cleanup(self, *, limit=100) -> int:
        from src.database.queries.observation_snapshot_queries import cleanup_expired

        return await cleanup_expired(self.engine, limit=limit)


async def authorize_private_snapshot(engine, owner, principal) -> None:
    """A3 readback gate, also used by authorized oversize artifact inspection."""
    from src.database.queries.observation_snapshot_queries import load_manifest

    try:
        row = await load_manifest(engine, owner.identifier)
        verify_authority(row, principal, now=time.time())
        if (row["scope_kind"], row["scope_owner"]) != (owner.scope_kind, owner.scope_owner):
            raise SnapshotError("snapshot.unauthorized")
    except SnapshotError as exc:
        raise PrivateValueError(exc.code) from None
