"""System commands mixin — config, diagnostics, status, orchestrator control."""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import signal
from pathlib import Path

import logging

from src.commands.helpers import _run_subprocess, _run_subprocess_shell

logger = logging.getLogger(__name__)


class SystemCommandsMixin:
    """System command methods mixed into CommandHandler."""

    # -----------------------------------------------------------------------
    # System commands — config reload, status, diagnostics
    # -----------------------------------------------------------------------

    async def _cmd_reload_config(self, args: dict) -> dict:
        """Manually trigger a config hot-reload from disk.

        Returns a summary of which sections changed, which were applied,
        and which require a restart.
        """
        watcher = self.orchestrator._config_watcher
        if not watcher:
            return {"error": "Config watcher is not active (no config file path)"}
        result = await watcher.reload()
        if "error" in result:
            return {"error": f"Config reload failed: {result['error']}"}
        if not result.get("changed_sections"):
            return {"message": "No configuration changes detected."}
        parts = []
        if result.get("applied"):
            parts.append(f"Applied: {', '.join(result['applied'])}")
        if result.get("restart_required"):
            parts.append(f"Restart required: {', '.join(result['restart_required'])}")
        return {
            "message": "Config reloaded.",
            "changed_sections": result["changed_sections"],
            "applied": result.get("applied", []),
            "restart_required": result.get("restart_required", []),
            "summary": "; ".join(parts) if parts else "No changes.",
        }

    async def _cmd_claude_usage(self, args: dict) -> dict:
        """Get Claude Code usage stats from live session data.

        Computes real token usage by scanning active session JSONL files
        in ``~/.claude/projects/``.  Also reads subscription info from
        ``~/.claude/.credentials.json``.
        """
        import json as _json
        from pathlib import Path as _Path
        from datetime import datetime as _dt, timezone as _tz

        result: dict = {}

        claude_dir = _Path.home() / ".claude"
        sessions_dir = claude_dir / "sessions"
        projects_dir = claude_dir / "projects"

        # --- 1. Subscription info from credentials ---
        creds_path = claude_dir / ".credentials.json"
        if creds_path.exists():
            try:
                creds = _json.loads(creds_path.read_text())
                oauth = creds.get("claudeAiOauth", {})
                result["subscription"] = oauth.get("subscriptionType", "unknown")
                result["rate_limit_tier"] = oauth.get("rateLimitTier", "unknown")
            except Exception:
                pass

        # --- 2. Active sessions with live token usage from session JSONLs ---
        active_sessions: list[dict] = []
        if sessions_dir.exists():
            for sf in sessions_dir.iterdir():
                try:
                    sess = _json.loads(sf.read_text())
                except Exception:
                    continue
                pid = sess.get("pid")
                # Check if process is still alive
                if not pid or not os.path.exists(f"/proc/{pid}"):
                    continue
                sid = sess.get("sessionId", "")
                cwd = sess.get("cwd", "")
                started_ms = sess.get("startedAt", 0)
                started = _dt.fromtimestamp(started_ms / 1000, tz=_tz.utc) if started_ms else None

                # Scan for the session JSONL in projects/
                usage = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
                msg_count = 0
                jsonl_path = None
                for pd in projects_dir.iterdir():
                    candidate = pd / f"{sid}.jsonl"
                    if candidate.exists():
                        jsonl_path = candidate
                        break
                if jsonl_path:
                    try:
                        with open(jsonl_path) as fh:
                            for line in fh:
                                data = _json.loads(line)
                                msg = data.get("message", {})
                                if isinstance(msg, dict) and "usage" in msg:
                                    u = msg["usage"]
                                    usage["input"] += u.get("input_tokens", 0)
                                    usage["output"] += u.get("output_tokens", 0)
                                    usage["cache_read"] += u.get("cache_read_input_tokens", 0)
                                    usage["cache_create"] += u.get("cache_creation_input_tokens", 0)
                                    msg_count += 1
                    except Exception:
                        pass

                total = sum(usage.values())
                # Derive project name from cwd
                project_name = os.path.basename(cwd) if cwd else "unknown"
                active_sessions.append(
                    {
                        "session_id": sid[:12],
                        "project": project_name,
                        "cwd": cwd,
                        "started": started.strftime("%H:%M") if started else "?",
                        "messages": msg_count,
                        "usage": usage,
                        "total_tokens": total,
                    }
                )

        result["active_sessions"] = active_sessions
        result["active_session_count"] = len(active_sessions)
        result["active_total_tokens"] = sum(s["total_tokens"] for s in active_sessions)

        # --- 3. Aggregate model usage from stats-cache (cumulative) ---
        stats_path = claude_dir / "stats-cache.json"
        if stats_path.exists():
            try:
                stats = _json.loads(stats_path.read_text())
                result["total_sessions"] = stats.get("totalSessions", 0)
                result["total_messages"] = stats.get("totalMessages", 0)
                model_usage = {}
                for model, data in (stats.get("modelUsage") or {}).items():
                    short = model.replace("claude-", "").split("-202")[0]
                    inp = data.get("inputTokens", 0)
                    out = data.get("outputTokens", 0)
                    cache_read = data.get("cacheReadInputTokens", 0)
                    cache_create = data.get("cacheCreationInputTokens", 0)
                    model_usage[short] = {
                        "input": inp,
                        "output": out,
                        "cache_read": cache_read,
                        "cache_create": cache_create,
                        "total": inp + out + cache_read + cache_create,
                    }
                result["model_usage"] = model_usage
                result["stats_date"] = stats.get("lastComputedDate", "unknown")
            except Exception as e:
                result["stats_error"] = str(e)

        # --- 4. Probe rate-limit status via a minimal API call ---
        try:
            rate_limit = await self._probe_claude_rate_limit()
            result["rate_limit"] = rate_limit
        except Exception as e:
            result["rate_limit_error"] = str(e)

        return result

    async def _probe_claude_rate_limit(self) -> dict:
        """Send a minimal 1-token API request to read rate-limit headers.

        This replicates what the Claude Code CLI does internally to check
        quota status.  Uses the OAuth token from ``~/.claude/.credentials.json``
        or falls back to ``ANTHROPIC_API_KEY``.
        """
        import json as _json
        from pathlib import Path as _Path

        # Get auth token
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        auth_header = {}
        creds_path = _Path.home() / ".claude" / ".credentials.json"
        if not api_key and creds_path.exists():
            try:
                creds = _json.loads(creds_path.read_text())
                oauth = creds.get("claudeAiOauth", {})
                token = oauth.get("accessToken")
                if token:
                    auth_header = {"Authorization": f"Bearer {token}"}
            except Exception:
                pass

        if not api_key and not auth_header:
            return {"error": "No API key or OAuth token available"}

        if api_key:
            auth_header = {"x-api-key": api_key}

        import aiohttp

        headers = {
            **auth_header,
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "q"}],
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    rate_info: dict = {}
                    # Extract all unified rate-limit headers
                    for key, val in resp.headers.items():
                        lk = key.lower()
                        if "ratelimit-unified" in lk:
                            # Simplify header names
                            short_key = lk.replace("anthropic-ratelimit-unified-", "")
                            rate_info[short_key] = val

                    # Parse utilisation into a percentage
                    for k, v in list(rate_info.items()):
                        if "utilization" in k:
                            try:
                                rate_info[k + "_pct"] = f"{float(v) * 100:.1f}%"
                            except ValueError:
                                pass

                    # Parse reset timestamp
                    reset_ts = rate_info.get("reset")
                    if reset_ts:
                        try:
                            from datetime import datetime, timezone

                            reset_dt = datetime.fromtimestamp(float(reset_ts), tz=timezone.utc)
                            rate_info["reset_human"] = reset_dt.strftime("%Y-%m-%d %H:%M UTC")
                            # Time until reset
                            now = datetime.now(timezone.utc)
                            delta = reset_dt - now
                            if delta.total_seconds() > 0:
                                hours = int(delta.total_seconds() // 3600)
                                mins = int((delta.total_seconds() % 3600) // 60)
                                rate_info["resets_in"] = f"{hours}h {mins}m"
                        except (ValueError, OSError):
                            pass

                    rate_info["http_status"] = resp.status
                    return rate_info
        except Exception as e:
            return {"error": f"API probe failed: {e}"}

    # -----------------------------------------------------------------------
    # System / control commands -- orchestrator pause/resume, active project
    # switching, and daemon restart.  These affect the global state of the
    # system rather than any single project or task.
    # -----------------------------------------------------------------------

    async def _cmd_set_active_project(self, args: dict) -> dict:
        pid = args.get("project_id")
        if pid:
            project = await self.db.get_project(pid)
            if not project:
                return {"error": f"Project '{pid}' not found"}
            self._active_project_id = pid
            return {"active_project": pid, "name": project.name}
        else:
            self._active_project_id = None
            return {"active_project": None, "message": "Active project cleared"}

    async def _cmd_orchestrator_control(self, args: dict) -> dict:
        action = args["action"]
        orch = self.orchestrator
        if action == "pause":
            orch.pause()
            return {
                "status": "paused",
                "message": "Orchestrator paused — no new tasks will be scheduled",
            }
        elif action == "resume":
            orch.resume()
            return {"status": "running", "message": "Orchestrator resumed"}
        else:  # status
            running = len(orch._running_tasks)
            return {
                "status": "paused" if orch._paused else "running",
                "running_tasks": running,
            }

    async def _cmd_shutdown(self, args: dict) -> dict:
        """Shut down the bot and all running agents.

        Supports two modes:
        - graceful (default): waits for current tasks to complete before exiting
        - force: immediately stops all running agents and exits

        The process exits with code 0 (no restart) rather than SIGTERM
        which the daemon supervisor would interpret as a restart request.
        """
        reason = args.get("reason", "No reason provided")
        force = args.get("force", False)
        mode = "force" if force else "graceful"

        # Log the shutdown event
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        shutdown_msg = (
            f"🛑 **Daemon shutdown initiated** ({mode})\n"
            f"**Reason:** {reason}\n"
            f"**Time:** {timestamp}"
        )
        await self.orchestrator._emit_text_notify(shutdown_msg)

        orch = self.orchestrator

        if force:
            # Force-stop all running tasks immediately
            running_task_ids = list(orch._running_tasks.keys())
            for task_id in running_task_ids:
                try:
                    await orch.stop_task(task_id)
                except Exception as e:
                    logger.warning("Error force-stopping task %s: %s", task_id, e)
        else:
            # Graceful: pause orchestrator so no new tasks are started,
            # then wait for running tasks to finish
            orch._paused = True
            await orch.wait_for_running_tasks(timeout=300)

        # Note: bot status is set to offline by the slash command caller
        # before invoking this handler.

        # Exit without restart — use os._exit(0) after a brief delay
        # to allow the Discord response to be sent
        async def _delayed_exit():
            await asyncio.sleep(2)
            # Don't set _restart_requested — this is a full shutdown, not restart
            os._exit(0)

        asyncio.ensure_future(_delayed_exit())

        return {
            "status": "shutting_down",
            "mode": mode,
            "reason": reason,
            "timestamp": timestamp,
            "tasks_stopped": len(orch._running_tasks) if force else 0,
        }

    async def _cmd_restart_daemon(self, args: dict) -> dict:
        reason = args.get("reason", "No reason provided")
        wait_for_tasks = args.get("wait_for_tasks", False)
        orch = self.orchestrator

        running_count = len(orch._running_tasks)

        if wait_for_tasks and running_count > 0:
            # Pause orchestrator so no new tasks are scheduled, then wait
            orch._paused = True
            await orch._emit_text_notify(
                f"🔄 **Daemon restart pending** — waiting for {running_count} "
                f"running task(s) to complete\n**Reason:** {reason}"
            )
            await orch.wait_for_running_tasks(timeout=300)

        # Log the restart reason to the notification channel before restarting
        await orch._emit_text_notify(f"🔄 **Daemon restart initiated** — {reason}")
        orch._restart_requested = True
        os.kill(os.getpid(), signal.SIGTERM)
        return {
            "status": "restarting",
            "message": "Daemon restart initiated",
            "reason": reason,
            "waited_for_tasks": wait_for_tasks and running_count > 0,
        }

    async def _cmd_update_and_restart(self, args: dict) -> dict:
        """Pull the latest source from git and restart the daemon."""
        reason = args.get("reason", "No reason provided")
        wait_for_tasks = args.get("wait_for_tasks", False)
        orch = self.orchestrator
        # Determine the repo root (where this source lives)
        repo_dir = str(Path(__file__).resolve().parent.parent)

        # git pull
        pull_rc, pull_stdout, pull_stderr = await _run_subprocess(
            "git",
            "pull",
            "--ff-only",
            cwd=repo_dir,
            timeout=30,
        )
        if pull_rc != 0:
            stderr = pull_stderr.strip() or pull_stdout.strip()
            return {"error": f"git pull failed: {stderr}"}

        pull_output = pull_stdout.strip()

        # pip install -e . to pick up any dependency changes
        pip_rc, pip_stdout, pip_stderr = await _run_subprocess(
            "pip",
            "install",
            "-e",
            ".",
            cwd=repo_dir,
            timeout=120,
        )
        if pip_rc != 0:
            stderr = pip_stderr.strip() or pip_stdout.strip()
            return {"error": f"pip install failed: {stderr}"}

        running_count = len(orch._running_tasks)

        if wait_for_tasks and running_count > 0:
            # Pause orchestrator so no new tasks are scheduled, then wait
            orch._paused = True
            await orch._emit_text_notify(
                f"🔄 **Daemon update & restart pending** — waiting for {running_count} "
                f"running task(s) to complete\n**Reason:** {reason}"
            )
            await orch.wait_for_running_tasks(timeout=300)

        # Log the update/restart reason to the notification channel
        await orch._emit_text_notify(f"🔄 **Daemon update & restart initiated** — {reason}")
        # Trigger restart
        orch._restart_requested = True
        os.kill(os.getpid(), signal.SIGTERM)
        return {
            "status": "updating",
            "message": "Update pulled and daemon restart initiated",
            "pull_output": pull_output,
            "reason": reason,
            "waited_for_tasks": wait_for_tasks and running_count > 0,
        }

    # -----------------------------------------------------------------------
    # File / shell commands -- sandboxed filesystem and shell access for the
    # chat agent.  These have no Discord slash command equivalent; they exist
    # so the LLM can inspect workspace files, run diagnostic commands, and
    # search codebases.  All paths are validated through _validate_path to
    # prevent escaping the workspace sandbox.
    # -----------------------------------------------------------------------

    # _cmd_read_file → moved to src/plugins/internal/files.py (aq-files plugin)

    async def _cmd_run_command(self, args: dict) -> dict:
        command = args["command"]
        working_dir = args["working_dir"]
        timeout = min(args.get("timeout", 30), 120)

        if not os.path.isabs(working_dir):
            ws_path = await self.db.get_project_workspace_path(working_dir)
            if ws_path:
                working_dir = ws_path
            else:
                working_dir = os.path.join(self.config.workspace_dir, working_dir)

        validated = await self._validate_path(working_dir)
        if not validated:
            return {"error": "Access denied: working directory is outside allowed directories"}
        if not os.path.isdir(validated):
            return {"error": f"Directory not found: {working_dir}"}

        try:
            rc, stdout, stderr = await _run_subprocess_shell(
                command,
                cwd=validated,
                timeout=timeout,
            )
            stdout = stdout[:4000] if stdout else ""
            stderr = stderr[:2000] if stderr else ""
            return {
                "returncode": rc,
                "stdout": stdout,
                "stderr": stderr,
            }
        except asyncio.TimeoutError:
            return {"error": f"Command timed out after {timeout}s"}

    # -----------------------------------------------------------------------
    # Memory commands — V1 MemoryManager commands removed (roadmap 8.6).
    # All memory operations are now handled by MemoryV2Plugin
    # (src/plugins/internal/memory_v2.py).
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Reference stub staleness scanning (Roadmap 6.3.4)
    # -----------------------------------------------------------------------

    async def _cmd_scan_stub_staleness(self, args: dict) -> dict:
        """Scan vault reference stubs for staleness by comparing source_hash.

        Walks the ``references/`` directory for one or all projects and checks
        each stub's ``source_hash`` frontmatter against the current content of
        the source file on disk.

        Returns a structured report with per-stub status and summary counts.
        """
        from src.reference_stub_enricher import scan_stale_stubs

        project_id = args.get("project_id") or self._active_project_id
        vault_projects_dir = os.path.join(self.config.data_dir, "vault", "projects")

        if not os.path.isdir(vault_projects_dir):
            return {"error": "Vault projects directory not found."}

        # Determine which projects to scan
        if project_id:
            project_ids = [project_id]
        else:
            # Scan all projects that have a references/ directory
            try:
                project_ids = sorted(
                    d
                    for d in os.listdir(vault_projects_dir)
                    if os.path.isdir(os.path.join(vault_projects_dir, d, "references"))
                )
            except OSError:
                return {"error": "Failed to list vault project directories."}

        if not project_ids:
            return {
                "projects": [],
                "summary": "No projects with reference stubs found.",
            }

        all_results = []
        totals = {
            "total": 0,
            "stale": 0,
            "missing_source": 0,
            "unenriched": 0,
            "orphaned": 0,
            "current": 0,
        }

        for pid in project_ids:
            scan = scan_stale_stubs(vault_projects_dir, pid)
            project_entry = {
                "project_id": pid,
                "total": scan.total,
                "stale": scan.stale,
                "missing_source": scan.missing_source,
                "unenriched": scan.unenriched,
                "orphaned": scan.orphaned,
                "current": scan.current,
                "stubs": [
                    {
                        "stub_name": s.stub_name,
                        "status": s.status,
                        "source_path": s.source_path,
                        "recorded_hash": s.recorded_hash,
                        "current_hash": s.current_hash,
                        "last_synced": s.last_synced,
                        "is_enriched": s.is_enriched,
                    }
                    for s in scan.stubs
                ],
            }
            all_results.append(project_entry)

            for key in totals:
                totals[key] += getattr(scan, key)

        # Build human-readable summary
        parts = []
        if totals["stale"]:
            parts.append(f"{totals['stale']} stale")
        if totals["missing_source"]:
            parts.append(f"{totals['missing_source']} missing source")
        if totals["unenriched"]:
            parts.append(f"{totals['unenriched']} unenriched")
        if totals["orphaned"]:
            parts.append(f"{totals['orphaned']} orphaned")
        if totals["current"]:
            parts.append(f"{totals['current']} current")

        summary = (
            f"Scanned {totals['total']} stub(s) across {len(project_ids)} project(s): "
            + ", ".join(parts)
            if parts
            else f"Scanned 0 stubs across {len(project_ids)} project(s)."
        )

        return {
            "projects": all_results,
            "totals": totals,
            "summary": summary,
        }

    # -----------------------------------------------------------------------
    # Prompt template commands -- read-only browsing of prompt templates
    # stored in <workspace>/prompts/.  Templates use YAML frontmatter for
    # metadata and Mustache-style {{variable}} placeholders for context
    # injection.  Modifications should only be done through tasks, not
    # through these commands.
    # -----------------------------------------------------------------------

    def _get_prompt_manager(self, workspace: str):
        """Create a PromptManager for the given workspace."""
        from src.prompt_manager import PromptManager

        prompts_dir = os.path.join(workspace, "prompts")
        return PromptManager(prompts_dir)

    async def _cmd_list_prompts(self, args: dict) -> dict:
        """List all prompt templates for a project, optionally filtered."""
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {
                "error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."
            }
        pm = self._get_prompt_manager(workspace)
        templates = pm.list_templates(
            category=args.get("category"),
            tag=args.get("tag"),
        )
        return {
            "project_id": args["project_id"],
            "prompts": [t.to_dict() for t in templates],
            "categories": pm.get_categories(),
            "total": len(templates),
        }

    async def _cmd_read_prompt(self, args: dict) -> dict:
        """Read a specific prompt template's content and metadata."""
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {
                "error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."
            }
        pm = self._get_prompt_manager(workspace)
        tmpl = pm.get_template(args["name"])
        if not tmpl:
            return {"error": f"Prompt template '{args['name']}' not found"}
        result = tmpl.to_dict()
        result["content"] = tmpl.body
        return result

    async def _cmd_render_prompt(self, args: dict) -> dict:
        """Render a prompt template with variable substitution."""
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {
                "error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."
            }
        pm = self._get_prompt_manager(workspace)
        variables = args.get("variables", {})
        rendered = pm.render(args["name"], variables)
        if rendered is None:
            return {"error": f"Prompt template '{args['name']}' not found"}
        return {
            "name": args["name"],
            "rendered": rendered,
            "variables_used": variables,
        }
