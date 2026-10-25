#!/usr/bin/env python3
"""Versioned, observational CI evidence. Never issues a required check or attestation.

BEP fields follow Bazel 8.3.1's build_event_stream.proto. Absent metrics remain
unknown; actionsCreated is deliberately not used as an eligible-action denominator.
Raw test outputs are copied unchanged before an output tree is discarded.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlparse

VERSION = 1
PHASES = ("incremental", "cold", "warm", "server_discard")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reference(root, path):
    return {"path": Path(path).relative_to(root).as_posix(), "sha256": digest(path)}


def file_manifest(root):
    return [{"path": p.relative_to(root).as_posix(), "sha256": digest(p)}
            for p in sorted(Path(root).rglob("*")) if p.is_file()]


def number(value):
    if isinstance(value, str):  # protobuf JSON encodes int64 as strings
        value = float(value)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid nonnegative metric")
    return value


def fraction(executed, eligible):
    if executed is None or eligible is None or eligible == 0:
        return None
    if executed > eligible:
        raise ValueError("executed exceeds eligible")
    return executed / eligible


def bep_report(events, expected_targets, *, prior_eligible_actions=None):
    """Require a complete event graph and one passed, non-retried result per shard."""
    issues, posted, announced = [], [], set()
    results, summaries, targets = [], {}, {}
    metrics, finished = [], []
    for event in events:
        identity = event.get("id", {})
        posted.append(json.dumps(identity, sort_keys=True))
        announced.update(json.dumps(child, sort_keys=True) for child in event.get("children", []))
        if "aborted" in event:
            issues.append("aborted event")
        if "testResult" in event:
            result = event["testResult"]
            key = identity["testResult"]
            label = key["label"]
            if key.get("run", 1) != 1 or key.get("attempt", 1) != 1:
                issues.append(f"retry or repeated run: {label}")
            if result.get("status") != "PASSED":
                issues.append(f"test not passed: {label}")
            info = result.get("executionInfo", {})
            source = ("local" if result.get("cachedLocally", False) else
                      "disk" if info.get("cachedRemotely", False) else "executed")
            # No remote cache is configured; cachedRemotely includes the disk cache.
            duration = result.get("testAttemptDuration")
            seconds = (number(duration.removesuffix("s")) if duration is not None else
                       number(result["testAttemptDurationMillis"]) / 1000
                       if "testAttemptDurationMillis" in result else None)
            # BEP IDs are one-based; pytest/Bazel's TEST_SHARD_INDEX is zero-based.
            results.append({"label": label, "shard": int(key.get("shard", 1)) - 1,
                            "source": source, "seconds": seconds})
            targets.setdefault(label, []).append(source)
        if "testSummary" in event:
            label = identity["testSummary"]["label"]
            if label in summaries:
                issues.append(f"duplicate target summary: {label}")
            summaries[label] = event["testSummary"]
        if "buildMetrics" in event:
            metrics.append(event["buildMetrics"])
        if "finished" in event:
            finished.append(event["finished"])
    duplicates = [key for key, count in Counter(posted).items() if count > 1]
    if duplicates or announced - set(posted) or not any(e.get("lastMessage") for e in events):
        issues.append("incomplete or duplicate BEP event graph")
    if (len(finished) != 1 or "exitCode" not in finished[0] or
            finished[0]["exitCode"].get("code", 0) != 0):
        issues.append("missing or failed build finish")
    if len(metrics) != 1:
        issues.append("missing or duplicate build metrics")
    if set(summaries) != set(expected_targets) or set(targets) != set(expected_targets):
        issues.append("missing or unexpected eligible target")
    for label, summary in summaries.items():
        rows = [r for r in results if r["label"] == label]
        # Bazel's unsharded TestParams uses zero, omitted by protobuf JSON.
        count = max(1, int(summary.get("shardCount", 0)))
        if (summary.get("overallStatus") != "PASSED" or
                Counter(r["shard"] for r in rows) != Counter(range(count)) or
                int(summary.get("totalRunCount", 0)) != count):
            issues.append(f"incomplete, failed or duplicate shards: {label}")
    data = metrics[0] if len(metrics) == 1 else {}
    actions = data.get("actionSummary", {})
    graph = data.get("buildGraphMetrics", {})
    executed_actions = (number(actions["actionsExecuted"]) if "actionsExecuted" in actions
                        else 0 if "actionSummary" in data else None)
    eligible_actions = (number(graph["actionCount"]) if "actionCount" in graph else None)
    denominator_source = "BEP configured action graph"
    if eligible_actions is None and prior_eligible_actions is not None:
        eligible_actions = number(prior_eligible_actions)
        denominator_source = "same-graph cold invocation (analysis reused)"
    if eligible_actions is None:
        issues.append("missing eligible-action count")
    executed = sum("executed" in sources for sources in targets.values())
    sources = dict(Counter(r["source"] for r in results))
    durations = [{"label": label,
                  "seconds": max(r["seconds"] for r in results
                                 if r["label"] == label and r["seconds"] is not None)}
                 for label in targets
                 if any(r["label"] == label and r["seconds"] is not None for r in results)]
    slowest = sorted(durations, key=lambda r: (-r["seconds"], r["label"]))[:5]
    return {"schema_version": VERSION, "complete": not issues, "issues": issues,
            "eligible_targets": len(expected_targets), "executed_targets": executed,
            "executed_target_fraction": fraction(executed, len(expected_targets)),
            "eligible_actions": eligible_actions, "eligible_action_source": denominator_source,
            "executed_actions": executed_actions,
            "executed_action_fraction": fraction(executed_actions, eligible_actions),
            "cache_sources": sources, "five_slowest": slowest, "results": results,
            "action_cache_statistics": actions.get("actionCacheStatistics"),
            "all_tests_cached": bool(results) and not issues and executed == 0}


def retain_test_outputs(events, output):
    """Keep BEP-local files only; never fetch a test-controlled URL or extract a zip."""
    output = Path(output)
    retained = {}
    for event in events:
        if "testResult" not in event:
            continue
        key = event["id"]["testResult"]
        label, shard = key["label"], int(key.get("shard", 1)) - 1
        name = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        destination = output / "targets" / name
        destination.mkdir(parents=True, exist_ok=True)
        files = {}
        for item in event["testResult"].get("testActionOutput", []):
            uri = urlparse(item.get("uri", ""))
            if uri.scheme != "file" or uri.netloc:
                raise ValueError("BEP artifact must be a local file")
            source = Path(unquote(uri.path))
            relative = Path(item["name"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe BEP artifact name")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files[relative.name] = target
        # --nozip_undeclared_test_outputs makes pytest.json and target.json visible.
        retained.setdefault(label, []).append({"index": shard, "files": files})
    return retained


def window_report(commits, samples, suites):
    """Oldest-first explicit first-parent dev history; missing commits reset streaks.

    This report is observational. A counter reaching twenty grants no permission.
    Duplicate attempts need an explicit operator choice rather than best-of-reruns.
    """
    if len(commits) != len(set(commits)):
        raise ValueError("duplicate dev commits")
    by_sha = {}
    for sample in samples:
        by_sha.setdefault(sample.get("sha"), []).append(sample)
    counters = {name: 0 for name in suites}
    rows = []
    for sha in commits:
        candidates = by_sha.get(sha, [])
        sample = candidates[0] if len(candidates) == 1 else None
        values = {}
        for name in suites:
            valid = bool(sample and sample.get("schema_version") == VERSION and
                         sample.get("event") == "push" and sample.get("ref") == "refs/heads/dev"
                         and sample.get("trusted") is True and
                         sample.get("suites", {}).get(name, {}).get("success") is True)
            counters[name] = counters[name] + 1 if valid else 0
            values[name] = {"success": valid, "consecutive_dev_commits": counters[name]}
        rows.append({"sha": sha, "suites": values,
                     "missing_or_ambiguous": sample is None})
    return {"schema_version": VERSION, "observational": True, "required_samples": 20,
            "commits": rows, "counters": counters}
