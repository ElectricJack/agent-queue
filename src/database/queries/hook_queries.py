"""Hook and hook run CRUD operations."""

from __future__ import annotations

import time

from sqlalchemy import delete, insert, select, update

from src.database.tables import hook_runs, hooks
from src.models import Hook, HookRun


class HookQueryMixin:
    """Query mixin for hook and hook_run operations.  Expects ``self._engine``."""

    # --- Hooks ---

    async def create_hook(self, hook: Hook) -> None:
        """Insert a new hook definition."""
        now = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(hooks).values(
                    id=hook.id,
                    project_id=hook.project_id,
                    name=hook.name,
                    enabled=int(hook.enabled),
                    trigger=hook.trigger,
                    context_steps=hook.context_steps,
                    prompt_template=hook.prompt_template,
                    llm_config=hook.llm_config,
                    cooldown_seconds=hook.cooldown_seconds,
                    max_tokens_per_run=hook.max_tokens_per_run,
                    last_triggered_at=hook.last_triggered_at,
                    source_hash=hook.source_hash,
                    created_at=now,
                    updated_at=now,
                )
            )

    async def get_hook(self, hook_id: str) -> Hook | None:
        """Fetch a single hook by ID."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(hooks).where(hooks.c.id == hook_id))
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_hook(row)

    async def list_hooks(
        self,
        project_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[Hook]:
        """List hooks with optional project/enabled filters."""
        stmt = select(hooks)
        if project_id:
            stmt = stmt.where(hooks.c.project_id == project_id)
        if enabled is not None:
            stmt = stmt.where(hooks.c.enabled == int(enabled))
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_hook(r) for r in result.mappings().fetchall()]

    async def update_hook(self, hook_id: str, **kwargs) -> None:
        """Update arbitrary hook fields."""
        values = {}
        for key, value in kwargs.items():
            if key == "enabled":
                value = int(value)
            values[key] = value
        values["updated_at"] = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(update(hooks).where(hooks.c.id == hook_id).values(**values))

    async def delete_hook(self, hook_id: str) -> None:
        """Delete a hook and its run history."""
        async with self._engine.begin() as conn:
            await conn.execute(delete(hook_runs).where(hook_runs.c.hook_id == hook_id))
            await conn.execute(delete(hooks).where(hooks.c.id == hook_id))

    async def list_hooks_by_id_prefix(self, prefix: str) -> list[Hook]:
        """Return all hooks whose ID starts with *prefix*."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(hooks).where(hooks.c.id.like(prefix + "%")))
            return [self._row_to_hook(r) for r in result.mappings().fetchall()]

    async def delete_hooks_by_id_prefix(self, prefix: str) -> int:
        """Delete all hooks whose ID starts with *prefix*. Returns count deleted."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(hooks.c.id).where(hooks.c.id.like(prefix + "%")))
            ids = [r[0] for r in result.fetchall()]
            if not ids:
                return 0
            await conn.execute(delete(hook_runs).where(hook_runs.c.hook_id.in_(ids)))
            await conn.execute(delete(hooks).where(hooks.c.id.in_(ids)))
            return len(ids)

    @staticmethod
    def _row_to_hook(row) -> Hook:
        """Convert a database row to a Hook model."""
        return Hook(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            trigger=row["trigger"],
            context_steps=row["context_steps"],
            prompt_template=row["prompt_template"],
            llm_config=row["llm_config"],
            cooldown_seconds=row["cooldown_seconds"],
            max_tokens_per_run=row["max_tokens_per_run"],
            last_triggered_at=row["last_triggered_at"],
            source_hash=row.get("source_hash"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # --- Hook Runs ---

    async def create_hook_run(self, run: HookRun) -> None:
        """Insert a new hook run record."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(hook_runs).values(
                    id=run.id,
                    hook_id=run.hook_id,
                    project_id=run.project_id,
                    trigger_reason=run.trigger_reason,
                    event_data=run.event_data,
                    context_results=run.context_results,
                    prompt_sent=run.prompt_sent,
                    llm_response=run.llm_response,
                    actions_taken=run.actions_taken,
                    skipped_reason=run.skipped_reason,
                    tokens_used=run.tokens_used,
                    status=run.status,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                )
            )

    async def update_hook_run(self, run_id: str, **kwargs) -> None:
        """Update arbitrary hook run fields."""
        async with self._engine.begin() as conn:
            await conn.execute(update(hook_runs).where(hook_runs.c.id == run_id).values(**kwargs))

    async def get_last_hook_run(self, hook_id: str) -> HookRun | None:
        """Return the most recent run for a hook."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(hook_runs)
                .where(hook_runs.c.hook_id == hook_id)
                .order_by(hook_runs.c.started_at.desc())
                .limit(1)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_hook_run(row)

    async def list_hook_runs(
        self,
        hook_id: str,
        limit: int = 20,
    ) -> list[HookRun]:
        """Return recent runs for a hook, newest first."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(hook_runs)
                .where(hook_runs.c.hook_id == hook_id)
                .order_by(hook_runs.c.started_at.desc())
                .limit(limit)
            )
            return [self._row_to_hook_run(r) for r in result.mappings().fetchall()]

    async def list_hook_runs_by_prefix(
        self,
        hook_id_prefix: str,
        limit: int = 20,
    ) -> list[HookRun]:
        """Return recent runs for all hooks matching a prefix, newest first."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(hook_runs)
                .where(hook_runs.c.hook_id.like(hook_id_prefix + "%"))
                .order_by(hook_runs.c.started_at.desc())
                .limit(limit)
            )
            return [self._row_to_hook_run(r) for r in result.mappings().fetchall()]

    @staticmethod
    def _row_to_hook_run(row) -> HookRun:
        """Convert a database row to a HookRun model."""
        return HookRun(
            id=row["id"],
            hook_id=row["hook_id"],
            project_id=row["project_id"],
            trigger_reason=row["trigger_reason"],
            status=row["status"],
            event_data=row["event_data"],
            context_results=row["context_results"],
            prompt_sent=row["prompt_sent"],
            llm_response=row["llm_response"],
            actions_taken=row["actions_taken"],
            skipped_reason=row["skipped_reason"],
            tokens_used=row["tokens_used"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
