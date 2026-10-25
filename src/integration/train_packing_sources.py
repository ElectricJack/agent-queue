"""Read bounded packing evidence and probe the production merge mechanism."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from src.database.tables import (
    integration_batch_members, integration_batches, integration_check_evidence,
    task_branch_origins, task_dependencies, tasks,
)
from src.git.manager import GitError
from src.integration.batches import SelectionIdentityChanged
from src.integration.regeneration import (
    GeneratedMergeConflict,
    RegenerationFailure,
    generated_paths,
    merge_generated_tree,
)
from src.integration.signatures import SignatureIdentity, SignatureRules, freeze_signature_documents
from src.integration.source_ancestry import effective_source_base
from src.integration.train_packing import PackingInput, dependency_units, pack
from src.integration.selection_metrics import selection_count


async def cumulative_probe(gitops, repo, target_sha, members):
    """Same bases/driver as assembly; generated rebuild and full CI remain owed.

    Synthetic commits exist only as unreachable local objects. No source code,
    generator, checkout ref or remote write runs during ranking.
    """
    current = target_sha
    try:
        for member in members:
            base = await effective_source_base(
                gitops.git, str(repo.store), member.base_sha, current, member.source_sha,
            )
            if await gitops._migration_conflicts(repo, current, member, base):
                return "migration_conflict"
            tree = await merge_generated_tree(
                gitops.git, Path(repo.store),
                ["--no-replace-objects", "merge-tree", "--write-tree",
                 f"--merge-base={base}", current, member.source_sha],
                command=repo.regenerate, timeout_seconds=repo.regenerate_timeout_seconds,
                defer_regeneration=True,
            )
            current = await gitops.run(
                repo, *gitops.git.resolve_commit_identity().config_args(),
                "commit-tree", tree, "-p", current, "-p", member.source_sha,
                "-m", "Temporary cumulative train selection probe",
            )
    except GeneratedMergeConflict:
        return "cumulative_conflict"
    except (GitError, OSError, ValueError, RegenerationFailure):
        return "probe_unavailable"
    return None


def path_rules(paths, generated):
    """Explicit v1 source classifier; unfamiliar/global/generated paths stay global.

    No generated output is subtracted without a provenance producer. Graph
    signatures remain observational. The shared bounded-window registry makes
    all signatures comparable, and its digest binds classifier inputs/version.
    """
    suffixes = {"src/": (".py",), "tests/": (".py",),
                "dashboard/src/": (".ts", ".tsx", ".css"), "docs/": (".md",)}
    known = tuple(sorted(path for path in paths if path not in generated
                         and any(path.startswith(root) and path.endswith(ext)
                                 for root, ext in suffixes.items())
                         and Path(path).name not in {"AGENTS.md", "CLAUDE.md", "conftest.py"}))
    return SignatureRules(known, "train-path-classifier-v1",
                          universal_paths=tuple(sorted(set(paths) - set(known))))


async def history_on(conn, target, policy):
    """Bounded recent verdicts and landed signatures; absent evidence is unknown."""
    landed = (await conn.execute(select(integration_batches.c.artifact_snapshot).where(
        integration_batches.c.project_id == target.project_id,
        integration_batches.c.repository_id == target.repository_id,
        integration_batches.c.target_ref == target.target_ref,
        integration_batches.c.lifecycle == "promoted",
    ).order_by(integration_batches.c.updated_at.desc(), integration_batches.c.id)
      .limit(policy.hotness_window_batches))).scalars().all()
    hot, known = set(), bool(landed)
    for document in landed:
        sources = (document or {}).get("train_signatures", {}).get("sources")
        if not sources or any(s.get("signature_version") != 1 or s.get("universal", True)
                              for s in sources):
            known = False
        else:
            hot.update(p for source in sources for p in source.get("paths", ()))
    # These observations affect scores only. Full exact-head admission remains
    # independent. Each retained producer/check/head counts at most once.
    checks = select(integration_check_evidence).where(
        integration_check_evidence.c.repository_id == target.repository_id,
        integration_check_evidence.c.conclusion.in_(("success", "failure", "missing")),
    ).order_by(integration_check_evidence.c.observed_at.desc(),
               integration_check_evidence.c.id).limit(1000).subquery()
    rows = (await conn.execute(select(
        integration_batches.c.artifact_snapshot, checks,
        integration_batch_members.c.task_id.label("source_task_id"),
        tasks.c.profile_id.label("source_profile"),
    ).select_from(checks.join(integration_batch_members,
                            integration_batch_members.c.source_sha == checks.c.sha)
                  .join(integration_batches,
                        integration_batches.c.id == integration_batch_members.c.batch_id)
                  .outerjoin(tasks, tasks.c.id == integration_batch_members.c.task_id)).where(
        integration_batches.c.project_id == target.project_id,
        integration_batches.c.repository_id == target.repository_id,
    ).order_by(checks.c.observed_at.desc(), checks.c.id,
               integration_batch_members.c.task_id).limit(1000))).mappings().all()
    rates, seen = {}, set()
    for row in rows:
        profiles = (row["artifact_snapshot"] or {}).get("train_packing", {}).get("profiles", {})
        profile = profiles.get(row["source_task_id"], row["source_profile"])
        key = (row["sha"], row["producer_id"], row["check_name"], profile)
        if not all(key) or key in seen:
            continue
        seen.add(key)
        passed, count = rates.get(profile, (0, 0))
        rates[profile] = (passed + (row["conclusion"] == "success"), count + 1)
    return hot, known, rates


async def select_packed(owner, target, snapshot, service, view, pending, *, deadline):
    """Rank only this admitted window; return evidence plus an immediate recheck."""
    members, requests, dependencies = pending
    policy, now = view.policy.packing, owner.clock()
    ids = [m.task_id for m in members]
    async with owner.db._engine.connect() as conn:
        rows = {row.id: row for row in (await conn.execute(select(
            tasks.c.id, tasks.c.pr_url, tasks.c.profile_id,
        ).where(tasks.c.id.in_(ids)))).all()}
        hot, hot_known, rates = await history_on(conn, target, policy)
        edge_query = select(task_dependencies.c.task_id,
                            task_dependencies.c.depends_on_task_id).where(
            task_dependencies.c.task_id.in_(ids), task_dependencies.c.dep_type == "blocks")
        original_edges = set((await conn.execute(edge_query)).all())
        origin_query = select(task_branch_origins.c.task_id, task_branch_origins.c.base_sha,
                              task_branch_origins.c.creation_generation,
                              task_branch_origins.c.retired_at).where(
            task_branch_origins.c.task_id.in_(ids),
            task_branch_origins.c.repository_id == target.repository_id,
        ).order_by(task_branch_origins.c.retired_at.is_(None),
                   task_branch_origins.c.creation_generation, task_branch_origins.c.id)
        original_origins = list((await conn.execute(origin_query)).all())
        current_bases = {row.task_id: row.base_sha for row in original_origins}
        if any(m.task_id in current_bases and current_bases[m.task_id] != m.base_sha
               for m in members):
            raise SelectionIdentityChanged("source origin changed during selection")
    observed = snapshot.observation
    repo = await service.gitops.repository(target)
    await service.gitops.validate_repository(repo)
    paths, generated, bases = set(), set(), {}
    observed_members = []
    positions = {m.task_id: index for index, m in enumerate(members)}
    units = dependency_units([PackingInput(m, None, 0) for m in members], dependencies)
    units.sort(key=lambda item: min(positions[c.member.task_id] for c in item[0]))
    def ready_at(tid):
        stamp = requests[tid].completed_at
        return now if stamp is None else stamp

    def ready_order(item):
        return min((ready_at(c.member.task_id), c.member.task_id)
                   for c in item[0])

    overdue = [item for item in units
               if now - ready_order(item)[0] > policy.oldest_slot_after_seconds]
    if overdue:
        oldest = min(overdue, key=ready_order)
        units.remove(oldest)
        units.insert(0, oldest)
    for index, (unit, _) in enumerate(units):
        if index and owner._selection_clock() >= deadline:
            selection_count("packing_observation_deferred", len(members) - len(observed_members))
            break
        for candidate in unit:
            member = candidate.member
            base = await service.gitops.run(repo, "merge-base", "--all", member.source_sha,
                                           snapshot.target_oid)
            if "\n" in base:
                raise ValueError("ambiguous source merge base")
            bases[member.task_id] = base
            output = await service.gitops.run(repo, "diff", "--no-ext-diff", "--no-textconv",
                                             "--no-renames", "--name-only", "-z", base,
                                             member.source_sha, "--")
            changed = set(output.split("\0")) - {""}
            paths.update(changed)
            generated.update(await generated_paths(observed.git, Path(observed.store),
                                                   member.source_sha, changed))
            observed_members.append(member)
    rules = path_rules(paths, generated)
    identities = {}
    for member in observed_members:
        tid = member.task_id
        request = requests[tid]
        identities[tid] = SignatureIdentity(
            target.project_id, target.repository_id, tid, rows[tid].pr_url,
            "refs/heads/" + (request.branch_name or "").removeprefix("refs/heads/"),
            target.target_ref, bases[tid], member.source_sha, snapshot.target_oid,
            view.policy_revision, rules.digest,
        )

    async def current():
        from src.integration.delivery_truth import load_delivery_requests
        from src.integration.train_policy import load_train_policy

        latest = await load_delivery_requests(owner.db, ids, repository_id=target.repository_id,
                                             target_ref=target.target_ref, reduced=True)
        if latest != requests or await load_train_policy(owner.db, target.project_id) != view:
            return False
        async with owner.db._engine.connect() as conn:
            urls = dict((await conn.execute(select(tasks.c.id, tasks.c.pr_url).where(
                tasks.c.id.in_(ids)))).all())
            if set((await conn.execute(edge_query)).all()) != original_edges:
                return False
            if list((await conn.execute(origin_query)).all()) != original_origins:
                return False
        if any(urls.get(tid) != rows[tid].pr_url for tid in ids):
            return False
        if not await observed.is_fresh():
            return False
        for identity in identities.values():
            head = await service.gitops.remote(repo, identity.source_ref)
            if head != identity.source_head:
                return False
        return True

    # The whole bounded set is checked before observation and once immediately
    # before freeze. source_signature also guards the local exact snapshot.
    fresh = await current()
    if not fresh:
        for identity in identities.values():
            snapshot.truth.signatures.invalidate(identity)
        selection_count("packing_stale")
        raise SelectionIdentityChanged("packing identity changed before observation")

    async def recheck(identity):
        return identity if fresh else None

    inputs = []
    for member in observed_members:
        tid, identity = member.task_id, identities[member.task_id]
        signature = await snapshot.source_signature(
            identity, rules, recheck=lambda identity=identity: recheck(identity),
        )
        passed, checks = rates.get(rows[tid].profile_id, (0, 0))
        # Exact admitted root successes are retained only for unrepaired green
        # heads by RootPullRequestGate. Unknown/custom gates earn no green bonus.
        green = owner.packing_green(target, member, snapshot.target_oid)
        clean = await owner._local_pr_conflict(snapshot, member) is None
        inputs.append(PackingInput(
            member, signature, ready_at(tid), green, clean,
            len(set(signature.paths) & hot) / max(1, len(signature.paths)) if hot_known else 1,
            passed, checks, rows[tid].profile_id,
        ))
    chosen, outcomes = await pack(
        inputs, dependencies, policy, now=now, deadline=deadline, clock=owner._selection_clock,
        probe=lambda proposed: cumulative_probe(service.gitops, repo, snapshot.target_oid, proposed),
    )
    chosen_ids = {m.task_id for m in chosen}
    selected = [c for c in inputs if c.member.task_id in chosen_ids]
    signatures = [next(c.signature for c in selected if c.member.task_id == m.task_id)
                  for m in chosen]

    async def revalidate():
        valid = await current()
        if not valid:
            for identity in identities.values():
                snapshot.truth.signatures.invalidate(identity)
            selection_count("packing_stale")
        return valid

    document = {}
    if chosen:
        document, _ = freeze_signature_documents(
            {}, {}, signatures, current_identities=[s.identity for s in signatures],
            selection_reasons=[row["outcome"] for row in outcomes],
        )
        document["train_packing"] = {
            "policy_revision": view.policy_revision, "outcomes": outcomes,
            "profiles": {c.member.task_id: c.profile for c in selected},
        }
    return (chosen, {tid: requests[tid] for tid in chosen_ids},
            {tid: dependencies[tid] for tid in chosen_ids}), document, revalidate
