#!/usr/bin/env python3
"""Drive the App-mode integration train proof (App-mode spec §10, S1-S10).

Run through ``scripts/e2e-app-train.sh run STEP``, which exports the isolated
world (``AQ_E2E_HOME``, ``AQ_API_URL``, the disposable database) and strips a
worker session's variables.  Every step is resumable: what it created is kept in
``$AQ_E2E_HOME/state.json``, and every id and SHA it observed is appended to
``$AQ_E2E_HOME/evidence.jsonl`` for the gate report.  Every GitHub payload a
step reads is saved under ``$AQ_E2E_HOME/payloads/`` so the offline tests can
replay what GitHub really returned.

Three identities touch GitHub, and each is used only for what spec §10 gives it:

* the disposable daemon, App-only, for everything AQ does;
* the operator's ``gh`` login (``gh_*`` helpers), for the anchors only the
  repository admin may write (variables, ruleset, the setup commits) and for the
  negative controls;
* a fixture-scoped App installation token minted here (``app_get``), for
  read-only recordings of what the App's own token sees, such as
  ``current_user_can_bypass``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smoke import CliError, Failure, api, aq, check, run_aq, session_token, wait_for

REPO_ROOT = Path(__file__).resolve().parents[2]
HOME = Path(os.environ["AQ_E2E_HOME"])
VAULT = HOME / "vault"
STATE_PATH = HOME / "state.json"
EVIDENCE_PATH = HOME / "evidence.jsonl"
PAYLOADS = HOME / "payloads"
ARTIFACTS = HOME / "artifacts"
APPROVAL_PATH = HOME / "APPROVED"
ONBOARDING_ROOT = Path(os.environ.get("E2E_ONBOARDING_ROOT", HOME / "onboarding"))
REPOSITORY = os.environ.get(
    "AQ_APP_TRAIN_REPOSITORY", "ElectricJack/aq-gh615-app-fixture-20260923"
)
REPOSITORY_URL = f"https://github.com/{REPOSITORY}.git"
OPERATOR_CONFIG = Path(
    os.environ.get("AQ_APP_TRAIN_OPERATOR_CONFIG", Path.home() / ".agent-queue/config.yaml")
)

ATTESTATION_NAME = "Agent Queue Integration Attestation"
ATTESTATION_VARIABLE = "AQ_INTEGRATION_ATTESTATION_APP_ID"
VERSION_VARIABLE = "AQ_INTEGRATION_REQUIRED_CHECK_VERSION"
ACTIONS_APP_ID = 15368
RULESET_NAME = "Train-only main"
#: GitHub's built-in repository role id for "admin" (a user-owned repository
#: has no organization-admin actor): the S10 break-glass bypass.
ADMIN_ROLE_ID = 5

WORKER_PROFILE = "train-worker"
REVIEWER_PROFILE = "reviewer"
TRAIN_CLASS = "standard-high"
CHECK_V1 = ("fixture-v1", "fixture")
CHECK_V2 = ("fixture-v2", "fixture (v2)")
#: Close refusals that mean "the daemon has not recorded the trusted CI
#: evidence for this generation and head yet" rather than a real failure:
#: the verifier keeps its claim and the driver keeps polling until it lands.
_VERIFIER_WAIT_REFUSALS = ("stale_verification", "awaiting_trusted_verification")

SHARED_ROUTES = {
    "parent-integration": (
        "sha256:65f970021fdf5c73b8aeabdd4bbbf22f2318d3c198d1176731b7196de1a3bf51"
    ),
    "root-train": "sha256:5808faf05a6723f49d510ced885cd4870fa3990a2f710c428ccf5a250874f2fe",
}


# ---------------------------------------------------------------------------
# State, evidence and payloads
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATE_PATH)


def record(scenario: str, key: str, value: Any, **extra: Any) -> None:
    """Append one observed fact to the evidence ledger and echo it."""
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "scenario": scenario,
           "key": key, "value": value, **extra}
    with EVIDENCE_PATH.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    shown = value if isinstance(value, (str, int, float, bool)) or value is None else json.dumps(
        value, sort_keys=True
    )
    print(f"   [{scenario}] {key}: {str(shown)[:300]}")


def save_payload(name: str, payload: Any) -> Path:
    PAYLOADS.mkdir(parents=True, exist_ok=True)
    path = PAYLOADS / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def step(title: str) -> None:
    print(f"\n==> {title}")


def require_approval() -> str:
    """Spec §10: the operator approves the live run before its first mutation."""
    text = APPROVAL_PATH.read_text().strip() if APPROVAL_PATH.exists() else ""
    if not text:
        raise Failure(
            f"no operator approval recorded at {APPROVAL_PATH}: every step from s1 on "
            "mutates the fixture repository. Record the approving message there first."
        )
    return text


# ---------------------------------------------------------------------------
# The operator's `gh` (repository admin)
# ---------------------------------------------------------------------------


def _operator_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
        env.pop(key, None)
    return env


def gh(*args: str, input_text: str | None = None, check_ok: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["gh", *args], input=input_text, capture_output=True, text=True, env=_operator_env(),
        timeout=120, check=False,
    )
    if check_ok and proc.returncode != 0:
        raise Failure(f"gh {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()[:600]}")
    return proc


def gh_api(path: str, *, method: str = "GET", body: Any = None, check_ok: bool = True) -> Any:
    args = ["api", "--method", method, path]
    if body is not None:
        args += ["--input", "-"]
    proc = gh(*args, input_text=None if body is None else json.dumps(body), check_ok=check_ok)
    if proc.returncode != 0:
        return {"_status": proc.returncode, "_stderr": proc.stderr.strip(), "_stdout": proc.stdout}
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def git(cwd: Path, *args: str, check_ok: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=_operator_env(), timeout=300,
        check=False,
    )
    if check_ok and proc.returncode != 0:
        raise Failure(f"git {' '.join(args)} in {cwd} exited {proc.returncode}: "
                      f"{(proc.stderr or proc.stdout).strip()[:800]}")
    return proc


def harness_clone() -> Path:
    """The operator's clone of the fixture (their `gh` credential), for admin pushes."""
    path = HOME / "harness-clone"
    if not (path / ".git").exists():
        gh("repo", "clone", REPOSITORY, str(path))
        git(path, "config", "user.name", "AQ App Train (operator)")
        git(path, "config", "user.email", "aq-app-train-operator@example.invalid")
    git(path, "fetch", "--prune", "origin")
    return path


def fixture_main_sha() -> str:
    return gh_api(f"repos/{REPOSITORY}/git/ref/heads/main")["object"]["sha"]


# ---------------------------------------------------------------------------
# The App's own token (read-only recordings)
# ---------------------------------------------------------------------------


_APP_TOKEN: dict[str, Any] = {}


def _app_config():
    import yaml

    from src.config import GitHubAppConfig

    app = yaml.safe_load(OPERATOR_CONFIG.read_text())["integration"]["github_app"]
    return GitHubAppConfig(
        client_id=str(app["client_id"]),
        app_id=int(app["app_id"]),
        installation_id=int(app["installation_id"]),
        private_key_path=str(app["private_key_path"]),
    )


def app_id() -> int:
    return _app_config().app_id


def repository_id() -> int:
    state = load_state()
    if "repository_id" not in state:
        state["repository_id"] = int(gh_api(f"repos/{REPOSITORY}")["id"])
        save_state(state)
    return int(state["repository_id"])


def _app_token() -> str:
    if _APP_TOKEN.get("expires_at", 0) - time.time() > 300:
        return _APP_TOKEN["token"]
    from src.git.github_app import AppTokenProvider, OwnerFilePrivateKeyProvider
    from src.git.github_contracts import GitHubRepositoryBinding

    provider = AppTokenProvider(_app_config(), key_provider=OwnerFilePrivateKeyProvider())
    candidate = asyncio.run(
        provider.mint(GitHubRepositoryBinding(repository_id(), REPOSITORY))
    )
    _APP_TOKEN.update(token=candidate.token, expires_at=candidate.expires_at)
    return candidate.token


def app_get(path: str) -> tuple[int, Any]:
    """GET *path* with a fixture-scoped installation token of the daemon's App."""
    req = urllib.request.Request(
        f"https://api.github.com/{path.lstrip('/')}",
        headers={
            "Authorization": f"Bearer {_app_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aq-app-train-e2e",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body or b"null")
        except json.JSONDecodeError:
            return exc.code, body.decode(errors="replace")


def record_protection(scenario: str, label: str) -> dict:
    """Record the three protection reads the daemon's reader makes, as the App."""
    rid = repository_id()
    status, rules = app_get(f"repositories/{rid}/rules/branches/main?per_page=100")
    save_payload(f"{label}-rules-main", {"status": status, "body": rules})
    rulesets = {}
    for ruleset_id in sorted({r.get("ruleset_id") for r in rules or [] if isinstance(r, dict)}):
        rs_status, ruleset = app_get(f"repositories/{rid}/rulesets/{ruleset_id}")
        save_payload(f"{label}-ruleset-{ruleset_id}", {"status": rs_status, "body": ruleset})
        rulesets[str(ruleset_id)] = {
            "status": rs_status,
            "name": (ruleset or {}).get("name") if isinstance(ruleset, dict) else None,
            "current_user_can_bypass": (ruleset or {}).get("current_user_can_bypass")
            if isinstance(ruleset, dict) else None,
            "has_bypass_actors_field": isinstance(ruleset, dict) and "bypass_actors" in ruleset,
        }
    classic_status, classic = app_get(f"repositories/{rid}/branches/main/protection")
    save_payload(f"{label}-classic-protection", {"status": classic_status, "body": classic})
    summary = {
        "rules_status": status,
        "rule_types": [(r.get("type"), r.get("ruleset_id")) for r in rules or []
                       if isinstance(r, dict)],
        "rulesets": rulesets,
        "classic_status": classic_status,
    }
    record(scenario, f"{label}.protection_as_app", summary)
    return summary


# ---------------------------------------------------------------------------
# The disposable daemon (operator CLI and API)
# ---------------------------------------------------------------------------


def operator(*args: str, check_ok: bool = True, timeout: float = 180.0) -> Any:
    return aq(*args, check_ok=check_ok, timeout=timeout)


def operator_text(*args: str, timeout: float = 180.0) -> tuple[int, str]:
    proc = run_aq(*args, json_mode=False, timeout=timeout)
    return proc.returncode, (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()


def generation(project_id: str) -> str:
    return str(operator("integration", "status", project_id)["generation"])


def project_id() -> str:
    state = load_state()
    if "project_id" not in state:
        raise Failure("no project yet: run `prepare` first")
    return state["project_id"]


def app_verify(scenario: str, label: str, *, policy: Path | None = None) -> dict:
    pid = project_id()
    args = ["integration", "app-verify", pid, "--repository-id", pid]
    if policy is not None:
        args += ["--policy", str(policy)]
    result = operator(*args, check_ok=False)
    if isinstance(result, dict) and "_error" in result:
        err = result["_error"]
        raise Failure(f"app-verify refused: {err}")
    save_payload(f"{label}-app-verify", result)
    items = {item["id"]: (item["status"], item.get("code")) for item in result.get("items", [])}
    record(scenario, f"{label}.app_verify", {"ready": result.get("ready"), "items": items})
    return result


def item(result: dict, item_id: str) -> dict:
    for entry in result.get("items", []):
        if entry["id"] == item_id:
            return entry
    raise Failure(f"app-verify has no {item_id!r} item: {result}")


# ---------------------------------------------------------------------------
# Vault profiles (written before the daemon starts)
# ---------------------------------------------------------------------------


_PROFILE = """---
id: {id}
name: "App train {id}"
tags: [profile, agent-type, e2e, app-train]
---

# App train {id}

## Role
A pool profile the App-mode train proof (scripts/e2e-app-train.sh) plays under
the fake session provider: the harness claims, commits, pushes and closes as
this session.  Nothing reads this Role.

## Config
```json
{config}
```

## Tools
```json
{{"allowed": {tools}}}
```

## MCP Servers
```json
[]
```
"""


def write_profiles(_args) -> None:
    """Pool profiles for the train: the primary/verifier/repair rung and the reviewer.

    The kit's own ``reviewer`` is task-lifecycle; the review evidence path
    requires the profile id ``reviewer``, so it is replaced by a pool profile.

    Parent verifiers and repair delegates are filed unrouted, with
    ``TRAIN_CLASS`` as a class hint, and the router picks their profile.
    ``play_verifier`` and ``play_repair`` claim them as ``WORKER_PROFILE``, so
    it must stay the router's only worker candidate at ``TRAIN_CLASS``: a
    writable pool with slots (it needs no ``extends``).  ``TRAIN_CLASS`` must
    not be ``deep-high``: the routing policy reserves deep-high Claude for the
    design lanes.  ``tests/test_e2e_kit_fixtures.py`` plans both delegates
    against these profiles.
    """
    for profile_id, read_only in ((WORKER_PROFILE, False), (REVIEWER_PROFILE, True)):
        config = {
            "harness": "claude",
            "lifecycle": "pool",
            "default_class": TRAIN_CLASS,
            "read_only": read_only,
            "min_active": 0,
            "max_active": 3,
            "max_claims_per_session": 1,
            "needs_workspace": True,
            "workspaces": ["project-repo"],
        }
        tools = ["Bash", "Read", "Glob", "Grep"] if read_only else [
            "Bash", "Read", "Write", "Edit", "Glob", "Grep"]
        path = VAULT / "agent-types" / profile_id / "profile.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_PROFILE.format(
            id=profile_id, config=json.dumps(config, indent=2), tools=json.dumps(tools)))
        print(f"==> wrote pool profile {profile_id} ({path})")


# ---------------------------------------------------------------------------
# Fixture content
# ---------------------------------------------------------------------------


def fixture_ci(checks: list[str]) -> str:
    """The fixture's gating workflow, shaped like agent-queue's tests.yml.

    Push runs only on the train's refs (a normal promotion runs no second CI
    on ``main``), ``workflow_call`` for the audit's fallback, and the 09-24
    design's in-place repair requirement on integration candidates.
    """
    jobs = []
    for name in checks:
        job_id = "fixture" if name == "fixture" else name.replace(" ", "-").replace(
            "(", "").replace(")", "")
        jobs.append(f"""  {job_id}:
    name: {json.dumps(name)}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Verify fixture
        run: test -f README.md
      - name: Require an in-place train repair
        if: startsWith(github.ref, 'refs/heads/aq/integration/')
        run: test -f train-repaired.txt
""")
    return """name: Fixture CI
on:
  push:
    branches:
      - 'aq/parent/**'
      - 'aq/integration/**'
  pull_request:
    branches: [main]
  workflow_dispatch:
  workflow_call:
permissions:
  contents: read
jobs:
""" + "".join(jobs)


# ---------------------------------------------------------------------------
# prepare (read-only on GitHub)
# ---------------------------------------------------------------------------


def _playbook_ready(playbook_id: str, sha: str) -> bool:
    health = operator("playbook", "activation-health", "--playbook-id", playbook_id,
                      check_ok=False)
    text = json.dumps(health)
    return sha in text and '"enabled": true' in text.replace('"enabled":true', '"enabled": true')


def prepare(args) -> None:
    state = load_state()
    pid = state.get("project_id") or args.project_id or time.strftime("aqapp-%m%d%H%M")
    state["project_id"] = pid
    save_state(state)
    record("prepare", "project_id", pid)
    record("prepare", "repository", {"full_name": REPOSITORY, "id": repository_id(),
                                     "app_id": app_id()})

    step("baseline: fixture main, variables and protection (read-only)")
    base = fixture_main_sha()
    state.setdefault("recorded_base_sha", base)
    save_state(state)
    record("prepare", "fixture_main_sha", base)
    variables = gh_api(f"repos/{REPOSITORY}/actions/variables")
    save_payload("baseline-variables-operator", variables)
    record("prepare", "baseline_variables",
           {v["name"]: v["value"] for v in variables.get("variables", [])})
    record_protection("prepare", "baseline")

    step(f"onboard {pid} from {REPOSITORY} (App-only clone)")
    projects = operator("project", "list", check_ok=False)
    known = {row.get("id") for row in projects} if isinstance(projects, list) else set()
    if pid not in known:
        (ONBOARDING_ROOT / "fixture").mkdir(parents=True, exist_ok=True)
        onboarded = operator(
            "project", "onboard", "--source-mode", "github_clone",
            "--root-id", "e2e-onboarding", "--relative-path", f"fixture/{pid}",
            "--project-name", f"App train fixture {pid}", "--project-id", pid,
            "--github-url", REPOSITORY_URL, "--request-id", f"onboard-{pid}",
            timeout=600,
        )
        save_payload("prepare-onboard", onboarded)
    workspaces = api("list_workspaces", {"project_id": pid}).get("workspaces", [])
    for n in range(len(workspaces), 4):
        operator("project", "add-workspace", "--project-id", pid, "--source", "clone",
                 "--name", f"{pid}-slot{n}", timeout=600)
    workspaces = api("list_workspaces", {"project_id": pid}).get("workspaces", [])
    record("prepare", "workspaces", [w.get("id") for w in workspaces])

    step("shared routes parent-integration and root-train")
    for playbook_id, sha in SHARED_ROUTES.items():
        if _playbook_ready(playbook_id, sha):
            continue
        path = f"reviewed-playbooks/{playbook_id}"
        operator("playbook", "v2-validate", "--path", f"{path}/artifact.json")
        operator("playbook", "v2-import", "--path", path)
        operator("playbook", "activate", "--playbook-id", playbook_id,
                 "--artifact-sha256", sha, "--enabled")
        check(_playbook_ready(playbook_id, sha), f"{playbook_id} not active at {sha}")
    record("prepare", "routes", SHARED_ROUTES)

    step("designate the integration repository")
    status = operator("integration", "status", pid)
    if status.get("repository_id") != pid and (status.get("repository") or {}).get("id") != pid:
        repo = json.dumps({"id": pid, "url": REPOSITORY_URL, "default_branch": "main"},
                          separators=(",", ":"))
        operator("project", "set", pid, "integration-repository", repo,
                 "--expected-integration-generation", generation(pid),
                 "--reason", "App-mode train proof: bind the disposable fixture")
    record("prepare", "integration_status", {
        key: operator("integration", "status", pid).get(key)
        for key in ("mode", "effective_mode", "generation", "repository_id")
    })

    step("render the policy, manifest, audit workflow and ruleset (onboard-train)")
    clone = harness_clone()
    git(clone, "checkout", "-q", "--detach", "origin/main")
    (clone / ".github/workflows/ci.yml").write_text(fixture_ci([CHECK_V1[1]]))
    git(clone, "add", "-A")
    git(clone, "-c", "commit.gpgsign=false", "commit", "-q", "-m",
        "App train setup: gate CI on the train refs, callable by the audit")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    rendered = _onboard_train(clone, CHECK_V1, ARTIFACTS / "v1")
    state["setup_ci_commit"] = git(clone, "rev-parse", "HEAD").stdout.strip()
    state["policy_v1"] = str(rendered["policy"])
    save_state(state)
    record("prepare", "policy_v1", str(rendered["policy"]))

    step("app-verify against the rendered policy (read-only)")
    app_verify("prepare", "baseline", policy=rendered["policy"])
    print("\nprepare complete: nothing on GitHub was changed.")


def _onboard_train(clone: Path, checks: tuple[str, str], out: Path) -> dict[str, Path]:
    """Render the anchors with the planner, then point the policy at the pool profiles."""
    pid = project_id()
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "policy": out / "train-policy.json",
        "manifest": out / "agent-queue-integration.json",
        "audit": out / "main-attestation.yml",
        "ruleset": out / "ruleset.json",
    }
    code, text = operator_text(
        "integration", "onboard-train", pid, "--repo", str(clone), "--ref", "HEAD",
        "--route", "shared", "--check", checks[1], "--check-version", checks[0],
        "--intelligence-class", TRAIN_CLASS,
        "--credential-mode", "app", "--github-repository-id", str(repository_id()),
        "--repository-id", pid, "--interval-seconds", "60",
        "--write-policy", str(paths["policy"]),
        "--write-trust-manifest", str(paths["manifest"]),
        "--write-audit-workflow", str(paths["audit"]),
        "--write-ruleset", str(paths["ruleset"]),
    )
    (out / "onboard-train.txt").write_text(text + "\n")
    if code != 0:
        raise Failure(f"onboard-train exited {code}:\n{text[-3000:]}")
    policy = json.loads(paths["policy"].read_text())
    for boundary in ("parent", "root"):
        section = policy[boundary]
        # Repairs and verifiers carry TRAIN_CLASS as a hint only; the router
        # assigns their profile (the policy's profile fields are refused).
        # A live repair is played by hand; the stage deadline must not expire
        # while a human reads the dossier (the 09-24 run's stage 0 did).
        section["repair"]["primary_seconds"] = 7200
        section["repair"]["debug_seconds"] = 7200
    paths["policy"].write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
    return paths


# ---------------------------------------------------------------------------
# GitHub Actions runs (the audit workflow and the negative controls)
# ---------------------------------------------------------------------------


def wait_workflow_run(scenario: str, sha: str, workflow_name: str, *, timeout: float = 900,
                      label: str | None = None) -> dict:
    """Wait for *workflow_name*'s run on *sha*; record its jobs and the verifier line."""
    def completed():
        runs = gh_api(f"repos/{REPOSITORY}/actions/runs?head_sha={sha}&per_page=50")
        matching = [r for r in runs.get("workflow_runs", []) if r.get("name") == workflow_name]
        if not matching:
            return None
        run = max(matching, key=lambda r: r["id"])
        return run if run.get("status") == "completed" else None

    run = wait_for(completed, what=f"{workflow_name} run on {sha[:12]}", timeout=timeout,
                   interval=10)
    jobs = gh_api(f"repos/{REPOSITORY}/actions/runs/{run['id']}/jobs?per_page=50")
    summary = {
        "run_id": run["id"],
        "event": run.get("event"),
        "head_sha": run.get("head_sha"),
        "conclusion": run.get("conclusion"),
        "html_url": run.get("html_url"),
        "jobs": [{"id": j["id"], "name": j["name"], "conclusion": j.get("conclusion")}
                 for j in jobs.get("jobs", [])],
    }
    for job in jobs.get("jobs", []):
        if job["name"] != "Main attestation":
            continue
        proc = gh("api", f"repos/{REPOSITORY}/actions/jobs/{job['id']}/logs", check_ok=False)
        lines = [ln.split(" ", 1)[-1] for ln in proc.stdout.splitlines()
                 if "Main attestation:" in ln and "summary" not in ln.lower()]
        summary["verifier"] = lines[-1] if lines else None
    save_payload(f"{label or scenario}-run-{workflow_name.replace(' ', '-').lower()}-{sha[:12]}",
                 {"run": run, "jobs": jobs})
    record(scenario, f"{label or scenario}.{workflow_name}", summary)
    return summary


def job_conclusions(run: dict) -> dict[str, str | None]:
    return {job["name"]: job["conclusion"] for job in run["jobs"]}


def push_main(clone: Path, scenario: str, what: str, *, expect_rejected: bool = False) -> dict:
    """Push the clone's HEAD to fixture main as the operator (repository admin)."""
    head = git(clone, "rev-parse", "HEAD").stdout.strip()
    before = fixture_main_sha()
    proc = git(clone, "push", "origin", "HEAD:refs/heads/main", check_ok=False)
    after = fixture_main_sha()
    result = {
        "what": what,
        "sha": head,
        "main_before": before,
        "main_after": after,
        "exit": proc.returncode,
        "stderr": [ln for ln in proc.stderr.splitlines() if ln.startswith("remote:")
                   or "rejected" in ln or "error" in ln.lower()][:20],
    }
    record(scenario, f"push_main.{what}", result)
    if expect_rejected:
        check(proc.returncode != 0 and after == before,
              f"{what}: GitHub accepted a push the ruleset must reject: {result}")
    else:
        check(proc.returncode == 0 and after == head, f"{what}: push to main failed: {result}")
    return result


def fresh_commit(clone: Path, message: str, files: dict[str, str | None], *,
                 base: str = "origin/main") -> str:
    git(clone, "fetch", "--prune", "origin")
    git(clone, "checkout", "-q", "--detach", base)
    for rel, content in files.items():
        path = clone / rel
        if content is None:
            if path.exists():
                git(clone, "rm", "-q", rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        git(clone, "add", rel)
    git(clone, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", message)
    return git(clone, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# The ruleset (repository admin)
# ---------------------------------------------------------------------------


def train_ruleset_id() -> int | None:
    for ruleset in gh_api(f"repos/{REPOSITORY}/rulesets") or []:
        if ruleset.get("name") == RULESET_NAME:
            return int(ruleset["id"])
    return None


def put_ruleset(scenario: str, body: dict, what: str) -> dict:
    ruleset_id = train_ruleset_id()
    if ruleset_id is None:
        result = gh_api(f"repos/{REPOSITORY}/rulesets", method="POST", body=body)
    else:
        result = gh_api(f"repos/{REPOSITORY}/rulesets/{ruleset_id}", method="PUT", body=body)
    save_payload(f"{scenario}-ruleset-{what}-operator", result)
    record(scenario, f"ruleset.{what}", {"id": result.get("id"),
                                         "bypass_actors": result.get("bypass_actors"),
                                         "rules": [r.get("type") for r in result.get("rules", [])],
                                         "updated_at": result.get("updated_at")})
    return result


def target_ruleset(*, bypass_actors: list[dict] | None = None) -> dict:
    body = json.loads((ARTIFACTS / "v1" / "ruleset.json").read_text())
    body["bypass_actors"] = bypass_actors or []
    return body


def app_bypass_actor() -> dict:
    return {"actor_id": app_id(), "actor_type": "Integration", "bypass_mode": "always"}


def admin_bypass_actor() -> dict:
    return {"actor_id": ADMIN_ROLE_ID, "actor_type": "RepositoryRole", "bypass_mode": "always"}


# ---------------------------------------------------------------------------
# S1 - setup: manifest, audit workflow, variables
# ---------------------------------------------------------------------------


def s1(_args) -> None:
    require_approval()
    state = load_state()
    pid = project_id()
    policy = Path(state["policy_v1"])
    clone = harness_clone()

    step("S1: remove the fixture's classic protection (repository admin)")
    classic = gh_api(f"repos/{REPOSITORY}/branches/main/protection", check_ok=False)
    if isinstance(classic, dict) and "_status" not in classic:
        save_payload("s1-classic-protection-before-operator", classic)
        gh_api(f"repos/{REPOSITORY}/branches/main/protection", method="DELETE")
    after = gh_api(f"repos/{REPOSITORY}/branches/main/protection", check_ok=False)
    record("S1", "classic_protection_removed", isinstance(after, dict) and "_status" in after)

    step("S1: trust-manifest --write, commit it with the audit workflow")
    if not state.get("setup_sha"):
        main = fixture_main_sha()
        check(main == state["recorded_base_sha"],
              f"fixture main moved from the recorded base: {main}")
        manifest_path = clone / ".github/agent-queue-integration.json"
        code, text = operator_text(
            "integration", "trust-manifest", pid, "--policy", str(policy),
            "--repository-id", pid, "--write", str(manifest_path))
        record("S1", "trust_manifest_write", {"exit": code, "output": text.splitlines()[:4]})
        check(code in (0, 1) and manifest_path.exists(), f"trust-manifest --write failed: {text}")
        rendered = (ARTIFACTS / "v1" / "agent-queue-integration.json").read_text()
        check(manifest_path.read_text() == rendered,
              "the daemon's trust-manifest differs from onboard-train's rendering")
        sha = fresh_commit(clone, "App train setup: trust manifest, main audit, callable CI", {
            ".github/workflows/ci.yml": fixture_ci([CHECK_V1[1]]),
            ".github/agent-queue-integration.json": rendered,
            ".github/workflows/main-attestation.yml":
                (ARTIFACTS / "v1" / "main-attestation.yml").read_text(),
        })
        push_main(clone, "S1", "setup")
        state["setup_sha"] = sha
        save_state(state)
    record("S1", "setup_sha", state["setup_sha"])

    step("S1: app-setup --apply (the operator's gh sets the differing variables)")
    code, text = operator_text("integration", "app-setup", pid, "--policy", str(policy),
                               "--repository-id", pid, "--apply", timeout=300)
    (HOME / "s1-app-setup.txt").write_text(text + "\n")
    record("S1", "app_setup_apply", {"exit": code, "variables_section": [
        ln for ln in text.splitlines() if "AQ_INTEGRATION" in ln or "variables" in ln][:8]})
    variables = gh_api(f"repos/{REPOSITORY}/actions/variables")
    save_payload("s1-variables-operator", variables)
    record("S1", "variables", {v["name"]: v["value"] for v in variables["variables"]})

    step("S1: app-verify: every item ok except protection (unprotected)")
    result = app_verify("S1", "s1", policy=policy)
    for item_id in ("credential", "repository", "producer", "manifest", "variables",
                    "audit_workflow"):
        check(item(result, item_id)["status"] == "ok", f"S1 {item_id} not ok: "
              f"{item(result, item_id)}")
    protection = item(result, "protection")
    check(protection["status"] == "fail" and protection["code"] == "main_protection_missing",
          f"S1 protection is not main_protection_missing: {protection}")
    record_protection("S1", "s1-unprotected")
    wait_workflow_run("S1", state["setup_sha"], "Main attestation", label="s1-setup")
    print("\nS1 passed.")


# ---------------------------------------------------------------------------
# S3 - preflight negatives (before the ruleset exists)
# ---------------------------------------------------------------------------


def s3(_args) -> None:
    require_approval()
    state = load_state()
    pid = project_id()
    policy = Path(state["policy_v1"])
    clone = harness_clone()

    step("S3a: delete one variable -> hosted_workflow_variables_unavailable")
    gh("variable", "delete", VERSION_VARIABLE, "--repo", REPOSITORY)
    result = app_verify("S3", "s3a-variable-deleted", policy=policy)
    variables = item(result, "variables")
    check(variables["code"] == "hosted_workflow_variables_unavailable",
          f"S3a variables item: {variables}")
    code, _ = operator_text("integration", "app-setup", pid, "--policy", str(policy),
                            "--repository-id", pid, "--apply", timeout=300)
    record("S3", "s3a.restored_by_app_setup", {"exit": code})
    check(item(app_verify("S3", "s3a-restored", policy=policy), "variables")["status"] == "ok",
          "S3a: app-setup --apply did not restore the variable")

    step("S3b: a slug-producer policy -> ci_producer_not_numeric")
    slug = json.loads(policy.read_text())
    for boundary in ("parent", "root"):
        slug[boundary]["required_checks"]["producer_id"] = "github-actions"
    slug_path = ARTIFACTS / "v1" / "train-policy.slug.json"
    slug_path.write_text(json.dumps(slug, indent=2, sort_keys=True) + "\n")
    result = app_verify("S3", "s3b-slug-policy", policy=slug_path)
    producer = item(result, "producer")
    check(producer["code"] == "ci_producer_not_numeric", f"S3b producer item: {producer}")
    check(item(result, "manifest")["code"] != "trust_manifest_mismatch",
          "S3b: a slug producer surfaced as trust_manifest_mismatch")
    _bind_policy(pid, slug_path, "S3b: bind a slug-producer policy")
    operator("integration", "enable", pid, "--mode", "observe",
             "--expected-generation", generation(pid), "--reason", "S3b: preflight a slug policy")
    operator("integration", "flush", pid)
    status = operator("integration", "status", pid)
    save_payload("s3b-status-slug-policy", status)
    codes = _blocker_codes(status)
    record("S3", "s3b.status_blockers", codes)
    check("ci_producer_not_numeric" in codes, f"S3b status blockers: {codes}")
    operator("integration", "enable", pid, "--mode", "disabled",
             "--expected-generation", generation(pid), "--reason", "S3b: restore")
    _bind_policy(pid, policy, "S3b: restore the numeric policy")

    step("S3c: a manifest naming another attestation App -> trust_manifest_mismatch")
    good = (ARTIFACTS / "v1" / "agent-queue-integration.json").read_text()
    wrong = json.loads(good)
    wrong["attestation_app_id"] = 5052310
    fresh_commit(clone, "S3c: manifest names the deleted test App", {
        ".github/agent-queue-integration.json": json.dumps(wrong, indent=2, sort_keys=True) + "\n",
    })
    pushed = push_main(clone, "S3", "s3c-wrong-manifest")
    result = app_verify("S3", "s3c-wrong-manifest", policy=policy)
    manifest = item(result, "manifest")
    check(manifest["code"] == "trust_manifest_mismatch", f"S3c manifest item: {manifest}")
    record("S3", "s3c.wrong_manifest_sha", pushed["sha"])
    fresh_commit(clone, "S3c: restore the trust manifest", {
        ".github/agent-queue-integration.json": good,
    })
    restored = push_main(clone, "S3", "s3c-restore-manifest")
    state["s3_restored_sha"] = restored["sha"]
    save_state(state)
    check(item(app_verify("S3", "s3c-restored", policy=policy), "manifest")["status"] == "ok",
          "S3c: the restored manifest is not ok")
    print("\nS3 passed.")


def _bind_policy(pid: str, policy: Path, reason: str) -> None:
    compact = json.dumps(json.loads(policy.read_text()), separators=(",", ":"))
    operator("project", "set", pid, "integration-policy", compact,
             "--expected-integration-generation", generation(pid), "--reason", reason)


def _blocker_codes(status: dict) -> list[str]:
    return sorted({b.get("code") for b in status.get("blockers") or []})


def _warning_codes(status: dict) -> list[str]:
    return sorted({w.get("code") for w in status.get("warnings") or []})


# ---------------------------------------------------------------------------
# S2 + S9 - the protection reader and the development guard
# ---------------------------------------------------------------------------


def s2(_args) -> None:
    require_approval()
    state = load_state()
    pid = project_id()
    policy = Path(state["policy_v1"])

    step("S2: apply the §8.1 ruleset (no bypass)")
    created = put_ruleset("S2", target_ruleset(), "attested-only")
    state["ruleset_id"] = created["id"]
    save_state(state)
    result = app_verify("S2", "s2-attested-only", policy=policy)
    protection = item(result, "protection")
    check(protection["status"] == "ok", f"S2 protection after §8.1: {protection}")
    reading = record_protection("S2", "s2-attested-only")
    bypass = reading["rulesets"][str(created["id"])]["current_user_can_bypass"]
    record("S2", "current_user_can_bypass.no_bypass", bypass)
    check(bypass == "never", f"S2: the App's current_user_can_bypass is {bypass!r}, not 'never'")

    step("S9a: develop refused while attested_only")
    refused = operator("integration", "develop", pid, "--validation", "none",
                       "--reason", "S9: development under attested_only", check_ok=False)
    refusal = json.dumps(refused["_error"].envelope if isinstance(refused, dict)
                         and "_error" in refused else refused, default=str)
    record("S9", "develop_under_attested_only", refusal[:600])
    check("main_protection_blocks_development_publisher" in refusal,
          f"S9: develop was not refused with main_protection_blocks_development_publisher: "
          f"{refusal[:600]}")

    step("S2: add the App bypass")
    put_ruleset("S2", target_ruleset(bypass_actors=[app_bypass_actor()]), "app-bypass")
    result = app_verify("S2", "s2-app-bypass", policy=policy)
    reading = record_protection("S2", "s2-app-bypass")
    bypass = reading["rulesets"][str(created["id"])]["current_user_can_bypass"]
    record("S2", "current_user_can_bypass.app_bypass", bypass)
    check(bypass == "always", f"S2: the App's current_user_can_bypass is {bypass!r}, not 'always'")
    record("S2", "protection.app_bypass_item", item(result, "protection"))

    step("S9b: develop accepted with the bypass, then disabled again")
    before = generation(pid)
    accepted = operator("integration", "develop", pid, "--validation", "none",
                        "--reason", "S9: development with the App bypass")
    save_payload("s9-develop-accepted", accepted)
    record("S9", "develop_with_app_bypass", {
        "generation_before": before, "generation_after": generation(pid),
        "desired_mode": operator("integration", "status", pid).get("desired_mode"),
        "outcome": accepted.get("outcome") if isinstance(accepted, dict) else accepted})
    operator("integration", "enable", pid, "--mode", "disabled",
             "--expected-generation", generation(pid), "--reason", "S9: leave development")
    record("S9", "disabled_again", operator("integration", "status", pid).get("effective_mode"))

    step("S2: remove the bypass again")
    put_ruleset("S2", target_ruleset(), "attested-only-restored")
    check(item(app_verify("S2", "s2-restored", policy=policy), "protection")["status"] == "ok",
          "S2: protection did not return to attested_only")
    record_protection("S2", "s2-restored")
    print("\nS2 and S9 passed.")


# ---------------------------------------------------------------------------
# S6 - negative controls
# ---------------------------------------------------------------------------


_FORGE_WORKFLOW = """name: Forge attestation
on:
  push:
    branches: ['aq-app-train/forged-attestation']
permissions:
  contents: read
  checks: write
jobs:
  forge:
    runs-on: ubuntu-latest
    steps:
      - name: Post a check run named like the attestation, as GITHUB_TOKEN
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh api --method POST "repos/${{ github.repository }}/check-runs" \\
            -f name='Agent Queue Integration Attestation' \\
            -f head_sha="${{ github.sha }}" \\
            -f status=completed -f conclusion=success \\
            -f external_id='aq-attestation-v1:forged' \\
            -f 'output[title]=forged' -f 'output[summary]=forged by GITHUB_TOKEN (App 15368)'
"""


def s6(_args) -> None:
    require_approval()
    state = load_state()
    clone = harness_clone()

    step("S6a: an unattested push to main")
    fresh_commit(clone, "S6a: unattested change", {"s6a-unattested.txt": "unattested\n"})
    state["s6a_sha"] = push_main(clone, "S6", "s6a-unattested", expect_rejected=True)["sha"]
    save_state(state)

    step("S6b: a check run named like the attestation, created with GITHUB_TOKEN")
    sha = fresh_commit(clone, "S6b: forge an attestation from Actions", {
        ".github/workflows/forge-attestation.yml": _FORGE_WORKFLOW,
    })
    git(clone, "push", "-f", "origin", "HEAD:refs/heads/aq-app-train/forged-attestation")
    state["s6b_sha"] = sha
    save_state(state)
    record("S6", "s6b.forged_sha", sha)
    wait_workflow_run("S6", sha, "Forge attestation", label="s6b")

    def forged():
        runs = gh_api(f"repos/{REPOSITORY}/commits/{sha}/check-runs?check_name="
                      f"{urllib.request.quote(ATTESTATION_NAME)}&filter=all")
        return runs if runs.get("total_count") else None

    runs = wait_for(forged, what="the forged check run", timeout=300, interval=10)
    save_payload("s6b-forged-check-runs", runs)
    record("S6", "s6b.forged_check_runs", [
        {"id": r["id"], "app_id": r["app"]["id"], "conclusion": r["conclusion"]}
        for r in runs["check_runs"]])
    check(all(r["app"]["id"] == ACTIONS_APP_ID for r in runs["check_runs"]),
          "S6b: the forged check run is not attributed to GitHub Actions")
    push_main(clone, "S6", "s6b-forged", expect_rejected=True)
    _daemon_ignores_forgery("S6", sha)
    gh_api(f"repos/{REPOSITORY}/git/refs/heads/aq-app-train/forged-attestation",
           method="DELETE", check_ok=False)
    print("\nS6 passed.")


def _daemon_ignores_forgery(scenario: str, sha: str) -> None:
    """``select_trusted_attestation`` over the live check runs of the forged SHA."""
    from src.integration.ci import (
        AttestationError,
        IntegrationTrustManifest,
        select_trusted_attestation,
    )

    status, runs = app_get(f"repositories/{repository_id()}/commits/{sha}/check-runs"
                           f"?check_name={urllib.request.quote(ATTESTATION_NAME)}&filter=all")
    save_payload(f"{scenario.lower()}-forged-check-runs-as-app", {"status": status, "body": runs})
    trust = IntegrationTrustManifest.model_validate_json(
        (ARTIFACTS / "v1" / "agent-queue-integration.json").read_text())
    try:
        selected = select_trusted_attestation(runs.get("check_runs", []), trust,
                                              expected_head_sha=sha)
    except AttestationError as exc:
        record(scenario, "select_trusted_attestation", f"refused: {exc}")
        return
    raise Failure(f"the daemon selected a forged attestation: {selected}")


# ---------------------------------------------------------------------------
# The train: bind, observe, hierarchy, train
# ---------------------------------------------------------------------------


def enter_train(args) -> None:
    """Bind the numeric policy with pull-request review, then observe -> hierarchy -> train."""
    require_approval()
    state = load_state()
    pid = project_id()
    policy = Path(args.policy or state["policy_v1"])
    status = operator("integration", "status", pid)
    if status["effective_mode"] != "disabled":
        operator("integration", "enable", pid, "--mode", "disabled",
                 "--expected-generation", generation(pid), "--reason", "rebind the train policy")
        wait_for(lambda: not operator("integration", "status", pid)["draining"]
                 and operator("integration", "status", pid)["effective_mode"] == "disabled",
                 what="the drain to disabled", timeout=600, interval=10)
    operator("project", "set", pid, "integration-review-mode", "pull_request",
             "--expected-integration-generation", generation(pid),
             "--reason", "App train: epics reach main through a reviewed pull request")
    _bind_policy(pid, policy, f"App train: bind {policy.name}")
    record(args.scenario, "policy_bound", {"path": str(policy),
                                           "version": json.loads(policy.read_text())
                                           ["root"]["required_checks"]["version"]})
    if args.app_setup:
        # Runbook §9.5 switch: the operator's gh moves the version variable.
        code, text = operator_text("integration", "app-setup", pid, "--policy", str(policy),
                                   "--repository-id", pid, "--apply", timeout=300)
        (HOME / f"{args.scenario.lower()}-app-setup.txt").write_text(text + "\n")
        variables = gh_api(f"repos/{REPOSITORY}/actions/variables")
        record(args.scenario, "app_setup_apply", {
            "exit": code, "variables": {v["name"]: v["value"] for v in variables["variables"]}})
    operator("integration", "enable", pid, "--mode", "observe",
             "--expected-generation", generation(pid), "--reason", "App train: preflight")
    operator("integration", "flush", pid)

    def ready():
        status = operator("integration", "status", pid)
        return status if status.get("ready") else None

    try:
        status = wait_for(ready, what="observe ready", timeout=180, interval=10)
    except Failure:
        status = operator("integration", "status", pid)
        save_payload(f"{args.scenario.lower()}-observe-not-ready", status)
        raise Failure(f"observe not ready: blockers {status.get('blockers')}")
    save_payload(f"{args.scenario.lower()}-observe-ready", status)
    record(args.scenario, "observe_ready", {"blockers": _blocker_codes(status),
                                            "warnings": _warning_codes(status),
                                            "generation": status["generation"]})
    operator("integration", "enable", pid, "--mode", "hierarchy",
             "--expected-generation", generation(pid), "--reason", "App train: hierarchy")
    operator("integration", "enable", pid, "--mode", "train", "--interval-seconds", "60",
             "--expected-generation", generation(pid), "--reason", "App train: train")
    status = operator("integration", "status", pid)
    record(args.scenario, "train_mode", {"effective_mode": status["effective_mode"],
                                         "generation": status["generation"],
                                         "warnings": _warning_codes(status)})


def render_v2(_args) -> None:
    """S8 artifacts: the expand and contract workflows, the v2 policy and manifest.

    Expand keeps ``fixture`` and adds ``fixture (v2)``; contract keeps only the
    new job.  The planner renders the v2 policy and manifest from the expand
    tree, naming only the new check (runbook §9.5).
    """
    out = ARTIFACTS / "v2"
    out.mkdir(parents=True, exist_ok=True)
    expand = fixture_ci([CHECK_V1[1], CHECK_V2[1]])
    contract = fixture_ci([CHECK_V2[1]])
    (out / "ci-expand.yml").write_text(expand)
    (out / "ci-contract.yml").write_text(contract)
    clone = harness_clone()
    fresh_commit(clone, "S8 expand (local render only)", {".github/workflows/ci.yml": expand})
    paths = _onboard_train(clone, CHECK_V2, out)
    git(clone, "checkout", "-q", "--detach", "origin/main")
    state = load_state()
    state["policy_v2"] = str(paths["policy"])
    save_state(state)
    manifest = json.loads(paths["manifest"].read_text())
    record("S8", "render_v2", {"policy": str(paths["policy"]),
                               "manifest_required_checks": manifest["required_checks"]})


# ---------------------------------------------------------------------------
# Epics and the pool workers the harness plays
# ---------------------------------------------------------------------------


def scenario_state(state: dict, scenario: str) -> dict:
    return state.setdefault("scenarios", {}).setdefault(scenario, {})


def _review_role_body(pid: str, scenario: str, leaf_title: str) -> dict:
    """A one-leaf playbook: only its PLAYBOOK principal can file the reviewer role."""
    rule = "file-review"
    prefix = f"{rule}--"
    source = {"path": f"projects/{pid}/playbooks/app-train-review-{scenario.lower()}.md",
              "start_line": 11, "end_line": 11, "excerpt": "File one reviewer role task."}

    def literal(value: str) -> dict:
        return {"type": "literal", "value": value}

    def event(path: str) -> dict:
        return {"type": "event_ref", "path": path}

    def bound(path: str) -> dict:
        return {"type": "binding_ref", "binding": "review", "path": path}

    def template(prefix: str, path: str) -> dict:
        return {"type": "template", "parts": [literal(prefix), event(path)]}

    def command(name: str, inputs: dict, next_step: str, *, save: bool = False) -> dict:
        from src.playbooks.validation import RegistryContractLookup

        contract = RegistryContractLookup().get(name)
        check(contract is not None, f"no playbook contract for {name}")
        transitions = {
            outcome: next_step if contract.outcome_classes.get(outcome) == "success"
            else prefix + "failed" for outcome in contract.outcomes
        }
        transitions["runtime_error"] = prefix + "failed"
        return {"type": "command", "rule": rule, "title": name, "source": source,
                "command": name, "inputs": inputs, "transitions": transitions,
                **({"save_result_as": "review"} if save else {})}

    return {
        "rules": [{"id": rule, "name": rule, "source": source,
                   "trigger": {"event_type": "task.created"},
                   "guard": {"type": "comparison", "op": "eq",
                             "left": event("title"), "right": literal(leaf_title)},
                   "entry_step": prefix + "ensure"}],
        "steps": {
            prefix + "ensure": command("ensure_task", {
                "project_id": event("project_id"),
                "dedup_key": template("app-train-review:", "task_id"),
                "title": literal(f"{scenario}: review the leaf"),
                "description": template("Review the exact checkpoint head of ", "task_id"),
                "profile_id": literal(REVIEWER_PROFILE),
                "intelligence_class": literal(TRAIN_CLASS),
                "parent_id": event("parent_task_id"),
            }, prefix + "provenance", save=True),
            prefix + "provenance": command("add_dependency", {
                "task_id": bound("task_id"), "depends_on": event("task_id"),
                "dep_type": literal("discovered-from"),
            }, prefix + "done"),
            prefix + "done": {"type": "terminal", "rule": rule, "title": "Done",
                              "source": source, "outcome": "completed"},
            prefix + "failed": {"type": "terminal", "rule": rule, "title": "Failed",
                                "source": source, "outcome": "failed"},
        },
    }


def _install_review_role_playbook(pid: str, scenario: str, leaf_title: str) -> str:
    """Compile a reviewer playbook for one explicit run after graph filing."""
    from types import SimpleNamespace

    import yaml

    from src.playbooks.authoring import PlaybookSource
    from src.playbooks.definition import canonical_bytes
    from src.playbooks.proposal import propose
    from src.playbooks.validation import (RegisteredEventLookup, RegistryContractLookup,
                                          VaultProfileLookup)
    from src.profiles.parser import parse_profile, parsed_profile_to_agent_profile

    playbook_id = f"app-train-review-{scenario.lower()}"
    source_path = VAULT / "projects" / pid / "playbooks" / f"{playbook_id}.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(
        f"---\nid: {playbook_id}\nkind: pipeline\nrole: app-train-review\n"
        f"scope: project:{pid}\ntriggers:\n  - task.created\n---\n\n"
        f"# App train {scenario} reviewer\n\n"
        f"File one `reviewer` role task for the leaf titled `{leaf_title}`, "
        "then link its provenance to the leaf.\n"
    )
    source = PlaybookSource.load(source_path, vault_root=VAULT)
    check(isinstance(source, PlaybookSource), f"invalid review playbook source: {source}")
    profile_path = VAULT / "agent-types" / REVIEWER_PROFILE / "profile.md"
    profile = parse_profile(profile_path.read_text())
    check(profile.is_valid, f"invalid {REVIEWER_PROFILE} profile: {profile.errors}")
    fields = parsed_profile_to_agent_profile(profile)
    proposal = propose(
        source, _review_role_body(pid, scenario, leaf_title),
        contracts=RegistryContractLookup(),
        profiles=VaultProfileLookup({REVIEWER_PROFILE: SimpleNamespace(**fields)}),
        events=RegisteredEventLookup(), version=1, enforce_inventory=False,
    )
    blocking = [d for d in proposal.diagnostics if d.severity in {"error", "question"}]
    check(proposal.artifact is not None and not blocking,
          f"review playbook did not compile: {[d.message for d in blocking]}")
    artifact = proposal.artifact
    assert artifact is not None and proposal.artifact_sha256 is not None
    bundle = VAULT / "reviewed-playbooks" / playbook_id
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "source.md").write_text(source.raw)
    (bundle / "artifact.json").write_bytes(canonical_bytes(artifact))
    (bundle / "artifact.sha256").write_text(proposal.artifact_sha256 + "\n")
    manifest = {
        "playbook_id": playbook_id,
        "artifact_sha256": proposal.artifact_sha256,
        "source_sha256": proposal.source_digest,
        "contract_fingerprint": proposal.contract_fingerprint,
        "profiles_referenced": [REVIEWER_PROFILE],
    }
    (bundle / "manifest.md").write_text("---\n" + yaml.safe_dump(manifest)
                                        + "---\n\nApp train reviewer role task.\n")
    return str(bundle)


def _file_review_role(scenario: str, epic_id: str, leaf_id: str, bundle: str) -> str:
    """Run one playbook after graph filing to file the reviewer role."""
    def reviewer_child() -> str | None:
        response = operator("task", "children", "--task-id", epic_id)
        children = response.get("children", []) if isinstance(response, dict) else response
        reviews = [child for child in children if child.get("profile_id") == REVIEWER_PROFILE]
        check(len(reviews) <= 1, f"multiple reviewer children of {epic_id}: {reviews}")
        return reviews[0]["id"] if reviews else None

    def ensure_edges(review_id: str) -> None:
        deps = operator("task", "deps", "--task-id", review_id)
        if not any(edge.get("id") == leaf_id and edge.get("dep_type") == "discovered-from"
                   for edge in deps.get("provenance", [])):
            operator("task", "add-dependency", "--task-id", review_id,
                     "--depends-on", leaf_id, "--dep-type", "discovered-from")

    created = reviewer_child()
    if created is None:
        playbook_id = f"app-train-review-{scenario.lower()}"
        operator("playbook", "import", "--path", bundle, "--activate")
        run = operator("playbook", "run", "--playbook-id", playbook_id,
                       "--event", json.dumps({"type": "task.created", "task_id": leaf_id,
                                              "project_id": project_id(),
                                              "parent_task_id": epic_id}))
        check(run.get("status") == "completed" and not run.get("failed_steps"),
              f"review role playbook failed: {run}")
        created = wait_for(reviewer_child, what=f"{scenario} reviewer role task",
                           timeout=60, interval=1)
    check(created is not None, f"playbook created no reviewer child of {epic_id}")
    ensure_edges(created)
    return created


def create_epic(args) -> None:
    """File one epic: a leaf that changes the fixture, and a reviewer of it."""
    require_approval()
    import yaml

    state = load_state()
    pid = project_id()
    sc = scenario_state(state, args.scenario)
    if sc.get("epic_recorded") or sc.get("leaf_head"):
        print(f"{args.scenario} epic already filed: {sc['epic_id']}")
        return
    changes: dict[str, str | None] = {}
    for spec in args.write or []:
        rel, _, content = spec.partition("=")
        changes[rel] = content.replace("\\n", "\n") if content else f"{args.scenario}\n"
    for rel in args.copy or []:
        rel, _, source = rel.partition("=")
        changes[rel] = Path(source).read_text()
    for rel in args.delete or []:
        changes[rel] = None
    stamp = time.strftime("%H%M%S")
    graph = {
        "version": 1,
        "parent": {
            "title": f"App train {args.scenario} {stamp}: {args.title}",
            "description": f"App-mode train proof {args.scenario} (spec §10). {args.title}.",
        },
        "defaults": {"intelligence_class": TRAIN_CLASS, "task_type": "feature"},
        "nodes": [{"key": "leaf", "title": f"{args.scenario}: {args.title}",
                   "description": f"Change the fixture: {sorted(changes)}"}],
    }
    if not sc.get("review_id"):
        review_bundle = _install_review_role_playbook(
            pid, args.scenario, f"{args.scenario}: {sc.get('title', args.title)}")
        if not sc.get("epic_id"):
            path = HOME / f"epic-{args.scenario.lower()}.yaml"
            path.write_text(yaml.safe_dump(graph, sort_keys=False))
            code, text = operator_text("task", "create", "--project", pid, "--graph", str(path),
                                       "--dry-run")
            print(text[-1500:])
            check(code == 0, f"graph dry run failed: {text[-800:]}")
            created = operator("task", "create", "--project", pid, "--graph", str(path))
            save_payload(f"{args.scenario.lower()}-epic-created", created)
            ids = _graph_ids(created)
            sc.update(epic_id=ids["parent"], leaf_id=ids["leaf"], title=args.title,
                      changes=changes)
            save_state(state)
        # Graph filing supplies a class hint, but mandatory routing requires a
        # separate route write. Keep it resumable if the process stops here.
        if not sc.get("leaf_routed"):
            operator("task", "route", "--task-id", sc["leaf_id"],
                     "--profile-id", WORKER_PROFILE, "--intelligence-class", TRAIN_CLASS)
            sc["leaf_routed"] = True
            save_state(state)
        review_id = _file_review_role(args.scenario, sc["epic_id"], sc["leaf_id"],
                                      review_bundle)
        sc["review_id"] = review_id
        save_state(state)
    # Task ids are per database: an earlier scratch database may have left
    # the same branch name on the fixture (the 09-24 report's collision).
    # Branch materialization may already have created this graph's branches
    # at the current main SHA, so only a divergent remote head is a collision.
    base_sha = fixture_main_sha()
    collisions = [f"aq/{sc[key]}" for key in ("leaf_id", "review_id")
                  if _fixture_branch_collides(f"aq/{sc[key]}", base_sha)]
    check(not collisions, f"the fixture has divergent branch heads at {collisions}: "
                          "delete this scratch epic with --branches keep before filing again")
    record(args.scenario, "epic", {"epic": sc["epic_id"], "leaf": sc["leaf_id"],
                                   "review": sc["review_id"], "changes": sorted(changes)})
    sc["epic_recorded"] = True
    save_state(state)


def _graph_ids(created: Any) -> dict[str, str]:
    """Map graph keys to task ids from the graph create envelope."""
    data = created if isinstance(created, dict) else {}
    ids = {"parent": data.get("parent_id")}
    ids.update({node["key"]: node["task_id"] for node in data.get("nodes") or []})
    missing = sorted(key for key in ("parent", "leaf") if not ids.get(key))
    if missing:
        raise Failure(f"cannot read {missing} from the graph create result: "
                      f"{json.dumps(created)[:1500]}")
    return ids


def _fixture_branch_collides(branch: str, base_sha: str) -> bool:
    found = gh_api(f"repos/{REPOSITORY}/git/ref/heads/{branch}", check_ok=False)
    if not isinstance(found, dict) or "_status" in found:
        return False
    return (found.get("object") or {}).get("sha") != base_sha


def _pool_instance(pid: str, profile_id: str) -> dict | None:
    for row in api("pool_status", {"project_id": pid}).get("pools", []):
        if row.get("profile_id") != profile_id:
            continue
        for instance in row.get("instances", []):
            if (instance.get("project_id") in (None, pid) and instance.get("state") == "running"
                    and not instance.get("task_id")):
                return instance
    return None


def claim_as(profile_id: str, *, expect: str | None = None, timeout: float = 600) -> dict:
    """Wait for an idle pool session of *profile_id*, then claim the next task as it."""
    pid = project_id()
    deadline = time.monotonic() + timeout
    while True:
        instance = wait_for(lambda: _pool_instance(pid, profile_id),
                            what=f"an idle {profile_id} pool session", timeout=timeout,
                            interval=5)
        sid = instance["session_id"]
        token = session_token(sid)
        out = aq("task", "claim", "--next", token=token, session_id=sid, check_ok=False)
        if isinstance(out, dict) and out.get("result") == "claimed":
            task = out["task"]
            claimed = {"session_id": sid, "token": token, "task_id": task["id"],
                       "claim_epoch": out["claim_epoch"], "claim": out}
            if expect and task["id"] != expect:
                print(f"   note: {profile_id} claimed {task['id']}, expected {expect}")
            return claimed
        print(f"   {profile_id} session {sid[:8]} claim: {json.dumps(out, default=str)[:300]}")
        if time.monotonic() > deadline:
            raise Failure(f"no {profile_id} claim succeeded within {timeout}s")
        time.sleep(10)


def _workspace_path(claimed: dict) -> Path:
    claim = claimed["claim"]
    for key in ("workspace_path", "work_dir", "path"):
        if isinstance(claim.get(key), str):
            return Path(claim[key])
    workspace = claim.get("workspace") or {}
    if isinstance(workspace, dict) and workspace.get("path"):
        return Path(workspace["path"])
    for ws in api("list_workspaces", {"project_id": project_id()}).get("workspaces", []):
        if ws.get("locked_by_task_id") == claimed["task_id"]:
            return Path(ws["workspace_path"] if "workspace_path" in ws else ws["path"])
    raise Failure(f"no workspace in the claim of {claimed['task_id']}: {json.dumps(claim)[:800]}")


def worker_aq(claimed: dict, *args: str, cwd: Path | None = None, check_ok: bool = True) -> Any:
    env = dict(os.environ)
    env.update(AQ_API_TOKEN=claimed["token"], AQ_SESSION_ID=claimed["session_id"],
               AQ_CLAIM_EPOCH=str(claimed["claim_epoch"]))
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/e2e/aq.py"), "--json", *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=600, check=False,
    )
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}
    if check_ok and (proc.returncode != 0 or (isinstance(payload, dict) and payload.get("error"))):
        raise Failure(f"worker aq {' '.join(args)} exited {proc.returncode}: "
                      f"{json.dumps(payload, default=str)[:1500]} {proc.stderr[-600:]}")
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        details = error.get("details")
        return {**(details if isinstance(details, dict) else {}), "_error": error}
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def _commit_in(workspace: Path, message: str, changes: dict[str, str | None]) -> str:
    ident = ["-c", "user.name=AQ App Train worker", "-c",
             "user.email=aq-app-train-worker@example.invalid", "-c", "commit.gpgsign=false"]
    for rel, content in changes.items():
        path = workspace / rel
        if content is None:
            subprocess.run(["git", "rm", "-q", "--ignore-unmatch", rel], cwd=workspace, check=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            subprocess.run(["git", "add", rel], cwd=workspace, check=True)
    subprocess.run(["git", *ident, "commit", "-q", "-m", message], cwd=workspace, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, check=True,
                          capture_output=True, text=True).stdout.strip()


def play_leaf(args) -> None:
    require_approval()
    state = load_state()
    sc = scenario_state(state, args.scenario)
    if sc.get("leaf_head"):
        print(f"leaf already pushed at {sc['leaf_head']}")
        return
    claimed = claim_as(WORKER_PROFILE, expect=sc["leaf_id"])
    check(claimed["task_id"] == sc["leaf_id"], f"claimed {claimed['task_id']}, not the leaf")
    workspace = _workspace_path(claimed)
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=workspace, check=True,
                            capture_output=True, text=True).stdout.strip()
    head = _commit_in(workspace, f"{args.scenario}: {sorted(sc['changes'])}", sc["changes"])
    pushed = worker_aq(claimed, "git", "push", cwd=workspace)
    closed = worker_aq(claimed, "task", "close", "--outcome", "pass", "--work-outcome",
                       "shipped", "--summary", f"{args.scenario} leaf: {sorted(sc['changes'])}",
                       "--claim-epoch", str(claimed["claim_epoch"]), cwd=workspace)
    worker_aq(claimed, "session", "drain-ack", cwd=workspace, check_ok=False)
    sc.update(leaf_head=head, leaf_branch=branch, leaf_session=claimed["session_id"])
    save_state(state)
    record(args.scenario, "leaf", {"task": sc["leaf_id"], "branch": branch, "head": head,
                                   "pushed": pushed, "close": _brief(closed)})


def _brief(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    return {k: payload[k] for k in ("status", "result", "outcome", "work_outcome",
                                    "pipeline_ok", "task_id") if k in payload}


def play_review(args) -> None:
    require_approval()
    state = load_state()
    sc = scenario_state(state, args.scenario)
    if sc.get("review_receipt"):
        print(f"review already recorded: {sc['review_receipt']}")
        return
    if not sc.get("review_closed"):
        claimed = claim_as(REVIEWER_PROFILE, expect=sc["review_id"])
        check(claimed["task_id"] == sc["review_id"], f"claimed {claimed['task_id']}, not review")
        workspace = _workspace_path(claimed)
        closed = worker_aq(claimed, "task", "close", "--outcome", "pass", "--work-outcome",
                           "no-op", "--summary",
                           f"Reviewed {sc['leaf_id']} at {sc['leaf_head']}: approve.",
                           "--claim-epoch", str(claimed["claim_epoch"]), cwd=workspace)
        worker_aq(claimed, "session", "drain-ack", cwd=workspace, check_ok=False)
        sc["review_closed"] = True
        save_state(state)
        record(args.scenario, "review_close", _brief(closed))
    shown = operator("task", "show", sc["review_id"])
    checkpoint = ((shown.get("integration_delivery") or {}).get("checkpoint_sha")
                  or (shown.get("task") or {}).get("integration_delivery", {}).get("checkpoint_sha"))
    check(checkpoint, f"no checkpoint on the review task: {json.dumps(shown)[:800]}")
    receipt = operator("integration", "record-noop", sc["review_id"],
                       "--expected-head-sha", checkpoint)
    sc["review_receipt"] = receipt
    save_state(state)
    record(args.scenario, "review_noop_receipt", {"checkpoint": checkpoint, "receipt": receipt})


def play_verifier(args) -> None:
    """Claim and close a READY parent verifier, if the route reserved one."""
    require_approval()
    claimed = claim_as(WORKER_PROFILE, timeout=args.timeout)
    workspace = _workspace_path(claimed)
    closed = wait_for(
        lambda: _verifier_close(args.scenario, claimed, workspace),
        what=f"parent verifier {claimed['task_id']} to close",
        timeout=args.timeout,
        interval=10,
    )
    worker_aq(claimed, "session", "drain-ack", cwd=workspace, check_ok=False)
    record(args.scenario, "verifier", {"task": claimed["task_id"], "close": _brief(closed)})


def _verifier_close(scenario: str, claimed: dict, workspace: Path) -> dict | None:
    result = worker_aq(
        claimed, "task", "close", "--outcome", "pass", "--work-outcome", "no-op",
        "--summary", "Parent verified from its recorded evidence.",
        "--claim-epoch", str(claimed["claim_epoch"]), cwd=workspace, check_ok=False,
    )
    if (isinstance(result, dict) and result.get("status") == "COMPLETED"
            and result.get("pipeline_ok") is True):
        return result
    issues = result.get("issues") if isinstance(result, dict) else None
    # Two refusals mean the same thing here: the verifier's own aggregate proof
    # is done and the close waits for the trusted CI evidence the daemon records
    # for this generation and head.  ``awaiting_trusted_verification`` is the
    # named form of that wait and escalates on an unchanged replay (the task is
    # then flagged for an operator), which is still a wait, not a failure.
    if (isinstance(result, dict) and result.get("result") == "verification_failed"
            and isinstance(issues, list) and issues
            and all(any(token in issue for token in _VERIFIER_WAIT_REFUSALS)
                    for issue in issues)):
        record(scenario, "verifier_close_retry", {"task": claimed["task_id"],
                                                  "escalated": result.get("escalated"),
                                                  "issues": issues})
        return None
    raise Failure(f"parent verifier close failed: {json.dumps(result, default=str)[:1500]}")


def epic_pull_request(sc: dict) -> dict | None:
    shown = operator("task", "show", sc["epic_id"])
    text = json.dumps(shown)
    marker = f"github.com/{REPOSITORY}/pull/"
    if marker not in text:
        return None
    number = int(text.split(marker, 1)[1].split('"', 1)[0].split("/", 1)[0])
    return gh_api(f"repos/{REPOSITORY}/pulls/{number}")


def approve(args) -> None:
    """Approve the epic PR's exact head with the operator's account (a test action)."""
    require_approval()
    state = load_state()
    sc = scenario_state(state, args.scenario)
    pr = wait_for(lambda: epic_pull_request(sc), what=f"{sc['epic_id']}'s epic PR",
                  timeout=args.timeout, interval=15)
    head = pr["head"]["sha"]
    review = gh_api(f"repos/{REPOSITORY}/pulls/{pr['number']}/reviews", method="POST", body={
        "commit_id": head, "event": "APPROVE",
        "body": f"App-mode train proof {args.scenario}: operator-account test approval "
                "(not an independent review).",
    })
    sc.update(pr_number=pr["number"], pr_head=head, review_id=review["id"])
    save_state(state)
    record(args.scenario, "epic_pr_approved", {"pr": pr["number"], "url": pr["html_url"],
                                               "head": head, "review": review["id"],
                                               "files": [f["filename"] for f in gh_api(
                                                   f"repos/{REPOSITORY}/pulls/{pr['number']}/files")]})


def _batch_view(status: dict) -> dict:
    batch = status.get("active_batch") or {}
    keys = ("batch_id", "lifecycle", "current_revision", "candidate_sha", "integration_branch",
            "tested_candidate_sha", "final_main_sha", "cleanup_state", "state", "phase")
    view = {k: batch.get(k) for k in keys if k in batch}
    view["schedule"] = status.get("schedule")
    view["parents"] = [
        {k: p.get(k) for k in ("parent_task_id", "state", "reason", "generation", "status")
         if k in p} for p in status.get("parent_readiness") or []]
    view["repair"] = [{k: r.get(k) for k in ("task_id", "state", "stage", "revision")
                       if k in r} for r in status.get("repair") or []]
    view["promotion"] = status.get("promotion")
    view["blockers"] = _blocker_codes(status)
    return view


def watch(args) -> None:
    """Poll status, record each change, and flush once the approval armed settling."""
    pid = project_id()
    deadline = time.monotonic() + args.timeout
    last = None
    flushed = False
    released_at_start = (operator("integration", "status", pid).get("release") or {}).get("batch_id")
    while time.monotonic() < deadline:
        status = operator("integration", "status", pid)
        view = _batch_view(status)
        if view != last:
            record(args.scenario, "status", view)
            save_payload(f"{args.scenario.lower()}-status-{time.strftime('%H%M%S')}", status)
            last = view
        schedule = status.get("schedule") or {}
        if args.flush and not flushed and schedule.get("settling_fires_at"):
            operator("integration", "flush", pid)
            flushed = True
            record(args.scenario, "flush", "settling window skipped by flush")
        released = (status.get("release") or {}).get("batch_id")
        if args.until == "release-changed" and released and released != released_at_start:
            record(args.scenario, "released", status["release"])
            print(f"batch {released} released")
            return
        if args.until and args.until != "release-changed" and _reached(status, args.until):
            print(f"reached {args.until}")
            return
        time.sleep(args.interval)
    raise Failure(f"{args.scenario}: {args.until or 'watch'} not reached in {args.timeout}s")


def _reached(status: dict, until: str) -> bool:
    text = json.dumps(status)
    if until == "promoted":
        return any(p.get("state") in ("promoted", "complete") or p.get("final_main_sha")
                   for p in status.get("promotion") or [] if isinstance(p, dict)) or \
            '"lifecycle": "promoted"' in text
    if until == "released":
        return status.get("active_batch") is None and bool(status.get("release"))
    return until in text


def play_repair(args) -> None:
    """Claim the repair delegate and restore the file the candidate needs."""
    require_approval()
    state = load_state()
    sc = scenario_state(state, args.scenario)
    claimed = claim_as(WORKER_PROFILE, timeout=args.timeout)
    workspace = _workspace_path(claimed)
    changes = {"train-repaired.txt": "The integration candidate was repaired in place.\n"}
    head = _commit_in(workspace, f"{args.scenario}: repair the integration candidate", changes)
    pushed = worker_aq(claimed, "git", "push", cwd=workspace)
    closed = worker_aq(claimed, "task", "close", "--outcome", "pass", "--work-outcome",
                       "shipped", "--summary", "Restored train-repaired.txt on the candidate.",
                       "--claim-epoch", str(claimed["claim_epoch"]), cwd=workspace)
    worker_aq(claimed, "session", "drain-ack", cwd=workspace, check_ok=False)
    sc.update(repair_task=claimed["task_id"], repair_head=head)
    save_state(state)
    record(args.scenario, "repair", {"task": claimed["task_id"], "head": head,
                                     "pushed": pushed, "close": _brief(closed)})


def assert_promotion(args) -> None:
    """The attested promotion: App attestation, no bypass, main == candidate, audit green."""
    state = load_state()
    sc = scenario_state(state, args.scenario)
    candidate = args.candidate or sc.get("candidate_sha")
    check(candidate, "pass --candidate SHA (the promoted candidate)")
    main = fixture_main_sha()
    record(args.scenario, "main_equals_candidate", {"main": main, "candidate": candidate})
    check(main == candidate, f"main {main} != candidate {candidate}")
    status, runs = app_get(f"repositories/{repository_id()}/commits/{candidate}/check-runs"
                           f"?check_name={urllib.request.quote(ATTESTATION_NAME)}&filter=all")
    save_payload(f"{args.scenario.lower()}-attestation-check-runs", {"status": status,
                                                                     "body": runs})
    attestations = [r for r in runs.get("check_runs", []) if r["app"]["id"] == app_id()]
    check(attestations, f"no attestation by App {app_id()} on {candidate}")
    newest = max(attestations, key=lambda r: r["id"])
    payload_text = (newest.get("output") or {}).get("text") or ""
    import hashlib

    canonical = json.dumps(json.loads(payload_text), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    record(args.scenario, "attestation", {
        "check_run_id": newest["id"], "app_id": newest["app"]["id"],
        "conclusion": newest["conclusion"], "external_id": newest.get("external_id"),
        "canonical_text": canonical == payload_text,
        "external_id_matches": newest.get("external_id") ==
        "aq-attestation-v1:" + hashlib.sha256(payload_text.encode()).hexdigest(),
        "required_check_set_version": json.loads(payload_text).get("required_check_set_version"),
        "checks": [c.get("name") for c in json.loads(payload_text).get("checks", [])],
    })
    ruleset_id = load_state().get("ruleset_id") or train_ruleset_id()
    check(ruleset_id is not None, f"fixture has no {RULESET_NAME!r} ruleset")
    ruleset = gh_api(f"repos/{REPOSITORY}/rulesets/{ruleset_id}")
    save_payload(f"{args.scenario.lower()}-ruleset-at-promotion", ruleset)
    record(args.scenario, "ruleset_at_promotion", {"bypass_actors": ruleset.get("bypass_actors"),
                                                    "updated_at": ruleset.get("updated_at")})
    check(ruleset.get("bypass_actors") == [], "the ruleset had a bypass actor at promotion")
    run = wait_workflow_run(args.scenario, candidate, "Main attestation", timeout=1200)
    jobs = job_conclusions(run)
    check(run.get("verifier") and "NOT attested" not in run["verifier"]
          and "attested" in run["verifier"], f"Main attestation verdict: {run.get('verifier')}")
    fallback = [name for name in jobs if name.startswith("unattested-ci")]
    check(all(jobs[name] == "skipped" for name in fallback),
          f"unattested-ci ran after an attested promotion: {jobs}")
    record(args.scenario, "promotion_asserted", {"candidate": candidate, "jobs": jobs})


# ---------------------------------------------------------------------------
# S10 - break-glass
# ---------------------------------------------------------------------------


def s10(_args) -> None:
    require_approval()
    state = load_state()
    clone = harness_clone()

    step("S10: the admin adds an owner bypass, pushes unattested, removes the bypass")
    rules = target_ruleset()
    rules["bypass_actors"] = [admin_bypass_actor()]
    put_ruleset("S10", rules, "admin-bypass")
    try:
        fresh_commit(clone, "S10: break-glass change", {"s10-break-glass.txt": "break glass\n"})
        pushed = push_main(clone, "S10", "s10-break-glass")
    finally:
        put_ruleset("S10", target_ruleset(), "admin-bypass-removed")
    state["s10_sha"] = pushed["sha"]
    save_state(state)
    run = wait_workflow_run("S10", pushed["sha"], "Main attestation", timeout=1200)
    jobs = job_conclusions(run)
    record("S10", "jobs", jobs)
    check(run.get("verifier") and "NOT attested" in run["verifier"],
          f"S10: the audit did not report NOT attested: {run.get('verifier')}")
    fallback = [name for name in jobs if name.startswith("unattested-ci")]
    check(fallback and all(jobs[name] == "success" for name in fallback),
          f"S10: unattested-ci did not run the fixture CI: {jobs}")
    print("\nS10 passed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def hotfix(args) -> None:
    """Exercise an operator-configured dev -> main flow using the fixture's worker."""
    require_approval()
    pid = project_id()
    status = operator("promote", "status", "--project", pid)
    flow = status.get("flow") or []
    check(len(flow) == 1 and flow[0]["source"] == "dev" and flow[0]["target"] == "main",
          "hotfix requires the prepared one-step dev -> main promotion fixture")
    release = flow[0]
    check(release["after"]["backmerge"], "hotfix fixture requires after.backmerge enabled")
    state = load_state()
    sc = scenario_state(state, args.scenario)
    clone = harness_clone()
    if not sc.get("leaf_id"):
        cli_args = ["promote", "hotfix", "--project", pid, "--step", release["id"],
                    "--title", f"{args.scenario}: fix the released fixture"]
        if args.version:
            cli_args += ["--version", args.version]
        filed = operator(*cli_args)
        changes = {"hotfix-fixture.txt": f"{args.scenario}: released fix\n"}
        for item in args.copy or []:
            path, _, source = item.partition("=")
            changes[path] = Path(source).read_text()
        for item in args.write or []:
            path, _, content = item.partition("=")
            changes[path] = content.replace("\\n", "\n")
        sc.update(leaf_id=filed["task_id"], changes=changes, hotfix_step=release["id"])
        save_state(state)
        record(args.scenario, "hotfix_filed", filed)
    if not sc.get("leaf_routed"):
        operator("task", "route", "--task-id", sc["leaf_id"],
                 "--profile-id", WORKER_PROFILE, "--intelligence-class", TRAIN_CLASS)
        sc["leaf_routed"] = True
        save_state(state)
    if not sc.get("leaf_head"):
        play_leaf(args)
        state = load_state()
        sc = scenario_state(state, args.scenario)
    git(clone, "fetch", "--prune", "origin")
    if not sc.get("hotfix_request"):
        check(git(clone, "merge-base", "--is-ancestor", "origin/main", sc["leaf_head"],
                  check_ok=False).returncode == 0, "hotfix source does not descend from main")
        cli_args = ["promote", "request", "--project", pid, "--step", release["id"],
                    "--from-task", sc["leaf_id"], "--from", sc["leaf_head"], "--notes-reviewed"]
        if args.version:
            cli_args += ["--version", args.version]
        opened = operator(*cli_args)
        pull = gh_api(f"repos/{REPOSITORY}/pulls/{opened['promotion']['pr_number']}")
        check(pull["base"]["ref"] == "main" and pull["head"]["sha"] == sc["leaf_head"],
              f"hotfix PR is not pinned to the task head on main: {pull}")
        sc["hotfix_request"] = opened
        save_state(state)
        record(args.scenario, "hotfix_request", opened)
    opened = sc["hotfix_request"]
    if release["gate"]["approval"] != "none":
        operator("promote", "approve", opened["request_id"], "--project", pid)

    def promoted():
        value = api("promote_status", {"project_id": pid, "limit": 100})
        return next((row for row in value.get("promotions", [])
                     if row["promotion"]["request_id"] == opened["request_id"]
                     and row["lifecycle"] == "promoted"), None)

    landed = wait_for(promoted, what="hotfix promotion on main", timeout=args.timeout, interval=10)
    record(args.scenario, "hotfix_promoted", landed)
    authored = api("integration_backmerge_source", {"project_id": pid, "step_id": release["id"]})
    check(authored.get("success"), f"backmerge mechanism refused: {authored}")
    record(args.scenario, "backmerges_authored", authored)
    debts = authored.get("backmerges") or []
    for debt in debts:
        check(debt.get("pr_url"), f"lower branch source lacks its own PR: {debt}")
        pull = gh_api(f"repos/{REPOSITORY}/pulls/{debt['pr_url'].rsplit('/', 1)[1]}")
        check(pull["base"]["ref"] == "dev" and pull["head"]["sha"] == landed["promotion"]["source_sha"],
              f"backmerge PR is not on dev at the released source: {pull}")

    def contained():
        git(clone, "fetch", "--prune", "origin")
        return git(clone, "merge-base", "--is-ancestor", sc["leaf_head"], "origin/dev",
                   check_ok=False).returncode == 0

    wait_for(contained, what="hotfix source contained in dev", timeout=args.timeout, interval=10)
    record(args.scenario, "hotfix_backmerged", {"source": sc["leaf_head"], "target": "dev"})


STEPS = {
    "write-profiles": write_profiles,
    "prepare": prepare,
    "s1": s1,
    "s3": s3,
    "s2": s2,
    "s6": s6,
    "s10": s10,
    "train": enter_train,
    "render-v2": render_v2,
    "epic": create_epic,
    "leaf": play_leaf,
    "review": play_review,
    "verifier": play_verifier,
    "approve": approve,
    "watch": watch,
    "repair": play_repair,
    "assert-promotion": assert_promotion,
    "hotfix": hotfix,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=sorted(STEPS))
    parser.add_argument("--project-id", help="prepare: the scratch project id (default: fresh)")
    parser.add_argument("--scenario", default="S4", help="the scenario a train step belongs to")
    parser.add_argument("--policy", help="train: the policy file to bind (default: v1)")
    parser.add_argument("--app-setup", action="store_true",
                        help="train: run app-setup --apply for the bound policy (S8 switch)")
    parser.add_argument("--title", default="add a fixture file", help="epic: the change")
    parser.add_argument("--write", action="append", help="epic: PATH[=CONTENT] the leaf writes")
    parser.add_argument("--copy", action="append", help="epic: PATH=SOURCE the leaf writes")
    parser.add_argument("--delete", action="append", help="epic: PATH the leaf deletes")
    parser.add_argument("--until",
                        help="watch: stop at release-changed | promoted | <status text>")
    parser.add_argument("--flush", action="store_true", help="watch: flush once settling arms")
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--candidate", help="assert-promotion: the promoted candidate SHA")
    parser.add_argument("--version", help="hotfix: PATCH version prepared by --copy or --write")
    args = parser.parse_args()
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        STEPS[args.step](args)
    except (Failure, CliError) as exc:
        print(f"\nFAILED {args.step}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
