"""Read-only doctor observations, explicit identities and absence authority.

Producers own adapters and coverage. This module never searches arbitrary legacy
JSON for task IDs, infers health from prose, or repairs a diagnostic finding.
Snapshot persistence/paging consumes ``collect_observations`` without rerunning
probes; the inline transport refuses oversize responses instead of truncating.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import replace

from src.doctor.models import DoctorContext, Severity
from src.doctor.runner import DoctorRegistry, _run_one, exit_code_for

GROUP = "task_observations_v1"
GROUPS = {GROUP: ("claims.holder_consistency", "stall.sweep")}
MAX_ROWS = 10_000
MAX_PRODUCER_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 262_144


def partition(check_id, kind, project_id, *, complete, observed=0, total=None, reasons=()):
    return {"check_id": check_id, "kind": kind, "kind_version": 1,
            "project_id": project_id, "complete": complete,
            "observed": observed, "total": total, "reasons": list(reasons)}


def finding(check_id, kind, project_id, subject_type, subject_id, detail, data,
            severity="warn"):
    """Producer adapter input. Identity validation and keying happen once below."""
    return {"check_id": check_id, "kind": kind, "kind_version": 1,
            "project_id": project_id, "subject": {"type": subject_type, "id": subject_id},
            "severity": severity, "detail": detail, "data": data}


def normalize(item: dict, check) -> dict:
    subject = item["subject"]
    project = item["project_id"]
    if (item["check_id"] != check.id or item["kind"] not in check.finding_schema["kinds"]
            or item["kind_version"] != 1
            or not isinstance(subject["type"], str) or not subject["type"]
            or not isinstance(subject["id"], str) or not subject["id"]
            or (project is not None and (not isinstance(project, str) or not project))
            or (subject["type"] == "task" and project is None)
            or item["severity"] not in {s.value for s in Severity}
            or not isinstance(item["detail"], str) or not isinstance(item["data"], dict)):
        raise ValueError("invalid finding identity or payload")
    identity = [check.identity_version, check.id, item["kind"], item["kind_version"],
                project, subject["type"], subject["id"]]
    digest = hashlib.sha256(json.dumps(identity, separators=(",", ":"),
                                      ensure_ascii=True).encode()).hexdigest()
    return {"key": f"doctor:v{check.identity_version}:{digest}", **item}


def catalog(registry: DoctorRegistry) -> dict:
    checks = []
    for check in registry.checks():
        supported = (check.read_only_audited and bool(check.supported_scopes)
                     and check.identity_adapter is not None and bool(check.completeness_contract))
        checks.append({"id": check.id, "owner": check.owner,
                       "supported_scopes": list(check.supported_scopes),
                       "read_only_audited": check.read_only_audited,
                       "finding_schema": check.finding_schema,
                       "identity_version": check.identity_version,
                       "payload_version": check.payload_version,
                       "completeness_contract": check.completeness_contract,
                       "timeout_s": check.timeout_s, "groups": list(check.groups),
                       "supported": bool(supported),
                       "diagnostics": [] if supported else [
                           "unsupported_read_only_scope_or_completeness"]})
    return {"success": True, "checks": checks,
            "groups": {name: list(ids) for name, ids in GROUPS.items()}}


def failure(code: str, *, observed_at=None, scan_id=None, detail=None) -> dict:
    return {"success": False, "error_code": code, "error": detail or code,
            "checks": [], "findings": [], "summary": {s.value: 0 for s in Severity}
            | {"fixes_applied": 0}, "exit_code": 0, "observed_at": observed_at,
            "scan_id": scan_id, "coverage": [], "complete": False,
            "snapshot_id": None, "page_tokens": [], "identity_manifest": [],
            "execution_status": "unavailable", "reasons": [code]}


def select_checks(registry, *, check_id=None, group=None):
    if (check_id is None) == (group is None):
        return [], "doctor.invalid_selection"
    if group is not None:
        if group not in GROUPS:
            return [], "doctor.unknown_group"
        ids = GROUPS[group]
        if not ids:
            return [], "doctor.empty_group"
    else:
        ids = (check_id,)
    checks = [registry.get(cid) for cid in ids]
    if any(check is None for check in checks):
        return [], "doctor.unknown_check" if check_id else "doctor.subsystem_unavailable"
    return checks, None


async def collect_observations(registry: DoctorRegistry, ctx: DoctorContext, *,
                               check_id=None, group=None, timeout_s=None) -> dict:
    """Full bounded producer result for inline delivery or C2 materialization.

    The command authorizes scope before entering here. Each probe inherits the
    dispatch principal through contextvars, including its nested command calls.
    """
    observed_at = ctx.reference_time if ctx.reference_time is not None else time.time()
    scan_id = str(uuid.uuid4())
    checks, error = select_checks(registry, check_id=check_id, group=group)
    if error:
        return failure(error, observed_at=observed_at, scan_id=scan_id)
    for check in checks:
        if (not check.read_only_audited or check.identity_adapter is None
                or not check.completeness_contract):
            return failure("doctor.unsupported_observation", observed_at=observed_at,
                           scan_id=scan_id, detail=f"{check.id}: read-only audit incomplete")
        scope = "project" if ctx.project_id is not None else "system"
        if scope not in check.supported_scopes:
            return failure("doctor.unsupported_scope", observed_at=observed_at, scan_id=scan_id)
    ctx = replace(ctx, reference_time=observed_at, read_only=True)
    results = await asyncio.gather(*(
        _run_one(replace(check, timeout_s=min(check.timeout_s, timeout_s)
                         if timeout_s is not None else check.timeout_s),
                 replace(ctx, observation_reasons=[])) for check in checks
    ))
    findings, coverage = [], []
    for check, result in zip(checks, results):
        parts = [dict(p) for p in result.coverage]
        if not parts:
            parts = [partition(check.id, kind, ctx.project_id, complete=False,
                               reasons=result.reasons or ["coverage_unknown"])
                     for kind in check.finding_schema["kinds"]]
        absent_kinds = set(check.finding_schema["kinds"]) - {p["kind"] for p in parts}
        if absent_kinds:
            parts.extend(partition(check.id, kind, ctx.project_id, complete=False,
                                   reasons=["coverage_unknown"]) for kind in sorted(absent_kinds))
        if absent_kinds or not all(p["complete"] for p in parts):
            result.complete = False
            if result.execution_status == "succeeded":
                result.execution_status = "partial"
            result.reasons = sorted(set(result.reasons) | {
                reason for part in parts for reason in part["reasons"]})
        invalid_kinds = set()
        try:
            adapted = check.identity_adapter(result)
            if not isinstance(adapted, list):
                raise ValueError("invalid adapter result")
            for item in adapted:
                try:
                    findings.append(normalize(item, check))
                except (ValueError, KeyError, TypeError):
                    kind = item.get("kind") if isinstance(item, dict) else None
                    invalid_kinds.add(kind if isinstance(kind, str)
                                      and kind in check.finding_schema["kinds"] else None)
        except Exception:
            invalid_kinds.add(None)
        if invalid_kinds:
            result.complete = False
            result.execution_status = "malformed"
            result.reasons.append("finding_identity_missing_or_invalid")
        for index, part in enumerate(parts):
            if (None in invalid_kinds or part.get("kind") in invalid_kinds
                    or result.execution_status in {"timeout", "exception", "unavailable",
                                                   "unsupported"}):
                part = dict(part, complete=False,
                            reasons=list(part.get("reasons", [])) + result.reasons)
                parts[index] = part
            coverage.append(part)
        result.coverage = parts
    complete = all(r.complete for r in results) and all(p["complete"] for p in coverage)
    success = all(r.execution_status == "succeeded" for r in results)
    return {"success": success, "checks": [r.to_dict() for r in results],
            "findings": findings,
            "summary": {s.value: sum(r.severity == s for r in results) for s in Severity}
            | {"fixes_applied": 0}, "exit_code": exit_code_for(results),
            "observed_at": observed_at, "scan_id": scan_id, "coverage": coverage,
            "complete": complete, "execution_status": "succeeded" if success else "partial",
            "reasons": sorted({reason for r in results for reason in r.reasons}),
            "snapshot_id": None, "page_tokens": [],
            "identity_manifest": [{key: f[key] for key in (
                "key", "check_id", "kind", "kind_version", "project_id", "subject")}
                for f in findings]}


def bounded_inline(response: dict) -> dict:
    if len(json.dumps(response, allow_nan=False).encode()) <= MAX_RESPONSE_BYTES:
        return response
    return failure("doctor.response_too_large", observed_at=response["observed_at"],
                   scan_id=response["scan_id"],
                   detail="Full observation exceeds inline limit; snapshot delivery required")
