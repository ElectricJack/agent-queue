"""Worktree commands mixin — workspace slot inspection and repair.

Worktree-execution implementation spec §6.8 / design §9:

* ``workspace_doctor`` — read-only diagnosis.  Reports findings for
  drifted excludes, stale worktree registrations, dirty unlocked slots,
  expired merge-slot leases, and redundant clones under worktree mode.
* ``workspace_reap`` — explicit reap of retired slots.  Refuses live
  slots (liveness guard).  ``all_retired: true`` sweeps every retired
  slot in the caller's project scope.
* ``workspace_retire_claim`` — remove a slot's leftover claim file when
  its holder provably no longer holds it.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method
takes a flat ``dict`` of arguments and returns a ``dict`` — domain data
on success, ``{"error": "..."}`` on failure.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.models import KIND_MODE_WORKTREE
from src.workspace_registration import path_is_missing, workspace_missing_detail

logger = logging.getLogger(__name__)


class WorktreeCommandsMixin:
    """Worktree command methods mixed into CommandHandler."""

    async def _cmd_workspace_doctor(self, args: dict) -> dict:
        """Diagnose workspace inventory.  Read-only unless ``fix`` is set.

        Returns::
            {"success": True, "findings": [{"kind": str, "workspace_id": str,
                                            "detail": str}, ...]}

        Kinds emitted:
        * ``missing_workspace_path`` — a workspace row's directory is gone;
          restore it or explicitly remove its registration.
        * ``exclude_missing`` — Git's exact exclude file lacks the block.
        * ``exclude_unverifiable`` — resolving or reading that exact path
          failed, so doctor cannot claim the workspace is protected.
        * ``stale_registration`` — git worktree list has an entry whose
          directory no longer exists.
        * ``dirty_unlocked_slot`` — slot dir has uncommitted work but is
          not locked.
        * ``unlocked_slot_claim`` — an unlocked slot still holds a claim
          file, which claim preparation refuses as a foreign claim unless
          it can prove the holder let go.
        * ``merge_lease_expired`` — a project's merge slot is held past
          its lease.
        * ``redundant_clone`` — a git kind is in worktree mode but the
          project still has extra non-slot rows beyond the designated base.
        """
        from src.claim_file import read_claim_file
        from src.orchestrator.worktree_manager import resolve_managed_exclude_path

        project_id = args.get("project_id") or self._active_project_id
        findings: list[dict] = []

        try:
            projects = await self.db.list_projects()
        except Exception as e:
            return {"success": False, "error": f"list_projects failed: {e}"}

        if project_id:
            projects = [p for p in projects if p.id == project_id]

        for project in projects:
            workspaces = await self.db.list_workspaces(project_id=project.id)
            missing_ids = set()
            for ws in workspaces:
                if path_is_missing(ws.workspace_path):
                    missing_ids.add(ws.id)
                    findings.append({
                        "kind": "missing_workspace_path",
                        "workspace_id": ws.id,
                        "detail": workspace_missing_detail(ws),
                    })
            bases = [ws for ws in workspaces if not ws.is_slot]
            slots = [ws for ws in workspaces if ws.is_slot]

            # Merge slot expiry
            try:
                ms = await self.db.get_merge_slot(project.id)
            except Exception:
                ms = None
            if ms is not None and ms.holder_task_id is not None:
                import time as _time

                if ms.expires_at is not None and ms.expires_at < _time.time():
                    findings.append(
                        {
                            "kind": "merge_lease_expired",
                            "workspace_id": project.id,
                            "detail": (
                                f"merge slot held by {ms.holder_task_id} past lease"
                            ),
                        }
                    )

            # Redundant clones under worktree mode
            by_kind: dict[str, list] = {}
            for ws in bases:
                by_kind.setdefault(ws.kind_id or "", []).append(ws)
            for kind_id, kind_bases in by_kind.items():
                if not kind_id:
                    continue
                try:
                    kind = await self.db.resolve_workspace_kind(project.id, kind_id)
                except Exception:
                    kind = None
                if kind is None:
                    continue
                mode = getattr(kind, "mode", None)
                if mode != KIND_MODE_WORKTREE:
                    continue
                if len(kind_bases) <= 1:
                    continue
                # More than one non-slot row for a worktree-mode kind — the
                # designated base is `find_worktree_base`; the rest are
                # redundant and not acquirable.
                try:
                    designated = await self.db.find_worktree_base(
                        project.id, kind_id
                    )
                except Exception:
                    designated = None
                designated_id = designated.id if designated else None
                for ws in kind_bases:
                    if ws.id == designated_id:
                        continue
                    findings.append(
                        {
                            "kind": "redundant_clone",
                            "workspace_id": ws.id,
                            "detail": (
                                f"kind {kind_id!r} is in worktree mode; this "
                                f"clone is not acquirable"
                            ),
                        }
                    )

            # Managed excludes apply to every Git handoff, independently of
            # whether worktree slots are enabled. Slot-specific diagnosis
            # remains behind the rollout flag.
            worktrees_cfg = getattr(self.config, "worktrees", None)
            mgr_factory = getattr(self.orchestrator, "_worktree_slots", None)
            mgr = mgr_factory() if callable(mgr_factory) else None
            if mgr is None:
                continue

            for base in bases:
                if base.id in missing_ids:
                    continue
                try:
                    kind = await self.db.resolve_workspace_kind(
                        project.id, base.kind_id or "project-repo"
                    )
                except Exception:
                    kind = None
                if kind is not None and not kind.is_git_repo:
                    continue
                base_path = base.workspace_path
                try:
                    exclude = await resolve_managed_exclude_path(
                        self.orchestrator.git,
                        base_path,
                    )
                    needs_repair = not mgr.git_exclude_is_current_path(exclude)
                except Exception as exc:
                    findings.append(
                        {
                            "kind": "exclude_unverifiable",
                            "workspace_id": base.id,
                            "detail": f"managed exclude path cannot be verified: {exc}",
                        }
                    )
                    continue
                if needs_repair:
                    findings.append(
                        {
                            "kind": "exclude_missing",
                            "workspace_id": base.id,
                            "detail": f"{exclude} is missing our exclude block",
                        }
                    )

                if worktrees_cfg is None or not worktrees_cfg.enabled:
                    continue

                # Stale git worktree registrations
                try:
                    registered = await self.orchestrator.git.aworktree_list(base_path)
                except Exception:
                    registered = []
                for entry in registered:
                    p = entry.get("path")
                    if not p:
                        continue
                    if Path(p).resolve() == Path(base_path).resolve():
                        continue
                    if not Path(p).is_dir():
                        findings.append(
                            {
                                "kind": "stale_registration",
                                "workspace_id": base.id,
                                "detail": f"{p} is registered but the dir is gone",
                            }
                        )

            if worktrees_cfg is None or not worktrees_cfg.enabled:
                continue

            for slot in slots:
                if slot.locked_by_task_id:
                    continue
                slot_dir = slot.workspace_path
                if not Path(slot_dir).is_dir():
                    continue
                claim = read_claim_file(slot_dir)
                if claim is not None:
                    findings.append(
                        {
                            "kind": "unlocked_slot_claim",
                            "workspace_id": slot.id,
                            "detail": (
                                f"{slot_dir} is not locked but its claim file names "
                                f"task {claim.get('task_id')}; workspace_retire_claim "
                                "retires it once the holder provably let go"
                            ),
                        }
                    )
                try:
                    dirty = await self.orchestrator.git.ahas_uncommitted_changes(slot_dir)
                except Exception:
                    dirty = False
                if dirty:
                    findings.append(
                        {
                            "kind": "dirty_unlocked_slot",
                            "workspace_id": slot.id,
                            "detail": (
                                f"{slot_dir} has uncommitted work and is not locked"
                            ),
                        }
                    )

        return {"success": True, "findings": findings}

    async def _cmd_workspace_retire_claim(self, args: dict) -> dict:
        """Retire a slot's leftover ``.aq/claim.json`` when its holder let go.

        The supervisor's control for a slot that preparation refuses with
        ``retains a foreign claim``.  The marker is removed only under the
        same proofs claim preparation applies (``retire_stale_slot_claim``):
        the slot is unlocked, the holder task is terminal, paused, released
        or superseded, and no live session works in the slot.  Otherwise the
        claim may still be live and the command refuses, naming the holder.
        """
        from src.claim_file import read_claim_file

        ws_id = args.get("workspace_id")
        if not ws_id:
            return {"success": False, "error": "workspace_id is required"}
        ws = await self.db.get_workspace(ws_id)
        if ws is None:
            return {"success": False, "error": f"workspace {ws_id} not found"}
        if not ws.is_slot:
            return {"success": False, "error": f"{ws_id} is not a worktree slot"}
        claim = read_claim_file(ws.workspace_path)
        if claim is None:
            return {"success": True, "workspace_id": ws.id, "retired": False,
                    "detail": "no claim file"}
        retired = await self._retire_stale_slot_claim(
            ws, ws.workspace_path, actor="workspace_retire_claim",
        )
        if retired is None:
            return {
                "success": False,
                "workspace_id": ws.id,
                "holder_task_id": claim.get("task_id"),
                "error": (
                    f"claim of task {claim.get('task_id')} in {ws.id} is not provably "
                    "stale: the slot is locked, the holder still holds an active claim, "
                    "or a live session works there"
                ),
            }
        return {
            "success": True,
            "workspace_id": ws.id,
            "retired": True,
            "holder_task_id": retired.get("task_id"),
            "holder_claim_epoch": retired.get("claim_epoch"),
            "holder_session_id": retired.get("session_id"),
            "reason": retired["reason"],
        }

    async def _cmd_workspace_reap(self, args: dict) -> dict:
        """Explicit reap of retired slots.  Refuses live slots.

        Accepts ``workspace_id`` (one slot) or ``all_retired: true``
        (every retired slot in the caller's project scope).  Returns
        ``{"success": True, "reaped": [ids], "skipped": [{"id":..., "reason":...}]}``.
        """
        worktrees_cfg = getattr(self.config, "worktrees", None)
        if worktrees_cfg is None or not worktrees_cfg.enabled:
            return {"success": False, "error": "worktrees.enabled is false"}

        mgr = getattr(self.orchestrator, "_worktree_slots", None)
        if mgr is None or not callable(mgr):
            return {"success": False, "error": "worktree slot manager unavailable"}
        mgr = mgr()

        ws_id = args.get("workspace_id")
        all_retired = bool(args.get("all_retired"))
        if not ws_id and not all_retired:
            return {"success": False, "error": "workspace_id or all_retired required"}

        reaped: list[str] = []
        skipped: list[dict] = []

        if ws_id:
            ws = await self.db.get_workspace(ws_id)
            if ws is None:
                return {"success": False, "error": f"workspace {ws_id} not found"}
            if not ws.is_slot:
                return {"success": False, "error": f"{ws_id} is not a slot"}
            ok = await mgr.reap_slot(ws, reason="cli:workspace_reap")
            if ok:
                reaped.append(ws.id)
            else:
                skipped.append(
                    {"id": ws.id, "reason": "live or unknowable liveness"}
                )
            return {"success": True, "reaped": reaped, "skipped": skipped}

        # all_retired: sweep every slot beyond its project's cap that is
        # not locked.  Require an explicit project scope — a bare
        # ``all_retired`` with no active project would otherwise sweep
        # every project on the daemon, which is almost never intended.
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {
                "success": False,
                "error": (
                    "all_retired requires an explicit project_id (or an "
                    "active project) — refusing to sweep every project"
                ),
            }
        projects = await self.db.list_projects()
        projects = [p for p in projects if p.id == project_id]
        for project in projects:
            workspaces = await self.db.list_workspaces(project_id=project.id)
            cap = project.max_concurrent_agents or 0
            for ws in workspaces:
                if not ws.is_slot:
                    continue
                if ws.locked_by_task_id or ws.locked_by_agent_id:
                    skipped.append({"id": ws.id, "reason": "locked"})
                    continue
                if (ws.slot_index or 0) < cap:
                    skipped.append({"id": ws.id, "reason": "within cap"})
                    continue
                ok = await mgr.reap_slot(ws, reason="cli:workspace_reap all_retired")
                if ok:
                    reaped.append(ws.id)
                else:
                    skipped.append(
                        {"id": ws.id, "reason": "live or unknowable liveness"}
                    )
        return {"success": True, "reaped": reaped, "skipped": skipped}
