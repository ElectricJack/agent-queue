"""Bazel pytest runtime contracts, independent of the environment launcher.

B1 calls run_target before bootstrap and supplies H2's owned-process cleanup.
Nothing here reads operator configuration or modifies the parent environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

TEST_SECONDS = 60.0
TARGET_SECONDS = 300.0
SCHEMA_VERSION = 1


def normalize_nodeid(nodeid: str) -> str:
    """Normalize only the path: parameter IDs are opaque pytest identities."""
    path, sep, suffix = nodeid.partition("::")
    path = path.replace("\\", "/")
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or ".." in parts or ":" in parts[0]:
        raise ValueError(f"node ID must be relative to the pytest root: {nodeid!r}")
    return str(PurePosixPath(path)) + sep + suffix


def shard_for(nodeid: str, count: int) -> int:
    if count < 1:
        raise ValueError("shard count must be positive")
    return int.from_bytes(hashlib.sha256(normalize_nodeid(nodeid).encode()).digest(), "big") % count


@dataclass(frozen=True)
class Shard:
    index: int = 0
    count: int = 1
    status_file: Path | None = None

    def __post_init__(self) -> None:
        if self.count < 1 or not 0 <= self.index < self.count:
            raise ValueError("require 0 <= TEST_SHARD_INDEX < TEST_TOTAL_SHARDS")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Shard:
        index, count = env.get("TEST_SHARD_INDEX"), env.get("TEST_TOTAL_SHARDS")
        status = env.get("TEST_SHARD_STATUS_FILE")
        if (index is None) != (count is None):
            raise ValueError("TEST_SHARD_INDEX and TEST_TOTAL_SHARDS must be set together")
        if count is not None and not status:
            raise ValueError("sharding requires TEST_SHARD_STATUS_FILE")
        return cls(
            int(index if index is not None else "0"),
            int(count if count is not None else "1"),
            Path(status) if status else None,
        )

    def acknowledge(self) -> None:
        if self.status_file is not None:
            # Do not invent a missing Bazel output directory or ignore an I/O failure.
            self.status_file.touch()

    def select(self, nodeids: Sequence[str]) -> list[str]:
        normalized = [normalize_nodeid(node) for node in nodeids]
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate normalized node IDs in collection")
        return [node for node in normalized if shard_for(node, self.count) == self.index]


class BudgetExpired(TimeoutError):
    """A failed test/target, never a skip or an expected failure."""


@dataclass
class Deadline:
    seconds: float
    clock: Callable[[], float] = time.monotonic
    started: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("budget must be finite and positive")
        if self.started is None:
            self.started = self.clock()

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed)

    def check(self, label: str) -> None:
        if self.elapsed > self.seconds:
            raise BudgetExpired(f"{label} exceeded {self.seconds:g}s budget ({self.elapsed:g}s)")


async def _bounded(operation: Callable[[], Awaitable[Any]], seconds: float) -> Any:
    if seconds <= 0:
        raise BudgetExpired("target deadline expired")
    task = asyncio.create_task(operation())
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if not done:
            raise BudgetExpired("target deadline expired")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            # Unlike wait_for, do not wait indefinitely for cooperative cancellation.
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())


async def run_target(
    bootstrap: Callable[[], Awaitable[Any]],
    execute: Callable[[], Awaitable[int]],
    cleanup: Callable[[], Awaitable[Any]],
    kill: Callable[[], None],
    *,
    clock: Callable[[], float] = time.monotonic,
    cleanup_reserve: float = 5.0,
    started: float | None = None,
) -> dict[str, Any]:
    """Bound the complete lifecycle to 300s, reserving time for owned cleanup.

    Call at launcher entry (or pass its monotonic ``started`` timestamp). All
    callbacks operate exclusively on H2's private resources. ``kill`` must be a
    synchronous, nonblocking SIGKILL of owned process groups; it is mandatory
    when cleanup fails or a budget expires. It must also handle partial bootstrap.
    Async callbacks must not block the event loop. B1 writes this receipt AFTER
    cleanup alongside the pytest sidecar; pytest alone cannot certify target time.
    """
    if not 0 < cleanup_reserve < TARGET_SECONDS:
        raise ValueError("cleanup reserve must be within target budget")
    budget = Deadline(TARGET_SECONDS, clock, started)
    phases: dict[str, float] = {}
    errors: list[str] = []
    status = 1
    cancelled = False
    try:
        for name, operation in (("bootstrap", bootstrap), ("pytest", execute)):
            begin = clock()
            try:
                result = await _bounded(operation, budget.remaining - cleanup_reserve)
                if budget.remaining < cleanup_reserve:
                    raise BudgetExpired(f"{name} exhausted target execution budget")
                if name == "pytest":
                    status = int(result)
            finally:
                phases[name] = clock() - begin
    except asyncio.CancelledError:
        cancelled = True
        errors.append("target cancelled")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        begin = clock()
        try:
            await _bounded(cleanup, budget.remaining)
            budget.check("target including cleanup")
        except BaseException as exc:
            errors.append(f"cleanup {type(exc).__name__}: {exc}")
        finally:
            phases["cleanup"] = clock() - begin
            if errors:
                try:
                    kill()
                except Exception as exc:
                    errors.append(f"kill {type(exc).__name__}: {exc}")
        if budget.elapsed > TARGET_SECONDS:
            errors.append("target exceeded 300s including cleanup")
    if cancelled:
        raise asyncio.CancelledError
    return {
        "schema_version": SCHEMA_VERSION,
        "budget_seconds": TARGET_SECONDS,
        "elapsed": budget.elapsed,
        "phases": phases,
        "errors": errors,
        "exit_status": 1 if errors else status,
    }


def validate_target(receipt: dict | None) -> list[str]:
    """A pytest sidecar alone cannot prove the launcher's full timing contract."""
    if receipt is None:
        return ["missing target lifecycle receipt"]
    errors = []
    if receipt.get("schema_version") != SCHEMA_VERSION:
        errors.append("unknown target receipt schema")
    if receipt.get("exit_status") != 0 or receipt.get("errors"):
        errors.append("target did not pass")
    if receipt.get("budget_seconds") != TARGET_SECONDS:
        errors.append("target budget changed")
    phases = receipt.get("phases", {})
    if set(phases) != {"bootstrap", "pytest", "cleanup"}:
        errors.append("incomplete target lifecycle")
    elapsed = receipt.get("elapsed", float("nan"))
    durations = [elapsed, *phases.values()]
    if any(not isinstance(d, (int, float)) or not math.isfinite(d) or d < 0 for d in durations):
        errors.append("invalid target timing")
    elif elapsed > TARGET_SECONDS or sum(phases.values()) > elapsed + 1e-6:
        errors.append("target timing exceeds budget or is inconsistent")
    return errors


def flake_contract(owner: str, evidence: str, tags: Sequence[str], retries: int = 0) -> dict:
    """Validate a quarantine without making it pass or excluding it from coverage."""
    if not owner.strip() or not evidence.strip():
        raise ValueError("flaky tests require an owner and evidence")
    if not {"flaky", "no-cache", "external"} <= set(tags):
        raise ValueError("flaky tests require explicit flaky, no-cache and external target tags")
    if retries != 0:
        raise ValueError("hidden retries are forbidden; Bazel flaky must remain False")
    return {"owner": owner, "evidence": evidence, "tags": sorted(set(tags)), "retries": 0}


def validate_shards(expected: Sequence[str], reports: Sequence[dict]) -> list[str]:
    """Fail closed on missing/duplicate nodes or shards, even for empty shards.

    ``expected`` comes from the suite manifest, not the result being validated.
    Outcomes are retained for V2's parity comparator; this checks execution coverage.
    """
    errors: list[str] = []
    expected = [normalize_nodeid(node) for node in expected]
    if not expected or len(expected) != len(set(expected)):
        errors.append("expected nodes must be nonempty and unique")
    if not reports:
        return errors + ["missing shard reports"]
    count = reports[0]["shard"]["count"]
    if Counter(r["shard"]["index"] for r in reports) != Counter(range(count)):
        errors.append("missing or duplicate shard indices")
    seen: list[str] = []
    for report in reports:
        shard = Shard(report["shard"]["index"], report["shard"]["count"])
        if report.get("schema_version") != SCHEMA_VERSION or shard.count != count:
            errors.append("incompatible shard contract")
        if Counter(report["collected"]) != Counter(expected):
            errors.append("collected nodes differ from expected suite")
        selected = shard.select(expected)
        if Counter(report["selected"]) != Counter(selected):
            errors.append("incorrect shard assignment")
        nodes = report["nodes"]
        if Counter(n["nodeid"] for n in nodes) != Counter(selected):
            errors.append("missing, duplicate or unexpected executed nodes")
        for node in nodes:
            elapsed = node.get("elapsed", float("nan"))
            if not math.isfinite(elapsed) or elapsed < 0 or elapsed > TEST_SECONDS:
                errors.append(f"invalid or exceeded node budget: {node['nodeid']}")
            if node.get("budget_exceeded"):
                errors.append(f"node budget failure: {node['nodeid']}")
            phases = node["phases"]
            phase_durations = [phase["duration"] for phase in phases.values()]
            if any(not math.isfinite(d) or d < 0 for d in phase_durations):
                errors.append(f"invalid phase timing: {node['nodeid']}")
            elif sum(phase_durations) > elapsed + 1e-6:
                errors.append(f"phase timing exceeds node total: {node['nodeid']}")
            if set(phases) - {"setup", "call", "teardown"}:
                errors.append(f"unexpected phases: {node['nodeid']}")
            if any(phase["outcome"] not in {"passed", "skipped"} for phase in phases.values()):
                errors.append(f"failed or invalid phase: {node['nodeid']}")
            if "setup" not in phases or "teardown" not in phases:
                errors.append(f"incomplete phases: {node['nodeid']}")
            elif phases["setup"]["outcome"] == "passed" and "call" not in phases:
                errors.append(f"missing call: {node['nodeid']}")
        seen.extend(n["nodeid"] for n in nodes)
        if report.get("errors") or report.get("collection_errors"):
            errors.append("shard recorded runtime or collection errors")
        if report.get("exit_status") != 0:
            errors.append("shard did not pass")
    if Counter(seen) != Counter(expected):
        errors.append("suite nodes were not executed exactly once")
    return errors


def promotion_blockers(reports: Sequence[dict]) -> list[str]:
    return sorted(
        {
            f"unresolved flaky quarantine: {node['nodeid']} ({node['flake']['owner']})"
            for report in reports
            for node in report["nodes"]
            if node.get("flake")
        }
    )
