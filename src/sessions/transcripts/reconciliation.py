"""Read-only Claude transcript/ledger reconciliation and compensation proposals."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from src.sessions.transcripts.base import parse_iso_ts, transcript_usage_key

COUNTERS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
}


def evidence_hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def reconcile_claude_usage(
    paths: list[Path], ledger: list[dict], *, since: float, until: float,
) -> dict:
    """Propose adjustments only for fully covered, exact UUID/counter matches.

    No connection or mutation capability is accepted. Each proposal includes
    immutable originals, a deterministic idempotency key, and an inverse.
    Ambiguity, incomplete coverage and counter disagreement fail closed.
    """
    if until <= since:
        raise ValueError("until must be after since")
    calls: dict[str, dict] = {}
    by_uuid: dict[str, set[str]] = defaultdict(set)
    source_hashes: dict[str, list[str]] = defaultdict(list)
    outside_window_calls: set[str] = set()
    skipped_lines = 0
    for path in sorted(set(paths)):
        with path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    skipped_lines += 1
                    continue
                try:
                    raw = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    skipped_lines += 1
                    continue
                if not isinstance(raw, dict):
                    continue
                message = raw.get("message")
                if not isinstance(message, dict) or raw.get("type") != "assistant":
                    continue
                usage = message.get("usage")
                content_id = raw.get("uuid")
                api_id = message.get("id")
                if not isinstance(usage, dict) or not usage or not content_id or not api_id:
                    continue
                counts = {key: int(usage.get(field) or 0) for key, field in COUNTERS.items()}
                if any(value < 0 for value in counts.values()):
                    skipped_lines += 1
                    continue
                key = transcript_usage_key("claude", str(path), str(api_id))
                if not since <= parse_iso_ts(raw.get("timestamp")) <= until:
                    outside_window_calls.add(key)
                    continue
                call = calls.setdefault(key, {
                    "usage_key": key, "transcript_path": str(path), "api_message_id": api_id,
                    "observations": defaultdict(list), "target": dict.fromkeys(COUNTERS, 0),
                })
                call["observations"][str(content_id)].append(counts)
                for field in COUNTERS:
                    call["target"][field] = max(call["target"][field], counts[field])
                by_uuid[str(content_id)].add(key)
                source_hashes[str(path)].append(hashlib.sha256(line).hexdigest())

    matched: dict[str, list[dict]] = defaultdict(list)
    unmatched: list[str] = []
    invalid: dict[str, set[str]] = defaultdict(set)
    seen_rows: set[str] = set()
    for row in ledger:
        if row["id"] in seen_rows:
            raise ValueError(f"duplicate ledger row ID: {row['id']}")
        seen_rows.add(row["id"])
        if not since <= float(row["timestamp"]) <= until:
            raise ValueError("ledger export contains rows outside the frozen window")
        if not str(row.get("model") or "").startswith("claude"):
            continue
        keys = by_uuid.get(row.get("call_id"), set())
        if len(keys) != 1:
            unmatched.append(row["id"])
            for key in keys:
                invalid[key].add("ambiguous_content_uuid")
            continue
        key = next(iter(keys))
        matched[key].append(row)
        counters = {field: row.get(field) for field in COUNTERS}
        if counters not in calls[key]["observations"][row["call_id"]]:
            invalid[key].add("counter_disagreement")
        if any(value is None for value in counters.values()) or sum(
            value or 0 for value in counters.values()
        ) != row["tokens_used"]:
            invalid[key].add("incomplete_ledger_split")

    totals = {name: dict.fromkeys([*COUNTERS, "tokens_used"], 0)
              for name in ("matched_recorded", "covered_call_target", "proposed_reduction")}
    proposals = []
    excluded = []
    covered_calls = 0
    for key, rows in sorted(matched.items()):
        call = calls[key]
        if key in outside_window_calls:
            invalid[key].add("call_spans_window_boundary")
        if set(call["observations"]) - {row["call_id"] for row in rows}:
            invalid[key].add("incomplete_call_coverage")
        if invalid[key]:
            excluded.append({"usage_key": key, "reasons": sorted(invalid[key])})
            continue
        covered_calls += 1
        actual = {field: sum(row[field] for row in rows) for field in COUNTERS}
        actual["tokens_used"] = sum(row["tokens_used"] for row in rows)
        target = dict(call["target"])
        target["tokens_used"] = sum(target.values())
        reduction = {field: actual[field] - target[field] for field in actual}
        if any(value < 0 for value in reduction.values()):
            excluded.append({"usage_key": key, "reasons": ["target_exceeds_recorded"]})
            covered_calls -= 1
            continue
        for name, counts in (("matched_recorded", actual), ("covered_call_target", target),
                             ("proposed_reduction", reduction)):
            for field, value in counts.items():
                totals[name][field] += value
        if not reduction["tokens_used"]:
            continue
        originals = [{"row": row, "sha256": evidence_hash(row)}
                     for row in sorted(rows, key=lambda row: row["id"])]
        correction_id = "claude-usage-v1:" + evidence_hash({
            "usage_key": key, "originals": originals, "target": target,
        })
        remaining = dict(target)
        adjustments = []
        for row in sorted(rows, key=lambda row: (row["timestamp"], row["id"])):
            removed = {}
            for field in COUNTERS:
                retained = min(row[field], remaining[field])
                remaining[field] -= retained
                removed[field] = row[field] - retained
            removed["tokens_used"] = sum(removed.values())
            if removed["tokens_used"]:
                adjustments.append({
                    "entry_id": correction_id + ":" + evidence_hash(row),
                    "original_ledger_id": row["id"],
                    "attribution": {field: row.get(field) for field in (
                        "project_id", "agent_id", "task_id", "session_id", "attempt_id",
                        "model", "model_source",
                    )},
                    "adjustment": {field: -value for field, value in removed.items()},
                    "reversal": removed,
                })
        proposals.append({
            "correction_id": correction_id, "usage_key": key,
            "transcript_path": call["transcript_path"],
            "api_message_id": call["api_message_id"], "originals": originals,
            "target": target, "adjustment": {field: -value for field, value in reduction.items()},
            "reversal": reduction, "attributed_adjustments": adjustments,
        })
    return {
        "schema_version": 1, "mode": "dry_run", "since": since, "until": until,
        "transcript_calls": len(calls), "matched_calls": len(matched),
        "matched_ledger_rows": sum(map(len, matched.values())), "covered_calls": covered_calls,
        "unmatched_claude_row_ids": unmatched, "excluded_calls": excluded,
        "skipped_lines": skipped_lines, "totals": totals, "proposals": proposals,
        "transcript_window_sha256": {path: evidence_hash(hashes)
                                     for path, hashes in sorted(source_hashes.items())},
        "apply_requirements": [
            "Apply through a separate CommandHandler command; this report cannot write a ledger.",
            "Atomically check every original row hash and uniqueness of correction_id.",
            "Append signed adjustments per original attribution, retaining all original rows.",
            "Reverse by appending the recorded inverse under a unique reversal ID.",
        ],
        "limitations": [
            "Only local supplied transcripts and the frozen ledger window are reconciled.",
            "Calls outside the window or without exact UUID and counter evidence are excluded.",
            "Counter maxima measure observed usage; token sums are not subscription quota percentages.",
        ],
    }
