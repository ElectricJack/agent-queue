"""Operational commands for the sole Playbooks V2 runtime."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any


def _last_run_summary(run: Any | None) -> dict[str, Any] | None:
    """The listing's view of one run snapshot, or ``None`` if it never ran."""
    if run is None:
        return None
    lifecycle = run.lifecycle
    return {
        "run_id": run.run_id,
        "status": lifecycle.value if hasattr(lifecycle, "value") else str(lifecycle),
        "started_at": getattr(run, "started_at", None),
        "completed_at": getattr(run, "completed_at", None),
        "tokens_used": int(getattr(getattr(run, "budget", None), "total_tokens", 0) or 0),
    }


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

        if not dry_run:
            prepared = await self._prepare_manual_event(event)
            if isinstance(prepared, str):
                return {"error": prepared}
            event = prepared
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
            # Match the ordinary dispatch path exactly: a trigger alone is
            # not admission.  In particular, the default pipeline's review
            # guards prevent no-code/review tasks and absent PR context from
            # reaching command steps.
            if engine._rule_selected(rule, event_type, event)
        ]
        if not rules:
            return {"error": f"No rule in '{playbook_id}' matches event '{event_type}'"}
        outcomes = [await engine.run_rule(ref, rule.id, event, principal) for rule in rules]
        runs = [self._manual_run_summary(outcome, rule.id) for outcome, rule in zip(outcomes, rules)]
        failed_steps = [
            failure
            for run in runs
            for failure in run["failed_steps"]
        ]
        return {
            "run_id": outcomes[0].run_id,
            "run_ids": [outcome.run_id for outcome in outcomes],
            "playbook_id": playbook_id,
            "version": ref.version,
            "status": outcomes[0].lifecycle.value,
            "runs": runs,
            # A completed lifecycle only means the graph reached a terminal
            # node.  A rule may intentionally handle a failed command edge,
            # so retain that lifecycle while making the failed effects
            # impossible for an operator to miss in the manual-run result.
            "failed_steps": failed_steps,
        }

    @staticmethod
    def _manual_run_summary(outcome: Any, rule_id: str) -> dict[str, Any]:
        """Project one manual rule outcome without exposing receipt inputs.

        Receipts intentionally redact most inputs/results.  The step id,
        engine outcome and already-sanitised diagnostic are enough to explain
        a handled failure without weakening that projection boundary.
        """
        failed_steps = [
            {
                "run_id": outcome.run_id,
                "rule_id": receipt.rule_id,
                "step_id": receipt.step_id,
                "outcome": receipt.error_code or receipt.outcome,
                "error": receipt.error,
            }
            for receipt in outcome.receipts
            if receipt.receipt_kind == "step"
            and (receipt.outcome == "failure" or receipt.error_code is not None)
        ]
        return {
            "run_id": outcome.run_id,
            "rule_id": rule_id,
            "status": outcome.lifecycle.value,
            "outcome": outcome.outcome,
            "failed_steps": failed_steps,
        }

    async def _prepare_manual_event(self, event: dict[str, Any]) -> dict[str, Any] | str:
        """Authenticate task context before a manual task-event run writes.

        Ordinary event dispatch calls ``PlaybookEngine._hydrate_event`` before
        selecting a rule.  Manual runs used to bypass that path, leaving
        ``event.task`` absent and allowing a graph to take its handled failure
        edge after every command input had failed.  Manual task events now use
        the database task as their identity authority before looking up an
        artifact or starting a run.  A supplied task snapshot remains useful
        for replay, but only after its identity agrees with that authority.
        """
        from src.commands.principal import current_principal

        prepared = dict(event)
        event_type = str(prepared.get("_event_type") or prepared.get("type") or "")
        if not event_type.startswith("task."):
            return prepared

        task_id = prepared.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return "event.task_id is required for manual task events"
        task_id = task_id.strip()
        prepared["task_id"] = task_id

        task = await self.db.get_task(task_id)
        if task is None:
            return "event.task_id does not name an accessible task"
        project_id = getattr(task, "project_id", None)
        if not isinstance(project_id, str) or not project_id:
            return "event.task_id resolved to a task without project_id"

        supplied_project_id = prepared.get("project_id")
        if supplied_project_id is not None and supplied_project_id != project_id:
            return "event.project_id does not match event.task_id"
        principal = current_principal()
        principal_project_id = getattr(principal, "project_id", None)
        if principal_project_id is not None and principal_project_id != project_id:
            return "out of scope: project_id mismatch"
        prepared["project_id"] = project_id
        # Task emitters always carry this top-level summary.  It is needed by
        # the shipped review title and makes ``{type, task_id}`` a useful
        # minimal authenticated manual replay rather than a delayed template
        # resolution failure.
        prepared.setdefault("title", getattr(task, "title", ""))

        engine = self._v2_engine()
        snapshot = prepared.get("task")
        if snapshot is not None:
            if not isinstance(snapshot, dict):
                return "event.task must be an object"
            snapshot_task_id = snapshot.get("id")
            if snapshot_task_id is not None and snapshot_task_id != task_id:
                return "event.task.id does not match event.task_id"
            snapshot_project_id = snapshot.get("project_id")
            if snapshot_project_id is not None and snapshot_project_id != project_id:
                return "event.task.project_id does not match event.project_id"
            # This is a valid frozen event snapshot.  Do not replace fields
            # such as a historic branch/PR state with a later database row.
            return self._with_manual_replay_id(await engine._hydrate_event(prepared))

        hydrated = await engine._hydrate_event(prepared)
        if not isinstance(hydrated.get("task"), dict):
            # Do not let a graph turn this into an apparently-successful
            # handled failure.  The path names the missing context precisely.
            return "event.task could not be hydrated from event.task_id"
        return self._with_manual_replay_id(hydrated)

    @staticmethod
    def _with_manual_replay_id(event: dict[str, Any]) -> dict[str, Any]:
        """Give an otherwise-unidentified manual task replay a stable identity.

        Event-bus deliveries already have an ``event_id``.  A CLI replay
        commonly does not, and the engine correctly treats an id-less *live*
        event as a fresh dispatch.  The manual surface is different: submitting
        the same authenticated task context again is a replay.  Callers can
        still request a distinct manual run by providing their own event id.
        """
        if event.get("event_id"):
            return event
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        event["event_id"] = "manual:" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
        return event

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
        requested_scope = str(args.get("scope") or "").strip()
        engine = self._v2_engine()
        selected = [
            dict(raw_row)
            for raw_row in rows
            if not requested_scope or dict(raw_row).get("scope") == requested_scope
        ]
        # The listing surfaces show "last run" and a live running count per
        # definition; both come from the run table, not from the activation.
        ids = [row["playbook_id"] for row in selected]
        latest_runs = await self.db.latest_run_per_playbook(ids)
        active_counts = await self.db.count_active_runs_per_playbook(ids)
        playbooks: list[dict[str, Any]] = []
        for row in selected:
            artifact = engine.services.artifact_store.load(row["active_artifact_sha256"])
            compiled_at = artifact.compiled_at
            playbooks.append({
                "id": row["playbook_id"],
                "scope": row["scope"],
                "scope_identifier": row.get("scope_identifier") or "",
                "triggers": list(dict.fromkeys(
                    rule.trigger.event_type for rule in artifact.rules
                )),
                "version": artifact.version,
                "compiled_at": (
                    compiled_at.isoformat()
                    if hasattr(compiled_at, "isoformat")
                    else str(compiled_at)
                ),
                "node_count": len(artifact.steps),
                "status": row.get("health") or "active",
                "enabled": bool(row.get("enabled", True)),
                "running_count": active_counts.get(row["playbook_id"], 0),
                "last_run": _last_run_summary(latest_runs.get(row["playbook_id"])),
            })
        return {"playbooks": playbooks, "count": len(playbooks)}

    async def _cmd_list_playbook_runs(self, args: dict) -> dict:
        limit = int(args.get("limit", 50))
        runs = await self.db.list_runs(
            playbook_id=args.get("playbook_id"), lifecycle=args.get("status"), limit=limit
        )
        engine = self._v2_engine()
        versions: dict[str, int] = {}
        summaries: list[dict[str, Any]] = []
        for run in runs:
            artifact_sha = str(run.artifact_sha256)
            if artifact_sha not in versions:
                versions[artifact_sha] = engine.services.artifact_store.load(artifact_sha).version
            status = run.lifecycle.value if hasattr(run.lifecycle, "value") else str(run.lifecycle)
            tokens_used = int(getattr(getattr(run, "budget", None), "total_tokens", 0) or 0)
            completed_at = getattr(run, "completed_at", None)
            started_at = getattr(run, "started_at", None)
            summaries.append({
                "run_id": run.run_id,
                "playbook_id": run.playbook_id,
                "playbook_version": versions[artifact_sha],
                "status": status,
                "current_node": getattr(run, "current_step_id", None),
                "tokens_used": tokens_used,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_seconds": (
                    max(0.0, completed_at - started_at)
                    if completed_at is not None and started_at is not None
                    else None
                ),
                "error": getattr(run, "error", None),
            })
        return {"runs": summaries, "count": len(summaries)}

    async def _cmd_get_playbook_source(self, args: dict) -> dict:
        from src.playbooks.definition import source_digest

        playbook_id = str(args.get("playbook_id") or "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}
        rows = await self.db.list_playbook_activations(enabled_only=False)
        row = next((dict(item) for item in rows if dict(item)["playbook_id"] == playbook_id), None)
        if row is None:
            return {"error": f"Playbook '{playbook_id}' not found"}
        configured = getattr(self.config, "vault_root", None)
        vault_root = Path(configured or (Path(self.config.data_dir) / "vault")).resolve()
        scope = row["scope"]
        identifier = row.get("scope_identifier") or ""
        if scope == "system":
            path = vault_root / "system" / "playbooks" / f"{playbook_id}.md"
        elif scope == "project":
            path = vault_root / "projects" / identifier / "playbooks" / f"{playbook_id}.md"
        elif scope == "agent_type":
            path = vault_root / "agent-types" / identifier / "playbooks" / f"{playbook_id}.md"
        else:
            return {"error": f"Unsupported playbook scope: {scope}"}
        resolved = path.resolve()
        try:
            resolved.relative_to(vault_root)
        except ValueError:
            return {"error": f"Playbook source path escapes vault: {playbook_id}"}
        if not resolved.is_file():
            return {"error": f"Playbook source not found: {playbook_id}"}
        markdown = resolved.read_text(encoding="utf-8")
        return {
            "playbook_id": playbook_id,
            "path": str(resolved),
            "markdown": markdown,
            "source_hash": source_digest(markdown),
        }

    async def _cmd_update_playbook_source(self, args: dict) -> dict:
        """Atomically save Markdown, compile a V2 artifact, and activate it.

        A failed compile leaves the newly saved source in the vault for the
        author to repair while the previous immutable artifact remains active.
        There is no V1 manager or compiled-registry fallback in this path.
        """
        import os
        import tempfile

        from src.commands.principal import current_principal
        from src.playbooks.activation import profile_fingerprint
        from src.playbooks.authoring import PlaybookSource, SourceError
        from src.playbooks.definition import canonical_bytes, source_digest
        from src.playbooks.pipeline_lowering import lower_pipeline
        from src.playbooks.proposal import propose
        from src.playbooks.run_state import PlaybookStorageError

        playbook_id = str(args.get("playbook_id") or "").strip()
        markdown = args.get("markdown")
        expected_hash = str(args.get("expected_source_hash") or "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}
        if not isinstance(markdown, str) or not markdown:
            return {"error": "markdown is required"}

        rows = [
            dict(row)
            for row in await self.db.list_playbook_activations(enabled_only=False)
            if dict(row).get("playbook_id") == playbook_id
        ]
        if not rows:
            return {"error": f"Playbook '{playbook_id}' not found"}
        row = rows[0]
        configured = getattr(self.config, "vault_root", None)
        vault_root = Path(configured or (Path(self.config.data_dir) / "vault")).resolve()
        scope = row["scope"]
        identifier = row.get("scope_identifier") or ""
        if scope == "system":
            path = vault_root / "system" / "playbooks" / f"{playbook_id}.md"
        elif scope == "project":
            path = vault_root / "projects" / identifier / "playbooks" / f"{playbook_id}.md"
        elif scope == "agent_type":
            path = vault_root / "agent-types" / identifier / "playbooks" / f"{playbook_id}.md"
        else:
            return {"error": f"Unsupported playbook scope: {scope}"}
        path = path.resolve()
        try:
            path.relative_to(vault_root)
        except ValueError:
            return {"error": f"Playbook source path escapes vault: {playbook_id}"}
        if not path.is_file():
            return {"error": f"Playbook source not found: {playbook_id}"}

        try:
            current = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"error": f"Failed to read current source: {exc}"}
        current_hash = source_digest(current)
        if expected_hash and current_hash != expected_hash:
            return {
                "playbook_id": playbook_id,
                "source_hash": current_hash,
                "compiled": False,
                "error": "conflict",
                "reason": "vault_changed_underneath",
                "current_source_hash": current_hash,
                "expected_source_hash": expected_hash,
            }

        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{playbook_id}.", suffix=".md.tmp", dir=str(path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(markdown)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            return {"error": f"Failed to write source: {exc}"}

        digest = source_digest(markdown)
        loaded = PlaybookSource.load(path, vault_root=vault_root)
        if isinstance(loaded, SourceError):
            return {
                "playbook_id": playbook_id,
                "source_hash": digest,
                "compiled": False,
                "errors": list(loaded.errors),
                "retries_used": 0,
            }
        if loaded.frontmatter.get("id") != playbook_id:
            return {
                "playbook_id": playbook_id,
                "source_hash": digest,
                "compiled": False,
                "errors": ["frontmatter id must match the playbook being edited"],
                "retries_used": 0,
            }

        contracts, profiles, events = await self._v2_lookups()
        kind = str(loaded.frontmatter.get("kind") or "")
        if kind == "pipeline":
            body, lowering_diagnostics = lower_pipeline(loaded, contracts=contracts)
        else:
            body, lowering_diagnostics = {}, []
        if not body:
            errors = [diagnostic.message for diagnostic in lowering_diagnostics]
            if not errors:
                errors = ["prose playbook requires a compiler-agent proposal"]
            return {
                "playbook_id": playbook_id,
                "source_hash": digest,
                "compiled": False,
                "errors": errors,
                "retries_used": 0,
            }

        store = self._v2_engine().services.artifact_store
        baseline = None
        active_sha = row.get("active_artifact_sha256")
        if active_sha:
            try:
                baseline = store.load(active_sha)
            except (OSError, ValueError, PlaybookStorageError) as exc:
                return {
                    "playbook_id": playbook_id,
                    "source_hash": digest,
                    "compiled": False,
                    "errors": [f"active V2 artifact could not be loaded: {exc}"],
                    "retries_used": 0,
                }
        proposal = propose(
            loaded,
            body,
            baseline=baseline,
            contracts=contracts,
            profiles=profiles,
            events=events,
            version=(baseline.version + 1) if baseline is not None else 1,
            enforce_inventory=False,
        )
        diagnostics = [*lowering_diagnostics, *proposal.diagnostics]
        errors = [
            diagnostic.message
            for diagnostic in diagnostics
            if diagnostic.severity in {"error", "question"}
        ]
        if proposal.artifact is None or errors:
            return {
                "playbook_id": playbook_id,
                "source_hash": digest,
                "compiled": False,
                "errors": errors or ["V2 compiler did not produce an artifact"],
                "retries_used": 0,
            }

        artifact = proposal.artifact
        artifact_sha = proposal.artifact_sha256
        if artifact_sha is None:
            return {
                "playbook_id": playbook_id,
                "source_hash": digest,
                "compiled": False,
                "errors": ["V2 compiler did not hash the artifact"],
                "retries_used": 0,
            }
        artifact_scope, artifact_identifier = self._v2_scope(artifact)
        if (artifact_scope, artifact_identifier) != (scope, identifier):
            return {
                "playbook_id": playbook_id,
                "source_hash": digest,
                "compiled": False,
                "errors": [
                    (
                        "frontmatter scope must match the installed activation "
                        f"({scope}:{identifier or '<root>'})"
                    )
                ],
                "retries_used": 0,
            }
        artifact_bytes = canonical_bytes(artifact)
        aggregate_profiles = profile_fingerprint(dict(artifact.compiled_against.profiles))
        validation = json.dumps(
            {
                "errors": [],
                "diagnostics": [asdict(diagnostic) for diagnostic in diagnostics],
                "save_and_compile": True,
            },
            default=str,
            sort_keys=True,
            separators=(",", ":"),
        )
        remove_file_on_failure = False
        try:
            async with self.db.artifact_hash_lock([artifact_sha]) as conn:
                existing_row = await self.db.get_playbook_artifact_row(artifact_sha, conn=conn)
                file_existed = store.exists(artifact_sha)
                remove_file_on_failure = existing_row is None and not file_existed
                ref = store.put(
                    artifact,
                    source_digest=proposal.source_digest,
                    contract_fingerprint=proposal.contract_fingerprint,
                    profile_fingerprint=aggregate_profiles,
                    compiler_build=proposal.compiler_build,
                    version=artifact.version,
                )
                await self.db.upsert_playbook_artifact(
                    ref,
                    scope=artifact_scope,
                    scope_identifier=artifact_identifier,
                    profile_fingerprint=aggregate_profiles,
                    path=store.path_for(ref.artifact_sha256),
                    size_bytes=len(artifact_bytes),
                    validation=validation,
                    conn=conn,
                )
        except BaseException as exc:
            if remove_file_on_failure:
                store.delete(artifact_sha)
            if not isinstance(exc, Exception):
                raise
            return {
                "playbook_id": playbook_id,
                "source_hash": digest,
                "compiled": False,
                "errors": [f"V2 artifact persistence failed: {exc}"],
                "retries_used": 0,
            }

        principal = current_principal()
        actor = principal.describe() if principal is not None else "local"
        try:
            await self.db.set_playbook_activation(
                playbook_id=playbook_id,
                scope=artifact_scope,
                scope_identifier=artifact_identifier,
                artifact_sha256=ref.artifact_sha256,
                enabled=bool(row.get("enabled", True)),
                activated_by=actor,
                health="ready" if row.get("enabled", True) else "disabled",
                reasons="[]",
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return {
                "playbook_id": playbook_id,
                "source_hash": digest,
                "compiled": False,
                "errors": [f"V2 activation failed: {exc}"],
                "retries_used": 0,
            }

        return {
            "playbook_id": playbook_id,
            "source_hash": digest,
            "compiled": True,
            "version": artifact.version,
            "node_count": len(artifact.steps),
            "scope": artifact_scope,
            "triggers": list(dict.fromkeys(rule.trigger.event_type for rule in artifact.rules)),
            "retries_used": 0,
        }

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
