"""Dormant suite/check policy inputs, also executable on a bare hosted runner.

This module only resolves policy and prepares artifacts. Operator activation uses
the existing CommandHandler policy paths; generating a plan grants no authority.
Legacy jobs remain dispatchable throughout coexistence and rollback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

ORDER = ("dashboard", "cli", "default", "postgres", "migration_slow", "e2e")
ALIASES = {
    "dashboard": ("dashboard",), "cli": ("cli", "cli_startup"),
    "default": ("default",), "postgres": ("postgres",),
    "migration_slow": ("migration_slow", "generated"), "e2e": ("e2e",),
}
CATALOG_PATH = ".github/agent-queue-suite-cutover.json"
TRUST_PATH = ".github/agent-queue-integration.json"


def read_catalog(path: str | Path = CATALOG_PATH) -> dict:
    catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog: dict) -> None:
    if catalog["schema"] != "aq.suite-cutover.v1" or set(catalog["suites"]) != set(ORDER):
        raise ValueError("unknown suite cutover schema or suites")
    active = catalog["active_suites"]
    if (not isinstance(active, list) or len(set(active)) != len(active)
            or set(active) - set(ORDER)):
        raise ValueError("unknown or duplicate active suites")
    legacy = catalog["legacy"]
    names = legacy["names"]
    if (not isinstance(legacy["version"], str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", legacy["version"]) is None or not names
            or len(set(names)) != len(names) or any(not name.strip() for name in names)):
        raise ValueError("legacy check/version identity is incomplete")
    covered = []
    for name, aliases in ALIASES.items():
        suite = catalog["suites"][name]
        if not suite["legacy_checks"]:
            raise ValueError("a suite has no legacy obligations")
        covered.extend(suite["legacy_checks"])
        expected = [{"name": f"Bazel ({alias})", "target": f"//checks:{alias}"}
                    for alias in aliases]
        if suite["bazel_checks"] != expected:
            raise ValueError("suite check names must bind the complete Bazel alias set")
    if len(set(covered)) != len(covered) or set(covered) != set(names):
        raise ValueError("suite obligations do not partition the legacy checks")
    for obligation, owner in (("cli_startup", "cli"), ("generated", "migration_slow")):
        pin = catalog["pinned_obligations"][obligation]
        if (pin["owner"] != owner or pin["legacy_check"] not in
                catalog["suites"][owner]["legacy_checks"]
                or pin["bazel_check"] != f"Bazel ({obligation})"):
            raise ValueError("startup and generated obligations must stay explicitly required")


def required_checks(catalog: dict) -> dict:
    """Resolve one immutable names/version pair; legacy defaults retain their identity."""
    validate_catalog(catalog)
    active = set(catalog["active_suites"])
    names, replaced = [], set()
    owners = {check: suite for suite in ORDER
              for check in catalog["suites"][suite]["legacy_checks"]}
    for check in catalog["legacy"]["names"]:
        owner = owners[check]
        if owner not in active:
            names.append(check)
        elif owner not in replaced:
            names.extend(entry["name"] for entry in catalog["suites"][owner]["bazel_checks"])
            replaced.add(owner)
    # Bind alias targets as well as names: an old version cannot authorize a
    # different alias under the same check name. Source changes also bind to SHA.
    identity = {"legacy_version": catalog["legacy"]["version"], "names": names,
                "targets": [entry for suite in ORDER if suite in active
                            for entry in catalog["suites"][suite]["bazel_checks"]]}
    version = catalog["legacy"]["version"] if not active else "ci-bazel-v1-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return {"version": version, "names": names}


def policy_input(catalog: dict, trust: dict) -> dict:
    """Refuse a partial workflow/trust update before accepting policy inputs."""
    required = required_checks(catalog)
    if trust["required_checks"] != required:
        raise ValueError("suite catalog and trust manifest checks/version differ")
    producer = trust["ci_producer_app_id"]
    if type(producer) is not int or producer <= 0:
        raise ValueError("CI producer must be a positive App identity")
    return {**required, "producer_id": str(producer)}


def workflow_matrix(catalog: dict, prepare_suite: str = "") -> dict:
    """Additional candidate checks; preparation never changes the required set."""
    validate_catalog(catalog)
    if prepare_suite and prepare_suite not in ORDER:
        raise ValueError("unknown preparation suite")
    enabled = set(catalog["active_suites"]) | ({prepare_suite} if prepare_suite else set())
    return {"include": [entry for suite in ORDER if suite in enabled
                        for entry in catalog["suites"][suite]["bazel_checks"]]}


def transition_catalog(catalog: dict, suite: str, *, rollback: bool = False) -> dict:
    validate_catalog(catalog)
    if suite not in ORDER:
        raise ValueError("unknown transition suite")
    active = set(catalog["active_suites"])
    if rollback:
        if suite not in active:
            raise ValueError("cannot roll back an inactive suite")
        active.remove(suite)
    else:
        if suite in active or not set(ORDER[:ORDER.index(suite)]).issubset(active):
            raise ValueError("activate one suite at a time in rollout order")
        active.add(suite)
    return {**catalog, "active_suites": [name for name in ORDER if name in active]}


def transition_plan(catalog: dict, trust: dict, suite: str, sha: str, *, rollback=False) -> dict:
    """Reviewable operator inputs, never an activation or proof of parity/check success."""
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("transition requires an exact candidate SHA")
    before = policy_input(catalog, trust)
    proposed = transition_catalog(catalog, suite, rollback=rollback)
    required = required_checks(proposed)
    return {
        "schema": "aq.suite-transition-plan.v1", "suite": suite, "candidate_sha": sha,
        "rollback": rollback, "activated": False, "verified": False,
        "expected_required_checks": before, "catalog": proposed,
        "trust_manifest": {**trust, "required_checks": required},
        "required_checks": {**required, "producer_id": before["producer_id"]},
        "attestation_variable": {"AQ_INTEGRATION_REQUIRED_CHECK_VERSION": required["version"]},
        "branch_protection_checks": required["names"],
        "operator_obligations": [
            "Observe the proposed complete check set on this exact candidate with trusted CI.",
            "Bind parity/cache/budget/graph evidence and approval/sign-off to this suite.",
            "Apply catalog, trust manifest, frozen train policy, branch protections and audit "
            "version together using the supported operator commands; retain activation receipts.",
            "Retain dispatchable legacy CI for two weeks; record the week comparison and "
            "rollback rehearsal before retirement.",
        ],
    }


def verify_candidate(
    catalog: dict, trust: dict, sha: str, pages: list[dict], *,
    trusted_catalog: dict, trusted_trust: dict,
) -> str:
    """Require the newest trusted check of every obligation on this exact candidate."""
    policy_input(trusted_catalog, trusted_trust)
    # Candidate files describe a proposed switch, not its trust authority. Only
    # active_suites and its derived required_checks may differ from trusted data.
    for key in ("schema", "legacy", "suites", "pinned_obligations"):
        if catalog[key] != trusted_catalog[key]:
            raise ValueError("candidate redefines trusted suite obligations")
    if ({k: v for k, v in trust.items() if k != "required_checks"}
            != {k: v for k, v in trusted_trust.items() if k != "required_checks"}):
        raise ValueError("candidate redefines trusted repository or producer")
    changed = set(catalog["active_suites"]) ^ set(trusted_catalog["active_suites"])
    if len(changed) > 1:
        raise ValueError("candidate must switch at most one suite")
    if changed:
        suite = changed.pop()
        rollback = suite not in catalog["active_suites"]
        if transition_catalog(trusted_catalog, suite, rollback=rollback) != catalog:
            raise ValueError("candidate must follow the ordered suite transition")
    policy = policy_input(catalog, trust)
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("invalid candidate SHA")
    latest = {}
    for page in pages:
        for run in page["check_runs"]:
            name = run.get("name")
            app = run.get("app")
            app_id = app.get("id") if isinstance(app, dict) else None
            if (name not in policy["names"] or type(app_id) is not int
                    or app_id != int(policy["producer_id"])):
                continue
            run_id = run.get("id")
            if type(run_id) is not int or run_id <= 0:
                raise ValueError("malformed trusted candidate check identity")
            if run_id > latest.get(name, {}).get("id", 0):
                latest[name] = run
    for name in policy["names"]:
        run = latest.get(name, {})
        if (run.get("head_sha") != sha or run.get("status") != "completed"
                or run.get("conclusion") != "success"):
            raise ValueError(f"candidate check is missing, stale or unsuccessful: {name}")
    return policy["version"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("workflow", "plan", "candidate"))
    parser.add_argument("--catalog", default=CATALOG_PATH)
    parser.add_argument("--trust", default=TRUST_PATH)
    parser.add_argument("--trusted-catalog", default=CATALOG_PATH)
    parser.add_argument("--trusted-trust", default=TRUST_PATH)
    parser.add_argument("--suite", default="")
    parser.add_argument("--sha")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--runs", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    catalog = read_catalog(args.catalog)
    trust = json.loads(Path(args.trust).read_text(encoding="utf-8"))
    policy_input(catalog, trust)
    if args.operation == "workflow":
        matrix = workflow_matrix(catalog, os.environ.get("PREPARE_SUITE", ""))
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"enabled={str(bool(matrix['include'])).lower()}\n")
            output.write("matrix=" + json.dumps(matrix, separators=(",", ":")) + "\n")
    elif args.operation == "candidate":
        pages = json.loads(args.runs.read_text(encoding="utf-8"))
        print(verify_candidate(
            catalog, trust, args.sha, pages,
            trusted_catalog=read_catalog(args.trusted_catalog),
            trusted_trust=json.loads(Path(args.trusted_trust).read_text(encoding="utf-8")),
        ))
    else:
        plan = transition_plan(catalog, trust, args.suite, args.sha, rollback=args.rollback)
        args.output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
