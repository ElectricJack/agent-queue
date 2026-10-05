"""Run the real daemon with local Git credentials for disposable fake sessions.

Cadence comes from production scheduling configuration. Commands, reconciliation
and events still run in the production daemon implementation.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
from pathlib import Path


def install_local_git_transport(orch, home: Path):
    """Substitute GitHub credentials only for this marked kit's local remotes.

    The production train still builds candidates and uses its real DB fences,
    Git transport and exact expected-old OIDs. Hosted credentials/checks belong
    to the separate App-mode acceptance kit.
    """
    from src.git.github_contracts import GitHubRepositoryBinding
    from src.git.manager import GitError

    home = home.resolve()
    if not (home / ".aq-e2e").is_file():
        raise ValueError("local transport requires a marked disposable e2e home")
    git = orch.git
    originals = (orch.github_repository_binding_resolver,
                 git.aremote_branch_head, git.apush_repository_oid, git.als_remote_ref)
    remotes = {}

    async def binding(repo):
        if repo.url and Path(repo.url).is_absolute():
            remote = Path(repo.url).resolve()
            if remote.is_relative_to(home) and remote.is_dir():
                digest = hashlib.sha256(str(remote).encode()).hexdigest()
                identity = GitHubRepositoryBinding(
                    int(digest[:12], 16), "e2e/" + digest[:16],
                )
                remotes[identity.full_name] = remote
                return identity
        return await originals[0](repo)

    async def head(*, repository, branch):
        remote = remotes.get(getattr(repository, "full_name", None))
        if remote is None:
            return await originals[1](repository=repository, branch=branch)
        result = await git.arun_git_result(
            ["show-ref", "--verify", "--quiet", "refs/heads/" + branch], cwd=str(remote),
        )
        if result.returncode == 1:
            return None
        if result.returncode:
            raise GitError(result.stderr)
        return await git.arev_parse(str(remote), "refs/heads/" + branch)

    async def push(store, *, repository, tip_oid, branch, expected_old_oid,
                   authority_deadline=None):
        remote = remotes.get(getattr(repository, "full_name", None))
        if remote is None:
            return await originals[2](
                store, repository=repository, tip_oid=tip_oid, branch=branch,
                expected_old_oid=expected_old_oid, authority_deadline=authority_deadline,
            )
        if authority_deadline is not None and authority_deadline <= asyncio.get_running_loop().time():
            raise GitError("local publication authority expired")
        async with asyncio.timeout_at(authority_deadline):
            result = await git.arun_git_result([
                "push", f"--force-with-lease=refs/heads/{branch}:{expected_old_oid}",
                str(remote), f"{tip_oid}:refs/heads/{branch}",
            ], cwd=store)
        if result.returncode:
            raise GitError(result.stderr)
        return tip_oid

    async def read_ref(checkout_path, branch, *, repository_url=None, **kwargs):
        for name, remote in remotes.items():
            if repository_url == f"https://github.com/{name}.git":
                repository_url = str(remote)
                break
        return await originals[3](
            checkout_path, branch, repository_url=repository_url, **kwargs,
        )

    orch.github_repository_binding_resolver = binding
    git.aremote_branch_head, git.apush_repository_oid = head, push
    git.als_remote_ref = read_ref

    def restore():
        orch.github_repository_binding_resolver = originals[0]
        git.aremote_branch_head, git.apush_repository_oid, git.als_remote_ref = originals[1:]

    return restore


async def run_fake_cycles(daemon, orch, shutdown_event, interval=None):
    """Expose completed cycles so negative assertions observe actual scheduling."""
    original = orch.run_one_cycle
    path = Path(os.environ["AQ_E2E_HOME"]) / "scheduler-cycles"
    count = 0
    restore_transport = install_local_git_transport(orch, path.parent) \
        if getattr(orch, "git", None) is not None else None

    async def observed_cycle():
        nonlocal count
        await original()
        count += 1
        temporary = path.with_suffix(".tmp")
        temporary.write_text(str(count))
        temporary.replace(path)

    orch.run_one_cycle = observed_cycle
    try:
        if interval is None:
            await daemon(orch, shutdown_event)
        else:
            await daemon(orch, shutdown_event, interval=interval)
    finally:
        orch.run_one_cycle = original
        if restore_transport is not None:
            restore_transport()


def main() -> None:
    from src import main as daemon

    if os.environ.get("AQ_E2E_SESSION_PROVIDER", "fake") == "fake":
        daemon._run_scheduler_cycles = functools.partial(
            run_fake_cycles, daemon._run_scheduler_cycles,
        )
    daemon.main()


if __name__ == "__main__":
    main()
