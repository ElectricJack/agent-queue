#!/usr/bin/env python3
"""Profile repeated train/frontier proofs over disposable, real Git objects.

Run: python scripts/benchmark-git-truth.py --members 137 --passes 3 --profile /tmp/truth.prof
No daemon, operator database, network, or existing repository is changed.
Wall time, Python CPU time and Git command counts include evaluation only.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.git.manager import GitManager  # noqa: E402
from src.integration.delivery_truth import DeliveryRequest, DeliverySnapshot  # noqa: E402
from src.integration.git_truth import GitTruth, GitTruthSnapshot  # noqa: E402
from src.integration.provenance import (  # noqa: E402
    CompletionIdentity, GitProvenance,
)


async def benchmark(members: int, passes: int, profile: str | None):
    with tempfile.TemporaryDirectory(prefix="aq-truth-benchmark-") as temporary:
        path = Path(temporary)
        git = GitManager()

        async def run(*args):
            return await git._arun(list(args), cwd=temporary)

        await run("init", "-b", "main")
        (path / "work").write_text("delivered\n")
        await run("add", "work")
        await git._arun(["-c", "user.name=Benchmark", "-c", "user.email=benchmark@example.test",
                        "-c", "commit.gpgsign=false", "commit", "-m", "base"], cwd=temporary)
        head = await run("rev-parse", "HEAD")
        provenance = GitProvenance(git, temporary, repository_url="benchmark")
        refs, requests = {}, []
        for index in range(members):
            identity = CompletionIdentity("benchmark", "repo", f"member-{index}", "close-1")
            oid = await provenance._stage({
                "version": 1, "kind": "completion", "identity": {
                    "project_id": identity.project_id, "repository_id": identity.repository_id,
                    "task_id": identity.task_id, "generation": identity.generation,
                }, "source_oid": head, "claim_epoch": 1, "artifact": True,
            })
            refs["refs/remotes/origin/" + identity.branch] = oid
            requests.append(DeliveryRequest(
                "benchmark", "repo", "refs/heads/main", identity.task_id, 1, "legacy",
                branch_name=identity.task_id, completion_id="close-1",
            ))
        # All members are delivered; unrelated epic refs must not add per-member work.
        refs.update({f"refs/remotes/origin/aq/epic/{i}": head for i in range(members)})
        truth = GitTruth(git)
        observed = DeliverySnapshot(git, temporary, "benchmark", "repo", "benchmark",
                                    "refs/heads/main", head, refs)
        commands = 0
        original = git.arun_git_result

        async def counted(*args, **kwargs):
            nonlocal commands
            commands += 1
            return await original(*args, **kwargs)

        git.arun_git_result = counted
        profiler = cProfile.Profile() if profile else None
        if profiler:
            profiler.enable()
        results = []
        for visit in range(passes + 1):
            snapshot = GitTruthSnapshot(truth, observed.for_request())
            start, cpu, before = time.perf_counter(), time.process_time(), commands
            for request in requests:
                proof = await snapshot.is_delivered(request)
                assert proof.satisfied, proof
            elapsed, cpu_used = time.perf_counter() - start, time.process_time() - cpu
            results.append({
                "phase": "cold" if visit == 0 else "warm", "members": members,
                "seconds": elapsed, "python_cpu_seconds": cpu_used,
                "git_commands": commands - before,
                "cpu_percent_at_5s_interval": cpu_used / 5 * 100,
            })
        if profiler:
            profiler.disable()
            profiler.dump_stats(profile)
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--members", type=int, default=137)
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--profile")
    args = parser.parse_args()
    if args.members < 1 or args.passes < 1:
        parser.error("members and passes must be positive")
    asyncio.run(benchmark(args.members, args.passes, args.profile))
