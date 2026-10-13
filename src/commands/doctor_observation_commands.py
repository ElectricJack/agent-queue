"""Doctor's strictly read-only command facade; no fix or authority impersonation."""
from __future__ import annotations

import math

from jsonschema import Draft202012Validator

from src.commands.doctor_observation_schemas import CATALOG_SCHEMA, RUN_SCHEMA
from src.commands.principal import PrincipalKind, current_principal


def failure(code: str) -> dict:
    # ``src.doctor`` loads every check (and the orchestrator) on import; the
    # handler module is imported by CLI command discovery, so defer it.
    from src.doctor.observations import failure as _failure

    return _failure(code)


class DoctorObservationCommandsMixin:
    async def _cmd_observation_snapshot_cleanup(self, args: dict) -> dict:
        """Collect at most 100 expired snapshots; daemon/operator maintenance only."""
        from src.database.queries.observation_snapshot_queries import cleanup_expired

        principal = current_principal()
        if principal is None or principal.kind not in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}:
            return {"success": False, "error_code": "operator_only", "error": "operator_only"}
        if args:
            return {"success": False, "error_code": "invalid_arguments", "error": "invalid_arguments"}
        removed = await cleanup_expired(self.db._engine)
        return {"success": True, "removed": removed}

    async def _cmd_doctor_catalog(self, args: dict) -> dict:
        """List doctor observation audits, identity schemas, scope and coverage contracts."""
        if not Draft202012Validator(CATALOG_SCHEMA).is_valid(args):
            return failure("doctor.invalid_arguments")
        if self.doctor_registry is None:
            return failure("doctor.subsystem_unavailable")
        from src.doctor.observations import catalog

        return catalog(self.doctor_registry)

    async def _cmd_doctor_run(self, args: dict) -> dict:
        """Observe one audited check or group; never repair or filter a global scan."""
        if (not Draft202012Validator(RUN_SCHEMA).is_valid(args)
                or any(not math.isfinite(args[key]) for key in ("now", "timeout_s")
                       if key in args)):
            return failure("doctor.invalid_arguments")
        principal = current_principal()
        if principal is not None and principal.enforced and (
            principal.project_id is not None
            or (principal.kind is PrincipalKind.SESSION and not principal.elevated)
        ):
            return failure("resource_scope_denied")
        if self.doctor_registry is None:
            return failure("doctor.subsystem_unavailable")
        if "pagination" in args:
            from src.observations.commands import snapshot_command

            return await snapshot_command(self, "doctor_run", args)
        from src.doctor.models import DoctorContext
        from src.doctor.observations import bounded_inline, collect_observations

        return bounded_inline(await collect_observations(
            self.doctor_registry,
            DoctorContext(config=self.config, db=self.db, handler=self,
                          project_id=args.get("project_id"), reference_time=args.get("now"),
                          read_only=True),
            check_id=args.get("check_id"), group=args.get("group"),
            timeout_s=args.get("timeout_s"),
        ))
