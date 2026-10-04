"""Receipt-driven parent collection and guarded completion."""

from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.queries.hierarchy_queries import HierarchyError
from src.database.queries.task_queries import StaleClaim
from src.database.tables import (
    archived_tasks,
    gates,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_check_evidence,
    integration_episode_receipt_acceptances,
    integration_operation_artifact_pins,
    integration_outbox,
    integration_parent_episodes,
    integration_parent_operation_completions,
    integration_parent_verification_evidence,
    integration_parent_verifications,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    playbook_artifacts,
    projects,
    repos,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    task_gates,
    task_integration_checkpoints,
    task_metadata,
    tasks,
    workspaces,
)
from src.integration.drain_owners import terminal_reservation_clause
from src.integration.hierarchy import HierarchyIntegration
from src.integration.models import (
    ArtifactSnapshot,
    BranchKey,
    Fence,
    HierarchicalIntegrationPolicy,
    IntegrationBoundaryPolicy,
    PlaybookRoute,
    RepairPolicy,
    RequiredCheckSet,
)
from src.integration.ownership import BranchOwnership
from src.integration.records import (
    AWAITING_TRUSTED_VERIFICATION,
    ParentEpisodeRecords,
)
from src.integration.status import IntegrationStatusService
from src.models import (
    AgentProfile,
    Project,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskCompletion,
    TaskStatus,
)
from src.profiles.capabilities import DENY_ALL, CapabilityPolicy


async def _parent_repair_case(db, tmp_path, *, children=1, first_receipt_child=0):
    from src.git.manager import GitManager
    from src.integration.parent_repair_heads import ParentHeadRecovery
    from src.integration.promotion import PromotionService

    origin, work = tmp_path / "origin.git", tmp_path / "work"

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=work if work.exists() else tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--bare", "--initial-branch=main", str(origin))
    git("clone", str(origin), str(work))
    git("config", "user.name", "Repair Test")
    git("config", "user.email", "repair@example.test")
    git("commit", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("push", "origin", "main")
    git("switch", "-c", "aq/parent")
    git("commit", "--allow-empty", "-m", "collected child")
    collected = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "authorized repair")
    head = git("rev-parse", "HEAD")
    git("push", "origin", "aq/parent")
    hierarchy, checkpointed, children = await _parent_tree(db, children=children, base_sha=base)
    await _code_receipt(
        db, children[first_receipt_child], base, collected,
        created_at=float(first_receipt_child + 1),
    )
    await db.create_task(
        Task(
            id="repair",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Repair",
            description="Authorized repair",
            status=TaskStatus.COMPLETED,
            created_by_kind="integration_repair",
            created_by_id=checkpointed["operation_id"],
        )
    )
    await db.save_task_completion(
        TaskCompletion(
            id="repair-completion",
            task_id="repair",
            outcome="pass",
            branch="aq/parent",
            commits=[head],
            completed_at=20.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "parent").values(status="PAUSED"))
        await conn.execute(
            update(task_integration_checkpoints)
            .where(
                task_integration_checkpoints.c.task_id == "parent",
            )
            .values(checkpoint_sha=collected, state="verifying")
        )
        await conn.execute(
            update(integration_branch_owners).values(
                owner_id=checkpointed["operation_id"],
                owner_role="collector",
                handoff_state="reserved",
                fence_token=3,
            )
        )
        from src.integration.outbox import enqueue_integration_event

        await enqueue_integration_event(
            conn,
            event_id="repair-closed",
            dedup_key="repair-closed",
            project_id="p",
            event_type="integration.repair_delegate_closed",
            available_at=20.0,
            payload={
                "operation_id": checkpointed["operation_id"],
                "stage": 0,
                "task_id": "repair",
                "session_id": "former-repair-session",
                "instance_token": "former-instance",
                "workspace_id": "former-workspace",
                "fence_token": 2,
            },
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=0,
                policy=_boundary().repair.model_dump(mode="json"),
                intelligence_class="medium",
                starting_sha=collected,
                trigger_id="failed-check",
                writer_kind="repair_delegate",
                repair_task_id="repair",
                current_subject={"kind": "parent", "generation": 1, "head_sha": head},
                started_at=2.0,
                deadline_at=10.0,
                deadline_event_id="old-deadline",
                attempts=1,
                state="awaiting_completion",
                dossier={"repair_commits": [head], "branch_sha": head},
            )
        )
    repo = RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin))
    recovery = ParentHeadRecovery(
        PromotionService(
            db,
            data_dir=tmp_path / "retained",
            git_manager=GitManager(),
            repository_resolver=lambda _: repo,
        )
    )
    request = SimpleNamespace(
        operation_id=checkpointed["operation_id"],
        head_sha=head,
        dry_run=True,
        expected_episode_id=checkpointed["episode_id"],
        expected_generation=1,
        expected_stage=0,
        expected_fence_token=3,
        reason="Reconcile authorized repair",
    )
    return recovery, hierarchy, request, git, collected, head


async def _finished_collection_case(
    db,
    tmp_path,
    *,
    wrong_tree=False,
    push_damage=None,
    interior_gap=False,
):
    """Three receipts, two audited repair gaps, and a receipt-covered final stage."""
    from src.database.queries.result_queries import close_identity
    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
    from src.integration.outbox import enqueue_integration_event

    recovery, hierarchy, request, git, collected, first_gap = await _parent_repair_case(
        db, tmp_path, children=4 if interior_gap else 3
    )
    git("commit", "--allow-empty", "-m", "second collected child")
    second_child = git("rev-parse", "HEAD")
    interior_commits = []
    if interior_gap:
        git("commit", "--allow-empty", "-m", "first interior repair")
        interior_commits.append(git("rev-parse", "HEAD"))
    git("commit", "--allow-empty", "-m", "second authorized repair")
    second_gap = git("rev-parse", "HEAD")
    middle_head = second_gap
    if interior_gap:
        git("commit", "--allow-empty", "-m", "middle child resolution")
        middle_head = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "final child resolution")
    final_head = git("rev-parse", "HEAD")
    final_tree = git("rev-parse", "HEAD^{tree}")
    if wrong_tree:
        final_tree = "d" * 40
    git("push", "origin", "aq/parent")
    await _code_receipt(db, "parent.2", first_gap, second_child)
    for task_id, head, completed_at in (
        ("repair-middle", middle_head, 22.0),
        ("repair-final", final_head, 26.0),
    ):
        await db.create_task(
            Task(
                id=task_id,
                project_id="p",
                repo_id="repo",
                branch_name="aq/parent",
                title=task_id,
                description="Authorized collection repair",
                status=TaskStatus.COMPLETED,
                created_by_kind="integration_repair",
                created_by_id=request.operation_id,
            )
        )
        await db.save_task_completion(
            TaskCompletion(
                id=f"completion-{task_id}",
                task_id=task_id,
                outcome="pass",
                branch="aq/parent",
                commits=[] if task_id == "repair-final" else [head],
                completed_at=completed_at,
            )
        )
    await db.set_task_meta(
        "repair-final",
        ACCEPTED_CLOSE_KEY,
        close_identity("completion-repair-final", session_id="final-session", claim_epoch=0),
    )
    remote = {
        "kind": "exact_resolution_tip",
        "remote_sha": final_head,
        "resolved_tree_sha": final_tree,
        "repair_commit_shas": [final_head],
    }
    evidence = {
        "kind": "conflict_resolution",
        "original_source_base": "a" * 40,
        "original_source_head": "b" * 40,
        "original_source_tree": "c" * 40,
        "original_expected_target": middle_head,
        "resolved_head_sha": final_head,
        "resolved_tree_sha": final_tree,
        "repair_commit_shas": [final_head],
        "authoring": {
            "operation_id": request.operation_id,
            "stage_ordinal": 2,
            "repair_task_id": "repair-final",
            "repair_session_id": "final-session",
            "repair_session_instance_token": "final-instance",
            "repair_workspace_id": "final-workspace",
            "fence": {
                "repository_id": "repo",
                "branch": "aq/parent",
                "owner_id": "repair-final",
                "token": 6,
            },
        },
        "remote_proof": remote,
    }
    async with db.immediate() as conn:
        original = dict((await conn.execute(select(integration_repair_stages))).mappings().one())
        await conn.execute(
            update(integration_repair_operations).values(
                state="escalated",
                active_stage=2,
            )
        )
        await conn.execute(update(integration_branch_owners).values(fence_token=7))
        await conn.execute(update(integration_repair_stages).values(state="expired"))
        for ordinal, task_id, start, head, state in (
            (
                1,
                "repair-middle",
                middle_head if interior_gap else second_child,
                middle_head,
                "expired",
            ),
            (2, "repair-final", middle_head, final_head, "active"),
        ):
            await conn.execute(
                insert(integration_repair_stages).values(
                    **(
                        original
                        | {
                            "ordinal": ordinal,
                            "deadline_event_id": f"deadline-{ordinal}",
                            "repair_task_id": task_id,
                            "starting_sha": start,
                            "current_subject": {
                                "kind": "parent",
                                "generation": 1,
                                "head_sha": head,
                            },
                            "state": state,
                            "dossier": {
                                "repair_commits": (
                                    [first_gap, *interior_commits, second_gap, middle_head]
                                    if interior_gap and ordinal == 1
                                    else [
                                        first_gap,
                                        *interior_commits,
                                        second_gap,
                                        middle_head,
                                        head,
                                    ]
                                    if interior_gap
                                    else [first_gap, head] if ordinal == 1
                                    else [first_gap, middle_head, head]
                                ),
                                "branch_sha": head,
                            },
                        }
                    )
                )
            )
        await enqueue_integration_event(
            conn,
            event_id="middle-closed",
            dedup_key="middle-closed",
            project_id="p",
            event_type="integration.repair_delegate_closed",
            available_at=22.0,
            payload={
                "project_id": "p",
                "operation_id": request.operation_id,
                "stage": 1,
                "task_id": "repair-middle",
                "session_id": "middle-session",
                "instance_token": "middle-instance",
                "workspace_id": "middle-workspace",
                "fence_token": 4,
            },
        )
        await conn.execute(
            insert(integration_promotion_intents).values(
                id="final-intent",
                domain_key="final-intent",
                operation_key=request.operation_id,
                project_id="p",
                receipt_id="receipt-parent.4" if interior_gap else "receipt-parent.3",
                source_task_id="parent.4" if interior_gap else "parent.3",
                target_task_id="parent",
                source_head="b" * 40,
                source_base="a" * 40,
                repository_id="repo",
                target_branch="aq/parent",
                expected_target=middle_head,
                fence_owner_id=request.operation_id,
                fence_token=5,
                state="committed",
                resolution_head_sha=final_head,
                resolution_tree_sha=final_tree,
                resolution_commit_shas=[final_head],
                resolution_operation_id=request.operation_id,
                resolution_stage_ordinal=2,
                resolution_task_id="repair-final",
                resolution_session_id="final-session",
                resolution_session_instance_token="final-instance",
                resolution_workspace_id="final-workspace",
                resolution_fence_owner_id="repair-final",
                resolution_fence_token=6,
                resolution_push_started_at=24.0,
                remote_evidence=remote,
                committed_at=25.0,
                created_at=23.0,
                updated_at=25.0,
            )
        )
        intent = dict(
            (
                await conn.execute(
                    select(integration_promotion_intents).where(
                        integration_promotion_intents.c.id == "final-intent",
                    )
                )
            )
            .mappings()
            .one()
        )
        # Use the real producer: its fenced observation carries the complete
        # writer identity, rather than only the kind and published SHA.
        pushed = await recovery.promotion._record_resolution_push_on(
            conn,
            "final-intent",
            intent,
            {
                "operation_id": request.operation_id,
                "stage": 2,
                "instance_token": "final-instance",
                "workspace_id": "final-workspace",
            },
            Fence(
                target=BranchKey(repository_id="repo", branch="aq/parent"),
                owner_id="repair-final",
                token=6,
            ),
            SimpleNamespace(task_id="repair-final", session_id="final-session"),
        )
        evidence["push_authority"] = pushed["resolution_push_evidence"]
        if push_damage:
            evidence["push_authority"] = dict(evidence["push_authority"])
            if push_damage == "two_fields":
                evidence["push_authority"] = {
                    key: evidence["push_authority"][key] for key in ("kind", "remote_sha")
                }
            else:
                evidence["push_authority"][push_damage] = "unrelated-writer"
            await conn.execute(
                update(integration_promotion_intents)
                .where(
                    integration_promotion_intents.c.id == "final-intent",
                )
                .values(resolution_push_evidence=evidence["push_authority"])
            )
        if interior_gap:
            middle_tree = git("rev-parse", f"{middle_head}^{{tree}}")
            middle_remote = {
                "kind": "exact_resolution_tip",
                "remote_sha": middle_head,
                "resolved_tree_sha": middle_tree,
                "repair_commit_shas": [middle_head],
            }
            middle_intent = intent | {
                "id": "middle-intent",
                "domain_key": "middle-intent",
                "receipt_id": "receipt-parent.3",
                "source_task_id": "parent.3",
                "expected_target": second_gap,
                "resolution_head_sha": middle_head,
                "resolution_tree_sha": middle_tree,
                "resolution_commit_shas": [middle_head],
                "resolution_stage_ordinal": 1,
                "resolution_task_id": "repair-middle",
                "resolution_session_id": "middle-session",
                "resolution_session_instance_token": "middle-instance",
                "resolution_workspace_id": "middle-workspace",
                "resolution_fence_owner_id": "repair-middle",
                "resolution_fence_token": 4,
                "resolution_push_started_at": 20.0,
                "committed_at": 21.0,
                "remote_evidence": middle_remote,
            }
            middle_intent.pop("resolution_push_evidence", None)
            await conn.execute(insert(integration_promotion_intents).values(**middle_intent))
            middle_push = await recovery.promotion._record_resolution_push_on(
                conn,
                "middle-intent",
                middle_intent,
                {
                    "operation_id": request.operation_id,
                    "stage": 1,
                    "instance_token": "middle-instance",
                    "workspace_id": "middle-workspace",
                },
                Fence(
                    target=BranchKey(repository_id="repo", branch="aq/parent"),
                    owner_id="repair-middle",
                    token=4,
                ),
                SimpleNamespace(task_id="repair-middle", session_id="middle-session"),
            )
            middle_evidence = evidence | {
                "original_expected_target": second_gap,
                "resolved_head_sha": middle_head,
                "resolved_tree_sha": middle_tree,
                "repair_commit_shas": [middle_head],
                "authoring": {
                    "operation_id": request.operation_id,
                    "stage_ordinal": 1,
                    "repair_task_id": "repair-middle",
                    "repair_session_id": "middle-session",
                    "repair_session_instance_token": "middle-instance",
                    "repair_workspace_id": "middle-workspace",
                    "fence": {
                        "repository_id": "repo",
                        "branch": "aq/parent",
                        "owner_id": "repair-middle",
                        "token": 4,
                    },
                },
                "push_authority": middle_push["resolution_push_evidence"],
                "remote_proof": middle_remote,
            }
    if interior_gap:
        await _code_receipt(
            db,
            "parent.3",
            second_gap,
            middle_head,
            squash_sha=None,
            review_evidence={"review": {"source_base": "a" * 40}},
            resolution_evidence=middle_evidence,
        )
    await _code_receipt(
        db,
        "parent.4" if interior_gap else "parent.3",
        middle_head,
        final_head,
        squash_sha=None,
        review_evidence={"review": {"source_base": "a" * 40}},
        resolution_evidence=evidence,
    )
    request.head_sha = final_head
    request.expected_stage = 2
    request.expected_fence_token = 7
    return recovery, hierarchy, request, git, ((collected, first_gap), (second_child, second_gap))


def _captured_recovery_git(tmp_path, captured):
    """Rebuild captured Git facts without requiring the live repository objects."""
    import os
    import re

    from src.git.manager import GitManager

    origin, work = tmp_path / "origin.git", tmp_path / "work"

    def git(*args, stdin=None, stamp=None):
        env = os.environ.copy()
        if stamp is not None:
            env.update(GIT_AUTHOR_DATE=f"@{stamp} +0000", GIT_COMMITTER_DATE=f"@{stamp} +0000")
        return subprocess.run(
            ["git", *args],
            cwd=work if work.exists() else tmp_path,
            input=stdin,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--bare", "--initial-branch=main", str(origin))
    git("clone", str(origin), str(work))
    git("config", "user.name", "Captured Repair")
    git("config", "user.email", "repair@example.test")
    aliases = {}
    for node in captured["git_nodes"]:
        if node["tree"] not in aliases:
            blob = git("hash-object", "-w", "--stdin", stdin=node["tree"])
            aliases[node["tree"]] = git("mktree", stdin=f"100644 blob {blob}\tcontent\n")
        parents = [argument for parent in node["parents"] for argument in ("-p", aliases[parent])]
        aliases[node["sha"]] = git(
            "commit-tree",
            aliases[node["tree"]],
            *parents,
            stdin=node["sha"] + "\n",
            stamp=node["timestamp"],
        )
    reverse = {value: key for key, value in aliases.items()}
    oid = re.compile(r"\b[0-9a-f]{40}\b")

    class CapturedGit(GitManager):
        # These adapters translate fixture OIDs only. Ancestry, ordered ranges,
        # first-parent traversal and trees are computed by real Git commands.
        async def arun_git_result(self, args, **kwargs):
            result = await super().arun_git_result(
                [oid.sub(lambda match: aliases.get(match[0], match[0]), arg) for arg in args],
                **kwargs,
            )
            return subprocess.CompletedProcess(
                result.args,
                result.returncode,
                oid.sub(lambda match: reverse.get(match[0], match[0]), result.stdout),
                oid.sub(lambda match: reverse.get(match[0], match[0]), result.stderr),
            )

        async def als_remote_ref(self, *args, **kwargs):
            from dataclasses import replace

            result = await super().als_remote_ref(*args, **kwargs)
            return replace(result, oid=reverse.get(result.oid, result.oid))

    return git, CapturedGit(), origin, aliases


async def _captured_collection_case(db, tmp_path, *, snapshot=False, damage=None):
    """Replay captured identities over a real Git DAG with translated object IDs."""
    from pathlib import Path

    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
    from src.integration.outbox import enqueue_integration_event
    from src.integration.parent_repair_heads import ParentHeadRecovery
    from src.integration.promotion import PromotionService

    captured = json.loads(
        (Path(__file__).parent / "fixtures/integration/parent_recovery_6397.json").read_text()
    )
    live_snapshot = json.loads((Path(__file__).parent / (
        "fixtures/integration/parent_recovery_6397_0441.json"
    )).read_text()) if snapshot else None
    git, git_manager, origin, aliases = _captured_recovery_git(tmp_path, captured)
    branch = captured["intents"][0]["target_branch"]
    git("push", "origin", f"{aliases[captured['base_sha']]}:refs/heads/main")
    git("push", "origin", f"{aliases[captured['head_sha']]}:refs/heads/{branch}")
    hierarchy, checkpointed, _children = await _parent_tree(
        db,
        children=11,
        base_sha=captured["base_sha"],
        parent_id=captured["parent_id"],
        branch=branch,
    )
    if live_snapshot:
        episode_id = live_snapshot["operation"]["episode_id"]
        async with db.immediate() as conn:
            episode = (await conn.execute(select(integration_parent_episodes).where(
                integration_parent_episodes.c.id == checkpointed["episode_id"],
            ))).mappings().one()
            await conn.execute(insert(integration_parent_episodes).values(
                **(dict(episode) | {"id": episode_id}),
            ))
            if damage == "other_episode":
                await conn.execute(insert(integration_parent_episodes).values(
                    **(dict(episode) | {"id": "unrelated-episode"}),
                ))
            await conn.execute(update(integration_repair_operations).where(
                integration_repair_operations.c.id == checkpointed["operation_id"],
            ).values(episode_id=episode_id))
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == captured["parent_id"],
            ).values(episode_id=episode_id))
        checkpointed["episode_id"] = episode_id
    operation_id, parent_id = captured["operation_id"], captured["parent_id"]
    for stage in captured["stages"]:
        await db.create_task(
            Task(
                id=stage["repair_task_id"],
                project_id="p",
                repo_id="repo",
                branch_name=branch,
                title="Captured repair",
                description="Captured authorized collection repair",
                status=TaskStatus.COMPLETED,
                created_by_kind="integration_repair",
                created_by_id=operation_id,
            )
        )
    for row in captured["completions"]:
        await db.save_task_completion(
            TaskCompletion(
                **(row | {"commits": json.loads(row["commits"])}),
            )
        )
    final_task = captured["stages"][-1]["repair_task_id"]
    await db.set_task_meta(final_task, ACCEPTED_CLOSE_KEY, captured["accepted_close"])
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == final_task).values(claim_epoch=2))
        await conn.execute(update(tasks).where(tasks.c.id == parent_id).values(status="PAUSED"))
        pins = (
            (
                await conn.execute(
                    select(integration_operation_artifact_pins).where(
                        integration_operation_artifact_pins.c.operation_id
                        == checkpointed["operation_id"],
                    )
                )
            )
            .mappings()
            .all()
        )
        await conn.execute(
            delete(integration_operation_artifact_pins).where(
                integration_operation_artifact_pins.c.operation_id == checkpointed["operation_id"],
            )
        )
        await conn.execute(
            update(integration_repair_operations)
            .where(
                integration_repair_operations.c.id == checkpointed["operation_id"],
            )
            .values(id=operation_id, state="escalated", active_stage=6)
        )
        for pin in pins:
            await conn.execute(
                insert(integration_operation_artifact_pins).values(
                    **(dict(pin) | {"operation_id": operation_id}),
                )
            )
        await conn.execute(
            update(integration_branch_owners).values(
                owner_id=operation_id,
                owner_role="collector",
                handoff_state="reserved",
                fence_token=41,
            )
        )
        for stage in captured["stages"]:
            await conn.execute(
                insert(integration_repair_stages).values(
                    operation_id=operation_id,
                    ordinal=stage["ordinal"],
                    policy=_boundary().repair.model_dump(mode="json"),
                    intelligence_class="deep-high",
                    starting_sha=stage["starting_sha"],
                    trigger_id="captured-check",
                    writer_kind=stage["writer_kind"],
                    repair_task_id=stage["repair_task_id"],
                    current_subject=stage["current_subject"],
                    success_subject=stage["success_subject"],
                    started_at=1.0,
                    deadline_at=2.0,
                    deadline_event_id=f"captured-{stage['ordinal']}",
                    attempts=0,
                    state=stage["state"],
                    dossier={
                        "repair_commits": stage["repair_commits"],
                        "branch_sha": stage["branch_sha"],
                        **({"parent_head_extensions": live_snapshot["stages"][stage["ordinal"]][
                            "parent_head_extensions"
                        ]} if live_snapshot else {}),
                    },
                )
            )
            if stage["ordinal"] < 6:
                row = (
                    (
                        await conn.execute(
                            select(tasks).where(
                                tasks.c.id == stage["repair_task_id"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                archived = {key: value for key, value in row.items() if key in archived_tasks.c}
                await conn.execute(
                    insert(archived_tasks).values(**archived, archived_at=1791076000.0)
                )
                await conn.execute(delete(tasks).where(tasks.c.id == stage["repair_task_id"]))
        for row in captured["close_events"]:
            event_id = row["payload"]["event_id"]
            await enqueue_integration_event(
                conn,
                event_id=event_id,
                dedup_key=event_id,
                project_id="p",
                event_type="integration.repair_delegate_closed",
                payload=row["payload"],
                available_at=row["created_at"],
            )
            await conn.execute(
                update(integration_outbox)
                .where(
                    integration_outbox.c.id == event_id,
                )
                .values(created_at=row["created_at"])
            )
        checkpoint = (
            (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == parent_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        # All eight committed conflict receipts use the captured producer fields.
        receipt_rows = []
        for intent in captured["intents"]:
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == intent["source_task_id"],
            ).values(checkpoint_sha=intent["source_head"]))
            await conn.execute(
                insert(integration_promotion_intents).values(
                    **{key: value for key, value in intent.items() if value is not None}
                )
            )
            receipt_rows.append(
                {
                    "id": intent["receipt_id"],
                    "source_task_id": intent["source_task_id"],
                    "before_sha": intent["expected_target"],
                    "after_sha": intent["resolution_head_sha"],
                    "squash_sha": None,
                    "reviewed_head_sha": intent["source_head"],
                    "reviewed_tree_sha": "c" * 40,
                    "review_evidence": {"review": {"source_base": intent["source_base"]}},
                    "resolution_evidence": {
                        "kind": "conflict_resolution",
                        "original_source_base": intent["source_base"],
                        "original_source_head": intent["source_head"],
                        "original_source_tree": "c" * 40,
                        "original_expected_target": intent["expected_target"],
                        "resolved_head_sha": intent["resolution_head_sha"],
                        "resolved_tree_sha": intent["resolution_tree_sha"],
                        "repair_commit_shas": intent["resolution_commit_shas"],
                        "authoring": {
                            "operation_id": operation_id,
                            "stage_ordinal": intent["resolution_stage_ordinal"],
                            "repair_task_id": intent["resolution_task_id"],
                            "repair_session_id": intent["resolution_session_id"],
                            "repair_session_instance_token": intent[
                                "resolution_session_instance_token"
                            ],
                            "repair_workspace_id": intent["resolution_workspace_id"],
                            "fence": {
                                "repository_id": "repo",
                                "branch": branch,
                                "owner_id": intent["resolution_fence_owner_id"],
                                "token": intent["resolution_fence_token"],
                            },
                        },
                        "push_authority": intent["resolution_push_evidence"],
                        "remote_proof": intent["remote_evidence"],
                    },
                }
            )
        nodes = {node["sha"]: node for node in captured["git_nodes"]}
        for child, head in (
            (2, captured["stages"][0]["starting_sha"]),
            (4, captured["stages"][1]["starting_sha"]),
            (11, captured["stages"][6]["starting_sha"]),
        ):
            receipt_rows.append(
                {
                    "id": f"captured-clean-{child}",
                    "source_task_id": f"{parent_id}.{child}",
                    "before_sha": nodes[head]["parents"][0],
                    "after_sha": head,
                    "squash_sha": head,
                    "reviewed_head_sha": "b" * 40,
                    "reviewed_tree_sha": "c" * 40,
                    "review_evidence": {"review": "captured clean delivery"},
                }
            )
        first_parent, cursor = [], captured["head_sha"]
        while cursor != captured["base_sha"]:
            first_parent.append(cursor)
            cursor = nodes[cursor]["parents"][0]
        first_parent.reverse()
        if live_snapshot:
            receipt_rows = live_snapshot["receipts"]
        for row in receipt_rows:
            row = dict(row)
            if live_snapshot:
                await conn.execute(update(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == row["source_task_id"],
                ).values(checkpoint_sha=row["reviewed_head_sha"]))
                edge = live_snapshot["stages"][0]["parent_head_extensions"][0]
                if (row["before_sha"], row["after_sha"]) == (edge["before_sha"], edge["after_sha"]):
                    _damage_captured_receipt(row, damage)
            await conn.execute(
                insert(task_delivery_receipts).values(
                    **(row | {"domain_key": row["id"], "target_task_id": parent_id,
                    "repository_id": "repo", "target_branch": branch,
                    "parent_operation_id": operation_id,
                    "parent_episode_id": (
                        "unrelated-episode" if live_snapshot and damage == "other_episode"
                        and row["after_sha"] == live_snapshot["stages"][0][
                            "parent_head_extensions"
                        ][0]["after_sha"] else checkpoint["episode_id"]
                    ),
                    "disposition": "code",
                    "created_at": row.get("created_at", float(first_parent.index(
                        row["after_sha"],
                    ) + 1)),
                    }),
                )
            )
    repo = RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin))
    recovery = ParentHeadRecovery(
        PromotionService(
            db,
            data_dir=tmp_path / "retained",
            git_manager=git_manager,
            repository_resolver=lambda _: repo,
        )
    )
    request = SimpleNamespace(
        operation_id=operation_id, head_sha=captured["head_sha"], dry_run=True,
        expected_episode_id=checkpoint["episode_id"], expected_generation=checkpoint["generation"],
        expected_stage=6, expected_fence_token=41, reason="Replay the captured collection proof",
    )
    return recovery, hierarchy, request, live_snapshot or captured


async def _captured_phase4_case(db, tmp_path, *, damage=None):
    from pathlib import Path

    from src.database.queries.result_queries import close_identity
    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
    from src.integration.outbox import enqueue_integration_event
    from src.integration.parent_repair_heads import ParentHeadRecovery
    from src.integration.promotion import PromotionService

    captured = json.loads((Path(__file__).parent / (
        "fixtures/integration/parent_recovery_phase4_0441.json"
    )).read_text())
    git, git_manager, origin, aliases = _captured_recovery_git(tmp_path, captured)
    operation = captured["operation"]
    parent_id = operation["parent_task_id"]
    branch = captured["completions"][0]["branch"]
    git("push", "origin", f"{aliases[captured['base_sha']]}:refs/heads/main")
    git("push", "origin", f"{aliases[captured['head_sha']]}:refs/heads/{branch}")
    hierarchy, initial, _children = await _parent_tree(
        db, children=3, base_sha=captured["base_sha"], parent_id=parent_id, branch=branch,
    )
    for stage in captured["stages"]:
        await db.create_task(Task(
            id=stage["repair_task_id"], project_id="p", repo_id="repo", branch_name=branch,
            title="Captured phase4 repair", description="Snapshot replay", status=TaskStatus.COMPLETED,
            created_by_kind="integration_repair", created_by_id=operation["id"],
        ))
    for index, row in enumerate(captured["completions"]):
        completion_id = f"captured-phase4-completion-{index}"
        await db.save_task_completion(TaskCompletion(
            **(row | {"id": completion_id, "commits": json.loads(row["commits"])}),
        ))
        await db.set_task_meta(row["task_id"], ACCEPTED_CLOSE_KEY, close_identity(
            completion_id, session_id=captured["close_audits"][index]["payload"]["session_id"],
            claim_epoch=0,
        ))
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == parent_id).values(status="PAUSED"))
        pins = (await conn.execute(select(integration_operation_artifact_pins).where(
            integration_operation_artifact_pins.c.operation_id == initial["operation_id"],
        ))).mappings().all()
        await conn.execute(delete(integration_operation_artifact_pins).where(
            integration_operation_artifact_pins.c.operation_id == initial["operation_id"],
        ))
        episode = (await conn.execute(select(integration_parent_episodes).where(
            integration_parent_episodes.c.id == initial["episode_id"],
        ))).mappings().one()
        await conn.execute(insert(integration_parent_episodes).values(
            **(dict(episode) | {"id": operation["episode_id"], "generation": 2}),
        ))
        await conn.execute(update(integration_repair_operations).where(
            integration_repair_operations.c.id == initial["operation_id"],
        ).values(id=operation["id"], episode_id=operation["episode_id"], state=operation["state"],
                 active_stage=1))
        for pin in pins:
            await conn.execute(insert(integration_operation_artifact_pins).values(
                **(dict(pin) | {"operation_id": operation["id"]}),
            ))
        if damage == "other_episode":
            await conn.execute(insert(integration_parent_episodes).values(
                **(dict(episode) | {"id": "unrelated-episode"}),
            ))
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == parent_id,
        ).values(episode_id=operation["episode_id"], generation=2, state="verifying",
                 checkpoint_sha=captured["base_sha"]))
        for stage in captured["stages"]:
            incident = stage["supervisor_recovery"]
            await conn.execute(insert(integration_repair_stages).values(
                operation_id=operation["id"], ordinal=stage["ordinal"],
                policy=_boundary().repair.model_dump(mode="json"), intelligence_class="deep-high",
                starting_sha=stage["starting_sha"], trigger_id="captured-phase4-check",
                writer_kind="repair_delegate", repair_task_id=stage["repair_task_id"],
                current_subject=stage["current_subject"], success_subject=stage["success_subject"],
                started_at=1.0, deadline_at=incident["deadline_at"] if incident else 2.0,
                deadline_event_id=f"captured-phase4-deadline-{stage['ordinal']}",
                attempts=stage["attempts"], state=stage["state"], completed_at=stage["completed_at"],
                dossier={
                    "repair_commits": stage["repair_commits"],
                    "branch_sha": stage["current_subject"]["head_sha"],
                    "parent_head_extensions": stage["parent_head_extensions"],
                    **({"supervisor_recovery": incident} if incident else {}),
                },
            ))
        for row in captured["close_audits"]:
            event_id = row["payload"]["event_id"]
            await enqueue_integration_event(
                conn, event_id=event_id, dedup_key=event_id, project_id="p",
                event_type="integration.repair_delegate_closed", payload=row["payload"],
                available_at=row["created_at"],
            )
            await conn.execute(update(integration_outbox).where(
                integration_outbox.c.id == event_id,
            ).values(created_at=row["created_at"]))
        for index, row in enumerate(captured["receipts"]):
            receipt = dict(row)
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == row["source_task_id"],
            ).values(checkpoint_sha=row["reviewed_head_sha"]))
            if index == 1:
                _damage_captured_receipt(receipt, damage)
                if damage == "other_episode":
                    receipt["parent_episode_id"] = "unrelated-episode"
            await conn.execute(insert(task_delivery_receipts).values(**receipt))
        await conn.execute(insert(workspaces).values(
            id="captured-phase4-confirmed", project_id="p", workspace_path=str(tmp_path / "work"),
            source_type="link", enabled=True, created_at=1.0,
        ))
        await conn.execute(update(integration_branch_owners).values(
            owner_id=operation["id"], owner_role="collector", handoff_state="reserved",
            fence_token=8, confirmed_workspace_id="captured-phase4-confirmed",
        ))
    repo = RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin))
    recovery = ParentHeadRecovery(PromotionService(
        db, data_dir=tmp_path / "retained", git_manager=git_manager,
        repository_resolver=lambda _: repo,
    ))
    request = SimpleNamespace(
        operation_id=operation["id"], head_sha=captured["head_sha"], dry_run=True,
        expected_episode_id=operation["episode_id"], expected_generation=2,
        expected_stage=1, expected_fence_token=8, reason="Replay captured phase4 snapshot",
    )
    return recovery, hierarchy, request, captured


def _damage_captured_receipt(receipt, damage):
    """Insert bad evidence as captured; append-only receipt triggers stay enabled."""
    from copy import deepcopy

    if damage in {"receipt_range", "untrusted_receipt"}:
        evidence = deepcopy(receipt["resolution_evidence"])
        if damage == "receipt_range":
            evidence["repair_commit_shas"] = ["a" * 40, *evidence["repair_commit_shas"]]
            evidence["remote_proof"]["repair_commit_shas"] = evidence["repair_commit_shas"]
        else:
            evidence["remote_proof"] = {}
        receipt["resolution_evidence"] = evidence


async def _captured_discord_snapshot_case(db, tmp_path, *, damage=None):
    return await _captured_collection_case(db, tmp_path, snapshot=True, damage=damage)


@pytest.mark.parametrize("active_origin", [False, True])
async def test_collection_apply_keeps_two_proved_gaps_from_one_stage(db, tmp_path, active_origin):
    """Repeated origin snapshots must not drop a gap before projecting readiness."""
    recovery, hierarchy, request, _git, gaps = await _finished_collection_case(db, tmp_path)
    assert (await recovery.run(request, principal="supervisor"))["outcome"] == "would_recover"
    async with db.immediate() as conn:
        proof = await recovery._proof_on(conn, request)
        origin = proof["stage"] if active_origin else proof["gap_proofs"][0]["stage"]
        author = proof["authoring"] if active_origin else proof["gap_proofs"][0]["authoring"]
        for gap in proof["gap_proofs"]:
            gap["stage"], gap["authoring"] = dict(origin), dict(author)
        await recovery._recover_collection_on(
            conn, proof,
            [{"base_sha": before, "head_sha": after, "commits": [after]}
             for before, after in gaps],
            request=request, principal="supervisor", result={},
        )
        dossier = await conn.scalar(select(integration_repair_stages.c.dossier).where(
            integration_repair_stages.c.ordinal == origin["ordinal"],
        ))
        assert [(edge["before_sha"], edge["after_sha"])
                for edge in dossier["parent_head_extensions"]] == list(gaps)
        if active_origin:
            assert dossier["collection_head_recovery"]["head_sha"] == request.head_sha
            assert dossier["resolution_verification"]["resolution_head_sha"] == request.head_sha
    assert (await hierarchy.readiness("parent"))["outcome"] == "ready"


@pytest.mark.parametrize("damage", ["duplicate", "missing", "extra"])
async def test_empty_aggregate_requires_each_git_commit_exactly_once(db, tmp_path, damage):
    recovery, _hierarchy, request, captured = await _captured_phase4_case(db, tmp_path)
    receipts = list(captured["receipts"])
    git = recovery.promotion.git
    repository = await recovery.promotion._resolve_repository("repo")
    await recovery.promotion._ensure_retained_repository(repository)
    store = repository.retained_git_dir
    async with git.arepository_transaction(str(store)):
        await recovery.promotion._fetch_all_heads(store, repository.origin_url)
        if damage == "duplicate":
            receipts.append(receipts[0])
        elif damage == "missing":
            receipts.pop(0)
        else:
            # A valid receipt for a new Git commit is outside the published head.
            extra = await git.arun_git_result(
                ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit-tree", captured["git_nodes"][0]["tree"],
                 "-p", request.head_sha, "-m", "Extra receipted commit"],
                cwd=str(store), lock_held=True,
            )
            assert extra.returncode == 0
            row = dict(receipts[0])
            row.update(before_sha=request.head_sha, after_sha=extra.stdout.strip(),
                       squash_sha=extra.stdout.strip())
            receipts.append(row)
        with pytest.raises(ValueError, match="outside the complete receipt chain"):
            await recovery._empty_verification_git_proof(
                {"base_sha": captured["base_sha"], "following_receipts": receipts},
                store, request.head_sha,
            )


@pytest.mark.parametrize("damage", ["wrong_second_parent", "three_parents"])
async def test_squash_merge_requires_exact_aggregate_and_review_parents(db, tmp_path, damage):
    recovery, _hierarchy, _request, captured = await _captured_phase4_case(db, tmp_path)
    receipt = dict(captured["receipts"][0])
    repository = await recovery.promotion._resolve_repository("repo")
    await recovery.promotion._ensure_retained_repository(repository)
    store, git = repository.retained_git_dir, recovery.promotion.git
    async with git.arepository_transaction(str(store)):
        await recovery.promotion._fetch_all_heads(store, repository.origin_url)
        if damage == "wrong_second_parent":
            receipt["reviewed_head_sha"] = captured["head_sha"]
        else:
            extra = await git.arun_git_result(
                ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                 "commit-tree", captured["git_nodes"][0]["tree"],
                 "-p", receipt["before_sha"], "-p", receipt["reviewed_head_sha"],
                 "-p", captured["head_sha"], "-m", "Unreviewed third merge parent"],
                cwd=str(store), lock_held=True,
            )
            assert extra.returncode == 0
            receipt.update(after_sha=extra.stdout.strip(), squash_sha=extra.stdout.strip())
        with pytest.raises(ValueError, match="merge parents differ from aggregate and review"):
            await recovery._receipt_git_commits(receipt, store)


@pytest.mark.parametrize("damage", ["first_parent_base", "complete_range"])
async def test_receipt_duplicate_requires_exact_git_range_and_lineage(db, tmp_path, damage):
    from copy import deepcopy

    recovery, _hierarchy, request, captured = await _captured_phase4_case(db, tmp_path)
    edge = deepcopy(captured["stages"][0]["parent_head_extensions"][0])
    node = next(n for n in captured["git_nodes"] if n["sha"] == edge["after_sha"])
    assert len(node["parents"]) == 2
    if damage == "first_parent_base":
        edge["before_sha"] = node["parents"][1]
    repository = await recovery.promotion._resolve_repository("repo")
    await recovery.promotion._ensure_retained_repository(repository)
    store = repository.retained_git_dir
    async with recovery.promotion.git.arepository_transaction(str(store)):
        await recovery.promotion._fetch_all_heads(store, repository.origin_url)
        edge["commits"] = await recovery.promotion._resolution_commit_range(
            store, edge["before_sha"], edge["after_sha"],
        )
        assert await recovery.promotion._is_ancestor(store, edge["before_sha"], edge["after_sha"])
        if damage == "complete_range":
            edge["commits"] = edge["commits"][1:]
        reason = (
            "base is absent from its first-parent lineage"
            if damage == "first_parent_base" else "differs from its complete Git commit range"
        )
        with pytest.raises(ValueError, match=reason):
            await recovery._recorded_extensions_git_proof(
                {"recorded_extensions": [edge], "covered_extensions": [{
                    "extension": edge, "receipt": captured["receipts"][1],
                }]},
                store, request.head_sha,
            )


@pytest.mark.parametrize(("snapshot", "damage"), [
    (snapshot, damage)
    for snapshot in ("discord", "phase4")
    for damage in (None, "edge_range", "receipt_range", "untrusted_receipt", "other_episode",
                   "unconsumed_edge", "introduced_commit", "non_empty_completion")
    if snapshot == "phase4" or damage not in {"introduced_commit", "non_empty_completion"}
])
async def test_parent_recovery_replays_0441_snapshot(db, tmp_path, snapshot, damage):
    from copy import deepcopy

    build = _captured_discord_snapshot_case if snapshot == "discord" else _captured_phase4_case
    recovery, hierarchy, request, captured = await build(db, tmp_path, damage=damage)
    async with db.immediate() as conn:
        stages = (await conn.execute(select(integration_repair_stages).order_by(
            integration_repair_stages.c.ordinal,
        ))).mappings().all()
        first = stages[0]
        dossier = deepcopy(first["dossier"])
        edge = dossier["parent_head_extensions"][0]
        if damage == "edge_range":
            edge["commits"] = edge["commits"][:-2] + edge["commits"][-1:]
        elif damage == "unconsumed_edge":
            extra = deepcopy(edge)
            extra["before_sha"], extra["after_sha"], extra["commits"] = (
                "e" * 40, "f" * 40, ["f" * 40],
            )
            dossier["parent_head_extensions"].append(extra)
        elif damage == "introduced_commit":
            current = stages[-1]
            await conn.execute(update(integration_repair_stages).where(
                integration_repair_stages.c.ordinal == current["ordinal"],
            ).values(dossier=current["dossier"] | {
                "repair_commits": [*current["dossier"]["repair_commits"], "a" * 40],
            }))
        elif damage == "non_empty_completion":
            current = stages[-1]
            await conn.execute(update(task_completion_records).where(
                task_completion_records.c.task_id == current["repair_task_id"],
            ).values(commits=json.dumps([current["current_subject"]["head_sha"]])))
        if damage in {"edge_range", "unconsumed_edge"}:
            await conn.execute(update(integration_repair_stages).where(
                integration_repair_stages.c.ordinal == 0,
            ).values(dossier=dossier))
        before_stages = (await conn.execute(select(integration_repair_stages))).mappings().all()
        before_receipts = (await conn.execute(select(task_delivery_receipts))).mappings().all()
    checkpoint = await db.get_integration_checkpoint(captured["operation"]["parent_task_id"])
    if damage:
        for dry_run in (True, False):
            request.dry_run = dry_run
            with pytest.raises(ValueError):
                await recovery.run(request, principal="supervisor")
    else:
        preview = await recovery.run(request, principal="supervisor")
        assert preview["outcome"] == "would_recover"
        assert preview["episode_id"] == captured["operation"]["episode_id"]
        if snapshot == "discord":
            assert preview["head_sha"] == "cd394b1a6c90d4cffcae1ce99aa6c8948286d021"
            assert preview["stage"] == 6
            assert "stage 2 6b2e6236" in preview["reason"]
            assert "stage 4 9c7910c7" in preview["reason"]
        else:
            assert preview["head_sha"] == "0c61aada8cb782c3a6a0d4150c62ff55feb47869"
            assert (preview["generation"], preview["stage"], preview["fence_token"]) == (2, 1, 8)
    assert await db.get_integration_checkpoint(captured["operation"]["parent_task_id"]) == checkpoint
    async with db.immediate() as conn:
        assert (await conn.execute(select(integration_repair_stages))).mappings().all() == before_stages
        assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == before_receipts
    if damage is None:
        request.dry_run = False
        assert (await recovery.run(request, principal="supervisor"))["outcome"] == "recovered"
        assert (await recovery.run(request, principal="supervisor"))["outcome"] == "already_recovered"
        readiness = await hierarchy.readiness(captured["operation"]["parent_task_id"])
        assert readiness["outcome"] == "ready"
        async with db.immediate() as conn:
            stages = (await conn.execute(select(integration_repair_stages).order_by(
                integration_repair_stages.c.ordinal,
            ))).mappings().all()
            assert sum(len((s["dossier"] or {}).get("receipt_covered_extensions", []))
                       for s in stages) == (10 if snapshot == "discord" else 1)
            for original, stage in zip(before_stages, stages, strict=True):
                assert stage["dossier"]["parent_head_extensions"] == (
                    original["dossier"]["parent_head_extensions"]
                )
                for field in ("attempts", "deadline_at", "current_subject"):
                    assert stage[field] == original[field]
            assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == before_receipts


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "missing_live_close",
        "live_epoch",
        "archived_live",
        "archived_extra_close",
        "archived_completion_time",
        "archived_extra_commit",
        "first_target",
        "stage4_audit",
        "stage4_fail",
        "stage4_completion_pair",
    ],
)
async def test_finished_collection_replays_captured_discord_epic(db, tmp_path, damage):
    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY

    recovery, _hierarchy, request, captured = await _captured_collection_case(db, tmp_path)
    async with db.immediate() as conn:
        stage2, stage4, stage6 = (captured["stages"][index] for index in (2, 4, 6))
        if damage == "missing_live_close":
            await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == stage6["repair_task_id"],
                    task_metadata.c.key == ACCEPTED_CLOSE_KEY,
                )
            )
        elif damage == "live_epoch":
            await conn.execute(
                update(tasks)
                .where(
                    tasks.c.id == stage6["repair_task_id"],
                )
                .values(claim_epoch=3)
            )
        elif damage == "archived_live":
            row = (
                (
                    await conn.execute(
                        select(archived_tasks).where(
                            archived_tasks.c.id == stage2["repair_task_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            await conn.execute(
                insert(tasks).values(**{key: value for key, value in row.items() if key in tasks.c})
            )
            await conn.execute(
                delete(archived_tasks).where(
                    archived_tasks.c.id == stage2["repair_task_id"],
                )
            )
        elif damage == "archived_extra_close":
            row = (
                (
                    await conn.execute(
                        select(integration_outbox).where(
                            integration_outbox.c.id
                            == captured["close_events"][3]["payload"]["event_id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            await conn.execute(
                insert(integration_outbox).values(
                    **(
                        dict(row)
                        | {
                            "id": "duplicate-archived-close",
                            "dedup_key": "duplicate-archived-close",
                        }
                    )
                )
            )
        elif damage == "archived_completion_time":
            await conn.execute(
                update(task_completion_records)
                .where(
                    task_completion_records.c.task_id == stage2["repair_task_id"],
                )
                .values(completed_at=1.0)
            )
        elif damage == "archived_extra_commit":
            stage = (
                (
                    await conn.execute(
                        select(integration_repair_stages).where(
                            integration_repair_stages.c.ordinal == 2,
                        )
                    )
                )
                .mappings()
                .one()
            )
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.ordinal == 2,
                )
                .values(
                    dossier=dict(stage["dossier"])
                    | {
                        "repair_commits": [*stage["dossier"]["repair_commits"], "a" * 40],
                    }
                )
            )
        elif damage == "first_target":
            first = next(row for row in captured["intents"] if row["resolution_stage_ordinal"] == 4)
            await conn.execute(
                update(integration_promotion_intents)
                .where(
                    integration_promotion_intents.c.id == first["id"],
                )
                .values(expected_target=captured["gaps"][1][0])
            )
        elif damage == "stage4_audit":
            row = (
                await conn.execute(
                    select(integration_outbox.c.payload).where(
                        integration_outbox.c.id
                        == captured["close_events"][7]["payload"]["event_id"],
                    )
                )
            ).scalar_one()
            await conn.execute(
                update(integration_outbox)
                .where(
                    integration_outbox.c.id == captured["close_events"][7]["payload"]["event_id"],
                )
                .values(payload=row | {"instance_token": "unrelated-instance"})
            )
        elif damage in {"stage4_fail", "stage4_completion_pair"}:
            first = next(
                row for row in captured["completions"] if row["task_id"] == stage4["repair_task_id"]
            )
            await conn.execute(
                update(task_completion_records)
                .where(
                    task_completion_records.c.id == first["id"],
                )
                .values(
                    **({"outcome": "fail"} if damage == "stage4_fail" else {"completed_at": 1.0})
                )
            )
        receipts = (await conn.execute(select(task_delivery_receipts))).mappings().all()
        stages = (await conn.execute(select(integration_repair_stages))).mappings().all()
        checkpoint = await db.get_integration_checkpoint(captured["parent_id"])
    if damage:
        for dry_run in (True, False):
            request.dry_run = dry_run
            with pytest.raises(ValueError):
                await recovery.run(request, principal="supervisor")
    else:
        preview = await recovery.run(request, principal="supervisor")
        assert preview["outcome"] == "would_recover"
        assert preview["head_sha"] == "cd394b1a6c90d4cffcae1ce99aa6c8948286d021"
        assert preview["stage"] == 6 and preview["fence_token"] == 41
        assert preview["completion_id"] == captured["accepted_close"]["completion_id"]
        for ordinal, (before, after) in zip((2, 4), captured["gaps"], strict=True):
            assert f"stage {ordinal} {before} -> {after}" in preview["reason"]
    assert await db.get_integration_checkpoint(captured["parent_id"]) == checkpoint
    async with db.immediate() as conn:
        assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == receipts
        assert (await conn.execute(select(integration_repair_stages))).mappings().all() == stages
    if damage is None:
        request.dry_run = False
        assert (await recovery.run(request, principal="supervisor"))["outcome"] == "recovered"
        assert (await recovery.run(request, principal="supervisor"))["outcome"] == "already_recovered"
        async with db.immediate() as conn:
            assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == receipts
            for ordinal, (before, after) in zip((2, 4), captured["gaps"], strict=True):
                stage = (await conn.execute(select(integration_repair_stages).where(
                    integration_repair_stages.c.ordinal == ordinal,
                ))).mappings().one()
                edge = stage["dossier"]["parent_head_extensions"][0]
                assert (edge["before_sha"], edge["after_sha"]) == (before, after)
                assert edge["authoring"]["fence_token"] == (12 if ordinal == 2 else 30)
                assert stage["current_subject"] == captured["stages"][ordinal]["current_subject"]
                assert stage["state"] == captured["stages"][ordinal]["state"]


@pytest.mark.parametrize(
    ("matching_closes", "incomplete_field"),
    [
        (0, None),
        (1, None),
        (2, None),
        (1, "session_id"),
        (1, "instance_token"),
        (1, "workspace_id"),
        (1, "fence_token"),
    ],
)
async def test_finished_collection_selects_one_exact_close_among_retries(
    db, tmp_path, matching_closes, incomplete_field
):
    from src.integration.outbox import enqueue_integration_event

    recovery, _hierarchy, request, _git, _gaps = await _finished_collection_case(db, tmp_path)
    async with db.immediate() as conn:
        for index in range(1 + matching_closes):
            payload = {
                "operation_id": request.operation_id,
                "stage": 2,
                "task_id": "repair-final",
                "session_id": "final-session" if index else "previous-session",
                "instance_token": "final-instance",
                "workspace_id": "final-workspace",
                "fence_token": 6,
            }
            if index == 0 and incomplete_field:
                del payload[incomplete_field]
            await enqueue_integration_event(
                conn,
                event_id=f"final-close-{index}",
                dedup_key=f"final-close-{index}",
                project_id="p",
                event_type="integration.repair_delegate_closed",
                available_at=26.0,
                payload=payload,
            )
    before = await db.get_integration_checkpoint("parent")
    for dry_run in (True, False):
        request.dry_run = dry_run
        if matching_closes == 1 and incomplete_field is None:
            assert (await recovery.run(request, principal="supervisor"))["outcome"] == (
                "would_recover" if dry_run else "recovered"
            )
        else:
            message = (
                "incomplete delegate-close audit"
                if incomplete_field
                else "no unique delegate-close audit"
            )
            with pytest.raises(ValueError, match=message):
                await recovery.run(request, principal="supervisor")
            assert await db.get_integration_checkpoint("parent") == before


@pytest.mark.parametrize(
    "field",
    [
        "kind",
        "remote_sha",
        "operation_id",
        "stage_ordinal",
        "repair_task_id",
        "repair_session_id",
        "repair_session_instance_token",
        "repair_workspace_id",
        "fence_owner_id",
        "fence_token",
        "two_fields",
        "unexpected_key",
    ],
)
async def test_finished_collection_refuses_matching_push_snapshots_with_wrong_identity(
    db, tmp_path, field
):
    recovery, _hierarchy, request, _git, _gaps = await _finished_collection_case(
        db,
        tmp_path,
        push_damage=field,
    )
    before = await db.get_integration_checkpoint("parent")
    for dry_run in (True, False):
        request.dry_run = dry_run
        with pytest.raises(ValueError, match="stage 2 repair lacks"):
            await recovery.run(request, principal="supervisor")
        assert await db.get_integration_checkpoint("parent") == before


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "missing_audit",
        "missing_gap",
        "wrong_subject",
        "wrong_first_target",
        "failed_completion",
        "wrong_earlier_audit",
        "extra_recorded_commit",
    ],
)
async def test_finished_collection_proves_interior_gap_from_its_originating_stage(
    db,
    tmp_path,
    damage,
):
    from src.integration.outbox import enqueue_integration_event

    recovery, hierarchy, request, git, gaps = await _finished_collection_case(
        db,
        tmp_path,
        interior_gap=True,
    )
    await db.save_task_completion(
        TaskCompletion(
            id="previous-middle-completion",
            task_id="repair-middle",
            outcome="pass",
            branch="aq/parent",
            commits=[],
            completed_at=20.0,
        )
    )
    async with db.immediate() as conn:
        # The historical stage has retry audits too. Only its exact latest
        # resolution author can prove its completed subject.
        await enqueue_integration_event(
            conn,
            event_id="previous-middle-close",
            dedup_key="previous-middle-close",
            project_id="p",
            event_type="integration.repair_delegate_closed",
            available_at=20.0,
            payload={
                "project_id": "p",
                "operation_id": request.operation_id,
                "stage": 1,
                "task_id": "repair-middle",
                "session_id": "previous-middle-session",
                "instance_token": "middle-instance",
                "workspace_id": "middle-workspace",
                "fence_token": 3,
            },
        )
        await conn.execute(
            update(integration_outbox)
            .where(
                integration_outbox.c.id == "previous-middle-close",
            )
            .values(created_at=19.0)
        )
        await conn.execute(
            update(integration_outbox)
            .where(
                integration_outbox.c.id == "middle-closed",
            )
            .values(created_at=21.5)
        )
        if damage == "missing_audit":
            await conn.execute(
                delete(integration_outbox).where(
                    integration_outbox.c.id == "middle-closed",
                )
            )
        elif damage == "missing_gap":
            stage = (
                (
                    await conn.execute(
                        select(integration_repair_stages).where(
                            integration_repair_stages.c.ordinal == 1,
                        )
                    )
                )
                .mappings()
                .one()
            )
            dossier = dict(stage["dossier"])
            dossier["repair_commits"] = [
                sha for sha in dossier["repair_commits"] if sha != gaps[1][1]
            ]
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.ordinal == 1,
                )
                .values(dossier=dossier)
            )
        elif damage == "wrong_subject":
            await conn.execute(
                update(task_completion_records)
                .where(
                    task_completion_records.c.task_id == "repair-middle",
                )
                .values(commits=json.dumps([gaps[1][1]]))
            )
        elif damage == "wrong_first_target":
            await conn.execute(
                update(integration_promotion_intents)
                .where(
                    integration_promotion_intents.c.id == "middle-intent",
                )
                .values(expected_target=gaps[1][0])
            )
        elif damage == "failed_completion":
            await conn.execute(
                update(task_completion_records)
                .where(
                    task_completion_records.c.id == "previous-middle-completion",
                )
                .values(outcome="fail")
            )
        elif damage == "wrong_earlier_audit":
            audit = (
                await conn.execute(
                    select(integration_outbox.c.payload).where(
                        integration_outbox.c.id == "middle-closed",
                    )
                )
            ).scalar_one()
            await conn.execute(
                update(integration_outbox)
                .where(
                    integration_outbox.c.id == "middle-closed",
                )
                .values(payload=audit | {"workspace_id": "unrelated-workspace"})
            )
        elif damage == "extra_recorded_commit":
            stage = (
                (
                    await conn.execute(
                        select(integration_repair_stages).where(
                            integration_repair_stages.c.ordinal == 1,
                        )
                    )
                )
                .mappings()
                .one()
            )
            dossier = dict(stage["dossier"])
            recorded = list(dossier["repair_commits"])
            recorded.insert(recorded.index(gaps[1][1]), gaps[1][0])
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.ordinal == 1,
                )
                .values(dossier=dossier | {"repair_commits": recorded})
            )
        stages = (
            (
                await conn.execute(
                    select(integration_repair_stages).order_by(
                        integration_repair_stages.c.ordinal,
                    )
                )
            )
            .mappings()
            .all()
        )
        receipts = (await conn.execute(select(task_delivery_receipts))).mappings().all()
    before = await db.get_integration_checkpoint("parent")
    for dry_run in (True, False):
        request.dry_run = dry_run
        if damage:
            with pytest.raises(ValueError):
                await recovery.run(request, principal="supervisor")
            assert await db.get_integration_checkpoint("parent") == before
        else:
            result = await recovery.run(request, principal="supervisor")
            assert result["outcome"] == ("would_recover" if dry_run else "recovered")
            assert all(f"{start} -> {end}" in result["reason"] for start, end in gaps)
            if dry_run:
                assert await db.get_integration_checkpoint("parent") == before
    if damage is None:
        assert (await hierarchy.readiness("parent"))["head_sha"] == request.head_sha
        assert (await recovery.run(request, principal="supervisor"))[
            "outcome"
        ] == "already_recovered"
    async with db._engine.connect() as conn:
        updated = (
            (
                await conn.execute(
                    select(integration_repair_stages).order_by(
                        integration_repair_stages.c.ordinal,
                    )
                )
            )
            .mappings()
            .all()
        )
        assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == receipts
        if damage:
            assert updated == stages
        else:
            (edge,) = updated[1]["dossier"]["parent_head_extensions"]
            assert (edge["before_sha"], edge["after_sha"]) == gaps[1]
            assert (
                edge["commits"]
                == git("rev-list", "--reverse", f"{gaps[1][0]}..{gaps[1][1]}").splitlines()
            )
            assert edge["authoring"]["fence_token"] == 4
            assert updated[1]["current_subject"] == stages[1]["current_subject"]
            assert updated[1]["starting_sha"] == stages[1]["starting_sha"]
            assert updated[1]["state"] == stages[1]["state"]


@pytest.mark.parametrize("archived", [False, True])
async def test_finished_collection_reconciles_both_gaps_and_files_fresh_verifier(
    db, tmp_path, archived
):
    recovery, hierarchy, request, git, gaps = await _finished_collection_case(db, tmp_path)
    async with db._engine.connect() as conn:
        receipts = (await conn.execute(select(task_delivery_receipts))).mappings().all()
        stages = (
            (
                await conn.execute(
                    select(integration_repair_stages).order_by(
                        integration_repair_stages.c.ordinal,
                    )
                )
            )
            .mappings()
            .all()
        )
        completions = (await conn.execute(select(task_completion_records))).mappings().all()
    if archived:
        from src.database.tables import task_metadata

        async with db.immediate() as conn:
            # Archives discard accepted-close metadata. An archived delegate
            # therefore needs its original nonempty completion proof.
            await conn.execute(delete(task_metadata).where(
                task_metadata.c.task_id == "repair-final",
            ))
            await conn.execute(update(task_completion_records).where(
                task_completion_records.c.task_id == "repair-final",
            ).values(commits=json.dumps([request.head_sha])))
            completions = (await conn.execute(select(task_completion_records))).mappings().all()
            task = dict((await conn.execute(select(tasks).where(
                tasks.c.id == "repair-final",
            ))).mappings().one())
            await conn.execute(insert(archived_tasks).values(
                **{key: value for key, value in task.items() if key in archived_tasks.c},
                archived_at=30.0,
            ))
            await conn.execute(delete(tasks).where(tasks.c.id == "repair-final"))
    refs = git("ls-remote", "origin")
    preview = await recovery.run(request, principal="supervisor")
    assert preview["outcome"] == "would_recover"
    assert all(f"{before} -> {after}" in preview["reason"] for before, after in gaps)
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == gaps[0][0]
    assert await db.get_task(f"verify-{request.operation_id}") is None
    request.dry_run = False
    assert (await recovery.run(request, principal="supervisor"))["outcome"] == "recovered"
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["checkpoint_sha"] == request.head_sha
    assert checkpoint["verified_sha"] is None
    assert checkpoint["current_verification_id"] is None
    assert checkpoint["state"] == "integration_ready"
    assert (await db.get_task(f"verify-{request.operation_id}")).status == TaskStatus.PAUSED
    assert (await hierarchy.verify_parent("parent", 1, request.head_sha, []))[
        "outcome"
    ] != "verified"
    assert (await recovery.run(request, principal="supervisor"))["outcome"] == "already_recovered"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == receipts
        assert (await conn.execute(select(task_completion_records))).mappings().all() == completions
        updated = (
            (
                await conn.execute(
                    select(integration_repair_stages).order_by(
                        integration_repair_stages.c.ordinal,
                    )
                )
            )
            .mappings()
            .all()
        )
        for index, stage in enumerate(updated):
            assert stage["attempts"] == stages[index]["attempts"]
            assert stage["deadline_at"] == stages[index]["deadline_at"]
            assert stage["policy"] == stages[index]["policy"]
            if index < 2:
                assert stage["state"] == stages[index]["state"]
                (edge,) = stage["dossier"]["parent_head_extensions"]
                assert (edge["before_sha"], edge["after_sha"]) == gaps[index]
        assert updated[2]["state"] == "passed"
        operation = (await conn.execute(select(integration_repair_operations))).mappings().one()
        assert operation["state"] == "escalated"
        events = (
            (
                await conn.execute(
                    select(integration_outbox).where(
                        integration_outbox.c.event_type == "task.integration_ready",
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(events) == 1
        assert events[0]["payload"]["head_sha"] == request.head_sha
    assert git("ls-remote", "origin") == refs
    transferred = await hierarchy.ownership.transfer(
        Fence(target=BranchKey(repository_id="repo", branch="aq/parent"),
              owner_id=request.operation_id, token=7),
        f"verify-{request.operation_id}", "verifier",
    )
    assert (await hierarchy.wake_verifier("parent", transferred))["outcome"] == "woken"
    assert (await hierarchy.complete_parent("parent", 1, request.head_sha))[
        "outcome"
    ] == AWAITING_TRUSTED_VERIFICATION
    async with db.immediate() as conn:
        await conn.execute(insert(integration_check_evidence).values(
            id="fresh-collection-check", operation_id=request.operation_id,
            parent_task_id="parent", parent_generation=1, parent_head_sha=request.head_sha,
            producer_id="forge-observer", workflow_id="workflow", run_id="fresh-run",
            attempt=1, required_check_version="parent-v1", checks={"unit": "success"},
            conclusion="success", classification="conclusive", observed_at=40.0,
        ))
    assert (await hierarchy.verify_parent("parent", 1, request.head_sha, [
        "fresh-collection-check",
    ]))["outcome"] == "verified"


@pytest.mark.parametrize(
    "damage",
    [
        "gap_audit",
        "gap_lineage",
        "final_intent_fence",
        "final_push",
        "final_tree",
        "accepted_close",
        "latest_completion",
        "remote_head",
        "check_failure",
    ],
)
async def test_finished_collection_refuses_unproved_gap_or_resolution(db, tmp_path, damage):
    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY

    recovery, _hierarchy, request, git, gaps = await _finished_collection_case(
        db, tmp_path, wrong_tree=damage == "final_tree"
    )
    if damage == "accepted_close":
        await db.set_task_meta("repair-final", ACCEPTED_CLOSE_KEY, "{}")
    elif damage == "remote_head":
        git("commit", "--allow-empty", "-m", "unrecorded write")
        git("push", "origin", "aq/parent")
    elif damage == "latest_completion":
        await db.save_task_completion(
            TaskCompletion(
                id="later-failure",
                task_id="repair-middle",
                outcome="fail",
                branch="aq/parent",
                commits=[gaps[1][1]],
                completed_at=30.0,
            )
        )
    else:
        async with db.immediate() as conn:
            if damage == "gap_audit":
                await conn.execute(
                    delete(integration_outbox).where(
                        integration_outbox.c.id == "middle-closed",
                    )
                )
            elif damage == "gap_lineage":
                await conn.execute(
                    update(integration_repair_stages)
                    .where(
                        integration_repair_stages.c.ordinal == 1,
                    )
                    .values(dossier={"branch_sha": gaps[1][1], "repair_commits": ["a" * 40]})
                )
            elif damage == "final_intent_fence":
                await conn.execute(
                    update(integration_promotion_intents).values(resolution_fence_token=4)
                )
            elif damage == "final_push":
                await conn.execute(
                    update(integration_promotion_intents).values(resolution_push_evidence={})
                )
            elif damage == "check_failure":
                await conn.execute(
                    insert(integration_check_evidence).values(
                        id="final-failed-check",
                        operation_id=request.operation_id,
                        parent_task_id="parent",
                        parent_generation=1,
                        parent_head_sha=request.head_sha,
                        producer_id="observer",
                        workflow_id="workflow",
                        run_id="failed-run",
                        attempt=1,
                        required_check_version="parent-v1",
                        checks={"unit": "failure"},
                        conclusion="failure",
                        classification="conclusive",
                        observed_at=26.0,
                    )
                )
    request.dry_run = False
    with pytest.raises(ValueError):
        await recovery.run(request, principal="supervisor")
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == gaps[0][0]
    assert await db.get_task(f"verify-{request.operation_id}") is None
    async with db._engine.connect() as conn:
        stages = (await conn.execute(select(integration_repair_stages))).mappings().all()
        assert all(not (stage["dossier"] or {}).get("parent_head_extensions") for stage in stages)


async def test_finished_collection_rechecks_gap_close_after_publication_proof(
    db, tmp_path, monkeypatch
):
    recovery, _hierarchy, request, _git, gaps = await _finished_collection_case(db, tmp_path)
    prove = recovery._git_proof
    changed = False

    async def change_gap_audit(*args, **kwargs):
        nonlocal changed
        proof = await prove(*args, **kwargs)
        if not changed:
            changed = True
            async with db.immediate() as conn:
                await conn.execute(delete(integration_outbox).where(
                    integration_outbox.c.id == "middle-closed",
                ))
        return proof

    monkeypatch.setattr(recovery, "_git_proof", change_gap_audit)
    request.dry_run = False
    with pytest.raises(ValueError, match="delegate-close audit"):
        await recovery.run(request, principal="supervisor")
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == gaps[0][0]
    assert await db.get_task(f"verify-{request.operation_id}") is None


@pytest.mark.parametrize("case", ["current", "ancestor", "diverged", "dirty", "writer"])
async def test_finished_collection_checks_confirmed_collector_workspace(db, tmp_path, case):
    recovery, _hierarchy, request, git, gaps = await _finished_collection_case(db, tmp_path)
    if case == "writer":
        await db.create_task(Task(
            id="retained-writer", project_id="p", title="Retained writer", description="",
        ))
    async with db.immediate() as conn:
        await conn.execute(insert(workspaces).values(
            id="former-collector", project_id="p", workspace_path=str(tmp_path / "work"),
            source_type="link", enabled=True, created_at=30.0,
            locked_by_task_id="retained-writer" if case == "writer" else None,
        ))
        await conn.execute(update(integration_branch_owners).values(
            confirmed_workspace_id="former-collector",
        ))
    if case == "ancestor":
        git("reset", "--hard", gaps[0][1])
    elif case == "diverged":
        git("commit", "--allow-empty", "-m", "unpublished collector work")
    elif case == "dirty":
        (tmp_path / "work" / "unpublished.txt").write_text("retain me")
    before = await db.get_integration_checkpoint("parent")
    local_head = git("rev-parse", "aq/parent")
    for dry_run in (True, False):
        request.dry_run = dry_run
        if case in {"current", "ancestor"}:
            assert (await recovery.run(request, principal="supervisor"))["outcome"] == (
                "would_recover" if dry_run else "recovered"
            )
        else:
            with pytest.raises(ValueError, match="confirmed parent"):
                await recovery.run(request, principal="supervisor")
            assert await db.get_integration_checkpoint("parent") == before
    assert git("rev-parse", "aq/parent") == local_head


@pytest.mark.parametrize("prior_verifier", [False, True])
async def test_finished_collection_uses_idle_verifier_and_fresh_handoff_after_prior_session(
    db, tmp_path, prior_verifier
):
    from src.database.tables import task_session_attempts
    from src.integration.outbox import enqueue_integration_event

    recovery, _hierarchy, request, _git, _gaps = await _finished_collection_case(db, tmp_path)
    if prior_verifier:
        await db.create_task(Task(
            id="old-verifier", project_id="p", repo_id="repo", branch_name="aq/parent",
            title="Completed verifier", description="", status=TaskStatus.COMPLETED,
        ))
    async with db.immediate() as conn:
        await conn.execute(insert(task_session_attempts).values(
            id="prior-parent-attempt", session_id="prior-parent-session", task_id="parent",
            project_id="p", profile_id="worker", name="old-parent", lifecycle="task",
            harness="codex", provider="fake", state="stopped", work_dir=str(tmp_path),
            started_at=1.0, session_started_at=1.0, ended_at=2.0,
        ))
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "parent",
        ).values(state="integration_ready"))
        await enqueue_integration_event(
            conn, event_id="old-ready", project_id="p", event_type="task.integration_ready",
            dedup_key=f"task.integration_ready:{request.operation_id}:1",
            available_at=30.0, payload={"head_sha": "f" * 40},
        )
        if prior_verifier:
            await conn.execute(update(integration_repair_operations).values(
                verifier_task_id="old-verifier",
            ))
    before = await db.get_integration_checkpoint("parent")
    request.dry_run = False
    if prior_verifier:
        with pytest.raises(ValueError, match="idle matching aggregate verifier"):
            await recovery.run(request, principal="supervisor")
        assert await db.get_integration_checkpoint("parent") == before
        return
    assert (await recovery.run(request, principal="supervisor"))["outcome"] == "recovered"
    async with db._engine.connect() as conn:
        operation = (await conn.execute(select(integration_repair_operations))).mappings().one()
        events = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "task.integration_ready",
        ))).mappings().all()
    assert operation["verifier_task_id"] == f"verify-{request.operation_id}"
    assert (await db.get_task(operation["verifier_task_id"])).status == TaskStatus.PAUSED
    assert len(events) == 2
    fresh = next(event for event in events if event["id"] != "old-ready")
    assert fresh["payload"]["head_sha"] == request.head_sha
    assert fresh["payload"]["next_owner_id"] == operation["verifier_task_id"]


@pytest.mark.parametrize("archived,empty_commits", [(False, False), (True, False), (False, True)])
async def test_post_collection_repair_head_recovery_preserves_receipts_and_requires_fresh_checks(
    db, tmp_path, archived, empty_commits
):
    recovery, hierarchy, request, _git, collected, head = await _parent_repair_case(db, tmp_path)
    if empty_commits:
        await _empty_repair_completion(db)
    async with db.immediate() as conn:
        receipts = (await conn.execute(select(task_delivery_receipts))).mappings().all()
        original_stage = dict(
            (await conn.execute(select(integration_repair_stages))).mappings().one()
        )
        if archived:
            task = dict(
                (await conn.execute(select(tasks).where(tasks.c.id == "repair"))).mappings().one()
            )
            archived_row = {key: value for key, value in task.items() if key in archived_tasks.c}
            await conn.execute(insert(archived_tasks).values(**archived_row, archived_at=30.0))
            await conn.execute(delete(tasks).where(tasks.c.id == "repair"))
        await conn.execute(
            insert(integration_check_evidence).values(
                id="old-check",
                operation_id=request.operation_id,
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha=collected,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="old-run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=2.0,
            )
        )
    assert (await hierarchy.verify_parent("parent", 1, collected, ["old-check"]))[
        "outcome"
    ] == "verified"
    preview = await recovery.run(request, principal="operator")
    assert preview["outcome"] == "would_recover"
    assert "--episode" in preview["apply_command"]
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == collected
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    verifier_id = f"verify-{request.operation_id}"
    assert (await db.get_task(verifier_id)).status == TaskStatus.PAUSED
    async with db._engine.connect() as conn:
        ready_events = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "task.integration_ready",
        ))).mappings().all()
    assert len(ready_events) == 1
    assert ready_events[0]["payload"]["head_sha"] == head
    assert ready_events[0]["payload"]["verifier_task_id"] == verifier_id
    assert ready_events[0]["payload"]["expected_token"] == 3
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["checkpoint_sha"] == head and checkpoint["current_verification_id"] is None
    assert checkpoint["episode_id"] == request.expected_episode_id and checkpoint["generation"] == 1
    assert (await hierarchy.readiness("parent"))["head_sha"] == head
    assert (await hierarchy.verify_parent("parent", 1, head, ["old-check"]))[
        "outcome"
    ] == "invalid_evidence"
    # The recovered head is the current subject and readiness still answers it
    # ready, so the refusal is not a superseded one: nothing binds trusted
    # evidence to this head yet, which is exactly what the fresh run below has
    # to supply. The old certification is never reused either way.
    before_refusal = (await db.get_task("parent")).status
    unverified = await hierarchy.complete_parent("parent", 1, head)
    assert unverified["outcome"] == AWAITING_TRUSTED_VERIFICATION
    assert unverified["reason"] == "verification_not_recorded"
    assert (await db.get_task("parent")).status is before_refusal
    async with db.immediate() as conn:
        assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == receipts
        stage = dict((await conn.execute(select(integration_repair_stages))).mappings().one())
        assert {k: v for k, v in stage.items() if k != "dossier"} == {
            k: v for k, v in original_stage.items() if k != "dossier"
        }
        await conn.execute(
            insert(integration_check_evidence).values(
                id="fresh-check",
                operation_id=request.operation_id,
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha=head,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="fresh-run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=40.0,
            )
        )
    assert (await hierarchy.verify_parent("parent", 1, head, ["fresh-check"]))[
        "outcome"
    ] == "verified"
    verified = await db.get_integration_checkpoint("parent")
    assert (await recovery.run(request, principal="operator"))["outcome"] == "already_recovered"
    assert await db.get_integration_checkpoint("parent") == verified
    if empty_commits:
        assert (await db.get_task_completions("repair"))[0].commits == []


async def test_empty_repair_recovery_enables_normal_fenced_verifier_handoff(db, tmp_path):
    recovery, hierarchy, request, _git, _collected, head = await _parent_repair_case(db, tmp_path)
    await _empty_repair_completion(db)
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    verifier_id = f"verify-{request.operation_id}"
    transferred = await hierarchy.ownership.transfer(
        Fence(target=BranchKey(repository_id="repo", branch="aq/parent"),
              owner_id=request.operation_id, token=3),
        verifier_id, "verifier",
    )
    assert (await hierarchy.wake_verifier("parent", transferred))["outcome"] == "woken"
    assert (await db.get_task(verifier_id)).status == TaskStatus.READY
    request.expected_fence_token = transferred.token
    assert (await recovery.run(request, principal="operator"))["outcome"] == "already_recovered"
    async with db._engine.connect() as conn:
        stage = (await conn.execute(select(integration_repair_stages))).mappings().one()
        operation = (await conn.execute(select(integration_repair_operations))).mappings().one()
        events = (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "task.integration_ready",
        ))).mappings().all()
    assert stage["state"] == "active" and stage["attempts"] == 1 and stage["deadline_at"] == 10.0
    assert operation["state"] == "escalated" and operation["active_stage"] == 0
    assert len(events) == 1
    assert stage["dossier"]["parent_head_extensions"][0]["authoring"]["accepted_close"][
        "completion_id"
    ] == "repair-completion"
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == head


async def test_repair_recovery_preserves_an_existing_verifier(db, tmp_path):
    recovery, _hierarchy, request, _git, _collected, head = await _parent_repair_case(db, tmp_path)
    await _empty_repair_completion(db)
    await db.create_task(Task(
        id="existing-verifier", project_id="p", title="Verify", description="",
        status=TaskStatus.PAUSED, repo_id="repo", branch_name="aq/parent",
    ))
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_operations).values(
            verifier_task_id="existing-verifier",
        ))
        original = (await conn.execute(select(tasks).order_by(tasks.c.id))).mappings().all()
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    assert (await recovery.run(request, principal="operator"))["outcome"] == "already_recovered"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(tasks).order_by(tasks.c.id))).mappings().all() == original
        assert not (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "task.integration_ready",
        ))).all()
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == head


async def _empty_repair_completion(db):
    from src.database.queries.result_queries import close_identity
    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
    from src.database.tables import task_completion_records

    await db.set_task_meta(
        "repair", ACCEPTED_CLOSE_KEY,
        close_identity("repair-completion", session_id="former-repair-session", claim_epoch=0),
    )
    async with db.immediate() as conn:
        await conn.execute(update(task_completion_records).values(commits="[]"))
        await conn.execute(update(integration_repair_operations).values(state="escalated"))
        await conn.execute(update(integration_repair_stages).values(state="active"))


async def test_empty_repair_recovery_rechecks_accepted_close_after_git_proof(
    db, tmp_path, monkeypatch
):
    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY

    recovery, _hierarchy, request, _git, collected, _head = await _parent_repair_case(db, tmp_path)
    await _empty_repair_completion(db)
    prove = recovery._git_proof

    async def changed_close(*args):
        proof = await prove(*args)
        identity = await db.get_task_meta("repair", ACCEPTED_CLOSE_KEY)
        identity["session_id"] = "replacement-session"
        await db.set_task_meta("repair", ACCEPTED_CLOSE_KEY, identity)
        return proof

    monkeypatch.setattr(recovery, "_git_proof", changed_close)
    request.dry_run = False
    with pytest.raises(ValueError, match="accepted-close identity"):
        await recovery.run(request, principal="operator")
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == collected
    async with db._engine.connect() as conn:
        stage = (await conn.execute(select(integration_repair_stages))).mappings().one()
    assert "parent_head_extensions" not in stage["dossier"]


async def test_repair_recovery_rolls_back_when_verifier_readiness_cannot_be_projected(db, tmp_path):
    recovery, _hierarchy, request, _git, _collected, _head = await _parent_repair_case(db, tmp_path)
    await _empty_repair_completion(db)
    async with db.immediate() as conn:
        operation = (await conn.execute(select(integration_repair_operations))).mappings().one()
        policy = dict(operation["policy_snapshot"])
        policy["parent"] = dict(policy["parent"]) | {"verifier_intelligence_class": None}
        await conn.execute(update(integration_repair_operations).values(policy_snapshot=policy))
        stage = dict((await conn.execute(select(integration_repair_stages))).mappings().one())
    checkpoint = await db.get_integration_checkpoint("parent")
    request.dry_run = False
    with pytest.raises(ValueError, match="cannot project verifier readiness"):
        await recovery.run(request, principal="operator")
    assert await db.get_integration_checkpoint("parent") == checkpoint
    assert await db.get_task(f"verify-{request.operation_id}") is None
    async with db._engine.connect() as conn:
        current = (await conn.execute(select(integration_repair_stages))).mappings().one()
        assert dict(current) == stage
        assert not (await conn.execute(select(integration_outbox).where(
            integration_outbox.c.event_type == "task.integration_ready",
        ))).all()


@pytest.mark.parametrize("case", [
    "missing_identity", "different_completion", "different_session", "different_epoch",
    "malformed_identity", "contradictory_commits", "malformed_commits", "missing_close_audit",
    "missing_lineage", "changed_remote", "different_stage_head", "missing_stage_subject",
    "different_stage_generation", "different_dossier_head", "missing_dossier_head",
    "different_audit_stage",
])
async def test_empty_repair_completion_requires_exact_close_and_publication_proof(
    db, tmp_path, case
):
    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
    from src.database.tables import task_completion_records, task_metadata

    recovery, _hierarchy, request, git, collected, head = await _parent_repair_case(db, tmp_path)
    await _empty_repair_completion(db)
    identity = await db.get_task_meta("repair", ACCEPTED_CLOSE_KEY)
    if case == "different_completion":
        identity["completion_id"] = "another-close"
    elif case == "different_session":
        identity["session_id"] = "another-session"
    elif case == "different_epoch":
        identity["claim_epoch"] = 1
    elif case == "malformed_identity":
        identity = []
    await db.set_task_meta("repair", ACCEPTED_CLOSE_KEY, identity)
    async with db.immediate() as conn:
        if case == "missing_identity":
            await conn.execute(
                delete(task_metadata).where(task_metadata.c.key == ACCEPTED_CLOSE_KEY)
            )
        elif case in {"contradictory_commits", "malformed_commits"}:
            await conn.execute(update(task_completion_records).values(
                commits=json.dumps([collected]) if case == "contradictory_commits" else "{",
            ))
        elif case == "missing_close_audit":
            await conn.execute(
                delete(integration_outbox).where(integration_outbox.c.id == "repair-closed")
            )
        elif case == "missing_lineage":
            await conn.execute(update(integration_repair_stages).values(
                dossier={"branch_sha": head, "repair_commits": []},
            ))
        elif case in {"different_stage_head", "missing_stage_subject", "different_stage_generation"}:
            subject = {"kind": "parent", "generation": 1, "head_sha": head}
            if case == "different_stage_head":
                subject["head_sha"] = collected
            elif case == "different_stage_generation":
                subject["generation"] = 2
            else:
                subject = None
            await conn.execute(update(integration_repair_stages).values(current_subject=subject))
        elif case in {"different_dossier_head", "missing_dossier_head"}:
            dossier = {"repair_commits": [head]}
            if case == "different_dossier_head":
                dossier["branch_sha"] = collected
            await conn.execute(update(integration_repair_stages).values(dossier=dossier))
        elif case == "different_audit_stage":
            event = (await conn.execute(select(integration_outbox).where(
                integration_outbox.c.id == "repair-closed",
            ))).mappings().one()
            await conn.execute(update(integration_outbox).where(
                integration_outbox.c.id == "repair-closed",
            ).values(payload=dict(event["payload"]) | {"stage": 1}))
        receipts = (await conn.execute(select(task_delivery_receipts))).mappings().all()
        stage = dict((await conn.execute(select(integration_repair_stages))).mappings().one())
    if case == "changed_remote":
        git("push", "origin", f"{collected}:refs/heads/aq/parent", "--force")
    for dry_run in (True, False):
        request.dry_run = dry_run
        with pytest.raises(ValueError):
            await recovery.run(request, principal="operator")
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == collected
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == receipts
        current = (await conn.execute(select(integration_repair_stages))).mappings().one()
        assert dict(current) == stage


async def test_attached_repair_close_returns_the_proven_writer_head(
    db, tmp_path, orchestrator_factory
):
    from unittest.mock import AsyncMock

    from src.git.manager import GitManager
    from src.models import SessionRecord

    _recovery, _hierarchy, _request, _git, collected, head = await _parent_repair_case(db, tmp_path)
    await db.create_profile(AgentProfile(id="repairer", name="Repairer"))
    await db.create_session(SessionRecord(
        id="repair-session", task_id="repair", project_id="p", profile_id="repairer",
        harness="fake", provider="fake", name="repair-session", lifecycle="task",
        state="running", work_dir=str(tmp_path / "work"), epoch="epoch",
        instance_token="instance", started_at=2.0,
    ))
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "repair").values(status="IN_PROGRESS"))
        await conn.execute(insert(workspaces).values(
            id="repair-workspace", project_id="p", workspace_path=str(tmp_path / "work"),
            source_type="link", locked_by_task_id="repair", enabled=True, created_at=2.0,
        ))
        await conn.execute(update(integration_branch_owners).values(
            owner_id="repair", owner_role="repair", session_id="repair-session",
            workspace_id="repair-workspace", handoff_state="attached",
        ))
        await conn.execute(update(integration_repair_stages).values(
            current_subject={"kind": "parent", "generation": 1, "head_sha": collected},
            dossier={},
        ))
        await conn.execute(
            delete(integration_outbox).where(integration_outbox.c.id == "repair-closed")
        )
    orchestrator = await orchestrator_factory()
    orchestrator.db = db
    orchestrator.git = GitManager()
    orchestrator._get_default_branch = AsyncMock(return_value="main")
    orchestrator.arelease_integration_writer_for_retry = AsyncMock(return_value=True)
    result = await orchestrator.complete_session_task(
        await db.get_task("repair"), outcome="pass", pool=True, session_live=True,
        session_id="repair-session",
    )
    assert result["status"] == "COMPLETED" and result["pipeline_ok"] is True
    assert result["completion_source"] == head
    assert (await db.get_task("repair")).status == TaskStatus.COMPLETED
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == head


@pytest.mark.parametrize(
    "case",
    [
        "stale_remote",
        "unrelated",
        "unaudited",
        "wrong_episode",
        "wrong_generation",
        "wrong_fence",
        "human",
        "gate",
        "writer",
        "missing_close_proof",
    ],
)
async def test_parent_head_recovery_rejects_unproven_or_held_heads(db, tmp_path, case):
    recovery, _hierarchy, request, git, collected, head = await _parent_repair_case(db, tmp_path)
    request.dry_run = False
    if case == "stale_remote":
        git("push", "origin", f"{collected}:refs/heads/aq/parent", "--force")
    elif case == "unrelated":
        git("switch", "--orphan", "unrelated")
        git("commit", "--allow-empty", "-m", "unrelated")
        request.head_sha = git("rev-parse", "HEAD")
        git("push", "origin", "HEAD:refs/heads/aq/parent", "--force")
        async with db.immediate() as conn:
            from src.database.tables import task_completion_records

            await conn.execute(
                update(integration_repair_stages).values(
                    current_subject={
                        "kind": "parent",
                        "generation": 1,
                        "head_sha": request.head_sha,
                    },
                    dossier={"branch_sha": request.head_sha, "repair_commits": [request.head_sha]},
                )
            )
            await conn.execute(
                update(task_completion_records).values(commits=json.dumps([request.head_sha]))
            )
    elif case == "unaudited":
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_repair_stages).values(
                    dossier={"branch_sha": head, "repair_commits": []}
                )
            )
    elif case == "wrong_episode":
        request.expected_episode_id = "another-episode"
    elif case == "wrong_generation":
        request.expected_generation = 2
    elif case == "wrong_fence":
        request.expected_fence_token = 2
    elif case == "human":
        async with db.immediate() as conn:
            await conn.execute(update(integration_repair_operations).values(state="human_required"))
    elif case == "gate":
        async with db.immediate() as conn:
            await conn.execute(
                insert(gates).values(
                    id="human-gate",
                    project_id="p",
                    gate_type="human",
                    title="Decision",
                    status="open",
                    created_at=5.0,
                )
            )
            await conn.execute(insert(task_gates).values(task_id="parent", gate_id="human-gate"))
    elif case == "writer":
        async with db.immediate() as conn:
            await conn.execute(update(integration_branch_owners).values(handoff_state="attached"))
    elif case == "missing_close_proof":
        async with db.immediate() as conn:
            await conn.execute(
                delete(integration_outbox).where(integration_outbox.c.id == "repair-closed")
            )
    if case.startswith("wrong_"):
        assert (await recovery.run(request, principal="operator"))["outcome"] == "changed"
    else:
        with pytest.raises(ValueError):
            await recovery.run(request, principal="operator")
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == collected


async def test_normal_parent_repair_binding_propagates_the_fenced_head(db, tmp_path):
    from src.integration.repair import RepairService
    from src.models import SessionRecord

    _recovery, hierarchy, request, _git, collected, head = await _parent_repair_case(db, tmp_path)
    await db.create_profile(AgentProfile(id="repairer", name="Repairer"))
    await db.create_session(
        SessionRecord(
            id="repair-session",
            task_id="repair",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="repair-session",
            lifecycle="task",
            state="running",
            work_dir=str(tmp_path),
            epoch="epoch",
            instance_token="instance",
            started_at=2.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "repair").values(status="IN_PROGRESS"))
        await conn.execute(
            insert(workspaces).values(
                id="repair-workspace",
                project_id="p",
                workspace_path=str(tmp_path),
                source_type="link",
                locked_by_task_id="repair",
                enabled=True,
                created_at=2.0,
            )
        )
        await conn.execute(
            update(integration_branch_owners).values(
                owner_id="repair",
                owner_role="repair",
                session_id="repair-session",
                workspace_id="repair-workspace",
                handoff_state="attached",
            )
        )
        await conn.execute(
            update(integration_repair_stages).values(
                current_subject={"kind": "parent", "generation": 1, "head_sha": collected},
                dossier={},
            )
        )
        result = await RepairService(db).bind_current_parent_subject_on(
            conn,
            request.operation_id,
            head_sha=head,
            commit_proof={"base_sha": collected, "head_sha": head, "commits": [head]},
        )
    assert result["changed"] is True
    assert (await hierarchy.readiness("parent"))["head_sha"] == head
    assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == head
    async with db.immediate() as conn:
        replay = await RepairService(db).bind_current_parent_subject_on(
            conn,
            request.operation_id,
            head_sha=head,
            commit_proof={"base_sha": collected, "head_sha": head, "commits": [head]},
        )
        assert replay["changed"] is False
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "parent",
        ).values(generation=2))
        generation_only = await RepairService(db).bind_current_parent_subject_on(
            conn, request.operation_id, head_sha=head,
            commit_proof={"base_sha": head, "head_sha": head, "commits": []},
        )
        assert generation_only["changed"] is True
    readiness = await hierarchy.readiness("parent")
    assert readiness["outcome"] == "ready" and readiness["head_sha"] == head


def _resolution_evidence(
    *, operation_id, before_sha, head_sha, tree_sha, commits, stage_ordinal=0, token=2
):
    """The audited resolution proof shape ``_trusted_code_receipt`` accepts."""
    return {
        "kind": "conflict_resolution",
        "original_source_base": "a" * 40,
        "original_source_head": "b" * 40,
        "original_source_tree": "c" * 40,
        "original_expected_target": before_sha,
        "resolved_head_sha": head_sha,
        "resolved_tree_sha": tree_sha,
        "repair_commit_shas": list(commits),
        "authoring": {
            "operation_id": operation_id,
            "stage_ordinal": stage_ordinal,
            "repair_task_id": "repair",
            "repair_session_id": "repair-session",
            "repair_session_instance_token": "instance",
            "repair_workspace_id": "repair-workspace",
            "fence": {
                "repository_id": "repo",
                "branch": "aq/parent",
                "owner_id": "repair",
                "token": token,
            },
        },
        "remote_proof": {
            "kind": "exact_resolution_tip",
            "remote_sha": head_sha,
            "resolved_tree_sha": tree_sha,
            "repair_commit_shas": list(commits),
        },
    }


async def _record_parent_resolution_on(
    db,
    *,
    operation_id,
    child_id,
    before_sha,
    head_sha,
    tree_sha,
    defect=None,
):
    """Finalize one reconciled resolution: committed intent plus its code receipt."""
    evidence = _resolution_evidence(
        operation_id=operation_id,
        before_sha=before_sha,
        head_sha=head_sha,
        tree_sha=tree_sha,
        commits=[head_sha],
    )
    overrides = {}
    intents = []
    if defect == "untrusted_proof":
        evidence = {**evidence, "remote_proof": {"kind": "exact_resolution_tip"}}
    elif defect == "foreign_episode":
        overrides["parent_episode_id"] = "another-episode"
    elif defect == "other_range":
        before_sha, head_sha = "1" * 40, "2" * 40
        evidence = _resolution_evidence(
            operation_id=operation_id,
            before_sha=before_sha,
            head_sha=head_sha,
            tree_sha=tree_sha,
            commits=[head_sha],
        )
    elif defect == "ambiguous_intents":
        intents.append("rival-resolution")
    if defect == "foreign_episode":
        # The episode column is foreign-keyed, so the rival episode is real:
        # this receipt proves a range in a collection that is not this one.
        async with db.immediate() as conn:
            episode = dict(
                (await conn.execute(select(integration_parent_episodes))).mappings().one()
            )
            await conn.execute(
                insert(integration_parent_episodes).values(
                    **(episode | {"id": "another-episode"})
                )
            )
    await _code_receipt(
        db,
        child_id,
        before_sha,
        head_sha,
        squash_sha=None,
        review_evidence={"review": {"source_base": "a" * 40}},
        resolution_evidence=evidence,
        **overrides,
    )
    async with db.immediate() as conn:
        for intent_id in [f"resolution-{child_id}", *intents]:
            await conn.execute(
                insert(integration_promotion_intents).values(
                    id=intent_id,
                    domain_key=intent_id,
                    operation_key=operation_id,
                    project_id="p",
                    receipt_id=(
                        f"receipt-{child_id}"
                        if intent_id == f"resolution-{child_id}"
                        else "receipt-rival"
                    ),
                    source_task_id=child_id,
                    target_task_id="parent",
                    source_head="b" * 40,
                    source_base="a" * 40,
                    repository_id="repo",
                    target_branch="aq/parent",
                    expected_target=before_sha,
                    fence_owner_id=operation_id,
                    fence_token=5,
                    state="committed",
                    resolution_head_sha=head_sha,
                    resolution_tree_sha=tree_sha,
                    resolution_commit_shas=[head_sha],
                    resolution_operation_id=operation_id,
                    resolution_stage_ordinal=0,
                    resolution_task_id="repair",
                    resolution_session_id="repair-session",
                    resolution_session_instance_token="instance",
                    resolution_workspace_id="repair-workspace",
                    resolution_fence_owner_id="repair",
                    resolution_fence_token=2,
                    resolution_push_started_at=29.0,
                    resolution_push_evidence={
                        "kind": "exact_resolution_push_observed",
                        "remote_sha": head_sha,
                    },
                    remote_evidence=evidence["remote_proof"],
                    committed_at=30.0,
                    created_at=28.0,
                    updated_at=30.0,
                )
            )


@pytest.mark.parametrize(
    "defect",
    [None, "untrusted_proof", "foreign_episode", "other_range", "ambiguous_intents"],
)
async def test_committed_resolution_receipt_suppresses_the_duplicate_head_edge(
    db, tmp_path, defect
):
    """A reconciled resolution owns its range; the close must not re-prove it.

    The writer's push finalizes the resolution receipt before its own close
    reaches the subject bind, so the pending-intent guard no longer holds and
    the bind appends a second proof of a range readiness has already consumed.
    The chain walk then strands on ``repair_head_chain`` and the parent waits
    forever. Only a fully proved receipt may stand in for the edge: every
    defect below still records it, because a receipt that proves nothing must
    not quietly drop the writer's audited head movement.
    """
    from src.integration.parent_repair_heads import EXTENSIONS
    from src.integration.repair import RepairService
    from src.models import SessionRecord

    _recovery, hierarchy, request, git, collected, head = await _parent_repair_case(
        db, tmp_path, children=2
    )
    tree = git("rev-parse", f"{head}^{{tree}}")
    async with db._engine.connect() as conn:
        resolving_child = (
            await conn.execute(
                select(tasks.c.id)
                .where(tasks.c.parent_task_id == "parent")
                .order_by(tasks.c.id)
            )
        ).scalars().all()[1]
    await db.create_profile(AgentProfile(id="repairer", name="Repairer"))
    await db.create_session(
        SessionRecord(
            id="repair-session",
            task_id="repair",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="repair-session",
            lifecycle="task",
            state="running",
            work_dir=str(tmp_path),
            epoch="epoch",
            instance_token="instance",
            started_at=2.0,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "repair").values(status="IN_PROGRESS"))
        await conn.execute(
            insert(workspaces).values(
                id="repair-workspace",
                project_id="p",
                workspace_path=str(tmp_path),
                source_type="link",
                locked_by_task_id="repair",
                enabled=True,
                created_at=2.0,
            )
        )
        await conn.execute(
            update(integration_branch_owners).values(
                owner_id="repair",
                owner_role="repair",
                session_id="repair-session",
                workspace_id="repair-workspace",
                handoff_state="attached",
            )
        )
        await conn.execute(
            update(integration_repair_stages).values(
                current_subject={"kind": "parent", "generation": 1, "head_sha": collected},
                dossier={},
            )
        )
    await _record_parent_resolution_on(
        db,
        operation_id=request.operation_id,
        child_id=resolving_child,
        before_sha=collected,
        head_sha=head,
        tree_sha=tree,
        defect=defect,
    )
    # The receipts alone already prove the aggregate head before the close;
    # a receipt this parent cannot fold proves nothing, which is exactly why
    # its close keeps the edge. Rival intents are invisible to readiness.
    before = await hierarchy.readiness("parent")
    if defect in {None, "ambiguous_intents"}:
        assert before["outcome"] == "ready" and before["head_sha"] == head
    else:
        assert before["outcome"] == "waiting"
    async with db.immediate() as conn:
        bound = await RepairService(db).bind_current_parent_subject_on(
            conn,
            request.operation_id,
            head_sha=head,
            commit_proof={"base_sha": collected, "head_sha": head, "commits": [head]},
        )
    assert bound["changed"] is True
    async with db._engine.connect() as conn:
        stage = (await conn.execute(select(integration_repair_stages))).mappings().one()
    dossier = stage["dossier"]
    assert dossier["repair_commits"] == [head]
    assert stage["current_subject"]["head_sha"] == head
    if defect is None:
        assert dossier.get(EXTENSIONS, []) == []
        assert dossier["receipt_covered_head"] == {
            "intent_id": f"resolution-{resolving_child}",
            "receipt_id": f"receipt-{resolving_child}",
            "source_task_id": resolving_child,
            "before_sha": collected,
            "after_sha": head,
            "committed_at": 30.0,
        }
        assert [item["id"] for item in dossier["receipts"]] == [
            f"receipt-{child}" for child in sorted({resolving_child, "parent.1"})
        ]
        # The receipt owns the head, so the checkpoint still advances at the
        # verifier handoff exactly as it does for an ordinary delivery.
        checkpoint = await db.get_integration_checkpoint("parent")
        assert checkpoint["checkpoint_sha"] == collected
        readiness = await hierarchy.readiness("parent")
        assert readiness["outcome"] == "ready" and readiness["head_sha"] == head
        assert readiness["blockers"] == []
        # The reported symptom end to end: the closed writer's stage settles on
        # its recorded resolution and the parent projects verification at that
        # head instead of waiting forever.
        async with db.immediate() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == "repair").values(status="COMPLETED")
            )
        service = RepairService(db, clock=lambda: 40.0)
        settled = await service.dispatch(request.operation_id, 0)
        assert settled["reason"] == "resolution_recorded_for_subject"
        assert settled["head_sha"] == head
        async with db._engine.connect() as conn:
            settled_stage = (
                await conn.execute(select(integration_repair_stages))
            ).mappings().one()
            ready = (
                (
                    await conn.execute(
                        select(integration_outbox.c.payload).where(
                            integration_outbox.c.event_type == "task.integration_ready",
                            integration_outbox.c.payload["head_sha"].as_string() == head,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert settled_stage["state"] == "passed"
        assert settled_stage["dossier"]["resolution_verification"]["intent_id"] == (
            f"resolution-{resolving_child}"
        )
        assert len(ready) == 1
        assert (await db.get_integration_checkpoint("parent"))["state"] == "integration_ready"
    else:
        assert [edge["after_sha"] for edge in dossier[EXTENSIONS]] == [head]
        assert "receipt_covered_head" not in dossier
        assert (await db.get_integration_checkpoint("parent"))["checkpoint_sha"] == head


@pytest.mark.parametrize("mismatch", ["episode_id", "generation", "operation_id"])
async def test_readiness_rejects_repair_edges_from_another_identity(db, tmp_path, mismatch):
    from src.integration.parent_repair_heads import EXTENSIONS

    recovery, hierarchy, request, _git, _collected, _head = await _parent_repair_case(db, tmp_path)
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    async with db.immediate() as conn:
        dossier = dict(
            (await conn.execute(select(integration_repair_stages.c.dossier))).scalar_one()
        )
        dossier[EXTENSIONS][0][mismatch] = 2 if mismatch == "generation" else "another-identity"
        await conn.execute(update(integration_repair_stages).values(dossier=dossier))
    readiness = await hierarchy.readiness("parent")
    assert readiness["outcome"] == "waiting"
    assert {item["reason"] for item in readiness["blockers"]} == {"repair_head_proof"}


async def test_repair_edge_survives_more_children_in_the_same_episode(db, tmp_path):
    recovery, hierarchy, request, _git, _collected, head = await _parent_repair_case(db, tmp_path)
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    filed = await hierarchy.file_children("parent", [{"title": "follow-up"}], 1)
    new_child = filed["children"][0]["task_id"]
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == new_child).values(status="COMPLETED"))
        await conn.execute(
            update(task_integration_checkpoints)
            .where(
                task_integration_checkpoints.c.task_id == new_child,
            )
            .values(checkpoint_sha="b" * 40)
        )
    await _code_receipt(db, new_child, head, "f" * 40)
    readiness = await hierarchy.readiness("parent")
    assert readiness["generation"] == 2
    assert readiness["outcome"] == "ready" and readiness["head_sha"] == "f" * 40


async def _advanced_parent_repair_case(db, tmp_path, *, edge_exists=False, receipt_case=None):
    recovery, hierarchy, request, git, _collected, repair_head = await _parent_repair_case(
        db, tmp_path
    )
    if edge_exists:
        request.dry_run = False
        assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    filed = await hierarchy.file_children("parent", [{"title": "K09 collected after repair"}], 1)
    child = filed["children"][0]["task_id"]
    git("commit", "--allow-empty", "-m", "collect K09")
    if receipt_case == "unaudited_range":
        git("commit", "--allow-empty", "-m", "unreceipted commit")
        git("commit", "--allow-empty", "-m", "collect K09 over unreceipted work")
    elif receipt_case == "non_ancestor":
        git("switch", "--orphan", "unrelated")
        git("commit", "--allow-empty", "-m", "unrelated aggregate")
    head = git("rev-parse", "HEAD")
    git("push", "origin", "HEAD:refs/heads/aq/parent", "--force")
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == child).values(status="COMPLETED"))
        await conn.execute(
            update(task_integration_checkpoints)
            .where(
                task_integration_checkpoints.c.task_id == child,
            )
            .values(checkpoint_sha="b" * 40)
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(
                task_integration_checkpoints.c.task_id == "parent",
            )
            .values(checkpoint_sha=head, state="integration_ready")
        )
        await conn.execute(update(integration_repair_operations).values(state="escalated"))
    receipt_overrides = {}
    if receipt_case == "foreign_receipt":
        async with db.immediate() as conn:
            episode = dict(
                (await conn.execute(select(integration_parent_episodes))).mappings().one()
            )
            await conn.execute(
                insert(integration_parent_episodes).values(
                    **(episode | {"id": "another-episode"}),
                )
            )
        receipt_overrides["parent_episode_id"] = "another-episode"
    repair_head_for_receipt = "f" * 40 if receipt_case == "receipt_gap" else repair_head
    if receipt_case != "missing_receipt":
        await _code_receipt(db, child, repair_head_for_receipt, head, **receipt_overrides)
    request.head_sha, request.expected_generation, request.dry_run = head, 2, True
    return recovery, hierarchy, request, git, child, repair_head, head


@pytest.mark.parametrize("stage_generation", [1, 2])
@pytest.mark.parametrize("edge_exists", [False, True])
async def test_repair_head_advanced_by_receipts_settles_repair_and_reverifies_current_head(
    db,
    tmp_path,
    edge_exists,
    stage_generation,
):
    from src.commands.contracts.integration import IntegrationRecoverParentHeadValue
    from src.integration.outbox import enqueue_integration_event

    (
        recovery,
        hierarchy,
        request,
        _git,
        _child,
        repair_head,
        head,
    ) = await _advanced_parent_repair_case(
        db,
        tmp_path,
        edge_exists=edge_exists,
    )
    async with db.immediate() as conn:
        await conn.execute(update(integration_repair_stages).values(
            current_subject={"kind": "parent", "generation": stage_generation, "head_sha": repair_head},
        ))
        await enqueue_integration_event(
            conn, event_id=f"parent-ready-{request.operation_id}-2",
            dedup_key=f"task.integration_ready:{request.operation_id}:2",
            project_id="p", event_type="task.integration_ready", available_at=30.0,
            payload={"head_sha": repair_head},
        )
    async with db._engine.connect() as conn:
        receipts = (await conn.execute(select(task_delivery_receipts))).mappings().all()
        original = dict((await conn.execute(select(integration_repair_stages))).mappings().one())
        operation = dict(
            (await conn.execute(select(integration_repair_operations))).mappings().one()
        )
    before = await db.get_integration_checkpoint("parent")
    preview = await recovery.run(request, principal="operator")
    IntegrationRecoverParentHeadValue.model_validate(
        {key: value for key, value in preview.items() if key != "outcome"},
    )
    assert preview["outcome"] == "would_recover"
    assert preview["repair_head_sha"] == repair_head
    assert len(preview["collection_receipt_ids"]) == 1
    assert await db.get_integration_checkpoint("parent") == before
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    after = await db.get_integration_checkpoint("parent")
    assert after["checkpoint_sha"] == head and after["current_verification_id"] is None
    assert (await hierarchy.readiness("parent"))["head_sha"] == head
    async with db.immediate() as conn:
        stage = (await conn.execute(select(integration_repair_stages))).mappings().one()
        assert stage["state"] == "passed"
        assert stage["current_subject"]["head_sha"] == repair_head
        assert stage["dossier"]["parent_head_extensions"][0]["after_sha"] == repair_head
        assert len(stage["dossier"]["parent_head_extensions"]) == 1
        for key in ("attempts", "deadline_at", "starting_sha", "policy", "deadline_event_id"):
            assert stage[key] == original[key]
        assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == receipts
        current = (await conn.execute(select(integration_repair_operations))).mappings().one()
        assert current["state"] == "active"
        assert current["episode_id"] == operation["episode_id"]
        events = (
            (
                await conn.execute(
                    select(integration_outbox).where(
                        integration_outbox.c.event_type == "task.integration_ready",
                        integration_outbox.c.payload["head_sha"].as_string() == head,
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(events) == 1
        await conn.execute(
            insert(integration_check_evidence).values(
                id="fresh-advanced-check",
                operation_id=request.operation_id,
                parent_task_id="parent",
                parent_generation=2,
                parent_head_sha=head,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="new-run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=40.0,
            )
        )
    ready_event = events[0]["payload"]
    target = BranchKey(repository_id="repo", branch="aq/parent")
    transferred = await BranchOwnership(db).transfer(
        Fence(target=target, owner_id=request.operation_id, token=request.expected_fence_token),
        ready_event["next_owner_id"], "verifier",
    )
    assert (await hierarchy.wake_verifier("parent", transferred))["outcome"] == "woken"
    assert (await db.get_task(ready_event["next_owner_id"])).status == TaskStatus.READY
    request.expected_fence_token = transferred.token
    assert (await hierarchy.complete_parent("parent", 2, head))[
        "outcome"
    ] == AWAITING_TRUSTED_VERIFICATION
    assert (await hierarchy.verify_parent("parent", 2, head, ["fresh-advanced-check"]))[
        "outcome"
    ] == "verified"
    verified = await db.get_integration_checkpoint("parent")
    assert (await recovery.run(request, principal="operator"))["outcome"] == "already_recovered"
    assert await db.get_integration_checkpoint("parent") == verified


async def test_advanced_repair_files_fresh_verifier_for_parent_with_a_prior_session(db, tmp_path):
    from src.database.tables import task_session_attempts

    recovery, _hierarchy, request, _git, _child, _repair_head, head = (
        await _advanced_parent_repair_case(db, tmp_path)
    )
    async with db.immediate() as conn:
        await conn.execute(insert(task_session_attempts).values(
            id="prior-parent-attempt", session_id="prior-parent-session", task_id="parent",
            project_id="p", profile_id="worker", name="old-parent", lifecycle="task",
            harness="codex", provider="fake", state="stopped", work_dir=str(tmp_path),
            started_at=1.0, session_started_at=1.0, ended_at=2.0,
        ))
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    async with db._engine.connect() as conn:
        operation = (
            await conn.execute(select(integration_repair_operations))
        ).mappings().one()
        event = (
            await conn.execute(select(integration_outbox.c.payload).where(
                integration_outbox.c.event_type == "task.integration_ready",
                integration_outbox.c.payload["head_sha"].as_string() == head,
            ))
        ).scalar_one()
    verifier_id = operation["verifier_task_id"]
    assert verifier_id and verifier_id != "parent"
    assert event["next_owner_id"] == verifier_id
    assert (await db.get_task(verifier_id)).status == TaskStatus.PAUSED


@pytest.mark.parametrize(
    "case",
    [
        "undelivered_child",
        "missing_receipt",
        "foreign_receipt",
        "receipt_gap",
        "unaudited_range",
        "non_ancestor",
        "holder",
        "missing_close_audit",
        "future_generation",
        "completed_verifier",
    ],
)
async def test_advanced_repair_recovery_refuses_gaps_and_live_holders(db, tmp_path, case):
    from src.models import SessionRecord

    (
        recovery,
        _hierarchy,
        request,
        _git,
        child,
        repair_head,
        _head,
    ) = await _advanced_parent_repair_case(
        db,
        tmp_path,
        receipt_case=case,
    )
    if case == "holder":
        await db.create_session(
            SessionRecord(
                id="holder",
                project_id="p",
                task_id="repair",
                profile_id="worker",
                harness="codex",
                provider="fake",
                name="holder",
                lifecycle="pool",
                work_dir=str(tmp_path / "work"),
                epoch="epoch",
                instance_token="token",
                started_at=40.0,
                state="running",
            )
        )
    elif case == "completed_verifier":
        await db.create_task(
            Task(
                id="old-verifier",
                project_id="p",
                repo_id="repo",
                branch_name="aq/parent",
                title="Old completed verifier",
                description="",
                status=TaskStatus.COMPLETED,
            )
        )
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_repair_operations).values(
                    verifier_task_id="old-verifier",
                )
            )
    else:
        async with db.immediate() as conn:
            if case == "undelivered_child":
                await conn.execute(update(tasks).where(tasks.c.id == child).values(status="READY"))
            elif case == "missing_close_audit":
                await conn.execute(
                    delete(integration_outbox).where(integration_outbox.c.id == "repair-closed")
                )
            elif case == "future_generation":
                await conn.execute(
                    update(integration_repair_stages).values(
                        current_subject={
                            "kind": "parent",
                            "generation": 3,
                            "head_sha": repair_head,
                        },
                    )
                )
    checkpoint = await db.get_integration_checkpoint("parent")
    async with db._engine.connect() as conn:
        stage = dict((await conn.execute(select(integration_repair_stages))).mappings().one())
    for dry_run in (True, False):
        request.dry_run = dry_run
        with pytest.raises(ValueError):
            await recovery.run(request, principal="operator")
    assert await db.get_integration_checkpoint("parent") == checkpoint
    async with db._engine.connect() as conn:
        assert (
            dict((await conn.execute(select(integration_repair_stages))).mappings().one()) == stage
        )


@pytest.mark.parametrize("case", ["current", "ancestor", "diverged", "dirty", "writer"])
async def test_advanced_repair_recovery_checks_confirmed_collector_workspace(db, tmp_path, case):
    (
        recovery,
        _hierarchy,
        request,
        git,
        _child,
        repair_head,
        _head,
    ) = await _advanced_parent_repair_case(
        db,
        tmp_path,
    )
    if case == "writer":
        await db.create_task(Task(
            id="unrelated-writer", project_id="p", title="Retained writer", description="",
        ))
    async with db.immediate() as conn:
        await conn.execute(
            insert(workspaces).values(
                id="former-slot",
                project_id="p",
                workspace_path=str(tmp_path / "work"),
                source_type="link",
                enabled=True,
                created_at=30.0,
                locked_by_task_id="unrelated-writer" if case == "writer" else None,
            )
        )
        await conn.execute(
            update(integration_branch_owners).values(
                confirmed_workspace_id="former-slot",
            )
        )
    if case == "ancestor":
        git("reset", "--hard", repair_head)
    elif case == "diverged":
        git("commit", "--allow-empty", "-m", "unpublished collector work")
    elif case == "dirty":
        (tmp_path / "work" / "unpublished.txt").write_text("retain me")
    before = await db.get_integration_checkpoint("parent")
    if case in {"current", "ancestor"}:
        assert (await recovery.run(request, principal="operator"))["outcome"] == "would_recover"
        assert await db.get_integration_checkpoint("parent") == before
        request.dry_run = False
        assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
        assert git("rev-parse", "aq/parent") == (
            repair_head if case == "ancestor" else request.head_sha
        )
    else:
        for dry_run in (True, False):
            request.dry_run = dry_run
            with pytest.raises(ValueError, match="confirmed parent"):
                await recovery.run(request, principal="operator")
        assert await db.get_integration_checkpoint("parent") == before


async def test_advanced_repair_recovery_rechecks_local_changes_before_apply(
    db, tmp_path, monkeypatch
):
    (
        recovery,
        _hierarchy,
        request,
        _git,
        _child,
        _repair_head,
        _head,
    ) = await _advanced_parent_repair_case(
        db,
        tmp_path,
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(workspaces).values(
                id="former-slot",
                project_id="p",
                workspace_path=str(tmp_path / "work"),
                source_type="link",
                enabled=True,
                created_at=30.0,
            )
        )
        await conn.execute(
            update(integration_branch_owners).values(
                confirmed_workspace_id="former-slot",
            )
        )
    before = await db.get_integration_checkpoint("parent")
    prove, calls = recovery._git_proof, 0

    async def change_local(*args):
        nonlocal calls
        result = await prove(*args)
        calls += 1
        if calls == 2:
            (tmp_path / "work" / "unpublished.txt").write_text("arrived during recovery")
        return result

    monkeypatch.setattr(recovery, "_git_proof", change_local)
    request.dry_run = False
    with pytest.raises(ValueError, match="unpublished changes"):
        await recovery.run(request, principal="operator")
    assert await db.get_integration_checkpoint("parent") == before


async def _empty_verification_stage_case(db, tmp_path, *, invalid=None, redundant_edge=True):
    """vivid-quest-44: .3 squash, stage-0 .1 resolution, empty stage 1, .2 squash."""
    from src.database.queries.result_queries import close_identity
    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
    from src.integration.outbox import enqueue_integration_event
    from src.integration.parent_repair_heads import EXTENSIONS, extension

    recovery, hierarchy, request, git, collected, repair_head = await _parent_repair_case(
        db,
        tmp_path,
        children=3,
        first_receipt_child=2,
    )
    base = git("rev-parse", "main")
    if invalid == "outside_receipts":
        git("commit", "--allow-empty", "-m", "unreceipted change")
    git("commit", "--allow-empty", "-m", "collect K09 after empty verification")
    head = git("rev-parse", "HEAD")
    git("push", "origin", "aq/parent")
    before = "f" * 40 if invalid == "gap" else collected
    evidence = {
        "kind": "conflict_resolution",
        "original_source_base": "a" * 40,
        "original_source_head": "b" * 40,
        "original_source_tree": "c" * 40,
        "original_expected_target": before,
        "resolved_head_sha": repair_head,
        "resolved_tree_sha": git("rev-parse", repair_head + "^{tree}"),
        "repair_commit_shas": [repair_head],
        "authoring": {
            "operation_id": request.operation_id,
            "stage_ordinal": 0,
            "repair_task_id": "repair",
            "repair_session_id": "former-repair-session",
            "repair_session_instance_token": "former-instance",
            "repair_workspace_id": "former-workspace",
            "fence": {
                "repository_id": "repo",
                "branch": "aq/parent",
                "owner_id": "repair",
                "token": 2,
            },
        },
    }
    evidence["remote_proof"] = {
        "kind": "exact_resolution_tip",
        "remote_sha": repair_head,
        "resolved_tree_sha": evidence["resolved_tree_sha"],
        "repair_commit_shas": [repair_head],
    }
    if invalid == "untrusted_receipt":
        evidence["remote_proof"] = {}
    await _code_receipt(
        db,
        "parent.1",
        before,
        repair_head,
        created_at=4.0,
        squash_sha=None,
        review_evidence={"review": {"source_base": "a" * 40}},
        resolution_evidence=evidence,
    )
    await _code_receipt(db, "parent.2", repair_head, head, created_at=5.0)
    await db.create_task(
        Task(
            id="repair-empty",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="Verification only",
            description="No commits",
            status=TaskStatus.COMPLETED,
            created_by_kind="integration_repair",
            created_by_id=request.operation_id,
        )
    )
    await db.save_task_completion(
        TaskCompletion(
            id="empty-completion",
            task_id="repair-empty",
            outcome="pass",
            branch="aq/parent",
            commits=[],
            completed_at=40.0,
        )
    )
    await db.set_task_meta(
        "repair-empty",
        ACCEPTED_CLOSE_KEY,
        close_identity("empty-completion", session_id="empty-session", claim_epoch=0),
    )
    async with db.immediate() as conn:
        stage = dict((await conn.execute(select(integration_repair_stages))).mappings().one())
        checkpoint = (
            (
                await conn.execute(
                    select(task_integration_checkpoints).where(
                        task_integration_checkpoints.c.task_id == "parent",
                    )
                )
            )
            .mappings()
            .one()
        )
        edge = extension(
            {"id": request.operation_id, "episode_id": request.expected_episode_id},
            checkpoint,
            stage,
            {"base_sha": collected, "head_sha": repair_head, "commits": [repair_head]},
            {
                "task_id": "repair",
                "session_id": "former-repair-session",
                "instance_token": "former-instance",
                "workspace_id": "former-workspace",
                "fence_token": 2,
            },
        )
        await conn.execute(
            update(integration_repair_stages).values(
                state="passed",
                dossier=stage["dossier"] | ({EXTENSIONS: [edge]} if redundant_edge else {}),
            )
        )
        subject = {"kind": "parent", "generation": 1, "head_sha": repair_head}
        incident = {
            "incident_id": f"repair-no-progress:{request.operation_id}:1",
            "stage": 1,
            "subject": subject,
            "attempts": stage["attempts"],
            "deadline_at": stage["deadline_at"],
            "repair_task_id": "repair-empty",
            "recorded_at": 41.0,
        }
        await conn.execute(
            insert(integration_repair_stages).values(
                **(
                    stage
                    | {
                        "ordinal": 1,
                        "starting_sha": repair_head,
                        "repair_task_id": "repair-empty",
                        "current_subject": subject,
                        "deadline_event_id": "empty-stage-deadline",
                        "state": "expired",
                        "completed_at": 41.0,
                        "dossier": {
                            "repair_commits": list(stage["dossier"]["repair_commits"]),
                            "branch_sha": repair_head,
                            "supervisor_recovery": incident,
                        },
                    }
                )
            )
        )
        await conn.execute(
            update(integration_repair_operations).values(state="escalated", active_stage=1)
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(
                task_integration_checkpoints.c.task_id == "parent",
            )
            .values(
                generation=2,
                checkpoint_sha=base,
                state="verifying",
                verified_sha=repair_head,
                verified_generation=1,
            )
        )
        await conn.execute(
            insert(workspaces).values(
                id="confirmed-parent",
                project_id="p",
                workspace_path=str(tmp_path / "work"),
                source_type="link",
                enabled=True,
                created_at=1.0,
            )
        )
        await conn.execute(
            update(integration_branch_owners).values(
                fence_token=8,
                confirmed_workspace_id="confirmed-parent",
            )
        )
        await enqueue_integration_event(
            conn,
            event_id="empty-closed",
            dedup_key="empty-closed",
            project_id="p",
            event_type="integration.repair_delegate_closed",
            available_at=40.0,
            payload={
                "operation_id": request.operation_id,
                "stage": 1,
                "task_id": "repair-empty",
                "session_id": "empty-session",
                "instance_token": "empty-instance",
                "workspace_id": "empty-workspace",
                "fence_token": 6,
            },
        )
        await enqueue_integration_event(
            conn,
            event_id="stale-ready",
            dedup_key=f"task.integration_ready:{request.operation_id}:2",
            project_id="p",
            event_type="task.integration_ready",
            available_at=30.0,
            payload={"head_sha": repair_head},
        )
    request.head_sha, request.expected_generation = head, 2
    request.expected_stage, request.expected_fence_token = 1, 8
    return recovery, hierarchy, request, git, base, repair_head


@pytest.mark.parametrize("redundant_edge", [False, True])
async def test_empty_verification_stage_recovers_receipt_proven_head_and_fresh_verification(
    db,
    tmp_path,
    redundant_edge,
):
    from src.commands.contracts.integration import IntegrationRecoverParentHeadValue
    from src.database.tables import task_session_attempts
    from src.integration.parent_repair_heads import EMPTY_VERIFICATION_RECOVERY, EXTENSIONS

    recovery, hierarchy, request, _git, _base, repair_head = await _empty_verification_stage_case(
        db,
        tmp_path,
        redundant_edge=redundant_edge,
    )
    if redundant_edge:
        readiness = await hierarchy.readiness("parent")
        assert readiness["blockers"] == [{"task_id": "parent", "reason": "repair_head_chain"}]
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_session_attempts).values(
                id="old-parent-attempt",
                session_id="old-parent-session",
                task_id="parent",
                project_id="p",
                profile_id="worker",
                name="old-parent",
                lifecycle="task",
                harness="codex",
                provider="fake",
                state="stopped",
                work_dir=str(tmp_path),
                started_at=1.0,
                session_started_at=1.0,
                ended_at=2.0,
            )
        )
        receipts = (await conn.execute(select(task_delivery_receipts))).mappings().all()
        stages = (
            (
                await conn.execute(
                    select(integration_repair_stages).order_by(
                        integration_repair_stages.c.ordinal,
                    )
                )
            )
            .mappings()
            .all()
        )
    checkpoint = await db.get_integration_checkpoint("parent")
    preview = await recovery.run(request, principal="operator")
    IntegrationRecoverParentHeadValue.model_validate(
        {k: v for k, v in preview.items() if k != "outcome"}
    )
    assert preview["outcome"] == "would_recover" and preview["stage"] == 1
    assert preview["repair_head_sha"] == repair_head
    assert preview["collection_receipt_ids"] == [
        "receipt-parent.3",
        "receipt-parent.1",
        "receipt-parent.2",
    ]
    assert "--fence 8" in preview["apply_command"]
    assert await db.get_integration_checkpoint("parent") == checkpoint
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    current = await db.get_integration_checkpoint("parent")
    assert current["episode_id"] == checkpoint["episode_id"] and current["generation"] == 2
    assert current["checkpoint_sha"] == request.head_sha
    assert current["verified_sha"] is None and current["verified_generation"] is None
    assert current["current_verification_id"] is None
    assert (await hierarchy.readiness("parent"))["outcome"] == "ready"
    async with db.immediate() as conn:
        new_stages = (
            (
                await conn.execute(
                    select(integration_repair_stages).order_by(
                        integration_repair_stages.c.ordinal,
                    )
                )
            )
            .mappings()
            .all()
        )
        assert new_stages[1]["state"] == "passed"
        assert EXTENSIONS not in new_stages[1]["dossier"]
        assert (
            new_stages[1]["dossier"][EMPTY_VERIFICATION_RECOVERY]["completion_id"]
            == "empty-completion"
        )
        assert (
            new_stages[1]["dossier"]["supervisor_recovery"]
            == stages[1]["dossier"]["supervisor_recovery"]
        )
        assert new_stages[0]["dossier"].get(EXTENSIONS) == stages[0]["dossier"].get(EXTENSIONS)
        for old, new in zip(stages, new_stages, strict=True):
            for key in (
                "attempts",
                "deadline_at",
                "starting_sha",
                "current_subject",
                "policy",
                "deadline_event_id",
            ):
                assert new[key] == old[key]
        assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == receipts
        operation = (await conn.execute(select(integration_repair_operations))).mappings().one()
        assert operation["state"] == "active" and operation["active_stage"] == 1
        verifier = (
            (
                await conn.execute(
                    select(tasks).where(
                        tasks.c.id == operation["verifier_task_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        assert verifier["status"] == "PAUSED" and verifier["branch_name"] == "aq/parent"
        events = (
            (
                await conn.execute(
                    select(integration_outbox).where(
                        integration_outbox.c.event_type == "task.integration_ready",
                        integration_outbox.c.payload["head_sha"].as_string() == request.head_sha,
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(events) == 1 and events[0]["dedup_key"].endswith(f":recovery:{request.head_sha}")
        await conn.execute(
            insert(integration_check_evidence).values(
                id="fresh-empty-stage-check",
                operation_id=request.operation_id,
                parent_task_id="parent",
                parent_generation=2,
                parent_head_sha=request.head_sha,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="fresh-after-empty-stage",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=50.0,
            )
        )
    assert (await hierarchy.complete_parent("parent", 2, request.head_sha))[
        "outcome"
    ] == AWAITING_TRUSTED_VERIFICATION
    assert (
        await hierarchy.verify_parent("parent", 2, request.head_sha, ["fresh-empty-stage-check"])
    )["outcome"] == "verified"
    verified = await db.get_integration_checkpoint("parent")
    assert (await recovery.run(request, principal="operator"))["outcome"] == "already_recovered"
    assert await db.get_integration_checkpoint("parent") == verified
    async with db._engine.connect() as conn:
        assert (
            len(
                (
                    await conn.execute(
                        select(integration_outbox).where(
                            integration_outbox.c.event_type == "task.integration_ready",
                            integration_outbox.c.payload["head_sha"].as_string()
                            == request.head_sha,
                        )
                    )
                ).all()
            )
            == 1
        )
    assert (await db.get_task_completions("repair-empty"))[0].commits == []


@pytest.mark.parametrize(
    "case",
    [
        "gap",
        "outside_receipts",
        "untrusted_receipt",
        "recorded_stage_commits",
        "non_pass_completion",
        "missing_incident",
        "wrong_incident",
        "changed_incident_subject",
        "changed_incident_budget",
        "missing_close_audit",
        "wrong_close_fence",
        "missing_accepted_close",
        "different_completion",
        "live_holder",
        "earlier_live_holder",
        "remote_moved",
        "dirty_workspace",
        "stage_changed_head",
        "open_gate",
        "ambiguous_write",
        "completed_verifier",
        "extension_outside_receipts",
        "subject_not_receipt_tip",
        "non_empty_completion",
        "missing_confirmed_workspace",
    ],
)
async def test_empty_verification_stage_refuses_incomplete_or_live_proof(db, tmp_path, case):
    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
    from src.database.tables import task_completion_records, task_metadata
    from src.models import SessionRecord

    recovery, _hierarchy, request, git, _base, repair_head = await _empty_verification_stage_case(
        db,
        tmp_path,
        invalid=case,
    )
    if case in {"live_holder", "earlier_live_holder"}:
        await db.create_profile(AgentProfile(id="repairer", name="Repairer"))
        await db.create_session(
            SessionRecord(
                id="retained-session",
                task_id="repair" if case == "earlier_live_holder" else "repair-empty",
                project_id="p",
                profile_id="repairer",
                harness="fake",
                provider="fake",
                name="retained",
                lifecycle="task",
                state="running",
                work_dir=str(tmp_path / "work"),
                epoch="epoch",
                instance_token="retained-instance",
                started_at=2.0,
            )
        )
    elif case == "remote_moved":
        git("push", "origin", f"{repair_head}:refs/heads/aq/parent", "--force")
    elif case == "dirty_workspace":
        (tmp_path / "work" / "unpublished.txt").write_text("unpublished")
    elif case == "completed_verifier":
        await db.create_task(
            Task(
                id="finished-verifier",
                project_id="p",
                repo_id="repo",
                branch_name="aq/parent",
                title="Finished verifier",
                description="",
                status=TaskStatus.COMPLETED,
            )
        )
    async with db.immediate() as conn:
        stage = (
            (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.ordinal == 1,
                    )
                )
            )
            .mappings()
            .one()
        )
        dossier = dict(stage["dossier"])
        if case == "recorded_stage_commits":
            dossier["repair_commits"] = [*dossier["repair_commits"], request.head_sha]
        elif case == "missing_incident":
            dossier.pop("supervisor_recovery")
        elif case in {"wrong_incident", "changed_incident_subject", "changed_incident_budget"}:
            incident = dict(dossier["supervisor_recovery"])
            if case == "wrong_incident":
                incident["incident_id"] = f"repair-no-progress:{request.operation_id}:0"
            elif case == "changed_incident_subject":
                incident["subject"] = incident["subject"] | {"head_sha": request.head_sha}
            else:
                incident["attempts"] += 1
            dossier["supervisor_recovery"] = incident
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.ordinal == 1,
            )
            .values(dossier=dossier)
        )
        if case == "non_pass_completion":
            await conn.execute(
                update(task_completion_records)
                .where(
                    task_completion_records.c.task_id == "repair-empty",
                )
                .values(outcome="fail")
            )
        elif case == "non_empty_completion":
            await conn.execute(
                update(task_completion_records)
                .where(
                    task_completion_records.c.task_id == "repair-empty",
                )
                .values(commits=json.dumps([repair_head]))
            )
        elif case == "extension_outside_receipts":
            earlier = dict(
                await conn.scalar(
                    select(integration_repair_stages.c.dossier).where(
                        integration_repair_stages.c.ordinal == 0,
                    )
                )
            )
            edge = dict(earlier["parent_head_extensions"][0])
            edge["commits"] = [edge["before_sha"], repair_head]
            earlier["parent_head_extensions"] = [edge]
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.ordinal == 0,
                )
                .values(dossier=earlier)
            )
        elif case == "subject_not_receipt_tip":
            base = git("rev-parse", "main")
            subject = {"kind": "parent", "generation": 1, "head_sha": base}
            dossier["branch_sha"] = base
            dossier["supervisor_recovery"] = dossier["supervisor_recovery"] | {"subject": subject}
            await conn.execute(
                update(integration_repair_stages)
                .where(
                    integration_repair_stages.c.ordinal == 1,
                )
                .values(current_subject=subject, starting_sha=base, dossier=dossier)
            )
        elif case == "missing_close_audit":
            await conn.execute(
                delete(integration_outbox).where(integration_outbox.c.id == "empty-closed")
            )
        elif case == "wrong_close_fence":
            payload = await conn.scalar(
                select(integration_outbox.c.payload).where(
                    integration_outbox.c.id == "empty-closed"
                )
            )
            await conn.execute(
                update(integration_outbox)
                .where(integration_outbox.c.id == "empty-closed")
                .values(
                    payload=payload | {"fence_token": 8},
                )
            )
        elif case == "missing_accepted_close":
            await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == "repair-empty",
                    task_metadata.c.key == ACCEPTED_CLOSE_KEY,
                )
            )
        elif case == "different_completion":
            await db._upsert_meta(
                "repair-empty",
                ACCEPTED_CLOSE_KEY,
                json.dumps(
                    {
                        "completion_id": "another-completion",
                        "session_id": "empty-session",
                        "claim_epoch": 0,
                    }
                ),
                conn=conn,
            )
        elif case == "stage_changed_head":
            await conn.execute(
                update(integration_repair_stages)
                .where(integration_repair_stages.c.ordinal == 1)
                .values(
                    starting_sha=git("rev-parse", "main"),
                )
            )
        elif case == "missing_confirmed_workspace":
            await conn.execute(
                update(integration_branch_owners).values(confirmed_workspace_id=None)
            )
        elif case == "open_gate":
            await conn.execute(
                insert(gates).values(
                    id="human-gate",
                    project_id="p",
                    gate_type="human",
                    title="Human approval",
                    status="open",
                    created_at=1.0,
                )
            )
            await conn.execute(insert(task_gates).values(task_id="parent", gate_id="human-gate"))
        elif case == "ambiguous_write":
            await conn.execute(
                insert(integration_promotion_intents).values(
                    id="ambiguous-write",
                    domain_key="ambiguous-write",
                    receipt_id="future-write-receipt",
                    operation_key=request.operation_id,
                    project_id="p",
                    source_task_id="parent.2",
                    source_head="b" * 40,
                    source_base="a" * 40,
                    repository_id="repo",
                    target_task_id="parent",
                    target_branch="aq/parent",
                    expected_target=repair_head,
                    fence_owner_id=request.operation_id,
                    fence_token=8,
                    state="prepared",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
        elif case == "completed_verifier":
            await conn.execute(
                update(integration_repair_operations).values(verifier_task_id="finished-verifier")
            )
        original = (
            (
                await conn.execute(
                    select(integration_repair_stages).order_by(integration_repair_stages.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
        receipts = (await conn.execute(select(task_delivery_receipts))).mappings().all()
    checkpoint = await db.get_integration_checkpoint("parent")
    for dry_run in (True, False):
        request.dry_run = dry_run
        with pytest.raises(ValueError):
            await recovery.run(request, principal="operator")
    assert await db.get_integration_checkpoint("parent") == checkpoint
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(
                select(integration_repair_stages).order_by(integration_repair_stages.c.ordinal)
            )
        ).mappings().all() == original
        assert (await conn.execute(select(task_delivery_receipts))).mappings().all() == receipts


async def test_empty_verification_stage_rechecks_close_proof_before_apply(
    db, tmp_path, monkeypatch
):
    from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY

    recovery, _hierarchy, request, _git, _base, _repair_head = await _empty_verification_stage_case(
        db, tmp_path
    )
    checkpoint = await db.get_integration_checkpoint("parent")
    prove = recovery._git_proof

    async def change_close(*args):
        proof = await prove(*args)
        await db.set_task_meta(
            "repair-empty",
            ACCEPTED_CLOSE_KEY,
            {
                "completion_id": "empty-completion",
                "session_id": "different-session",
                "claim_epoch": 0,
            },
        )
        return proof

    monkeypatch.setattr(recovery, "_git_proof", change_close)
    request.dry_run = False
    with pytest.raises(ValueError, match="accepted-close identity"):
        await recovery.run(request, principal="operator")
    assert await db.get_integration_checkpoint("parent") == checkpoint


@pytest.mark.parametrize("change", ["remote", "workspace", "dirty_workspace"])
async def test_empty_verification_stage_rechecks_git_and_workspace_under_apply_lock(
    db,
    tmp_path,
    monkeypatch,
    change,
):
    recovery, _hierarchy, request, git, _base, repair_head = await _empty_verification_stage_case(
        db,
        tmp_path,
    )
    checkpoint = await db.get_integration_checkpoint("parent")
    prove, calls = recovery._git_proof, 0

    async def change_git(*args):
        nonlocal calls
        proof = await prove(*args)
        calls += 1
        if change == "remote" and calls == 1:
            git("push", "origin", f"{repair_head}:refs/heads/aq/parent", "--force")
        elif calls == 2:
            if change == "workspace":
                # Still clean, and still published ancestry: it must also be unchanged.
                git("reset", "--hard", repair_head)
            elif change == "dirty_workspace":
                (tmp_path / "work" / "unpublished.txt").write_text("arrived during apply")
        return proof

    monkeypatch.setattr(recovery, "_git_proof", change_git)
    request.dry_run = False
    with pytest.raises(
        ValueError,
        match={
            "remote": "published parent head differs",
            "workspace": "workspace changed during recovery",
            "dirty_workspace": "unpublished changes",
        }[change],
    ):
        await recovery.run(request, principal="operator")
    assert await db.get_integration_checkpoint("parent") == checkpoint
    async with db._engine.connect() as conn:
        stage = (
            (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.ordinal == 1,
                    )
                )
            )
            .mappings()
            .one()
        )
        assert stage["state"] == "expired"
        assert "empty_verification_recovery" not in stage["dossier"]


async def test_empty_verification_stage_rolls_back_if_fresh_verifier_cannot_be_filed(db, tmp_path):
    recovery, _hierarchy, request, _git, _base, _head = await _empty_verification_stage_case(
        db, tmp_path
    )
    async with db.immediate() as conn:
        operation = (await conn.execute(select(integration_repair_operations))).mappings().one()
        policy = dict(operation["policy_snapshot"])
        policy["parent"] = dict(policy["parent"]) | {"verifier_intelligence_class": None}
        await conn.execute(update(integration_repair_operations).values(policy_snapshot=policy))
        stages = (
            (
                await conn.execute(
                    select(integration_repair_stages).order_by(
                        integration_repair_stages.c.ordinal,
                    )
                )
            )
            .mappings()
            .all()
        )
    checkpoint = await db.get_integration_checkpoint("parent")
    request.dry_run = False
    with pytest.raises(ValueError, match="cannot project verifier readiness"):
        await recovery.run(request, principal="operator")
    assert await db.get_integration_checkpoint("parent") == checkpoint
    assert await db.get_task(f"verify-{request.operation_id}") is None
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(
                select(integration_repair_stages).order_by(
                    integration_repair_stages.c.ordinal,
                )
            )
        ).mappings().all() == stages


async def test_empty_verification_stage_without_later_collection_still_requires_fresh_verifier(
    db,
    tmp_path,
):
    recovery, _hierarchy, request, _git, _base, _head = await _empty_verification_stage_case(
        db, tmp_path
    )
    async with db.immediate() as conn:
        dossier = dict(
            await conn.scalar(
                select(integration_repair_stages.c.dossier).where(
                    integration_repair_stages.c.ordinal == 1,
                )
            )
        )
        subject = {"kind": "parent", "generation": 2, "head_sha": request.head_sha}
        dossier["branch_sha"] = request.head_sha
        dossier["supervisor_recovery"] = dossier["supervisor_recovery"] | {"subject": subject}
        await conn.execute(
            update(integration_repair_stages)
            .where(
                integration_repair_stages.c.ordinal == 1,
            )
            .values(starting_sha=request.head_sha, current_subject=subject, dossier=dossier)
        )
    request.dry_run = False
    assert (await recovery.run(request, principal="operator"))["outcome"] == "recovered"
    async with db._engine.connect() as conn:
        stage = (
            (
                await conn.execute(
                    select(integration_repair_stages).where(
                        integration_repair_stages.c.ordinal == 1,
                    )
                )
            )
            .mappings()
            .one()
        )
        operation = (await conn.execute(select(integration_repair_operations))).mappings().one()
        assert stage["state"] == "passed" and operation["state"] == "active"
        assert operation["verifier_task_id"] is not None
        assert (
            len(
                (
                    await conn.execute(
                        select(integration_outbox).where(
                            integration_outbox.c.event_type == "task.integration_ready",
                            integration_outbox.c.payload["head_sha"].as_string()
                            == request.head_sha,
                        )
                    )
                ).all()
            )
            == 1
        )


@pytest.fixture
async def db(tmp_path, reuse_database):
    database = await reuse_database("parent-completion.db")
    await database.create_project(Project(id="p", name="integration project"))
    yield database


async def _enable_project(
    db, *, on_failed_child: str = "block", boundary: IntegrationBoundaryPolicy | None = None
) -> dict:
    artifact = _artifact()
    policy = HierarchicalIntegrationPolicy(
        parent=boundary or _boundary(),
        root=_boundary(),
        branchless_parent="verifier",
        on_failed_child=on_failed_child,
    ).model_dump(mode="json")
    await db.create_repo(RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK))
    await db.update_project(
        "p",
        hierarchical_integration_mode="hierarchy",
        integration_repository_id="repo",
        hierarchical_integration_policy=policy,
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
    return policy


async def _seed_parent_identity(db, *, generation: int = 0) -> None:
    await db.create_repo(
        RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK)
    )
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=generation,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )


async def _parent_tree(
    db,
    *,
    children: int = 2,
    on_failed_child: str = "block",
    boundary: IntegrationBoundaryPolicy | None = None,
    base_sha: str = "a" * 40,
    parent_id: str = "parent",
    branch: str = "aq/parent",
):
    await _enable_project(db, on_failed_child=on_failed_child, boundary=boundary)
    await db.create_task(
        Task(
            id=parent_id,
            project_id="p",
            repo_id="repo",
            branch_name=branch,
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    hierarchy = HierarchyIntegration(
        db,
        default_head_resolver=lambda _repo, _branch: base_sha,
        checkpoint_verifier=lambda _task, _repo, head: head,
    )
    filed = await hierarchy.file_children(
        parent_id, [{"title": f"child {index}"} for index in range(children)], 0
    )
    checkpointed = await hierarchy.checkpoint_parent(parent_id, base_sha, 1)
    child_ids = [row["task_id"] for row in filed["children"]]
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id.in_(child_ids)).values(status="COMPLETED")
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id.in_(child_ids))
            .values(checkpoint_sha="b" * 40)
        )
    return hierarchy, checkpointed, child_ids


async def _code_receipt(
    db, child_id: str, before_sha: str, after_sha: str, **overrides
) -> None:
    async with db.immediate() as conn:
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == "parent"
                )
            )
        ).mappings().one()
        operation = (
            await conn.execute(
                select(integration_repair_operations).where(
                    integration_repair_operations.c.parent_task_id == "parent",
                    integration_repair_operations.c.episode_id == checkpoint["episode_id"],
                )
            )
        ).mappings().one()
        values = {
            "id": f"receipt-{child_id}",
            "domain_key": f"delivery-{child_id}",
            "source_task_id": child_id,
            "target_task_id": "parent",
            "repository_id": "repo",
            "target_branch": "aq/parent",
            "reviewed_head_sha": "b" * 40,
            "reviewed_tree_sha": "c" * 40,
            "before_sha": before_sha,
            "squash_sha": after_sha,
            "after_sha": after_sha,
            "review_evidence": {"id": f"review-{child_id}"},
            "parent_operation_id": operation["id"],
            "parent_episode_id": checkpoint["episode_id"],
            "disposition": "code",
            "created_at": float(int(child_id.rsplit(".", 1)[1])),
        }
        values.update(overrides)
        await conn.execute(insert(task_delivery_receipts).values(**values))


def _artifact() -> ArtifactSnapshot:
    return ArtifactSnapshot(
        playbook_id="hierarchical-delivery",
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test-build",
        compiled_at="2026-09-05T00:00:00Z",
        version=4,
    )


def _boundary(**overrides) -> IntegrationBoundaryPolicy:
    # The deprecated ``*_profile_id`` fields model a policy stored before
    # mandatory routing: it must still validate and every code path ignore them.
    values = {
        "required_checks": RequiredCheckSet(
            version="parent-v1", names=("unit",), producer_id="forge-observer"
        ),
        "repair": RepairPolicy(debug_intelligence_class="deep-high"),
        "route": PlaybookRoute(
            playbook_id="hierarchical-delivery",
            scope="project",
            scope_identifier="p",
            activation_id="activation-audit-only",
            artifact=_artifact(),
        ),
        "primary_intelligence_class": "medium",
        "primary_profile_id": "integrator",
        "verifier_intelligence_class": "high",
        "verifier_profile_id": "verifier",
    }
    values.update(overrides)
    return IntegrationBoundaryPolicy(**values)


def test_hierarchical_policy_freezes_full_parent_and_root_inputs():
    policy = HierarchicalIntegrationPolicy(
        version=1,
        parent=_boundary(),
        root=_boundary(),
        branchless_parent="verifier",
        on_failed_child="block",
    )

    dumped = policy.model_dump(mode="json")
    assert dumped["parent"]["route"]["artifact"] == _artifact().model_dump(mode="json")
    assert dumped["parent"]["route"]["playbook_id"] == "hierarchical-delivery"
    assert dumped["parent"]["route"]["scope"] == "project"
    assert dumped["parent"]["route"]["scope_identifier"] == "p"
    with pytest.raises(Exception):
        policy.parent.required_checks.names = ("changed",)


@pytest.mark.parametrize("field,value", [("branchless_parent", "guess"), ("on_failed_child", "ignore")])
def test_hierarchical_policy_rejects_unruled_choices(field, value):
    values = {
        "version": 1,
        "parent": _boundary(),
        "root": _boundary(),
        "branchless_parent": "verifier",
        "on_failed_child": "block",
    }
    values[field] = value
    with pytest.raises(Exception):
        HierarchicalIntegrationPolicy(**values)


async def test_project_policy_round_trips_as_nullable_json(db):
    assert (await db.get_project("p")).hierarchical_integration_policy is None
    policy = HierarchicalIntegrationPolicy(
        parent=_boundary(),
        root=_boundary(),
        branchless_parent="verifier",
        on_failed_child="block",
    ).model_dump(mode="json")
    await db.update_project("p", hierarchical_integration_policy=policy)
    assert (await db.get_project("p")).hierarchical_integration_policy == policy


async def test_parent_episode_operation_identity_survives_completion(db):
    await _seed_parent_identity(db)
    values = {
        "target_kind": "parent",
        "parent_task_id": "parent",
        "episode_id": "episode",
        "active_stage": 0,
        "state": "active",
        "policy_snapshot": {},
        "artifact_snapshot": {},
        "required_check_version": "checks-v1",
        "route_playbook_id": "hierarchical-delivery",
        "route_scope": "project",
        "route_scope_identifier": "p",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    async with db.immediate() as conn:
        await conn.execute(insert(integration_repair_operations).values(id="op-1", **values))
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == "op-1")
            .values(state="completed")
        )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert(integration_repair_operations).values(id="op-2", **values)
                )


async def test_parent_identity_foreign_keys_reject_mismatched_episode_links(db):
    await _seed_parent_identity(db)
    async with db.immediate() as conn:
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert(integration_repair_operations).values(
                        id="wrong-operation",
                        target_kind="parent",
                        parent_task_id="parent",
                        episode_id="missing-episode",
                        active_stage=0,
                        state="active",
                        policy_snapshot={},
                        artifact_snapshot={},
                        required_check_version="test",
                        created_at=1.0,
                        updated_at=1.0,
                    )
                )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert(task_integration_checkpoints).values(
                        task_id="parent",
                        repository_id="repo",
                        branch="aq/parent",
                        generation=0,
                        episode_id="missing-episode",
                        state="working",
                        version=0,
                        updated_at=1.0,
                    )
                )


async def test_check_evidence_and_verification_links_are_append_only(db):
    await _seed_parent_identity(db, generation=1)
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                generation=1,
                state="verifying",
                version=0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                route_playbook_id="hierarchical-delivery",
                route_scope="project",
                route_scope_identifier="p",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="evidence",
                operation_id="op",
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha="a" * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="checks-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="verification",
                operation_id="op",
                parent_task_id="parent",
                episode_id="episode",
                generation=1,
                head_sha="a" * 40,
                required_check_version="checks-v1",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_parent_verification_evidence).values(
                verification_id="verification", evidence_id="evidence"
            )
        )
        for statement in (
            update(integration_check_evidence)
            .where(integration_check_evidence.c.id == "evidence")
            .values(conclusion="failure"),
            delete(integration_check_evidence).where(
                integration_check_evidence.c.id == "evidence"
            ),
            update(integration_parent_verification_evidence)
            .where(
                integration_parent_verification_evidence.c.verification_id
                == "verification"
            )
            .values(evidence_id="changed"),
        ):
            with pytest.raises(DBAPIError):
                async with conn.begin_nested():
                    await conn.execute(statement)


async def test_parent_operation_artifact_pin_prevents_collection(db):
    await _seed_parent_identity(db)
    artifact = _artifact()
    async with db.immediate() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                **artifact.model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="op",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="episode",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot=artifact.model_dump(mode="json"),
                required_check_version="checks-v1",
                route_playbook_id="hierarchical-delivery",
                route_scope="project",
                route_scope_identifier="p",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_operation_artifact_pins).values(
                operation_id="op", artifact_sha256=artifact.artifact_sha256
            )
        )

    assert await db.collect_playbook_artifacts(before=2.0, min_versions=0) == []
    async with db._engine.connect() as conn:
        assert (
            await conn.execute(
                select(playbook_artifacts.c.artifact_sha256).where(
                    playbook_artifacts.c.artifact_sha256 == artifact.artifact_sha256
                )
            )
        ).scalar_one() == artifact.artifact_sha256


async def test_first_parent_checkpoint_reserves_one_frozen_episode_operation(db):
    policy = await _enable_project(db)
    await db.create_task(
        Task(
            id="parent",
            project_id="p",
            repo_id="repo",
            branch_name="aq/parent",
            parent_task_id=None,
            title="parent",
            description="parent",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="parent",
                repository_id="repo",
                branch="aq/parent",
                generation=2,
                checkpoint_sha="a" * 40,
                state="working",
                version=0,
                updated_at=1.0,
            )
        )
    hierarchy = HierarchyIntegration(
        db, checkpoint_verifier=lambda _task, _repo, head: head
    )

    with pytest.raises(StaleClaim):
        await hierarchy.checkpoint_and_suspend_parent(
            "parent", "b" * 40, 2, expect_claim_epoch=99,
            accepted_close={"completion_id": "c", "session_id": "s", "claim_epoch": 99},
        )
    assert (await db.get_integration_checkpoint("parent"))["episode_id"] is None
    # A fenced-out suspension accepts no close.
    assert await db.get_task_meta("parent", "accepted_close") is None
    assert await db.get_active_parent_integration_operation("parent") is None

    result = await hierarchy.checkpoint_parent("parent", "b" * 40, 2)
    checkpoint = await db.get_integration_checkpoint("parent")
    operation = await db.get_integration_operation(result["operation_id"])

    assert result["episode_id"] == checkpoint["episode_id"]
    assert operation["episode_id"] == result["episode_id"]
    assert operation["policy_snapshot"] == policy
    assert operation["artifact_snapshot"] == policy["parent"]["route"]["artifact"]
    assert operation["route_playbook_id"] == "hierarchical-delivery"
    assert operation["route_scope"] == "project"
    assert operation["route_scope_identifier"] == "p"
    assert operation["active_stage"] == 0
    async with db._engine.connect() as conn:
        pins = (
            await conn.execute(
                select(integration_operation_artifact_pins).where(
                    integration_operation_artifact_pins.c.operation_id == operation["id"]
                )
            )
        ).mappings().all()
    assert [row["artifact_sha256"] for row in pins] == [
        policy["parent"]["route"]["artifact"]["artifact_sha256"]
    ]


async def test_terminal_children_require_complete_contiguous_receipt_chain(db):
    hierarchy, _checkpointed, children = await _parent_tree(db)

    assert (await hierarchy.readiness("parent"))["outcome"] == "waiting"
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    partial = await hierarchy.readiness("parent")
    assert partial["outcome"] == "waiting"
    assert partial["head_sha"] == "d" * 40
    await _code_receipt(db, children[1], "d" * 40, "e" * 40)

    ready = await hierarchy.readiness("parent")
    assert ready["outcome"] == "ready"
    assert ready["head_sha"] == "e" * 40
    assert [row["source_task_id"] for row in ready["receipts"]] == children


async def test_status_uses_current_receipt_among_multiple_historical_deliveries(db):
    _hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    child_id = children[0]
    async with db.immediate() as conn:
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="historic-receipt",
                domain_key="historic-delivery",
                source_task_id=child_id,
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                reviewed_head_sha="1" * 40,
                reviewed_tree_sha="2" * 40,
                before_sha="a" * 40,
                squash_sha="3" * 40,
                after_sha="3" * 40,
                review_evidence={"id": "historic-review"},
                parent_operation_id=None,
                parent_episode_id=None,
                disposition="code",
                created_at=0.0,
            )
        )
    await _code_receipt(db, child_id, "a" * 40, "d" * 40)

    projection = await IntegrationStatusService(db).task_blockers("parent")

    assert projection is not None
    assert projection["parent_readiness"]["operation_id"] == checkpointed["operation_id"]
    assert "missing_receipt" not in {
        blocker["code"] for blocker in projection["blockers"]
    }


@pytest.mark.parametrize(
    "invalid_values",
    [
        {"reviewed_head_sha": "9" * 40},
        {"repository_id": "wrong-repository"},
        {"target_branch": "wrong-branch"},
        {"parent_operation_id": None, "parent_episode_id": None},
    ],
)
async def test_status_rejects_receipt_not_applicable_to_current_parent_context(
    db, invalid_values
):
    _hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    child_id = children[0]
    await _code_receipt(db, child_id, "a" * 40, "d" * 40, **invalid_values)

    projection = await IntegrationStatusService(db).task_blockers("parent")

    assert projection is not None
    assert "missing_receipt" in {
        blocker["code"] for blocker in projection["blockers"]
    }


async def test_status_blocks_failed_child_without_current_disposition_receipt(db):
    _hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == children[0]).values(status="FAILED")
        )

    projection = await IntegrationStatusService(db).task_blockers("parent")

    assert projection is not None
    blocker = next(item for item in projection["blockers"] if item["code"] == "missing_receipt")
    assert blocker["cause"] == "failed_child"


@pytest.mark.parametrize(
    "ordinal,attempts,expected_limit",
    [(0, 2, 2), (1, 1, 1)],
)
async def test_status_uses_typed_limit_for_current_repair_stage(
    db, ordinal, attempts, expected_limit
):
    _hierarchy, checkpointed, _children = await _parent_tree(db, children=1)
    policy = _boundary().repair.model_dump(mode="json")
    policy["primary_attempts"] = 2
    policy["debug_attempts"] = 1
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == checkpointed["operation_id"])
            .values(active_stage=ordinal)
        )
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=ordinal,
                policy=policy,
                intelligence_class="medium",
                profile_id="integrator",
                starting_sha="a" * 40,
                started_at=1.0,
                deadline_at=100.0,
                attempts=attempts,
                state="active",
            )
        )

    projection = await IntegrationStatusService(db, clock=lambda: 50.0).task_blockers(
        "parent"
    )

    assert projection is not None
    blocker = next(item for item in projection["blockers"] if item["code"] == "budget_exhausted")
    assert blocker["cause"] == "attempts"
    assert blocker["stage"] == ordinal
    assert blocker["limit"] == expected_limit


async def test_parent_current_stage_remains_deadline_bound_while_awaiting_completion(db):
    _hierarchy, checkpointed, _children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=0,
                policy=_boundary().repair.model_dump(mode="json"),
                intelligence_class="medium",
                profile_id="integrator",
                starting_sha="a" * 40,
                started_at=1.0,
                deadline_at=100.0,
                attempts=1,
                state="awaiting_completion",
            )
        )

    projection = await IntegrationStatusService(db, clock=lambda: 101.0).task_blockers(
        "parent"
    )

    assert projection is not None
    blocker = next(item for item in projection["blockers"] if item["code"] == "budget_exhausted")
    assert blocker["cause"] == "deadline"
    assert blocker["deadline_at"] == 100.0


@pytest.mark.parametrize("policy", ["block", "ask"])
async def test_failed_child_readiness_exposes_frozen_disposition_policy(db, policy):
    hierarchy, _checkpointed, children = await _parent_tree(
        db, children=1, on_failed_child=policy
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == children[0]).values(status="FAILED")
        )

    readiness = await hierarchy.readiness("parent")

    assert readiness["outcome"] == "failed"
    assert readiness["on_failed_child"] == policy
    assert readiness["blockers"] == [
        {"task_id": children[0], "reason": "failed_child"}
    ]


async def test_arbitrary_resolution_json_cannot_satisfy_code_receipt_chain(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    # Receipts are append-only (trg_task_delivery_receipts_update), so the
    # forged row is written as-is rather than patched after the fact.
    await _code_receipt(
        db,
        children[0],
        "a" * 40,
        "d" * 40,
        squash_sha=None,
        resolution_evidence={"kind": "conflict_resolution", "trusted": True},
    )

    readiness = await hierarchy.readiness("parent")

    assert readiness["outcome"] == "waiting"
    assert readiness["head_sha"] == "a" * 40
    assert readiness["blockers"] == [
        {"task_id": children[0], "reason": "receipt_chain"}
    ]


async def test_unbound_historic_receipts_do_not_satisfy_current_parent_episode(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    # Receipts are append-only (trg_task_delivery_receipts_update), so the
    # unbound historic row is written as-is rather than unbound afterwards.
    await _code_receipt(
        db,
        children[0],
        "a" * 40,
        "d" * 40,
        parent_operation_id=None,
        parent_episode_id=None,
    )

    readiness = await hierarchy.readiness("parent")
    assert readiness["outcome"] == "waiting"
    assert readiness["blockers"] == [
        {"task_id": children[0], "reason": "receipt_missing"}
    ]


async def test_receipt_bound_to_unrelated_historic_episode_is_rejected(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id="unrelated-episode",
                parent_task_id="parent",
                repository_id="repo",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=0.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id="unrelated-operation",
                target_kind="parent",
                parent_task_id="parent",
                episode_id="unrelated-episode",
                active_stage=0,
                state="completed",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="test",
                created_at=0.0,
                updated_at=0.0,
            )
        )
        await conn.execute(
            insert(task_delivery_receipts).values(
                id="historic-receipt",
                domain_key="historic-delivery",
                source_task_id=children[0],
                target_task_id="parent",
                repository_id="repo",
                target_branch="aq/parent",
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                before_sha="a" * 40,
                squash_sha="d" * 40,
                after_sha="d" * 40,
                review_evidence={"id": "historic-review"},
                parent_operation_id="unrelated-operation",
                parent_episode_id="unrelated-episode",
                disposition="code",
                created_at=0.0,
            )
        )

    readiness = await hierarchy.readiness("parent")
    assert readiness["outcome"] == "waiting"
    assert readiness["receipts"] == []


async def test_disposition_revision_supersedes_only_changed_child(db):
    hierarchy, _checkpointed, children = await _parent_tree(db)
    await _code_receipt(db, children[1], "a" * 40, "d" * 40)

    first = await hierarchy.record_disposition(
        children[0],
        disposition="noop",
        reviewed_head_sha="b" * 40,
        reviewed_tree_sha="c" * 40,
        verification_evidence={"producer_id": "forge-observer", "evidence_id": "noop-1"},
        resolution_evidence={"authority": "playbook", "decision_id": "decision-1"},
    )
    assert first["revision"] == 0
    assert (await hierarchy.readiness("parent"))["outcome"] == "ready"

    second = await hierarchy.record_disposition(
        children[0],
        disposition="skipped",
        reviewed_head_sha="b" * 40,
        reviewed_tree_sha="c" * 40,
        verification_evidence={"producer_id": "forge-observer", "evidence_id": "noop-2"},
        resolution_evidence={"authority": "operator", "decision_id": "decision-2"},
    )
    assert second["revision"] == 1
    projection = await hierarchy.readiness("parent")
    assert projection["outcome"] == "ready"
    selected = {row["source_task_id"]: row for row in projection["receipts"]}
    assert selected[children[0]]["disposition"] == "skipped"
    assert selected[children[1]]["id"] == f"receipt-{children[1]}"
    status = await IntegrationStatusService(db).task_blockers("parent")
    assert status is not None
    assert "missing_receipt" not in {item["code"] for item in status["blockers"]}


async def test_record_noop_command_binds_review_close_and_exact_child_head(db):
    hierarchy, _checkpointed, children = await _parent_tree(db)
    reviewer_id = children[0]
    await _code_receipt(db, children[1], "a" * 40, "d" * 40)
    await db.create_profile(
        AgentProfile(
            id="reviewer", name="Reviewer", harness="codex", lifecycle="task",
            aq_commands=[], harness_tools=[], plugin_tools=[], needs_workspace=False,
        )
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == reviewer_id)
            .values(profile_id="reviewer", route_source="role")
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == reviewer_id)
            .values(checkpoint_sha="a" * 40)
        )
        await conn.execute(
            insert(integration_review_evidence).values(
                id="approved-review",
                source_task_id=children[1],
                repository_id="repo",
                source_base="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                reviewer_task_id=reviewer_id,
                review_kind="leaf",
                generation=0,
                verdict="approved",
                evidence={"decision_path": "review_task_close"},
                created_at=1.0,
            )
        )
    reviewer = await db.get_task(reviewer_id)
    await db.save_task_completion(
        TaskCompletion(
            id="noop-close-1", task_id=reviewer_id, outcome="pass", work_outcome="no-op",
            branch=reviewer.branch_name, completed_at=2.0,
        )
    )

    @asynccontextmanager
    async def repository_transaction(_path):
        yield

    class Promotion:
        git = SimpleNamespace(arepository_transaction=repository_transaction)

        async def _resolve_repository(self, _repository_id):
            return SimpleNamespace(
                repo=SimpleNamespace(project_id="p"),
                retained_git_dir="/tmp/retained-repo",
                origin_url="https://example.invalid/repo.git",
            )

        async def _ensure_retained_repository(self, _resolved):
            return None

        async def _fetch_all_heads(self, _path, _origin_url):
            return None

        async def _tree_oid(self, _path, _head):
            return "c" * 40

    class Handler(IntegrationCommandsMixin):
        orchestrator = SimpleNamespace(
            hierarchy_integration=hierarchy, promotion_service=Promotion()
        )

    handler = Handler()
    handler.db = db
    args = {"child_task_id": reviewer_id, "expected_head_sha": "a" * 40}
    worker = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, project_id="p", policy=DENY_ALL,
    )
    with principal_context(worker):
        denied = await handler._cmd_integration_record_noop(args)
    assert denied["outcome"] == "unauthorized"
    assert (await hierarchy.readiness("parent"))["outcome"] == "waiting"

    stale = await handler._cmd_integration_record_noop(args | {"expected_head_sha": "b" * 40})
    assert stale["outcome"] == "stale_head"
    recorded = await handler._cmd_integration_record_noop(args)
    assert recorded["outcome"] == "recorded"
    assert recorded["reviewed_tree_sha"] == "c" * 40
    assert (await handler._cmd_integration_record_noop(args))["receipt_id"] == recorded["receipt_id"]
    projection = await hierarchy.readiness("parent")
    assert projection["outcome"] == "ready"
    receipt = next(row for row in projection["receipts"] if row["source_task_id"] == reviewer_id)
    assert receipt["verification_evidence"]["review_evidence_id"] == "approved-review"
    assert receipt["resolution_evidence"]["completion_id"] == "noop-close-1"

    await db.save_task_completion(
        TaskCompletion(
            id="noop-close-2", task_id=reviewer_id, outcome="pass", work_outcome="no-op",
            branch=reviewer.branch_name, completed_at=3.0,
        )
    )
    revised = await handler._cmd_integration_record_noop(args)
    assert revised["revision"] == 1
    assert revised["receipt_id"] != recorded["receipt_id"]
    assert (await hierarchy.readiness("parent"))["outcome"] == "ready"


async def test_record_noop_refuses_an_elevated_supervisor_session(db):
    """A no-code receipt is an operator control, so no session may record one.

    ``integration_record_noop`` is deliberately absent from
    ``_SUPERVISOR_REDRIVE_CAPABILITIES``: even a live, elevated supervisor
    session carrying a granting policy is refused, before the child is checked.
    That is why the guide and the supervisor profile hand the command to a
    local operator instead of telling a session to run it.
    """
    from src.commands.integration_commands import _SUPERVISOR_REDRIVE_CAPABILITIES

    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    child_id = children[0]
    child = await db.get_task(child_id)
    await db.save_task_completion(
        TaskCompletion(
            id="supervisor-noop", task_id=child_id, outcome="pass", work_outcome="no-op",
            branch=child.branch_name, completed_at=2.0,
        )
    )

    class Handler(IntegrationCommandsMixin):
        orchestrator = SimpleNamespace(hierarchy_integration=hierarchy)

    handler = Handler()
    handler.db = db
    supervisor = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        session_id="supervisor-1",
        profile_id="supervisor",
        project_id="p",
        elevated=True,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=frozenset({"integration_record_noop"})
        ),
    )
    with principal_context(supervisor):
        refused = await handler._cmd_integration_record_noop(
            {"child_task_id": child_id, "expected_head_sha": "b" * 40}
        )

    assert refused == {
        "success": False,
        "outcome": "unauthorized",
        "error": "caller cannot dispose this child",
    }
    assert "integration_record_noop" not in _SUPERVISOR_REDRIVE_CAPABILITIES
    assert (await hierarchy.readiness("parent"))["outcome"] == "waiting"


async def test_verified_noop_refuses_a_child_branch_advanced_from_its_reserved_base(db):
    hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    child_id = children[0]
    child = await db.get_task(child_id)
    await db.save_task_completion(
        TaskCompletion(
            id="claimed-noop", task_id=child_id, outcome="pass", work_outcome="no-op",
            branch=child.branch_name, completed_at=2.0,
        )
    )
    with pytest.raises(HierarchyError, match="reserved base"):
        await hierarchy.record_disposition(
            child_id,
            disposition="noop",
            reviewed_head_sha="b" * 40,
            reviewed_tree_sha="c" * 40,
            verification_evidence={"completion_id": "claimed-noop"},
            resolution_evidence={"completion_id": "claimed-noop"},
            verified_completion_id="claimed-noop",
        )
    assert (await hierarchy.readiness("parent"))["outcome"] == "waiting"


async def test_parent_completion_pins_exact_verification_for_rollover(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="older-verification",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                episode_id=checkpointed["episode_id"],
                generation=0,
                head_sha="c" * 40,
                required_check_version="parent-v1",
                created_at=1.5,
            )
        )
        await conn.execute(
            insert(integration_check_evidence).values(
                id="check-unit",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha="d" * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=2.0,
            )
        )
        await conn.execute(update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS"))

    verified = await hierarchy.verify_parent("parent", 1, "d" * 40, ["check-unit"])
    assert verified["outcome"] == "verified"
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["current_verification_id"] == verified["verification_id"]
    assert checkpoint["checkpoint_sha"] == "d" * 40
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_repair_stages).values(
                operation_id=checkpointed["operation_id"],
                ordinal=0,
                policy=_boundary().repair.model_dump(mode="json"),
                intelligence_class="medium",
                profile_id="integrator",
                starting_sha="a" * 40,
                trigger_id="check-unit",
                current_subject={
                    "kind": "parent",
                    "generation": 1,
                    "head_sha": "d" * 40,
                },
                deadline_event_id=f"repair-deadline-{checkpointed['operation_id']}-0",
                success_subject={
                    "kind": "parent",
                    "generation": 1,
                    "head_sha": "d" * 40,
                },
                success_evidence_id="check-unit",
                started_at=1.0,
                deadline_at=100.0,
                attempts=1,
                state="awaiting_completion",
            )
        )
    async with db._engine.connect() as conn:
        verified_events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "task.integration_verified"
                )
            )
        ).mappings().all()
    assert len(verified_events) == 1
    assert verified_events[0]["payload"]["operation_id"] == checkpointed["operation_id"]

    with pytest.raises(Exception, match="integration completion"):
        await db.transition_task("parent", TaskStatus.COMPLETED, force=True)
    assert (await hierarchy.complete_parent("parent", 1, "d" * 40))["outcome"] == "invariant_error"
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(owner_id="parent", owner_role="verifier", fence_token=2)
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(branch_owner_id="parent")
        )
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == checkpointed["operation_id"])
            .values(state="human_required")
        )
    assert (
        await hierarchy.complete_parent("parent", 1, "d" * 40)
    )["outcome"] == "invariant_error"
    async with db.immediate() as conn:
        await conn.execute(
            update(integration_repair_operations)
            .where(integration_repair_operations.c.id == checkpointed["operation_id"])
            .values(state="escalated")
        )
    completed = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert completed["outcome"] == "completed"
    assert (await db.get_task("parent")).status is TaskStatus.COMPLETED
    assert (await db.get_integration_operation(checkpointed["operation_id"]))["state"] == "completed"
    async with db._engine.connect() as conn:
        verifier_is_terminal = (await conn.execute(
            select(terminal_reservation_clause())
            .select_from(integration_branch_owners)
            .where(integration_branch_owners.c.ref == "aq/parent")
        )).scalar_one()
    assert verifier_is_terminal is True
    completed_status = await IntegrationStatusService(db).task_blockers("parent")
    assert completed_status is not None
    assert completed_status["parent_readiness"]["operation_id"] == checkpointed["operation_id"]
    assert "missing_receipt" not in {
        item["code"] for item in completed_status["blockers"]
    }
    assert completed_status["repair"] == []
    async with db._engine.connect() as conn:
        completion = (
            await conn.execute(select(integration_parent_operation_completions))
        ).mappings().one()
        repair_stage = (
            await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id
                    == checkpointed["operation_id"]
                )
            )
        ).mappings().one()
    assert completion["operation_id"] == checkpointed["operation_id"]
    assert completion["verification_id"] == verified["verification_id"]
    assert completion["parent_task_id"] == "parent"
    assert completion["episode_id"] == checkpointed["episode_id"]
    assert repair_stage["state"] == "passed"
    assert repair_stage["completed_at"] is not None
    completed_checkpoint = await db.get_integration_checkpoint("parent")
    assert completed_checkpoint["last_completed_operation_id"] == checkpointed["operation_id"]
    assert completed_checkpoint["last_completed_verification_id"] == verified["verification_id"]

    await db.transition_task("parent", TaskStatus.READY, assigned_agent_id=None)
    rolled = await db.get_integration_checkpoint("parent")
    assert rolled["episode_id"] is None
    assert rolled["current_verification_id"] is None
    assert rolled["last_completed_operation_id"] == checkpointed["operation_id"]
    assert rolled["last_completed_verification_id"] == verified["verification_id"]
    assert rolled["generation"] == 2

    rollover = HierarchyIntegration(
        db,
        checkpoint_verifier=lambda _task, _repo, head: head,
        ancestry_verifier=lambda _repo, ancestor, descendant: (
            ancestor == "d" * 40 and descendant == "d" * 40
        ),
    )
    next_episode = await rollover.checkpoint_parent("parent", "d" * 40, 2)
    readiness = await rollover.readiness("parent")
    assert next_episode["episode_id"] != checkpointed["episode_id"]
    assert readiness["outcome"] == "ready"
    assert [row["source_task_id"] for row in readiness["receipts"]] == children
    async with db._engine.connect() as conn:
        carried = (
            await conn.execute(select(integration_episode_receipt_acceptances))
        ).mappings().one()
    assert carried["receipt_id"] == f"receipt-{children[0]}"
    assert carried["previous_verification_id"] == verified["verification_id"]
    assert carried["operation_id"] == next_episode["operation_id"]
    status = await IntegrationStatusService(db).task_blockers("parent")
    assert status is not None
    assert "missing_receipt" not in {item["code"] for item in status["blockers"]}


async def _verified_parent_tree(db, *, children: int = 1):
    """A collected parent whose aggregate verifier is the recorded owner."""
    hierarchy, checkpointed, child_ids = await _parent_tree(db, children=children)
    await _code_receipt(db, child_ids[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS"))
        await conn.execute(
            update(integration_branch_owners)
            .where(
                integration_branch_owners.c.repository_id == "repo",
                integration_branch_owners.c.ref == "aq/parent",
            )
            .values(owner_id="parent", owner_role="verifier", fence_token=2)
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(branch_owner_id="parent")
        )
    return hierarchy, checkpointed, child_ids


async def _trusted_check(db, checkpointed, head_sha="d" * 40, generation=1, **overrides):
    values = {
        "id": "check-unit",
        "operation_id": checkpointed["operation_id"],
        "parent_task_id": "parent",
        "parent_generation": generation,
        "parent_head_sha": head_sha,
        "producer_id": "forge-observer",
        "workflow_id": "workflow",
        "run_id": "run",
        "attempt": 1,
        "required_check_version": "parent-v1",
        "checks": {"unit": "success"},
        "conclusion": "success",
        "classification": "conclusive",
        "observed_at": 2.0,
    }
    values.update(overrides)
    async with db.immediate() as conn:
        await conn.execute(insert(integration_check_evidence).values(**values))
    return values["id"]


@pytest.fixture
async def git_completed_parent(db, tmp_path):
    """An actual ParentEpisodeRecords close, with real Git and no leaf close row."""
    from src.git.manager import GitManager
    from src.integration.development import DevelopmentPrimitives
    from tests.test_delivery_consumers import Origin, git

    origin = Origin(tmp_path)
    base = git(origin.clone, "rev-parse", "origin/main")
    partial = origin.work("parent", "first")
    head = origin.work("parent", "last")
    hierarchy, checkpointed, children = await _parent_tree(db, children=1, base_sha=base)
    await _code_receipt(db, children[0], base, head)
    async with db.immediate() as conn:
        await conn.execute(update(repos).where(repos.c.id == "repo").values(url=origin.url))
        await conn.execute(update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS"))
        await conn.execute(update(integration_branch_owners).values(
            owner_id="parent", owner_role="verifier", fence_token=2,
        ))
        # Like the live incident: the episode starts at 1 and collection
        # advances to generation 6 before trusted verification completes it.
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "parent",
        ).values(branch_owner_id="parent", generation=6))
    await _trusted_check(db, checkpointed, head_sha=head, generation=6)
    verified = await hierarchy.verify_parent("parent", 6, head, ["check-unit"])
    assert verified["outcome"] == "verified"
    assert (await hierarchy.complete_parent("parent", 6, head))["outcome"] == "completed"
    assert await db.get_task_completion("parent") is None
    service = DevelopmentPrimitives(db, data_dir=tmp_path / "data", git=GitManager())
    db.set_delivery_observer(service.delivery_observer)
    await db.update_project("p", hierarchical_integration_mode="train")
    return service, origin, base, partial, head, checkpointed, verified


async def test_completed_parent_delivery_uses_its_verified_generation(
    db, git_completed_parent,
):
    from src.integration.delivery_truth import DeliveryState

    service, origin, _base, _partial, head, checkpointed, verified = git_completed_parent
    before = await service.delivery_observer.observe(["parent"])
    assert before.get("parent").state == DeliveryState.PENDING
    assert before.get("parent").source_oid == head
    request = before.get("parent").request
    assert request.parent_completion.operation_id == checkpointed["operation_id"]
    assert request.parent_completion.generation == 6
    assert request.completion_id == "parent:" + verified["verification_id"]

    origin.land("parent")
    view = await service.delivery_observer.observe(["parent"])
    assert view.get("parent").state == DeliveryState.CONTAINED
    # Delivery truth uses the recorded verified generation without a manual
    # adoption or provenance writer, even though no leaf close exists.
    assert await db.get_task_completion("parent") is None
    async with db._engine.connect() as conn:
        assert len((await conn.execute(select(integration_check_evidence))).all()) == 1
        assert len((await conn.execute(select(integration_parent_verifications))).all()) == 1
        assert len((await conn.execute(select(integration_parent_operation_completions))).all()) == 1
    assert (await service.delivery_observer.observe(["parent"])).get("parent").satisfied


@pytest.mark.parametrize("branch", ["advanced", "deleted"])
async def test_parent_delivery_keeps_the_verified_source_when_its_ref_changes(
    db, git_completed_parent, branch,
):
    from src.integration.delivery_truth import DeliveryState
    from tests.test_delivery_consumers import git

    service, origin, _base, _partial, head, *_rest = git_completed_parent
    origin.land("parent")
    git(origin.clone, "rev-parse", "main")
    if branch == "advanced":
        assert origin.work("parent", "later") != head
    else:
        git(origin.clone, "push", "origin", "--delete", "aq/parent")
    proof = (await service.delivery_observer.observe(["parent"])).get("parent")
    assert (proof.state, proof.source_oid) == (DeliveryState.CONTAINED, head)
    assert await db.get_task_completion("parent") is None


@pytest.mark.parametrize("binding", ["partial", "cleared", "missing"])
async def test_partial_parent_binding_cannot_fall_back_to_a_retained_old_leaf_close(
    db, git_completed_parent, binding,
):
    from src.integration.delivery_truth import DeliveryState
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
    from tests.test_delivery_consumers import git

    service, origin, _base, _partial, head, *_rest = git_completed_parent
    origin.land("parent")
    git(origin.clone, "rev-parse", "main")
    await db.save_task_completion(TaskCompletion(
        id="old-leaf", task_id="parent", outcome="pass", commits=[head], completed_at=1.0,
    ))
    await GitProvenance(service.git, str(origin.clone), repository_url=origin.url).write_completion(
        CompletedSource(CompletionIdentity("p", "repo", "parent", "old-leaf"), head),
    )
    async with db.immediate() as conn:
        if binding == "missing":
            await conn.execute(delete(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "parent",
            ))
        else:
            values = dict(episode_id=None, last_completed_operation_id=None,
                          last_completed_verification_id=None)
            if binding == "cleared":
                values["current_verification_id"] = None
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "parent",
            ).values(**values))
    proof = (await service.delivery_observer.observe(["parent"])).get("parent")
    assert (proof.state, proof.reason) == (DeliveryState.UNKNOWN, "invalid_parent_completion")



async def test_parent_delivery_cannot_substitute_a_branch_when_exact_git_observation_fails(
    db, git_completed_parent, monkeypatch,
):
    from src.git.manager import GitError
    from src.integration.delivery_truth import DeliveryState
    from src.integration.provenance import GitProvenance
    from tests.test_delivery_consumers import git

    service, origin, _base, _partial, head, *_rest = git_completed_parent
    origin.land("parent")
    git(origin.clone, "rev-parse", "main")
    refs = git(origin.clone, "ls-remote", "origin")
    exact = GitProvenance.exact

    async def missing_source(store, oid):
        if oid == head:
            raise GitError("exact parent source unavailable")
        return await exact(store, oid)

    monkeypatch.setattr(GitProvenance, "exact", missing_source)
    assert (await service.delivery_observer.observe(["parent"])).get("parent").state == (
        DeliveryState.UNKNOWN
    )
    assert git(origin.clone, "ls-remote", "origin") == refs
    assert await service.rows("p") == []
    assert await db.get_task_completion("parent") is None


@pytest.mark.parametrize("delivery", ["ancestry", "equivalent"])
async def test_root_admission_uses_exact_verified_parent_delivery(
    db, git_completed_parent, delivery,
):
    from src.integration.delivery_truth import DeliveryState
    from src.integration.scheduler import TrainService
    from tests.test_delivery_consumers import git
    from tests.test_integration_sealing import _origin_row, _request, _review_row

    service, origin, base, _partial, head, _checkpointed, verified = git_completed_parent
    async with db.immediate() as conn:
        await conn.execute(update(projects).values(integration_mode="pull_request"))
        await conn.execute(update(tasks).where(tasks.c.id == "parent").values(
            pr_url="https://github.com/example/repo/pull/1",
        ))
        if not await conn.scalar(select(task_branch_origins.c.task_id).where(
            task_branch_origins.c.task_id == "parent",
        )):
            await conn.execute(insert(task_branch_origins).values(**{
                **_origin_row("parent"), "branch_name": "aq/parent", "base_sha": base,
            }))
        await conn.execute(insert(integration_review_evidence).values(**_review_row(
            "parent", head, evidence_id="parent-root-review", source_base=base,
            review_kind="parent", generation=6,
            reviewed_tree_sha=git(origin.clone, "rev-parse", head + "^{tree}"),
            evidence={"decision": "approved", "verification_id": verified["verification_id"]},
        )))
        frontier = await db.eligible_root_page_on(
            conn, project_id="p", repository_id="repo", after=None, limit=10,
        )
        assert [member["task_id"] for member in frontier] == ["parent"]
    if delivery == "ancestry":
        origin.land("parent")
    else:
        git(origin.clone, "checkout", "main")
        git(origin.clone, "merge", "--squash", "aq/parent")
        git(origin.clone, "commit", "-m", "reviewed equivalent aggregate")
        git(origin.clone, "push", "origin", "main")
    git(origin.clone, "rev-parse", "main")
    view = await service.delivery_observer.observe(["parent"])
    train = TrainService(db)
    if delivery == "equivalent":
        assert view.get("parent").state == DeliveryState.PENDING
        async with db._engine.connect() as conn:
            repository = (await conn.execute(select(repos))).mappings().one()
            assert await train._git_delivered_roots_on(conn, view, "p", repository) == set()
        # An equivalent tree is not an ancestry receipt. The retired adoption
        # control cannot turn this pending exact revision into delivery proof.
        return
    view = await service.delivery_observer.observe(["parent"])
    async with db._engine.connect() as conn:
        repository = (await conn.execute(select(repos))).mappings().one()
        assert await train._git_delivered_roots_on(conn, view, "p", repository) == {"parent"}
    for now in (10.0, 30.0):
        request = await _request(db, now=now)
        assert (await train.seal("p", request["request_id"], now + 1))["outcome"] == "empty"
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches))).all() == []
        # Only the real child-to-parent receipt, never a fabricated root receipt.
        assert len((await conn.execute(select(task_delivery_receipts))).all()) == 1
        assert len((await conn.execute(select(integration_check_evidence))).all()) == 1
    assert await db.get_task_completion("parent") is None
    await _drift_parent(db, "generation", base)
    async with db._engine.connect() as conn:
        assert await train._git_delivered_roots_on(conn, view, "p", repository) == set()


@pytest.mark.parametrize("target", ["partial", "other"])
async def test_verified_parent_requires_complete_source_on_the_requested_target(
    db, git_completed_parent, target,
):
    from src.integration.delivery_truth import DeliveryState
    from tests.test_delivery_consumers import git

    service, origin, _base, partial, _head, *_rest = git_completed_parent
    if target == "partial":
        git(origin.clone, "push", "origin", f"{partial}:refs/heads/main")
    else:
        git(origin.clone, "push", "origin", "aq/parent:refs/heads/other")
    view = await service.delivery_observer.observe(["parent"])
    assert view.get("parent").state == DeliveryState.PENDING
    assert await db.get_task_completion("parent") is None


async def _drift_parent(db, kind, base):
    if kind == "project":
        await db.create_project(Project(id="other", name="Other"))
    if kind in {"repository", "checkpoint_repository", "designated_repository", "project"}:
        await db.create_repo(RepoConfig(
            id="other", project_id="other" if kind == "project" else "p",
            source_type=RepoSourceType.CLONE,
            url=(await db.get_repo("repo")).url,
        ))
    if kind == "project":
        await db.update_project("other", integration_repository_id="other")
    async with db.immediate() as conn:
        if kind == "reopened":
            await conn.execute(update(tasks).where(tasks.c.id == "parent").values(status="IN_PROGRESS"))
        elif kind == "generation":
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "parent",
            ).values(generation=7))
        elif kind == "head":
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "parent",
            ).values(checkpoint_sha=base))
        elif kind == "verification":
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "parent",
            ).values(current_verification_id=None))
        elif kind == "operation":
            await conn.execute(update(integration_repair_operations).values(state="cancelled"))
        elif kind == "checks":
            await conn.execute(update(integration_repair_operations).values(required_check_version="new"))
        elif kind == "episode":
            await conn.execute(insert(integration_parent_episodes).values(
                id="new-episode", parent_task_id="parent", repository_id="repo",
                generation=7, pre_collection_checkpoint_sha=base, created_at=10.0,
            ))
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "parent",
            ).values(episode_id="new-episode"))
        elif kind == "completion":
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "parent",
            ).values(last_completed_operation_id=None, last_completed_verification_id=None))
        elif kind == "branch":
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "parent",
            ).values(branch="aq/other"))
        elif kind == "project":
            await conn.execute(update(tasks).where(tasks.c.id == "parent").values(project_id="other"))
        elif kind == "repository":
            await conn.execute(update(tasks).where(tasks.c.id == "parent").values(repo_id="other"))
        elif kind == "unbound_repository":
            await conn.execute(update(tasks).where(tasks.c.id == "parent").values(repo_id=None))
        elif kind == "checkpoint_repository":
            await conn.execute(update(task_integration_checkpoints).where(
                task_integration_checkpoints.c.task_id == "parent",
            ).values(repository_id="other"))
        elif kind == "designated_repository":
            await conn.execute(update(projects).where(projects.c.id == "p").values(
                integration_repository_id="other",
            ))
        elif kind == "target":
            await conn.execute(update(repos).values(default_branch="other"))
        elif kind == "new_close":
            await conn.execute(insert(task_completion_records).values(
                id="later-close", task_id="parent", outcome="pass", commits="[]",
                completed_at=10**12,
            ))
        elif kind == "reclosed":
            await db._upsert_meta("parent", "integration_rework_at", 10**12, conn=conn)
        elif kind == "incomplete_close":
            await db._upsert_meta("parent", "development_completion_id", "new-close", conn=conn)
        else:
            raise AssertionError(kind)


@pytest.mark.parametrize("kind", [
    "reopened", "generation", "head", "verification", "operation", "checks", "episode",
    "completion", "branch", "repository", "unbound_repository", "checkpoint_repository",
    "designated_repository",
    "project", "new_close", "reclosed", "incomplete_close",
])
async def test_stale_parent_binding_invalidates_observed_delivery(
    db, git_completed_parent, kind,
):
    from src.integration.delivery_truth import DeliveryState

    service, origin, base, *_rest = git_completed_parent
    origin.land("parent")
    observed = await service.delivery_observer.observe(["parent"])
    await _drift_parent(db, kind, base)
    async with db.immediate() as conn:
        assert await observed.verified_on(conn, ["parent"]) == {}
    assert (await service.delivery_observer.observe(["parent"])).get("parent").state == (
        DeliveryState.UNKNOWN
    )


async def test_completion_without_trusted_binding_names_the_missing_producer(db):
    """No trusted evidence is a wait on the CI producer, not a stale subject.

    The verifier's own aggregate validation already passed; only the trusted
    integration check evidence is absent, and no worker-side re-run can record
    it.  The refusal must say so, and must carry the exact owner action.
    """
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentEpisodeRecords(db)

    diagnosis = await completion.diagnose_trusted_binding("parent", 1, "d" * 40)
    assert diagnosis is not None
    assert diagnosis["reason"] == "verification_not_recorded"
    assert diagnosis["required_producer_id"] == "forge-observer"
    assert diagnosis["required_check_version"] == "parent-v1"
    assert diagnosis["required_check_names"] == ["unit"]

    refused = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert refused["outcome"] == AWAITING_TRUSTED_VERIFICATION
    assert refused["reason"] == "verification_not_recorded"
    assert refused["generation"] == 1
    assert refused["head_sha"] == "d" * 40
    assert refused["verification_id"] is None
    assert refused["next_owner"] == "parent_ci_producer"
    assert "aq integration status" in refused["next_action"]
    assert "durable Subject visit" in refused["next_action"]
    assert "do not re-run the local suite" in refused["next_action"]
    # A refusal completes nothing and transitions nothing.
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_parent_operation_completions))).all() == []
        assert (
            await conn.execute(
                select(integration_parent_verifications).where(
                    integration_parent_verifications.c.operation_id
                    == checkpointed["operation_id"]
                )
            )
        ).all() == []


async def test_unchanged_missing_binding_repeats_one_diagnosis(db):
    """Replaying the close on unchanged evidence cannot reach completion."""
    hierarchy, _checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentEpisodeRecords(db)

    first = await hierarchy.complete_parent("parent", 1, "d" * 40)
    second = await hierarchy.complete_parent("parent", 1, "d" * 40)
    third = await completion.diagnose_trusted_binding("parent", 1, "d" * 40)

    assert first["reason"] == second["reason"] == "verification_not_recorded"
    assert {first["reason"], second["reason"]} == {"verification_not_recorded"}
    assert third["reason"] == first["reason"]
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS


async def test_trusted_evidence_after_the_wait_completes_the_parent(db):
    """The wait ends the same way: real evidence, then ordinary completion."""
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentEpisodeRecords(db)
    assert (
        await hierarchy.complete_parent("parent", 1, "d" * 40)
    )["outcome"] == AWAITING_TRUSTED_VERIFICATION

    evidence_id = await _trusted_check(db, checkpointed)
    verified = await hierarchy.verify_parent("parent", 1, "d" * 40, [evidence_id])
    assert verified["outcome"] == "verified"
    assert await completion.diagnose_trusted_binding("parent", 1, "d" * 40) is None

    completed = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert completed["outcome"] == "completed"
    assert (await db.get_task("parent")).status is TaskStatus.COMPLETED
    assert (await hierarchy.complete_parent("parent", 1, "d" * 40))["outcome"] == (
        "already_completed"
    )


async def test_verification_of_another_head_is_a_missing_binding_not_a_stale_head(db):
    """A verification recorded against another subject is a wait, not staleness."""
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentEpisodeRecords(db)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_verifications).values(
                id="other-head-verification",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                episode_id=checkpointed["episode_id"],
                generation=1,
                head_sha="c" * 40,
                required_check_version="parent-v1",
                created_at=1.5,
            )
        )
        await conn.execute(
            update(task_integration_checkpoints)
            .where(task_integration_checkpoints.c.task_id == "parent")
            .values(
                current_verification_id="other-head-verification",
                verified_generation=1,
                verified_sha="c" * 40,
            )
        )

    diagnosis = await completion.diagnose_trusted_binding("parent", 1, "d" * 40)
    assert diagnosis["reason"] == "verified_other_head"
    assert diagnosis["verification_id"] == "other-head-verification"
    refused = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert refused["outcome"] == AWAITING_TRUSTED_VERIFICATION
    assert refused["reason"] == "verified_other_head"
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS


async def test_superseded_aggregate_head_still_answers_stale_verification(db):
    """The collected head moving on is the genuinely superseded subject."""
    hierarchy, checkpointed, children = await _verified_parent_tree(db, children=2)
    await _code_receipt(
        db, children[1], "d" * 40, "e" * 40, id="receipt-second", domain_key="delivery-second"
    )
    evidence_id = await _trusted_check(db, checkpointed, head_sha="e" * 40)
    assert (
        await hierarchy.verify_parent("parent", 1, "e" * 40, [evidence_id])
    )["outcome"] == "verified"

    # This close quotes the aggregate head as it stood before the second
    # child's receipt advanced it: a superseded subject, not missing evidence.
    refused = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert refused["outcome"] == "stale_verification"
    assert "reason" not in refused
    assert (await db.get_task("parent")).status is TaskStatus.IN_PROGRESS


async def test_recorded_green_evidence_names_the_playbook_as_the_owner(db):
    """Green CI evidence with no verification belongs to the verify rule."""
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentEpisodeRecords(db)
    await _trusted_check(db, checkpointed)

    refused = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert refused["outcome"] == AWAITING_TRUSTED_VERIFICATION
    assert refused["reason"] == "verification_not_recorded"
    assert refused["recorded_evidence_ids"] == ["check-unit"]
    assert refused["recorded_conclusions"] == ["success"]
    assert refused["next_owner"] == "parent_integration_playbook"
    assert "integration_parent_verify" in refused["next_action"]
    assert (
        await completion.diagnose_trusted_binding("parent", 1, "d" * 40)
    )["next_owner"] == "parent_integration_playbook"


async def test_recorded_failing_evidence_names_the_repair_ladder_as_the_owner(db):
    """Recorded red evidence is a failed aggregate, not a pending CI run."""
    hierarchy, checkpointed, _ = await _verified_parent_tree(db)
    completion = ParentEpisodeRecords(db)
    await _trusted_check(db, checkpointed, conclusion="failure", checks={"unit": "failure"})

    refused = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert refused["outcome"] == AWAITING_TRUSTED_VERIFICATION
    assert refused["recorded_conclusions"] == ["failure"]
    assert refused["next_owner"] == "parent_repair_ladder"
    assert "repair ladder" in refused["next_action"]
    assert (
        await completion.diagnose_trusted_binding("parent", 1, "d" * 40)
    )["next_owner"] == "parent_repair_ladder"


async def test_child_added_after_verification_makes_completion_stale(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_check_evidence).values(
                id="check-unit",
                operation_id=checkpointed["operation_id"],
                parent_task_id="parent",
                parent_generation=1,
                parent_head_sha="d" * 40,
                producer_id="forge-observer",
                workflow_id="workflow",
                run_id="run",
                attempt=1,
                required_check_version="parent-v1",
                checks={"unit": "success"},
                conclusion="success",
                classification="conclusive",
                observed_at=2.0,
            )
        )
    await hierarchy.verify_parent("parent", 1, "d" * 40, ["check-unit"])
    await hierarchy.file_children("parent", [{"title": "new defect"}], 1)

    result = await hierarchy.complete_parent("parent", 1, "d" * 40)
    assert result["outcome"] in {"waiting", "stale_verification"}


async def test_collector_to_parent_verifier_wake_advances_live_head(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn,
            "parent",
            TaskStatus.PAUSED,
            context="integration_parent_suspended",
            _manual_pause_control=True,
        )
    with pytest.raises(HierarchyError, match="guarded verifier wake"):
        await db.transition_task("parent", TaskStatus.READY, force=True)
    target = BranchKey(repository_id="repo", branch="aq/parent")
    owner = await BranchOwnership(db).get_owner(target)
    worker = Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"])
    collector = await BranchOwnership(db).transfer(
        worker, checkpointed["operation_id"], "collector"
    )
    verifier = await BranchOwnership(db).transfer(collector, "parent", "verifier")

    result = await hierarchy.wake_verifier("parent", verifier)

    assert result["outcome"] == "woken"
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["checkpoint_sha"] == "d" * 40
    assert checkpoint["branch_owner_id"] == "parent"
    assert checkpoint["state"] == "verifying"
    assert (await db.get_task("parent")).status is TaskStatus.READY


@pytest.mark.parametrize("checkpoint_state", ["integration_ready", "verifying"])
async def test_manual_resume_allows_guarded_parent_verifier_wake(db, checkpoint_state):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "parent",
        ).values(state=checkpoint_state))
    target = BranchKey(repository_id="repo", branch="aq/parent")
    ownership = BranchOwnership(db)
    owner = await ownership.get_owner(target)
    worker = Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"])
    collector = await ownership.transfer(worker, checkpointed["operation_id"], "collector")
    verifier = await ownership.transfer(collector, "parent", "verifier")
    snapshot = await db.pause_task("parent")
    await db.finish_task_pause("parent", snapshot)

    with pytest.raises(HierarchyError, match="operator manual pause is active"):
        await hierarchy.wake_verifier("parent", verifier)
    assert (await db.resume_task("parent")).status is TaskStatus.PAUSED
    with pytest.raises(HierarchyError, match="guarded verifier wake"):
        await db.transition_task("parent", TaskStatus.READY, force=True)

    result = await hierarchy.wake_verifier("parent", verifier)

    assert result["outcome"] == "woken"
    assert (await db.get_task("parent")).status is TaskStatus.READY
    checkpoint = await db.get_integration_checkpoint("parent")
    assert checkpoint["checkpoint_sha"] == "d" * 40
    assert checkpoint["state"] == "verifying"


async def test_parent_prime_summary_uses_receipt_readiness_projection(db):
    from src.prime.sections import build_integration_delivery_summary

    _hierarchy, _checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)

    summary = await build_integration_delivery_summary(db, await db.get_task("parent"))

    assert "Readiness: **ready**" in summary
    assert "Pre-collection head: `" + "a" * 40 + "`" in summary
    assert "Current aggregate head: `" + "d" * 40 + "`" in summary
    assert f"`{children[0]}`: code squash `{'d' * 40}`" in summary
    assert "Required aggregate checks: `unit`" in summary


async def test_branchless_parent_creates_unrouted_verifier_delegate_before_handoff(db):
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )
        projection = await hierarchy.parent_completion.mark_ready_on(conn, "parent")

    operation = await db.get_integration_operation(checkpointed["operation_id"])
    delegate = await db.get_task(operation["verifier_task_id"])
    assert projection["state"] == "integration_ready"
    assert delegate.parent_task_id is None
    assert delegate.status is TaskStatus.PAUSED
    assert delegate.repo_id == "repo"
    assert delegate.branch_name == "aq/parent"
    # The frozen snapshot still names ``verifier_profile_id``; it is ignored:
    # the verifier is filed with the class hint and the router routes it.
    assert operation["policy_snapshot"]["parent"]["verifier_profile_id"] == "verifier"
    assert delegate.profile_id is None
    assert delegate.intelligence_class is None
    assert delegate.class_hint == "high"
    assert delegate.route_source == "unrouted"

    async with db._engine.connect() as conn:
        ready_event = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "task.integration_ready"
                )
            )
        ).mappings().one()
    payload = dict(ready_event["payload"])
    payload.pop("event_id")
    assert payload == {
        "project_id": "p",
        "operation_id": checkpointed["operation_id"],
        "task_id": "parent",
        "title": "parent",
        "episode_id": checkpointed["episode_id"],
        "generation": 1,
        "head_sha": "d" * 40,
        "verifier_task_id": delegate.id,
        "target": {"repository_id": "repo", "branch": "aq/parent"},
        "expected_token": 1,
        "next_owner_id": delegate.id,
        "next_role": "verifier",
    }

    target = BranchKey(repository_id="repo", branch="aq/parent")
    owner = await BranchOwnership(db).get_owner(target)
    worker = Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"])
    collector = await BranchOwnership(db).transfer(
        worker, checkpointed["operation_id"], "collector"
    )
    verifier = await BranchOwnership(db).transfer(
        collector, delegate.id, "verifier"
    )
    async with db.immediate() as conn:
        replayed_projection = await hierarchy.parent_completion.mark_ready_on(
            conn, "parent"
        )
    async with db._engine.connect() as conn:
        ready_events = (
            await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.event_type == "task.integration_ready"
                )
            )
        ).mappings().all()
    assert replayed_projection["state"] == "integration_ready"
    assert len(ready_events) == 1
    assert ready_events[0]["payload"] == ready_event["payload"]
    assert (await hierarchy.wake_verifier("parent", verifier))["outcome"] == "woken"
    assert (await db.get_task("parent")).status is TaskStatus.PAUSED
    assert (await db.get_task(delegate.id)).status is TaskStatus.READY


@pytest.mark.parametrize(
    ("overrides", "blocked"),
    [
        # A stored pre-routing policy with a profile but no class blocks:
        # the profile is ignored, so nothing names the verifier's class.
        ({"verifier_intelligence_class": None}, True),
        # The class alone is a complete route request now.
        ({"verifier_profile_id": None}, False),
    ],
)
async def test_branchless_parent_verifier_needs_only_the_class_hint(db, overrides, blocked):
    hierarchy, checkpointed, children = await _parent_tree(
        db, children=1, boundary=_boundary(**overrides)
    )
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )
        projection = await hierarchy.parent_completion.mark_ready_on(conn, "parent")

    operation = await db.get_integration_operation(checkpointed["operation_id"])
    if blocked:
        assert projection["outcome"] == "configuration_blocked"
        assert projection["reason"] == "verifier_routing_missing"
        assert operation["verifier_task_id"] is None
        return
    assert projection["state"] == "integration_ready"
    delegate = await db.get_task(operation["verifier_task_id"])
    assert (delegate.profile_id, delegate.intelligence_class) == (None, None)
    assert delegate.class_hint == "high"
    assert delegate.route_source == "unrouted"


@pytest.mark.parametrize(
    "invalid", [None, "operation", "role", "attached", "branch", "binding", "target"]
)
async def test_woken_verifier_delegate_passes_the_pool_claim_origin_gate(db, invalid):
    """A verifier delegate is claimable only on its exact reserved parent fence.

    It checks the parent's branch and never gets a ``task_branch_origins`` row
    of its own, so the hierarchy origin gate must admit it by reservation, as
    it does a repair delegate.  Before that, a pool never saw it: ``aq task
    claim --next`` answered ``no_ready_work`` while the parent waited forever.
    """
    from src.database.queries.claim_queries import _frontier_where
    from src.database.queries.hierarchy_queries import ProjectIntegrationMode

    await db.create_profile(AgentProfile(id="verifier", name="Verifier", harness="claude"))
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )
        await hierarchy.parent_completion.mark_ready_on(conn, "parent")
    operation = await db.get_integration_operation(checkpointed["operation_id"])
    delegate_id = operation["verifier_task_id"]
    target = BranchKey(repository_id="repo", branch="aq/parent")
    ownership = BranchOwnership(db)
    owner = await ownership.get_owner(target)
    collector = await ownership.transfer(
        Fence(target=target, owner_id=owner["owner_id"], token=owner["fence_token"]),
        checkpointed["operation_id"],
        "collector",
    )
    verifier = await ownership.transfer(collector, delegate_id, "verifier")
    assert (await hierarchy.wake_verifier("parent", verifier))["outcome"] == "woken"
    assert (await db.get_task(delegate_id)).status is TaskStatus.READY
    # The verifier is filed unrouted; stand in for the router writing its
    # route so the pool selection below exercises only the origin gate.
    async with db.immediate() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == delegate_id)
            .values(profile_id="verifier", intelligence_class="high", route_source="router")
        )

    verifier_owner = update(integration_branch_owners).where(
        integration_branch_owners.c.owner_id == delegate_id
    )
    this_operation = update(integration_repair_operations).where(
        integration_repair_operations.c.id == operation["id"]
    )
    async with db.immediate() as conn:
        if invalid == "operation":
            await conn.execute(this_operation.values(state="completed"))
        elif invalid == "role":
            await conn.execute(verifier_owner.values(owner_role="worker"))
        elif invalid == "attached":
            await conn.execute(
                verifier_owner.values(
                    handoff_state="attached", session_id="session", workspace_id="slot"
                )
            )
        elif invalid == "branch":
            await conn.execute(verifier_owner.values(ref="aq/unrelated"))
        elif invalid == "binding":
            await conn.execute(this_operation.values(verifier_task_id="impostor"))
        elif invalid == "target":
            await conn.execute(
                update(tasks).where(tasks.c.id == "parent").values(branch_name="aq/other")
            )
        for mode in (None, ProjectIntegrationMode(True, "repo")):
            claimable = await conn.scalar(
                select(tasks.c.id).where(tasks.c.id == delegate_id, _frontier_where("p", mode))
            )
            assert (claimable == delegate_id) is (invalid is None), mode
            selected = await db.select_ready_for_profile(
                conn,
                project_id="p",
                profile_id="verifier",
                agent_id="pool-agent",
                hierarchy_mode=mode,
            )
            assert (selected == delegate_id) is (invalid is None), mode
    assert await db.is_hierarchy_task_runnable(delegate_id) is (invalid is None)


async def test_transfer_owner_replay_after_crash_still_wakes_verifier(
    command_handler_factory,
):
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p", name="integration project"))
    await db.create_profile(AgentProfile(id="verifier", name="Verifier", harness="claude"))
    hierarchy, checkpointed, children = await _parent_tree(db, children=1)
    await _code_receipt(db, children[0], "a" * 40, "d" * 40)
    async with db.immediate() as conn:
        await db._apply_transition(
            conn, "parent", TaskStatus.PAUSED, _manual_pause_control=True
        )
        await hierarchy.parent_completion.mark_ready_on(conn, "parent")
    operation = await db.get_integration_operation(checkpointed["operation_id"])
    target = BranchKey(repository_id="repo", branch="aq/parent")
    current = await BranchOwnership(db).get_owner(target)
    crashed_transfer = await BranchOwnership(db).transfer(
        Fence(target=target, owner_id=current["owner_id"], token=current["fence_token"]),
        operation["verifier_task_id"],
        "verifier",
    )

    result = await handler.execute(
        "integration_transfer_owner",
        {
            "target": target.model_dump(mode="json"),
            "expected_token": crashed_transfer.token - 1,
            "next_owner_id": operation["verifier_task_id"],
            "next_role": "verifier",
        },
    )

    assert result["outcome"] == "transferred"
    assert (await db.get_task(operation["verifier_task_id"])).status is TaskStatus.READY


async def test_sealed_batch_member_protects_descendant_mutation(db):
    await _enable_project(db)
    await db.create_task(Task(id="root", project_id="p", title="root", description=""))
    await db.create_task(
        Task(id="root.1", project_id="p", parent_task_id="root", title="child", description="")
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_batches).values(
                id="batch",
                project_id="p",
                repository_id="repo",
                request_id="request-batch",
                source_manifest_digest="sha256:" + "d" * 64,
                base_sha="a" * 40,
                lifecycle="sealing",
                integration_branch="refs/heads/integration/batch",
                current_revision=0,
                policy_snapshot={},
                artifact_snapshot={},
                cleanup_state="pending",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_review_evidence).values(
                id="review-batch",
                source_task_id="root",
                repository_id="repo",
                source_base="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                reviewer_task_id="root",
                review_kind="review",
                generation=0,
                verdict="approved",
                evidence={},
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_batch_members).values(
                batch_id="batch",
                ordinal=0,
                task_id="root",
                repository_id="repo",
                source_base_sha="a" * 40,
                reviewed_head_sha="b" * 40,
                reviewed_tree_sha="c" * 40,
                review_evidence_id="review-batch",
                review_evidence={},
            )
        )
        await conn.execute(
            update(integration_batches)
            .where(integration_batches.c.id == "batch")
            .values(lifecycle="sealed")
        )
        with pytest.raises(HierarchyError, match="sealed"):
            await db.guard_integration_mutation("root.1", "reopen", conn=conn)


@pytest.fixture(autouse=True)
def reconciler_primitive_authority(monkeypatch):
    from tests.integration_primitive_scope import authorize_root_primitives

    authorize_root_primitives(monkeypatch)
