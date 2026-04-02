"""Hook and hook run CRUD operations."""

from __future__ import annotations

import time

from src.models import Hook, HookRun


class HookQueryMixin:
    """Query mixin for hook and hook_run operations.  Expects ``self._db``."""

    # --- Hooks ---

    async def create_hook(self, hook: Hook) -> None:
        """Insert a new hook definition."""
        now = time.time()
        await self._db.execute(
            "INSERT INTO hooks (id, project_id, name, enabled, trigger, "
            "context_steps, prompt_template, llm_config, cooldown_seconds, "
            "max_tokens_per_run, last_triggered_at, source_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (hook.id, hook.project_id, hook.name, int(hook.enabled),
             hook.trigger, hook.context_steps, hook.prompt_template,
             hook.llm_config, hook.cooldown_seconds, hook.max_tokens_per_run,
             hook.last_triggered_at, hook.source_hash, now, now),
        )
        await self._db.commit()

    async def get_hook(self, hook_id: str) -> Hook | None:
        """Fetch a single hook by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM hooks WHERE id = ?", (hook_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_hook(row)

    async def list_hooks(
        self, project_id: str | None = None, enabled: bool | None = None,
    ) -> list[Hook]:
        """List hooks with optional project/enabled filters."""
        conditions = []
        vals = []
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        if enabled is not None:
            conditions.append("enabled = ?")
            vals.append(int(enabled))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT * FROM hooks {where}", vals
        )
        rows = await cursor.fetchall()
        return [self._row_to_hook(r) for r in rows]

    async def update_hook(self, hook_id: str, **kwargs) -> None:
        """Update arbitrary hook fields."""
        sets = []
        vals = []
        for key, value in kwargs.items():
            if key == "enabled":
                value = int(value)
            sets.append(f"{key} = ?")
            vals.append(value)
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(hook_id)
        await self._db.execute(
            f"UPDATE hooks SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    async def delete_hook(self, hook_id: str) -> None:
        """Delete a hook and its run history."""
        await self._db.execute("DELETE FROM hook_runs WHERE hook_id = ?", (hook_id,))
        await self._db.execute("DELETE FROM hooks WHERE id = ?", (hook_id,))
        await self._db.commit()

    async def list_hooks_by_id_prefix(self, prefix: str) -> list[Hook]:
        """Return all hooks whose ID starts with *prefix*."""
        cursor = await self._db.execute(
            "SELECT * FROM hooks WHERE id LIKE ?", (prefix + "%",)
        )
        rows = await cursor.fetchall()
        return [self._row_to_hook(r) for r in rows]

    async def delete_hooks_by_id_prefix(self, prefix: str) -> int:
        """Delete all hooks whose ID starts with *prefix*. Returns count deleted."""
        cursor = await self._db.execute(
            "SELECT id FROM hooks WHERE id LIKE ?", (prefix + "%",)
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        await self._db.execute(
            f"DELETE FROM hook_runs WHERE hook_id IN ({placeholders})", ids
        )
        await self._db.execute(
            f"DELETE FROM hooks WHERE id IN ({placeholders})", ids
        )
        await self._db.commit()
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
            source_hash=row["source_hash"] if "source_hash" in row.keys() else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # --- Hook Runs ---

    async def create_hook_run(self, run: HookRun) -> None:
        """Insert a new hook run record."""
        await self._db.execute(
            "INSERT INTO hook_runs (id, hook_id, project_id, trigger_reason, "
            "event_data, context_results, prompt_sent, llm_response, "
            "actions_taken, skipped_reason, tokens_used, status, started_at, "
            "completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run.id, run.hook_id, run.project_id, run.trigger_reason,
             run.event_data, run.context_results, run.prompt_sent,
             run.llm_response, run.actions_taken, run.skipped_reason,
             run.tokens_used, run.status, run.started_at, run.completed_at),
        )
        await self._db.commit()

    async def update_hook_run(self, run_id: str, **kwargs) -> None:
        """Update arbitrary hook run fields."""
        sets = []
        vals = []
        for key, value in kwargs.items():
            sets.append(f"{key} = ?")
            vals.append(value)
        vals.append(run_id)
        await self._db.execute(
            f"UPDATE hook_runs SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    async def get_last_hook_run(self, hook_id: str) -> HookRun | None:
        """Return the most recent run for a hook."""
        cursor = await self._db.execute(
            "SELECT * FROM hook_runs WHERE hook_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (hook_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_hook_run(row)

    async def list_hook_runs(
        self, hook_id: str, limit: int = 20,
    ) -> list[HookRun]:
        """Return recent runs for a hook, newest first."""
        cursor = await self._db.execute(
            "SELECT * FROM hook_runs WHERE hook_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (hook_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_hook_run(r) for r in rows]

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
