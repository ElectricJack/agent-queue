"""State command boundary: strict input, original authority and pinned namespaces."""
from __future__ import annotations

from jsonschema import Draft202012Validator
from sqlalchemy.exc import SQLAlchemyError

from src.commands.principal import PrincipalKind, current_principal
from src.observations.snapshots import SnapshotError, validate_paging
from src.playbooks.private_values import PrivateValueError, PrivateValueService
from src.playbooks.state import Namespace, StateError, StateService, encoded, resolve_namespace

_TEXT = {"type": "string", "minLength": 1, "maxLength": 512}
_REVISION = {"type": "integer", "minimum": 0, "maximum": 9_223_372_036_854_775_806}
_NAMESPACE = {
    "type": "object", "additionalProperties": False,
    "required": ["scope_kind", "scope_owner", "playbook_id", "state_schema_version"],
    "properties": {"scope_kind": {"enum": ["system", "project", "profile"]},
                   "scope_owner": {**_TEXT, "maxLength": 256},
                   "playbook_id": {**_TEXT, "maxLength": 256},
                   "state_schema_version": {"type": "integer", "minimum": 1,
                                            "maximum": 2_147_483_647}},
}
_READ = {"record_space": {"enum": ["kv", "notification"]}}
_PAGES = {
    **_READ, "prefix": {"type": "string", "maxLength": 512},
    "pagination": {"enum": ["snapshot"]},
    "snapshot_id": {**_TEXT, "maxLength": 128},
    "page_token": {**_TEXT, "maxLength": 128},
    "page_size": {"type": "integer", "minimum": 1, "maximum": 500},
}


def _schema(properties, required=()):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(required)}


STATE_SCHEMAS = {
    "playbook_state_notify": _schema({
        "key": _TEXT, "action": {"enum": ["present", "cleared"]},
        "observed_at": {"type": "number"}, "observation_id": _TEXT,
        "expected_revision": _REVISION, "last_value": {},
        "source_partition": {"type": ["object", "null"]},
        "repeat_after_seconds": {"type": ["integer", "null"], "minimum": 1,
                                 "maximum": 2_147_483_647},
        "message": _schema({
            "to_kind": {"enum": ["session", "task", "profile", "user"]},
            "to_id": _TEXT,
            "body": {"type": "string", "minLength": 1, "maxLength": 16384, "pattern": r"\S"},
            "subject": {"type": ["string", "null"], "maxLength": 512},
        }, ["to_kind", "to_id", "body"]),
    }, ["key", "action", "observed_at", "observation_id", "message"]),
    "playbook_state_get": _schema({"key": _TEXT, **_READ}, ["key"]),
    "playbook_state_list": _schema(_PAGES),
    "playbook_state_set": _schema({
        "key": _TEXT, "value": {}, "expected_revision": _REVISION, "operation_key": _TEXT,
        "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647},
    }, ["key", "value", "expected_revision", "operation_key"]),
    "playbook_state_delete": _schema({
        "key": _TEXT, "expected_revision": _REVISION, "operation_key": _TEXT,
    }, ["key", "expected_revision", "operation_key"]),
    "playbook_state_inspect": _schema({"namespace": _NAMESPACE, "key": _TEXT, **_PAGES},
                                      ["namespace"]),
    "playbook_state_reset": _schema({
        "namespace": _NAMESPACE, "expected_revision": _REVISION, "operation_key": _TEXT,
        "reason": {**_TEXT, "pattern": r"\S", "maxLength": 4096},
    }, ["namespace", "expected_revision", "operation_key", "reason"]),
}


class PlaybookStateCommandsMixin:
    async def _state_command(self, command, args):
        try:
            # JSON schema cannot reject Python NaN; strict encoding does, before effects.
            encoded(args)
            if not Draft202012Validator(STATE_SCHEMAS[command]).is_valid(args):
                raise StateError("state.invalid_arguments")
            principal = current_principal()
            operator = command in {"playbook_state_inspect", "playbook_state_reset"}
            if operator:
                if principal is None or principal.kind not in {PrincipalKind.LOCAL,
                                                               PrincipalKind.SERVICE}:
                    raise StateError("operator_only")
                namespace = Namespace(**args["namespace"])
            else:
                namespace = await resolve_namespace(self, command)
            if command == "playbook_state_notify":
                from src.playbooks.invocation import current_invocation

                invocation = current_invocation()
                if (not principal.policy.allows_aq_command("message_send")
                        or "message_send" not in invocation.aq_commands):
                    raise StateError("unauthorized")
                if args["action"] == "cleared" and "expected_revision" not in args:
                    raise StateError("state.invalid_arguments")
                disabled = self._messages_disabled_error()
                if disabled:
                    return disabled
            engine = getattr(self.db, "_engine", None)
            if engine is None:
                raise StateError("state.storage_unavailable")
            private = getattr(self.db, "_playbook_private_values", None)
            if private is None:
                private = PrivateValueService.from_config(engine, self.config.playbooks)
            service = StateService(engine, private)
            inputs = {key: value for key, value in args.items() if key != "namespace"}
            if command == "playbook_state_notify":
                from src.playbooks.state_notify import NotificationService

                return await NotificationService(engine, private).notify(
                    self, namespace, principal, **inputs)
            if command == "playbook_state_get" or (command == "playbook_state_inspect"
                                                   and "key" in inputs):
                if set(inputs) - {"key", "record_space"}:
                    raise StateError("state.invalid_arguments")
                return await service.get(namespace, principal, **inputs)
            if command in {"playbook_state_list", "playbook_state_inspect"}:
                inputs.setdefault("pagination", "snapshot")
                validate_paging(inputs)
                return await service.list(namespace, principal, producer=command, **inputs)
            if command == "playbook_state_reset":
                return await service.reset(namespace, principal, **inputs)
            return await service.mutate(namespace, principal, action=command.rsplit("_", 1)[1],
                                        **inputs)
        except (StateError, PrivateValueError, SnapshotError) as exc:
            return {"success": False, "error": exc.code, "error_code": exc.code}
        except SQLAlchemyError:
            return {"success": False, "error": "state.storage_unavailable",
                    "error_code": "state.storage_unavailable"}

    async def _cmd_playbook_state_get(self, args: dict) -> dict:
        """Read an invocation-owned typed key, including its retained revision."""
        return await self._state_command("playbook_state_get", args)

    async def _cmd_playbook_state_list(self, args: dict) -> dict:
        """Materialize immutable pre-scan state revisions and protected pages."""
        return await self._state_command("playbook_state_list", args)

    async def _cmd_playbook_state_set(self, args: dict) -> dict:
        """Set a private KV value using CAS and a retained operation key."""
        return await self._state_command("playbook_state_set", args)

    async def _cmd_playbook_state_delete(self, args: dict) -> dict:
        """Create a versioned KV tombstone with replay-safe CAS."""
        return await self._state_command("playbook_state_delete", args)

    async def _cmd_playbook_state_inspect(self, args: dict) -> dict:
        """Inspect an explicit namespace only under independent operator authority."""
        return await self._state_command("playbook_state_inspect", args)

    async def _cmd_playbook_state_reset(self, args: dict) -> dict:
        """Operator reset: retain revisions/journals and warn of deliberate re-alert."""
        return await self._state_command("playbook_state_reset", args)

    async def _cmd_playbook_state_notify(self, args: dict) -> dict:
        """Atomically enqueue episode/reminder/clear messages with observation replay."""
        return await self._state_command("playbook_state_notify", args)
