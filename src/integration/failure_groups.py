"""Pure red-run grouping (§7.1 of fast-smooth-integration).

Callers supply stored check runs (including output.annotations), optional job
logs bound by check_id, and frozen ``E..S --name-only`` evidence. No current
checkout, provider, task state or clock participates in a grouping decision.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.integration.models import RequiredCheckSet
from src.integration.source_ci import classify_source_checks

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BUILD = re.compile(
    r"fatal error:|(?:^|\s)error(?:\[[^]]+\]| [A-Z]+\d+)?\s*:|"
    r"undefined reference|cannot find -l|CMake Error|BUILD FAILED", re.I,
)
_TEST = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)|^E\s+(.+)|(?:Assertion|\w+Error):")
_PATH = re.compile(r'''(?:File ["']([^"']+)["']|([\w./\\-]+\.(?:py|rs|c|cc|cpp|h|ts|tsx|js|go))(?=[:(\s]|$))''')
_KINDS = {"build", "test", "other"}


@dataclass(frozen=True)
class FrozenMemberFiles:
    """Files read at freeze time, never inferred from mutable task branches."""

    task_id: str
    source_base_sha: str
    source_sha: str
    files: tuple[str, ...]


@dataclass(frozen=True, order=True)
class FailureRecord:
    job: str
    platform: str
    kind: str
    subsystem: str
    files: tuple[str, ...]
    signature: str
    message: str
    check_id: str
    location: str = ""


@dataclass(frozen=True)
class FailureGroup:
    key: str
    kind: str
    job: str
    platform: str
    subsystem: str
    implicated_members: tuple[str, ...]
    signatures: tuple[str, ...]
    records: tuple[FailureRecord, ...]
    attributed_member: str | None
    # All implicated files, including ones with zero owners, stay in the proof.
    file_owners: tuple[tuple[str, tuple[str, ...]], ...]
    frozen_members: tuple[FrozenMemberFiles, ...]

    @property
    def attribution(self) -> str:
        return "attributed" if self.attributed_member is not None else "shared"

    @property
    def count(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class FailureGrouping:
    state: str
    evidence: dict[str, Any]
    groups: tuple[FailureGroup, ...]


def normalize_signature(message: str) -> str:
    """First error line, without volatile addresses, temporary paths or numbers."""
    lines = [_ANSI.sub("", line).strip() for line in message.splitlines() if line.strip()]
    line = next((line for line in lines if _BUILD.search(line) or _TEST.search(line)),
                lines[0] if lines else "unknown failure")
    line = re.sub(r"\b\d{4}-\d\d-\d\dT[\d:.]+Z\s*", "", line)
    line = re.sub(r"(?:[A-Za-z]:)?[/\\](?:tmp|var[/\\]tmp|private[/\\]tmp|"
                  r"[^\s]*[/\\]Temp)[/\\][^\s\"']+", "<tmp>", line, flags=re.I)
    line = re.sub(r"0x[\da-f]+", "<address>", line, flags=re.I)
    return re.sub(r"\d+(?:\.\d+)?", "<n>", line).strip()


def _path(value: str, root: str) -> str | None:
    value = value.replace("\\", "/").split("::", 1)[0]
    value = re.sub(r":\d+(?::\d+)?$", "", value)
    root = root.replace("\\", "/").rstrip("/")
    if root and value.startswith(root + "/"):
        value = value[len(root) + 1:]
    value = posixpath.normpath(value)
    if value in {".", ".."} or value.startswith(("/", "../")) or re.match(r"^\w:", value):
        return None
    return value


def _files(item: Mapping, message: str, root: str) -> tuple[str, ...]:
    values = list(item.get("files") or ())
    values.extend(item[key] for key in ("path", "test_file", "module") if item.get(key))
    values.extend(frame.get("path", "") if isinstance(frame, Mapping) else frame
                  for frame in item.get("stack_frames", ()))
    values.extend(first or second for first, second in _PATH.findall(message))
    return tuple(sorted({path for value in values if isinstance(value, str)
                         and (path := _path(value, root))}))


def _record(item: Mapping, check: Mapping, root: str) -> FailureRecord:
    message = str(item.get("message") or item.get("text") or item.get("title") or "")
    files = _files(item, message, root)
    job = str(item.get("job") or check.get("job") or check["name"])
    kind = item.get("kind")
    if kind not in _KINDS:
        if re.search(r"\b(lint|ruff|mypy|format|packaging)\b", job, re.I):
            kind = "other"
        elif re.match(r"^(FAILED|ERROR)\s+tests[/\\]", message):
            kind = "test"
        elif _BUILD.search(message):
            kind = "build"
        elif _TEST.search(message) or any(path.startswith("tests/") for path in files):
            kind = "test"
        else:
            kind = "build" if re.search(r"\b(build|compile|cmake|link)\b", job, re.I) else "other"
    # A top test directory is the subsystem; flat tests use the owning module.
    test = next((path for path in files if path.startswith("tests/")), "")
    module = next((p for p in files if p.startswith("src/")), "")
    parts = (test if test.count("/") > 1 else module or test or
             (files[0] if files else "")).split("/")
    subsystem = str(item.get("subsystem") or (
        "/".join(parts[:2]) if len(parts) > 2 else posixpath.dirname("/".join(parts))
    ) or "unknown")
    return FailureRecord(job, str(item.get("platform") or check.get("platform") or ""),
                         str(kind), subsystem, files, normalize_signature(message), message,
                         str(check.get("id") or ""), str(item.get("test_id") or
                         item.get("start_line") or ""))


def normalize_failure_records(check: Mapping, logs: Iterable[Mapping] = (), *,
                              repository_root: str = "") -> tuple[FailureRecord, ...]:
    """Normalize stored annotations and structured or plain-text job logs.

    Structured entries accept kind, message, path/test_file/module/stack_frames,
    test_id, subsystem, job and platform. Raw logs recognize pytest failure
    summaries, traceback error lines, and compiler errors. Unknown failures keep
    a shared fallback record, so a failed check never silently disappears.
    """
    output = check.get("output") or {}
    items = list(check.get("annotations") or output.get("annotations") or ())
    items = [item for item in items if isinstance(item, Mapping)
             and item.get("annotation_level", "failure") == "failure"]
    for log in logs:
        if log.get("records") is not None:
            items.extend({**log, **item} for item in log["records"])
        else:
            text = str(log.get("text") or "")
            lines = [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s*", "",
                            _ANSI.sub("", line).strip()) for line in text.splitlines()]
            summaries = [line for line in lines if re.match(r"^(FAILED|ERROR)\s+tests[/\\]", line)]
            build_errors = [line for line in lines if _BUILD.search(line)]
            # Pytest's short summary supplies one record per failing test. A
            # multiline traceback/compiler diagnostic is one record with its
            # first error signature and repository frames retained as evidence.
            if build_errors and summaries:
                items.extend({**log, "message": line} for line in build_errors + summaries)
            elif summaries or any(_BUILD.search(line) or _TEST.search(line) for line in lines):
                items.extend({**log, "message": line} for line in summaries or [text])
    if not items:
        items = [{"message": output.get("summary") or output.get("text") or
                  f"{check['name']}: {check.get('conclusion')}"}]
    return tuple(sorted({_record(item, check, repository_root) for item in items}))


def group_failure_records(records: Iterable[FailureRecord],
                          members: Iterable[FrozenMemberFiles]) -> tuple[FailureGroup, ...]:
    """Group normalized records against only the frozen E..S file sets."""
    members = tuple(sorted((FrozenMemberFiles(
        member.task_id, member.source_base_sha, member.source_sha,
        tuple(sorted({path for file in member.files if (path := _path(file, ""))})),
    ) for member in members), key=lambda member: member.task_id))
    if len({member.task_id for member in members}) != len(members):
        raise ValueError("duplicate frozen member")
    owners: dict[str, set[str]] = {}
    for member in members:
        if not member.task_id or not member.source_base_sha or not member.source_sha:
            raise ValueError("frozen file evidence requires member, base and source identities")
        for file in member.files:
            if path := _path(file, ""):
                owners.setdefault(path, set()).add(member.task_id)
    records = tuple(sorted(set(records)))
    build_jobs = {(record.job, record.platform) for record in records if record.kind == "build"}
    buckets: dict[tuple, list[FailureRecord]] = {}
    for record in records:
        if record.kind == "test" and (record.job, record.platform) in build_jobs:
            continue
        implicated = tuple(sorted({owner for file in record.files for owner in owners.get(file, ())}))
        # Build signatures stay separate. Test/other symptoms with identical
        # job, subsystem and implicated members share one root-cause group.
        key = (record.kind == "build", record.job, record.platform,
               record.signature if record.kind == "build" else record.subsystem,
               () if record.kind == "build" else implicated)
        buckets.setdefault(key, []).append(record)
    groups = []
    for key, bucket in sorted(buckets.items()):
        files = tuple(sorted({file for record in bucket for file in record.files}))
        proof = tuple((file, tuple(sorted(owners.get(file, ())))) for file in files)
        implicated = tuple(sorted({owner for _, file_owners in proof for owner in file_owners}))
        attributed = implicated[0] if len(implicated) == 1 and proof and all(
            file_owners == implicated for _, file_owners in proof) else None
        signatures = tuple(sorted({record.signature for record in bucket}))
        kind = "build" if key[0] else "test" if any(r.kind == "test" for r in bucket) else "other"
        identity = (kind, key[1], key[2], key[3], implicated, signatures)
        group_key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        groups.append(FailureGroup(group_key, kind, key[1], key[2],
                                   "build" if key[0] else key[3], implicated, signatures,
                                   tuple(bucket), attributed, proof, members))
    return tuple(sorted(groups, key=lambda group: (group.kind != "build", -group.count, group.key)))


def group_run_failures(checks: Iterable[dict], *, head: str, required: RequiredCheckSet,
                       members: Iterable[FrozenMemberFiles], job_logs: Iterable[Mapping] = (),
                       repository_root: str = "") -> FailureGrouping:
    """Classify exact trusted checks first; only latest failed checks produce groups.

    A log must name check_id: old attempts, optional checks, successful jobs and
    foreign heads/producers cannot contribute failures to this run.
    """
    checks, job_logs = tuple(checks), tuple(job_logs)
    state, evidence = classify_source_checks(checks, head=head, required=required)
    evidence = {**evidence, "failing_checks": sorted(evidence["failing_checks"])}
    if state != "red":
        return FailureGrouping(state, evidence, ())
    failed = {str(check["id"]) for check in evidence["checks"]
              if check["conclusion"] in {"failure", "timed_out", "action_required"}
              and check["status"] == "completed"}
    records = []
    for check in checks:
        if (isinstance(check, dict) and str(check.get("id")) in failed
                and check.get("head_sha") == head and check.get("name") in required.names
                and str((check.get("app") or {}).get("id")) == required.producer_id):
            records.extend(normalize_failure_records(
                check, (log for log in job_logs if str(log.get("check_id")) == str(check.get("id"))),
                repository_root=repository_root))
    return FailureGrouping(state, evidence, group_failure_records(records, members))
