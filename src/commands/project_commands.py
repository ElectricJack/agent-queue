"""Project commands mixin — CRUD, scheduling and workspace management."""

from __future__ import annotations

import logging

from src.git.manager import GitError, RemoteRefState
from src.models import (
    Project,
    ProjectStatus,
    TaskStatus,
)
from src.commands.helpers import _count_by

logger = logging.getLogger(__name__)


class ProjectCommandsMixin:
    """Project command methods mixed into CommandHandler."""

    # -----------------------------------------------------------------------
    # Project commands -- CRUD and pause/resume.
    # Projects are the top-level grouping: each project has its own workspace
    # directory and scheduling weight.
    # -----------------------------------------------------------------------

    async def _cmd_get_status(self, args: dict) -> dict:
        filter_project = args.get("project_id")
        projects = await self.db.list_projects()
        tasks = await self.db.list_tasks(project_id=filter_project)

        in_progress = [
            {
                "id": t.id,
                "title": t.title,
                "project_id": t.project_id,
                "assigned_agent": t.assigned_agent_id,
            }
            for t in tasks
            if t.status == TaskStatus.IN_PROGRESS
        ]
        ready = [
            {"id": t.id, "title": t.title, "project_id": t.project_id}
            for t in tasks
            if t.status == TaskStatus.READY
        ]

        return {
            "projects": 1 if filter_project else len(projects),
            "tasks": {
                "total": len(tasks),
                "by_status": _count_by(tasks, lambda t: t.status.value),
                "in_progress": in_progress,
                "ready_to_work": ready,
            },
            "orchestrator_paused": self.orchestrator._paused,
            "graph_layout_enabled": bool(
                getattr(self.config, "graph_layout", None)
                and self.config.graph_layout.enabled
            ),
        }

    async def _cmd_list_projects(self, args: dict) -> dict:
        projects = await self.db.list_projects()
        result = []
        for p in projects:
            ws_path = await self.db.get_project_workspace_path(p.id)
            info = {
                "id": p.id,
                "name": p.name,
                "status": p.status.value,
                "credit_weight": p.credit_weight,
                "max_concurrent_agents": p.max_concurrent_agents,
                "workspace": ws_path,
            }
            if p.repo_url:
                info["repo_url"] = p.repo_url
            if p.assignment_playbook_id:
                info["assignment_playbook_id"] = p.assignment_playbook_id
            result.append(info)
        return {"projects": result}

    async def _cmd_create_project(self, args: dict) -> dict:
        name = args.get("name") or args.get("project_id")
        if not name:
            return {"error": "'name' is required to create a project"}
        project_id = name.lower().replace(" ", "-")

        if args.get("repo_url") or args.get("create_repo"):
            if args.get("repo_url") and args.get("create_repo"):
                return {"success": False, "error": "repo_url and create_repo are mutually exclusive"}
            roots = self.config.project_roots
            root_id = args.get("root_id") or (roots[0].id if len(roots) == 1 else None)
            if not root_id:
                return {"success": False, "error": "Choose a configured project root with --root-id"}
            from uuid import uuid4
            from src.projects.github import GitHubError, parse_github_repository

            request = {
                "request_id": args.get("request_id") or str(uuid4()),
                "root_id": root_id,
                "relative_path": args.get("relative_path") or project_id,
                "credit_weight": args.get("credit_weight", 1.0),
                "max_concurrent_agents": args.get("max_concurrent_agents", 2),
                "project_name": name,
                "project_id": project_id,
                "default_branch": args.get("default_branch"),
            }
            if args.get("create_repo"):
                try:
                    repo = parse_github_repository(args["create_repo"])
                except GitHubError as exc:
                    return {"success": False, "error": exc.message}
                request.update(source_mode="init", create_github=True,
                               github_owner=repo.owner, github_repo=repo.name,
                               github_visibility="private" if args.get("private", True) else "public")
            else:
                request.update(source_mode="github_clone", github_url=args["repo_url"])
            result = await self._cmd_onboard_project(request)
            if result.get("success"):
                result.update(created=project_id, name=name,
                              assignment_playbook_id=self.config.routing.default_router)
            return result

        project = Project(
            id=project_id,
            name=name,
            credit_weight=args.get("credit_weight", 1.0),
            max_concurrent_agents=args.get("max_concurrent_agents", 2),
            repo_url=args.get("repo_url", ""),
            repo_default_branch=args.get("default_branch", "main"),
            # Every project is bound to a router and has no default profile
            # (mandatory-routing spec §8): the router routes every task.
            assignment_playbook_id=self.config.routing.default_router,
        )
        await self.db.create_project(project)

        # Keep command-created and onboarding-created projects on one storage contract.
        from src.projects.storage import ensure_project_storage

        ensure_project_storage(self.config.data_dir, project_id)

        # No workspace yet (``add_workspace`` registers one later), so the
        # event carries no workspace fields.
        from src.projects.events import emit_project_created, project_created_payload

        await emit_project_created(
            getattr(self.orchestrator, "bus", None),
            project_created_payload(
                project_id=project_id,
                name=project.name,
                source="command",
                vault_root=self.config.vault_root,
            ),
        )

        return {
            "created": project_id,
            "name": project.name,
            "assignment_playbook_id": project.assignment_playbook_id,
        }

    async def _cmd_project_doctor(self, args: dict) -> dict:
        """Diagnose the local-first project authorization gap without changing it."""
        import shlex
        from src.projects.github import GitHubError, parse_github_repository

        project = await self.db.get_project(args["project_id"])
        if project is None:
            return {"success": False, "error": "Project not found"}
        path = await self.db.get_project_workspace_path(project.id)
        if project.repo_url or not path:
            return {"success": True, "project_id": project.id, "ready": bool(project.repo_url),
                    "code": "repository_bound" if project.repo_url else "workspace_missing"}
        remote = await self.orchestrator.git.aget_remote_url(path)
        try:
            url = parse_github_repository(remote or "").clone_https
        except GitHubError:
            url = "OWNER/REPOSITORY"
        command = shlex.join([
            "aq", "project", "bind-repository", project.id, "--repo-url", url,
            "--expected-repo-url", "", "--reason", "Authorize local-first project repository",
        ])
        return {"success": True, "project_id": project.id, "ready": False,
                "code": "repository_authorization_missing", "recovery_command": command,
                "message": "Local repository has no authorized remote. Review and run the binding command."}

    async def _cmd_pause_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        await self.db.update_project(pid, status=ProjectStatus.PAUSED)
        return {"paused": pid, "name": project.name}

    async def _cmd_resume_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        await self.db.update_project(pid, status=ProjectStatus.ACTIVE)
        # Wakes any ``task_claim`` long-poll blocked on ``not_admissible``
        # (swarm-work-model §10) — a paused project resuming is exactly the
        # kind of admission change a blocked claimer is waiting for.
        await self.orchestrator.bus.emit("project.resumed", {"project_id": pid})
        return {"resumed": pid, "name": project.name}

    async def _cmd_set_project_constraint(self, args: dict) -> dict:
        """Set a scheduling constraint on a project.

        Supports three constraint types that can be combined ("stacked"):
        - exclusive: only one agent may work on the project at a time
        - max_agents_by_type: per-agent-type concurrency limits
        - pause_scheduling: stop all new task assignments

        When called on a project that already has a constraint, the new
        fields are merged with the existing constraint — unspecified fields
        retain their previous values.
        """
        import time as _time

        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        # Load existing constraint (if any) for merge/stacking behavior.
        existing = await self.db.get_project_constraint(pid)

        exclusive = args.get("exclusive", existing.exclusive if existing else False)
        pause_scheduling = args.get(
            "pause_scheduling", existing.pause_scheduling if existing else False
        )
        max_agents_by_type = args.get(
            "max_agents_by_type",
            existing.max_agents_by_type if existing else {},
        )
        created_by = args.get("created_by", existing.created_by if existing else None)

        if not exclusive and not pause_scheduling and not max_agents_by_type:
            return {
                "error": (
                    "At least one constraint must be set: "
                    "exclusive, max_agents_by_type, or pause_scheduling."
                )
            }

        from src.models import ProjectConstraint

        constraint = ProjectConstraint(
            project_id=pid,
            exclusive=bool(exclusive),
            max_agents_by_type=max_agents_by_type if isinstance(max_agents_by_type, dict) else {},
            pause_scheduling=bool(pause_scheduling),
            created_by=created_by,
            created_at=_time.time(),
        )
        await self.db.set_project_constraint(constraint)

        active_fields = []
        if constraint.exclusive:
            active_fields.append("exclusive")
        if constraint.pause_scheduling:
            active_fields.append("pause_scheduling")
        if constraint.max_agents_by_type:
            active_fields.append(f"max_agents_by_type={constraint.max_agents_by_type}")

        return {
            "project_id": pid,
            "constraint_set": True,
            "active_fields": active_fields,
        }

    async def _cmd_release_project_constraint(self, args: dict) -> dict:
        """Release (remove) the scheduling constraint from a project.

        If specific fields are provided (exclusive, pause_scheduling, or
        max_agents_by_type), only those fields are cleared.  If no fields
        are specified, the entire constraint is removed.
        """
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        existing = await self.db.get_project_constraint(pid)
        if not existing:
            return {"error": f"No active constraint on project '{pid}'"}

        # Check if caller wants to release specific fields only.
        fields_to_release = args.get("fields", [])
        if fields_to_release:
            import time as _time

            from src.models import ProjectConstraint

            exclusive = existing.exclusive
            pause_scheduling = existing.pause_scheduling
            max_agents_by_type = dict(existing.max_agents_by_type)

            for f in fields_to_release:
                if f == "exclusive":
                    exclusive = False
                elif f == "pause_scheduling":
                    pause_scheduling = False
                elif f == "max_agents_by_type":
                    max_agents_by_type = {}

            # If all fields are now empty, remove the constraint entirely.
            if not exclusive and not pause_scheduling and not max_agents_by_type:
                await self.db.delete_project_constraint(pid)
                await self.orchestrator.bus.emit("constraint.released", {"project_id": pid})
                return {"project_id": pid, "constraint_released": True, "fields": "all"}

            updated = ProjectConstraint(
                project_id=pid,
                exclusive=exclusive,
                max_agents_by_type=max_agents_by_type,
                pause_scheduling=pause_scheduling,
                created_by=existing.created_by,
                created_at=_time.time(),
            )
            await self.db.set_project_constraint(updated)
            await self.orchestrator.bus.emit("constraint.released", {"project_id": pid})
            return {
                "project_id": pid,
                "constraint_released": False,
                "fields_released": fields_to_release,
                "remaining_fields": [
                    k
                    for k, v in [
                        ("exclusive", exclusive),
                        ("pause_scheduling", pause_scheduling),
                        ("max_agents_by_type", bool(max_agents_by_type)),
                    ]
                    if v
                ],
            }

        # Release the entire constraint.
        await self.db.delete_project_constraint(pid)
        await self.orchestrator.bus.emit("constraint.released", {"project_id": pid})
        return {"project_id": pid, "constraint_released": True, "fields": "all"}

    async def _cmd_bind_project_repository(self, args: dict) -> dict:
        """Authorize an initialized project's first repository, never a reassignment."""
        from src.commands.supervisor_authority import operator_or_supervisor
        from src.projects.github import GitHubError, parse_github_repository

        operator_id, refusal = await operator_or_supervisor(
            self.db, None, subject="repository binding",
        )
        if refusal:
            return {"success": False, "error_code": "global_operator_required", "error": refusal}
        required = {"project_id", "repo_url", "expected_repo_url", "reason"}
        if set(args) != required or any(not isinstance(args.get(k), str) for k in required):
            return {"success": False, "error_code": "invalid_arguments",
                    "error": (
                        "Provide project_id, repo_url, expected_repo_url and reason as strings"
                    )}
        if not args["project_id"].strip() or not args["reason"].strip():
            return {"success": False, "error_code": "invalid_arguments",
                    "error": "Project ID and audit reason must be nonempty"}
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in args["repo_url"]):
            return {"success": False, "error_code": "invalid_repository_url",
                    "error": "Repository URL contains control characters"}
        try:
            repository = parse_github_repository(args["repo_url"])
        except GitHubError as exc:
            return {"success": False, "error_code": "invalid_repository_url", "error": exc.message}
        project = await self.db.get_project(args["project_id"])
        if project is None:
            return {"success": False, "error_code": "project_not_found",
                    "error": "Project not found"}
        # Avoid credential selection for stale requests or a reassignment.
        # The transactional helper repeats both checks after the network read.
        if project.repo_url != args["expected_repo_url"]:
            return {"success": False, "error_code": "repository_binding_stale",
                    "error": "The expected repository URL is stale"}
        if project.repo_url and project.repo_url != repository.clone_https:
            return {"success": False, "error_code": "repository_already_bound",
                    "error": "An existing repository cannot be reassigned"}
        if not project.repo_url:
            try:
                await self._github_client().validate_repository(repository.html_url)
            except GitHubError as exc:
                return {"success": False, "error_code": exc.code.value, "error": exc.message}
        operator_id, refusal = await operator_or_supervisor(
            self.db, None, subject="repository binding",
        )
        if refusal:
            return {"success": False, "error_code": "global_operator_required", "error": refusal}
        return await self.db.bind_project_repository(
            project.id, repo_url=repository.clone_https,
            expected_repo_url=args["expected_repo_url"], reason=args["reason"].strip(),
            operator_id=operator_id,
        )

    async def _cmd_edit_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        if "repo_url" in args or "expected_repo_url" in args:
            return {"success": False, "error_code": "repository_binding_required",
                    "error": "Use bind_project_repository for an audited first repository binding"}
        if "review_delegate_to" in args:
            from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal

            principal = current_principal() or TRUSTED_LOCAL
            if principal.kind is not PrincipalKind.LOCAL:
                return {
                    "success": False,
                    "error_code": "local_operator_only",
                    "error": "review delegation requires local operator",
                }
            if args["review_delegate_to"] not in {"", "user", "supervisor", None}:
                return {
                    "success": False,
                    "error_code": "local_operator_only",
                    "error": "review_delegate_to must be user, supervisor, or empty",
                }
        from src.integration.records import PolicyActivation

        rollout_fields = {
            "hierarchical_integration_desired_mode",
            "hierarchical_integration_draining",
            "hierarchical_integration_generation",
        }
        if rollout_fields.intersection(args):
            return {
                "error": (
                    "Derived rollout fields cannot be edited directly; configure policy "
                    "with an explicit expected generation."
                )
            }
        sensitive = {
            key: args[key]
            for key in (
                "hierarchical_integration_mode",
                "integration_repository",
                "integration_repository_id",
                "hierarchical_integration_policy",
                "integration_mode",
                "promotion_flow",
            )
            if key in args
        }
        if sensitive:
            from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
            from src.commands.supervisor_authority import integration_operator

            principal = current_principal() or TRUSTED_LOCAL
            if "promotion_flow" in sensitive and principal.kind is not PrincipalKind.LOCAL:
                return {
                    "success": False,
                    "error": "local_operator_only",
                    "message": "A promotion flow is set by the operator only.",
                }
            if principal.kind is PrincipalKind.LOCAL:
                operator_id = principal.describe()
            elif principal.kind is PrincipalKind.SESSION:
                # The supervisor drives the train cutover end to end: it may
                # bind the repository, review mode and policy, but only as a
                # live named supervisor of this project, the gate every
                # operator integration control uses (dispatch has already
                # checked the ``edit_project`` grant).  The disabled-and-drained
                # state and the generation CAS below still apply to it exactly
                # as to LOCAL.
                operator_id, refusal = await integration_operator(self.db, pid)
                if refusal is not None:
                    return {"error": f"Integration configuration refused: {refusal}"}
            else:
                return {
                    "error": (
                        "Integration configuration requires LOCAL operator or supervisor "
                        "authority"
                    )
                }
            if "expected_integration_generation" not in args:
                return {"error": "expected_integration_generation is required"}
            if "promotion_flow" in sensitive:
                return await self._configure_promotion_flow(
                    project, sensitive, args, operator_id=operator_id
                )
            return await PolicyActivation(self.db).configure(
                pid,
                updates=sensitive,
                expected_generation=int(args["expected_integration_generation"]),
                reason=str(args.get("reason") or "configure hierarchical integration"),
                operator_id=operator_id,
            )
        updates = {}
        if "name" in args:
            updates["name"] = args["name"]
        if "credit_weight" in args:
            updates["credit_weight"] = args["credit_weight"]
        if "max_concurrent_agents" in args:
            updates["max_concurrent_agents"] = args["max_concurrent_agents"]
        if "budget_limit" in args:
            updates["budget_limit"] = args["budget_limit"]
        if "assignment_playbook_id" in args:
            refusal = await self._router_binding_refusal(project, args["assignment_playbook_id"])
            if refusal is not None:
                return refusal
            updates["assignment_playbook_id"] = str(args["assignment_playbook_id"]).strip()
        if "repo_default_branch" in args:
            updates["repo_default_branch"] = args["repo_default_branch"]
        if "review_delegate_to" in args:
            updates["review_delegate_to"] = args["review_delegate_to"] or None
        if "git_identity_name" in args or "git_identity_email" in args:
            identity_updates = _git_identity_updates(project, args)
            if "error" in identity_updates:
                return identity_updates
            updates.update(identity_updates)
        if not updates:
            return {
                "error": (
                    "No fields to update. Provide name, credit_weight, "
                    "max_concurrent_agents, budget_limit, "
                    "assignment_playbook_id, repo_default_branch, "
                    "review_delegate_to, or git_identity_name/git_identity_email."
                )
            }
        await self.db.update_project(pid, **updates)
        if "assignment_playbook_id" in updates:
            from src.routing.readiness import RouterReadiness

            readiness = getattr(self.orchestrator, "router_readiness", None)
            if isinstance(readiness, RouterReadiness):
                # The claim frontier reads the cached ready set; re-read it
                # now rather than on the next cycle.
                await readiness.refresh()
        return {"updated": pid, "fields": list(updates.keys())}

    async def _router_binding_refusal(self, project, playbook_id) -> dict | None:
        """Why *project* may not be bound to *playbook_id* (routing spec §8), or ``None``.

        Re-binding is the local operator's alone.  The playbook must be a
        router -- an activated artifact of it grants ``task_route_apply`` --
        with an enabled system activation or one scoped to this project.  A
        project is never unbound: the column is NOT NULL.
        """
        from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
        from src.routing.readiness import BINDING_READY, artifact_grants, binding_state

        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL:
            return {
                "success": False,
                "error_code": "local_operator_only",
                "error": "binding a project to a router requires the local operator",
            }
        bound = str(playbook_id or "").strip()
        if not bound:
            return {
                "success": False,
                "error_code": "router_required",
                "error": (
                    "a project is always bound to a router; name a routing playbook "
                    f"(the default is '{self.config.routing.default_router}')"
                ),
            }
        state, detail = binding_state(
            project.id,
            bound,
            await self.db.list_playbook_activations(),
            artifact_grants(self.orchestrator, self.config),
        )
        if state != BINDING_READY:
            return {"success": False, "error_code": f"router_{state}", "error": detail}
        return None

    async def _configure_promotion_flow(
        self, project, updates: dict, args: dict, *, operator_id: str
    ) -> dict:
        """Activate a promotion flow: layers 1-4, then one fenced write (§3.11).

        The trust manifest is read at the designated default branch first. A
        dry run of the fence then runs every refusal check, missing chain
        targets are created with no database lock held (git network work never
        waits inside the fence), and the write re-runs the checks behind the
        same generation. A refusal writes no row; the only state one can leave
        behind is a create-only branch at its source's tip.
        """
        from src.integration.promotion_steps import FlowSchema
        from src.integration.records import PolicyActivation

        pid = project.id
        repository_id = project.integration_repository_id
        repository = await self.db.get_repo(repository_id) if repository_id else None
        if repository is not None and repository.project_id != pid:
            repository = None
        default_branch = repository.default_branch if repository else project.repo_default_branch
        # Structural and chain errors win over a trust read failure, as in
        # ``promote validate``; the fence re-runs layers 1-3 against the fenced
        # default branch.
        precheck = FlowSchema.validate(
            updates["promotion_flow"], default_branch=default_branch or ""
        )
        if precheck.layer < 3:
            problems = [problem.as_dict() for problem in precheck.problems]
            return {"success": False, "outcome": "invalid", "project_id": pid,
                    "error": problems[0]["code"], "problems": problems}
        manifest: dict | None = None
        if precheck.flow:
            if repository is None:
                return {
                    "success": False,
                    "outcome": "blocked",
                    "project_id": pid,
                    "error": "integration_repository_required",
                    "message": "Designate the project's integration repository before "
                    "activating a promotion flow.",
                }
            try:
                manifest = await self._promotion_manifest(repository)
            except Exception as exc:  # noqa: BLE001 - provider boundary; activation fails closed
                return {
                    "success": False,
                    "outcome": "blocked",
                    "error": "trust_manifest_unavailable",
                    "message": str(exc),
                }
        activation = PolicyActivation(self.db)
        fence = {
            "expected_generation": int(args["expected_integration_generation"]),
            "reason": str(args.get("reason") or "configure promotion flow"),
            "operator_id": operator_id,
            "promotion_manifest": manifest,
        }
        result = await activation.configure(pid, updates=dict(updates), dry_run=True, **fence)
        created: dict[str, str] = {}
        if result.get("outcome") == "checked":
            flow = result["updates"]["promotion_flow"] or []
            refusal, created = await self._create_flow_targets(pid, repository, project, flow)
            if refusal is not None:
                result = {"project_id": pid, "generation": result["generation"], **refusal}
            else:
                result = await activation.configure(pid, updates=updates, **fence)
        if created:
            # A create-only branch outlives a refused write; name it so the
            # operator can delete it or simply retry, which reuses it.
            result["created_targets"] = created
        success = result.get("outcome") == "configured"
        recorded = getattr(self.orchestrator, "promotion_flow_problems", None)
        if success and isinstance(recorded, dict):
            # The daemon-start finding described the flow this write replaced.
            recorded.pop(pid, None)
        if not success and result.get("outcome") == "stale":
            result.setdefault("error", "integration_generation_stale")
        return {"success": success, **result}

    async def _create_flow_targets(
        self, pid, repository, project, flow
    ) -> tuple[dict | None, dict[str, str]]:
        """Layer 4 at activation: create the flow's absent targets, create-only."""
        from src.integration.promotion_steps import create_missing_targets

        if not flow:
            return None, {}
        checkout = await self.db.get_project_workspace_path(pid)
        if not checkout:
            return {
                "outcome": "blocked",
                "error": "workspace_unavailable",
                "message": "No project workspace can create the missing targets.",
            }, {}
        repository_url = (getattr(repository, "url", None) or project.repo_url) or None
        return await create_missing_targets(
            self.orchestrator.git, checkout, flow, repository_url=repository_url
        )

    async def _cmd_set_default_branch(self, args: dict) -> dict:
        """Set (or change) a project's default branch.

        If the branch does not exist on the remote yet, it is created by
        pushing the tip of the old default branch (or, when that was never
        pushed, the workspace ``HEAD``) to the new name.  Both are daemon
        pushes under the exact-OID contract in ``docs/specs/git.md``: the
        source is resolved once and that object id is what reaches origin.
        """
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        branch = args.get("branch", "").strip()
        if not branch:
            return {"error": "branch is required"}

        old_branch = project.repo_default_branch or "main"

        # If the project has a workspace, optionally create the branch
        # on the remote when it doesn't exist yet.
        ws_path = await self.db.get_project_workspace_path(pid)
        branch_created = False
        if ws_path:
            git = self.orchestrator.git
            try:
                # Fetch latest so we know what branches exist on the remote
                await git._arun(["fetch", "origin"], cwd=ws_path)

                # Check if the branch exists on the remote
                try:
                    await git._arun(
                        ["rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
                        cwd=ws_path,
                    )
                except Exception:
                    # Branch does not exist on the remote — create it from
                    # the current default branch (or HEAD).  No local branch
                    # is created: the exact-OID push updates
                    # ``origin/<branch>`` itself, which is what every reader
                    # of the default branch consults.
                    try:
                        old_remote = f"refs/remotes/origin/{old_branch}"
                        try:
                            await git._arun(
                                ["rev-parse", "--verify", f"{old_remote}^{{commit}}"],
                                cwd=ws_path,
                            )
                        except Exception:
                            # The recorded default was never pushed, so the
                            # workspace HEAD is the only source — and nothing on
                            # origin has vetted that tree.  A root delivery gates
                            # every tracked path for daemon bookkeeping before
                            # pushing the same resolved OID.
                            try:
                                published_oid = await git.apush_validated_delivery(
                                    ws_path, None, "HEAD", branch)
                            except GitError as exc:
                                if str(exc).startswith("reserved delivery paths:"):
                                    return {
                                        "error": (
                                            f"refusing to create {branch} from the workspace "
                                            f"HEAD: {exc}"
                                        )
                                    }
                                raise
                        else:
                            # The content is already on origin; pushing the
                            # remote-tracking ref's exact OID is the whole delivery.
                            published_oid = await git.apush_validated_ref(ws_path, old_remote, branch)
                        branch_created = True
                        verified = await git.als_remote_ref(ws_path, branch)
                        if verified.state is not RemoteRefState.PRESENT or verified.oid != published_oid:
                            raise GitError("new default branch was not verified on origin")
                    except Exception as exc:
                        return {"error": f"could not verify/create default branch {branch}: {exc}"}
            except Exception as exc:
                logger.warning(
                    "Could not verify/create branch %s for project %s: %s",
                    branch,
                    pid,
                    exc,
                )

        await self.db.update_project(pid, repo_default_branch=branch)

        result: dict = {
            "project_id": pid,
            "default_branch": branch,
            "previous_branch": old_branch,
            "status": "updated",
        }
        if branch_created:
            result["branch_created"] = True
        return result

    async def _cmd_get_project(self, args: dict) -> dict:
        """Return full details for a single project."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        ws_path = await self.db.get_project_workspace_path(pid)
        usage = await self.db.get_project_token_usage(pid)
        info: dict = {
            "id": project.id,
            "name": project.name,
            "status": project.status.value,
            "repo_url": project.repo_url or "",
            "repo_default_branch": project.repo_default_branch,
            "workspace": ws_path,
            "credit_weight": project.credit_weight,
            "max_concurrent_agents": project.max_concurrent_agents,
            "total_tokens_used": project.total_tokens_used,
            "tokens_used_recent": usage,
        }
        if project.budget_limit is not None:
            info["budget_limit"] = project.budget_limit
        if project.assignment_playbook_id:
            info["assignment_playbook_id"] = project.assignment_playbook_id
        from src.git.identity import resolve_git_identity

        info["git_identity_name"] = project.git_identity_name
        info["git_identity_email"] = project.git_identity_email
        info["git_identity"] = resolve_git_identity(self.config, project).as_dict()
        return info

    async def _cmd_delete_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        from src.integration.records import PolicyActivation

        if (
            project.hierarchical_integration_mode != "disabled"
            or project.hierarchical_integration_desired_mode != "disabled"
            or project.hierarchical_integration_draining
            or await PolicyActivation(self.db).has_active_work(pid)
        ):
            return {
                "error": (
                    "Cannot delete: hierarchical integration must be disabled and drained "
                    "with no live rollout projection."
                )
            }
        tasks = await self.db.list_tasks(project_id=pid, status=TaskStatus.IN_PROGRESS)
        if tasks:
            return {
                "error": f"Cannot delete: {len(tasks)} task(s) currently IN_PROGRESS. "
                "Stop them first."
            }

        try:
            await self.db.delete_project(pid)
        except Exception as exc:
            from src.database.queries.hierarchy_queries import HierarchyError

            if isinstance(exc, HierarchyError):
                return {
                    "success": False,
                    "code": f"hierarchy.{exc.code}",
                    "error": f"hierarchy.{exc.code}: {exc.detail}",
                    **(exc.context or {}),
                }
            raise
        return {"deleted": pid, "name": project.name}


def _git_identity_updates(project, args: dict) -> dict:
    """``edit_project``'s identity override: set as a pair, or clear both to inherit.

    A field not supplied keeps its stored value, so changing only the email of
    an existing override works; the result must still be a full pair or none.
    """
    from src.commands.git_identity_commands import agent_identity_refusal
    from src.git.identity import GitIdentityError, validate_git_email, validate_git_name

    refusal = agent_identity_refusal()
    if refusal is not None:
        return refusal
    def text(value):
        return value.strip() if isinstance(value, str) else ("" if value is None else value)

    name = text(args.get("git_identity_name", project.git_identity_name))
    email = text(args.get("git_identity_email", project.git_identity_email))
    if not name and not email:
        return {"git_identity_name": None, "git_identity_email": None}
    if not name or not email:
        return {
            "success": False,
            "error_code": "invalid_git_identity",
            "error": (
                "A project Git identity override needs both git_identity_name and "
                "git_identity_email; clear both to inherit the installation default."
            ),
        }
    try:
        return {
            "git_identity_name": validate_git_name(name),
            "git_identity_email": validate_git_email(email),
        }
    except GitIdentityError as exc:
        return {
            "success": False,
            "error_code": "invalid_git_identity",
            "field": f"git_identity_{exc.field}",
            "error": str(exc),
        }
