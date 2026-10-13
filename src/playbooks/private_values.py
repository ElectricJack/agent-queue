"""Protected playbook data. References are identifiers, never bearer credentials.

Only the daemon composes owners and key rings. No authored sensitivity flag can
change this policy. Encryption batches join the caller's transaction so a failed
run boundary cannot leave committed private data behind.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import insert, select, text

REFERENCE_KEY = "__private_value__"
FORMAT_VERSION = 1


class PrivateValueError(RuntimeError):
    """Safe diagnostics only: never include plaintext, keys or driver errors."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PrivateOwner:
    kind: str
    identifier: str
    scope_kind: str = "system"
    scope_owner: str = "system"

    def __post_init__(self) -> None:
        if self.kind not in {"run", "namespace", "snapshot"} or not self.identifier:
            raise PrivateValueError("private_owner_invalid")
        if self.scope_kind not in {"system", "project", "profile"} or not self.scope_owner:
            raise PrivateValueError("private_owner_invalid")


def is_reference(value: Any) -> bool:
    return isinstance(value, Mapping) and REFERENCE_KEY in value


def canonical_reference(value: Mapping[str, Any]) -> dict[str, str]:
    handle = value.get(REFERENCE_KEY)
    if not isinstance(handle, str) or re.fullmatch(r"[0-9a-f]{32}", handle) is None:
        raise PrivateValueError("private_reference_invalid")
    # Do not trust metadata or extra payload attached to a supplied reference.
    return {REFERENCE_KEY: handle}


def public_value(value: Any) -> Any:
    """A payload-free projection, including for already protected references."""
    if is_reference(value):
        return {**canonical_reference(value), "redacted": True}
    return {"__redacted__": True}


def contains_reference(value: Any) -> bool:
    if is_reference(value):
        return True
    if isinstance(value, Mapping):
        return any(contains_reference(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(contains_reference(item) for item in value)
    return False


def private_result_fields(execution: Any, names: Any) -> frozenset[str]:
    """Only audited descriptor/contract fields are ordinary persisted JSON."""
    if execution.result_mode == "json_object":
        # Generated metadata is server-owned. Plugins/author assertions do not
        # acquire a public classification by claiming one in their result.
        return frozenset(names)
    return (frozenset(names) - frozenset(execution.receipt_projection)) | (
        frozenset(names) & frozenset(execution.sensitive_result_fields)
    )


def needs_private_result(execution: Any) -> bool:
    return execution.result_mode == "json_object" or bool(
        private_result_fields(execution, execution.result_model.model_fields)
    )


def _json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise PrivateValueError("private_value_invalid_json") from None


def _aad(row: Mapping[str, Any]) -> bytes:
    return _json(
        {
            key: row[key]
            for key in (
                "value_id",
                "owner_kind",
                "owner_id",
                "scope_kind",
                "scope_owner",
                "content_type",
                "format_version",
                "key_id",
                "byte_count",
                "expires_at",
            )
        }
    )


@dataclass
class PrivateValueBatch:
    service: PrivateValueService
    owner: PrivateOwner
    rows: list[dict[str, Any]] = field(default_factory=list)

    def protect(
        self, value: Any, *, content_type: str = "application/json", expires_at: float | None = None
    ) -> dict[str, Any]:
        # References supplied by a different run are not accepted on trust.
        # Reusing a stored reference is checked against its row at read time.
        if is_reference(value):
            return canonical_reference(value)
        key_id, key = self.service.require_ready()
        if content_type == "application/json":
            payload = _json(value)
        elif content_type == "application/octet-stream" and isinstance(value, bytes):
            payload = value
        else:
            raise PrivateValueError("private_content_type_invalid")
        row = {
            "value_id": uuid.uuid4().hex,
            "owner_kind": self.owner.kind,
            "owner_id": self.owner.identifier,
            "scope_kind": self.owner.scope_kind,
            "scope_owner": self.owner.scope_owner,
            "content_type": content_type,
            "format_version": FORMAT_VERSION,
            "key_id": key_id,
            "nonce": os.urandom(12),
            "byte_count": len(payload),
            "expires_at": expires_at,
        }
        aad = _aad(row)
        row["ciphertext"] = AESGCM(key).encrypt(row["nonce"], payload, aad)
        row["digest"] = hmac.digest(key, b"aq-private-digest\0" + aad + payload, "sha256").hex()
        self.rows.append(row)
        return {
            REFERENCE_KEY: row["value_id"],
            "byte_count": len(payload),
            "content_type": content_type,
        }

    async def persist(self, conn: Any) -> None:
        from src.database.tables import playbook_private_values

        if self.rows:
            await conn.execute(insert(playbook_private_values), self.rows)
            self.rows.clear()


class PrivateValueService:
    def __init__(
        self,
        engine: Any,
        *,
        active_key_id: str = "",
        keys: Mapping[str, bytes] | None = None,
        namespace_authorizer: Any = None,
    ):
        self.engine = engine
        self.active_key_id = active_key_id
        self._keys = dict(keys or {})
        self._namespace_authorizer = namespace_authorizer

    @classmethod
    def from_config(cls, engine: Any, config: Any) -> PrivateValueService:
        keys = {}
        for key_id, env_name in getattr(config, "private_value_key_env", {}).items():
            try:
                key = base64.b64decode(os.environ.get(env_name, ""), validate=True)
            except (ValueError, TypeError):
                continue
            if len(key) == 32:
                keys[key_id] = key
        return cls(
            engine, active_key_id=getattr(config, "private_value_active_key_id", ""), keys=keys
        )

    def require_ready(self) -> tuple[str, bytes]:
        key = self._keys.get(self.active_key_id)
        if not key or len(key) != 32:
            raise PrivateValueError("private_storage_key_unavailable")
        if self.engine is None:
            raise PrivateValueError("private_storage_unavailable")
        return self.active_key_id, key

    async def check_ready(self) -> None:
        """Check the configured key and storage schema before a bound effect."""
        from src.database.tables import playbook_private_values

        self.require_ready()
        try:
            async with self.engine.connect() as conn:
                await conn.execute(select(playbook_private_values.c.value_id).limit(0))
                if not await conn.scalar(
                    text("SELECT has_table_privilege('playbook_private_values', 'INSERT')")
                ):
                    raise PrivateValueError("private_storage_unavailable")
        except Exception:
            raise PrivateValueError("private_storage_unavailable") from None

    def batch(self, owner: PrivateOwner) -> PrivateValueBatch:
        return PrivateValueBatch(self, owner)

    async def authorize(self, owner: PrivateOwner, principal: Any) -> None:
        from src.commands.principal import PrincipalKind
        from src.playbooks.invocation import current_invocation

        if owner.kind == "snapshot":
            from src.observations.snapshots import authorize_private_snapshot

            await authorize_private_snapshot(self.engine, owner, principal)
            return
        kind = getattr(principal, "kind", None)
        if kind in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}:
            return
        if kind != PrincipalKind.PLAYBOOK:
            raise PrivateValueError("private_value_unauthorized")
        invocation = current_invocation()
        if owner.scope_kind == "project" and principal.project_id != owner.scope_owner:
            raise PrivateValueError("private_value_unauthorized")
        if owner.scope_kind == "system" and principal.project_id:
            raise PrivateValueError("private_value_unauthorized")
        if owner.scope_kind == "profile" and principal.profile_id != owner.scope_owner:
            raise PrivateValueError("private_value_unauthorized")
        if owner.kind == "run" and invocation is not None and invocation.run_id == owner.identifier:
            return
        if owner.kind == "namespace" and self._namespace_authorizer is not None:
            if await self._namespace_authorizer(owner, principal, invocation):
                return
        raise PrivateValueError("private_value_unauthorized")

    async def read(
        self, reference: Mapping[str, Any], *, owner: PrivateOwner, principal: Any, conn: Any = None
    ) -> Any:
        from src.database.tables import playbook_private_values as table

        await self.authorize(owner, principal)
        if not is_reference(reference):
            raise PrivateValueError("private_reference_invalid")
        reference = canonical_reference(reference)
        if self.engine is None:
            raise PrivateValueError("private_storage_unavailable")
        query = select(table).where(
            table.c.value_id == reference[REFERENCE_KEY],
            table.c.owner_kind == owner.kind,
            table.c.owner_id == owner.identifier,
            table.c.scope_kind == owner.scope_kind,
            table.c.scope_owner == owner.scope_owner,
        )
        if conn is None:
            async with self.engine.connect() as connection:
                row = (await connection.execute(query)).mappings().first()
        else:
            row = (await conn.execute(query)).mappings().first()
        if row is None:
            raise PrivateValueError("private_value_unavailable")
        if row["expires_at"] is not None and row["expires_at"] <= time.time():
            raise PrivateValueError("private_value_expired")
        key = self._keys.get(row["key_id"])
        if key is None or len(key) != 32:
            raise PrivateValueError("private_storage_key_unavailable")
        if row["format_version"] != FORMAT_VERSION:
            raise PrivateValueError("private_format_unsupported")
        try:
            aad = _aad(row)
            payload = AESGCM(key).decrypt(row["nonce"], row["ciphertext"], aad)
            digest = hmac.digest(key, b"aq-private-digest\0" + aad + payload, "sha256").hex()
            if len(payload) != row["byte_count"] or not hmac.compare_digest(digest, row["digest"]):
                raise InvalidTag
        except (InvalidTag, ValueError, TypeError):
            raise PrivateValueError("private_value_integrity_failed") from None
        return json.loads(payload) if row["content_type"] == "application/json" else payload

    async def resolve(
        self, value: Any, *, owner: PrivateOwner, principal: Any, conn: Any = None
    ) -> Any:
        if is_reference(value):
            return await self.read(value, owner=owner, principal=principal, conn=conn)
        if isinstance(value, Mapping):
            return {
                key: await self.resolve(item, owner=owner, principal=principal, conn=conn)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                await self.resolve(item, owner=owner, principal=principal, conn=conn)
                for item in value
            ]
        return value
