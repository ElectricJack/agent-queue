#!/usr/bin/env python3
"""Delivery metrics from explicit, allowlisted evidence, with no live state writes.

    python scripts/bazel/report_metrics.py report --input observations.json --output report.json
    python scripts/bazel/report_metrics.py history --runs page1.json --runs page2.json \
        --baseline full-suite-baseline-2026-09-22.md --output history.json

Report input (schema_version=1): attempts, suites, deliveries, worker_waits,
events, incidents and prs lists. Each attempt and suite carries every JOIN_FIELDS
field; attempts declare required_suites, suites add suite/status/conclusion and
queued_at/started_at/completed_at timestamps. Times are ISO8601 with a timezone.
deliveries declare task_id/head_sha/base_sha, first_commit_at/landed_at and
rebase_events/rerun_events (null means unobserved). worker_waits declare worker_id,
start/end and reason=checks; only these explicit intervals count as waiting.
events carry id/kind; incidents carry id/root_cause; prs carry id, green,
mergeable and waiting_since. Every report requires an explicit as_of timestamp.

Incomplete identities and duplicate observations remain in the report and cannot
certify green. Report windows select attempt first_push_at and delivery landed_at
in [window_start, window_end); unassignable observations remain visible. This is
also the daily/14-day window interface; historical snapshots are not live capture.
No credential, environment or diff payloads are copied from source records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

JOIN_FIELDS = ("task_id", "head_sha", "base_sha", "candidate_revision", "commit_sha", "workflow_id",
               "run_id", "run_attempt", "suite_version")
FAILURE_CLASSES = {"product_regression", "test_flake", "infrastructure", "timeout", "unknown"}
TIMES = ("created_at", "run_started_at", "updated_at", "queued_at", "started_at",
         "completed_at", "first_push_at", "first_commit_at", "landed_at")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")


def timestamp(value) -> float | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timezone required: {value}")
    return dt.timestamp()


def duration(start, end) -> float | None:
    left, right = timestamp(start), timestamp(end)
    if left is None or right is None or right < left:
        return None
    return right - left


def number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected nonnegative finite number or null")
    if not math.isfinite(value) or value < 0:
        raise ValueError("expected nonnegative finite number or null")
    return value


def distribution(values: list, total: int | None = None) -> dict:
    """Nearest-rank percentiles: x[ceil(p*n)-1], including failed samples as missing."""
    observed = sorted(number(v) for v in values if v is not None)
    denominator = len(values) if total is None else total
    if denominator < len(observed):
        raise ValueError("denominator smaller than observed population")
    return {"population": denominator, "observed": len(observed),
            "missing": denominator - len(observed),
            "p50": observed[math.ceil(0.5 * len(observed)) - 1] if observed else None,
            "p90": observed[math.ceil(0.9 * len(observed)) - 1] if observed else None,
            "sum": sum(observed) if observed else None}


def identity(row: dict) -> tuple | None:
    if any(row.get(k) is None or row.get(k) == "" for k in JOIN_FIELDS):
        return None
    return tuple(str(row[k]) for k in JOIN_FIELDS)


def project(row: dict, fields) -> dict:
    return {k: row[k] for k in fields if k in row}


def in_window(value, start, end) -> bool:
    point = timestamp(value)
    return point is not None and (start is None or point >= start) and (end is None or point < end)


def interval_union(rows: list[dict], start=None, end=None) -> dict:
    workers = defaultdict(list)
    missing = 0
    for row in rows:
        if row.get("reason") != "checks":
            continue
        left, right = timestamp(row.get("start")), timestamp(row.get("end"))
        if not row.get("worker_id") or left is None or right is None or right < left:
            missing += 1
            continue
        left = max(left, start) if start is not None else left
        right = min(right, end) if end is not None else right
        if right > left:
            workers[row["worker_id"]].append((left, right))
    totals = {}
    for worker, intervals in sorted(workers.items()):
        merged = []
        for left, right in sorted(intervals):
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else:
                merged.append((left, right))
        totals[worker] = sum(right - left for left, right in merged)
    return {"seconds_by_worker": totals, "seconds": sum(totals.values()),
            "hours": sum(totals.values()) / 3600, "missing_intervals": missing,
            "observations": sum(r.get("reason") == "checks" for r in rows)}


def metrics(document: dict) -> dict:
    if document.get("schema_version") != 1:
        raise ValueError("expected schema_version=1")
    as_of = timestamp(document["as_of"])
    start, end = timestamp(document.get("window_start")), timestamp(document.get("window_end"))
    if start is not None and end is not None and end <= start:
        raise ValueError("window_end must follow window_start")
    suite_fields = (*JOIN_FIELDS, "suite", "status", "conclusion", "queued_at", "started_at",
                    "completed_at", "billed_minutes")
    suites = [project(r, suite_fields) for r in document.get("suites", [])]
    indexed = defaultdict(list)
    for row in suites:
        key = identity(row)
        if key is not None and row.get("suite"):
            indexed[(key, row["suite"])].append(row)
    attempts = []
    queue_samples = []
    run_samples = []
    excluded = []
    joined_indices = set()
    seen = Counter(identity(a) for a in document.get("attempts", []))
    for raw in document.get("attempts", []):
        attempt = project(raw, (*JOIN_FIELDS, "first_push_at", "required_suites"))
        if (start is not None or end is not None) and not in_window(
                raw.get("first_push_at"), start, end):
            excluded.append(attempt)
            continue
        key = identity(raw)
        required = raw.get("required_suites", [])
        if len(required) != len(set(required)):
            raise ValueError("required_suites must be unique")
        issues = []
        if key is None:
            issues.append("incomplete_identity")
        elif seen[key] != 1:
            issues.append("duplicate_attempt")
        if not required:
            issues.append("missing_required_suite_manifest")
        observations = []
        queue, runtime, completion = [], [], []
        green = bool(required) and not issues
        for suite in required:
            matches = indexed.get((key, suite), []) if key is not None else []
            if len(matches) != 1:
                issues.append(f"{suite}:" + ("missing" if not matches else "duplicate"))
                observations.append({"suite": suite, "status": "missing" if not matches else "duplicate",
                                     "observations": matches})
                queue.append(None)
                runtime.append(None)
                green = False
                continue
            row = matches[0]
            joined_indices.add((key, suite))
            q = duration(row.get("queued_at"), row.get("started_at"))
            r = duration(row.get("started_at"), row.get("completed_at"))
            green = green and row.get("status") == "completed" and row.get("conclusion") == "success"
            t = timestamp(row.get("completed_at"))
            if t is None or q is None or r is None or t > as_of:
                green = False
                issues.append(f"{suite}:missing_or_invalid_timing")
            else:
                completion.append(t)
            queue.append(q)
            runtime.append(r)
            observations.append(row)
        first_push = timestamp(raw.get("first_push_at"))
        green_at = max(completion) if green and completion else None
        push_to_green = green_at - first_push if green_at is not None and first_push is not None else None
        if push_to_green is not None and push_to_green < 0:
            issues.append("green_precedes_push")
            push_to_green = None
            green = False
        attempt.update({"observations": observations, "issues": issues, "all_required_green": green,
                        "push_to_green_seconds": push_to_green,
                        "queue_seconds": distribution(queue), "run_seconds": distribution(runtime)})
        attempts.append(attempt)
        queue_samples.extend(queue)
        run_samples.extend(runtime)
    deliveries = []
    excluded_deliveries = []
    delivery_fields = ("task_id", "head_sha", "base_sha", "first_commit_at", "landed_at",
                       "rebase_events", "rerun_events")
    delivery_counts = Counter((r.get("task_id"), r.get("head_sha"), r.get("base_sha"))
                              for r in document.get("deliveries", []))
    for raw in document.get("deliveries", []):
        row = project(raw, delivery_fields)
        if (start is not None or end is not None) and not in_window(raw.get("landed_at"), start, end):
            excluded_deliveries.append(row)
            continue
        dkey = (raw.get("task_id"), raw.get("head_sha"), raw.get("base_sha"))
        landed = timestamp(raw.get("landed_at"))
        valid = all(dkey) and delivery_counts[dkey] == 1 and landed is not None and landed <= as_of
        row["identity_valid"] = bool(valid)
        row["landing_seconds"] = duration(raw.get("first_commit_at"), raw.get("landed_at")) if valid else None
        # Billed minutes are explicit provider accounting, never inferred from wall time.
        related_attempts = [a for a in document.get("attempts", []) if identity(a) is not None and
                            (a["task_id"], a["head_sha"], a["base_sha"]) == dkey]
        expected = {(identity(a), name) for a in related_attempts
                    for name in a.get("required_suites", [])}
        related = [s for s in suites if
                   (s.get("task_id"), s.get("head_sha"), s.get("base_sha")) == dkey]
        bills = [number(s.get("billed_minutes")) for s in related]
        actual = Counter((identity(s), s.get("suite")) for s in related)
        complete = (bool(expected) and expected <= actual.keys()
                    and all(identity(s) is not None for s in related)
                    and all(a.get("required_suites") for a in related_attempts)
                    and len({identity(a) for a in related_attempts}) == len(related_attempts)
                    and all(v == 1 for v in actual.values()))
        row["billed_minutes"] = (sum(bills) if valid and complete and bills
                                 and all(b is not None for b in bills) else None)
        deliveries.append(row)
    waits = interval_union(document.get("worker_waits", []), start,
                           min(end, as_of) if end is not None else as_of)
    # Events without explicit IDs are missing evidence; never count duplicates twice.
    events = {}
    conflicting_events = []
    missing_events = 0
    for event in document.get("events", []):
        if not event.get("id"):
            missing_events += 1
        elif (start is None and end is None) or in_window(event.get("at"), start, end):
            projected = project(event, ("id", "kind", "at", *JOIN_FIELDS))
            if event["id"] in events and events[event["id"]] != projected:
                conflicting_events.append(projected)
            else:
                events[event["id"]] = projected
    incidents = {r["root_cause"] for r in document.get("incidents", []) if r.get("root_cause")
                 and ((start is None and end is None) or in_window(r.get("at"), start, end))}
    waiting = []
    for row in document.get("prs", []):
        age = duration(row.get("waiting_since"), document["as_of"])
        if row.get("green") is True and row.get("mergeable") is True and age is not None and age > 7200:
            waiting.append(project(row, ("id", "head_sha", "base_sha", "waiting_since")))
    orphaned = [r for r in suites if (identity(r), r.get("suite")) not in joined_indices]
    return {
        "schema_version": 1, "kind": "delivery_metrics", "as_of": document["as_of"],
        "window_start": document.get("window_start"), "window_end": document.get("window_end"),
        "percentile_method": "nearest_rank_ceiling", "join_fields": list(JOIN_FIELDS),
        "attempts": attempts, "excluded_or_undated_attempts": excluded,
        "unjoined_suite_observations": orphaned, "suite_observations": suites,
        "suite_status_counts": dict(sorted(Counter(r.get("status", "missing") for r in suites).items())),
        "deliveries": deliveries, "excluded_or_undated_deliveries": excluded_deliveries,
        "metrics": {
            "push_to_green_seconds": distribution([a["push_to_green_seconds"] for a in attempts]),
            "required_suite_queue_seconds": distribution(queue_samples),
            "required_suite_run_seconds": distribution(run_samples),
            "first_commit_to_dev_seconds": distribution([d["landing_seconds"] for d in deliveries]),
            "billed_minutes_per_landing": distribution([d["billed_minutes"] for d in deliveries]),
            "rebases_per_landed_task": distribution([d.get("rebase_events") for d in deliveries]),
            "reruns_per_landed_task": distribution([d.get("rerun_events") for d in deliveries]),
            "worker_check_wait": waits,
            "train_events": dict(sorted(Counter(
                e.get("kind", "unknown") for e in events.values()
                if e["id"] not in {c["id"] for c in conflicting_events}).items())),
            "missing_event_ids": missing_events, "red_dev_root_causes": len(incidents),
            "incidents_missing_root_cause": sum(not r.get("root_cause") for r in document.get("incidents", [])),
            "green_mergeable_prs_waiting_over_2h": waiting,
        },
        "events": list(events.values()), "conflicting_events": conflicting_events,
        "worker_wait_observations": [project(r, ("worker_id", "start", "end", "reason", *JOIN_FIELDS))
                                      for r in document.get("worker_waits", [])],
        "incident_observations": [project(r, ("id", "root_cause", "at", *JOIN_FIELDS))
                                   for r in document.get("incidents", [])],
        "red_dev_root_cause_ids": sorted(incidents),
        "missing_sources": [key for key in ("attempts", "suites", "deliveries", "worker_waits",
                                            "events", "incidents", "prs") if key not in document],
    }


def history(pages: list[dict], classifications: list[dict], baseline: Path | None,
            requested: int = 300) -> dict:
    """Snapshot completed runs, preserving active/missing metadata and unknown causes."""
    indexed = {}
    unidentified = []
    for page in pages:
        for raw in page.get("workflow_runs", []):
            row = project(raw, ("id", "workflow_id", "path", "name", "head_sha", "head_branch",
                                "run_attempt", "status", "conclusion", "html_url", *TIMES))
            if row.get("id") is None or row.get("run_attempt") is None:
                unidentified.append(row)
            else:
                key = (str(row["id"]), str(row["run_attempt"]))
                if key in indexed and indexed[key] != row:
                    raise ValueError(f"conflicting workflow observation {key}")
                indexed[key] = row
    rows = sorted(indexed.values(), key=lambda r: (r.get("created_at", ""), str(r["id"])), reverse=True)
    completed = [r for r in rows if r.get("status") == "completed"][:requested]
    active = [r for r in rows if r.get("status") != "completed"]
    evidence = defaultdict(list)
    for row in classifications:
        evidence[(str(row["run_id"]), str(row["run_attempt"]))].append(row)
    for row in completed + active:
        candidates = evidence.get((str(row["id"]), str(row["run_attempt"])), [])
        classification = "unknown"
        refs = []
        if len(candidates) == 1:
            claim = candidates[0]
            if claim.get("classification") not in FAILURE_CLASSES:
                raise ValueError("unknown failure class")
            if claim.get("evidence"):
                classification = claim["classification"]
                refs = claim["evidence"]
        row["failure_classification"] = classification
        row["classification_evidence"] = refs
        row["missing_join_fields"] = [k for k in JOIN_FIELDS if row.get(k) is None]
        row["queue_seconds"] = duration(row.get("created_at"), row.get("run_started_at"))
        # updated_at can be artifact/metadata updates; it is not job completion.
        row["run_seconds"] = None
    failures = [r for r in completed if r.get("conclusion") in {"failure", "timed_out"}]
    baseline_record = None
    if baseline:
        text = baseline.read_text(encoding="utf-8")
        import re

        baseline_record = {"name": baseline.name,
                           "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
                           "known_failed_nodeids": re.findall(r"^- `(tests/[^`]+)`", text, re.M)}
    return {
        "schema_version": 1, "kind": "historical_ci_snapshot", "requested_completed": requested,
        "completed_count": len(completed), "shortage": max(0, requested - len(completed)),
        "completed_runs": completed, "active_runs": active, "unidentified_runs": unidentified,
        "source_page_count": len(pages), "population": "workflow runs; not individual test failures",
        "conclusions": dict(sorted(Counter(r.get("conclusion") or "missing" for r in completed).items())),
        "failure_classes": dict(sorted(Counter(r["failure_classification"] for r in failures).items())),
        "workflow_counts": dict(sorted(Counter(r.get("path", "missing") for r in completed).items())),
        "flake_run_count": sum(r["failure_classification"] == "test_flake" for r in failures),
        "baseline": baseline_record,
        "limitations": ["Historical only; no controlled or live 14-day delivery baseline",
                        "Workflow metadata cannot establish task/base/candidate/suite identity",
                        "No failed node or flake inferred from run conclusion; unknown remains unknown"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    historical = sub.add_parser("history")
    historical.add_argument("--runs", type=Path, action="append", required=True)
    historical.add_argument("--classifications", type=Path)
    historical.add_argument("--baseline", type=Path)
    historical.add_argument("--requested", type=int, default=300)
    historical.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "report":
        result = metrics(load(args.input))
    else:
        if args.requested < 1:
            parser.error("--requested must be positive")
        result = history([load(p) for p in args.runs],
                         load(args.classifications) if args.classifications else [],
                         args.baseline, args.requested)
    inputs = [args.input] if args.command == "report" else [
        *args.runs, *([args.classifications] if args.classifications else []),
        *([args.baseline] if args.baseline else [])]
    result["input_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    result["tool_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write(args.output, result)
    print(f"Wrote {result['kind']} to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
