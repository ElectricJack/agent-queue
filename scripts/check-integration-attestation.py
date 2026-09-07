#!/usr/bin/env python3
"""Fail-closed hosted-CI reuse decision for an exact integration workflow run."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


INTEGRATION_BRANCH = re.compile(
    r"aq/integration/p-[0-9a-f]{32}/r-[0-9a-f]{32}"
)
SHA = re.compile(r"[0-9a-f]{40}")
EVIDENCE_KEYS = {"current_run", "workflow_runs"}
ENTRY_KEYS = {"listed", "attempt", "latest"}


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _load(path: str) -> Any:
    source = Path(path)
    if source.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("input too large")
    return json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_pairs)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _repository_matches(run: dict[str, Any], full_name: str) -> bool:
    repository = run.get("repository")
    head_repository = run.get("head_repository")
    return bool(
        isinstance(repository, dict)
        and repository.get("full_name") == full_name
        and isinstance(head_repository, dict)
        and head_repository.get("full_name") == full_name
    )


def _workflow_file(path: object) -> str | None:
    if not isinstance(path, str):
        return None
    workflow_file = path.split("@", 1)[0]
    if not workflow_file.startswith(".github/workflows/"):
        return None
    return workflow_file


def _valid_current_run(
    run: object,
    *,
    repository: str,
    checkout_sha: str,
    current_run_id: int,
    current_run_attempt: int,
) -> bool:
    return bool(
        isinstance(run, dict)
        and _positive_int(run.get("id"))
        and _positive_int(run.get("run_attempt"))
        and run.get("id") == current_run_id
        and run.get("run_attempt") == current_run_attempt
        and _positive_int(run.get("workflow_id"))
        and _workflow_file(run.get("path")) is not None
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("head_sha") == checkout_sha
        and _repository_matches(run, repository)
    )


def _same_candidate_identity(
    run: object,
    *,
    run_id: int,
    run_attempt: int,
    workflow_id: int,
    workflow_path: str,
    repository: str,
    checkout_sha: str,
) -> bool:
    return bool(
        isinstance(run, dict)
        and _positive_int(run.get("id"))
        and _positive_int(run.get("run_attempt"))
        and _positive_int(run.get("workflow_id"))
        and run.get("id") == run_id
        and run.get("run_attempt") == run_attempt
        and run.get("workflow_id") == workflow_id
        and _workflow_file(run.get("path")) == _workflow_file(workflow_path)
        and run.get("event") == "push"
        and isinstance(run.get("head_branch"), str)
        and INTEGRATION_BRANCH.fullmatch(run["head_branch"])
        and run.get("head_sha") == checkout_sha
        and _repository_matches(run, repository)
    )


def _candidate(entry: object, current: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
        return None
    listed = entry["listed"]
    if not isinstance(listed, dict):
        return None
    run_id = listed.get("id")
    run_attempt = listed.get("run_attempt")
    if (
        not _positive_int(run_id)
        or not _positive_int(run_attempt)
        or run_id >= args.current_run_id
    ):
        return None
    expected = {
        "run_id": run_id,
        "run_attempt": run_attempt,
        "workflow_id": current["workflow_id"],
        "workflow_path": current["path"],
        "repository": args.repository_full_name,
        "checkout_sha": args.checkout_sha,
    }
    if not all(
        _same_candidate_identity(entry[view], **expected)
        for view in ("listed", "attempt", "latest")
    ):
        return None
    return entry


def _successful(entry: dict[str, Any]) -> bool:
    return all(
        entry[view].get("status") == "completed"
        and entry[view].get("conclusion") == "success"
        for view in ("listed", "attempt", "latest")
    )


def decide(args: argparse.Namespace) -> bool:
    if (
        args.event_name != "push"
        or args.default_branch != "main"
        or args.ref != "refs/heads/main"
        or not isinstance(args.repository_full_name, str)
        or args.repository_full_name.count("/") != 1
        or not isinstance(args.checkout_sha, str)
        or SHA.fullmatch(args.checkout_sha) is None
        or not _positive_int(args.current_run_id)
        or not _positive_int(args.current_run_attempt)
    ):
        return False

    evidence = _load(args.workflow_runs_file)
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        return False
    current = evidence["current_run"]
    if not _valid_current_run(
        current,
        repository=args.repository_full_name,
        checkout_sha=args.checkout_sha,
        current_run_id=args.current_run_id,
        current_run_attempt=args.current_run_attempt,
    ):
        return False
    records = evidence["workflow_runs"]
    if not isinstance(records, list) or not records:
        return False

    candidates = [_candidate(entry, current, args) for entry in records]
    if any(candidate is None for candidate in candidates):
        return False
    typed_candidates = [candidate for candidate in candidates if candidate is not None]
    run_ids = [candidate["listed"]["id"] for candidate in typed_candidates]
    if len(run_ids) != len(set(run_ids)):
        return False
    newest = max(typed_candidates, key=lambda entry: entry["listed"]["id"])
    return _successful(newest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matching-integration-pr", action="store_true")
    for name in (
        "event-name",
        "ref",
        "default-branch",
        "repository-full-name",
        "checkout-sha",
        "workflow-runs-file",
    ):
        parser.add_argument(f"--{name}")
    for name in ("current-run-id", "current-run-attempt"):
        parser.add_argument(f"--{name}", type=int)
    for name in ("head-repository-full-name", "head-ref"):
        parser.add_argument(f"--{name}")
    args = parser.parse_args()
    try:
        if args.matching_integration_pr:
            result = bool(
                args.event_name == "pull_request"
                and args.repository_full_name
                and args.head_repository_full_name == args.repository_full_name
                and isinstance(args.head_ref, str)
                and INTEGRATION_BRANCH.fullmatch(args.head_ref)
            )
        else:
            result = decide(args)
    except Exception:
        result = False
    print("true" if result else "false")


if __name__ == "__main__":
    main()
