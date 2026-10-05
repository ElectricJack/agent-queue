"""Recursive epic readiness using ordinary tasks, Git, checks and tree reviews.

This is the reduced train's parent component, not another runtime. CommandHandler
adapters supply policy/check producers and the shared batch/ordinary completion
actions. No episode, verification generation, receipt or delegate audit is read.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from sqlalchemy import literal, select

from src.database.tables import (
    archived_tasks, gates, projects, repos, task_completion_records, task_dependencies,
    task_gates, task_labels, task_metadata, tasks,
)
from src.git.manager import GitError
from src.integration.delivery_truth import DeliveryRequest, DeliveryState
from src.integration.git_truth import GitDeliveryEvidence, GitTruthSnapshot
from src.integration.reviews import ReviewRequirements, ReviewSubject, TreeReviews, TreeVerdict


@dataclass(frozen=True)
class EpicPolicy:
    check_names: tuple[str, ...]
    check_trust: str
    reviews: ReviewRequirements = ReviewRequirements()


@dataclass(frozen=True)
class HeadChecks:
    repository_id: str
    head_sha: str
    names: tuple[str, ...]
    trust: str
    state: Literal["green", "red", "pending", "unknown"]


@dataclass(frozen=True)
class EpicNode:
    request: DeliveryRequest
    parent_id: str | None
    branch_ref: str | None
    policy: EpicPolicy
    required: bool = True
    holds: tuple[str, ...] = ()
    source_base: str | None = None
    container: bool = False

    @property
    def task_id(self) -> str:
        return self.request.task_id


@dataclass(frozen=True)
class EpicGraph:
    project_id: str
    repository_id: str
    repository_url: str
    default_ref: str
    epic_id: str
    nodes: tuple[EpicNode, ...]
    dependencies: tuple[tuple[str, str, str], ...] = ()
    project_holds: tuple[str, ...] = ()

    def node(self, task_id: str) -> EpicNode:
        return next(node for node in self.nodes if node.task_id == task_id)

    def children(self, task_id: str) -> tuple[EpicNode, ...]:
        return tuple(node for node in self.nodes if node.parent_id == task_id and node.required)

    def target(self, task_id: str) -> str:
        node = self.node(task_id)
        if node.parent_id:
            try:
                parent = self.node(node.parent_id)
            except StopIteration:
                raise ValueError("delivery requires the parent target in the graph") from None
            if not parent.branch_ref:
                raise ValueError("parent target has no branch")
            return parent.branch_ref
        return self.default_ref


@dataclass(frozen=True)
class EpicReadiness:
    graph: EpicGraph
    task_id: str
    state: Literal["ready", "waiting", "unknown", "held", "rejected"]
    head_sha: str | None = None
    tree_sha: str | None = None
    blockers: tuple[tuple[str, str], ...] = ()
    sources: tuple[GitDeliveryEvidence, ...] = ()
    nested: tuple[EpicReadiness, ...] = ()
    checks: HeadChecks | None = None
    review: TreeVerdict | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    @property
    def missing_sources(self) -> tuple[GitDeliveryEvidence, ...]:
        return tuple(source for source in self.sources if source.state == DeliveryState.PENDING)


class EpicGraphReader:
    """Read ordinary current task/completion/hold inputs in one caller transaction.

    ``policy_on`` resolves current project policy through its existing owner.
    ``required_on`` optionally excludes children that the task/gate owner has
    explicitly waived. A terminal failure is not an implicit waiver.
    """

    def __init__(self, *, policy_on: Callable, required_on: Callable | None = None,
                 source_base_on: Callable | None = None):
        self.policy_on, self.required_on = policy_on, required_on
        self.source_base_on = source_base_on

    async def read_on(self, conn, epic_id: str) -> EpicGraph:
        root = (await conn.execute(select(tasks).where(tasks.c.id == epic_id))).mappings().one()
        project = (await conn.execute(select(projects).where(
            projects.c.id == root["project_id"],
        ))).mappings().one()
        repository = (await conn.execute(select(repos).where(
            repos.c.id == project["integration_repository_id"],
            repos.c.project_id == project["id"],
        ))).mappings().one()
        fields = ("id", "project_id", "repo_id", "parent_task_id", "branch_name", "status",
                  "updated_at", "legacy_completion_id", "task_type")
        ordinary = select(*(tasks.c[name] for name in fields), tasks.c.claim_epoch,
                          literal(False).label("archived")).union_all(select(
            *(archived_tasks.c[name] for name in fields), literal(0).label("claim_epoch"),
            literal(True).label("archived"),
        )).cte("epic_ordinary_tasks")
        descendants = select(ordinary).where(ordinary.c.id == epic_id).cte(
            "epic_descendants", recursive=True,
        )
        descendants = descendants.union_all(select(ordinary).join(
            descendants, ordinary.c.parent_task_id == descendants.c.id,
        ).where(ordinary.c.project_id == root["project_id"]))
        rows = list((await conn.execute(select(descendants))).mappings())
        # The immediate parent supplies the delivery target but is not another
        # subtree to complete in this visit.
        if root["parent_task_id"]:
            parent = (await conn.execute(select(ordinary).where(
                ordinary.c.id == root["parent_task_id"],
            ))).mappings().one()
            rows.append(parent)
        ids = [row["id"] for row in rows]
        completions = (await conn.execute(select(task_completion_records).where(
            task_completion_records.c.task_id.in_(ids),
        ).order_by(task_completion_records.c.completed_at.desc(),
                   task_completion_records.c.id.desc()))).mappings()
        latest = {}
        for completion in completions:
            latest.setdefault(completion["task_id"], completion)
        holds: dict[str, list[str]] = {task_id: [] for task_id in ids}
        containers = {row["parent_task_id"] for row in rows if row["parent_task_id"]}
        for task_id, label in await conn.execute(select(task_labels.c.task_id,
                                                      task_labels.c.label).where(
            task_labels.c.task_id.in_(ids), task_labels.c.label.like("hold:%"),
        )):
            holds[task_id].append(label)
        for task_id, key, value in await conn.execute(select(
            task_metadata.c.task_id, task_metadata.c.key, task_metadata.c.value,
        ).where(task_metadata.c.task_id.in_(ids),
                task_metadata.c.key.in_(("manual_pause", "container")))):
            if key == "manual_pause":
                holds[task_id].append("manual_pause")
            elif json.loads(value) is True:
                containers.add(task_id)
        for task_id, gate_id in await conn.execute(select(task_gates.c.task_id, gates.c.id)
                                                  .join(gates, gates.c.id == task_gates.c.gate_id)
                                                  .where(task_gates.c.task_id.in_(ids),
                                                         gates.c.status == "open")):
            holds[task_id].append("gate:" + gate_id)
        nodes = []
        default_ref = "refs/heads/" + repository["default_branch"].removeprefix("refs/heads/")
        for row in rows:
            completion = latest.get(row["id"])
            request = DeliveryRequest(
                project_id=row["project_id"], repository_id=row["repo_id"] or repository["id"],
                target_ref=default_ref, task_id=row["id"], task_version=row["updated_at"],
                legacy_generation=row["legacy_completion_id"], branch_name=row["branch_name"],
                completion_id=completion["id"] if completion else None,
                completed_at=completion["completed_at"] if completion else None,
                task_status=row["status"], claim_epoch=row["claim_epoch"], archived=row["archived"],
            )
            required = await self.required_on(conn, row) if self.required_on else True
            base = await self.source_base_on(conn, row) if self.source_base_on else None
            nodes.append(EpicNode(
                request, row["parent_task_id"], "refs/heads/" + row["branch_name"].removeprefix(
                    "refs/heads/") if row["branch_name"] else None,
                await self.policy_on(conn, row, project), required, tuple(sorted(holds[row["id"]])),
                base, row["id"] in containers,
            ))
        dependencies = tuple(await conn.execute(select(
            task_dependencies.c.task_id, task_dependencies.c.depends_on_task_id,
            task_dependencies.c.dep_type,
        ).where(task_dependencies.c.task_id.in_(ids)).order_by(
            task_dependencies.c.task_id, task_dependencies.c.depends_on_task_id,
            task_dependencies.c.dep_type,
        )))
        return EpicGraph(project["id"], repository["id"], repository["url"], default_ref,
                         epic_id, tuple(sorted(nodes, key=lambda node: node.task_id)), dependencies,
                         () if project["status"] == "ACTIVE" else ("project_paused",))


def exact_head_checks(
    exact_for: Callable[[str, EpicPolicy], Any],
) -> Callable[[str, str, EpicPolicy], Awaitable[HeadChecks]]:
    """Adapt the shared exact-commit cache (``src.integration.checks``) to epics.

    *exact_for* returns the ``ExactChecks`` reader for a repository and policy.
    The cached verdict is read without contacting a provider, so the reported
    names and trust are those the cache holds; a mismatch with the policy is
    reported by the evaluator as ``checks_scope_changed``.
    """
    from src.integration.subjects import HeadIdentity

    async def read(repository_id: str, head_sha: str, policy: EpicPolicy) -> HeadChecks:
        exact = exact_for(repository_id, policy)
        result = await exact.read(HeadIdentity(
            repository_id=repository_id, ref="refs/heads/epic-readiness",
            sha=head_sha, generation=0,
        ))
        return HeadChecks(repository_id, head_sha, tuple(result.required.names),
                          result.required.producer_id, result.state.value)

    return read


class EpicReadinessEvaluator:
    """One fetched snapshot; nested targets use the same checks and batch path."""

    def __init__(self, db, graph_reader: EpicGraphReader, reviews: TreeReviews, *,
                 checks: Callable[[str, str, EpicPolicy], Awaitable[HeadChecks]]):
        self.db, self.graph_reader, self.reviews, self.checks = db, graph_reader, reviews, checks

    async def evaluate(self, graph: EpicGraph, snapshot: GitTruthSnapshot,
                       *, task_id: str | None = None) -> EpicReadiness:
        task_id = task_id or graph.epic_id
        node = graph.node(task_id)
        blockers, sources, nested = [], [], []
        unknown, held, rejected = False, False, False
        if node.holds or graph.project_holds:
            return EpicReadiness(graph, task_id, "held", blockers=tuple(
                (task_id, reason) for reason in (*graph.project_holds, *node.holds)))
        observation = snapshot.observation
        if (graph.project_id, graph.repository_id, graph.repository_url) != (
            observation.project_id, observation.repository_id, observation.repository_url,
        ) or not node.branch_ref:
            return EpicReadiness(graph, task_id, "unknown", blockers=((task_id, "target_scope"),))
        target = snapshot.for_target(node.branch_ref)
        if target.observation.error or not target.target_oid:
            return EpicReadiness(graph, task_id, "unknown", blockers=(
                (task_id, target.observation.error or "missing_target"),))
        for child in graph.children(task_id):
            if child.holds:
                held = True
                blockers.extend((child.task_id, reason) for reason in child.holds)
            if child.container:
                readiness = await self.evaluate(graph, snapshot, task_id=child.task_id)
                nested.append(readiness)
                if not readiness.ready:
                    blockers.append((child.task_id, "nested_" + readiness.state))
                    unknown |= readiness.state == "unknown"
                    held |= readiness.state == "held"
                    rejected |= readiness.state == "rejected"
            if child.request.task_status != "COMPLETED":
                blockers.append((child.task_id, "work_" + child.request.task_status.lower()))
                continue
            proof = await target.is_delivered(replace(child.request, target_ref=node.branch_ref),
                                              source_base=child.source_base)
            sources.append(proof)
            if proof.state not in {DeliveryState.CONTAINED, DeliveryState.NO_ARTIFACT}:
                blockers.append((child.task_id, proof.reason))
                unknown |= proof.state == DeliveryState.UNKNOWN
            if nested and nested[-1].task_id == child.task_id and nested[-1].ready:
                # A closed nested epic must locate the aggregate just examined,
                # not an old close that predates a newly collected child.
                if proof.source_oid != nested[-1].head_sha:
                    blockers.append((child.task_id, "nested_source_changed"))
        head, tree, checks, verdict = target.target_oid, None, None, None
        try:
            tree = await snapshot.truth.git.atree_sha(observation.store, head)
            checks = await self.checks(graph.repository_id, head, node.policy)
            if (checks.repository_id, checks.head_sha, checks.names, checks.trust) != (
                graph.repository_id, head, node.policy.check_names, node.policy.check_trust,
            ):
                unknown = True
                blockers.append((task_id, "checks_scope_changed"))
            elif checks.state != "green":
                unknown |= checks.state == "unknown"
                blockers.append((task_id, "checks_" + checks.state))
            verdict = await self.reviews.verdict(ReviewSubject(
                graph.project_id, graph.repository_id, task_id, tree,
            ), node.policy.reviews)
            if not verdict.satisfied:
                rejected |= verdict.state == "rejected"
                blockers.append((task_id, "review_" + verdict.state))
        except (GitError, OSError, ValueError):
            unknown = True
            blockers.append((task_id, "head_evidence_unavailable"))
        state = "held" if held else "rejected" if rejected else "unknown" if unknown else (
            "waiting" if blockers else "ready")
        return EpicReadiness(graph, task_id, state, head, tree, tuple(blockers), tuple(sources),
                             tuple(nested), checks, verdict)

    async def current_on(self, conn, readiness: EpicReadiness,
                         snapshot: GitTruthSnapshot) -> bool:
        """Recheck ordinary graph/source/policy and remote OIDs at the action boundary.

        Call inside the completion command's project lock. Repeat checks and
        verdict lookup so a rerun or rejection observed after evaluation binds.
        The task owner still enforces open-child and transition authorization.
        """
        current = await self.graph_reader.read_on(conn, readiness.graph.epic_id)
        if current != readiness.graph:
            return False
        node = current.node(readiness.task_id)
        target = snapshot.for_target(node.branch_ref)
        if readiness.head_sha != target.target_oid or not await target.observation.is_fresh():
            return False
        for child in readiness.nested:
            if not await self.current_on(conn, child, snapshot):
                return False
        refreshed = await self.evaluate(current, snapshot, task_id=readiness.task_id)
        return refreshed.ready and refreshed.head_sha == readiness.head_sha

    async def complete_on(self, conn, readiness: EpicReadiness, snapshot: GitTruthSnapshot, *,
                          complete: Callable[[object, str, str], Awaitable[dict]]) -> dict:
        """Delegate ordinary completion only after revalidation, with the exact head."""
        await self.db.lock_hierarchy_project(conn, readiness.graph.project_id)
        if not readiness.ready or not await self.current_on(conn, readiness, snapshot):
            return {"success": False, "outcome": "changed", "task_id": readiness.task_id}
        return await complete(conn, readiness.task_id, readiness.head_sha)

    async def collect(self, readiness: EpicReadiness, snapshot: GitTruthSnapshot, *,
                      submit_batch: Callable[..., Awaitable[dict]]) -> dict:
        """Freeze eligible missing sources for the shared child/root batch builder.

        The batch command repeats source/graph identity checks when freezing
        these inputs. Publication is always its exact-candidate checked path;
        this component never pushes an epic or an unchecked merge to main.
        """
        if readiness.state in {"unknown", "held", "rejected"}:
            return {"success": False, "outcome": readiness.state}
        node = readiness.graph.node(readiness.task_id)
        target = snapshot.for_target(node.branch_ref)
        async with self.db._engine.connect() as conn:
            if await self.graph_reader.read_on(conn, readiness.graph.epic_id) != readiness.graph:
                return {"success": False, "outcome": "changed"}
        if not await target.observation.is_fresh():
            return {"success": False, "outcome": "changed"}
        refreshed = await self.evaluate(readiness.graph, snapshot, task_id=readiness.task_id)
        if refreshed.state in {"unknown", "held", "rejected"}:
            return {"success": False, "outcome": refreshed.state}
        readiness = refreshed
        missing = {source.request.task_id: source for source in readiness.missing_sources}
        for child in readiness.nested:
            source = missing.get(child.task_id)
            if source and (not child.ready or source.source_oid != child.head_sha):
                missing.pop(child.task_id)
        ordered = _ordered_members(readiness.graph, missing)
        if not ordered:
            return {"success": True, "outcome": "nothing_to_collect"}
        return await submit_batch(
            project_id=readiness.graph.project_id, repository_id=readiness.graph.repository_id,
            target_ref=node.branch_ref, expected_target=readiness.head_sha,
            members=tuple({"task_id": task_id, "source_sha": missing[task_id].source_oid,
                           "source_base": missing[task_id].source_base} for task_id in ordered),
        )

    async def deliver(self, readiness: EpicReadiness, snapshot: GitTruthSnapshot, *,
                      submit_batch: Callable[..., Awaitable[dict]]) -> dict:
        """Deliver a closed epic through the same exact-candidate batch path.

        Even when the epic head is green, a moved parent/default target needs
        its own built candidate and checks. No green epic authorizes a push of
        an unchecked merge result. The batch publisher owns that final gate.
        """
        node = readiness.graph.node(readiness.task_id)
        if not readiness.ready or node.request.task_status != "COMPLETED":
            return {"success": False, "outcome": readiness.state if not readiness.ready
                    else "work_open"}
        async with self.db._engine.connect() as conn:
            if not await self.current_on(conn, readiness, snapshot):
                return {"success": False, "outcome": "changed"}
        target_ref = readiness.graph.target(readiness.task_id)
        if node.parent_id and readiness.graph.node(node.parent_id).holds:
            return {"success": False, "outcome": "held"}
        target = snapshot.for_target(target_ref)
        proof = await target.is_delivered(replace(node.request, target_ref=target_ref),
                                          source_base=node.source_base)
        if proof.state == DeliveryState.UNKNOWN:
            return {"success": False, "outcome": "unknown", "reason": proof.reason}
        if not await target.observation.is_fresh():
            return {"success": False, "outcome": "changed"}
        if proof.state in {DeliveryState.CONTAINED, DeliveryState.NO_ARTIFACT}:
            return {"success": True, "outcome": "delivered"}
        if proof.source_oid != readiness.head_sha:
            return {"success": False, "outcome": "changed"}
        return await submit_batch(
            project_id=readiness.graph.project_id, repository_id=readiness.graph.repository_id,
            target_ref=target_ref, expected_target=target.target_oid,
            members=({"task_id": node.task_id, "source_sha": proof.source_oid,
                      "source_base": proof.source_base},),
        )


def _ordered_members(graph: EpicGraph, members: dict) -> tuple[str, ...]:
    """Stable dependency order for the same frozen inputs used by every batch."""
    remaining, result = set(members), []
    prerequisites = {task_id: {upstream for task, upstream, kind in graph.dependencies
                              if task == task_id and kind in {"blocks", "waits-for",
                                                              "conditional-blocks"}}
                     for task_id in remaining}
    while remaining:
        ready = sorted(task_id for task_id in remaining if not prerequisites[task_id] & remaining)
        if not ready:
            raise ValueError("cyclic batch dependencies")
        result.extend(ready)
        remaining.difference_update(ready)
    return tuple(result)
