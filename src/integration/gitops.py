"""Shared retained-repository mechanisms for the Git-first batch pipeline.

These ports do not activate an engine or select policy. The calling command owns
admission and fenced publication. Subject adapters preserve the existing engine
during cutover; the shared merge and transport carry no progress journal.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import integration_branch_owners
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitError, GitManager, commit_identity
from src.integration.development import DevelopmentBusy, publisher_exclusion
from src.integration.migration_heads import declaration
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchOwnership, BranchOwnershipError, StaleFence
from src.integration.regeneration import GeneratedMergeConflict, merge_generated_tree
from src.integration.source_trailer import source_identity, with_source_trailers
from src.integration.subjects import (
    AncestryArgs,
    MaterializeRefArgs,
    MergeMembersArgs,
    PreserveArgs,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    PublishArgs,
    Subject,
    SubjectEngine,
    SubjectState,
)

ZERO = "0" * 40


@dataclass(frozen=True)
class RetainedRepository:
    """A repository resolver supplies an isolated retained clone, never a slot."""

    repository_id: str
    store: Path
    binding: GitHubRepositoryBinding
    default_branch: str
    regenerate: str | None = None
    regenerate_timeout_seconds: int = 600


class SubjectGitAuthority:
    """Recheck the durable engine, human hold and branch owner before a write.

    ``trusted_green`` is supplied by the existing CI/evidence service. It must
    validate the producer, check-set version, generation and exact head; a
    caller-supplied boolean or a journal row alone is never CI authority.
    """

    def __init__(
        self, db, *, trusted_green: Callable[[Subject, str], Awaitable[bool]], clock=time.time
    ):
        self.db, self.trusted_green, self.clock = db, trusted_green, clock
        self.owners = BranchOwnership(db, clock=clock)

    async def __call__(self, subject: Subject, fence: Fence | None = None):
        row = await self.db.get_integration_subject(subject.id)
        self._subject_current(subject, row)
        if fence is not None:
            self._fence_current(subject, fence)
            await self.owners.assert_current(
                fence, expected_role="collector" if subject.writer.task_id is None else None
            )
            owner = await self.owners.get_owner(fence.target)
            self._lease_current(owner)

    def _subject_current(self, subject, row):
        if row is None:
            raise StaleFence("subject disappeared")
        current = Subject.from_row(row)
        if (
            current.engine is not SubjectEngine.RECONCILER
            or current.schedule.state in {SubjectState.HELD, SubjectState.DONE}
            or current.generation != subject.generation
            or current.policy != subject.policy
            or current.repository_id != subject.repository_id
            or current.project_id != subject.project_id
            or current.kind != subject.kind
            or current.phase != subject.phase
            or current.target_ref != subject.target_ref
            or current.head_sha != subject.head_sha
            or current.base_sha != subject.base_sha
            or current.writer != subject.writer
        ):
            raise StaleFence("subject engine, identity or human hold changed")

    def _lease_current(self, owner):
        if owner is None or (
            owner["expires_at"] is not None and owner["expires_at"] <= self.clock()
        ):
            raise StaleFence("branch lease expired")

    @staticmethod
    def _fence_current(subject, fence):
        if fence.target.repository_id != subject.repository_id:
            raise StaleFence("fence names another repository")
        # The repair/verifier writer is optional. Ordinary construction and
        # publication belong to the subject's canonical collector, whose lease
        # is still checked against the durable branch-owner row below.
        if subject.writer.task_id is None:
            if fence.owner_id not in {subject.id, subject.batch_id}:
                raise StaleFence("fence differs from the subject collector")
        elif fence.owner_id != subject.writer.task_id or fence.token != subject.writer.fence_token:
            raise StaleFence("fence differs from the subject writer")

    @asynccontextmanager
    async def mutation(self, subject, fence=None, *, unowned_ref=None):
        """Hold subject/owner authority across the transport's bounded push.

        The intent is already committed. A hold, engine rollback or fence
        transfer cannot race this write. The transport also gets the lease's
        deadline, so an expired grant cannot finish a new authenticated push.
        """
        async with self.db.immediate() as conn:
            row = await self.db.lock_integration_subject_on(conn, subject.id)
            self._subject_current(subject, row)
            deadline = None
            if fence is not None:
                self._fence_current(subject, fence)
                owner = await self.owners._locked_row(conn, fence.target)
                self.owners._require_current(owner, fence)
                if subject.writer.task_id is None and owner["owner_role"] != "collector":
                    raise StaleFence("subject collector lease has another role")
                if owner["handoff_state"] not in {"reserved", "attached"}:
                    raise StaleFence("branch owner is not write-authoritative")
                self._lease_current(owner)
                deadline = owner["expires_at"]
            if unowned_ref is not None:
                # A released tombstone makes an absent ownership key lockable.
                # A concurrent first acquirer waits on its unique key, then
                # takes the next fence only after this bounded mutation ends.
                now = self.clock()
                await conn.execute(
                    pg_insert(integration_branch_owners)
                    .values(
                        id="git-unowned-"
                        + hashlib.sha256(
                            f"{subject.repository_id}:{unowned_ref}".encode()
                        ).hexdigest(),
                        repository_id=subject.repository_id,
                        ref=unowned_ref,
                        owner_id=subject.id,
                        owner_role="collector",
                        fence_token=0,
                        handoff_state="released",
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(constraint="uq_integration_branch_owners_ref")
                )
                owner = await self.owners._locked_row(
                    conn, BranchKey(repository_id=subject.repository_id, branch=unowned_ref)
                )
                if owner["handoff_state"] != "released":
                    raise StaleFence("retention/cleanup ref acquired a writer")
            yield deadline

    async def unowned(self, repository_id: str, ref: str) -> bool:
        owner = await self.owners.get_owner(BranchKey(repository_id=repository_id, branch=ref))
        return owner is None or owner["handoff_state"] == "released"


def branch(ref: str) -> str:
    if not ref.startswith("refs/heads/") or not ref.removeprefix("refs/heads/"):
        raise ValueError("remote mutation requires a fully qualified branch ref")
    return ref.removeprefix("refs/heads/")


class GitOperations:
    """Bind rows 3–7 to typed primitive ports using existing retained-clone APIs.

    ``exclusion`` must be the repository-wide publisher fence. By default this
    is the *same* PostgreSQL advisory lock used by the development publisher,
    so old and new engines cannot publish concurrently during the cutover. It
    is called as ``exclusion(repository_id, subject)``: a repository the
    reconciler engine owns admits no unnamed root mutation, so the fence has to
    carry the exact subject the primitive acts for.
    ``repository`` is subject-scoped (a frozen ``Batch`` also qualifies: it carries
    the same ``repository_id``) for the same reason: the retained clone a
    primitive acts in is the one carrying *that* subject's own pinned settings,
    so it is resolved through the subject rather than through a bare repository
    id a caller could answer from anything else.
    Authority is mandatory; batch construction uses immutable Git inputs and
    writes no journal. Tests may inject real-Git ports.
    """

    def __init__(
        self,
        db,
        *,
        git: GitManager,
        repository: Callable[[Subject], Awaitable[RetainedRepository]],
        authority: SubjectGitAuthority,
        exclusion=None,
    ):
        self.git, self.repository, self.authority = git, repository, authority
        self.db = db
        self.exclusion = exclusion or (
            lambda rid, subject=None: publisher_exclusion(db, rid, subject=subject)
        )

    def bind(self, ports: PrimitivePorts):
        for primitive, method in (
            (Primitive.GIT_MATERIALIZE_REF, self.materialize_ref),
            (Primitive.GIT_MERGE_MEMBERS, self.merge_members),
            (Primitive.GIT_PRESERVE, self.preserve),
            (Primitive.GIT_PUBLISH, self.publish),
            (Primitive.GIT_ANCESTRY, self.ancestry),
        ):
            ports.bind(primitive, method)

    async def _repository(self, subject, repository_id=None):
        rid = repository_id or subject.repository_id
        if rid != subject.repository_id:
            raise StaleFence("request names another repository")
        return await self.retained(subject)

    async def retained(self, subject_or_repository_id):
        """The validated retained clone for a subject, or for a bare repository id.

        The subject runtimes resolve by subject so the clone carries that
        subject's own pinned settings. Batch construction names a repository
        instead: its members are immutable Git facts with no subject to scope
        by, and the batch's own lease is the authority for its writes.
        """
        repository_id = getattr(subject_or_repository_id, "repository_id",
                                subject_or_repository_id)
        repo = await self.repository(subject_or_repository_id)
        if repo.repository_id != repository_id:
            raise StaleFence("retained repository binding mismatch")
        return await self.validate_repository(repo)

    async def validate_repository(self, repo):
        """Reject mutable ancestry customization for both batch and legacy calls."""
        common = Path(await self.run(repo, "rev-parse", "--git-common-dir"))
        common = common if common.is_absolute() else repo.store / common
        grafts = common / "info" / "grafts"
        if grafts.exists() and grafts.stat().st_size:
            raise GitError("retained repository has ancestry grafts")
        if await self.run(repo, "for-each-ref", "--format=%(refname)", "refs/replace/"):
            raise GitError("retained repository has replacement objects")
        return repo

    async def run(self, repo, *args, stdin=None, env=None):
        result = await self.git.arun_git_result(
            ["--no-replace-objects", *args],
            cwd=str(repo.store),
            stdin=stdin,
            env=env,
        )
        if result.returncode:
            raise GitError(result.stderr or result.stdout or "git probe failed")
        return result.stdout.strip()

    async def exact(self, repo, oid):
        if len(oid) != 40 or any(c not in "0123456789abcdef" for c in oid):
            raise ValueError("expected a full commit SHA")
        if await self.run(repo, "rev-parse", "--verify", f"{oid}^{{commit}}") != oid:
            raise GitError("commit identity mismatch")
        return oid

    async def remote(self, repo, ref):
        return await self.git.aremote_branch_head(repository=repo.binding, branch=branch(ref))

    async def is_ancestor(self, repo, a, b):
        result = await self.git.arun_git_result(
            ["--no-replace-objects", "merge-base", "--is-ancestor", a, b],
            cwd=str(repo.store),
        )
        if result.returncode not in {0, 1}:
            raise GitError(result.stderr or "ancestry probe failed")
        return result.returncode == 0

    async def push(self, repo, ref, sha, expected, *, deadline=None):
        await self.git.apush_repository_oid(
            str(repo.store),
            repository=repo.binding,
            tip_oid=sha,
            branch=branch(ref),
            expected_old_oid=expected,
            authority_deadline=deadline,
        )

    async def _fence(self, subject, ref):
        writer = subject.writer
        target = BranchKey(repository_id=subject.repository_id, branch=ref)
        if writer.task_id is None:
            owner = await self.authority.owners.get_owner(target)
            if owner is None:
                raise StaleFence("subject has no collector lease")
            fence = Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"])
        else:
            if writer.fence_token is None:
                raise StaleFence("subject writer has no branch fence")
            fence = Fence(target=target, owner_id=writer.task_id, token=writer.fence_token)
        await self.authority(subject, fence)
        return fence

    async def _transfer(
        self, subject, repo, primitive, ref, sha, expected, *, fence=None, green=False
    ):
        """Read-back and expected-old transport settle retries without a journal."""
        try:
            actual = await self.remote(repo, ref)
        except Exception as exc:
            return "unknown_after_push", {"reason": str(exc)}
        if actual == sha:
            return "published", {
                "head": sha,
                "observed_sha": actual,
                "replayed": True,
            }
        if (actual or ZERO) != expected:
            return "target_moved", {"observed_sha": actual}
        await self.authority(subject, fence)
        if green and not await self.authority.trusted_green(subject, sha):
            raise StaleFence("trusted exact-head green changed before push")
        error = None
        try:
            async with self.authority.mutation(
                subject, fence, unowned_ref=ref if fence is None else None
            ) as deadline:
                await self.push(repo, ref, sha, expected, deadline=deadline)
        except Exception as exc:
            error = str(exc)
        try:
            actual = await self.remote(repo, ref)
        except Exception as exc:
            return "unknown_after_push", {"reason": str(exc), "error": error}
        if actual == sha:
            return "published", {"head": sha, "observed_sha": actual}
        if (actual or ZERO) != expected:
            return "target_moved", {"observed_sha": actual, "error": error}
        return "unknown_after_push", {"observed_sha": actual, "error": error}

    async def materialize_ref(self, subject: Subject, args: MaterializeRefArgs):
        p = args.primitive
        try:
            repo = await self._repository(subject, args.repository_id)
            async with self.exclusion(repo.repository_id, subject):
                await self.exact(repo, args.base_sha)
                fence = await self._fence(subject, args.ref)
                # The default branch always goes through publish's green guard.
                if branch(args.ref) == repo.default_branch:
                    return PrimitiveOutcome.unknown(p, "default_branch_requires_publish")
                actual = await self.remote(repo, args.ref)
                if actual is not None:
                    return PrimitiveOutcome(
                        primitive=p,
                        outcome=("exists_exact" if actual == args.base_sha else "exists_other"),
                        detail={"observed_sha": actual},
                    )
                outcome, detail = await self._transfer(
                    subject, repo, p, args.ref, args.base_sha, ZERO, fence=fence
                )
                if outcome == "published":
                    return PrimitiveOutcome(primitive=p, outcome="created", detail=detail)
                if outcome == "target_moved":
                    return PrimitiveOutcome(primitive=p, outcome="exists_other", detail=detail)
                return PrimitiveOutcome.unknown(p, "materialization_unconfirmed", transfer=detail)
        except (GitError, BranchOwnershipError, DevelopmentBusy, ValueError) as exc:
            return PrimitiveOutcome.unknown(p, str(exc))

    async def preserve(self, subject: Subject, args: PreserveArgs):
        p = args.primitive
        try:
            repo = await self._repository(subject, args.repository_id)
            async with self.exclusion(repo.repository_id, subject):
                await self.authority(subject)
                await self.exact(repo, args.sha)
                ref = args.retention_ref
                if branch(ref) == repo.default_branch or ref == subject.target_ref:
                    raise StaleFence("retention ref is a delivery target")
                if not await self.authority.unowned(repo.repository_id, ref):
                    raise StaleFence("retention ref has a writer")
                actual = await self.remote(repo, ref)
                if actual == args.sha:
                    evidence = {"ref": ref, "head": args.sha}
                    return PrimitiveOutcome(primitive=p, outcome="exists", detail=evidence)
                if actual is not None:
                    return PrimitiveOutcome.unknown(p, "retention_ref_moved", observed_sha=actual)
                outcome, detail = await self._transfer(subject, repo, p, ref, args.sha, ZERO)
                if outcome == "published":
                    return PrimitiveOutcome(primitive=p, outcome="preserved", detail=detail)
                return PrimitiveOutcome.unknown(p, "preservation_unconfirmed", transfer=detail)
        except (GitError, BranchOwnershipError, DevelopmentBusy, ValueError) as exc:
            return PrimitiveOutcome.unknown(p, str(exc))

    async def publish(self, subject: Subject, args: PublishArgs):
        p = args.primitive
        try:
            repo = await self._repository(subject)
            ref = args.fence.target.branch
            if ref != subject.target_ref or args.new_sha != subject.head_sha:
                raise StaleFence("publication differs from the subject's exact target/head")
            async with self.exclusion(repo.repository_id, subject):
                await self.authority(subject, args.fence)
                await self.exact(repo, args.new_sha)
                if (
                    branch(ref) == repo.default_branch
                    and subject.base_sha is not None
                    and args.expected_old_sha != subject.base_sha
                ):
                    raise StaleFence(
                        "default-branch publication differs from the construction base"
                    )
                if branch(ref) == repo.default_branch or args.require_green:
                    if not await self.authority.trusted_green(subject, args.new_sha):
                        return PrimitiveOutcome.unknown(p, "trusted_exact_head_green_required")
                if args.expected_old_sha != ZERO:
                    await self.exact(repo, args.expected_old_sha)
                    if not await self.is_ancestor(repo, args.expected_old_sha, args.new_sha):
                        return PrimitiveOutcome.unknown(p, "publication_discards_target_history")
                outcome, detail = await self._transfer(
                    subject,
                    repo,
                    p,
                    ref,
                    args.new_sha,
                    args.expected_old_sha,
                    fence=args.fence,
                    green=branch(ref) == repo.default_branch or args.require_green,
                )
                return PrimitiveOutcome(primitive=p, outcome=outcome, detail=detail)
        except (GitError, BranchOwnershipError, DevelopmentBusy, ValueError) as exc:
            return PrimitiveOutcome.unknown(p, str(exc))

    async def _migrations(self, repo, sha):
        heads = []
        paths = (await self.run(repo, "ls-tree", "-r", "--name-only", "-z", sha)).split("\0")
        for path in paths:
            parts = PurePosixPath(path).parts
            if (
                len(parts) >= 3
                and parts[-2] == "versions"
                and parts[-3] in {"alembic", "migrations"}
                and path.endswith(".py")
                and parts[-1] != "__init__.py"
            ):
                heads.append(declaration(path, await self.run(repo, "show", f"{sha}:{path}")))
        return heads

    async def _migration_conflicts(self, repo, current, member):
        ours = await self._migrations(repo, current)
        theirs = await self._migrations(repo, member.head_sha)
        base = {h.path: h for h in await self._migrations(repo, member.base_sha)}
        added = [h for h in theirs if h.path not in base]
        conflicts = []
        for h in added:
            for other in ours:
                if other.scope != h.scope or other == h:
                    continue
                if other.revision == h.revision or (
                    set(other.down_revisions or (None,)) & set(h.down_revisions or (None,))
                    and other.revision not in h.down_revisions
                    and h.revision not in other.down_revisions
                ):
                    conflicts.extend((h.path, other.path))
        return sorted(set(conflicts))

    async def merge_sources(self, repo, base_sha, members, *, created_at, squash=False,
                            regenerate_generated=True):
        """One deterministic merge path for every target, without progress records.

        Source bases and heads are frozen retained inputs. Generated conflicts
        rebuild from merged sources; colliding migration heads become ordinary
        repair work. Local object pins retain computation, never delivery truth.
        """
        await self.validate_repository(repo)
        await self.exact(repo, base_sha)
        current, results = base_sha, []
        for member in members:
            try:
                await self.exact(repo, member.head_sha)
                await self.exact(repo, member.base_sha)
                if not await self.is_ancestor(repo, member.base_sha, member.head_sha):
                    raise ValueError("recorded source base is not an ancestor")
                if await self.git.areserved_paths_in_diff(
                    str(repo.store), member.base_sha, member.head_sha
                ):
                    raise ValueError("source changes reserved AQ bookkeeping paths")
            except (GitError, ValueError, TypeError) as exc:
                return {"outcome": "source_moved", "head": current, "members": results,
                        "member": member.task_id, "reason": str(exc)}
            head, regenerated = current, False
            if not await self.is_ancestor(repo, member.head_sha, current):
                collisions = await self._migration_conflicts(repo, current, member)
                if collisions:
                    return {"outcome": "conflict", "head": current, "members": results,
                            "member": member.task_id, "files": collisions,
                            "reason": "alembic_head_collision"}
                args = ["--no-replace-objects", "merge-tree", "--write-tree",
                        f"--merge-base={member.base_sha}", current, member.head_sha]
                try:
                    with commit_identity(self.git.resolve_commit_identity()):
                        tree = await merge_generated_tree(
                            self.git, repo.store, args,
                            command=(repo.regenerate or "") if regenerate_generated else "",
                            timeout_seconds=repo.regenerate_timeout_seconds,
                        )
                except GeneratedMergeConflict as exc:
                    files = sorted({line.split("\t", 1)[1]
                                    for line in exc.stdout.splitlines()[1:]
                                    if "\t" in line and
                                    line.split("\t", 1)[0].endswith((" 1", " 2", " 3"))})
                    return {"outcome": "conflict", "head": current, "members": results,
                            "member": member.task_id, "files": files, "reason": exc.reason}
                # Attribute-driven regeneration can change the raw merge tree.
                regenerated = bool(repo.regenerate and regenerate_generated)
                stamp = f"@{int(created_at)} +0000"
                parents = ["-p", current] + ([] if squash else ["-p", member.head_sha])
                head = await self.run(
                    repo, *self.git.resolve_commit_identity().config_args(),
                    "commit-tree", tree, *parents, "-m",
                    with_source_trailers(f"Integrate {member.task_id} ({member.head_sha})",
                                         [source_identity(member.task_id, member.head_sha)]),
                    env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
                )
            key = hashlib.sha256(f"{base_sha}:{current}:{member.head_sha}:{head}".encode()).hexdigest()
            await self.run(repo, "update-ref", f"refs/aq/batch-objects/{key}", head)
            results.append({"member": member.task_id, "source": member.head_sha,
                            "head": head, "regenerated": regenerated})
            current = head
        return {"outcome": "merged", "head": current, "members": results}

    async def merge_members(self, subject: Subject, args: MergeMembersArgs):
        """Compatibility primitive; the batch pipeline shares its merge implementation."""
        p = args.primitive
        try:
            repo = await self._repository(subject)
            async with self.exclusion(repo.repository_id, subject):
                await self.authority(subject)
                if args.target_ref != subject.target_ref:
                    raise StaleFence("merge target differs from subject")
                actual = await self.remote(repo, args.target_ref)
                if actual != args.base_sha:
                    return PrimitiveOutcome(primitive=p, outcome="base_moved",
                                            detail={"observed_sha": actual})
                result = await self.merge_sources(
                    repo, args.base_sha, args.members, created_at=subject.created_at,
                    regenerate_generated=args.regenerate_generated,
                )
                await self.authority(subject)
                actual = await self.remote(repo, args.target_ref)
                if actual != args.base_sha:
                    result.update(outcome="base_moved", observed_sha=actual)
                outcome = result.pop("outcome")
                return PrimitiveOutcome(primitive=p, outcome=outcome, detail=result)
        except (GitError, BranchOwnershipError, DevelopmentBusy, ValueError) as exc:
            return PrimitiveOutcome.unknown(p, str(exc))

    async def ancestry(self, subject: Subject, args: AncestryArgs):
        p = args.primitive
        try:
            repo = await self._repository(subject, args.repository_id)
            facts = []
            for query in args.queries:
                # Resolve both sides exactly once, then use only these SHAs.
                a = await self.run(repo, "rev-parse", "--verify", f"{query.ancestor}^{{commit}}")
                b = await self.run(repo, "rev-parse", "--verify", f"{query.descendant}^{{commit}}")
                await self.exact(repo, a)
                await self.exact(repo, b)
                ancestor = await self.is_ancestor(repo, a, b)
                bases = await self.git.arun_git_result(
                    ["--no-replace-objects", "merge-base", "--all", a, b], cwd=str(repo.store)
                )
                if bases.returncode not in {0, 1}:
                    raise GitError(bases.stderr or "merge-base failed")
                cherry = await self.run(repo, "cherry", b, a)
                patches = cherry.splitlines()
                facts.append(
                    {
                        "ancestor": a,
                        "descendant": b,
                        "is_ancestor": ancestor,
                        "merge_bases": bases.stdout.split(),
                        "patch_equivalent": bool(patches)
                        and all(line.startswith("- ") for line in patches),
                        "patches": patches,
                    }
                )
            return PrimitiveOutcome(primitive=p, outcome="facts", detail={"queries": facts})
        except (GitError, StaleFence, ValueError) as exc:
            return PrimitiveOutcome.unknown(p, str(exc))
