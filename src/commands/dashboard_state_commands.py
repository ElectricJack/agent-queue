"""Authenticated command surface for durable dashboard state."""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import ValidationError

from src.commands.principal import (
    TRUSTED_LOCAL,
    ExecutionPrincipal,
    PrincipalKind,
    current_principal,
)
from src.dashboard_state.namespaces import NAMESPACES, NamespaceSpec

DASHBOARD_STATE_EVENT = "dashboard_state.changed.v1"
MAX_VALUE_BYTES = 64 * 1024


def resolve_human_id(principal: ExecutionPrincipal) -> str | None:
    """Resolve the server-owned roaming-preference identity for a caller."""
    if principal.kind is PrincipalKind.LOCAL:
        return "human:local-operator"
    return None


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": message, **extra}


class DashboardStateCommandsMixin:
    """Read, replace, and reset typed dashboard state documents."""

    def _dashboard_human_id(self) -> tuple[str | None, dict[str, Any] | None]:
        principal = current_principal() or TRUSTED_LOCAL
        human_id = resolve_human_id(principal)
        if human_id is None:
            return None, _error(
                "human_required",
                "dashboard state is available only to an authenticated human principal",
            )
        return human_id, None

    async def _dashboard_address(
        self, args: dict[str, Any], human_id: str
    ) -> tuple[NamespaceSpec | None, str, str, dict[str, Any] | None]:
        namespace = args.get("namespace")
        spec = NAMESPACES.get(namespace) if isinstance(namespace, str) else None
        if spec is None:
            return (
                None,
                "",
                "",
                _error("unknown_namespace", f"unknown dashboard state namespace: {namespace!r}"),
            )

        subject = args.get("subject")
        if spec.subject == "none":
            if subject is not None:
                return (
                    None,
                    "",
                    "",
                    _error("subject_not_allowed", f"{spec.name} does not accept a subject"),
                )
            stored_subject = ""
        else:
            if not isinstance(subject, str) or not subject:
                return (
                    None,
                    "",
                    "",
                    _error("subject_required", f"{spec.name} requires a project subject"),
                )
            if await self.db.get_project(subject) is None:
                return (
                    None,
                    "",
                    "",
                    _error(
                        "unknown_subject", f"unknown dashboard state project subject: {subject}"
                    ),
                )
            stored_subject = subject

        owner_id = "" if spec.scope == "workspace" else human_id
        return spec, owner_id, stored_subject, None

    @staticmethod
    def _dashboard_document(spec: NamespaceSpec, row: dict | None, owner_id: str) -> dict:
        value = spec.default_value() if row is None or row.get("value") is None else row["value"]
        return {
            "scope": spec.scope,
            "owner_id": owner_id,
            "namespace": spec.name,
            "subject": None if row is None or row.get("subject") == "" else row["subject"],
            "revision": 0 if row is None else int(row["revision"]),
            "exists": bool(row is not None and row.get("value") is not None),
            "value": value,
            "updated_at": None if row is None else float(row["updated_at"]),
        }

    async def _dashboard_get_document(
        self, spec: NamespaceSpec, owner_id: str, stored_subject: str
    ) -> dict:
        row = await self.db.get_dashboard_document(
            scope=spec.scope,
            owner_id=owner_id,
            namespace=spec.name,
            subject=stored_subject,
        )
        return self._dashboard_document(spec, row, owner_id)

    async def _cmd_dashboard_state_list(self, args: dict[str, Any]) -> dict[str, Any]:
        human_id, error = self._dashboard_human_id()
        if error:
            return error
        assert human_id is not None

        rows = await self.db.list_dashboard_documents(owner_id=human_id)
        by_namespace: dict[str, list[dict]] = {}
        for row in rows:
            by_namespace.setdefault(row["namespace"], []).append(row)

        documents: list[dict] = []
        for spec in NAMESPACES.values():
            owner_id = "" if spec.scope == "workspace" else human_id
            visible = by_namespace.get(spec.name, [])
            if spec.subject == "none":
                row = next((item for item in visible if item["subject"] == ""), None)
                documents.append(self._dashboard_document(spec, row, owner_id))
                continue
            for row in visible:
                subject = row.get("subject")
                if isinstance(subject, str) and subject and await self.db.get_project(subject):
                    documents.append(self._dashboard_document(spec, row, owner_id))

        return {"success": True, "owner_id": human_id, "documents": documents}

    async def _cmd_dashboard_state_get(self, args: dict[str, Any]) -> dict[str, Any]:
        human_id, error = self._dashboard_human_id()
        if error:
            return error
        assert human_id is not None
        spec, owner_id, subject, error = await self._dashboard_address(args, human_id)
        if error:
            return error
        assert spec is not None
        document = await self._dashboard_get_document(spec, owner_id, subject)
        return {"success": True, "document": document}

    @staticmethod
    def _validate_dashboard_value(
        spec: NamespaceSpec, raw_value: Any
    ) -> tuple[dict | None, dict[str, Any] | None]:
        try:
            value = spec.model.model_validate(raw_value).model_dump(mode="json")
        except ValidationError as exc:
            errors = []
            for item in exc.errors(include_url=False):
                path = ".".join(str(part) for part in item["loc"]) or "value"
                errors.append({"path": path, "message": item["msg"]})
            return None, _error("invalid_value", "dashboard state value is invalid", errors=errors)
        try:
            actual = len(
                json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            return None, _error(
                "invalid_value",
                "dashboard state value is not JSON serializable",
                errors=[{"path": "value", "message": str(exc)}],
            )
        if actual > MAX_VALUE_BYTES:
            return None, _error(
                "value_too_large",
                f"dashboard state value exceeds {MAX_VALUE_BYTES} bytes",
                limit_bytes=MAX_VALUE_BYTES,
                actual_bytes=actual,
            )
        return value, None

    async def _dashboard_mutate(
        self,
        *,
        spec: NamespaceSpec,
        owner_id: str,
        subject: str,
        value: dict | None,
        base_revision: int | None,
        change: str,
    ) -> dict[str, Any]:
        now = time.time()
        row, conflict = await self.db.write_dashboard_document(
            scope=spec.scope,
            owner_id=owner_id,
            namespace=spec.name,
            subject=subject,
            value=value,
            base_revision=base_revision,
            now=now,
        )
        if row is None:
            current = self._dashboard_document(spec, conflict, owner_id)
            return _error(
                "revision_conflict",
                "dashboard state changed since it was loaded",
                current=current,
            )

        document = self._dashboard_document(spec, row, owner_id)
        payload = {
            "version": 1,
            "scope": spec.scope,
            "owner_id": owner_id,
            "namespace": spec.name,
            "subject": document["subject"],
            "revision": document["revision"],
            "change": change,
            "updated_at": document["updated_at"],
        }
        seq = await self.db.log_event(
            DASHBOARD_STATE_EVENT,
            project_id=document["subject"] if spec.subject == "project" else None,
            payload=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        )
        payload["seq"] = seq
        await self.orchestrator.bus.emit(DASHBOARD_STATE_EVENT, payload)
        return {"success": True, "document": document}

    async def _cmd_dashboard_state_put(self, args: dict[str, Any]) -> dict[str, Any]:
        human_id, error = self._dashboard_human_id()
        if error:
            return error
        assert human_id is not None
        spec, owner_id, subject, error = await self._dashboard_address(args, human_id)
        if error:
            return error
        assert spec is not None

        base_revision = args.get("base_revision")
        if spec.write_mode == "cas" and base_revision is None:
            return _error("base_revision_required", f"{spec.name} requires base_revision")
        value, error = self._validate_dashboard_value(spec, args.get("value"))
        if error:
            return error
        assert value is not None
        return await self._dashboard_mutate(
            spec=spec,
            owner_id=owner_id,
            subject=subject,
            value=value,
            base_revision=base_revision,
            change="write",
        )

    async def _cmd_dashboard_state_reset(self, args: dict[str, Any]) -> dict[str, Any]:
        human_id, error = self._dashboard_human_id()
        if error:
            return error
        assert human_id is not None
        spec, owner_id, subject, error = await self._dashboard_address(args, human_id)
        if error:
            return error
        assert spec is not None
        return await self._dashboard_mutate(
            spec=spec,
            owner_id=owner_id,
            subject=subject,
            value=None,
            base_revision=None,
            change="reset",
        )
