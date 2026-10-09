"""Immutable completion and explicit replacement evidence carried by Git.

Completion refs retain the exact source as their parent; their metadata commits
are never delivery proof. Readers test the *source*, not a trailer or marker.
The caller supplies the generation id used by the shared delivery evaluator:
an existing close/verification id, or a version-fenced legacy generation for
an operator-attested completed task without a descriptive completion row.
All writes are invoked by CommandHandler's completion/operator paths.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from src.git.identity import LEDGER_IDENTITY
from src.git.manager import GitError, RemoteRefState, is_valid_git_oid

PREFIX = "refs/aq/provenance/"
LEGACY_PREFIX = "aq-provenance/"
MAX_REPLACEMENTS = 1000
MAX_RECORD_BYTES = 64 * 1024

#: Event type the ``development_deliveries`` retirement revision (a00000000038)
#: appends once per retired journal row that located sources: its identity,
#: target, manifest and source bindings, without its delivery state. Only the
#: operator provenance migration and a legacy repair's filing fence read it.
LEGACY_PROVENANCE_EVENT = "development.legacy_provenance"


class ProvenanceMigrationRequired(ValueError):
    """Historical repair evidence lacks the required completion identity."""

    def __init__(self, detail: str, project_id: str, *, task_id: str | None = None):
        # Name a read-only diagnostic scoped to the missing identity evidence.
        remedy = f"aq task show {task_id}" if task_id else f"aq integration status {project_id}"
        self.context = {
            "fixable_by": "operator",
            "precondition": "provenance_migration",
            "remedy": remedy,
        }
        super().__init__(detail)


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
    generation: str  # leaf close id or parent:<verification id>; claim_epoch is supplementary

    def __post_init__(self):
        if any(not isinstance(v, str) or not v or len(v) > 500
               or any(ord(c) < 32 for c in v) for v in asdict(self).values()):
            raise ValueError("completion requires exact project/repository/task/generation")

    @property
    def ref(self) -> str:
        subject = [self.project_id, self.repository_id, self.task_id]
        digest = hashlib.sha256(_json(subject).encode()).hexdigest()
        generation = hashlib.sha256(self.generation.encode()).hexdigest()
        return f"{PREFIX}completions/{digest}/{generation}"

    @property
    def legacy_branch(self) -> str:
        return LEGACY_PREFIX + self.ref.removeprefix(PREFIX)

    @property
    def read_refs(self) -> tuple[str, ...]:
        return (self.ref, "refs/remotes/origin/" + self.legacy_branch,
                "refs/heads/" + self.legacy_branch)


@dataclass(frozen=True)
class CompletedSource:
    identity: CompletionIdentity
    source_oid: str

    def __post_init__(self):
        if not is_valid_git_oid(self.source_oid):
            raise ValueError("source must be a full lowercase Git OID")


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _completion_record(completed: CompletedSource, claim_epoch: int, artifact: bool) -> dict:
    if type(claim_epoch) is not int or claim_epoch < 0 or type(artifact) is not bool:
        raise ValueError("invalid completion claim/artifact")
    return {"version": 1, "kind": "completion", "identity": asdict(completed.identity),
            "source_oid": completed.source_oid, "claim_epoch": claim_epoch,
            "artifact": artifact}


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
        if (not isinstance(record, dict) or record.get("version") != 1
                or record.get("kind") not in {"completion", "replacement"}):
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

    async def read_completion(
        self, identity: CompletionIdentity, *, refs: Mapping[str, str] | None = None
    ) -> dict | None:
        # New refs are explicitly fetched into the same non-branch namespace.
        # Legacy clones and bare stores remain readable during rollout.
        for ref in identity.read_refs:
            if refs is not None:
                # A visit pins refs after its fetch. Never reread a mutable ref
                # or fall back to a local marker absent from that observation.
                ref = refs.get(ref)
                if ref is None:
                    continue
            record = await self._read(ref)
            if record is not None:
                if record["kind"] != "completion" or record["identity"] != asdict(identity):
                    raise ValueError("completion ref has a different immutable identity")
                return record
        return None

    async def remote_completion(self, identity: CompletionIdentity):
        """Observe retention directly on the authorized remote, with legacy fallback."""
        refs = [identity.ref, "refs/heads/" + identity.legacy_branch]
        observed = await self.git.als_remote_qualified_refs(
            self.checkout, refs, repository_url=self.repository_url,
        )
        for ref in refs:
            result = observed[ref]
            if result.state is RemoteRefState.ERROR:
                raise GitError(result.error or "cannot inspect published provenance")
            if result.state is RemoteRefState.PRESENT:
                return ref, result
        return identity.ref, observed[identity.ref]

    async def _stage(self, record: dict) -> str:
        """The validated local metadata commit for *record*; nothing is published."""
        body = _json(record)
        if len(body.encode()) > MAX_RECORD_BYTES:
            raise ValueError("provenance record exceeds bounded size")
        source = await self.exact(record["source_oid"])
        tree = await self.run("rev-parse", source + "^{tree}")
        # Deterministic metadata object makes retry after an uncertain push
        # idempotent. Plumbing leaves the index, hooks and worktree untouched.
        # A ledger object, not authored work: fixed identity (LEDGER_IDENTITY).
        oid = await self.run("commit-tree", tree, "-p", source, stdin=body + "\n", env={
            **LEDGER_IDENTITY.env(),
            "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000",
        })
        await self._validate(record, oid)
        return oid

    async def _write(self, ref: str, record: dict) -> str:
        oid = await self._stage(record)
        legacy_ref = "refs/heads/" + LEGACY_PREFIX + ref.removeprefix(PREFIX)
        observed = await self.git.als_remote_qualified_refs(
            self.checkout, [ref, legacy_ref], repository_url=self.repository_url
        )
        legacy = observed[legacy_ref]
        if legacy.state is RemoteRefState.ERROR:
            raise GitError(legacy.error or "cannot inspect legacy provenance")
        if legacy.state is RemoteRefState.PRESENT and legacy.oid != oid:
            raise ValueError("immutable completion generation already binds different evidence")
        remote = observed[ref]
        if remote.state is RemoteRefState.ERROR:
            raise GitError(remote.error or "cannot inspect published provenance")
        if remote.state is RemoteRefState.PRESENT:
            if remote.oid != oid:
                raise ValueError("immutable completion generation already binds different evidence")
        else:
            await self.git.apush_new_refs(
                self.checkout, {ref: oid}, qualified=True, repository_url=self.repository_url,
            )
        verified = (await self.git.als_remote_qualified_refs(
            self.checkout, [ref], repository_url=self.repository_url
        ))[ref]
        if verified.state is not RemoteRefState.PRESENT or verified.oid != oid:
            raise GitError("complete provenance is not verified on the authorized remote")
        await self.run("update-ref", ref, oid)
        return oid

    async def write_completion(
        self, completed: CompletedSource, *, claim_epoch: int = 0, artifact: bool = True
    ) -> str:
        return await self._write(completed.identity.ref,
                                 _completion_record(completed, claim_epoch, artifact))

    async def write_completions(
        self, completions: list[CompletedSource], *, claim_epoch: int = 0, artifact: bool = True
    ) -> dict[CompletionIdentity, str | Exception]:
        """:meth:`write_completion` for many generations in one remote transfer.

        Each record is staged and validated exactly as a single write stages
        it; one read, one push leased to absence and one read-back settle every
        ref. A ref already holding the same deterministic object is kept, and
        each generation reports its own oid or its own failure, never a peer's.
        """
        results: dict[CompletionIdentity, str | Exception] = {}
        staged: dict[str, tuple[CompletionIdentity, str]] = {}
        for completed in completions:
            try:
                oid = await self._stage(_completion_record(completed, claim_epoch, artifact))
            except (ValueError, KeyError, TypeError, GitError) as exc:
                results[completed.identity] = exc
                continue
            # An identical repeat shares its generation's one ref and result.
            if staged.setdefault(completed.identity.ref, (completed.identity, oid))[1] != oid:
                results[completed.identity] = ValueError(
                    "one completion generation is bound to two sources in this batch")
        staged = {branch: item for branch, item in staged.items() if item[0] not in results}
        if not staged:
            return results
        legacy_refs = {ref: "refs/heads/" + LEGACY_PREFIX + ref.removeprefix(PREFIX)
                       for ref in staged}
        before = await self.git.als_remote_qualified_refs(
            self.checkout, [*staged, *legacy_refs.values()], repository_url=self.repository_url
        )
        absent = {}
        for branch, (identity, oid) in staged.items():
            remote = before[branch]
            legacy = before[legacy_refs[branch]]
            if legacy.state is RemoteRefState.ERROR:
                results[identity] = GitError(legacy.error or "cannot inspect legacy provenance")
            elif legacy.state is RemoteRefState.PRESENT and legacy.oid != oid:
                results[identity] = ValueError(
                    "immutable completion generation already binds different evidence")
            elif remote.state is RemoteRefState.ERROR:
                results[identity] = GitError(remote.error or "cannot inspect published provenance")
            elif remote.state is RemoteRefState.PRESENT and remote.oid != oid:
                results[identity] = ValueError(
                    "immutable completion generation already binds different evidence")
            elif remote.state is RemoteRefState.ABSENT:
                absent[branch] = oid
        after = await self.git.apush_new_refs(
            self.checkout, absent, qualified=True, repository_url=self.repository_url
        ) if absent else {}
        for branch, (identity, oid) in staged.items():
            if identity in results:
                continue
            verified = after.get(branch, before[branch])
            if verified.state is not RemoteRefState.PRESENT or verified.oid != oid:
                results[identity] = GitError(
                    verified.error or "complete provenance is not verified on the authorized remote")
                continue
            try:
                await self.run("update-ref", branch, oid)
            except GitError as exc:
                results[identity] = exc
                continue
            results[identity] = oid
        return results

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
            "--format=%(refname)", PREFIX + "replacements/",
            "refs/remotes/origin/" + LEGACY_PREFIX + "replacements/",
            "refs/heads/" + LEGACY_PREFIX + "replacements/")).splitlines()
        if len(refs) > MAX_REPLACEMENTS:
            raise ValueError("replacement inventory exceeds bounded evaluation limit")
        replacements = []
        for ref in refs:
            replacement = await self._read(ref)
            if replacement and replacement["kind"] == "replacement":
                replacements.append(replacement)
        # Repairs may themselves be replaced. Traverse only exact immutable
        # bindings, with a visited set and the same bounded ref inventory.
        pending = [completed]
        seen = set()
        while pending:
            binding = pending.pop()
            key = _json(asdict(binding))
            if key in seen:
                continue
            seen.add(key)
            for replacement in replacements:
                if asdict(binding) not in replacement["replaces"]:
                    continue
                source = replacement["source_oid"]
                if await self.ancestor(source, target_oid):
                    return True
                for next_replacement in replacements:
                    for item in next_replacement["replaces"]:
                        if item["source_oid"] == source:
                            identity = CompletionIdentity(**item["identity"])
                            original = await self.read_completion(identity)
                            if original and original["artifact"] and original["source_oid"] == source:
                                pending.append(CompletedSource(identity, source))
        return False


async def record_worker_completion(
    db, git, task, project, checkout, generation, *, commit="", no_code_intent=False,
    allow_unpublished_no_change=False,
):
    """Verify and publish completion evidence before the task can become terminal."""
    from src.integration.hierarchy import resolve_workspace_checkpoint
    from sqlalchemy import select

    repo = await db.get_repo(task.repo_id or project.integration_repository_id or "")
    if repo is None:
        raise ValueError("completion provenance has no authorized repository")
    origin = await db.get_task_branch_origin_for_promotion(task.id, repo.id)
    unchanged_base = None
    if allow_unpublished_no_change:
        unchanged_base = origin.get("base_sha") if origin else None
        if not is_valid_git_oid(unchanged_base):
            raise ValueError("no-change completion requires its exact recorded origin base")
    subject = {"id": task.id, "repo_id": repo.id, "branch_name": task.branch_name}
    source = await resolve_workspace_checkpoint(db, git, subject, repo, unchanged_base=unchanged_base)
    if commit and commit != source:
        raise ValueError("--commit must identify the exact final source, not an earlier commit")
    store = GitProvenance(git, checkout, repository_url=repo.url)
    from src.integration.batches import BatchStore
    from src.integration.repair import OrdinaryRepairService

    ordinary = await OrdinaryRepairService(db).input(task.id)
    if ordinary is not None:
        batches = BatchStore(db)
        batch = await batches.get(ordinary["batch_id"])
        if batch is None or (batch.project_id, batch.repository_id) != (project.id, repo.id):
            raise ValueError("ordinary repair frozen batch identity changed")
        missing = [member.task_id for member in await batches.members(batch.id)
                   if not await store.ancestor(member.source_sha, source)]
        if missing:
            raise ValueError(
                "ordinary repair does not contain every frozen source; still to merge: "
                + ", ".join(missing)
            )
    from src.database.tables import projects
    from src.integration.promotion_routing import promotion_origin_target

    async with db._engine.connect() as conn:
        flow, cutover = (await conn.execute(select(projects.c.promotion_flow,
            projects.c.default_branch_cutover).where(projects.c.id == project.id))).one()
    target = promotion_origin_target(repo.default_branch, flow, origin, repo.id, cutover)
    base = await store.run("rev-parse", "--verify", "refs/remotes/origin/" + target)
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
            raise ProvenanceMigrationRequired("invalid exact development repair contract", project.id)
        for member in contract:
            completion = await db.get_task_completion(member["task_id"])
            if completion is None or completion.outcome != "pass":
                raise ProvenanceMigrationRequired(
                    "repair source has no passing immutable completion", project.id
                )
            original = CompletionIdentity(project.id, repo.id, member["task_id"], completion.id)
            if await store.read_completion(original) is None:
                # Upgrade bridge for an original closed before provenance.
                # Ambiguous old closes still require the explicit migration.
                exact = await legacy_repair_source(
                    db, store, project.id, task.id, member, completion, repair_head=source
                )
                if exact is None:
                    raise ProvenanceMigrationRequired(
                        f"unlabelled repair source {member['task_id']} requires exact "
                        "provenance migration", project.id, task_id=task.id,
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
    if await resolve_workspace_checkpoint(
        db, git, subject, repo, unchanged_base=unchanged_base,
    ) != source or await db.get_task_branch_origin_for_promotion(task.id, repo.id) != origin:
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
    async with db._engine.connect() as conn:
        row = await filing_operation_on(conn, delivery_id)
    if (
        row is None or row["project_id"] != project_id
        or float(row["created_at"]) < completion.completed_at
        or not any(isinstance(item, dict) and item.get("task_id") == member["task_id"]
                   and item.get("source_sha") == source for item in row["manifest"] or [])
    ):
        return None
    try:
        await store.exact(source)
        if repair_head is not None and not await store.ancestor(source, repair_head):
            return None
    except GitError:
        return None  # a source this checkout lacks is not in the repair either
    return source


async def filing_operation_on(conn, delivery_id):
    """The publisher attempt a repair's evidence names, as first recorded.

    A current ``development.operation`` event, or a retired journal row retained
    as :data:`LEGACY_PROVENANCE_EVENT`. Its manifest and creation time fence a
    legacy source binding; neither says anything about delivery.
    """
    from sqlalchemy import and_, cast, or_, select
    from sqlalchemy.dialects.postgresql import JSONB

    from src.database.tables import events

    payload = cast(events.c.payload, JSONB)
    raw = await conn.scalar(select(events.c.payload).where(or_(
        and_(events.c.event_type == "development.operation",
             payload["id"].as_string() == delivery_id),
        and_(events.c.event_type == LEGACY_PROVENANCE_EVENT,
             payload["legacy_id"].as_string() == delivery_id),
    )).order_by(events.c.id).limit(1))
    return json.loads(raw) if raw else None
