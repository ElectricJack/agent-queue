"""Read-only token-efficiency report: corrected usage, polling and outcomes per attempt.

An operator-run comparison of two bounded windows, not a continuous audit loop and
not a model call.  Inputs are a frozen database export taken in a READ ONLY
transaction (:func:`export_rows`) and the local harness transcripts the attempts
name.  Nothing here writes to the database.

Accounting follows the corrected rules from the 2026-10-01 usage audit:

* Claude: one API call per ``message.id``, keeping the maximum of each token
  category across the streamed content rows that repeat it.
* Codex: one call per ``token_usage_record.response_id`` (or, on older rollouts,
  per change in cumulative ``token_count`` totals).  ``input_tokens`` includes
  ``cached_input_tokens``, so uncached input is the difference.

Token categories are kept apart.  Their sum is a count of token *events*, not a
subscription quota percentage: cache reads, cache writes, uncached input and
output are priced and rate-limited differently, so no total here measures quota.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.sessions.transcripts.base import parse_iso_ts

CATEGORIES = ("uncached_input", "cache_read", "cache_write", "output")

#: Heuristic command categories.  They overlap (``aq task close --claim-next`` is
#: a close and a claim) and count tool calls, not avoidable work.
_AQ = r"\baq(?:\s+--[\w-]+)*\s+"  # ``aq`` and global flags, then the command group
COMMAND_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern)
    for name, pattern in {
        "sleep": r"(?:^|[\s;&|(]|\\n)sleep\s+\d",
        "claim": _AQ + r"task\s+claim\b|--claim-next\b",
        "close": _AQ + r"task\s+close\b",
        "heartbeat": _AQ + r"task\s+heartbeat\b",
        "inbox": _AQ + r"message\s+inbox\b",
        "durable_wait": (
            _AQ + r"wait\s+(?:register|show|get)\b"
            r"|" + _AQ + r"job\s+submit\b[^\n;&|]*--wait\b|--aq-detach\b"
        ),
        "status_poll": (
            _AQ + r"(?:task\s+(?:show|comments|status)|integration\s+status"
            r"|job\s+(?:show|status|list)|message\s+status)\b"
            r"|\bgh\s+(?:pr\s+(?:view|checks|status)|run\s+(?:view|watch|list))\b"
        ),
        "output_poll": r"\btail\b[^\n;&|'\"]*\.(?:log|output)\b|\bps\s+-|\bkill\s+-0\b",
        "test": r"\baq\s+test\b|\bpytest\b",
    }.items()
}
#: Claude Code's locally generated assistant rows (no API call was made).
SYNTHETIC_MODEL = "<synthetic>"
#: Tool names that are output polls by construction (Claude background output).
OUTPUT_POLL_TOOLS = frozenset({"TaskOutput", "BashOutput"})
#: The categories summed as "polling" in the report.  Claims, closes, tests and
#: durable waits are necessary work and are reported but never counted as polls.
POLLING = ("sleep", "status_poll", "output_poll", "inbox")

DEFAULT_THRESHOLDS: dict[str, float] = {
    "min_attempts": 5,
    "pass_rate_drop_pp": 10.0,
    "latency_increase_pct": 50.0,
    "repair_share_increase_pp": 10.0,
    "churn_share_increase_pp": 10.0,
    "stuck_after_hours": 4.0,
}
#: An attempt that ends this soon without an outcome never worked the task (a slot
#: reset or launch failure, a reclaim): it is churn, not a fast task.
CHURN_SECONDS = 60.0
#: Attempt or session states that mean the attempt cannot still be running.
ENDED_STATES = frozenset({"stopped"})


@dataclass
class Call:
    ts: float
    model: str | None
    usage: dict[str, int]
    subagent: bool = False

    @property
    def context(self) -> int:
        return self.usage["uncached_input"] + self.usage["cache_read"] + self.usage["cache_write"]


@dataclass
class TranscriptUsage:
    calls: list[Call] = field(default_factory=list)
    tools: list[tuple[float, str, str]] = field(default_factory=list)
    compactions: list[float] = field(default_factory=list)
    quota: list[tuple[float, float, int]] = field(default_factory=list)
    skipped_lines: int = 0


def classify_tool(name: str, text: str) -> set[str]:
    found = {category for category, pattern in COMMAND_PATTERNS.items() if pattern.search(text)}
    if name in OUTPUT_POLL_TOOLS or (name == "Read" and text.endswith(".output")):
        found.add("output_poll")
    return found


def _records(lines: Iterable[bytes | str], usage: TranscriptUsage):
    for line in lines:
        if isinstance(line, bytes) and not line.endswith(b"\n"):
            usage.skipped_lines += 1  # a partial last line is still being written
            continue
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            usage.skipped_lines += 1
            continue
        if isinstance(raw, dict):
            yield raw


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def read_claude(lines: Iterable[bytes | str], *, subagent: bool = False) -> TranscriptUsage:
    """One call per API ``message.id``; maximum of each category across its rows."""
    result = TranscriptUsage()
    calls: dict[str, Call] = {}
    tool_ids: set[str] = set()
    for raw in _records(lines, result):
        ts = parse_iso_ts(raw.get("timestamp"))
        if raw.get("subtype") == "compact_boundary":
            result.compactions.append(ts)
        message = raw.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        api_id = message.get("id")
        if (raw.get("type") == "assistant" and isinstance(usage, dict) and usage and api_id
                and message.get("model") != SYNTHETIC_MODEL):
            observed = {
                "uncached_input": _int(usage.get("input_tokens")),
                "cache_read": _int(usage.get("cache_read_input_tokens")),
                "cache_write": _int(usage.get("cache_creation_input_tokens")),
                "output": _int(usage.get("output_tokens")),
            }
            call = calls.get(api_id)
            if call is None:
                calls[api_id] = Call(ts, message.get("model"), observed, subagent)
            else:
                for key, value in observed.items():
                    call.usage[key] = max(call.usage[key], value)
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            block_id = block.get("id")
            if block_id in tool_ids:
                continue
            tool_ids.add(block_id)
            tool_input = block.get("input")
            text = ""
            if isinstance(tool_input, dict):
                text = tool_input.get("command") or tool_input.get("file_path") or ""
            result.tools.append((ts, str(block.get("name") or ""), str(text)))
    result.calls = sorted(calls.values(), key=lambda call: call.ts)
    return result


def _codex_usage(raw: Mapping[str, Any]) -> dict[str, int]:
    total_input = _int(raw.get("input_tokens"))
    cached = _int(raw.get("cached_input_tokens"))
    return {
        "uncached_input": max(0, total_input - cached),
        "cache_read": cached,
        "cache_write": _int(raw.get("cache_write_input_tokens")),
        "output": _int(raw.get("output_tokens")),
    }


def read_codex(lines: Iterable[bytes | str]) -> TranscriptUsage:
    """One call per ``response_id``; cumulative ``token_count`` changes as fallback."""
    result = TranscriptUsage()
    by_response: dict[str, Call] = {}
    fallback: list[Call] = []
    last_total: tuple | None = None
    model: str | None = None
    tool_ids: set[str] = set()
    for raw in _records(lines, result):
        ts = parse_iso_ts(raw.get("timestamp"))
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = raw.get("type")
        if kind == "turn_context" and payload.get("model"):
            model = str(payload["model"])
        elif kind == "token_usage_record":
            response_id = payload.get("response_id")
            usage = payload.get("usage")
            if response_id and isinstance(usage, dict):
                observed = _codex_usage(usage)
                call = by_response.setdefault(response_id, Call(ts, model, observed))
                for key, value in observed.items():
                    call.usage[key] = max(call.usage[key], value)
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                total = tuple(sorted(info["total_token_usage"].items()))
                if total != last_total and isinstance(info.get("last_token_usage"), dict):
                    fallback.append(Call(ts, model, _codex_usage(info["last_token_usage"])))
                last_total = total
            limits = payload.get("rate_limits")
            primary = limits.get("primary") if isinstance(limits, dict) else None
            if isinstance(primary, dict) and isinstance(primary.get("used_percent"), (int, float)):
                result.quota.append(
                    (ts, float(primary["used_percent"]), _int(primary.get("window_minutes")))
                )
        elif kind == "event_msg" and "compact" in str(payload.get("type", "")):
            result.compactions.append(ts)
        elif kind == "response_item" and payload.get("type") in {
            "function_call", "custom_tool_call", "local_shell_call",
        }:
            call_id = payload.get("call_id") or payload.get("id")
            if call_id in tool_ids:
                continue
            tool_ids.add(call_id)
            text = payload.get("arguments") or payload.get("input") or payload.get("action") or ""
            result.tools.append((ts, str(payload.get("name") or ""), str(text)))
    calls = list(by_response.values()) or fallback
    result.calls = sorted(calls, key=lambda call: call.ts)
    return result


def claude_transcript_paths(root: Path, work_dir: str, session_key: str) -> list[Path]:
    """The main transcript plus any native subagent transcripts of one conversation."""
    slug = re.sub(r"[/.]", "-", work_dir)
    main = root / slug / f"{session_key}.jsonl"
    if not main.exists():
        main = next(iter(sorted(root.glob(f"*/{session_key}.jsonl"))), main)
    if not main.exists():
        return []
    return [main, *sorted((main.parent / session_key / "subagents").glob("*.jsonl"))]


def codex_transcript_path(root: Path, session_key: str) -> Path | None:
    return next(iter(sorted(root.glob(f"*/*/*/rollout-*-{session_key}.jsonl"))), None)


def load_transcript(attempt: Mapping[str, Any], *, claude_root: Path, codex_root: Path):
    """Read one attempt's conversation; ``None`` when no transcript is on disk."""
    key = attempt.get("session_key")
    if not key:
        return None
    if attempt.get("harness") == "claude":
        paths = claude_transcript_paths(claude_root, str(attempt.get("work_dir") or ""), key)
        if not paths:
            return None
        with paths[0].open("rb") as stream:
            usage = read_claude(stream)
        for path in paths[1:]:
            with path.open("rb") as stream:
                sub = read_claude(stream, subagent=True)
            usage.calls.extend(sub.calls)
            usage.skipped_lines += sub.skipped_lines
        usage.calls.sort(key=lambda call: call.ts)
        return usage
    if attempt.get("harness") == "codex":
        path = codex_transcript_path(codex_root, key)
        if path is None:
            return None
        with path.open("rb") as stream:
            return read_codex(stream)
    return None


def task_kind(task_id: str, task_type: str | None) -> str:
    if task_type:
        return task_type
    for prefix, kind in (("repair-", "integration-repair"), ("verify-", "verify"),
                         ("rev-", "review")):
        if task_id.startswith(prefix):
            return kind
    return "untyped"


def attempt_spans(attempts: Iterable[Mapping[str, Any]]) -> dict[str, tuple[float, float]]:
    """Each attempt owns its conversation from its start to the next attempt's start.

    The first attempt of a session also owns the conversation's start-up and claim
    loop (from ``session_started_at``), so per-task numbers include the fixed cost
    of a fresh worker.  ``attempts`` must include every attempt of each session,
    not only those in the window, or a later attempt's calls are double-counted.
    """
    by_session: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        by_session[attempt["session_id"]].append(attempt)
    spans: dict[str, tuple[float, float]] = {}
    for rows in by_session.values():
        rows = sorted(rows, key=lambda row: (row["started_at"], row["id"]))
        for index, row in enumerate(rows):
            start = row["started_at"]
            if index == 0:
                start = min(start, row.get("session_started_at") or start)
            end = rows[index + 1]["started_at"] if index + 1 < len(rows) else float("inf")
            spans[row["id"]] = (start, end)
    return spans


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def attempt_metrics(
    attempt: Mapping[str, Any], usage: TranscriptUsage | None, span: tuple[float, float],
    *, now: float, final_outcome: str | None,
) -> dict[str, Any]:
    start, end = span
    row: dict[str, Any] = {
        "attempt_id": attempt["id"],
        "task_id": attempt["task_id"],
        "session_id": attempt["session_id"],
        "harness": attempt.get("harness"),
        "model": attempt.get("model"),
        "intelligence_class": attempt.get("intelligence_class"),
        "profile_id": attempt.get("profile_id"),
        "kind": task_kind(attempt["task_id"], attempt.get("task_type")),
        "outcome": attempt.get("outcome"),
        "final_outcome": final_outcome,
        "end_reason": attempt.get("end_reason"),
        "open": attempt.get("ended_at") is None,
        "duration_s": (attempt.get("ended_at") or now) - attempt["started_at"],
        "transcript": usage is not None,
    }
    row["churn"] = (not row["open"] and row["outcome"] not in {"pass", "fail"}
                    and row["duration_s"] < CHURN_SECONDS)
    if usage is None:
        return row
    calls = [call for call in usage.calls if start <= call.ts < end]
    main = [call for call in calls if not call.subagent]
    tools = [tool for tool in usage.tools if start <= tool[0] < end]
    counts: Counter[str] = Counter()
    for _ts, name, text in tools:
        counts.update(classify_tool(name, text))
    tokens = {key: sum(call.usage[key] for call in calls) for key in CATEGORIES}
    row.update(
        calls=len(main),
        subagent_calls=len(calls) - len(main),
        startup_context=main[0].context if main else None,
        contexts=[call.context for call in main],
        tokens=tokens,
        subagent_tokens={key: sum(c.usage[key] for c in calls if c.subagent) for key in CATEGORIES},
        tool_calls=len(tools),
        commands=dict(counts),
        polls=sum(counts[name] for name in POLLING),
        compactions=sum(1 for ts in usage.compactions if start <= ts < end),
        models=dict(Counter(call.model for call in main if call.model)),
    )
    quota = [sample for sample in usage.quota if start <= sample[0] < end]
    if quota:
        row["quota_used_percent"] = {"first": quota[0][1], "last": quota[-1][1],
                                     "window_minutes": quota[-1][2]}
    return row


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def summarize(rows: list[dict[str, Any]], *, stuck_after_hours: float) -> dict[str, Any]:
    """Distribution summary for one cohort (or the whole window).

    Churn attempts never worked the task, so they are counted but kept out of the
    per-attempt distributions; otherwise a burst of launch failures would read as
    cheaper, faster tasks.
    """
    measured = [row for row in rows if row.get("transcript") and not row["churn"]]
    decided_rows = [row for row in rows if row.get("outcome") in {"pass", "fail"}]
    outcomes = Counter(row.get("outcome") or "none" for row in rows)
    decided = outcomes["pass"] + outcomes["fail"]
    contexts = [value for row in measured for value in row["contexts"]]
    startups = [row["startup_context"] for row in measured if row["startup_context"] is not None]
    tokens = {key: [row["tokens"][key] for row in measured] for key in CATEGORIES}
    commands: Counter[str] = Counter()
    for row in measured:
        commands.update(row["commands"])
    durations = [row["duration_s"] for row in decided_rows]
    return {
        "attempts": len(rows),
        "tasks": len({row["task_id"] for row in rows}),
        "with_transcript": len(measured),
        "outcomes": dict(outcomes),
        "end_reasons": dict(Counter(row.get("end_reason") or "open" for row in rows)),
        "churn": sum(1 for row in rows if row["churn"]),
        "pass_rate": round(outcomes["pass"] / decided, 4) if decided else None,
        "calls_per_attempt": {
            "median": _median([row["calls"] for row in measured]),
            "mean": round(statistics.fmean([row["calls"] for row in measured]), 2)
            if measured else None,
        },
        "subagent_calls": sum(row["subagent_calls"] for row in measured),
        "startup_context": {"median": _median(startups), "p90": _pct(startups, 0.9)},
        "call_context": {"p50": _pct(contexts, 0.5), "p90": _pct(contexts, 0.9),
                         "max": max(contexts) if contexts else None},
        "tokens_total": {key: sum(values) for key, values in tokens.items()},
        "tokens_per_attempt_median": {key: _median(values) for key, values in tokens.items()},
        "commands_per_attempt": {
            key: round(value / len(measured), 2) for key, value in sorted(commands.items())
        } if measured else {},
        "polls_per_attempt": round(sum(row["polls"] for row in measured) / len(measured), 2)
        if measured else None,
        "compactions": sum(row["compactions"] for row in measured),
        "duration_s": {"median": _median(durations), "p90": _pct(durations, 0.9)},
        "long_running": sum(1 for row in decided_rows
                            if row["duration_s"] > stuck_after_hours * 3600),
    }


def ledger_comparison(rows: list[dict[str, Any]],
                      ledger: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    """The production ledger's totals against corrected transcript totals, same attempts.

    Before the Claude streaming fix the ledger charges every content block of a call,
    so its cache reads run near twice the transcript maxima; afterwards the ratio
    should sit near 1.  Attribution of start-up calls differs slightly between the
    two, so read the ratio as a check, not an exact reconciliation.
    """
    both = [row for row in rows if row.get("transcript") and row["attempt_id"] in ledger]
    if not both:
        return None
    recorded = {key: sum(int(ledger[row["attempt_id"]][key]) for row in both)
                for key in CATEGORIES}
    corrected = {key: sum(row["tokens"][key] for row in both) for key in CATEGORIES}
    return {
        "attempts": len(both),
        "ledger": recorded,
        "transcript": corrected,
        "ratio": {key: round(recorded[key] / corrected[key], 3) if corrected[key] else None
                  for key in CATEGORIES},
    }


def cohort_key(row: Mapping[str, Any]) -> str:
    return "|".join(str(row.get(name) or "-") for name in
                    ("harness", "model", "intelligence_class", "kind"))


def route_distribution(routes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    by_provider: Counter[str] = Counter()
    by_profile: Counter[str] = Counter()
    by_kind_provider: Counter[str] = Counter()
    adjusted = 0
    for route in routes:
        provider = route.get("provider") or "unrouted"
        by_provider[provider] += 1
        by_profile[route.get("profile_id") or "none"] += 1
        kind = task_kind(route.get("task_id") or "", route.get("task_type"))
        by_kind_provider[f"{kind}|{provider}"] += 1
        adjusted += bool(route.get("adjusted_at_apply"))
    return {"routes": sum(by_provider.values()), "by_provider": dict(by_provider),
            "by_profile": dict(by_profile), "by_kind_provider": dict(by_kind_provider),
            "adjusted_at_apply": adjusted}


def build_report(
    export: Mapping[str, Any], *, claude_root: Path, codex_root: Path,
    thresholds: Mapping[str, float] | None = None,
    loader: Callable[..., TranscriptUsage | None] = load_transcript,
) -> dict[str, Any]:
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    since, until = float(export["since"]), float(export["until"])
    now = float(export["exported_at"])
    spans = attempt_spans(export["session_attempts"])
    completions: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in export["completions"]:
        completions[record["task_id"]].append(record)
    final = {task: max(records, key=lambda r: r["completed_at"])["outcome"]
             for task, records in completions.items()}
    rows: list[dict[str, Any]] = []
    missing = Counter()
    for attempt in export["attempts"]:
        usage = loader(attempt, claude_root=claude_root, codex_root=codex_root)
        if usage is None:
            missing[attempt.get("harness") or "-"] += 1
        # Transcripts keep growing after the export; stopping at its timestamp keeps
        # a report reproducible from the frozen export and comparable with its ledger.
        start, end = spans[attempt["id"]]
        rows.append(attempt_metrics(attempt, usage, (start, min(end, now)), now=now,
                                    final_outcome=final.get(attempt["task_id"])))
    ledger = {row["attempt_id"]: row for row in export.get("ledger", [])}
    cohorts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    harnesses: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cohorts[cohort_key(row)].append(row)
        harnesses[row.get("harness") or "-"].append(row)
    hours = limits["stuck_after_hours"]
    in_window = [r for r in export["completions"] if since <= r["completed_at"] < until]
    tasks = {row["task_id"]: row["kind"] for row in rows}
    repairs = sum(1 for kind in tasks.values() if kind == "integration-repair")
    quota = [row["quota_used_percent"] for row in rows if "quota_used_percent" in row]
    live, orphaned = [], 0
    for row in export.get("open_attempts", []):
        if row.get("state") in ENDED_STATES or row.get("session_state") in ENDED_STATES:
            orphaned += 1
        elif now - row["started_at"] > hours * 3600:
            live.append({key: row.get(key) for key in (
                "id", "task_id", "harness", "state", "started_at", "session_state",
                "last_activity")})
    return {
        "since": since, "until": until, "exported_at": now,
        "method": __doc__.strip().split("\n\n", 1)[1],
        "thresholds": limits,
        "overall": summarize(rows, stuck_after_hours=hours),
        "by_harness": {key: summarize(value, stuck_after_hours=hours)
                       for key, value in sorted(harnesses.items())},
        "cohorts": {key: summarize(value, stuck_after_hours=hours)
                    for key, value in sorted(cohorts.items())},
        "completions_in_window": dict(Counter(r["outcome"] for r in in_window)),
        "integration_repair_share": round(repairs / len(tasks), 4) if tasks else None,
        "churn_share": round(sum(row["churn"] for row in rows) / len(rows), 4) if rows else None,
        "routes": route_distribution(export["routes"]),
        "ledger_vs_transcript": {key: ledger_comparison(value, ledger)
                                 for key, value in sorted(harnesses.items())},
        "codex_quota_samples": len(quota),
        "codex_quota_used_percent": {
            "min_first": min(q["first"] for q in quota),
            "max_last": max(q["last"] for q in quota),
        } if quota else None,
        "stuck_attempts_now": live,
        "orphaned_open_attempts": orphaned,
        "missing_transcripts": dict(missing),
        "attempts": rows,
    }


def compare(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Matched-cohort deltas and the halt conditions for a staged rollout.

    Only cohorts with at least ``min_attempts`` in both windows are compared;
    everything else is listed as insufficient rather than guessed at.  A halt is
    advisory evidence for the operator, never an automatic rollback.
    """
    limits = after["thresholds"]
    minimum = limits["min_attempts"]
    matched: dict[str, Any] = {}
    insufficient: list[str] = []
    halts: list[str] = []
    for key in sorted(set(before["cohorts"]) | set(after["cohorts"])):
        old, new = before["cohorts"].get(key), after["cohorts"].get(key)
        if not old or not new or min(old["attempts"], new["attempts"]) < minimum:
            insufficient.append(key)
            continue
        delta: dict[str, Any] = {"attempts": [old["attempts"], new["attempts"]]}
        for label, getter in (
            ("pass_rate", lambda s: s["pass_rate"]),
            ("calls_per_attempt", lambda s: s["calls_per_attempt"]["median"]),
            ("startup_context", lambda s: s["startup_context"]["median"]),
            ("call_context_p90", lambda s: s["call_context"]["p90"]),
            ("polls_per_attempt", lambda s: s["polls_per_attempt"]),
            ("duration_s", lambda s: s["duration_s"]["median"]),
            *((f"tokens_{name}", lambda s, n=name: s["tokens_per_attempt_median"][n])
              for name in CATEGORIES),
        ):
            delta[label] = [getter(old), getter(new)]
        matched[key] = delta
        if (old["pass_rate"] is not None and new["pass_rate"] is not None
                and (old["pass_rate"] - new["pass_rate"]) * 100 >= limits["pass_rate_drop_pp"]):
            halts.append(f"{key}: pass rate {old['pass_rate']:.2f} -> {new['pass_rate']:.2f}")
        old_d, new_d = old["duration_s"]["median"], new["duration_s"]["median"]
        if old_d and new_d and (new_d / old_d - 1) * 100 >= limits["latency_increase_pct"]:
            halts.append(f"{key}: median duration {old_d:.0f}s -> {new_d:.0f}s")
    for label, field_name, limit in (
        ("integration repair share of tasks", "integration_repair_share",
         "repair_share_increase_pp"),
        ("no-outcome churn share of attempts", "churn_share", "churn_share_increase_pp"),
    ):
        old_r, new_r = before[field_name], after[field_name]
        if old_r is not None and new_r is not None and (new_r - old_r) * 100 >= limits[limit]:
            halts.append(f"{label} {old_r:.2f} -> {new_r:.2f}")
    if after["stuck_attempts_now"]:
        halts.append(f"{len(after['stuck_attempts_now'])} live attempt(s) open longer than "
                     f"{limits['stuck_after_hours']}h")
    if after["overall"]["long_running"] > before["overall"]["long_running"]:
        halts.append(f"decided attempts longer than {limits['stuck_after_hours']}h "
                     f"{before['overall']['long_running']} -> {after['overall']['long_running']}")
    return {"matched": matched, "insufficient": insufficient, "halt": bool(halts),
            "halt_reasons": halts}


# --- Frozen export --------------------------------------------------------------------

ATTEMPTS_SQL = """
SELECT a.id, a.session_id, a.task_id, a.profile_id, a.harness, a.model,
       a.intelligence_class, a.llm_provider, a.lifecycle, a.state, a.started_at,
       a.session_started_at, a.ended_at, a.end_reason, a.outcome, a.session_key,
       a.work_dir, COALESCE(t.task_type, x.task_type) AS task_type,
       COALESCE(t.status, x.status) AS task_status,
       COALESCE(t.created_at, x.created_at) AS task_created_at
FROM task_session_attempts a
LEFT JOIN tasks t ON t.id = a.task_id
LEFT JOIN archived_tasks x ON x.id = a.task_id
WHERE a.started_at >= $1 AND a.started_at < $2
ORDER BY a.started_at, a.id
"""
SESSION_ATTEMPTS_SQL = """
SELECT id, session_id, started_at, session_started_at
FROM task_session_attempts WHERE session_id = ANY($1::text[])
"""
COMPLETIONS_SQL = """
SELECT id, task_id, outcome, work_outcome, failure_class, completed_at
FROM task_completion_records
WHERE (completed_at >= $1 AND completed_at < $2) OR task_id = ANY($3::text[])
"""
ROUTES_SQL = """
SELECT id AS task_id, task_type, route->>'provider' AS provider,
       route->>'profile_id' AS profile_id, route->>'intelligence_class' AS intelligence_class,
       route->>'rule' AS rule, (route->>'adjusted_at_apply') = 'true' AS adjusted_at_apply,
       (route->>'routed_at')::float AS routed_at
FROM (SELECT id, task_type, route FROM tasks
      UNION ALL SELECT id, task_type, route FROM archived_tasks) routed
WHERE jsonb_typeof(route->'routed_at') = 'number'
  AND (route->>'routed_at')::float >= $1 AND (route->>'routed_at')::float < $2
"""
LEDGER_SQL = """
SELECT attempt_id, count(*) AS rows,
       COALESCE(sum(input_tokens), 0) AS uncached_input,
       COALESCE(sum(cache_read_tokens), 0) AS cache_read,
       COALESCE(sum(cache_write_tokens), 0) AS cache_write,
       COALESCE(sum(output_tokens), 0) AS output
FROM token_ledger WHERE attempt_id = ANY($1::text[]) GROUP BY attempt_id
"""
OPEN_ATTEMPTS_SQL = """
SELECT a.id, a.task_id, a.harness, a.state, a.started_at, s.state AS session_state,
       s.last_activity
FROM task_session_attempts a LEFT JOIN sessions s ON s.id = a.session_id
WHERE a.ended_at IS NULL
"""

Fetch = Callable[..., Awaitable[list[Mapping[str, Any]]]]


async def export_rows(fetch: Fetch, *, since: float, until: float, now: float) -> dict[str, Any]:
    """Collect every row the report needs.  ``fetch`` must run in a READ ONLY transaction."""
    if until <= since:
        raise ValueError("until must be after since")
    attempts = [dict(row) for row in await fetch(ATTEMPTS_SQL, since, until)]
    sessions = sorted({row["session_id"] for row in attempts})
    tasks = sorted({row["task_id"] for row in attempts})
    attempt_ids = [row["id"] for row in attempts]
    return {
        "since": since, "until": until, "exported_at": now,
        "attempts": attempts,
        "session_attempts": [dict(r) for r in await fetch(SESSION_ATTEMPTS_SQL, sessions)],
        "completions": [dict(r) for r in await fetch(COMPLETIONS_SQL, since, until, tasks)],
        "routes": [dict(r) for r in await fetch(ROUTES_SQL, since, until)],
        "ledger": [dict(r) for r in await fetch(LEDGER_SQL, attempt_ids)],
        "open_attempts": [dict(r) for r in await fetch(OPEN_ATTEMPTS_SQL)],
    }
