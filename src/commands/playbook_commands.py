"""Operational commands for the sole Playbooks V2 runtime."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any


class PlaybookCommandsMixin:
    def _v2_engine(self):
        from src.playbooks.services import build_v2_engine

        return build_v2_engine(
            config=self.config,
            db=self.db,
            handler=self,
            llm=getattr(self.orchestrator, "llm", None),
            bus=getattr(self.orchestrator, "bus", None),
        )

    async def _v2_artifact_for(self, playbook_id: str, project_id: str | None = None):
        from src.playbooks.services import DatabaseActivationSource

        return await DatabaseActivationSource(self.db).artifact_for(
            playbook_id, scope_identifier=project_id
        )

    @staticmethod
    def _event(args: dict[str, Any], default_type: str) -> dict[str, Any] | str:
        event = args.get("event") or {}
        if isinstance(event, str):
            try:
                event = json.loads(event)
            except (json.JSONDecodeError, TypeError):
                return f"Invalid event JSON: {event}"
        if not isinstance(event, dict):
            return "event must be a JSON object (dict)"
        event = dict(event)
        event.setdefault("type", default_type)
        event.setdefault("_event_type", event["type"])
        return event

    async def _run_v2_artifact(
        self, playbook_id: str, event: dict[str, Any], *, dry_run: bool = False,
        invoke_ai: bool = False,
    ) -> dict[str, Any]:
        from src.commands.principal import ExecutionPrincipal, current_principal

        ref = await self._v2_artifact_for(playbook_id, event.get("project_id"))
        if ref is None:
            return {"error": f"No ready V2 artifact is active for '{playbook_id}'"}
        engine = self._v2_engine()
        principal = current_principal() or ExecutionPrincipal.service("playbook-command")
        if dry_run:
            limits = self.config.playbooks
            tree = await engine.dry_run(
                ref, event, principal, invoke_ai=invoke_ai,
                max_paths=limits.v2_dry_run_max_paths,
                max_step_visits=limits.v2_dry_run_max_step_visits,
            )
            return {"dry_run": True, "playbook_id": playbook_id, **asdict(tree)}

        artifact = engine.services.artifact_store.load(ref.artifact_sha256)
        event_type = engine._event_type(event)
        rules = [
            rule for rule in artifact.rules
            if engine._trigger_matches(rule, event_type, event)
        ]
        if not rules:
            return {"error": f"No rule in '{playbook_id}' matches event '{event_type}'"}
        outcomes = [await engine.run_rule(ref, rule.id, event, principal) for rule in rules]
        return {
            "run_id": outcomes[0].run_id,
            "run_ids": [outcome.run_id for outcome in outcomes],
            "playbook_id": playbook_id,
            "version": ref.version,
            "status": outcomes[0].lifecycle.value,
        }

    async def _cmd_run_playbook(self, args: dict) -> dict:
        playbook_id = str(args.get("playbook_id") or "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}
        event = self._event(args, "manual")
        if isinstance(event, str):
            return {"error": event}
        return await self._run_v2_artifact(playbook_id, event)

    async def _cmd_dry_run_playbook(self, args: dict) -> dict:
        playbook_id = str(args.get("playbook_id") or "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}
        event = self._event(args, "dry_run")
        if isinstance(event, str):
            return {"error": event}
        return await self._run_v2_artifact(
            playbook_id, event, dry_run=True, invoke_ai=bool(args.get("invoke_ai", False))
        )

    async def _cmd_resume_playbook(self, args: dict) -> dict:
        from src.commands.principal import ExecutionPrincipal, current_principal
        from src.playbooks.engine import HumanDecision
        from src.playbooks.services import load_v2_snapshot

        run_id = str(args.get("run_id") or "").strip()
        if not run_id:
            return {"error": "run_id is required"}
        if await load_v2_snapshot(self.db, run_id) is None:
            return {"error": f"Playbooks V2 run '{run_id}' not found"}
        decision = str(args.get("human_input") or args.get("decision") or "continue")
        principal = current_principal() or ExecutionPrincipal.service("playbook-command")
        outcome = await self._v2_engine().resume(
            run_id, HumanDecision(decision=decision, payload=dict(args)), principal
        )
        return {"run_id": run_id, "status": outcome.lifecycle.value, "outcome": outcome.outcome}

    async def _cmd_cancel_playbook_run(self, args: dict) -> dict:
        from src.commands.principal import ExecutionPrincipal, current_principal
        from src.playbooks.services import load_v2_snapshot

        run_id = str(args.get("run_id") or "").strip()
        if not run_id:
            return {"error": "run_id is required"}
        if await load_v2_snapshot(self.db, run_id) is None:
            return {"error": f"Playbooks V2 run '{run_id}' not found"}
        principal = current_principal() or ExecutionPrincipal.service("playbook-command")
        outcome = await self._v2_engine().cancel(run_id, principal)
        return {"run_id": run_id, "status": outcome.lifecycle.value, "outcome": outcome.outcome}

    async def _cmd_list_playbooks(self, args: dict) -> dict:
        rows = await self.db.list_playbook_activations(enabled_only=False)
        return {"playbooks": [dict(row) for row in rows], "count": len(rows)}

    async def _cmd_list_playbook_runs(self, args: dict) -> dict:
        limit = int(args.get("limit", 50))
        runs = await self.db.list_runs(
            playbook_id=args.get("playbook_id"), lifecycle=args.get("status"), limit=limit
        )
        return {"runs": [asdict(run) for run in runs], "count": len(runs)}

    async def _cmd_inspect_playbook_run(self, args: dict) -> dict:
        run_id = str(args.get("run_id") or "").strip()
        run = await self.db.load_run(run_id) if run_id else None
        if run is None:
            return {"error": f"Playbooks V2 run '{run_id}' not found"}
        return {"run": asdict(run)}

    async def _cmd_show_playbook_graph(self, args: dict) -> dict:
        return await self._cmd_playbook_v2_graph(args)

    async def _cmd_playbook_graph_view(self, args: dict) -> dict:
        return await self._cmd_playbook_v2_graph(args)

    async def _cmd_playbook_health(self, args: dict) -> dict:
        return await self._cmd_playbook_activation_health(args)

    async def check_paused_playbook_timeouts(self) -> list[dict]:
        from src.commands.principal import ExecutionPrincipal
        from src.playbooks.engine import WaitScheduler

        resumed = await WaitScheduler(
            self._v2_engine(), self.db, ExecutionPrincipal.service("playbook-timeout")
        ).tick(time.time(), limit=100)
        return [{"run_id": run_id, "status": "resumed"} for run_id in resumed]
