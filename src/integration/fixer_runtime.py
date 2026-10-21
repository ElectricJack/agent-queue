"""Registered named-session and managed Git ports for an in-place batch fixer."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from pathlib import Path

from sqlalchemy import func, select, update

from src.agents.configuration import resolve_launch_settings
from src.database.tables import integration_batches, sessions, tasks
from src.git.manager import GitError
from src.integration.lock import BranchLock, PublishWithdrawn
from src.integration.manual_control import enabled_train_write
from src.integration.models import BranchKey
from src.models import SessionRecord
from src.sessions.env import session_db_isolation
from src.sessions.launch import launch_session
from src.sessions.provider import SessionHandle


class FixerCapacityUnavailable(ValueError):
    """No process or checkout was created; retry the same identity when due."""


class DaemonFixerRuntime:
    def __init__(self, orchestrator, *, lanes, clock=time.time):
        self.orchestrator, self.db = orchestrator, orchestrator.db
        self.git, self.lanes, self.clock = orchestrator.git, lanes, clock
        self.locks = BranchLock(self.db, clock=clock)

    async def split(self, batch_id, evidence, *, policy, authorize):
        from src.integration.fixer_batches import FixerBatches

        return await FixerBatches(self.db, clock=self.clock).split(
            self, batch_id, evidence, policy=policy, authorize=authorize,
        )

    async def lane(self, state):
        from src.integration.batches import Batch
        from src.integration.train import TrainTarget

        row = await self.db.get_integration_batch(state["batch_id"])
        batch = Batch.from_row(row)
        project = await self.db.get_project(batch.project_id)
        repo = await self.db.get_repo(batch.repository_id)
        default = "refs/heads/" + (repo.default_branch or "main").removeprefix("refs/heads/")
        kind = ("development" if project.hierarchical_integration_mode == "development"
                else "root") if batch.target_ref == default else "epic"
        step = (row["policy_snapshot"] or {}).get("promotion_step")
        if row["trigger"] == "promotion":
            kind = "promotion"
        lane = await self.lanes(TrainTarget(
            project_id=batch.project_id, repository_id=batch.repository_id,
            target_ref=batch.target_ref, kind=kind, step=step,
        ))
        return batch, lane

    async def repository(self, state):
        batch, lane = await self.lane(state)
        return await lane.service.gitops.repository(batch)

    async def failure_input(self, batch_id, evidence):
        from src.integration.batches import BatchStore
        from src.integration.failure_groups import (
            FrozenMemberFiles, group_failure_records, normalize_failure_records,
        )

        row = await self.db.get_integration_batch(batch_id)
        repo = await self.repository({"batch_id": batch_id})
        members = await BatchStore(self.db).members(batch_id)
        frozen = []
        for member in members:
            result = await self.git.arun_git_result([
                "--no-replace-objects", "diff", "--name-only", "-z", "--no-renames",
                member.source_base_sha, member.source_sha, "--",
            ], cwd=str(repo.store))
            if result.returncode:
                raise GitError("cannot read frozen member file ownership")
            frozen.append(FrozenMemberFiles(member.task_id, member.source_base_sha,
                                            member.source_sha,
                                            tuple(filter(None, result.stdout.split("\0")))))
        records, checks = [], []
        required = evidence["required"]
        for check in evidence["checks"]:
            if (check["sha"] != evidence["sha"]
                    or check["repository_id"] != row["repository_id"]
                    or check["producer_id"] != required["producer_id"]
                    or check["required_check_version"] != required["version"]
                    or check["name"] not in required["names"]
                    or check["conclusion"] not in {"failure", "timed_out", "action_required"}):
                continue
            detail = check.get("detail") or {}
            raw = {"id": detail.get("check_run_id") or check.get("run_id") or check["name"],
                   "name": check["name"], "conclusion": check["conclusion"],
                   "output": detail.get("output") or {"summary": check.get("reason") or ""}}
            logs = [{"records": [{"path": test.split("::", 1)[0],
                                   "message": "FAILED " + test, "kind": "test"}
                                  for test in detail.get("failing_test_ids", [])
                                  if isinstance(test, str)]}]
            records.extend(normalize_failure_records(raw, logs))
            # Exclude observation timestamps: an unchanged stored result is not
            # a new round merely because it was fetched again.
            checks.append({key: check.get(key) for key in (
                "name", "conclusion", "run_id", "attempt", "run_url", "reason", "detail",
            )})
        return {"head_sha": evidence["sha"], "checks": checks,
                "groups": [asdict(group) for group in group_failure_records(records, frozen)]}

    async def route(self, state, *, conn):
        """Use the project's reviewed router and live provider facts, without a task.

        The in-place agent consumes a session seat, not a pool workspace seat.
        A missing/ambiguous literal policy is a held launch, never an invented
        hard-coded worker rung or a new task sent through the claim frontier.
        """
        from src.playbooks.definition import CommandStep
        from src.playbooks.expressions import LiteralValue
        from src.playbooks.routing import _loaded_activations
        from src.routing.planner import Snapshot, TaskFacts, plan_route
        from src.routing.policy import parse_policy

        project = await self.db.get_project(state["project_id"])
        bound = project.assignment_playbook_id
        if not bound:
            raise ValueError("project has no bound routing policy")
        activations = _loaded_activations(self.orchestrator.playbook_manager, {
            "type": "task.created", "project_id": project.id,
        }, selected_ids={bound})
        policies = {}
        for _activation, artifact in activations:
            for step in artifact.steps.values():
                value = step.inputs.get("policy") if isinstance(step, CommandStep) else None
                if (isinstance(step, CommandStep) and step.command == "task_route_plan"
                        and isinstance(value, LiteralValue)):
                    policy, digest = parse_policy(value.value)
                    policies[digest] = policy
        if len(policies) != 1:
            raise ValueError("bound router must expose one reviewed literal selection policy")
        digest, policy = next(iter(policies.items()))
        commands = self.orchestrator._command_handler
        profiles, providers = await commands._routing_static_facts(
            project.id, policy.class_order, conn=conn,
        )
        busy = await self.db.count_busy_sessions_by_profile(conn=conn)
        # The normal count covers claimed workers. These task-free sessions
        # consume the same profile seats even though they never hold a claim.
        fixers = (await conn.execute(select(sessions.c.profile_id).where(
            sessions.c.state.in_(("starting", "running", "draining")),
            sessions.c.name.like("n-batch-fixer-%"), sessions.c.task_id.is_(None),
        ))).scalars().all()
        for profile_id in fixers:
            busy[profile_id] = busy.get(profile_id, 0) + 1
        snapshot = Snapshot(profiles=profiles, providers=providers, busy=busy)
        selected = plan_route(TaskFacts(
            task_id=state["batch_id"], title="Repair batch in place", task_type="bugfix",
            created_by_kind="system", on_train=True, blocks_work=True,
            preferred_provider=project.preferred_provider,
        ), policy, snapshot, policy_sha256=digest, classification={"failed": True})
        if selected.outcome == "held":
            raise FixerCapacityUnavailable("fixer provider is temporarily unavailable")
        if selected.outcome != "planned":
            raise ValueError(f"fixer routing is {selected.outcome}")
        profile_id = selected.value["profile_id"]
        facts = snapshot.profile(profile_id)
        if facts is None or busy.get(profile_id, 0) >= facts.slots:
            raise FixerCapacityUnavailable("selected fixer profile has no session capacity")
        live = (await conn.execute(select(sessions.c.project_id, sessions.c.profile_id).where(
            sessions.c.state.in_(("starting", "running", "draining")),
        ))).mappings().all()
        project_live = sum(s["project_id"] == project.id for s in live)
        if project.max_concurrent_agents and project_live >= project.max_concurrent_agents:
            raise FixerCapacityUnavailable("project has no fixer session capacity")
        cap = self.orchestrator._pool_global_cap()
        if cap and len(live) >= cap:
            raise FixerCapacityUnavailable("fleet has no fixer session capacity")
        return await self.db.get_profile(profile_id), selected.value

    async def start(self, state):
        async with enabled_train_write(self.db, state["repository_id"]):
            return await self._start(state)

    async def _start(self, state):
        orch = self.orchestrator
        if getattr(orch, "_paused", False) or not orch.config.sessions.enabled:
            raise ValueError("session launches are paused or disabled")
        previous = await self.db.get_session(state["session_id"])
        if previous is not None:
            raise ValueError("fixer launch already registered; preserve it for reconciliation")
        repo = await self.repository(state)
        # Allocation shares the fleet routing lock with session registration.
        # Previous fixers require both durable release and a stopped process.
        branch = state["target_ref"].removeprefix("refs/heads/")
        # Selection and registration share the ordinary fleet routing lock.
        # Registered starting sessions count before process creation.
        async with self.db.routing_apply_lock() as conn:
            profile, route = await self.route(state, conn=conn)
            harness = orch.harness_registry.get(profile.harness, project_id=state["project_id"])
            if harness is None:
                raise ValueError("fixer harness is not registered")
            await self.git.afetch_repository_oid(
                str(repo.store), repository=repo.binding, oid=state["starting_sha"],
                destination_ref=state["target_ref"],
            )
            from src.warm_workspaces import WarmWorkspacePool, primary_store

            store = await primary_store(self.db, self.git, state["project_id"],
                                        state["repository_id"], repo.store, state["starting_sha"])
            pool = WarmWorkspacePool(self.git, store,
                                     Path(orch.config.data_dir) / "batch-fixers")
            path = await pool.acquire(
                {"session_id": state["session_id"], "instance_token": state["instance_token"]},
                state["starting_sha"], released=self._checkout_released, branch=branch,
            )
            actual_branch, dirty = await self.git.aread_status_summary(str(path))
            if (actual_branch != branch or dirty
                    or await self.git.arev_parse(str(path), "HEAD") != state["starting_sha"]):
                raise ValueError("fixer preparation changed; preserve it for inspection")
            token = await orch.token_store.mint(
                session_id=state["session_id"], session_instance_token=state["instance_token"],
                task_id=None, project_id=state["project_id"], elevated=False,
            )
            project = await self.db.get_project(state["project_id"])
            identity = self.git.resolve_commit_identity(project)
            spec = orch.session_spec_builder.build_named_spec(
                profile=profile, harness=harness, project_id=state["project_id"],
                work_dir=str(path), session_id=state["session_id"],
                instance_token=state["instance_token"], epoch=orch.daemon_epoch,
                api_token=token, prompt=fixer_prompt(state),
            )
            env = dict(spec.env)
            for key in ("AQ_TASK_ID", "AQ_CLAIM_EPOCH"):
                env.pop(key, None)
            env.update(session_db_isolation(str(path)))
            env.update(identity.env())
            env.update(AQ_SESSION_NAME=state["session_name"], AQ_SESSION_KIND="named")
            spec = replace(spec, session_name=state["session_name"], env=env)
            row = SessionRecord(
                id=state["session_id"], project_id=state["project_id"], task_id=None,
                profile_id=profile.id, harness=harness.id, provider=orch.config.sessions.provider,
                name=state["session_name"], lifecycle="named", work_dir=str(path),
                epoch=orch.daemon_epoch, instance_token=state["instance_token"],
                started_at=self.clock(), state="starting", desired_state="running",
                session_key=state["session_id"] if harness.session_id_flag else None,
                hooks_provisioned=spec.hooks_provisioned,
                **resolve_launch_settings(profile, harness, orch.session_spec_builder),
            )
            current = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == state["batch_id"],
            ).with_for_update())).mappings().one()
            if (current["intent"] != "open" or current["fixer_state"]["status"] != "launching"
                    or current["fixer_state"]["session_id"] != row.id):
                raise PublishWithdrawn("fixer registration authority was withdrawn")
            await self.db.create_session(row, conn=conn)
            # Commit the checkout/session binding before the harness can call
            # back. A crash after start cannot lose where the fixer worked.
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == state["batch_id"],
            ).values(fixer_state={**current["fixer_state"], "work_dir": str(path),
                                  "route": route, "status": "active"}))
        current = await self.db.get_integration_batch(state["batch_id"])
        if (current["intent"] != "open" or current["fixer_state"]["status"] != "active"
                or getattr(orch, "_paused", False)):
            await self.db.update_session(row.id, state="stopped", desired_state="stopped")
            raise PublishWithdrawn("fixer launch authority was withdrawn")
        provider = orch.session_providers.create(row.provider)
        await launch_session(self.db, provider, spec, row, registered=True,
                             bus=getattr(orch, "bus", None))
        return {"work_dir": str(path), "route": route}

    async def _checkout_released(self, manifest, path, head):
        """Unknown, live and unpublished owners retain their checkout."""
        owner = manifest["owner"]
        if not owner.get("session_id") or not owner.get("instance_token"):
            return False
        session = await self.db.get_session(owner["session_id"])
        if (session is None or session.instance_token != owner["instance_token"]
                or session.work_dir != str(path) or session.state != "stopped"
                or session.desired_state != "stopped"):
            return False
        provider = self.orchestrator.session_providers.create(session.provider)
        if await provider.is_running(SessionHandle(session.name, session.provider,
                                                   session.instance_token)):
            return False
        from src.database.tables import job_workspace_pins, workspaces

        async with self.db._engine.connect() as conn:
            if await conn.scalar(select(sessions.c.id).where(
                sessions.c.work_dir == str(path), sessions.c.state != "stopped",
            ).limit(1)):
                return False
            if await conn.scalar(select(workspaces.c.id).where(
                workspaces.c.workspace_path == str(path),
                workspaces.c.enabled.is_(True) | workspaces.c.locked_by_task_id.is_not(None)
                | workspaces.c.locked_by_agent_id.is_not(None),
            ).limit(1)):
                return False
            if await conn.scalar(select(job_workspace_pins.c.job_id).join(
                workspaces, workspaces.c.id == job_workspace_pins.c.workspace_id,
            ).where(workspaces.c.workspace_path == str(path)).limit(1)):
                return False
            rows = (await conn.execute(select(integration_batches).where(
                integration_batches.c.fixer_state["session_id"].astext == session.id,
            ))).mappings().all()
        if len(rows) != 1:
            return False
        batch = rows[0]
        state = batch["fixer_state"]
        return (batch["lifecycle"] in {"promoted", "aborted", "failed", "empty"}
                and state.get("instance_token") == session.instance_token
                and state.get("work_dir") == str(path)
                and head == state.get("published_sha", state.get("starting_sha")))

    async def alive(self, state):
        row = await self.db.get_session(state["session_id"])
        if row is None or row.instance_token != state["instance_token"]:
            return False
        if row.state not in {"starting", "running", "idle", "waiting"}:
            return False
        provider = self.orchestrator.session_providers.create(row.provider)
        return await provider.is_running(SessionHandle(row.name, row.provider, row.instance_token))

    async def push(self, batch, session):
        state = batch["fixer_state"]
        repo = await self.repository(state)
        path, branch = session.work_dir, state["target_ref"].removeprefix("refs/heads/")
        if path != state.get("work_dir"):
            raise PublishWithdrawn("fixer checkout binding changed")
        actual_branch, dirty = await self.git.aread_status_summary(path)
        if actual_branch != branch:
            raise PublishWithdrawn("fixer must publish its assigned batch branch")
        if dirty:
            raise GitError("commit all batch fixes before publication")
        tip = await self.git.arev_parse(path, "HEAD")
        if tip is None:
            raise GitError("fixer has no commit to publish")
        if not await self.git.ais_ancestor(path, state["starting_sha"], tip):
            raise PublishWithdrawn("fixer must preserve its starting candidate")
        for member in state["members"]:
            if not await self.git.ais_ancestor(path, member["source_sha"], tip):
                raise PublishWithdrawn("fixer must merge every frozen member before publication")
        if await self.git.areserved_paths_in_diff(path, state["starting_sha"], tip):
            raise PublishWithdrawn("fixer changed reserved AQ paths")
        target = BranchKey(repository_id=batch["repository_id"], branch=branch)
        async with enabled_train_write(self.db, batch["repository_id"]):
            # Batch intent and session binding stay locked across the fenced push.
            async with self.db.immediate() as conn:
                current = (await conn.execute(select(integration_batches).where(
                    integration_batches.c.id == batch["id"],
                ).with_for_update())).mappings().one()
                bound = current["fixer_state"]
                live = await self.db.get_session(session.id)
                if (getattr(self.orchestrator, "_paused", False)
                        or (current["intent"] != "open" and not (
                            current["intent"] == "aborted" and
                            (current["fixer_queue"] or {}).get("status") in {"deferred", "companion"}))
                        or current["lifecycle"] in {"promoted", "failed", "empty"}
                        or not bound
                        or bound["status"] != "active"
                        or bound["target_ref"] != state["target_ref"]
                        or bound["session_id"] != session.id
                        or bound["instance_token"] != session.instance_token
                        or live is None or live.instance_token != session.instance_token
                        or live.state not in {"running", "idle", "waiting"}):
                    raise PublishWithdrawn("batch fixer authority was withdrawn")
                await conn.execute(select(tasks.c.id).where(
                    tasks.c.id.in_([member["task_id"] for member in bound["members"]]),
                ).order_by(tasks.c.id).with_for_update())
                from src.integration.batches import BatchStore

                current_batch, lane = await self.lane(state)
                members = await BatchStore(self.db).members(batch["id"])
                if (current["fixer_queue"] or {}).get("status") in {"deferred", "companion"}:
                    from src.operator_decisions import OperatorDecisions

                    authorized = (not await OperatorDecisions(self.db).holds("batch", batch["id"])
                                  and await lane.service.eligible(current_batch, members))
                else:
                    authorized = await lane.service._authorized(current_batch, members)
                if not authorized:
                    raise PublishWithdrawn("batch source or policy authority changed")
                fence = await self.locks.acquire(target, "session:" + session.id, role="repair")
                try:
                    actual_branch, dirty = await self.git.aread_status_summary(path)
                    if (actual_branch != branch or dirty
                            or await self.git.arev_parse(path, "HEAD") != tip):
                        raise PublishWithdrawn("fixer checkout changed during publication")
                    expected = await self.git.aremote_branch_head(
                        repository=repo.binding, branch=branch,
                    )
                    if expected is None:
                        raise PublishWithdrawn("batch branch disappeared")
                    if tip != expected:
                        if not await self.git.ais_ancestor(path, expected, tip):
                            raise PublishWithdrawn("fetch and retain the previously published head")
                        await self.locks.fenced_push(
                            fence, git=self.git, checkout_path=path, repository=repo.binding,
                            tip_oid=tip, expected_old_oid=expected,
                        )
                    state = {**bound, "published_sha": tip}
                    await conn.execute(update(integration_batches).where(
                        integration_batches.c.id == batch["id"],
                    ).values(fixer_state=state, updated_at=self.clock()))
                finally:
                    await self.locks.release(fence)
        train = getattr(self.orchestrator, "integration_train", None)
        if train is not None:
            train.wake(batch["project_id"], batch["repository_id"])
        return {"success": True, "outcome": "published", "head_sha": tip,
                "branch": state["target_ref"]}

    async def rebind(self, batch, session):
        """The fixer explicitly yields its clean checkout for a serialized switch.

        A crash after checkout but before commit is replayable: both old and new
        clean heads are accepted, but no unpushed commit is reset or discarded.
        Publication compares the binding again under these same row/ref locks.
        """
        from src.integration.batches import BatchStore, candidate_ref
        from src.operator_decisions import OperatorDecisions

        before = batch["fixer_state"]
        pending = before.get("pending_binding")
        if not pending:
            return {"success": True, "outcome": "already_bound"}
        repo = await self.repository(before)
        path = session.work_dir
        if path != before.get("work_dir"):
            raise PublishWithdrawn("fixer checkout binding changed")
        destination = pending["batch_id"]
        async with enabled_train_write(self.db, batch["repository_id"]):
            async with self.db.immediate() as conn:
                await conn.execute(select(func.pg_advisory_xact_lock(
                    func.hashtextextended("batch-fixer-family:" + before["family_id"], 0),
                )))
                rows = (await conn.execute(select(integration_batches).where(
                    integration_batches.c.id.in_((batch["id"], destination)),
                ).order_by(integration_batches.c.id).with_for_update())).mappings().all()
                by_id = {row["id"]: row for row in rows}
                current, next_batch = by_id[batch["id"]], by_id[destination]
                bound = current["fixer_state"]
                live = await self.db.get_session(session.id)
                if (not bound or bound["session_id"] != session.id
                        or bound.get("pending_binding") != pending
                        or next_batch["intent"] != "open" or next_batch["fixer_state"]
                        or live is None or live.instance_token != session.instance_token
                        or live.state not in {"running", "idle", "waiting"}
                        or live.desired_state != "running"
                        or getattr(self.orchestrator, "_paused", False)
                        or await OperatorDecisions(self.db).holds("batch", destination)):
                    raise PublishWithdrawn("fixer rebind authority changed")
                old_ref, new_ref = bound["target_ref"], candidate_ref(destination)
                fences = []
                try:
                    for ref in sorted((old_ref, new_ref)):
                        fences.append(await self.locks.acquire(BranchKey(
                            repository_id=batch["repository_id"],
                            branch=ref.removeprefix("refs/heads/"),
                        ), "session:" + session.id, role="repair"))
                    new_head = await self.git.aremote_branch_head(
                        repository=repo.binding, branch=new_ref.removeprefix("refs/heads/"))
                    if new_head != pending["head_sha"]:
                        raise PublishWithdrawn("requested candidate changed; wait for fresh evidence")
                    branch, dirty = await self.git.aread_status_summary(path)
                    tip = await self.git.arev_parse(path, "HEAD")
                    old_head = await self.git.aremote_branch_head(
                        repository=repo.binding, branch=old_ref.removeprefix("refs/heads/"))
                    if dirty or (branch, tip) not in {
                        (old_ref.removeprefix("refs/heads/"), old_head),
                        (new_ref.removeprefix("refs/heads/"), new_head),
                    }:
                        raise PublishWithdrawn("save and publish all fixes before rebinding")
                    if branch != new_ref.removeprefix("refs/heads/"):
                        await self.git.afetch_repository_oid(path,
                            repository=repo.binding, oid=new_head, destination_ref=new_ref)
                        await self.git.acheckout_branch(path, new_ref.removeprefix("refs/heads/"))
                    members = await BatchStore(self.db).members(destination)
                    state = {**bound, "batch_id": destination, "status": "active",
                             "target_ref": new_ref, "starting_sha": new_head, "head_sha": new_head,
                             "brief": pending["brief"], "failure_evidence": pending["evidence"],
                             "red_heads": [*bound["red_heads"], new_head],
                             "red_keys": [*bound["red_keys"], pending["evidence_key"]],
                             "members": [{"task_id": m.task_id, "source_sha": m.source_sha,
                                          "source_base_sha": m.source_base_sha} for m in members]}
                    state.pop("pending_binding", None)
                    await conn.execute(update(integration_batches).where(
                        integration_batches.c.id == current["id"],
                    ).values(fixer_state=None))
                    await conn.execute(update(integration_batches).where(
                        integration_batches.c.id == destination,
                    ).values(fixer_state=state,
                             repair_attempt_count=max(1, next_batch["repair_attempt_count"])))
                finally:
                    for fence in reversed(fences):
                        await self.locks.release(fence)
        return {"success": True, "outcome": "bound", "fixer": state}


def fixer_prompt(state):
    return (
        "You are the single in-place integration fixer for this batch. You have no task "
        "or claim. Work in this retained checkout on the existing batch branch. "
        "Do not claim, create, or close tasks, and do not prepare another worktree. "
        "Resolve conflicts, generated-file drift and red checks here, retaining every "
        "frozen member. Run the repository's focused checks, commit the fixes, then run "
        "`aq integration batch-fix --action push`. This is your publication command; "
        "the train publishes to the delivery target after trusted checks are green. "
        "After pushing, end your turn. New CI evidence arrives through your inbox; "
        "continue in this same session and checkout. Do not poll or schedule wakes. "
        "On a rebind request save and push your current work, then use "
        "`aq integration batch-fix --action rebind`; never switch branches yourself. "
        "On a green message stop editing. On a blocker report it once to the project "
        "supervisor and preserve your work. Budget expiry does not create a successor.\n\n"
        "Frozen batch input:\n" + json.dumps(state, indent=2, sort_keys=True)
    )
