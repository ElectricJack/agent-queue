"""Real local Git proof microprofile; disposable temp repo, no DB/network/config edits.

Uses inherited daemon Git identity. Measures proof work, NOT an end-to-end selection.
"""
import asyncio
import json
import math
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from src.git.manager import GitManager  # noqa: E402
from src.integration.delivery_truth import DeliveryRequest, DeliverySnapshot  # noqa: E402
from src.integration.git_truth import GitTruth, GitTruthSnapshot  # noqa: E402
from src.integration.provenance import (  # noqa: E402
    CompletedSource, CompletionIdentity, GitProvenance, _completion_record,
)


class CountGit(GitManager):
    def __init__(self):
        super().__init__()
        self.count = 0

    async def _arun_git_result_unlocked(self, *args, **kwargs):
        self.count += 1
        return await super()._arun_git_result_unlocked(*args, **kwargs)


def summary(values):
    values = sorted(values)
    return {"n": len(values), **{key: values[math.ceil(p * len(values)) - 1]
            for key, p in (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1))}}


async def main():
    git = CountGit()
    with tempfile.TemporaryDirectory(prefix="aq-proof-audit-") as directory:
        async def run(*args):
            return await git._arun(list(args), cwd=directory)
        await run("init", "-b", "main")
        Path(directory, "seed").write_text("seed\n")
        await run("add", "seed")
        await run("-c", "commit.gpgsign=false", "commit", "-m", "synthetic base")
        base = await run("rev-parse", "HEAD")
        Path(directory, "work").write_text("work\n")
        await run("add", "work")
        await run("-c", "commit.gpgsign=false", "commit", "-m", "synthetic source")
        source = await run("rev-parse", "HEAD")
        provenance = GitProvenance(git, directory, repository_url="local-fixture")
        heads, requests = {}, []
        # Many generations, shared small source; isolates metadata validation cost.
        for n in range(200):
            identity = CompletionIdentity("p", "r", f"task-{n}", "close-1")
            marker = await provenance._stage(_completion_record(
                CompletedSource(identity, source), 1, True))
            heads["refs/remotes/origin/" + identity.branch] = marker
            requests.append(DeliveryRequest("p", "r", "refs/heads/main", f"task-{n}",
                1.0, "legacy", completion_id="close-1"))
        truth = GitTruth(git)
        snapshot = GitTruthSnapshot(truth, DeliverySnapshot(git, directory, "p", "r",
            "local-fixture", "refs/heads/main", source, heads))
        output = {"fixture": {"completions": 200, "targets": 55, "source_commits": 2,
            "concurrency": 1, "network_requests": 0, "db_queries": 0,
            "warning": "Scale in completions/targets only; not representative repository history, "
                       "DB cardinality, contention, or remote latency."}}

        async def measure(name, snap, repetitions):
            before = git.count
            durations = []
            for _ in range(repetitions):
                start = time.perf_counter()
                proofs = [await snap.is_delivered(r, source_base=base) for r in requests]
                durations.append(time.perf_counter() - start)
                assert all(p.satisfied for p in proofs)
            output[name] = {"seconds_per_200_proofs": summary(durations),
                            "git_processes": git.count - before}

        await measure("cold", snapshot, 1)
        await measure("warm", snapshot, 100)
        # Same proof repeated by 55 targets' root-delivered scans, twice per target.
        await measure("warm_root_scan_repetition", snapshot, 110)
        # New metadata generation absent in pinned snapshot must not inherit success.
        changed = replace(requests[0], completion_id="close-2")
        before = git.count
        missing = await snapshot.is_delivered(changed, source_base=base)
        output["changed_generation"] = {"state": str(missing.state), "reason": missing.reason,
                                        "git_processes": git.count - before}
        # Eviction/cold diagnostic returns unknown without expensive proof work.
        cold_display = replace(snapshot, truth=GitTruth(git), cached_only=True)
        before = git.count
        unavailable = await cold_display.is_delivered(requests[0], source_base=base)
        output["cold_cached_only"] = {"state": str(unavailable.state),
            "reason": unavailable.reason, "git_processes": git.count - before}
        assert not missing.satisfied and not unavailable.satisfied
        assert output["warm"]["git_processes"] == 0
        assert output["warm_root_scan_repetition"]["git_processes"] == 0
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
