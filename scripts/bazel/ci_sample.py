#!/usr/bin/env python3
"""Run and package exact-commit shadow samples; only for isolated hosted runners.

Shared workstations use aq test. All output/cache directories must be outside
this checkout. No timestamp, commit SHA or workflow ID is added to action inputs.
Use `window --commits commits.json --samples retained --output window.json` to
recompute counters from oldest-first first-parent dev commits and retained reports.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import shlex
import shutil
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ci_evidence as evidence  # noqa: E402
import compare_results as parity  # noqa: E402

PLAN = ROOT / "tools/bazel/test_suites.json"
DASHBOARD = ("dashboard_typecheck", "dashboard_build")
EXTRA_TARGETS = ("//tools/bazel:dashboard_check", "//tools/bazel:generated_check")


def suite_names(plan):
    return sorted(k for k, v in plan["suites"].items() if v.get("command")) + list(DASHBOARD)


def plan_document():
    plan = evidence.read(PLAN)
    for name in DASHBOARD:
        plan["suites"][name] = {"kind": "dashboard", "marker": "",
                                "artifacts_required": name == "dashboard_build"}
    return plan


async def command(args, output, *, cwd=ROOT, env=None):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with output.open("wb") as log:
        process = await asyncio.create_subprocess_exec(*map(str, args), cwd=cwd, env=env,
                                                       stdout=log, stderr=asyncio.subprocess.STDOUT)
        try:
            code = await process.wait()
        except BaseException:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
            raise
    result = {"exit_status": code, "seconds": time.monotonic() - started,
              "command": list(map(str, args))}
    evidence.write(output.with_suffix(output.suffix + ".json"), result)
    return result


async def git(*args):
    process = await asyncio.create_subprocess_exec("git", *args, cwd=ROOT,
                                                   stdout=asyncio.subprocess.PIPE)
    output, _ = await process.communicate()
    if process.returncode:
        raise ValueError("git identity/history unavailable")
    return output.decode().strip()


async def context(sha):
    if await git("rev-parse", "HEAD") != sha:
        raise ValueError("checkout does not match exact sample SHA")
    paths = (await git("ls-files", "*BUILD.bazel", "*.bzl", "REPO.bazel")).splitlines()

    def combined(names):
        return hashlib.sha256(json.dumps({name: evidence.digest(ROOT / name)
                                         for name in names}, sort_keys=True).encode()).hexdigest()

    return {"sha": sha, "platform": os.environ.get("RUNNER_OS", sys.platform) + "/" +
            os.environ.get("RUNNER_ARCH", "unknown") + "/" + os.environ.get("ImageOS", "unknown"),
            "toolchain_lock_sha256": evidence.digest(ROOT / "tools/bazel/toolchains.lock.json"),
            "dependency_lock_sha256": combined(["MODULE.bazel.lock", "package-lock.json",
                                               "tools/bazel/requirements_linux_x86_64.txt"]),
            "environment_sha256": combined(["tools/bazel/test_environment.py",
                                            "tools/bazel/pytest_main.py",
                                            "tools/bazel/runtime-contract.md"]),
            "graph_sha256": combined(paths)}


async def initialize(output, sha, names):
    output.mkdir(parents=True, exist_ok=True)
    evidence.write(output / "plan.json", plan_document())
    for name in ("MODULE.bazel.lock", ".bazelversion", "tools/bazel/toolchains.lock.json",
                 "tools/bazel/requirements_linux_x86_64.txt", "package-lock.json"):
        target = output / "locks" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return {"schema_version": 1, "context": await context(sha),
            "suite_manifest": evidence.reference(output, output / "plan.json"),
            "required_suites": names, "suites": {}}


def expected(plan, suite):
    return parity.expected_nodes(plan, plan["suites"][suite])


def legacy_arguments(suite, plan, output):
    args = shlex.split(plan["suites"][suite]["command"])
    args = [arg for arg in args if not arg.startswith("--junitxml=")]
    args += ["-p", "tools.bazel.pytest_plugin", "--aq-outcomes", str(output / "pytest.json"),
             "--junitxml=" + str(output / "test.xml")]
    return args


async def legacy(output, sha, suite):
    plan = plan_document()
    if suite not in suite_names(plan):
        raise ValueError("unknown legacy suite")
    manifest = await initialize(output, sha, [suite])
    if suite in DASHBOARD:
        kind = suite.removeprefix("dashboard_")
        result = await command(["npm", "run", kind], output / "command.log", cwd=ROOT / "dashboard")
        artifacts = evidence.file_manifest(ROOT / "dashboard/dist") if kind == "build" else []
        evidence.write(output / "artifacts.json", artifacts)
        manifest["suites"][suite] = {"marker": "", "exit_status": result["exit_status"],
                                     "artifacts": evidence.reference(output, output / "artifacts.json")}
    else:
        result = await command(legacy_arguments(suite, plan, output), output / "command.log")
        manifest["suites"][suite] = {"marker": plan["suites"][suite]["marker"], "targets": [{
            "id": "legacy/" + suite, "expected_nodes": expected(plan, suite),
            "shard_count": 1, "shards": [{"index": 0,
                "sidecar": evidence.reference(output, output / "pytest.json"),
                "junit": evidence.reference(output, output / "test.xml")}]}]}
    manifest["invocation"] = result
    evidence.write(output / "manifest.json", manifest)
    return result["exit_status"]


def package_bazel(output, manifest, plan, events, bazel_bin):
    retained = evidence.retain_test_outputs(events, output)
    for suite in manifest["required_suites"]:
        if suite in DASHBOARD:
            kind = suite.removeprefix("dashboard_")
            tree = bazel_bin / f"dashboard/{kind}.tree"
            result = evidence.read(tree / "result.json")
            if result != {"command": kind, "success": True}:
                raise ValueError("missing dashboard command result")
            artifacts = evidence.file_manifest(tree / "dist") if kind == "build" else []
            evidence.write(output / f"{suite}.json", artifacts)
            manifest["suites"][suite] = {"marker": "", "exit_status": 0,
                "artifacts": evidence.reference(output, output / f"{suite}.json")}
            continue
        targets = []
        for module, indices in plan["suites"][suite]["modules"].items():
            label = plan["modules"][module]["targets"][suite]
            shards = retained.get(label, [])
            targets.append({"id": label,
                "expected_nodes": [plan["collection"]["nodes"][i]["nodeid"] for i in indices],
                "shard_count": plan["modules"][module].get("shard_count", 1),
                "shards": [{"index": shard["index"],
                    "sidecar": evidence.reference(output, shard["files"]["pytest.json"]),
                    "junit": evidence.reference(output, shard["files"]["test.xml"]),
                    "lifecycle": evidence.reference(output, shard["files"]["target.json"])}
                    for shard in shards]})
        manifest["suites"][suite] = {"marker": plan["suites"][suite]["marker"], "targets": targets}
    evidence.write(output / "manifest.json", manifest)


async def bazel(output, sha, state, cache):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("direct all-suite Bazel sampling requires an isolated hosted CI runner")
    plan = plan_document()
    names = suite_names(plan)
    targets = sorted({t for name in names if name not in DASHBOARD
                      for t in plan["suites"][name]["targets"]} | set(EXTRA_TARGETS))
    reports = {}
    for phase in evidence.PHASES:
        phase_output = output / phase
        manifest = await initialize(phase_output, sha, names)
        root = state / ("incremental" if phase == "incremental" else "repeat")
        disk = cache if phase == "incremental" else state / "cold-disk-cache"
        startup = ["bazel", "--output_user_root=" + str(root)]
        # clean --expunge discards server and output state but leaves the separate disk cache.
        if phase == "server_discard":
            clean = await command(startup + ["clean", "--expunge"], phase_output / "discard.log")
            if clean["exit_status"]:
                raise ValueError("could not discard Bazel server/output state")
        result = await command(startup + ["mod", "deps", "--lockfile_mode=error"],
                               phase_output / "mod-deps.log")
        if result["exit_status"]:
            raise ValueError("locked dependency resolution failed")
        bep = phase_output / "bep.jsonl"
        result = await command(startup + ["test", "--config=shadow", "//checks:all", "--keep_going",
            "--disk_cache=" + str(disk), "--remote_cache=", "--remote_executor=",
            "--cache_test_results=yes", "--flaky_test_attempts=1", "--runs_per_test=1",
            "--nozip_undeclared_test_outputs", "--build_event_json_file=" + str(bep),
            "--build_event_publish_all_actions", "--profile=" + str(phase_output / "profile.json.gz")],
            phase_output / "test.log")
        events = [json.loads(line) for line in bep.read_text().splitlines() if line.strip()]
        report = evidence.bep_report(events, targets,
            prior_eligible_actions=reports.get("cold", {}).get("eligible_actions"))
        report.update(result)
        reports[phase] = report
        evidence.write(phase_output / "execution.json", report)
        # Resolve outputs without checkout convenience symlinks (.bazelrc disables them).
        info = await command(startup + ["info", "bazel-bin"], phase_output / "bazel-bin.log")
        if info["exit_status"]:
            raise ValueError("Bazel output tree unavailable")
        bin_path = Path((phase_output / "bazel-bin.log").read_text().strip().splitlines()[-1])
        manifest["invocation"] = {"phase": phase, **result}
        try:
            package_bazel(phase_output, manifest, plan, events, bin_path)
        except (ValueError, OSError, KeyError) as exc:
            evidence.write(phase_output / "packaging-error.json", {"error": str(exc)})
            report["complete"] = report["all_tests_cached"] = False
            report["issues"].append("missing or invalid retained artifacts: " + str(exc))
            evidence.write(phase_output / "execution.json", report)
    evidence.write(output / "executions.json", reports)
    return int(any(r["exit_status"] or not r["complete"] for r in reports.values()))


def compare_suite(legacy_root, bazel_root, suite, output):
    """Project one suite, preserving result bytes and the original manifests."""
    roots = []
    for label, root in (("legacy", legacy_root), ("bazel", bazel_root)):
        projected = output / label
        manifest = evidence.read(root / "manifest.json")
        references = [manifest["suite_manifest"]]
        value = manifest["suites"][suite]
        if "artifacts" in value:
            references.append(value["artifacts"])
        else:
            for target in value["targets"]:
                for shard in target["shards"]:
                    references.extend(shard[key] for key in ("junit", "sidecar", "lifecycle")
                                      if key in shard)
        for ref in references:
            # Validate path and hash before linking; do not trust downloaded manifests.
            parity.artifact(root, ref)
            target = projected / ref["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / ref["path"], target)

        manifest["required_suites"] = [suite]
        manifest["suites"] = {suite: manifest["suites"][suite]}
        evidence.write(projected / "manifest.json", manifest)
        roots.append(projected)
    return parity.compare(*roots)


async def sample(output, sha, inputs):
    plan = plan_document()
    suites = suite_names(plan)
    current_context = await context(sha)
    reports = {}
    executions_path = inputs / "bazel/executions.json"
    executions = evidence.read(executions_path) if executions_path.is_file() else {}
    for suite in suites:
        comparisons, issues = {}, []
        try:
            invocation = evidence.read(inputs / ("legacy-" + suite) / "manifest.json")["invocation"]
            if type(invocation.get("exit_status")) is not int or invocation["exit_status"] != 0:
                issues.append("legacy command failed or exit status missing")
        except (OSError, ValueError, KeyError) as exc:
            issues.append(f"legacy invocation unavailable: {exc}")
        digests = []
        for phase in evidence.PHASES:
            try:
                scratch = output / "comparisons" / suite / phase
                result = compare_suite(inputs / ("legacy-" + suite), inputs / "bazel" / phase,
                                       suite, scratch)
                comparisons[phase] = result
                if not result["success"]:
                    issues.append(f"{phase}: parity failed or missing")
                for backend in ("legacy", "bazel"):
                    candidate = result.get("contexts", {}).get(backend, {})
                    if any(candidate.get(key) != value for key, value in current_context.items()):
                        issues.append(f"{phase}: {backend} evidence is not this candidate")
                digests.append(result.get("result_digests", {}).get("bazel"))
            except (OSError, ValueError, KeyError) as exc:
                issues.append(f"{phase}: {exc}")
            execution = executions.get(phase, {})
            if not execution.get("complete") or execution.get("exit_status") != 0:
                issues.append(f"{phase}: incomplete or failed execution")
            if phase in ("warm", "server_discard") and not execution.get("all_tests_cached"):
                issues.append(f"{phase}: unchanged tests executed or cache evidence missing")
        if len(digests) != len(evidence.PHASES) or any(d != digests[0] for d in digests):
            issues.append("cached result bytes changed or missing")
        reports[suite] = {"success": not issues, "issues": issues, "comparisons": comparisons}
    trusted = (os.environ.get("GITHUB_EVENT_NAME") == "push" and
               os.environ.get("GITHUB_REF") == "refs/heads/dev" and
               os.environ.get("GITHUB_REF_PROTECTED") == "true")
    document = {"schema_version": 1, "observational": True, "sha": sha,
                "context": current_context, "change": await change_report(sha),
                "event": os.environ.get("GITHUB_EVENT_NAME"),
                "ref": os.environ.get("GITHUB_REF"), "trusted": trusted,
                "run_id": os.environ.get("GITHUB_RUN_ID"),
                "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "suites": reports, "executions": executions,
                "cache": evidence.read(inputs / "bazel/cache.json")
                         if (inputs / "bazel/cache.json").is_file() else None,
                "success": all(r["success"] for r in reports.values())}
    evidence.write(output / "sample.json", document)
    summary = summary_markdown(document)
    (output / "summary.md").write_text(summary, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(summary)
    return int(not document["success"])


def summary_markdown(document):
    lines = [f"Shadow sample `{document['sha']}` (observational)", "",
             "| Phase | Wall seconds | Executed / eligible targets | Actions | Cached tests |",
             "| --- | ---: | ---: | ---: | --- |"]
    for phase in evidence.PHASES:
        row = document["executions"].get(phase, {})
        lines.append(f"| {phase} | {row.get('seconds')} | "
                     f"{row.get('executed_targets')} / {row.get('eligible_targets')} | "
                     f"{row.get('executed_actions')} / {row.get('eligible_actions')} | "
                     f"{row.get('cache_sources')} |")
    lines += ["", f"Change: {document['change']['category']}", "",
              "| Suite | Complete paired evidence |", "| --- | --- |"]
    lines += [f"| {name} | {row['success']} |" for name, row in document["suites"].items()]
    for phase, row in document["executions"].items():
        lines += ["", f"Five slowest targets ({phase}; maximum shard seconds):", ""]
        lines += [f"- `{r['label']}`: {r['seconds']}s" for r in row.get("five_slowest", [])]
    lines += ["", "Cache transfer, provider usage/limits and raw receipts: retained artifacts.", ""]
    return "\n".join(lines)


async def change_report(sha):
    """Name changed global inputs; never infer a measured dependency closure."""
    try:
        parent = await git("rev-parse", sha + "^1")
        paths = (await git("diff", "--name-only", parent, sha)).splitlines()
    except ValueError:
        return {"category": "unknown", "reason": "first parent unavailable"}
    global_inputs = {".bazelrc", ".bazelversion", "MODULE.bazel", "MODULE.bazel.lock",
        "package-lock.json", "tools/bazel/toolchains.lock.json",
        "tools/bazel/requirements_linux_x86_64.txt", "tools/bazel/pytest.bzl",
        "tools/bazel/pytest_main.py", "tools/bazel/test_environment.py",
        "tools/bazel/native_toolchain.bzl", "tools/bazel/runtime.py",
        "tools/bazel/pytest_plugin.py"}
    reasons = sorted(set(paths) & global_inputs)
    return {"parent_sha": parent, "paths": paths, "global_inputs": reasons,
            "category": "global inputs changed" if reasons else "source or graph edit",
            "interpretation": "compare measured incremental fractions; no fanout waiver"}


class TokenScopedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise ValueError("refusing non-HTTPS artifact redirect")
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        if urllib.parse.urlparse(newurl).netloc != "api.github.com":
            redirected.remove_header("Authorization")
        return redirected


def github_api(path):
    repository = os.environ["GITHUB_REPOSITORY"]
    request = urllib.request.Request("https://api.github.com/repos/" + repository + path,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"})
    with urllib.request.build_opener(TokenScopedRedirect()).open(request, timeout=30) as response:
        return response.read()


def cache_report(cache):
    temporary = Path(os.environ["RUNNER_TEMP"])
    def elapsed(kind):
        try:
            return (int((temporary / f"cache-{kind}-end").read_text()) -
                    int((temporary / f"cache-{kind}-start").read_text())) / 1e9
        except OSError:
            return None
    result = {"schema_version": 1, "restore_seconds": elapsed("restore"),
              "save_seconds": elapsed("save"), "matched_key": os.environ.get("CACHE_MATCH"),
              "namespace": os.environ.get("CACHE_PREFIX"),
              "save_status": os.environ.get("CACHE_SAVE"),
              "observed_at": datetime.now(timezone.utc).isoformat(),
              "exact_hit": os.environ.get("CACHE_HIT") == "true",
              "disk_bytes": sum(p.stat().st_size for p in cache.rglob("*") if p.is_file()),
              "provider_limit_bytes": None, "eviction_rate": None,
              "eviction_note": "unknown until comparable inventory snapshots exist"}
    try:
        result["provider_storage_limit"] = json.loads(github_api("/actions/cache/storage-limit"))
        limit = evidence.number(result["provider_storage_limit"]["max_cache_size_gb"])
        result["provider_limit_bytes"] = limit * 1_000_000_000
        result["provider_limit_unit"] = "GB converted as decimal bytes; raw response retained"
    except (OSError, ValueError, KeyError) as exc:
        result["provider_limit_error"] = str(exc)
    try:
        result["provider_usage"] = json.loads(github_api("/actions/cache/usage"))
        limit = result["provider_limit_bytes"]
        result["provider_usage_fraction"] = (
            result["provider_usage"]["active_caches_size_in_bytes"] / limit if limit else None)
        prefix = urllib.parse.quote(os.environ["CACHE_PREFIX"], safe="")
        inventory = json.loads(github_api("/actions/caches?per_page=100&key=" + prefix))
        result["inventory"] = [{k: item.get(k) for k in
            ("id", "key", "ref", "last_accessed_at", "size_in_bytes")}
            for item in inventory["actions_caches"]]
        result["inventory_complete"] = inventory["total_count"] <= 100
        result["restore_archive_bytes"] = next((row["size_in_bytes"] for row in result["inventory"]
            if row["key"] == result["matched_key"]), None)
        result["save_archive_bytes"] = next((row["size_in_bytes"] for row in result["inventory"]
            if row["key"] == os.environ.get("CACHE_KEY")), None)
    except (OSError, ValueError, KeyError) as exc:
        result["inventory_error"] = str(exc)
    return result


async def history(output, sha):
    # Missing/cancelled runs and commits without a push event stay in the history.
    commits = (await git("rev-list", "--first-parent", "--max-count=100", sha)).splitlines()[::-1]
    samples, unavailable = [], []
    current = output / "sample.json"
    if current.is_file():
        samples.append(evidence.read(current))
    for page in range(1, 4):
        try:
            listing = json.loads(github_api("/actions/workflows/bazel-shadow.yml/runs?" +
                                 f"event=push&branch=dev&per_page=100&page={page}"))
        except (OSError, ValueError) as exc:
            unavailable.append(str(exc))
            break
        for run in listing["workflow_runs"]:
            if str(run["id"]) == os.environ.get("GITHUB_RUN_ID") or run["head_sha"] not in commits:
                continue
            try:
                artifacts = json.loads(github_api(f"/actions/runs/{run['id']}/artifacts"))
                for artifact in artifacts["artifacts"]:
                    if artifact["name"] != "bazel-shadow-evidence-v1" or artifact["expired"]:
                        continue
                    raw = github_api(f"/actions/artifacts/{artifact['id']}/zip")
                    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                        info = archive.getinfo("sample.json")
                        if info.file_size > 32 * 1024 * 1024:
                            raise ValueError("historical sample exceeds bounded report size")
                        sample = json.loads(archive.read(info))
                    if (sample.get("sha") == run["head_sha"] and
                            str(sample.get("run_id")) == str(run["id"]) and
                            str(sample.get("run_attempt")) == str(run["run_attempt"])):
                        samples.append(sample)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
                unavailable.append({"run_id": run["id"], "error": str(exc)})
        if len(listing["workflow_runs"]) < 100:
            break
    report = evidence.window_report(commits, samples, suite_names(plan_document()))
    # Provider inventories can reveal disappearance, not prove eviction causality.
    inventories = [s["cache"] for s in samples if (s.get("cache") or {}).get("inventory_complete")]
    report["cache_inventory_disappearances"] = []
    by_sha = {s.get("sha"): s for s in samples
              if sum(candidate.get("sha") == s.get("sha") for candidate in samples) == 1}
    for before, after in zip(commits, commits[1:]):
        left, right = by_sha.get(before, {}).get("cache"), by_sha.get(after, {}).get("cache")
        if (not left or not right or left not in inventories or right not in inventories or
                left.get("namespace") != right.get("namespace")):
            continue
        previous = {row["id"] for row in left["inventory"]}
        following = {row["id"] for row in right["inventory"]}
        report["cache_inventory_disappearances"].append({"before": before, "after": after,
            "previous": len(previous), "disappeared": len(previous - following),
            "fraction": len(previous - following) / len(previous) if previous else None,
            "cause": "unknown (eviction or deletion); limits remain provider observations"})
    evidence.write(output / "window.json", report)
    evidence.write(output / "history.json", {"schema_version": 1, "unavailable": unavailable,
        "sample_count": len(samples), "commit_count": len(commits), "max_runs_scanned": 300})


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "legacy", "bazel", "sample", "window", "cache", "history"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--suite")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--commits", type=Path)
    parser.add_argument("--samples", type=Path)
    args = parser.parse_args()
    if args.operation == "cache":
        evidence.write(args.output, cache_report(args.cache))
        return 0
    if args.operation == "history":
        await history(args.output, args.sha)
        return 0
    if args.operation == "plan":
        names = suite_names(plan_document())
        evidence.write(args.output, {"include": [{"suite": name} for name in names]})
        return 0
    if args.operation == "window":
        samples = [evidence.read(p) for p in args.samples.rglob("sample.json")]
        evidence.write(args.output, evidence.window_report(evidence.read(args.commits), samples,
                                                          suite_names(plan_document())))
        return 0
    for path in (args.output, args.state, args.cache, args.inputs):
        if path is not None and path.resolve().is_relative_to(ROOT):
            raise ValueError("CI state and evidence must live outside the checkout")
    if args.operation == "legacy":
        return await legacy(args.output, args.sha, args.suite)
    if args.operation == "bazel":
        return await bazel(args.output, args.sha, args.state, args.cache)
    return await sample(args.output, args.sha, args.inputs)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
