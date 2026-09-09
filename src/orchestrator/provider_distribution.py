"""Bind triaged work to available providers before sizing pools or pushing work."""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter

from sqlalchemy import or_, select, update

from src.database.queries.blocked_state import apply_label_filters
from src.database.tables import sessions, task_metadata, task_workspace_requirements, tasks
from src.models import ProjectStatus, TaskStatus


class ProviderDistributionMixin:
    async def _distribute_triaged_tasks(self) -> int:
        # A cascade and an operator-triggered pass must see each other's
        # reservations. The conditional database write also fences claims/pins.
        lock = self.__dict__.setdefault("_provider_distribution_lock", asyncio.Lock())
        async with lock:
            return await self._distribute_triaged_tasks_locked()

    async def _distribute_triaged_tasks_locked(self) -> int:
        from src.commands.routing_commands import build_route_options, profile_provider

        if not self.config.sessions.enabled:
            return 0
        now = time.time()
        profiles = await self.db.list_profiles()
        agents = await self.db.list_agents()
        projects = {p.id: p for p in await self.db.list_projects() if p.status == ProjectStatus.ACTIVE}
        classes = self.session_spec_builder._intelligence_classes
        providers = {p.id: profile_provider(p, self.harness_registry) for p in profiles}
        options = {}
        for project_id in projects:
            rows = build_route_options(project_id, profiles, agents, self.harness_registry, classes)
            # ``build_route_options`` deliberately retains disabled profiles
            # for pinned-route diagnostics.  Distribution, however, is an
            # automatic route chooser and must never reserve work on one.
            options[project_id] = [row for row in rows if row.get("enabled", True)
                                   and row["configured_capacity"] > 0 and (
                row["lifecycle"] != "pool" or (
                    self.config.swarm.enabled
                    and not self._pool_quarantine_state(project_id, row["profile_id"], now)[0]
                )
            )]
        active_session = select(sessions.c.id).where(
            sessions.c.task_id == tasks.c.id,
            sessions.c.state.in_(("starting", "running", "draining")),
        ).exists()
        eligible = (
            tasks.c.status == TaskStatus.READY.value,
            tasks.c.is_blocked == 0,
            tasks.c.assigned_agent_id.is_(None),
            tasks.c.is_plan_subtask == 0,
            tasks.c.intelligence_class.is_not(None),
            tasks.c.intelligence_class != "",
            ~active_session,
        )
        count = 0
        async with self.db.immediate() as conn:
            automatic = {r.task_id: json.loads(r.value) for r in (await conn.execute(
                select(task_metadata.c.task_id, task_metadata.c.value).where(
                    task_metadata.c.key == "orchestration_profile_id"
                )
            ))}
            requirements = {}
            for r in (await conn.execute(select(task_workspace_requirements))):
                requirements.setdefault(r.task_id, set()).add(r.kind_id)
            candidates = (await conn.execute(apply_label_filters(
                select(tasks).where(*eligible).with_for_update(), exclude_hold=True,
            ).order_by(tasks.c.priority, tasks.c.created_at, tasks.c.id))).mappings().all()
            pending = []
            for task in candidates:
                serving = [r for r in options.get(task["project_id"], [])
                           if r["intelligence_class"] == task["intelligence_class"]
                           and (r["lifecycle"] != "pool" or not (
                               requirements.get(task["id"], set()) - {"project-repo", "vault"}
                           ))]
                if task["affinity_agent_id"]:
                    affinity = next((a for a in agents if a.id == task["affinity_agent_id"]), None)
                    if affinity is not None:
                        serving = [r for r in serving if r["profile_id"] == affinity.profile_id]
                previous = task["profile_id"]
                if previous:
                    if automatic.get(task["id"]) != previous:
                        continue
                    if any(r["profile_id"] == previous for r in serving):
                        continue
                    cleared = await conn.execute(update(tasks).where(
                        tasks.c.id == task["id"], *eligible, tasks.c.profile_id == previous,
                        select(task_metadata.c.task_id).where(
                            task_metadata.c.task_id == tasks.c.id,
                            task_metadata.c.key == "orchestration_profile_id",
                            task_metadata.c.value == json.dumps(previous),
                        ).exists(),
                    ).values(profile_id=None))
                    if cleared.rowcount != 1:
                        continue
                pending.append((task, serving))
            # Recent completed tasks break ties when work arrives one at a time;
            # queued and running bindings reserve load before workers start.
            workload = (await conn.execute(select(
                tasks.c.profile_id, tasks.c.intelligence_class, tasks.c.status,
            ).where(tasks.c.profile_id.is_not(None), or_(
                tasks.c.status.in_(("READY", "IN_PROGRESS")),
                tasks.c.updated_at >= now - 86400,
            )))).mappings().all()
            load, recent, profile_load = Counter(), Counter(), Counter()
            for row in workload:
                key = (row["intelligence_class"], providers.get(row["profile_id"], ""))
                recent[key] += 1
                if row["status"] in {"READY", "IN_PROGRESS"}:
                    load[key] += 1
                    profile_load[row["profile_id"]] += 1
            for task, serving in pending:
                if not serving:
                    continue
                def rank(row):
                    key = (task["intelligence_class"], row["provider"])
                    return (
                        not row.get("unbounded") and profile_load[row["profile_id"]] >= row["configured_capacity"],
                        load[key], recent[key],
                        profile_load[row["profile_id"]] / row["configured_capacity"],
                        -row["idle_count"], row["profile_id"],
                    )
                chosen = min(serving, key=rank)
                changed = await conn.execute(update(tasks).where(
                    tasks.c.id == task["id"], *eligible, tasks.c.profile_id.is_(None),
                    tasks.c.intelligence_class == task["intelligence_class"],
                ).values(profile_id=chosen["profile_id"]))
                if changed.rowcount != 1:
                    continue
                key = (task["intelligence_class"], chosen["provider"])
                reason = (f"orchestration: {chosen['provider']} / {chosen['profile_id']}; "
                          f"class {task['intelligence_class']}; provider queued/running={load[key]}, "
                          f"recent tasks={recent[key]}, profile capacity={chosen['configured_capacity']}")
                await self.db._upsert_meta(task["id"], "provider_route_reason", reason, conn=conn)
                await self.db._upsert_meta(task["id"], "orchestration_profile_id", chosen["profile_id"], conn=conn)
                load[key] += 1
                recent[key] += 1
                profile_load[chosen["profile_id"]] += 1
                count += 1
        return count
