"""Build a paired benchmark report without inventing missing attribution."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from fnmatch import fnmatchcase
import hashlib
import json

STAGES = (
    "author_edit",
    "local_validation",
    "bake",
    "readiness",
    "capture",
    "scoring",
    "queue_wait",
    "human_review",
)


def _priced_row(row: dict, pricing) -> dict:
    model = row.get("model")
    rate = pricing.match(model) if model else None
    components = {
        "input": int(row.get("input_tokens") or 0),
        "output": int(row.get("output_tokens") or 0),
        "cache_read": int(row.get("cache_read_tokens") or 0),
        "cache_write": int(row.get("cache_write_tokens") or 0),
    }
    total = int(row.get("tokens_used") or 0)
    known = sum(components.values())
    rates = (
        {
            "input": rate.input_per_mtok,
            "output": rate.output_per_mtok,
            "cache_read": rate.cache_read_per_mtok,
            "cache_write": rate.cache_write_per_mtok,
        }
        if rate
        else {}
    )
    priced = sum(count for key, count in components.items() if rates.get(key) is not None)
    cost = sum(
        count * rates[key] / 1_000_000
        for key, count in components.items()
        if rates.get(key) is not None
    )
    return {
        "ledger_id": row["id"],
        "task_id": row["task_id"],
        "attempt_id": row.get("attempt_id"),
        "call_id": row.get("call_id"),
        "session_id": row.get("session_id"),
        "model_observed": model,
        "model_source": row.get("model_source"),
        "timestamp": row["timestamp"],
        "tokens": components,
        "tokens_used": total,
        "unattributed_tokens": max(0, total - known),
        "unpriced_tokens": max(0, total - priced),
        "unpriced_cache_read_tokens": (
            components["cache_read"] if rates.get("cache_read") is None else 0
        ),
        "unpriced_cache_write_tokens": (
            components["cache_write"] if rates.get("cache_write") is None else 0
        ),
        "estimated_api_cost_usd": round(cost, 9) if priced else None,
        "cost_complete": priced == total,
    }


def _manifest_pairs(manifest: dict) -> tuple[list[dict], list[str]]:
    if manifest.get("version") != 1:
        raise ValueError("benchmark manifest version must be 1")
    if not manifest.get("project_id") or not manifest.get("policy_sha256"):
        raise ValueError("manifest needs project_id and frozen policy_sha256")
    if not manifest.get("rate_card_version"):
        raise ValueError("manifest needs a rate_card_version")
    arms = manifest.get("arms")
    pairs = manifest.get("pairs")
    if not isinstance(arms, dict) or not arms or not isinstance(pairs, list) or not pairs:
        raise ValueError("manifest needs arms and a nonempty pairs list")
    task_ids: list[str] = []
    keys: set[tuple[str, str, str]] = set()
    for arm, spec in arms.items():
        if not arm or not isinstance(spec, dict) or not spec.get("requested_model"):
            raise ValueError(f"arm {arm!r} needs a requested_model")
        models = spec.get("observed_models")
        if (
            not isinstance(models, list)
            or not models
            or any(not isinstance(model, str) or not model for model in models)
            or not isinstance(spec.get("class"), str)
            or not spec["class"]
            or not isinstance(spec.get("harness"), str)
            or not spec["harness"]
        ):
            raise ValueError(f"arm {arm!r} needs observed_models, class and harness")
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("arm") not in arms:
            raise ValueError("every pair must name an allowlisted arm")
        key = (str(pair.get("specimen") or ""), str(pair["arm"]), str(pair.get("attempt") or ""))
        if not all(key) or key in keys:
            raise ValueError("pairs need distinct specimen/arm/attempt keys")
        keys.add(key)
        ids = pair.get("task_ids")
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(t, str) or not t for t in ids)
        ):
            raise ValueError("each pair needs explicit task_ids")
        task_ids.extend(ids)
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("a task_id belongs to exactly one paired attempt")
    return pairs, task_ids


def build_report(manifest: dict, evidence: dict, pricing) -> dict:
    """Return every declared pair, every session retry and every ledger row.

    The manifest is the frozen comparison set. Missing model, attempt and
    stage information stays unknown; it is never inferred from time overlap.
    """
    pairs, _task_ids = _manifest_pairs(manifest)
    task_rows = evidence["tasks"]
    sessions_by_task: dict[str, list[dict]] = defaultdict(list)
    for row in evidence["attempts"]:
        sessions_by_task[row["task_id"]].append(row)
    ledger_by_task: dict[str, list[dict]] = defaultdict(list)
    for row in evidence["ledger"]:
        ledger_by_task[row["task_id"]].append(row)
    stages_by_task: dict[str, list[dict]] = defaultdict(list)
    for row in evidence.get("stage_spans", []):
        stages_by_task[row["task_id"]].append(row)

    exported = []
    for pair in pairs:
        arm_name = pair["arm"]
        arm = manifest["arms"][arm_name]
        task_ids = pair["task_ids"]
        task_info = []
        session_info = []
        usage = []
        mismatches = []
        unknowns = []
        for task_id in task_ids:
            task = task_rows[task_id]
            route = task.get("route") or {}
            task_info.append(
                {
                    "task_id": task_id,
                    "status": task["status"],
                    "requested_arm": route.get("benchmark_arm"),
                    "requested_model": route.get("requested_model"),
                    "policy_sha256": route.get("policy_sha256"),
                }
            )
            if not route or not route.get("benchmark_arm"):
                unknowns.append(f"{task_id}: route unknown")
            elif (
                route.get("benchmark_arm") != arm_name
                or route.get("requested_model") != arm["requested_model"]
                or route.get("benchmark_class") != arm["class"]
                or route.get("benchmark_harness") != arm["harness"]
                or route.get("observed_models") != arm["observed_models"]
                or route.get("policy_sha256") != manifest["policy_sha256"]
            ):
                mismatches.append(f"{task_id}: requested route differs from frozen manifest")
            for session in sessions_by_task[task_id]:
                session_info.append(
                    {
                        "id": session["id"],
                        "task_id": task_id,
                        "profile_id": session["profile_id"],
                        "class": session.get("intelligence_class"),
                        "harness": session["harness"],
                        "provider": session.get("llm_provider"),
                        "model_configured": session.get("model"),
                        "started_at": session["started_at"],
                        "ended_at": session.get("ended_at"),
                        "wall_seconds": (
                            max(0, session["ended_at"] - session["started_at"])
                            if session.get("ended_at") is not None
                            else None
                        ),
                        "outcome": session.get("outcome"),
                        "end_reason": session.get("end_reason"),
                    }
                )
                if (
                    session.get("intelligence_class") != arm["class"]
                    or session["harness"] != arm["harness"]
                ):
                    mismatches.append(
                        f"{task_id}: session {session['id']} routed to another class/harness"
                    )
                configured = session.get("model")
                if configured is None:
                    unknowns.append(f"{task_id}: session {session['id']} has no configured model")
                elif configured != arm["requested_model"]:
                    mismatches.append(
                        f"{task_id}: session {session['id']} configured {configured} "
                        "instead of requested model"
                    )
            for raw in ledger_by_task[task_id]:
                priced = _priced_row(raw, pricing)
                usage.append(priced)
                observed = priced["model_observed"]
                if observed is None:
                    unknowns.append(f"{task_id}: ledger {raw['id']} has no observed model")
                elif not any(fnmatchcase(observed, glob) for glob in arm["observed_models"]):
                    mismatches.append(f"{task_id}: observed {observed} is outside arm {arm_name}")
                if priced["model_source"] not in {"assistant_response", "provider_result"}:
                    unknowns.append(
                        f"{task_id}: ledger {raw['id']} lacks provider response model evidence"
                    )
                if not raw.get("attempt_id") or not raw.get("call_id"):
                    unknowns.append(f"{task_id}: ledger {raw['id']} lacks stable attempt/call ID")

        if not session_info:
            unknowns.append("no session attempt recorded")
        if not usage:
            unknowns.append("no token usage recorded")
        attempt_ids = {session["id"] for session in session_info}
        for priced in usage:
            if priced["attempt_id"] and priced["attempt_id"] not in attempt_ids:
                unknowns.append(f"ledger {priced['ledger_id']}: attempt ID not in task set")
        observed_calls = pair.get("observed_calls") or []
        if not isinstance(observed_calls, list):
            raise ValueError("observed_calls must be a list")
        if arm["harness"] == "opencode" and not observed_calls:
            unknowns.append("OpenCode underlying provider/model not observed")
        for call in observed_calls:
            if (
                not isinstance(call, dict)
                or not all(
                    call.get(key)
                    for key in (
                        "task_id",
                        "session_attempt_id",
                        "message_id",
                        "provider_id",
                        "model_id",
                        "source_sha256",
                    )
                )
                or call.get("source") != "opencode_export"
            ):
                raise ValueError("observed_calls need an OpenCode export identity and hash")
            if (
                arm["harness"] != "opencode"
                or call["task_id"] not in task_ids
                or call["session_attempt_id"] not in attempt_ids
            ):
                mismatches.append("OpenCode observation does not belong to this arm/attempt")
            observed = call["model_id"]
            qualified = f"{call['provider_id']}/{observed}"
            if not any(
                fnmatchcase(value, glob)
                for glob in arm["observed_models"]
                for value in (observed, qualified)
            ):
                mismatches.append(f"OpenCode observed {qualified} is outside arm {arm_name}")
        failed = any(t["status"] in {"FAILED", "BLOCKED"} for t in task_info) or any(
            s["outcome"] == "fail" for s in session_info
        )
        status = (
            "failed"
            if failed
            else (
                "completed" if all(t["status"] == "COMPLETED" for t in task_info) else "incomplete"
            )
        )
        spans = list(pair.get("stage_spans") or [])
        if not isinstance(spans, list) or any(
            not isinstance(s, dict)
            or s.get("stage") not in STAGES
            or not isinstance(s.get("duration_ms"), (int, float))
            or s["duration_ms"] < 0
            or s.get("clock") != "monotonic"
            for s in spans
        ):
            raise ValueError("stage_spans require a known stage, monotonic clock and duration_ms")
        for task_id in task_ids:
            for stage in stages_by_task[task_id]:
                spans.append(
                    {
                        "id": stage["id"],
                        "task_id": task_id,
                        "session_attempt_id": stage["session_attempt_id"],
                        "stage": stage["stage"],
                        "clock": "monotonic",
                        "started_monotonic_ns": stage["started_monotonic_ns"],
                        "ended_monotonic_ns": stage["ended_monotonic_ns"],
                        "duration_ms": stage["duration_ms"],
                    }
                )
        covered = {s["stage"] for s in spans}
        provider_charges = pair.get("provider_charges_usd") or {}
        if not isinstance(provider_charges, dict) or any(
            provider_charges.get(kind) is not None
            and (not isinstance(provider_charges[kind], (int, float)) or provider_charges[kind] < 0)
            for kind in ("image", "tool", "retry")
        ):
            raise ValueError("provider_charges_usd must contain nonnegative amounts or null")
        unknown_charges = [
            kind for kind in ("image", "tool", "retry") if provider_charges.get(kind) is None
        ]
        token_cost = sum(r["estimated_api_cost_usd"] or 0 for r in usage)
        other_cost = sum(provider_charges.get(kind) or 0 for kind in ("image", "tool", "retry"))
        token_totals = {
            key: sum(row["tokens"].get(key, 0) for row in usage)
            for key in ("input", "output", "cache_read", "cache_write")
        }
        token_totals["total"] = sum(row["tokens_used"] for row in usage)
        exported.append(
            {
                "specimen": pair["specimen"],
                "arm": arm_name,
                "attempt": pair["attempt"],
                "status": status,
                "tasks": task_info,
                "session_attempts": session_info,
                "ledger": usage,
                "token_totals": token_totals,
                "observed_calls": observed_calls,
                "stage_spans": spans,
                "missing_stage_spans": sorted(set(STAGES) - covered),
                "estimated_api_cost_usd": round(token_cost + other_cost, 9),
                "provider_charges_usd": provider_charges,
                "unknown_provider_charges": unknown_charges,
                "subscription_cash_cost_usd": None,
                "unpriced_tokens": sum(r["unpriced_tokens"] for r in usage),
                "cost_complete": bool(usage)
                and not unknown_charges
                and all(r["cost_complete"] for r in usage),
                "route_check": "contaminated"
                if mismatches
                else "unknown"
                if unknowns
                else "matched",
                "mismatches": mismatches,
                "unknowns": unknowns,
            }
        )
    return {
        "project_id": manifest["project_id"],
        "policy_sha256": manifest["policy_sha256"],
        "rate_card_version": manifest["rate_card_version"],
        "rate_card_sha256": "sha256:"
        + hashlib.sha256(
            json.dumps(
                [asdict(row) for row in pricing.models],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "pairs": exported,
        "attempts_total": len(exported),
        "attempts_failed": sum(row["status"] == "failed" for row in exported),
        "cost_complete": all(row["cost_complete"] for row in exported),
        "estimated_api_cost_usd": round(sum(row["estimated_api_cost_usd"] for row in exported), 9),
        "unpriced_tokens": sum(row["unpriced_tokens"] for row in exported),
        "tokens_total": sum(row["token_totals"]["total"] for row in exported),
    }


__all__ = ["build_report", "_manifest_pairs", "STAGES"]
