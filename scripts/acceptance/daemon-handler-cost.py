#!/usr/bin/env python3
"""Compare hot handler work without a daemon, credentials or an operator DB.

Run the same arguments before/after. PR and transcript I/O use real temporary
files; database ownership and GitHub inventory are fixed recording doubles.
These numbers measure avoided work, not production daemon CPU acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict
import inspect
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.git.manager import GitManager
from src.integration.pr_delivery import DeliveredPullRequestClosure
from src.integration.promotion_contracts import ResolvedRepository
from src.integration.repository_git import RepositoryGit
from src.sessions.transcripts.claude import ClaudeTranscriptReader, slug_work_dir
from src.wait_runtime import measurements


async def measure(root: Path, *, records: int, repeats: int) -> list[dict]:
    git = GitManager()
    store = root / "retained.git"
    await git.arun_git_result(["init", "--bare", str(store)], cwd=str(root))
    await git.arun_git_result(
        ["remote", "add", "origin", str(store)], cwd=str(store),
    )
    repo = SimpleNamespace(id="repo", project_id="p", url=str(store), default_branch="main")
    project = SimpleNamespace(integration_repository_id="repo")

    class Database:
        async def get_project(self, _id):
            return project

        async def get_repo(self, _id):
            return repo

        async def get_session(self, _id):
            return session

    db = Database()

    class Repository(RepositoryGit):
        async def _resolve_repository(self, _id):
            return ResolvedRepository(repo=repo, origin_url=repo.url, retained_git_dir=store)

    class Closure(DeliveredPullRequestClosure):
        async def _tracking_tasks(self, *args, **kwargs):
            return ["active"], ["active"]

    pull = {"number": 1, "html_url": "https://github.com/o/r/pull/1", "state": "open",
            "head": {"ref": "aq/active", "sha": "a" * 40, "repo": {"id": 7}},
            "base": {"ref": "main"}}

    async def binding(_url):
        return SimpleNamespace(repository_id=7, full_name="o/r")

    git.bind_github_repository = binding
    git._github_client = lambda _binding: None
    calls = 0
    run_git = git.arun_git_result

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await run_git(*args, **kwargs)

    git.arun_git_result = counted
    control = Closure(db, Repository(db, data_dir=root, git_manager=git))
    started, cpu = time.perf_counter(), time.process_time()
    for _ in range(repeats):
        result = await control.run("p", 1, inventory_pull=pull)
        assert result["outcome"] == "not_eligible"
    results = [{"case": "active_pr_preview", "repeats": repeats, "git_calls": calls,
                "wall_seconds": time.perf_counter() - started,
                "cpu_seconds": time.process_time() - cpu}]

    work_dir = str(root / "worker")
    session = SimpleNamespace(id="s", harness="claude", session_key="s", work_dir=work_dir)
    path = root / ".claude" / "projects" / slug_work_dir(work_dir) / "s.jsonl"
    path.parent.mkdir(parents=True)
    row = {"type": "assistant", "uuid": "call", "timestamp": "2026-10-25T00:00:00Z",
           "message": {"content": [{"type": "text", "text": "pending"}],
                       "usage": {"input_tokens": 10}}}
    line = (json.dumps(row) + "\n").encode()
    with path.open("wb") as stream:
        for _ in range(records):
            stream.write(line)
    reader = ClaudeTranscriptReader(base_dir=root)
    measurements.resolve_reader = lambda _harness: reader
    reads = 0
    read_new = reader.read_new

    async def counted_read(*args):
        nonlocal reads
        reads += 1
        return await read_new(*args)

    reader.read_new = counted_read
    cache = OrderedDict()
    options = {"cache": cache} if "cache" in inspect.signature(
        measurements.session_turn_tokens).parameters else {}
    started, cpu = time.perf_counter(), time.process_time()
    for _ in range(repeats):
        assert await measurements.session_turn_tokens(db, "s", since=0, **options) is None
    results.append({"case": "unchanged_pending_transcript", "records": records,
                    "repeats": repeats, "full_reads": reads,
                    "wall_seconds": time.perf_counter() - started,
                    "cpu_seconds": time.process_time() - cpu})
    return results


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="aq-handler-cost-") as folder:
        print(json.dumps(await measure(Path(folder), **vars(args)), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
