"""Durable retirement decisions and pre-delete local/remote SHA audits."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from src.database.queries.hierarchy_queries import LIVE_SESSION_STATES
from src.database.tables import (
    branch_deletion_audit,
    branch_retirements as retirements,
    integration_batches,
    integration_branch_owners,
    projects,
    repos,
    sessions,
    task_branch_origins,
    task_completion_records,
    tasks,
    workspaces,
)
from src.git.manager import GitError, repository_urls_match
from src.integration.branch_discard import BranchDiscardService, _bundle
from src.integration.delivery_branches import (
    LIVE_TASK_STATUSES,
    branch_of,
    deletable,
    live_branch_references,
    repository_protected_branches,
)
from src.integration.lock import BranchLock
from src.integration.models import BranchKey
from src.integration.ownership import BranchBusy


async def request_branch_retirement_on(
    conn, *, request_id, project_id, repository_id, branch, reason, task_id=None,
    claim_epoch=None, now=None,
) -> str | None:
    """Record intent on the lifecycle writer's transaction; never perform Git here."""
    branch = branch_of(branch)
    if not branch or not repository_id:
        return None
    statement = insert(retirements).values(
        id=uuid.uuid4().hex, request_id=request_id, project_id=project_id,
        repository_id=repository_id, task_id=task_id, branch=branch, reason=reason,
        claim_epoch=claim_epoch, requested_at=time.time() if now is None else now,
    ).on_conflict_do_nothing(index_elements=["request_id", "repository_id", "branch"])
    await conn.execute(statement)
    return await conn.scalar(select(retirements.c.id).where(
        retirements.c.request_id == request_id, retirements.c.repository_id == repository_id,
        retirements.c.branch == branch,
    ))


async def request_task_retirement_on(conn, task_id, *, request_id, reason, now=None):
    """Queue a task's exact named branch, WIP sibling and origin refs."""
    row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
    if row is None:
        return
    repository_id = row["repo_id"] or await conn.scalar(
        select(projects.c.integration_repository_id).where(projects.c.id == row["project_id"])
    )
    if not repository_id:
        repository_ids = list((await conn.execute(
            select(repos.c.id).where(repos.c.project_id == row["project_id"])
        )).scalars())
        repository_id = repository_ids[0] if len(repository_ids) == 1 else None
    named = {(repository_id, branch_of(row["branch_name"]))}
    named.update((origin.repository_id, branch_of(origin.branch_name)) for origin in (
        await conn.execute(select(task_branch_origins.c.repository_id,
                                  task_branch_origins.c.branch_name).where(
            task_branch_origins.c.task_id == task_id,
        ))
    ))
    for repo_id, branch in named:
        if repo_id and branch:
            for ref in (branch, branch + "-wip"):
                await request_branch_retirement_on(
                    conn, request_id=request_id, project_id=row["project_id"],
                    repository_id=repo_id, branch=ref, task_id=task_id, reason=reason,
                    claim_epoch=row["claim_epoch"], now=now,
                )


async def request_batch_retirement_on(conn, batch, *, reason, now=None):
    """Queue only the private refs of a committed abort or supersede decision."""
    from src.integration.batches import candidate_ref
    from src.integration.train_sources import RETAINED_CANDIDATE_PREFIX

    for branch in (candidate_ref(batch["id"]), RETAINED_CANDIDATE_PREFIX + batch["id"]):
        await request_branch_retirement_on(
            conn, request_id="abort:" + batch["id"], project_id=batch["project_id"],
            repository_id=batch["repository_id"], branch=branch, reason=reason, now=now,
        )


class RetirementConflict(GitError):
    pass


class RetirementWithdrawn(GitError):
    pass


class BranchRetirementService(BranchDiscardService):
    """Delete only explicitly retired refs; retain audit and verified bundles."""

    def __init__(self, db, *, candidate_store=None, **kwargs):
        super().__init__(db, **kwargs)
        self.candidate_store = candidate_store

    async def discard_origin(self, origin):
        async with self.db.immediate() as conn:
            repository = await self.db.get_repo(origin["repository_id"])
            if repository is None:
                return "retryable", "discard repository is unavailable"
            identity = await request_branch_retirement_on(
                conn, request_id="origin:" + origin["id"],
                project_id=repository.project_id, repository_id=repository.id,
                task_id=origin["task_id"], branch=origin["branch_name"],
                reason="explicit branch discard", now=origin["discard_requested_at"],
            )
        if identity is None:
            return "conflict", "origin branch is unknown"
        state, error = await self.advance(identity)
        return ("retryable" if state == "pending" else
                "conflict" if state == "withdrawn" else state), error

    async def drain_due(self, *, now=None, limit=20):
        now = self.clock() if now is None else now
        await self._import_previous_audits(limit=limit)
        async with self.db._engine.connect() as conn:
            ids = list((await conn.execute(select(retirements.c.id).where(
                retirements.c.state == "pending", retirements.c.next_attempt_at <= now,
            ).order_by(retirements.c.requested_at, retirements.c.id).limit(limit))).scalars())
        return [await self.advance(identity, now=now) for identity in ids]

    async def _import_previous_audits(self, *, limit):
        """Preserve pending audit decisions from the retained WIP schema."""
        async with self.db.immediate() as conn:
            rows = (await conn.execute(select(branch_deletion_audit).where(
                branch_deletion_audit.c.outcome.in_(("pending", "held", "failed")),
                branch_deletion_audit.c.repository_id.is_not(None),
                ~select(retirements.c.id).where(
                    retirements.c.request_id == "previous-audit:" + branch_deletion_audit.c.id,
                ).exists(),
            ).order_by(branch_deletion_audit.c.recorded_at).limit(limit)
              .with_for_update(skip_locked=True))).mappings()
            for previous in rows:
                identity = await request_branch_retirement_on(
                    conn, request_id="previous-audit:" + previous["id"],
                    project_id=previous["project_id"], repository_id=previous["repository_id"],
                    task_id=previous["task_id"], branch=previous["branch"],
                    reason=previous["reason"], now=previous["recorded_at"],
                )
                if identity:
                    await conn.execute(update(retirements).where(
                        retirements.c.id == identity, retirements.c.attempts == 0,
                        retirements.c.evidence == {},
                    ).values(evidence={"previous_audit": {
                        "id": previous["id"], "sha": previous["head_sha"],
                        "bundle": previous["backup_path"],
                    }}))

    async def advance(self, identity, *, now=None):
        now = self.clock() if now is None else now
        async with self.db.immediate() as conn:
            row = (await conn.execute(select(retirements).where(
                retirements.c.id == identity,
            ).with_for_update())).mappings().first()
            if row is None:
                return "withdrawn", "retirement is unavailable"
            if row["state"] != "pending" or row["next_attempt_at"] > now:
                return row["state"], row["last_error"]
            row = dict(row)
            row["attempts"] += 1
            await conn.execute(update(retirements).where(retirements.c.id == identity).values(
                attempts=row["attempts"], next_attempt_at=now + self._backoff(row["attempts"]),
            ))
        state, error = "complete", None
        try:
            await self._retire(row)
        except RetirementWithdrawn as exc:
            state, error = "withdrawn", str(exc)
        except RetirementConflict as exc:
            state, error = "conflict", str(exc)
        except Exception as exc:
            state, error = "pending", str(exc) or type(exc).__name__
        async with self.db.immediate() as conn:
            await conn.execute(update(retirements).where(
                retirements.c.id == identity, retirements.c.attempts == row["attempts"],
            ).values(state=state, last_error=error))
            if state == "complete" and row["request_id"].startswith("origin:"):
                await conn.execute(update(task_branch_origins).where(
                    task_branch_origins.c.id == row["request_id"].removeprefix("origin:"),
                ).values(discard_state="complete", discard_last_error=None,
                         discard_next_attempt_at=None))
            if row["request_id"].startswith("previous-audit:") and state != "pending":
                await conn.execute(update(branch_deletion_audit).where(
                    branch_deletion_audit.c.id == row["request_id"].removeprefix("previous-audit:"),
                ).values(outcome={"complete": "deleted", "conflict": "moved",
                                  "withdrawn": "held"}[state], detail_reason=error,
                         next_attempt_at=None, deleted_at=now if state == "complete" else None))
        return state, error

    async def _guard(self, conn, row, *, holder=None):
        current = await conn.scalar(select(retirements.c.attempts).where(
            retirements.c.id == row["id"], retirements.c.state == "pending",
        ).with_for_update())
        if current != row["attempts"]:
            raise BranchBusy("retirement attempt was superseded")
        branch = row["branch"]
        protected = await repository_protected_branches(
            conn, row["repository_id"], default_branch="main",
        )
        if not deletable(branch, "main", protected=protected | {"main", "dev", "staging"}):
            raise RetirementConflict("protected or non-AQ branch")
        if row["task_id"]:
            task = (await conn.execute(select(tasks).where(
                tasks.c.id == row["task_id"],
            ).with_for_update())).mappings().first()
            if task is not None:
                if (task["status"] in LIVE_TASK_STATUSES
                    or (row["claim_epoch"] is not None
                        and task["claim_epoch"] != row["claim_epoch"])):
                    raise RetirementWithdrawn("task changed or resumed after retirement")
            live = await conn.scalar(select(sessions.c.id).where(
                sessions.c.task_id == row["task_id"], sessions.c.state.in_(LIVE_SESSION_STATES),
            ).limit(1))
            if live:
                raise BranchBusy("task has a live session")
            latest = (await conn.execute(select(task_completion_records.c.id,
                                               task_completion_records.c.completed_at).where(
                task_completion_records.c.task_id == row["task_id"],
            ).order_by(task_completion_records.c.completed_at.desc(),
                       task_completion_records.c.id.desc()).limit(1))).first()
            if row["request_id"].startswith("close:"):
                if latest is None or latest.id != row["request_id"].removeprefix("close:"):
                    raise RetirementWithdrawn("completion changed after retirement")
            elif latest is not None and latest.completed_at > row["requested_at"]:
                raise RetirementWithdrawn("new work completed after retirement")
        # Refuse unreleased owners, including expired legacy rows. The cleanup
        # service never substitutes lease expiry for proof that a writer stopped.
        owner = await conn.scalar(select(integration_branch_owners.c.id).where(
            integration_branch_owners.c.repository_id == row["repository_id"],
            integration_branch_owners.c.ref.in_((branch, "refs/heads/" + branch)),
            integration_branch_owners.c.handoff_state != "released",
            integration_branch_owners.c.owner_id != (holder or ""),
        ).limit(1))
        if owner:
            raise BranchBusy("branch has an active owner")
        held = await live_branch_references(
            conn, retiring_task_id=row["task_id"], retiring_owner_id=holder,
        )
        if branch in held:
            raise BranchBusy(held[branch])
        if row["request_id"].startswith("abort:"):
            from src.integration.batches import candidate_ref
            from src.integration.train_sources import RETAINED_CANDIDATE_PREFIX

            batch_id = row["request_id"].removeprefix("abort:")
            batch = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch_id,
            ).with_for_update())).mappings().first()
            if (batch is None or batch["intent"] != "aborted"
                or batch["lifecycle"] != "aborted"
                or batch["repository_id"] != row["repository_id"]):
                raise RetirementWithdrawn("batch is no longer aborted")
            candidates = {branch_of(candidate_ref(batch_id)),
                          branch_of(RETAINED_CANDIDATE_PREFIX + batch_id)}
            if branch not in candidates or branch == branch_of(batch["target_ref"]):
                raise RetirementConflict("ref is not a private aborted candidate")

    async def _retire(self, row):
        if self.git is None:
            raise GitError("retirement transport is unavailable")
        holder = "service:branch-retirement:" + row["id"]
        async with self.db.immediate() as conn:
            await self._guard(conn, row, holder=holder)
        repository = await self.db.get_repo(row["repository_id"])
        if repository is None:
            raise GitError("retirement repository is unavailable")
        binding = await self._binding(repository)
        client = await self._github_client(binding) if binding else None
        if client is None:
            raise GitError("authenticated retirement transport is unavailable")
        store = self.retained_store(repository.id)
        await self._ensure_store(store)
        paths = await self._checkout_paths(row, repository)
        locks = BranchLock(self.db, clock=self.clock)
        target = BranchKey(repository_id=repository.id, branch=row["branch"])
        async with self.db.immediate() as conn:
            # Check under the acquisition lock as well: an expired owner can
            # arrive after the initial guard, and must never be overwritten.
            previous = await locks.lock_on(conn, target)
            if (previous and previous["handoff_state"] != "released"
                and previous["owner_id"] != holder):
                raise BranchBusy("branch has an unreleased owner")
            fence = await locks.acquire(target, holder, role="integration", conn=conn)
        try:
            # Audit writes commit independently before Git. The exclusion is
            # acquired afterwards so a crash cannot roll back a deletion's audit.
            head = await client.exact_head_ref(row["branch"])
            evidence = dict(row["evidence"])
            previous_sha = evidence.get("previous_audit", {}).get("sha")
            if head and previous_sha and head != previous_sha:
                raise RetirementConflict("remote branch moved since the previous audit")
            remote = dict(evidence.get("remote") or {})
            if head and remote and remote.get("sha") is None:
                raise RetirementConflict("remote branch appeared after audit")
            if head and remote.get("sha") and head != remote["sha"]:
                raise RetirementConflict("remote branch moved after audit")
            if head:
                if not remote:
                    main_head = await client.exact_head_ref(repository.default_branch)
                    if not main_head:
                        raise GitError("default branch cannot be observed for backup")
                    bundle = await self._backup_before_delete(
                        {**row, "task_id": row["task_id"] or row["request_id"]},
                        binding=binding, branch=row["branch"], head=head, main_head=main_head,
                    )
                    remote = {"sha": head, "state": "observed",
                              "bundle": str(bundle) if bundle else None,
                              "backup_base_sha": main_head}
            elif not remote:
                remote = {"sha": None, "state": "absent"}
            evidence["remote"] = remote
            for path in paths:
                for key, ref in self._local_refs(path, row["branch"]):
                    head_local = await self.git.arev_parse(path, ref)
                    if head_local and previous_sha and head_local != previous_sha:
                        raise RetirementConflict("local ref moved since the previous audit")
                    previous = evidence.get(key)
                    if previous and head_local and previous["sha"] != head_local:
                        raise RetirementConflict("local branch moved after audit")
                    if previous:
                        continue
                    bundle = None
                    if head_local:
                        # A tracking ref can retain an older unmerged tip too.
                        local = evidence.get("local:" + path, {})
                        if local.get("sha") == head_local and local.get("bundle"):
                            bundle = local["bundle"]
                        else:
                            bundle = await _bundle(
                                self._run_git, Path(path), {row["branch"]: head_local},
                                main_head=None, backup_dir=self.backup_dir,
                                repository_id=repository.id, now=self.clock(),
                            )
                    evidence[key] = {"sha": head_local, "state": "observed" if head_local else
                                     "absent", "bundle": str(bundle) if bundle else None}
            await self._audit(row, evidence)
            async with locks.exclusion(fence) as owner:
                # Use a separate connection for the task lock; the ref exclusion
                # stays held through the exact-SHA deletion and its read-back.
                async with self.db.immediate() as conn:
                    await self._guard(conn, row, holder=holder)
                    for path in paths:
                        attached = await self.git.aworktree_list(path)
                        if any(branch_of(entry.get("branch")) == row["branch"]
                               for entry in attached):
                            raise BranchBusy("branch is attached to a worktree")
                    current_head = await client.exact_head_ref(row["branch"])
                    if current_head:
                        if (current_head != remote["sha"]
                            or remote["state"] in {"deleted", "absent"}):
                            raise RetirementConflict("remote branch changed after audit")
                        try:
                            await self.git.adelete_repository_ref(
                                str(store), repository=binding, branch=row["branch"],
                                expected_old_oid=remote["sha"],
                                authority_deadline=asyncio.get_running_loop().time()
                                + max(0, owner["expires_at"] - self.clock()),
                            )
                        except GitError:
                            actual = await client.exact_head_ref(row["branch"])
                            if actual is not None:
                                if actual != remote["sha"]:
                                    raise RetirementConflict("remote branch moved during retirement")
                                raise
                    if await client.exact_head_ref(row["branch"]) is not None:
                        raise RetirementConflict("remote deletion was not confirmed")
                    for path in paths:
                        for key, ref in self._local_refs(path, row["branch"]):
                            local = evidence[key]
                            actual = await self.git.arev_parse(path, ref)
                            if actual is not None:
                                if actual != local["sha"]:
                                    raise RetirementConflict("local ref moved during retirement")
                                delete_ref = (self.git.adelete_remote_tracking_ref_exact
                                              if key.startswith("tracking:") else
                                              self.git.adelete_local_ref_exact)
                                await delete_ref(
                                    path, ref=ref, expected_old_oid=actual,
                                )
                                if await self.git.arev_parse(path, ref) is not None:
                                    raise GitError("local deletion was not confirmed")
                            local["state"] = "deleted" if local["sha"] else "absent"
            remote["state"] = "deleted" if remote["sha"] else "absent"
            await self._audit(row, evidence)
        finally:
            await locks.release(fence)

    @staticmethod
    def _local_refs(path, branch):
        return (("local:" + path, "refs/heads/" + branch),
                ("tracking:" + path, "refs/remotes/origin/" + branch))

    async def _audit(self, row, evidence):
        async with self.db.immediate() as conn:
            result = await conn.execute(update(retirements).where(
                retirements.c.id == row["id"], retirements.c.attempts == row["attempts"],
            ).values(evidence=evidence))
            if result.rowcount != 1:
                raise BranchBusy("retirement attempt was superseded")

    async def _checkout_paths(self, row, repository):
        async with self.db._engine.connect() as conn:
            candidates = list((await conn.execute(select(workspaces.c.workspace_path).where(
                workspaces.c.project_id == row["project_id"],
            ))).scalars())
            candidates += list((await conn.execute(select(projects.c.workspace_path).where(
                projects.c.id == row["project_id"],
            ))).scalars())
        candidates += [repository.checkout_base_path, str(self.retained_store(repository.id))]
        if self.candidate_store is not None:
            candidate_store = self.candidate_store(repository)
            if inspect.isawaitable(candidate_store):
                candidate_store = await candidate_store
            candidates.append(candidate_store)
        paths = set()
        for candidate in candidates:
            if not candidate or not Path(candidate).exists():
                continue
            common = await self.git.aworktree_base_path(candidate)
            if not common:
                continue
            if Path(common) != self.retained_store(repository.id):
                url = await self.git.aget_remote_url(common)
                if not url or not repository_urls_match(url, repository.url, base=common):
                    # Do not inspect other repositories in a multi-repo workspace.
                    continue
            paths.add(str(Path(common).resolve()))
        return sorted(paths)
