"""Bounded, read-only audit inventory and sanitized telemetry (no daemon/DB calls)."""
import ast
import hashlib
import json
import math
import os
import platform
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent


def stats(values):
    values = sorted(values)
    return {"n": len(values), **{name: values[max(0, math.ceil(p * len(values)) - 1)]
            for name, p in (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1))}}


def main():
    inventory = []
    scanned = 0
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text())
        scanned += 1
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            awaits = [n for n in ast.walk(node) if isinstance(n, ast.Await)]
            loops = [n for n in ast.walk(node) if isinstance(n, (ast.For, ast.AsyncFor, ast.While))]
            loop_awaits = sorted({n.lineno for loop in loops for n in ast.walk(loop)
                                 if isinstance(n, ast.Await)})
            if loop_awaits or any(s in node.name for s in ("tick", "poll", "check", "observe",
                                                         "refresh", "claim", "reconcile")):
                inventory.append({"path": str(path.relative_to(ROOT)), "function": node.name,
                                  "line": node.lineno, "end": node.end_lineno,
                                  "awaits": len(awaits), "await_lines_in_loops": loop_awaits})
    (OUT / "inventory.json").write_text(json.dumps({"scanned_python_modules": scanned,
        "method": "AST syntactic inventory; nested callables included, not proof of runtime cost",
        "functions": inventory}, indent=2) + "\n")
    log = Path.home() / ".agent-queue/logs/agent-queue.log"
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - 1_500_000))
        sample = stream.read(1_500_000)
    visits, warnings, timestamps = [], Counter(), []
    for line in sample.decode(errors="replace").splitlines()[1:]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        event = record.get("event", "")
        timestamps.append(record.get("timestamp"))
        if event.startswith("integration train visit timing for "):
            target, timing = event.removeprefix("integration train visit timing for ").split(": ", 1)
            visits.append({"timestamp": record.get("timestamp"), "target": ast.literal_eval(target),
                           **ast.literal_eval(timing)})
        for term in ("root discovery probe unavailable", "hierarchy frontier", "pool timeout",
                     "event loop stall"):
            if term in event:
                warnings[term] += 1
    groups = defaultdict(lambda: defaultdict(list))
    for visit in visits:
        for group in ("all", "root" if visit["target"][2] == "refs/heads/main" else "nonroot"):
            groups[group]["elapsed"].append(visit["elapsed_seconds"])
            for stage, seconds in visit["stages_seconds"].items():
                groups[group][stage].append(seconds)
    cpu = next((s.split(":", 1)[1].strip() for s in Path("/proc/cpuinfo").read_text().splitlines()
                if s.startswith("model name")), "unknown")
    evidence = {"source_head": subprocess.check_output(["git", "rev-parse", "HEAD"],
                    cwd=ROOT, text=True).strip(), "sample_bytes": len(sample),
                "sample_sha256": hashlib.sha256(sample).hexdigest(),
                "first_timestamp": timestamps[0], "last_timestamp": timestamps[-1],
                "host": {"cpu": cpu, "logical_cpus": os.cpu_count(),
                         "affinity_cpus": len(os.sched_getaffinity(0)),
                         "load_average": os.getloadavg(), "python": platform.python_version(),
                         "kernel": platform.release()},
                "warnings": warnings, "visits": visits,
                "seconds_nearest_rank": {g: {s: stats(v) for s, v in stages.items()}
                                         for g, stages in groups.items()},
                "limitations": "Completed visits only, mixed cache states; stage times include awaits. "
                "No DB rows, SQL counts, cache hit counts, lock waits or deployed SHA attribution. "
                "Local source HEAD need not equal daemon deployment. Tail sampling is not an SLO run."}
    (OUT / "telemetry.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"modules": scanned, "inventory_functions": len(inventory),
                      "window": [timestamps[0], timestamps[-1]], "warnings": warnings,
                      "stats": evidence["seconds_nearest_rank"], "host": evidence["host"]}, indent=2))


if __name__ == "__main__":
    main()
