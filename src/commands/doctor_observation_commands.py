"""Doctor's strictly read-only command facade; no fix or authority impersonation."""
from __future__ import annotations

import math

from jsonschema import Draft202012Validator

from src.commands.principal import PrincipalKind, current_principal
from src.doctor.models import DoctorContext
from src.doctor.observations import bounded_inline, catalog, collect_observations, failure

CATALOG_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
RUN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "check_id": {"type": "string", "minLength": 1},
        "group": {"type": "string", "minLength": 1},
        "project_id": {"type": ["string", "null"], "minLength": 1},
        "now": {"type": "number", "minimum": 0, "maximum": 253402300799},
        "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
        "pagination": {"type": "string", "enum": ["snapshot"]},
        "snapshot_id": {"type": "string", "minLength": 1},
        "page_token": {"type": "string", "minLength": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 500},
    },
    "oneOf": [{"required": ["check_id"]}, {"required": ["group"]}],
    "dependentRequired": {"page_token": ["snapshot_id", "pagination"],
                          "snapshot_id": ["page_token", "pagination"],
                          "page_size": ["pagination"]},
}


class DoctorObservationCommandsMixin:
    async def _cmd_doctor_catalog(self, args: dict) -> dict:
        """List doctor observation audits, identity schemas, scope and coverage contracts."""
        if not Draft202012Validator(CATALOG_SCHEMA).is_valid(args):
            return failure("doctor.invalid_arguments")
        if self.doctor_registry is None:
            return failure("doctor.subsystem_unavailable")
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
            # C2 attaches durable materialization here. Never rerun a continuation
            # as a live query or pretend inline data is an immutable snapshot.
            return failure("doctor.snapshot_unavailable")
        return bounded_inline(await collect_observations(
            self.doctor_registry,
            DoctorContext(config=self.config, db=self.db, handler=self,
                          project_id=args.get("project_id"), reference_time=args.get("now"),
                          read_only=True),
            check_id=args.get("check_id"), group=args.get("group"),
            timeout_s=args.get("timeout_s"),
        ))
