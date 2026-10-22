"""Disposable observations and additive original-object retention for cutover."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from src.git.manager import GitError, GitManager
from src.integration.git_truth import GitTruth
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance


class ContainmentGit:
    def __init__(self, *, git=None, protection=None):
        self.git = git or GitManager()
        self.protection = protection

    @asynccontextmanager
    async def observe(self, facts):
        repository = facts["repository"]
        # Fetching into an operator checkout would violate preview's read-only contract.
        with TemporaryDirectory(prefix="aq-containment-preview-") as store:
            await self.git._arun(["init", "--bare", store])
            await self.git._arun(["remote", "add", "origin", repository["url"]], cwd=store)
            snapshot = await GitTruth(self.git).snapshot(
                store, project_id=repository["project_id"], repository_id=repository["id"],
                repository_url=repository["url"],
                target_ref="refs/heads/" + repository["default_branch"].removeprefix("refs/heads/"),
            )
            yield snapshot

    async def external(self, facts, snapshot):
        observed = snapshot.observation
        refs = dict(observed.source_heads)
        paths = {facts["repository"]["checkout_base_path"]}
        paths.update(row["workspace_path"] for row in facts["inventory"]["workspaces"])
        checkouts = []
        for path in sorted(paths - {""}):
            row = {"path": path}
            try:
                # Git's global flag prevents status from refreshing the user's index.
                result = await self.git.arun_git_result(
                    ["--no-optional-locks", "status", "--porcelain=v1",
                     "--untracked-files=normal"], cwd=path,
                )
                if result.returncode:
                    raise GitError("checkout_state_unavailable")
                row["status"] = result.stdout
                row["refs"] = await self.git._arun(
                    ["for-each-ref", "--format=%(refname) %(objectname)"], cwd=path,
                )
                row["head"] = await self.git._arun(["rev-parse", "HEAD"], cwd=path)
            except (GitError, OSError):
                row["error"] = "checkout_state_unavailable"
            checkouts.append(row)
        trust = None
        if observed.target_oid:
            result = await self.git.arun_git_result(
                ["show", observed.target_oid + ":.github/agent-queue-integration.json"],
                cwd=observed.store,
            )
            if result.returncode == 0:
                try:
                    trust = json.loads(result.stdout)
                except ValueError:
                    trust = {"error": "invalid_ci_trust_manifest"}
        protection = await self.protection(facts) if self.protection else None
        revision = await self.git._arun(
            ["rev-parse", "HEAD"], cwd=str(Path(__file__).resolve().parents[2]),
        )
        return {"refs": refs, "checkouts": checkouts, "ci_trust": trust,
                "protection": protection, "code_revision": revision}

    async def retain(self, snapshot, rows):
        """Retain verified sources under create-only private refs, never replace them."""
        observed = snapshot.observation
        provenance = GitProvenance(self.git, observed.store,
                                   repository_url=observed.repository_url)
        retained = {}
        for row in rows:
            if not row["source_oid"]:
                continue
            identity = CompletionIdentity(observed.project_id, observed.repository_id,
                                          row["task_id"], row["completion_id"])
            record = await provenance.read_completion(identity, refs=observed.source_heads)
            if record is None and row["verified_parent"]:
                retained[identity.ref] = await provenance.write_completion(
                    CompletedSource(identity, row["source_oid"]),
                )
                continue
            if record is None or record["source_oid"] != row["source_oid"]:
                raise ValueError("original_object_unretained")
            # Preserve artifact/epoch evidence, including legacy markers. _write verifies
            # exact remote identity and uses create-only publication with readback.
            retained[identity.ref] = await provenance._write(identity.ref, record)
        return retained
