"""Deliberate policy installation/setup against real disposable daemons.

The kit builder performs installation before adding profiles/formulas. Each case
records that fixture step, then prepares its own startup state. Restarts call only
e2e-daemon.sh; they never call the builder or an installation helper.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import asyncpg
import yaml

from scripts.e2e.dbsetup import refuse
from src.playbooks.activation import profile_fingerprint
from src.playbooks.artifact_store import ArtifactStore
from src.playbooks.definition import PlaybookDefinition
from src.playbooks.required import (
    DEFAULT_SYSTEM_PLAYBOOK_IDS,
    INITIAL_SYSTEM_PLAYBOOK_IDS,
    INSTALL_RECEIPT,
    REQUIRED_SYSTEM_PLAYBOOK_IDS,
)

REPO = Path(__file__).resolve().parents[2]
INITIAL = set(INITIAL_SYSTEM_PLAYBOOK_IDS)
ACTIVE = set(REQUIRED_SYSTEM_PLAYBOOK_IDS + DEFAULT_SYSTEM_PLAYBOOK_IDS)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def inventory(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    } if root.exists() else {}


class Kit:
    def __init__(self, parent, name, evidence):
        self.home = parent / "policy-cases" / name
        self.name = name
        self.evidence = evidence
        self.env = dict(os.environ)
        self.db_name = os.environ["E2E_DB_NAME"] + "_policy_" + name.replace("-", "_")
        check(len(self.db_name) <= 63 and refuse(self.db_name) is None, "unsafe child DB name")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.env.update(
            AQ_E2E_HOME=str(self.home), AQ_E2E_PORT=str(port), AQ_E2E_API_URL=self.url,
            AQ_API_URL=self.url, AQ_E2E_TMUX_SOCKET=f"aq-policy-{port}",
            AQ_E2E_SESSION_PROVIDER="fake", E2E_DB_NAME=self.db_name,
            AQ_E2E_FORBID_LLM="1",
        )
        for key in ("AQ_E2E_CLI_SOCKET", "AQ_E2E_EXTRA_CONFIG"):
            self.env.pop(key, None)
        self.dsn = ""
        self.running = False

    def record(self, event, **data):
        self.evidence["events"].append({"case": self.name, "event": event, **data})
        if event == "snapshot":
            print(f"{self.name}: {data['label']}", flush=True)

    def run(self, argv, *, input=None, expected=0, timeout=180):
        proc = subprocess.run(
            argv, cwd=REPO, env=self.env, input=input, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        self.record("command", argv=[str(arg) for arg in argv], exit_status=proc.returncode,
                    stdout=proc.stdout, stderr=proc.stderr)
        if expected is not None:
            check(proc.returncode == expected,
                  f"{argv} exit {proc.returncode}: {proc.stdout}\n{proc.stderr}")
        return proc

    def build(self):
        self.run(["scripts/e2e-env.sh", "--reset"])
        config_path = self.home / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        check(config["data_dir"] == str(self.home), "child config escaped its home")
        self.dsn = config["database"]["url"].replace("postgresql+asyncpg:", "postgresql:")
        check(self.dsn.rsplit("/", 1)[-1] == self.db_name, "child DSN mismatch")
        # No demand scheduling or real provider probes in policy-only cases.
        config["swarm"]["enabled"] = False
        config["providers"] = {"claude": {"usage_probe_enabled": False}}
        config_path.write_text(yaml.safe_dump(config))
        snapshot = self.snapshot("builder-first-install", database=False)
        check(set(snapshot["bundles"]) == INITIAL, "builder installed extra/missing bundles")
        check(snapshot["receipt"]["activation_pending"], "builder receipt is not pending")

    def assert_owned(self):
        check((self.home / ".aq-e2e").is_file(), "fixture mutation needs kit marker")
        check(self.home.resolve().parent.name == "policy-cases", "unexpected child home")
        check(refuse(self.db_name) is None, "fixture mutation needs disposable DB")

    def start(self):
        self.assert_owned()
        # Mark before launch so a failed readiness gate still cleans up our daemon.
        self.running = True
        self.run(["scripts/e2e-daemon.sh", "start"])
        deadline = time.monotonic() + 60
        while self.receipt().get("activation_pending") and time.monotonic() < deadline:
            time.sleep(0.2)
        check(not self.receipt().get("activation_pending"), "activation attempt never consumed")

    def stop(self):
        if self.running:
            self.run(["scripts/e2e-daemon.sh", "stop"])
            self.running = False
        check(not (self.home / "llm-forbidden.jsonl").exists(), "unexpected LLM attempt")

    def receipt(self):
        path = self.home / INSTALL_RECEIPT
        return json.loads(path.read_text()) if path.exists() else None

    def sql(self, query, *args):
        self.assert_owned()

        async def execute():
            conn = await asyncpg.connect(self.dsn)
            try:
                return [dict(row) for row in await conn.fetch(query, *args)]
            finally:
                await conn.close()

        return asyncio.run(execute())

    def rows(self):
        return self.sql("SELECT * FROM playbook_activations ORDER BY activation_id")

    def snapshot(self, label, *, database=True):
        vault = self.home / "vault"
        bundles = vault / "reviewed-playbooks"
        result = {
            "label": label, "home": str(self.home), "database": self.db_name,
            "url": self.url, "receipt": self.receipt(),
            "sources": inventory(vault / "system/playbooks"),
            "bundles": sorted(path.name for path in bundles.iterdir() if path.is_dir())
            if bundles.exists() else [],
            # The generated root Obsidian hub can change when a directory is
            # removed. Only files inside bundle directories are policy bytes.
            "bundle_files": {path: digest for path, digest in inventory(bundles).items()
                             if "/" in path},
            "activations": self.rows() if database else [],
        }
        self.record("snapshot", **result)
        return result

    def api(self, command, args):
        req = urllib.request.Request(
            self.url + "/api/execute", method="POST",
            data=json.dumps({"command": command, "args": args}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.load(response)
        self.record("http-command", command=command, args=args, response=result)
        check(result.get("ok"), f"{command}: {result}")
        result = result["result"]
        check(result.get("success", True), f"{command}: {result}")
        return result

    def cli(self, *args, input=None, expected=0, json_mode=False):
        env_before = self.env
        # Like smoke._cli_env: only these child CLI processes use kit-local scope.
        self.env = dict(env_before)
        for key in ("AQ_API_TOKEN", "AQ_SESSION_ID", "AQ_TASK_ID", "AQ_CLAIM_EPOCH"):
            self.env.pop(key, None)
        self.env.update(AGENT_QUEUE_DATA=str(self.home), AQ_DATABASE_URL=self.dsn,
                        AGENT_QUEUE_DB=self.dsn)
        try:
            return self.run(
                [sys.executable, "scripts/e2e/aq.py", *(["--json"] if json_mode else []), *args],
                input=input, expected=expected,
            )
        finally:
            self.env = env_before

    def doctor(self, identifier, *, fix=False):
        proc = self.cli("doctor", "--check", identifier, *(["--fix"] if fix else []),
                        expected=None, json_mode=True)
        payload = json.loads(proc.stdout)
        check(proc.returncode in (0, 1, 2) and payload["data"]["success"]
              and payload["data"]["exit_code"] == proc.returncode,
              f"doctor infrastructure error: {proc.stdout}\n{proc.stderr}")
        self.record("doctor", identifier=identifier, fix=fix, result=payload)
        if fix:
            check(proc.returncode == 0, f"explicit repair did not restore requested guarantee: {payload}")
        return payload

    def health(self):
        proc = self.cli("playbook", "activation-health", json_mode=True)
        return json.loads(proc.stdout)["data"]["activations"]


def first_start(kit, empty=False):
    kit.build()
    kit.assert_owned()
    shutil.rmtree(kit.home / "vault")
    (kit.home / INSTALL_RECEIPT).unlink()
    if empty:
        (kit.home / "vault").mkdir()
    kit.record("startup-input", vault="empty" if empty else "absent", receipt="absent")
    kit.start()
    # /ready's database check can succeed before late initial activation ends.
    deadline = time.monotonic() + 60
    while {row["playbook_id"] for row in kit.rows()} != ACTIVE and time.monotonic() < deadline:
        time.sleep(0.2)
    snapshot = kit.snapshot("first-start-complete")
    check(set(snapshot["bundles"]) == INITIAL, "startup installed extra/missing bundles")
    check(set(snapshot["sources"]) == {identifier + ".md" for identifier in INITIAL},
          "startup source inventory differs from six-item contract")
    check({row["playbook_id"] for row in snapshot["activations"]} == ACTIVE,
          "required/default activation inventory differs")
    check(all(row["enabled"] for row in snapshot["activations"]), "initial activation disabled")
    check(not snapshot["receipt"]["activation_pending"], "receipt not consumed")
    health = kit.health()
    check(all(row["health"] == "ready" for row in health), f"initial unhealthy policy: {health}")
    return snapshot


def restart_preservation(kit):
    baseline = kit.snapshot("restart-baseline")
    receipt = (kit.home / INSTALL_RECEIPT).read_bytes()
    kit.stop()
    backup = kit.home / "saved-vault"
    shutil.copytree(kit.home / "vault", backup)
    for case, target in (
        ("source-deleted", "system/playbooks/provider-usage-probe.md"),
        ("bundle-deleted", "reviewed-playbooks/morning-report"),
        ("vault-deleted", "."),
    ):
        vault = kit.home / "vault"
        if case != "source-deleted":
            shutil.rmtree(vault)
            shutil.copytree(backup, vault)
        path = vault / target
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        before = kit.snapshot(case + "-before")
        kit.start()
        after = kit.snapshot(case + "-after")
        check(after["sources"] == before["sources"] and after["bundle_files"] == before["bundle_files"],
              case + " reinstalled policy")
        check(after["activations"] == baseline["activations"], case + " changed activations")
        check((kit.home / INSTALL_RECEIPT).read_bytes() == receipt, case + " changed receipt")
        kit.stop()
    shutil.rmtree(kit.home / "vault")
    shutil.copytree(backup, kit.home / "vault")


def explicit_repairs(kit):
    # Use the same stale profile fingerprint fixture as test_required_playbooks.
    store = ArtifactStore(str(kit.home / "compiled"))
    original = next(row for row in kit.rows() if row["playbook_id"] == "default-assignment-routing")
    body = store.load(original["active_artifact_sha256"]).model_dump(mode="json")
    body["compiled_against"]["profiles"]["playbook-compiler"] = "sha256:" + "d" * 64
    definition = PlaybookDefinition.model_validate(body)
    fingerprint = profile_fingerprint(dict(definition.compiled_against.profiles))
    ref = store.put(
        definition, source_digest=definition.source_hash,
        contract_fingerprint=definition.contract_fingerprint(), profile_fingerprint=fingerprint,
        compiler_build=definition.compiler_build, version=definition.version,
    )
    kit.sql("""INSERT INTO playbook_artifacts SELECT $1, playbook_id, scope, scope_identifier,
        schema_generation, version, source_digest, contract_fingerprint, $2, compiler_build,
        $3, $4, validation, compiled_at, created_at FROM playbook_artifacts
        WHERE artifact_sha256=$5 RETURNING artifact_sha256""",
        ref.artifact_sha256, fingerprint, store.path_for(ref.artifact_sha256),
        len(store.canonical_bytes(definition)), original["active_artifact_sha256"])
    kit.sql("""UPDATE playbook_activations SET active_artifact_sha256=$1
        WHERE playbook_id='default-assignment-routing'""", ref.artifact_sha256)
    kit.sql("DELETE FROM playbook_activations WHERE playbook_id IN "
            "('provider-failover', 'supervisor-failure-triage')")
    kit.sql("UPDATE playbook_activations SET enabled=false "
            "WHERE playbook_id IN ('provider-usage-probe', 'default-pipeline')")
    root = kit.home / "vault/reviewed-playbooks"
    # One installed optional bundle drifts; another is deliberately uninstalled.
    (root / "default-pipeline/artifact.sha256").write_text("sha256:" + "0" * 64)
    note = root / "default-pipeline/operator-note.txt"
    note.write_text("keep this operator note\n")
    shutil.rmtree(root / "morning-report")
    source = kit.home / "vault/system/playbooks/default-assignment-routing.md"
    source.write_text("operator-owned routing source\n")
    before = kit.snapshot("missing-disabled-stale-before-restart")
    kit.start()
    check(kit.rows() == before["activations"], "restart repaired fixture activations")
    health = {row["playbook_id"]: row for row in kit.health()}
    check(health["default-assignment-routing"]["health"] == "stale_contract", "stale fixture not stale")
    check(health["provider-usage-probe"]["enabled"] is False, "disabled fixture not disabled")
    for identifier in ("playbooks.required", "playbooks.reviewed_bundles"):
        kit.doctor(identifier)
        after = kit.snapshot(identifier + "-read-only")
        check(after["activations"] == before["activations"], "doctor read mutated activations")
        check(after["bundle_files"] == before["bundle_files"] and after["sources"] == before["sources"],
              "doctor read mutated files")
    optional = [row for row in kit.rows() if row["playbook_id"] not in REQUIRED_SYSTEM_PLAYBOOK_IDS]
    kit.doctor("playbooks.required", fix=True)
    required_repair = kit.snapshot("required-repair")
    check(required_repair["bundle_files"] == before["bundle_files"], "required repair refreshed optional bundle")
    check([row for row in kit.rows() if row["playbook_id"] not in REQUIRED_SYSTEM_PLAYBOOK_IDS] == optional,
          "required repair changed optional activations")
    check({row["playbook_id"] for row in kit.rows() if row["enabled"]} == set(REQUIRED_SYSTEM_PLAYBOOK_IDS),
          "required repair enabled extra/missing policy")
    repaired_rows = kit.rows()
    kit.doctor("playbooks.reviewed_bundles", fix=True)
    after = kit.snapshot("reviewed-bundle-repair")
    check(kit.rows() == repaired_rows, "bundle repair changed healthy/optional activations")
    check("morning-report" not in after["bundles"], "bundle repair installed an optional bundle")
    check(note.read_text() == "keep this operator note\n", "operator note lost")
    backups = list(root.glob("default-pipeline.bak-*"))
    check(len(backups) == 1 and (backups[0] / "artifact.sha256").read_text() == "sha256:" + "0" * 64,
          "superseded bundle not retained")
    check(source.read_text() == "operator-owned routing source\n", "repair replaced operator source")
    result = kit.doctor("playbooks.required")
    check(result["data"]["exit_code"] == 0, "required health not restored")


def existing_vault(kit):
    kit.build()
    shutil.rmtree(kit.home / "vault")
    (kit.home / INSTALL_RECEIPT).unlink()
    source = kit.home / "vault/system/playbooks/operator.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nid: operator\nscope: system\n---\n# Existing operator policy\n")
    # Include a stale reviewed copy: existing vaults receive neither migration nor activation.
    destination = kit.home / "vault/reviewed-playbooks/morning-report"
    shutil.copytree(REPO / "src/prompts/reviewed_playbooks/morning-report", destination)
    (destination / "artifact.sha256").write_text("sha256:" + "0" * 64)
    before = kit.snapshot("existing-nonempty-no-receipt", database=False)
    kit.start()
    after = kit.snapshot("existing-after-start")
    check(after["sources"] == before["sources"] and after["bundle_files"] == before["bundle_files"],
          "existing vault migrated")
    check(after["activations"] == [] and after["receipt"]["activation_pending"] is False,
          "existing vault activated policy")
    kit.stop()


def consumed_failure(kit):
    kit.build()
    bundle = kit.home / "vault/reviewed-playbooks/default-assignment-routing"
    (bundle / "artifact.sha256").write_text("sha256:" + "0" * 64)
    kit.start()
    deadline = time.monotonic() + 60
    expected = ACTIVE - {"default-assignment-routing"}
    while {row["playbook_id"] for row in kit.rows()} != expected and time.monotonic() < deadline:
        time.sleep(0.2)
    failed = kit.snapshot("consumed-first-attempt-failure")
    check({row["playbook_id"] for row in failed["activations"]} == expected,
          "failure did not preserve independent initial activations")
    receipt = (kit.home / INSTALL_RECEIPT).read_bytes()
    kit.stop()
    # Even restoring valid bytes is not authority for another initial attempt.
    shutil.copy2(REPO / "src/prompts/reviewed_playbooks/default-assignment-routing/artifact.sha256",
                 bundle / "artifact.sha256")
    kit.start()
    check(kit.rows() == failed["activations"], "restart retried consumed initial activation")
    kit.doctor("playbooks.required")
    check(kit.rows() == failed["activations"], "doctor read retried initial activation")
    kit.doctor("playbooks.required", fix=True)
    repaired = kit.snapshot("consumed-failure-explicit-repair")
    check({row["playbook_id"] for row in repaired["activations"]} == ACTIVE, "explicit recovery failed")
    check([row for row in repaired["activations"] if row["playbook_id"] != "default-assignment-routing"]
          == failed["activations"], "explicit recovery changed independent rows")
    check((kit.home / INSTALL_RECEIPT).read_bytes() == receipt, "explicit recovery changed receipt")
    kit.stop()


def project_setup(kit):
    existing = kit.sql("SELECT * FROM projects ORDER BY id")
    existing_files = inventory(kit.home / "vault/projects")
    activations = kit.rows()
    export_path = kit.home / "exported.aqpolicy"
    args = {"project_id": "e2e", "path": str(export_path)}
    preview = kit.api("policy_export", args)
    check(not export_path.exists(), "export preview wrote archive")
    kit.api("policy_export", {**args, "expected_checksum": preview["checksum"]})

    for command in ("create", "onboard"):
        for choice in ("default", "exported", "none", "omitted", "cancel"):
            pid = f"policy-{command}-{choice}"
            options = ["project", command]
            if command == "create":
                options += ["--name", pid]
            else:
                options += ["--project-name", pid, "--project-id", pid, "--root-id", "e2e-onboarding",
                            "--relative-path", pid, "--source-mode", "init", "--no-create-github"]
            profile = str(export_path) if choice == "exported" else "default" if choice == "cancel" else choice
            if choice != "omitted":
                options += ["--from-policy", profile]
            if choice in ("none", "omitted"):
                kit.cli(*options, json_mode=True)
                check(not (kit.home / "vault/projects" / pid / "policy").exists(), "none applied policy")
                continue
            # Obtain item order/placeholder values through the live preview on an existing target.
            request = {"project_id": "e2e", **({"path": profile} if choice == "exported" else {"builtin": "default"})}
            diff = kit.api("policy_diff", request)
            responses = [pid if p["name"] == "project_id" else diff["values"][p["name"]]
                         for p in diff["placeholders"]]
            responses += ["skip" if row["original_scope"] == "system" else "project" for row in diff["items"]]
            responses += ["n" if choice == "cancel" else "y"]
            proc = kit.cli(*options, input="\n".join(responses) + "\n", expected=1 if choice == "cancel" else 0)
            check("Import the selected policy items?" in proc.stdout, "creation skipped selection acknowledgement")
            check(any(row["id"] in proc.stdout for row in diff["items"]), "creation omitted preview")
            project_rows = kit.sql("SELECT * FROM projects WHERE id=$1", pid)
            check(len(project_rows) == 1, "project lost after creation/cancellation")
            root = kit.home / "vault/projects" / pid
            if choice == "cancel":
                check(not (root / "policy").exists(), "cancel wrote policy")
                continue
            check(kit.rows() == activations, "project policy import activated playbooks")
            if choice == "exported":
                check(any((root / "policy").rglob("*.json")), "exported policy did not stage configuration")
            if choice == "default":
                check((root / "templates/spec/default.md").exists(), "default spec template missing")
                receipts = sorted((root / "policy/pending-playbooks").glob("*/review.json"))
                check(len(receipts) == 2, "default playbooks not submitted for review")
                check(kit.rows() == activations, "import activated playbooks")
                for path in receipts:
                    approved = kit.api("review_decide", {
                        "review_id": json.loads(path.read_text())["review_id"],
                        "revision": 1, "decision": "approve",
                    })
                    check(approved["playbook"]["stored"] and not approved["playbook"]["activated"],
                          "approval activated a staged playbook")
                check(kit.rows() == activations, "approval changed activations")
    after_existing = kit.sql("SELECT * FROM projects WHERE id=ANY($1::text[]) ORDER BY id",
                             [row["id"] for row in existing])
    check(after_existing == existing, "existing project configuration changed retroactively")
    after_files = inventory(kit.home / "vault/projects")
    check(all(after_files.get(path) == digest for path, digest in existing_files.items()),
          "existing project files changed retroactively")
    check(not (kit.home / "vault/projects/e2e/templates/spec/default.md").exists(),
          "existing project received default template")
    kit.record("existing-projects-preserved", before=existing, after=after_existing,
               files_before=existing_files, activations_after=kit.rows())
    kit.snapshot("project-setup-complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    parent = Path(os.environ["AQ_E2E_HOME"]).resolve()
    check((parent / ".aq-e2e").is_file(), "run e2e-env.sh first in a unique marked home")
    check(parent != Path.home() / ".agent-queue", "operator home refused")
    output = args.output or parent / "policy-acceptance.json"
    evidence = {
        "tested_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "scenario_files": inventory(REPO / "scripts/e2e"), "events": [], "passed": False,
    }
    kits = [Kit(parent, name, evidence) for name in ("absent", "empty", "existing", "failed")]
    try:
        first_start(kits[0])
        project_setup(kits[0])
        restart_preservation(kits[0])
        explicit_repairs(kits[0])
        kits[0].stop()
        first_start(kits[1], empty=True)
        kits[1].stop()
        existing_vault(kits[2])
        consumed_failure(kits[3])
        evidence["passed"] = True
        print("PASS policy acceptance: absent/empty/existing/failure, restarts, repairs, create/onboard")
    except Exception:
        evidence["failure"] = traceback.format_exc()
        print(evidence["failure"], file=sys.stderr)
    finally:
        for kit in kits:
            try:
                kit.stop()
            except Exception:
                evidence.setdefault("cleanup_failures", []).append(traceback.format_exc())
                evidence["passed"] = False
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2, default=str) + "\n")
        print(f"Evidence: {output}")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
