"""Playbook commands mixin — compile, run, pause, resume, health."""

from __future__ import annotations

import json
import logging
import time

from src.logging_config import CorrelationContext

logger = logging.getLogger(__name__)


class PlaybookCommandsMixin:
    """Playbook command methods mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # Playbook commands (spec §15)
    # ------------------------------------------------------------------

    async def _cmd_list_playbooks(self, args: dict) -> dict:
        """List all playbooks across scopes with status and last run info.

        Returns every active compiled playbook with its scope, triggers,
        compilation metadata, cooldown state, and most recent run info
        from the database.  Supports optional filtering by scope and by
        project.

        When ``project_id`` is provided (e.g. when the command is invoked
        from a project-specific Discord channel), project-scoped playbooks
        belonging to a *different* project are excluded.  System-scoped
        and agent-type-scoped playbooks are always included because they
        apply across projects.

        Args:
            scope: Optional — filter by scope type
                (``system``, ``project``, ``agent-type``).
            project_id: Optional — when set, restrict project-scoped
                playbooks to those belonging to this project.
        """
        pm = getattr(self.orchestrator, "playbook_manager", None)
        if pm is None:
            return {"error": "Playbook manager is not initialised"}

        scope_filter = args.get("scope")
        valid_scopes = {"system", "project", "agent-type"}
        if scope_filter and scope_filter not in valid_scopes:
            return {
                "error": (
                    f"Invalid scope '{scope_filter}'. Valid: {', '.join(sorted(valid_scopes))}"
                )
            }

        project_filter = args.get("project_id")
        if project_filter is not None:
            project_filter = str(project_filter).strip() or None

        playbooks_data = []
        for pb_id, pb in sorted(pm.active_playbooks.items()):
            # Parse scope for filtering
            scope_enum, scope_detail = pb.parse_scope()
            if scope_filter and scope_enum.value != scope_filter:
                continue

            # Trigger event types
            triggers = [t.event_type if hasattr(t, "event_type") else str(t) for t in pb.triggers]

            # Scope identifier (project_id for project-scoped playbooks)
            scope_id = pm.get_scope_identifier(pb_id)

            # Project filter: exclude project-scoped playbooks that do not
            # belong to the requested project.  Non-project scopes pass
            # through untouched (they apply across all projects).
            if project_filter and scope_enum.value == "project":
                if scope_id != project_filter:
                    continue

            # In-flight runs
            running_runs = pm.get_runs_for_playbook(pb_id)

            # Cooldown state
            cooldown_remaining = pm.get_cooldown_remaining(pb_id, pb.scope)

            # Last run from database
            last_run_info: dict | None = None
            try:
                runs = await self.db.list_playbook_runs(
                    playbook_id=pb_id,
                    limit=1,
                )
                if runs:
                    r = runs[0]
                    last_run_info = {
                        "run_id": r.run_id,
                        "status": r.status,
                        "started_at": r.started_at,
                        "completed_at": r.completed_at,
                        "tokens_used": r.tokens_used,
                    }
            except Exception:
                pass  # DB unavailable — skip last run info

            entry: dict = {
                "id": pb_id,
                "scope": pb.scope,
                "triggers": triggers,
                "version": pb.version,
                "compiled_at": pb.compiled_at,
                "node_count": len(pb.nodes),
                "status": "active",
                "running_count": len(running_runs),
            }
            if scope_id:
                entry["scope_identifier"] = scope_id
            if scope_detail:
                entry["agent_type"] = scope_detail
            if pb.cooldown_seconds:
                entry["cooldown_seconds"] = pb.cooldown_seconds
                if cooldown_remaining > 0:
                    entry["cooldown_remaining"] = round(cooldown_remaining, 1)
            if pb.max_tokens:
                entry["max_tokens"] = pb.max_tokens
            if last_run_info:
                entry["last_run"] = last_run_info

            playbooks_data.append(entry)

        return {
            "playbooks": playbooks_data,
            "count": len(playbooks_data),
        }

    async def _cmd_list_playbook_runs(self, args: dict) -> dict:
        """List recent playbook runs with status and path taken.

        Returns summary data for recent playbook runs including the path
        taken through the graph (compact node trace with node IDs and
        statuses).  Supports filtering by playbook_id and/or status
        (e.g. ``"paused"`` to find runs awaiting human review).  Returns
        newest first.

        Roadmap 5.5.5.

        Args:
            playbook_id: Optional — filter to a specific playbook.
            status: Optional — filter by run status
                (``running``, ``paused``, ``completed``, ``failed``, ``timed_out``).
            limit: Optional — max results (default 20).
        """
        playbook_id = args.get("playbook_id")
        status = args.get("status")
        limit = int(args.get("limit", 20))

        valid_statuses = {"running", "paused", "completed", "failed", "timed_out"}
        if status and status not in valid_statuses:
            return {
                "error": f"Invalid status '{status}'. Valid: {', '.join(sorted(valid_statuses))}"
            }

        runs = await self.db.list_playbook_runs(
            playbook_id=playbook_id,
            status=status,
            limit=limit,
        )
        return {
            "runs": [self._format_playbook_run_summary(r) for r in runs],
            "count": len(runs),
        }

    @staticmethod
    def _format_playbook_run_summary(run) -> dict:
        """Build a summary dict for a single playbook run.

        Extracts a compact *path* from the persisted ``node_trace`` JSON —
        a list of ``{"node_id": ..., "status": ...}`` dicts showing which
        nodes were visited and their outcome.  Also computes a
        ``duration_seconds`` when both ``started_at`` and ``completed_at``
        are available.
        """
        # Parse node_trace JSON → compact path (just node_id + status)
        path: list[dict[str, str]] = []
        try:
            raw_trace = (
                json.loads(run.node_trace)
                if isinstance(run.node_trace, str)
                else run.node_trace or []
            )
            for entry in raw_trace:
                path.append(
                    {
                        "node_id": entry.get("node_id", "unknown"),
                        "status": entry.get("status", "unknown"),
                    }
                )
        except (json.JSONDecodeError, TypeError):
            pass  # leave path as empty list

        summary: dict = {
            "run_id": run.run_id,
            "playbook_id": run.playbook_id,
            "playbook_version": run.playbook_version,
            "status": run.status,
            "current_node": run.current_node,
            "tokens_used": run.tokens_used,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "path": path,
        }

        # Compute duration for finished runs
        if run.started_at and run.completed_at:
            summary["duration_seconds"] = round(run.completed_at - run.started_at, 3)

        if run.error:
            summary["error"] = run.error

        return summary

    async def _cmd_inspect_playbook_run(self, args: dict) -> dict:
        """Inspect a playbook run: full node trace, conversation, and token usage.

        Returns the complete state of a playbook run including the parsed
        node trace (with per-node timing and transition data), the full
        conversation history, token usage, trigger event, and run metadata.

        This is the detailed inspection counterpart to ``list_playbook_runs``
        which returns only summary data.  Spec §15, roadmap 5.5.6.

        Args:
            run_id: The playbook run ID to inspect.
        """
        run_id = args.get("run_id")
        if not run_id:
            return {"error": "run_id is required"}

        db_run = await self.db.get_playbook_run(run_id)
        if not db_run:
            return {"error": f"Playbook run '{run_id}' not found"}

        # Parse JSON fields — they're stored as serialised strings
        try:
            node_trace = (
                json.loads(db_run.node_trace)
                if isinstance(db_run.node_trace, str)
                else db_run.node_trace or []
            )
        except (json.JSONDecodeError, TypeError):
            node_trace = []

        try:
            conversation_history = (
                json.loads(db_run.conversation_history)
                if isinstance(db_run.conversation_history, str)
                else db_run.conversation_history or []
            )
        except (json.JSONDecodeError, TypeError):
            conversation_history = []

        try:
            trigger_event = (
                json.loads(db_run.trigger_event)
                if isinstance(db_run.trigger_event, str)
                else db_run.trigger_event or {}
            )
        except (json.JSONDecodeError, TypeError):
            trigger_event = {}

        # Compute per-node durations from the trace
        enriched_trace = []
        for entry in node_trace:
            enriched = dict(entry)
            started = entry.get("started_at")
            completed = entry.get("completed_at")
            if started is not None and completed is not None:
                enriched["duration_seconds"] = round(completed - started, 3)
            enriched_trace.append(enriched)

        # Build the response
        result: dict = {
            "run_id": db_run.run_id,
            "playbook_id": db_run.playbook_id,
            "playbook_version": db_run.playbook_version,
            "status": db_run.status,
            "current_node": db_run.current_node,
            "started_at": db_run.started_at,
            "completed_at": db_run.completed_at,
            "tokens_used": db_run.tokens_used,
            "node_trace": enriched_trace,
            "node_count": len(enriched_trace),
            "conversation_history": conversation_history,
            "message_count": len(conversation_history),
            "trigger_event": trigger_event,
        }
        if db_run.error:
            result["error"] = db_run.error
        if db_run.paused_at:
            result["paused_at"] = db_run.paused_at

        # Compute total run duration if completed
        if db_run.started_at and db_run.completed_at:
            result["total_duration_seconds"] = round(db_run.completed_at - db_run.started_at, 3)

        return result

    async def _cmd_resume_playbook(self, args: dict) -> dict:
        """Resume a paused (human-in-the-loop) playbook run.

        Fetches the paused run from the database, validates it is in
        ``paused`` status, creates a Supervisor for LLM calls, and invokes
        :meth:`PlaybookRunner.resume` with the human's input.

        The human input is injected into the conversation history and the
        run continues from the paused node, evaluating transitions with
        the new context.

        A ``playbook.run.resumed`` event is emitted on the EventBus
        so downstream subscribers can react (spec §9).  Note: external
        systems can also fire ``human.review.completed`` on the EventBus
        to trigger a resume via the :class:`PlaybookResumeHandler`
        (roadmap 5.4.3).

        Args:
            run_id: The playbook run to resume.
            human_input: The human reviewer's decision / feedback text.
        """
        run_id = args.get("run_id")
        human_input = args.get("human_input", "").strip()

        if not run_id:
            return {"error": "run_id is required"}
        if not human_input:
            return {"error": "human_input is required — provide your review decision or feedback"}

        # Fetch the paused run
        db_run = await self.db.get_playbook_run(run_id)
        if not db_run:
            return {"error": f"Playbook run '{run_id}' not found"}
        if db_run.status != "paused":
            return {
                "error": (
                    f"Run '{run_id}' has status '{db_run.status}', not 'paused'. "
                    f"Only paused runs can be resumed."
                )
            }

        # Check pause timeout (spec §9, roadmap 5.4.4: configurable, default 24h)
        # Resolve the effective graph for timeout configuration
        timeout_graph = None
        if db_run.pinned_graph:
            timeout_graph = json.loads(db_run.pinned_graph)
        elif hasattr(self.orchestrator, "playbook_manager"):
            pb = self.orchestrator.playbook_manager._active.get(db_run.playbook_id)
            if pb:
                timeout_graph = pb.to_dict() if hasattr(pb, "to_dict") else pb.__dict__

        from src.playbooks.runner import PlaybookRunner

        if timeout_graph:
            pause_timeout_seconds = PlaybookRunner._resolve_pause_timeout(
                timeout_graph, db_run.current_node
            )
        else:
            pause_timeout_seconds = int(args.get("timeout_seconds", 86400))

        # Determine when the run was paused (prefer dedicated column, fall back
        # to node trace for backward compatibility)
        paused_at = db_run.paused_at or self._get_paused_at(db_run)
        if paused_at and (time.time() - paused_at) > pause_timeout_seconds:
            # Handle timeout — may transition to a timeout node if configured
            event_bus = getattr(self.orchestrator, "bus", None)

            # Try to create a Supervisor for timeout node execution
            supervisor = None
            paused_node = (timeout_graph or {}).get("nodes", {}).get(db_run.current_node or "", {})
            on_timeout_node = paused_node.get("on_timeout")
            if on_timeout_node:
                from src.supervisor import Supervisor as SupervisorCls

                supervisor = SupervisorCls(self.orchestrator, self.config)
                if not supervisor.initialize():
                    supervisor = None

            try:
                with CorrelationContext(run_id=db_run.run_id):
                    result = await PlaybookRunner.handle_timeout(
                        db_run=db_run,
                        graph=timeout_graph or {},
                        supervisor=supervisor,
                        db=self.db,
                        event_bus=event_bus,
                    )
            except Exception as exc:
                logger.error("Timeout handling failed for run %s: %s", run_id, exc, exc_info=True)
                await self.db.update_playbook_run(
                    run_id,
                    status="timed_out",
                    completed_at=time.time(),
                    error=f"Pause timeout exceeded ({pause_timeout_seconds}s)",
                )
                return {
                    "error": (
                        f"Run '{run_id}' has exceeded its pause timeout "
                        f"({pause_timeout_seconds}s). The run has been marked as timed_out."
                    )
                }

            if result.status == "timed_out":
                return {
                    "error": (
                        f"Run '{run_id}' has exceeded its pause timeout "
                        f"({pause_timeout_seconds}s). The run has been marked as timed_out."
                    )
                }
            # Timeout node transition succeeded — return the result
            resp = {
                "resumed": run_id,
                "playbook_id": db_run.playbook_id,
                "status": result.status,
                "tokens_used": result.tokens_used,
                "timeout_transition": True,
            }
            if result.error:
                resp["error"] = result.error
            return resp

        # Resolve the playbook graph — pinned_graph is preferred (version
        # pinning), but fall back to the current active version if needed.
        graph = None
        if db_run.pinned_graph:
            graph = json.loads(db_run.pinned_graph)
        elif hasattr(self.orchestrator, "playbook_manager"):
            pb = self.orchestrator.playbook_manager._active.get(db_run.playbook_id)
            if pb:
                graph = pb.to_dict() if hasattr(pb, "to_dict") else pb.__dict__
        if not graph:
            return {
                "error": (
                    f"Cannot resolve playbook graph for '{db_run.playbook_id}'. "
                    f"The playbook may have been removed."
                )
            }

        # Create a Supervisor for LLM calls during resume
        from src.supervisor import Supervisor

        supervisor = Supervisor(self.orchestrator, self.config)
        if not supervisor.initialize():
            return {"error": "Failed to initialize LLM provider for playbook resume"}

        # Resolve event_bus
        event_bus = getattr(self.orchestrator, "bus", None)

        from src.playbooks.runner import PlaybookRunner

        try:
            with CorrelationContext(run_id=db_run.run_id):
                result = await PlaybookRunner.resume(
                    db_run=db_run,
                    graph=graph,
                    supervisor=supervisor,
                    human_input=human_input,
                    db=self.db,
                    event_bus=event_bus,
                )
        except Exception as exc:
            logger.error("Playbook resume failed for run %s: %s", run_id, exc, exc_info=True)
            return {"error": f"Resume failed: {exc}"}

        resp = {
            "resumed": run_id,
            "playbook_id": db_run.playbook_id,
            "status": result.status,
            "tokens_used": result.tokens_used,
        }
        if result.error:
            resp["error"] = result.error
        return resp

    async def _cmd_recover_workflow(self, args: dict) -> dict:
        """Recover an orphaned coordination workflow (Roadmap 7.5.6).

        If the coordination playbook for a workflow has died (crashed,
        failed, timed out), this command attempts recovery:

        - If the playbook was paused waiting for stage completion and all
          tasks are done, re-emits the missed ``workflow.stage.completed``
          event to resume the playbook automatically.
        - If the playbook run failed/timed_out, emits a ``workflow.orphaned``
          event for operator alerting and manual re-triggering.
        - If the playbook is still running or paused for human input,
          reports the current state (no recovery needed).

        Tasks in the workflow continue executing independently regardless
        of playbook state — this command only re-establishes playbook control.

        Args:
            workflow_id: The workflow ID to recover.
        """
        workflow_id = args.get("workflow_id")
        if not workflow_id:
            return {"error": "workflow_id is required"}

        recovery = getattr(self.orchestrator, "orphan_workflow_recovery", None)
        if not recovery:
            return {"error": "Orphan workflow recovery not initialized"}

        return await recovery.recover_workflow(workflow_id)

    async def _cmd_compile_playbook(self, args: dict) -> dict:
        """Manually trigger compilation of a playbook markdown.

        Accepts either inline markdown content or a file path to a ``.md``
        file.  Delegates to :meth:`PlaybookManager.compile_playbook` which
        handles source-hash change detection, LLM invocation, validation,
        and persistence.

        This is the manual trigger (roadmap 5.5.1) — it always sets
        ``force=True`` so that recompilation happens even if the source
        hash is unchanged.

        Args:
            markdown: Full playbook markdown including YAML frontmatter.
                      Either this, ``path``, or ``playbook_id`` must be provided.
            path: Absolute path to a playbook ``.md`` file on disk.
                  If provided, the file is read and used as the markdown.
            playbook_id: ID of an already-known playbook. Resolves to its
                  source path via the playbook manager, then re-reads and
                  recompiles it.
            force: Force recompilation even if unchanged (default ``True``
                   for manual trigger).
        """
        markdown = args.get("markdown", "").strip()
        path = args.get("path", "").strip()
        playbook_id = args.get("playbook_id", "").strip()
        force = args.get("force", True)  # default True for manual trigger
        if isinstance(force, str):
            force = force.lower() in ("true", "1", "yes")

        # Resolve playbook_id → source path. Try the manager's in-memory map
        # first (populated when this process compiled the playbook), then fall
        # back to a vault scan for `<id>.md` (covers the case where the
        # playbook was loaded from compiled JSON at startup).
        if playbook_id and not path and not markdown:
            pm = getattr(self.orchestrator, "playbook_manager", None)
            if pm is None:
                return {"error": "Playbook manager is not initialised"}
            resolved = pm._source_paths.get(playbook_id)
            if not resolved:
                import pathlib

                name = f"{playbook_id}.md"
                search_dirs = [
                    pathlib.Path(self.config.data_dir) / "vault" / "system" / "playbooks",
                    pathlib.Path(self.config.data_dir)
                    / "vault"
                    / "agent-types"
                    / "supervisor"
                    / "playbooks",
                ]
                projects_dir = pathlib.Path(self.config.data_dir) / "vault" / "projects"
                if projects_dir.is_dir():
                    for proj in projects_dir.iterdir():
                        pb_dir = proj / "playbooks"
                        if pb_dir.is_dir():
                            search_dirs.append(pb_dir)
                for d in search_dirs:
                    candidate = d / name
                    if candidate.is_file():
                        resolved = str(candidate)
                        break
            if not resolved:
                return {
                    "error": (
                        f"Unknown playbook_id '{playbook_id}'. Use 'list_playbooks' "
                        "to see compiled playbooks, or pass 'path' instead."
                    )
                }
            path = resolved

        # Resolve markdown from path if provided
        if path and not markdown:
            import pathlib

            p = pathlib.Path(path)
            if not p.is_file():
                # Try resolving as a playbook name from vault directories.
                # Search system, orchestrator, and per-project playbook dirs.
                name = path if path.endswith(".md") else f"{path}.md"
                search_dirs = [
                    pathlib.Path(self.config.data_dir) / "vault" / "system" / "playbooks",
                    pathlib.Path(self.config.data_dir)
                    / "vault"
                    / "agent-types"
                    / "supervisor"
                    / "playbooks",
                ]
                # Also search per-project playbook dirs
                projects_dir = pathlib.Path(self.config.data_dir) / "vault" / "projects"
                if projects_dir.is_dir():
                    for proj in projects_dir.iterdir():
                        pb_dir = proj / "playbooks"
                        if pb_dir.is_dir():
                            search_dirs.append(pb_dir)
                for d in search_dirs:
                    candidate = d / name
                    if candidate.is_file():
                        p = candidate
                        path = str(p)
                        break
                if not p.is_file():
                    return {"error": f"File not found: {path}"}
            try:
                markdown = p.read_text(encoding="utf-8")
            except Exception as exc:
                return {"error": f"Failed to read file: {exc}"}

        if not markdown:
            return {"error": "Either 'markdown', 'path', or 'playbook_id' is required"}

        # Ensure playbook manager is available
        pm = getattr(self.orchestrator, "playbook_manager", None)
        if pm is None:
            return {"error": "Playbook manager is not initialised"}

        try:
            result = await pm.compile_playbook(
                markdown,
                source_path=path,
                rel_path=path,
                force=force,
            )
        except Exception as exc:
            logger.error("Playbook compilation failed: %s", exc, exc_info=True)
            return {"error": f"Compilation failed: {exc}"}

        if not result.success:
            return {
                "error": "Compilation failed",
                "errors": result.errors,
                "source_hash": result.source_hash,
                "retries_used": result.retries_used,
            }

        # Build success response
        pb = result.playbook
        resp: dict = {
            "compiled": True,
            "playbook_id": pb.id if pb else "",
            "version": pb.version if pb else 0,
            "source_hash": result.source_hash,
            "skipped": result.skipped,
            "retries_used": result.retries_used,
        }
        if pb:
            resp["node_count"] = len(pb.nodes)
            resp["triggers"] = [
                t.event_type if hasattr(t, "event_type") else str(t) for t in pb.triggers
            ]
            resp["scope"] = pb.scope
        return resp

    async def _cmd_show_playbook_graph(self, args: dict) -> dict:
        """Render a compiled playbook graph as ASCII or Mermaid diagram.

        Loads the compiled playbook from the active playbook set (or from
        the compiled store by scope) and renders it in the requested format.

        This is the ``show_playbook_graph`` command from spec §15,
        roadmap 5.5.3.

        Args:
            playbook_id: The playbook identifier to render.
            format: Output format — ``"ascii"`` (default) or ``"mermaid"``.
            direction: Mermaid flowchart direction — ``"TD"`` (default) or ``"LR"``.
                Only used when format is ``"mermaid"``.
            show_prompts: Include truncated prompt previews (default ``True``).
        """
        from src.playbooks.graph import render_ascii, render_mermaid

        playbook_id = args.get("playbook_id", "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}

        fmt = args.get("format", "ascii").strip().lower()
        if fmt not in ("ascii", "mermaid"):
            return {"error": f"Invalid format '{fmt}'. Valid: ascii, mermaid"}

        direction = args.get("direction", "TD").strip().upper()
        if direction not in ("TD", "LR"):
            return {"error": f"Invalid direction '{direction}'. Valid: TD, LR"}

        show_prompts = args.get("show_prompts", True)
        if isinstance(show_prompts, str):
            show_prompts = show_prompts.lower() in ("true", "1", "yes")

        # Resolve the compiled playbook — check active set first
        playbook = None
        pm = getattr(self.orchestrator, "playbook_manager", None)
        if pm is not None:
            playbook = pm.get_playbook(playbook_id)

        if playbook is None:
            return {
                "error": (
                    f"Playbook '{playbook_id}' not found. "
                    "Make sure it has been compiled (use compile_playbook first)."
                ),
            }

        # Render
        if fmt == "mermaid":
            output = render_mermaid(
                playbook,
                direction=direction,
                show_prompts=show_prompts,
            )
        else:
            output = render_ascii(
                playbook,
                show_prompts=show_prompts,
            )

        return {
            "playbook_id": playbook.id,
            "format": fmt,
            "graph": output,
            "node_count": len(playbook.nodes),
            "version": playbook.version,
        }

    async def _cmd_run_playbook(self, args: dict) -> dict:
        """Manually trigger a playbook run.

        Looks up the compiled playbook by ID, creates a Supervisor for
        LLM calls, and executes the full graph from entry to terminal.
        The run is persisted to the database and emits events like any
        trigger-initiated run.

        Args:
            playbook_id: The compiled playbook to execute.
            event: Optional trigger event dict (default: ``{"type": "manual"}``).
        """
        playbook_id = args.get("playbook_id", "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}

        pm = getattr(self.orchestrator, "playbook_manager", None)
        if pm is None:
            return {"error": "Playbook manager is not initialised"}

        playbook = pm.get_playbook(playbook_id)
        if playbook is None:
            return {
                "error": (
                    f"Playbook '{playbook_id}' not found. "
                    "Make sure it has been compiled (use compile_playbook first)."
                ),
            }

        # Build event payload — merge user-provided event with defaults
        event = args.get("event") or {}
        if isinstance(event, str):
            try:
                event = json.loads(event)
            except (json.JSONDecodeError, TypeError):
                return {"error": f"Invalid event JSON: {event}"}
        if not isinstance(event, dict):
            return {"error": "event must be a JSON object (dict)"}
        if "type" not in event:
            event["type"] = "manual"

        # Inject project_id for project-scoped playbooks so that events
        # (pause notifications, task creation, etc.) route to the correct
        # project channel.  The project ID comes from the playbook manager's
        # scope_identifiers — not parse_scope(), which only returns the
        # scope enum without the identifier for project-scoped playbooks.
        if "project_id" not in event and pm:
            scope_identifier = pm.get_scope_identifier(playbook_id)
            if scope_identifier:
                event["project_id"] = scope_identifier

        # Inject notification_channel_id so the playbook LLM knows where
        # to post summaries via send_message.
        if "notification_channel_id" not in event:
            bot = getattr(self.orchestrator, "_discord_bot", None)
            if bot:
                # Use project channel for project-scoped, global for system
                ch = bot._get_channel(event.get("project_id"))
                if ch:
                    event["notification_channel_id"] = str(ch.id)

        graph = playbook.to_dict()

        # Create a Supervisor for LLM calls
        from src.supervisor import Supervisor

        llm_logger = getattr(self.orchestrator, "llm_logger", None)
        supervisor = Supervisor(self.orchestrator, self.config, llm_logger=llm_logger)
        if not supervisor.initialize():
            return {"error": "Failed to initialize LLM provider for playbook execution"}
        # Set the active project so the supervisor's system prompt includes
        # project context, and tools like create_task and memory_search
        # default to the correct project.
        if event.get("project_id"):
            supervisor.set_active_project(event["project_id"])
        # Wire plugin tools so the supervisor can discover and load them
        plugin_registry = getattr(self.orchestrator, "plugin_registry", None)
        if plugin_registry:
            supervisor._registry.set_plugin_registry(plugin_registry)

        event_bus = getattr(self.orchestrator, "bus", None)

        from src.playbooks.runner import PlaybookRunner

        runner = PlaybookRunner(
            graph=graph,
            event=event,
            supervisor=supervisor,
            db=self.db,
            event_bus=event_bus,
        )

        try:
            with CorrelationContext(run_id=runner.run_id):
                result = await runner.run()
        except Exception as exc:
            logger.error(
                "Manual playbook run failed for '%s': %s",
                playbook_id,
                exc,
                exc_info=True,
            )
            return {"error": f"Playbook execution failed: {exc}"}

        resp = {
            "run_id": result.run_id,
            "playbook_id": playbook_id,
            "version": playbook.version,
            "status": result.status,
            "tokens_used": result.tokens_used,
            "node_count": len(result.node_trace),
            "node_trace": result.node_trace,
        }
        if result.error:
            resp["error"] = result.error
        if result.final_response:
            resp["final_response"] = result.final_response
        return resp

    async def _cmd_dry_run_playbook(self, args: dict) -> dict:
        """Simulate playbook execution with a mock event, producing no side effects.

        Walks the compiled playbook graph from entry to terminal without
        making real LLM calls, writing to the database, or emitting events.
        Returns the node trace showing the path that *would* be taken.

        This is the ``dry_run_playbook`` command from spec §15,
        roadmap 5.5.2, addressing Open Question #2 (testing and validation).

        Transition strategy during dry-run:

        - Unconditional ``goto`` edges: followed normally.
        - Structured conditions: evaluated against the simulated response
          (most won't match, falling through to ``otherwise``).
        - Natural-language conditions: the first candidate is followed
          without an LLM call.
        - Human-in-the-loop nodes: executed and continued past (no pause).

        Args:
            playbook_id: The compiled playbook to simulate.
            event: Mock trigger event dict (default: ``{"type": "dry_run"}``).
        """
        playbook_id = args.get("playbook_id", "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}

        # Resolve the compiled playbook from the active set
        pm = getattr(self.orchestrator, "playbook_manager", None)
        if pm is None:
            return {"error": "Playbook manager is not initialised"}

        playbook = pm.get_playbook(playbook_id)
        if playbook is None:
            return {
                "error": (
                    f"Playbook '{playbook_id}' not found. "
                    "Make sure it has been compiled (use compile_playbook first)."
                ),
            }

        # Build mock event — merge user-provided event with sensible defaults
        event = args.get("event") or {}
        if isinstance(event, str):
            try:
                event = json.loads(event)
            except (json.JSONDecodeError, TypeError):
                return {"error": f"Invalid event JSON: {event}"}
        if not isinstance(event, dict):
            return {"error": "event must be a JSON object (dict)"}

        # Ensure a type field exists for seed message clarity
        if "type" not in event:
            event["type"] = "dry_run"

        # Convert CompiledPlaybook to the dict format PlaybookRunner expects
        graph = playbook.to_dict()

        from src.playbooks.runner import PlaybookRunner

        try:
            result = await PlaybookRunner.dry_run(graph=graph, event=event)
        except Exception as exc:
            logger.error(
                "Dry-run failed for playbook '%s': %s",
                playbook_id,
                exc,
                exc_info=True,
            )
            return {"error": f"Dry-run simulation failed: {exc}"}

        # Annotate transitions with dry-run-specific info
        annotated_trace = []
        for entry in result.node_trace:
            annotated = dict(entry)
            method = entry.get("transition_method")
            if method == "llm":
                annotated["transition_note"] = "first candidate (LLM skipped in dry-run)"
            annotated_trace.append(annotated)

        return {
            "dry_run": True,
            "playbook_id": playbook_id,
            "version": playbook.version,
            "status": result.status,
            "node_trace": annotated_trace,
            "node_count": len(annotated_trace),
            "tokens_used": result.tokens_used,
            "mock_event": event,
        }

    async def _cmd_playbook_health(self, args: dict) -> dict:
        """Compute health metrics for playbook runs: tokens per node, run duration,
        transition paths, and failure rates.

        Returns a comprehensive health report with per-node metrics (avg duration,
        token usage, failure rate), run duration statistics (avg, p50, p95), most
        common transition paths through the graph, and failure analysis.

        Roadmap 5.7.1.

        Args:
            playbook_id: Optional — filter to a specific playbook.
            limit: Optional — max runs to analyse (default 200).
            status: Optional — filter by run status.
        """
        from src.playbooks.health import compute_playbook_health

        playbook_id = args.get("playbook_id")
        status = args.get("status")
        limit = int(args.get("limit", 200))

        valid_statuses = {"running", "paused", "completed", "failed", "timed_out"}
        if status and status not in valid_statuses:
            return {
                "error": f"Invalid status '{status}'. Valid: {', '.join(sorted(valid_statuses))}"
            }

        runs = await self.db.list_playbook_runs(
            playbook_id=playbook_id,
            status=status,
            limit=limit,
        )

        return compute_playbook_health(runs, playbook_id=playbook_id)

    async def _cmd_playbook_graph_view(self, args: dict) -> dict:
        """Return structured graph view data for dashboard rendering of a playbook.

        Produces a complete JSON representation of the playbook graph suitable
        for interactive dashboard visualization: nodes as positioned boxes
        (color-coded by type), transitions as labelled arrows, with optional
        overlays for live state, run path highlighting, and health metrics.

        Implements spec §14 (Dashboard Visualization), roadmap 5.7.2.

        Args:
            playbook_id: The playbook identifier to visualize.
            direction: Layout direction — ``"TD"`` (top-down) or ``"LR"`` (left-right).
                Default: ``"TD"``.
            show_prompts: Include truncated prompt previews in nodes. Default: ``True``.
            run_id: Optional — overlay a specific run's path on the graph.
            include_live_state: Include live state for running/paused instances.
                Default: ``True``.
            include_metrics: Include per-node health metrics overlay.
                Default: ``False``.
            include_history: Include run history timeline. Default: ``False``.
            history_limit: Max runs in the history timeline. Default: ``20``.
        """
        from src.playbooks.graph_view import build_graph_view
        from src.playbooks.health import compute_node_metrics

        playbook_id = args.get("playbook_id", "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}

        direction = args.get("direction", "TD").strip().upper()
        if direction not in ("TD", "LR"):
            return {"error": f"Invalid direction '{direction}'. Valid: TD, LR"}

        show_prompts = args.get("show_prompts", True)
        if isinstance(show_prompts, str):
            show_prompts = show_prompts.lower() in ("true", "1", "yes")

        include_live = args.get("include_live_state", True)
        if isinstance(include_live, str):
            include_live = include_live.lower() in ("true", "1", "yes")

        include_metrics = args.get("include_metrics", False)
        if isinstance(include_metrics, str):
            include_metrics = include_metrics.lower() in ("true", "1", "yes")

        include_history = args.get("include_history", False)
        if isinstance(include_history, str):
            include_history = include_history.lower() in ("true", "1", "yes")

        history_limit = int(args.get("history_limit", 20))
        run_id = args.get("run_id", "").strip() if args.get("run_id") else None

        # Resolve the compiled playbook
        playbook = None
        pm = getattr(self.orchestrator, "playbook_manager", None)
        if pm is not None:
            playbook = pm.get_playbook(playbook_id)

        if playbook is None:
            return {
                "error": (
                    f"Playbook '{playbook_id}' not found. "
                    "Make sure it has been compiled (use compile_playbook first)."
                ),
            }

        # Fetch active runs for live state
        active_runs = None
        if include_live:
            running = await self.db.list_playbook_runs(
                playbook_id=playbook_id,
                status="running",
                limit=50,
            )
            paused = await self.db.list_playbook_runs(
                playbook_id=playbook_id,
                status="paused",
                limit=50,
            )
            active_runs = running + paused if (running or paused) else None

        # Fetch a specific run for overlay
        run_overlay = None
        if run_id:
            run_overlay = await self.db.get_playbook_run(run_id)
            if run_overlay is None:
                return {"error": f"Run '{run_id}' not found"}
            if run_overlay.playbook_id != playbook_id:
                return {
                    "error": (
                        f"Run '{run_id}' belongs to playbook "
                        f"'{run_overlay.playbook_id}', not '{playbook_id}'"
                    ),
                }

        # Fetch all runs for history and/or metrics
        all_runs = None
        node_metrics = None
        if include_history or include_metrics:
            fetch_limit = max(history_limit, 200) if include_metrics else history_limit
            all_runs = await self.db.list_playbook_runs(
                playbook_id=playbook_id,
                limit=fetch_limit,
            )

        if include_metrics and all_runs:
            node_metrics = compute_node_metrics(all_runs)

        # Build the graph view
        result = build_graph_view(
            playbook,
            direction=direction,
            show_prompts=show_prompts,
            active_runs=active_runs,
            run_overlay=run_overlay,
            all_runs=all_runs if include_history else None,
            node_metrics=node_metrics,
            history_limit=history_limit,
        )

        result["success"] = True
        return result

    # ------------------------------------------------------------------
    # Source read/write, create, delete (dashboard authoring loop)
    # ------------------------------------------------------------------

    def _vault_playbook_dirs(self) -> list:
        """Directories where playbook markdown files may live.

        Mirrors the glob patterns registered with the vault watcher
        (see ``src/playbooks/handler.py``).
        """
        import pathlib

        vault = pathlib.Path(self.config.data_dir) / "vault"
        dirs = [vault / "system" / "playbooks"]

        agent_types = vault / "agent-types"
        if agent_types.is_dir():
            for agent_type in agent_types.iterdir():
                pb_dir = agent_type / "playbooks"
                if pb_dir.is_dir():
                    dirs.append(pb_dir)

        projects = vault / "projects"
        if projects.is_dir():
            for project in projects.iterdir():
                pb_dir = project / "playbooks"
                if pb_dir.is_dir():
                    dirs.append(pb_dir)

        return dirs

    def _resolve_playbook_source_path(self, playbook_id: str):
        """Resolve a playbook_id to its ``.md`` path on disk.

        Checks the manager's in-memory map first (populated when this
        process compiled the playbook), then scans vault playbook dirs
        for ``<id>.md`` — covers the case where the playbook was loaded
        from compiled JSON at startup.
        """
        import pathlib

        pm = getattr(self.orchestrator, "playbook_manager", None)
        if pm is not None:
            cached = pm._source_paths.get(playbook_id)
            if cached and pathlib.Path(cached).is_file():
                return pathlib.Path(cached)

        name = f"{playbook_id}.md"
        for d in self._vault_playbook_dirs():
            candidate = d / name
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _scope_to_vault_dir(vault_root, scope: str):
        """Map a scope string (``system`` / ``project:<id>`` / ``agent-type:<name>``)
        to the corresponding vault playbook directory.

        Returns ``(dir_path, error)`` — ``error`` is non-empty if scope
        is malformed.
        """
        if scope == "system":
            return vault_root / "system" / "playbooks", ""
        if scope.startswith("project:"):
            project_id = scope[len("project:") :].strip()
            if not project_id:
                return None, "project scope requires an identifier (e.g. 'project:my-app')"
            return vault_root / "projects" / project_id / "playbooks", ""
        if scope.startswith("agent-type:"):
            agent_type = scope[len("agent-type:") :].strip()
            if not agent_type:
                return None, "agent-type scope requires a type (e.g. 'agent-type:coding')"
            return vault_root / "agent-types" / agent_type / "playbooks", ""
        return None, (
            f"Invalid scope '{scope}'. Valid: 'system', 'project:<id>', 'agent-type:<name>'"
        )

    async def _cmd_get_playbook_source(self, args: dict) -> dict:
        """Return the raw playbook markdown and source hash.

        The source hash is the content-addressable identifier the dashboard
        sends back on save for optimistic concurrency.

        Args:
            playbook_id: The playbook identifier.
        """
        from src.playbooks.compiler import PlaybookCompiler

        playbook_id = args.get("playbook_id", "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}

        path = self._resolve_playbook_source_path(playbook_id)
        if path is None:
            return {"error": f"Playbook '{playbook_id}' not found in vault"}

        try:
            markdown = path.read_text(encoding="utf-8")
        except Exception as exc:
            return {"error": f"Failed to read source: {exc}"}

        return {
            "playbook_id": playbook_id,
            "path": str(path),
            "markdown": markdown,
            "source_hash": PlaybookCompiler._compute_source_hash(markdown),
        }

    async def _cmd_update_playbook_source(self, args: dict) -> dict:
        """Write new playbook markdown to the vault and compile synchronously.

        Implements the "save & compile" flow (dashboard option 2 — sync):
        write atomically, then invoke the compiler with ``force=True`` and
        surface any validation errors inline.  The file watcher will also
        fire, but will skip recompilation because the source_hash matches.

        Args:
            playbook_id: The playbook identifier.
            markdown: The new full markdown content (including frontmatter).
            expected_source_hash: Optional — content hash the caller last
                saw. If provided and the vault copy has changed underneath,
                returns a conflict error instead of overwriting.
        """
        import os
        import pathlib
        import tempfile
        from src.playbooks.compiler import PlaybookCompiler

        playbook_id = args.get("playbook_id", "").strip()
        markdown = args.get("markdown", "")
        expected_hash = args.get("expected_source_hash", "").strip()

        if not playbook_id:
            return {"error": "playbook_id is required"}
        if not markdown:
            return {"error": "markdown is required"}

        path = self._resolve_playbook_source_path(playbook_id)
        if path is None:
            return {"error": f"Playbook '{playbook_id}' not found in vault"}

        if expected_hash:
            try:
                current = path.read_text(encoding="utf-8")
                current_hash = PlaybookCompiler._compute_source_hash(current)
            except Exception as exc:
                return {"error": f"Failed to read current source: {exc}"}
            if current_hash != expected_hash:
                return {
                    "error": "conflict",
                    "reason": "vault_changed_underneath",
                    "current_source_hash": current_hash,
                    "expected_source_hash": expected_hash,
                }

        pm = getattr(self.orchestrator, "playbook_manager", None)
        if pm is None:
            return {"error": "Playbook manager is not initialised"}

        # Atomic write: tempfile in same dir + fsync + rename
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=f".{playbook_id}.",
                suffix=".md.tmp",
                dir=str(path.parent),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(markdown)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(path))
            except Exception:
                pathlib.Path(tmp_path).unlink(missing_ok=True)
                raise
        except Exception as exc:
            return {"error": f"Failed to write source: {exc}"}

        try:
            result = await pm.compile_playbook(
                markdown,
                source_path=str(path),
                rel_path=str(path),
                force=True,
            )
        except Exception as exc:
            logger.error("Compile after source update failed: %s", exc, exc_info=True)
            return {"error": f"Compilation failed: {exc}"}

        resp: dict = {
            "playbook_id": playbook_id,
            "source_hash": result.source_hash,
            "compiled": result.success,
        }
        if not result.success:
            resp["errors"] = result.errors
            resp["retries_used"] = result.retries_used
            return resp
        pb = result.playbook
        if pb is not None:
            resp["version"] = pb.version
            resp["node_count"] = len(pb.nodes)
            resp["scope"] = pb.scope
            resp["triggers"] = [
                t.event_type if hasattr(t, "event_type") else str(t) for t in pb.triggers
            ]
        return resp

    async def _cmd_create_playbook(self, args: dict) -> dict:
        """Create a new playbook ``.md`` in the vault.

        Writes the file atomically at the scope-appropriate vault path and
        returns immediately — compilation is **not** triggered synchronously
        so authors can iterate on the source without blocking on LLM calls
        or failing on a half-written draft. The vault file watcher will pick
        the file up and compile it in the background; an explicit
        ``update_playbook_source`` call from the editor also force-compiles.

        Args:
            playbook_id: The new playbook identifier (filename without ``.md``).
            scope: Where the file lives — one of ``system``,
                ``project:<project_id>``, or ``agent-type:<type>``.
                Note: the **frontmatter** ``scope:`` field takes the bare
                form (``system`` / ``project`` / ``agent-type:<type>``);
                the project id is recovered from the vault path.
            markdown: Full markdown content (including YAML frontmatter).
        """
        import os
        import pathlib
        import tempfile
        from src.playbooks.compiler import PlaybookCompiler

        playbook_id = args.get("playbook_id", "").strip()
        scope = args.get("scope", "").strip()
        markdown = args.get("markdown", "")

        if not playbook_id:
            return {"error": "playbook_id is required"}
        if not scope:
            return {"error": "scope is required"}
        if not markdown:
            return {"error": "markdown is required"}

        # Reject collisions with an existing playbook (by id, anywhere in vault)
        existing = self._resolve_playbook_source_path(playbook_id)
        if existing is not None:
            return {
                "error": f"Playbook '{playbook_id}' already exists at {existing}",
            }

        vault_root = pathlib.Path(self.config.data_dir) / "vault"
        target_dir, scope_err = self._scope_to_vault_dir(vault_root, scope)
        if scope_err:
            return {"error": scope_err}
        target_path = target_dir / f"{playbook_id}.md"

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return {"error": f"Failed to create scope directory: {exc}"}

        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=f".{playbook_id}.",
                suffix=".md.tmp",
                dir=str(target_dir),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(markdown)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(target_path))
            except Exception:
                pathlib.Path(tmp_path).unlink(missing_ok=True)
                raise
        except Exception as exc:
            return {"error": f"Failed to write new playbook: {exc}"}

        return {
            "created": True,
            "playbook_id": playbook_id,
            "path": str(target_path),
            "source_hash": PlaybookCompiler._compute_source_hash(markdown),
        }

    async def _cmd_delete_playbook(self, args: dict) -> dict:
        """Archive a playbook's source and remove it from the active registry.

        The source markdown is moved to ``vault/trash/playbooks/`` with a
        timestamp suffix rather than hard-deleted — historical
        ``playbook_runs`` rows still reference the id.

        Args:
            playbook_id: The playbook identifier to delete.
        """
        import pathlib
        import time as _time

        playbook_id = args.get("playbook_id", "").strip()
        if not playbook_id:
            return {"error": "playbook_id is required"}

        path = self._resolve_playbook_source_path(playbook_id)
        if path is None:
            return {"error": f"Playbook '{playbook_id}' not found in vault"}

        vault_root = pathlib.Path(self.config.data_dir) / "vault"
        trash_dir = vault_root / "trash" / "playbooks"
        try:
            trash_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return {"error": f"Failed to create trash directory: {exc}"}

        archived_path = trash_dir / f"{playbook_id}.{int(_time.time())}.md"
        try:
            path.rename(archived_path)
        except Exception as exc:
            return {"error": f"Failed to archive source: {exc}"}

        pm = getattr(self.orchestrator, "playbook_manager", None)
        removed = False
        if pm is not None:
            try:
                removed = await pm.remove_playbook(playbook_id)
            except Exception as exc:
                logger.error(
                    "Playbook '%s' source archived but registry remove failed: %s",
                    playbook_id,
                    exc,
                    exc_info=True,
                )
                return {
                    "error": f"Source archived but registry remove failed: {exc}",
                    "archived_path": str(archived_path),
                }

        return {
            "deleted": True,
            "playbook_id": playbook_id,
            "archived_path": str(archived_path),
            "removed_from_registry": removed,
        }

    @staticmethod
    def _get_paused_at(db_run) -> float | None:
        """Extract the timestamp when a run was paused.

        Looks at the last entry in node_trace for the completed_at time
        of the paused node, falling back to started_at.
        """
        try:
            trace = json.loads(db_run.node_trace) if isinstance(db_run.node_trace, str) else []
            if trace:
                last_entry = trace[-1]
                # The completed_at of the last node is when the pause happened
                completed_at = last_entry.get("completed_at")
                if completed_at:
                    return completed_at
        except (json.JSONDecodeError, TypeError):
            pass
        return db_run.started_at

    async def check_paused_playbook_timeouts(self) -> list[dict]:
        """Sweep all paused playbook runs and handle any that have timed out.

        Called by the orchestrator tick loop (roadmap 5.4.4).  For each
        paused run whose timeout has expired, either transitions to the
        designated timeout node or marks the run as ``timed_out``.

        Returns a list of result dicts for each timed-out run processed.
        """
        paused_runs = await self.db.list_playbook_runs(status="paused", limit=100)
        if not paused_runs:
            return []

        results = []
        now = time.time()

        for db_run in paused_runs:
            # Resolve the effective graph
            graph = None
            if db_run.pinned_graph:
                graph = json.loads(db_run.pinned_graph)
            elif hasattr(self.orchestrator, "playbook_manager"):
                pb = self.orchestrator.playbook_manager._active.get(db_run.playbook_id)
                if pb:
                    graph = pb.to_dict() if hasattr(pb, "to_dict") else pb.__dict__

            if not graph:
                continue

            from src.playbooks.runner import PlaybookRunner

            timeout_seconds = PlaybookRunner._resolve_pause_timeout(graph, db_run.current_node)

            # Determine when the run was paused
            paused_at = db_run.paused_at or self._get_paused_at(db_run)
            if not paused_at:
                continue

            if (now - paused_at) <= timeout_seconds:
                continue  # Not timed out yet

            logger.info(
                "Playbook run %s (node '%s') exceeded pause timeout (%ds)",
                db_run.run_id,
                db_run.current_node,
                timeout_seconds,
            )

            # Check if a Supervisor is needed (on_timeout node present)
            supervisor = None
            paused_node = graph.get("nodes", {}).get(db_run.current_node or "", {})
            on_timeout_node = paused_node.get("on_timeout")
            if on_timeout_node and on_timeout_node in graph.get("nodes", {}):
                from src.supervisor import Supervisor

                supervisor = Supervisor(self.orchestrator, self.config)
                if not supervisor.initialize():
                    logger.warning(
                        "Failed to create Supervisor for timeout transition "
                        "on run %s — will mark as timed_out",
                        db_run.run_id,
                    )
                    supervisor = None

            event_bus = getattr(self.orchestrator, "bus", None)

            try:
                with CorrelationContext(run_id=db_run.run_id):
                    result = await PlaybookRunner.handle_timeout(
                        db_run=db_run,
                        graph=graph,
                        supervisor=supervisor,
                        db=self.db,
                        event_bus=event_bus,
                    )
                results.append(
                    {
                        "run_id": db_run.run_id,
                        "playbook_id": db_run.playbook_id,
                        "status": result.status,
                        "timeout_seconds": timeout_seconds,
                        "on_timeout": on_timeout_node,
                    }
                )
            except Exception as exc:
                logger.error(
                    "Failed to handle timeout for run %s: %s",
                    db_run.run_id,
                    exc,
                    exc_info=True,
                )
                # Best-effort: mark as timed_out directly
                try:
                    await self.db.update_playbook_run(
                        db_run.run_id,
                        status="timed_out",
                        completed_at=time.time(),
                        error=f"Pause timeout exceeded ({timeout_seconds}s)",
                    )
                except Exception:
                    pass

        return results
