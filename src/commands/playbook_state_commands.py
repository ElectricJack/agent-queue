"""State command boundary: strict input, original authority and pinned namespaces."""
from __future__ import annotations

from jsonschema import Draft202012Validator
from sqlalchemy.exc import SQLAlchemyError

from src.commands.playbook_state_schemas import STATE_SCHEMAS
from src.commands.principal import PrincipalKind, current_principal
from src.observations.snapshots import SnapshotError, validate_paging
from src.playbooks.private_values import PrivateValueError, PrivateValueService
from src.playbooks.state import Namespace, StateError, StateService, encoded, resolve_namespace


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
