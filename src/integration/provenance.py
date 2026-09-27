"""Immutable completion and explicit replacement evidence carried by Git.

Completion refs retain the exact source as their parent; their metadata commits
are never delivery proof. Readers test the *source*, not a trailer or marker.
The caller supplies an existing completion id, never a new delivery identity.
All writes are invoked by CommandHandler's completion/operator paths.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from src.git.manager import GitError, RemoteRefState, is_valid_git_oid

PREFIX = "aq-provenance/"
MAX_REPLACEMENTS = 1000
MAX_RECORD_BYTES = 64 * 1024


def task_message(message: str, task_id: str) -> str:
    """Append task identity without amending history or changing hook configuration."""
    if not task_id or any(ord(c) < 32 for c in task_id):
        raise ValueError("invalid task identity")
    trailer = f"AQ-Task: {task_id}"
    if message.rstrip().splitlines()[-1:] == [trailer]:
        return message
    return message.rstrip() + "\n\n" + trailer


@dataclass(frozen=True)
class CompletionIdentity:
    project_id: str
    repository_id: str
    task_id: str
    generation: str  # task_completion_records.id; claim_epoch is supplementary

    def __post_init__(self):
        if any(not isinstance(v, str) or not v or len(v) > 500
               or any(ord(c) < 32 for c in v) for v in asdict(self).values()):
            raise ValueError("completion requires exact project/repository/task/generation")

    @property
    def branch(self) -> str:
        subject = [self.project_id, self.repository_id, self.task_id]
        digest = hashlib.sha256(_json(subject).encode()).hexdigest()
        generation = hashlib.sha256(self.generation.encode()).hexdigest()
        return f"{PREFIX}completions/{digest}/{generation}"


@dataclass(frozen=True)
class CompletedSource:
    identity: CompletionIdentity
    source_oid: str

    def __post_init__(self):
        if not is_valid_git_oid(self.source_oid):
            raise ValueError("source must be a full lowercase Git OID")


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class GitProvenance:
    def __init__(self, git, checkout: str, *, repository_url: str):
        self.git, self.checkout, self.repository_url = git, checkout, repository_url

    async def run(self, *args, stdin=None, env=None) -> str:
        result = await self.git.arun_git_result(
            ["--no-replace-objects", *args], cwd=self.checkout, stdin=stdin, env=env
        )
        if result.returncode:
            raise GitError(result.stderr or result.stdout or "provenance Git operation failed")
        return result.stdout.strip()

    async def exact(self, oid: str) -> str:
        if not is_valid_git_oid(oid):
            raise ValueError("provenance requires a full lowercase Git OID")
        resolved = await self.run("--no-replace-objects", "rev-parse", "--verify",
                                  "--end-of-options", oid + "^{commit}")
        if resolved != oid:
            raise GitError("source does not resolve to the exact commit")
        return oid

    async def ancestor(self, source: str, target: str) -> bool:
        result = await self.git.arun_git_result(
            ["--no-replace-objects", "merge-base", "--is-ancestor", source, target],
            cwd=self.checkout,
        )
        if result.returncode not in (0, 1):
            raise GitError(result.stderr or "source ancestry is unknown")
        return result.returncode == 0

    async def _validate(self, record: dict, oid: str) -> dict:
        if record.get("version") != 1 or record.get("kind") not in {"completion", "replacement"}:
            raise ValueError("unsupported provenance record")
        source = await self.exact(record["source_oid"])
        parents = await self.run("--no-replace-objects", "show", "-s", "--format=%P", oid)
        if parents.split() != [source]:
            raise ValueError("provenance record does not retain its exact source")
        if await self.run("rev-parse", oid + "^{tree}") != await self.run(
            "rev-parse", source + "^{tree}"
        ):
            raise ValueError("provenance marker changes the source tree")
        if record["kind"] == "completion":
            CompletionIdentity(**record["identity"])
            if (
                type(record.get("artifact")) is not bool
                or type(record.get("claim_epoch")) is not int or record["claim_epoch"] < 0
            ):
                raise ValueError("invalid completion artifact/claim evidence")
        else:
            if record.get("authority") not in {"repair_contract", "operator"} or not record.get("reason"):
                raise ValueError("replacement requires explicit authority and reason")
            replaced = record.get("replaces")
            if not isinstance(replaced, list) or not replaced or len(replaced) > 100:
                raise ValueError("replacement requires every exact replaced completion")
            for item in replaced:
                CompletedSource(CompletionIdentity(**item["identity"]), item["source_oid"])
            await self._changed_source(source, record["base_oid"])
        return record

    async def _changed_source(self, source: str, base: str) -> None:
        await self.exact(base)
        if not await self.ancestor(base, source):
            raise ValueError("replacement does not descend from its declared base")
        if await self.run("rev-parse", base + "^{tree}") == await self.run(
            "rev-parse", source + "^{tree}"
        ):
            raise ValueError("an empty marker cannot prove complete replacement")

    async def _read(self, ref: str) -> dict | None:
        result = await self.git.arun_git_result(
            ["--no-replace-objects", "rev-parse", "--verify", "--quiet", ref], cwd=self.checkout
        )
        if result.returncode == 1:
            return None
        if result.returncode:
            raise GitError(result.stderr or "could not inspect provenance ref")
        oid = result.stdout.strip()
        await self.exact(oid)
        raw = await self.run("show", "-s", "--format=%B", oid)
        if len(raw.encode()) > MAX_RECORD_BYTES:
            raise ValueError("provenance record exceeds bounded size")
        return await self._validate(json.loads(raw), oid)

    async def read_completion(self, identity: CompletionIdentity) -> dict | None:
        # A fetched snapshot may be a clone (remote-tracking ref) or a bare
        # publisher store (local ref). Never scan historical AQ-Task trailers.
        for prefix in ("refs/remotes/origin/", "refs/heads/"):
            record = await self._read(prefix + identity.branch)
            if record is not None:
                if record["kind"] != "completion" or record["identity"] != asdict(identity):
                    raise ValueError("completion ref has a different immutable identity")
                return record
        return None

    async def _write(self, branch: str, record: dict) -> str:
        body = _json(record)
        if len(body.encode()) > MAX_RECORD_BYTES:
            raise ValueError("provenance record exceeds bounded size")
        source = await self.exact(record["source_oid"])
        tree = await self.run("rev-parse", source + "^{tree}")
        # Deterministic metadata object makes retry after an uncertain push
        # idempotent. Plumbing leaves the index, hooks and worktree untouched.
        oid = await self.run("commit-tree", tree, "-p", source, stdin=body + "\n", env={
            "GIT_AUTHOR_NAME": "Agent Queue", "GIT_AUTHOR_EMAIL": "aq@localhost",
            "GIT_COMMITTER_NAME": "Agent Queue", "GIT_COMMITTER_EMAIL": "aq@localhost",
            "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000",
        })
        await self._validate(record, oid)
        remote = await self.git.als_remote_ref(
            self.checkout, branch, repository_url=self.repository_url
        )
        if remote.state is RemoteRefState.ERROR:
            raise GitError(remote.error or "cannot inspect published provenance")
        if remote.state is RemoteRefState.PRESENT:
            if remote.oid != oid:
                raise ValueError("immutable completion generation already binds different evidence")
        else:
            await self.git._apush_oid(
                self.checkout, oid, branch, expected_old_oid="0" * 40,
                repository_url=self.repository_url,
            )
        verified = await self.git.als_remote_ref(
            self.checkout, branch, repository_url=self.repository_url
        )
        if verified.state is not RemoteRefState.PRESENT or verified.oid != oid:
            raise GitError("complete provenance is not verified on the authorized remote")
        await self.run("update-ref", "refs/remotes/origin/" + branch, oid)
        return oid

    async def write_completion(
        self, completed: CompletedSource, *, claim_epoch: int = 0, artifact: bool = True
    ) -> str:
        if type(claim_epoch) is not int or claim_epoch < 0 or type(artifact) is not bool:
            raise ValueError("invalid completion claim/artifact")
        record = {"version": 1, "kind": "completion", "identity": asdict(completed.identity),
                  "source_oid": completed.source_oid, "claim_epoch": claim_epoch,
                  "artifact": artifact}
        return await self._write(completed.identity.branch, record)

    async def write_replacement(
        self, *, source_oid: str, base_oid: str, replaces: list[CompletedSource],
        authority: str, reason: str,
    ) -> str:
        """Called only after operator authorization or exact repair-contract fencing.

        Git verifies identity/ancestry, not semantic equivalence. The caller
        attests that *all* work is replaced and supplies every original source.
        """
        for item in replaces:
            original = await self.read_completion(item.identity)
            if original is None or original["source_oid"] != item.source_oid or not original["artifact"]:
                raise ValueError("replacement lacks the exact original completion binding")
        record = {"version": 1, "kind": "replacement", "source_oid": source_oid,
                  "base_oid": base_oid, "replaces": [asdict(item) for item in replaces],
                  "authority": authority, "reason": reason.strip()}
        branch = PREFIX + "replacements/" + hashlib.sha256(_json(record).encode()).hexdigest()
        return await self._write(branch, record)

    async def contained(self, completed: CompletedSource, target_oid: str) -> bool:
        """Proof primitive for the shared evaluator; failures propagate as unknown."""
        await self.exact(target_oid)
        record = await self.read_completion(completed.identity)
        if record is None or record["source_oid"] != completed.source_oid:
            return False
        if not record["artifact"]:
            return False  # evaluator reports no_artifact separately
        if await self.ancestor(completed.source_oid, target_oid):
            return True
        refs = (await self.run("for-each-ref", f"--count={MAX_REPLACEMENTS + 1}",
            "--format=%(refname)", "refs/remotes/origin/" + PREFIX + "replacements/",
            "refs/heads/" + PREFIX + "replacements/")).splitlines()
        if len(refs) > MAX_REPLACEMENTS:
            raise ValueError("replacement inventory exceeds bounded evaluation limit")
        for ref in refs:
            replacement = await self._read(ref)
            if replacement and replacement["kind"] == "replacement" and asdict(completed) in replacement["replaces"]:
                if await self.ancestor(replacement["source_oid"], target_oid):
                    return True
        return False


async def record_worker_completion(
    db, git, task, project, checkout, generation, *, commit="", no_code_intent=False
):
    """Verify and publish completion evidence before the task can become terminal."""
    from src.integration.hierarchy import resolve_workspace_checkpoint

    repo = await db.get_repo(task.repo_id or project.integration_repository_id or "")
    if repo is None:
        raise ValueError("completion provenance has no authorized repository")
    subject = {"id": task.id, "repo_id": repo.id, "branch_name": task.branch_name}
    source = await resolve_workspace_checkpoint(db, git, subject, repo)
    if commit and commit != source:
        raise ValueError("--commit must identify the exact final source, not an earlier commit")
    store = GitProvenance(git, checkout, repository_url=repo.url)
    base = await store.run("rev-parse", "--verify", "refs/remotes/origin/" + repo.default_branch)
    await store.exact(base)
    identity = CompletionIdentity(project.id, repo.id, task.id, generation)
    await store.write_completion(CompletedSource(identity, source),
                                 claim_epoch=task.claim_epoch,
                                 artifact=not (no_code_intent and source == base))
    contract = await db.get_task_meta(task.id, "development_repair_sources")
    if contract:
        # No inference from repair-looking names or copied trailers. The
        # daemon-authored exact source contract is the authority for replacing
        # a full set of original completions; a reopened original fails closed.
        replaces = []
        if not isinstance(contract, list) or len(contract) > 100:
            raise ValueError("invalid exact development repair contract")
        for member in contract:
            completion = await db.get_task_completion(member["task_id"])
            if completion is None or completion.outcome != "pass":
                raise ValueError("repair source has no passing immutable completion")
            original = CompletionIdentity(project.id, repo.id, member["task_id"], completion.id)
            if await store.read_completion(original) is None:
                # Upgrade bridge for an original closed before provenance.
                # Ambiguous old closes still require the explicit migration.
                exact = await legacy_repair_source(
                    db, store, project.id, task.id, member, completion, repair_head=source
                )
                if exact is None:
                    raise ValueError(
                        f"unlabelled repair source {member['task_id']} requires exact provenance "
                        f"migration: aq integration migrate-provenance {project.id} "
                        f"--task-id {task.id}"
                    )
                await store.write_completion(CompletedSource(original, exact))
            replaces.append(CompletedSource(original, member["source_sha"]))
        # The replacement base is the default-branch commit this repair
        # builds on. Main advancing after the repair merged it does not make
        # the repair incomplete, so never require the live tip as its base.
        repair_base = await store.run("merge-base", source, base)
        if await store.run("rev-parse", repair_base + "^{tree}") != await store.run(
            "rev-parse", source + "^{tree}"
        ):
            await store.write_replacement(
                source_oid=source, base_oid=repair_base, replaces=replaces,
                authority="repair_contract", reason=f"Exact development repair contract: {task.id}",
            )
        else:
            # A repair that changes nothing cannot prove a replacement; it may
            # close only when ancestry alone carries every original source.
            for item in replaces:
                if not await store.ancestor(item.source_oid, source):
                    raise ValueError("an empty repair cannot replace a source it does not contain")
    # A concurrent local commit or source push must not close an older snapshot.
    if await resolve_workspace_checkpoint(db, git, subject, repo) != source:
        raise ValueError("final source changed while recording completion provenance")
    return source


async def legacy_repair_source(db, store, project_id, repair_id, member, completion, *,
                               repair_head=None):
    """Exact source of a repair-contract original closed before provenance existed.

    Returns the full source OID to bind to *completion*, or ``None`` when only
    the explicit migration can decide. A final completion source that names the
    contract source (in full, or as its unique abbreviation) is exact. A
    commits-less legacy close reports nothing to contradict: the daemon-authored
    contract source stands when the delivery that filed the repair names it and
    postdates this completion (the generation fence) and, given *repair_head*,
    the repair still contains it by ancestry (the pre-provenance check). A
    different reported source is never overridden.
    """
    source = member.get("source_sha")
    if not is_valid_git_oid(source):
        return None
    reported = completion.commits[-1] if completion.commits else None
    if reported is not None:
        if reported != source and not (
            isinstance(reported, str) and re.fullmatch(r"[0-9a-f]{7,39}", reported)
            and source.startswith(reported)
        ):
            return None
        try:
            resolved = await store.run("rev-parse", "--verify", "--end-of-options",
                                       reported + "^{commit}")
        except GitError:
            return None  # an ambiguous abbreviation is not an exact source
        return await store.exact(source) if resolved == source else None
    evidence = await db.get_task_meta(repair_id, "development_repair_evidence")
    delivery_id = evidence.get("delivery_id") if isinstance(evidence, dict) else None
    if not delivery_id:
        return None
    from sqlalchemy import select

    from src.database.tables import development_deliveries

    async with db._engine.connect() as conn:
        row = (await conn.execute(select(development_deliveries).where(
            development_deliveries.c.id == delivery_id,
        ))).mappings().first()
    if (
        row is None or row["project_id"] != project_id
        or float(row["created_at"]) < completion.completed_at
        or not any(item.get("task_id") == member["task_id"] and item.get("source_sha") == source
                   for item in row["manifest"] or [])
    ):
        return None
    await store.exact(source)
    if repair_head is not None and not await store.ancestor(source, repair_head):
        return None
    return source
