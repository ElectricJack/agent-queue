"""Command adapters for immutable observation pages; default paths stay live."""

from __future__ import annotations

import time

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from src.commands.principal import TRUSTED_LOCAL, current_principal
from src.database.queries.observation_snapshot_queries import project_rows, task_rows
from src.doctor.models import DoctorContext
from src.doctor.observations import collect_observations
from src.observations.snapshots import (
    MAX_IDENTITIES, PAGING_KEYS, SnapshotError, SnapshotService, authority, failure, validate_paging,
)
from src.playbooks.invocation import current_invocation
from src.playbooks.private_values import PrivateValueError, PrivateValueService

# Bounded generic fields a manifest row keeps, so an authored filter/key map
# applies the same status/age predicate to manifests as to payload rows.
MANIFEST_KEYS = ("id", "task_id", "project_id", "status", "kind", "seconds_in_state")


async def snapshot_command(handler, producer: str, args: dict) -> dict:
    try:
        return await _snapshot_command(handler, producer, args)
    except (SnapshotError, PrivateValueError) as exc:
        return failure(exc.code)
    except (ValidationError, ValueError):
        return failure("snapshot.invalid_arguments")
    except SQLAlchemyError:
        return failure("snapshot.storage_unavailable")


async def _snapshot_command(handler, producer, args):
    from src.commands.contracts.builtin import GetStuckTasksArgs, ListProjectsArgs, ListTasksArgs

    models = {"list_projects": ListProjectsArgs, "list_tasks": ListTasksArgs,
              "get_stuck_tasks": GetStuckTasksArgs}
    validate_paging({key: value for key, value in args.items() if value is not None})
    if producer in models:
        args = models[producer].model_validate(args).model_dump(exclude_none=True)
    validate_paging(args)
    if producer == "list_tasks" and args.get("display_mode", "flat") != "flat":
        raise SnapshotError("snapshot.unsupported_display_mode")
    if producer == "list_tasks" and args.get("status"):
        from src.models import TaskStatus

        TaskStatus(args["status"])
    principal = current_principal() or TRUSTED_LOCAL
    project = args.get("project_id") or principal.project_id
    if principal.project_id and project != principal.project_id:
        raise SnapshotError("snapshot.unauthorized")
    scope = ("project", project) if project else ("system", "system")
    authority(principal, producer, scope)
    args = {**args, **({"project_id": project} if project and producer != "list_projects" else {})}
    request = {key: value for key, value in args.items() if key not in PAGING_KEYS}
    # Page size is part of the original request; changing it cannot repartition.
    request["page_size"] = args.get("page_size", 500)
    engine = getattr(handler.db, "_engine", None)
    if engine is None:
        raise SnapshotError("snapshot.unavailable")
    private = getattr(handler.db, "_playbook_private_values", None)
    if private is None:
        private = PrivateValueService.from_config(engine, handler.config.playbooks)
    service = SnapshotService(engine, private)
    if args.get("snapshot_id"):
        return await service.read(producer=producer, request=request, principal=principal,
                                  scope=scope, snapshot_id=args["snapshot_id"],
                                  page_token=args["page_token"])
    # Fail before potentially expensive live probes when private storage is absent.
    await private.check_ready()
    started = time.time()
    # A playbook run's frozen observation time is server-derived and not part
    # of the authored request, so a continuation repeats the same request.
    invocation = current_invocation()
    frozen = invocation.observed_at if invocation else None
    observed = args.get("now", started if frozen is None else frozen)
    if frozen is not None and args.get("now") is None:
        args = {**args, "now": frozen}
    if producer == "doctor_run":
        raw = await collect_observations(
            handler.doctor_registry,
            DoctorContext(config=handler.config, db=handler.db, handler=handler,
                          project_id=project, reference_time=args.get("now"), read_only=True),
            check_id=args.get("check_id"), group=args.get("group"), timeout_s=args.get("timeout_s"),
        )
        # Selection errors aren't empty successful snapshots.
        if raw.get("error_code") and not raw.get("checks"):
            return raw
        collections = {"checks": raw["checks"], "findings": raw["findings"]}
        identities, coverage, complete = raw["identity_manifest"], raw["coverage"], raw["complete"]
        metadata = {key: raw[key] for key in ("summary", "exit_code", "scan_id", "reasons")}
        observed = raw["observed_at"]
    else:
        if producer == "list_projects":
            rows = await project_rows(engine, project)
            kind = "projects"
            metadata = {}
        elif producer == "list_tasks":
            rows = await task_rows(handler, args)
            kind = "tasks"
            metadata = {"display_mode": "flat", "total": len(rows)}
        else:
            raw = await handler._get_stuck_tasks_observation(args, max_tasks=MAX_IDENTITIES)
            if raw.get("success") is not True:
                return {**raw, "complete": False, "execution_status": "incomplete"}
            rows, kind = raw["stuck"], "stuck"
            metadata = {"now_used": raw["now_used"], "thresholds": raw["thresholds"],
                        "summary": f"{len(rows)} stuck task(s)"}
            observed = raw["now_used"]
        collections = {kind: rows}
        identities = [{key: row[key] for key in MANIFEST_KEYS if key in row} for row in rows]
        coverage = [{"source": producer, "project_id": project, "complete": True,
                     "observed": len(rows), "total": len(rows), "reasons": []}]
        complete = True
    return await service.materialize(
        producer=producer, request=request, principal=principal, scope=scope,
        collections=collections, identities=identities, coverage=coverage, complete=complete,
        observed_at=observed, metadata=metadata, page_size=args.get("page_size", 500),
        interval_started_at=started,
    )
