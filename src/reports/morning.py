"""Read-only morning evidence collection, independent of schedule/delivery policy."""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.reports.authoring import surface_map
from src.reports.git import read_git_evidence
from src.reports.hourly import MAX_BRIEF_BYTES, hash_brief

REPLAY_SECONDS = 72 * 3600


def preview_until(now: float, timezone: str, clock: str = "07:00") -> float:
    """The latest configured daily boundary, using the same DST rules as ticks."""
    from src.reports.schedule import planned_at

    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    planned = planned_at(local.date(), clock, timezone)
    if planned > now:
        planned = planned_at(local.date() - timedelta(days=1), clock, timezone)
    return planned


def report_window(since: float | None, until: float, max_lookback_hours: int = 72) -> dict:
    if not math.isfinite(until) or not 1 <= max_lookback_hours <= 72:
        raise ValueError("until must be finite; max_lookback_hours must be 1–72")
    requested = until - 24 * 3600 if since is None else since
    if not math.isfinite(requested) or requested >= until:
        raise ValueError("since must be finite and before until")
    start = max(requested, until - max_lookback_hours * 3600)
    return {
        "since": start,
        "until": until,
        "omitted_interval": {"since": requested, "until": start} if requested < start else None,
        "replay_since": min(start, until - REPLAY_SECONDS),
        "late_arrivals_supported_hours": 72,
    }


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value if value is not None else default


def _commits(row: dict) -> list[str]:
    value = _json(row.get("commits"), [])
    return (
        [item for item in value if isinstance(item, str)][:200] if isinstance(value, list) else []
    )


def _tests(row: dict) -> list[str]:
    value = _json(row.get("tests"), [])
    return (
        [item[:500] for item in value if isinstance(item, str)][:20]
        if isinstance(value, list)
        else []
    )


def _repository(project: dict, sources: dict) -> tuple[tuple[str, ...], str, str] | None:
    """Checkout paths to try, default branch and repository id for *project*.

    The configured repository's own path comes first. Development clone rows
    carry no path and a linked source can move, so the project's one
    project-repo base workspace, which its worktrees come from, follows.
    """
    bases = [
        row["workspace_path"]
        for row in sources.get("workspaces", [])
        if row["project_id"] == project["id"]
        and (row.get("kind_id") or "project-repo") == "project-repo"
    ]
    base = tuple(bases) if len(bases) == 1 else ()
    candidates = [row for row in sources.get("repos", []) if row["project_id"] == project["id"]]
    selected_id = project.get("integration_repository_id")
    if selected_id:
        candidates = [row for row in candidates if row["id"] == selected_id]
    elif len(candidates) > 1:
        candidates = [row for row in candidates if row["url"] == project.get("repo_url")]
    if len(candidates) == 1:
        repo = candidates[0]
        path = repo["source_path"] if repo["source_type"] == "link" else repo["checkout_base_path"]
        paths = tuple(dict.fromkeys(item for item in (path, *base) if item))
        return (paths, repo["default_branch"], repo["id"]) if paths else None
    if candidates:
        return None
    if base:
        return base, project.get("repo_default_branch") or "main", ""
    return None


async def _checkout(paths: tuple[str, ...]) -> str:
    """The first existing directory, else the configured path to report on."""
    for path in paths:
        if await asyncio.to_thread(os.path.isdir, path):
            return path
    return paths[0]


_GROUPS = ("landed", "pending", "failures")
# Per-commit history and session telemetry: the first evidence to omit.
_TELEMETRY = frozenset({"git", "attempt", "reroute", "provider"})
# Everything a git fact or a fact's shipment already carries.
_GIT_DETAIL = frozenset({"commits", "files", "diffstat", "ancestry"})
_COMPACT_TEXT = 160
_COMPACT_ITEMS = 3


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def _compact(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_COMPACT_TEXT]
    if isinstance(value, list):
        return [_compact(item) for item in value[:_COMPACT_ITEMS]]
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items()}
    return value


def _keep_order(facts: list[dict]) -> list[dict]:
    """Facts most worth keeping first.

    In-window evidence leads, late evidence follows and per-commit history
    and session telemetry come last. Within each band projects take turns, so
    one busy project cannot crowd the others out, and each project offers its
    landed work and unresolved problems before the rest, newest first.
    """

    def band(fact: dict) -> tuple[bool, bool]:
        return fact["source"] in _TELEMETRY, fact["late"]

    def lesser(fact: dict) -> bool:
        detail = fact["detail"]
        return not (detail.get("unresolved") or detail.get("shipment") == "landed")

    ranked = sorted(facts, key=lambda fact: (band(fact), lesser(fact), -fact["at"], fact["key"]))
    turns: Counter = Counter()
    order = []
    for fact in ranked:
        project = (band(fact), fact["project_id"])
        turns[project] += 1
        order.append((band(fact), turns[project]))
    # A stable sort keeps each turn in ranked order.
    return [fact for _, fact in sorted(zip(order, ranked), key=lambda pair: pair[0])]


def _fit_brief(brief: dict) -> None:
    """Bound *brief* to MAX_BRIEF_BYTES, dropping context before evidence.

    Hourly digest context is context only, and the raw git reads repeat what
    the facts carry: each commit is a git fact, ancestry is each fact's
    shipment and the surface map is already drawn from the diffstat. Both go
    before any fact (2026-09-28: a 48 KB git read and 8 KB of digests pushed
    all 984 facts out and left a no-facts brief). Fact prose is shortened
    next, and only then are facts omitted, least important first, with
    per-project counts. A limit is incomplete coverage, never an apparent
    no-change day, so nothing it proposes advances coverage.
    """
    if _size(brief) <= MAX_BRIEF_BYTES:
        return
    brief["coverage"]["gaps"].append({"source": "brief", "reason": "byte_limit"})
    brief["coverage"]["complete"] = False
    brief["source_cursors"] = {}
    brief["source_heads"] = {}
    brief["omitted"]["digest_context"] = len(brief["digest_context"])
    brief["digest_context"] = []
    brief["git"] = {
        project_id: {
            **{key: value for key, value in observed.items() if key not in _GIT_DETAIL},
            "commit_count": len(observed.get("commits", [])),
        }
        for project_id, observed in brief["git"].items()
    }
    facts = [{**fact, "detail": _compact(fact["detail"])} for fact in brief["facts"]]
    members = {
        project["id"]: {group: project[group] for group in _GROUPS} for project in brief["projects"]
    }
    # Each list element costs its encoding plus a ", " separator.
    refs: dict[str, int] = {}
    for groups in members.values():
        for keys in groups.values():
            for key in keys:
                refs[key] = refs.get(key, 0) + _size(key) + 2

    def select(kept: list[dict]) -> None:
        keys = {fact["key"] for fact in kept}
        brief["facts"] = sorted(kept, key=lambda fact: (fact["at"], fact["key"]))
        for project in brief["projects"]:
            for group in _GROUPS:
                project[group] = [key for key in members[project["id"]][group] if key in keys]
        omitted = Counter(fact["project_id"] for fact in facts if fact["key"] not in keys)
        brief["omitted"]["facts"] = omitted.total()
        brief["omitted"]["projects"] = {
            project_id: count for project_id, count in omitted.items() if project_id is not None
        }

    select([])
    while _size(brief) > MAX_BRIEF_BYTES and brief["git"]:
        brief["git"].pop(next(reversed(brief["git"])))
    # Strict priority: a fact is omitted only when everything kept outranks
    # it. Omission counts only shrink as facts are kept, so the budget holds.
    budget = MAX_BRIEF_BYTES - _size(brief)
    kept: list[dict] = []
    for fact in _keep_order(facts):
        budget -= _size(fact) + 2 + refs.get(fact["key"], 0)
        if budget < 0:
            break
        kept.append(fact)
    select(kept)
    while _size(brief) > MAX_BRIEF_BYTES:
        if not kept:
            raise ValueError("morning brief metadata exceeds 24 KiB")
        kept.pop()
        select(kept)


async def collect_morning_evidence(
    db: Any,
    git: Any,
    *,
    window: dict,
    now: float,
    project_ids: tuple[str, ...] | None = None,
    previous_heads: dict[str, str] | None = None,
    reported_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Build a frozen brief; future schedule code owns storing heads/membership.

    ``reported_keys`` are morning membership, never hourly digest keys. Replay
    facts before the labelled window are late, and unsuccessful reads have no
    proposed cursor. No model, reservation, message, transport, or file write.
    """
    snapshot = await db.collect_morning_report_sources(
        since=window["replay_since"], until=window["until"], project_ids=project_ids
    )
    sources = snapshot["sources"]
    gaps = list(snapshot["gaps"])
    if window["omitted_interval"]:
        gaps.append({"source": "window", "reason": "lookback_capped"})
    project_rows = snapshot["projects"]
    allowed = {project["id"] for project in project_rows}
    facts: list[dict] = []
    git_reads: dict[str, dict] = {}
    for project in project_rows:
        project_id = project["id"]
        repo = _repository(project, sources)
        if repo is None:
            git_reads[project_id] = {
                "head": None,
                "ancestry": {},
                "commits": [],
                "files": [],
                "gaps": ["configured_repository_unavailable"],
                "warnings": [],
            }
        else:
            paths, branch, repository_id = repo
            path = await _checkout(paths)
            proof_commits = {
                commit
                for row in sources.get("completions", [])
                if row["project_id"] == project_id
                for commit in _commits(row)
            }
            proof_commits.update(
                row.get("prepared_sha")
                for row in sources.get("deliveries", [])
                if row["project_id"] == project_id
                and row.get("prepared_sha")
                and (not repository_id or row["repository_id"] == repository_id)
            )
            git_reads[project_id] = await read_git_evidence(
                git,
                checkout=path,
                default_branch=branch,
                since=window["since"],
                until=window["until"],
                now=now,
                previous_head=(previous_heads or {}).get(project_id),
                proof_commits=tuple(sorted(proof_commits)),
            )
            git_reads[project_id]["repository_id"] = repository_id
        gaps.extend(
            {"source": f"git:{project_id}", "reason": gap} for gap in git_reads[project_id]["gaps"]
        )

    def add(source: str, row: dict, *, at: float, detail: dict, record_id: str | None = None):
        project_id = row.get("project_id")
        if project_id is not None and project_id not in allowed:
            return
        key = f"{source}:{record_id or row['id']}"
        unresolved = bool(detail.get("unresolved"))
        if key in reported_keys and not unresolved:
            return
        if at < window["replay_since"] and not unresolved and source != "git":
            return
        facts.append(
            {
                "key": key,
                "source": source,
                "record_id": str(record_id or row["id"]),
                "project_id": project_id,
                "task_id": row.get("task_id"),
                "at": at,
                "late": at < window["since"] and source != "git",
                "detail": detail,
            }
        )

    for row in sources.get("completions", []):
        recorded_commits = _json(row.get("commits"), None)
        complete_proof = (
            isinstance(recorded_commits, list)
            and len(recorded_commits) <= 200
            and all(isinstance(commit, str) for commit in recorded_commits)
        )
        if not complete_proof:
            gaps.append({"source": "completions", "reason": "invalid_or_truncated_commits"})
        proofs = [
            git_reads.get(row["project_id"], {}).get("ancestry", {}).get(sha, "unknown")
            for sha in _commits(row)
        ]
        shipment = (
            "landed"
            if complete_proof and proofs and all(p == "landed" for p in proofs)
            else ("pending" if "pending" in proofs or not proofs else "unknown")
        )
        add(
            "completion",
            row,
            at=float(row["completed_at"]),
            detail={
                "title": row.get("title", "")[:160],
                "outcome": row["outcome"],
                "changes": row.get("changes", "")[:1500],
                "summary": row.get("summary", "")[:1500],
                "verification": row.get("verification", "")[:1000],
                "tests": _tests(row),
                "verification_label": "agent-reported",
                "commits": _commits(row),
                "pr_url": row.get("pr_url"),
                "shipment": shipment,
            },
        )
    for row in sources.get("deliveries", []):
        observed = git_reads.get(row["project_id"], {})
        proof = observed.get("ancestry", {}).get(row.get("prepared_sha"), "unknown")
        repo_matches = observed.get("repository_id") == row.get("repository_id")
        target_matches = row["target_ref"] in (
            observed.get("ref"),
            str(observed.get("ref") or "").removeprefix("refs/remotes/origin/"),
            str(observed.get("ref") or "").removeprefix("refs/heads/"),
            "refs/heads/" + str(observed.get("ref") or "").removeprefix("refs/remotes/origin/"),
        )
        # Git alone decides landing; the operation state is the action's outcome.
        landed = proof == "landed" and repo_matches and target_matches
        add(
            "delivery",
            row,
            at=float(row["updated_at"]),
            detail={
                "state": row["state"],
                "target_ref": row["target_ref"],
                "head": row.get("prepared_sha"),
                "shipment": "landed" if landed else "pending" if proof == "pending" else "unknown",
                "manifest": row.get("manifest", {}),
            },
        )
    for row in sources.get("incidents", []):
        incident = _json(row.get("value"), {})
        if not isinstance(incident, dict) or not incident.get("id"):
            gaps.append({"source": "incidents", "reason": "invalid_record"})
            continue
        add(
            "incident",
            row,
            at=float(row["updated_at"]),
            record_id=incident["id"],
            detail={
                "reason": str(incident.get("reason") or "")[:500],
                "status": row["status"],
                "unresolved": row["status"] in ("FAILED", "BLOCKED"),
            },
        )
    for row in sources.get("escalations", []):
        add(
            "escalation",
            row,
            at=float(row["updated_at"]),
            detail={
                "state": row["state"],
                "severity": row["severity"],
                "summary": row["summary"][:1000],
                "unresolved": row["state"] in ("needs_human", "reply_received", "resolving"),
            },
        )
    for row in sources.get("attempts", []):
        add(
            "attempt",
            row,
            at=float(row["ended_at"]),
            detail={
                "outcome": row.get("outcome"),
                "end_reason": row.get("end_reason"),
                "state": row["state"],
            },
        )
    for row in sources.get("providers", []):
        add(
            "provider",
            row,
            at=float(row["at"]),
            detail={
                "provider": row["provider"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "reason_code": row["reason_code"],
            },
        )
    for row in sources.get("reroutes", []):
        add(
            "reroute",
            row,
            at=float(row["at"]),
            detail={
                "from_provider": row["from_provider"],
                "to_provider": row["to_provider"],
                "reason_code": row["reason_code"],
            },
        )
    for project_id, observed in git_reads.items():
        for commit in observed["commits"]:
            if commit["at"] >= window["until"]:
                gaps.append({"source": f"git:{project_id}", "reason": "commits_after_window"})
                continue
            add(
                "git",
                {"id": commit["sha"], "project_id": project_id},
                at=commit["at"],
                detail={
                    **commit,
                    "shipment": "landed",
                    "time_basis": "range_observation" if previous_heads else "committer_time",
                },
            )

    facts.sort(key=lambda fact: (fact["at"], fact["key"]))
    project_briefs = []
    for row in project_rows:
        local_facts = [fact for fact in facts if fact["project_id"] == row["id"]]
        project_briefs.append(
            {
                "id": row["id"],
                "name": row["name"],
                "landed": [
                    fact["key"]
                    for fact in local_facts
                    if fact["detail"].get("shipment") == "landed"
                ],
                "pending": [
                    fact["key"]
                    for fact in local_facts
                    if fact["detail"].get("shipment") in ("pending", "unknown")
                ],
                "failures": [
                    fact["key"]
                    for fact in local_facts
                    if fact["source"] in ("incident", "escalation", "attempt")
                    or fact["detail"].get("outcome") == "fail"
                ],
            }
        )
    brief = {
        "version": 1,
        "kind": "morning",
        "window": window,
        "facts": facts,
        "projects": project_briefs,
        "git": git_reads,
        "surface_map": surface_map(git_reads),
        "digest_context": [
            {"id": row["id"], "send_status": row["send_status"], "window_end": row["window_end"]}
            for row in sources.get("digests", [])
        ],
        "coverage": {
            "complete": not gaps,
            "gaps": gaps,
            "excluded_sources": snapshot.get("excluded_sources", []),
            "warnings": ["late_arrivals_outside_72h_unsupported", "ci_not_collected"],
        },
        "source_cursors": {
            name: window["until"]
            for name in sources
            if not any(gap["source"] == name for gap in gaps)
        },
        "source_heads": {
            project_id: observed["head"]
            for project_id, observed in git_reads.items()
            if observed["head"] and not any(gap["source"] == f"git:{project_id}" for gap in gaps)
        },
        "omitted": {"facts": 0},
    }
    _fit_brief(brief)
    has_changes = bool(brief["facts"])
    would_suppress = not has_changes and brief["coverage"]["complete"]
    return {
        "brief": brief,
        "brief_hash": hash_brief(brief),
        "would_suppress": would_suppress,
        "reason": "no_changes" if would_suppress else "partial_sources" if gaps else "activity",
    }
