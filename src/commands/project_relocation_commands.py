"""Operator-only relocation through the common command boundary."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.commands.operator_authority import local_or_operator
from src.git.manager import GitError
from src.integration.checks import ChecksState, ExactChecks, RequiredChecks
from src.integration.development_policy import project_development_pin
from src.integration.runtime_contracts import HeadIdentity
from src.playbooks.artifact_store import ArtifactStore
from src.projects.relocation import ProjectRelocation, RelocationRefused


class ProjectRelocationCommandsMixin:
    async def _cmd_project_relocate(self, args: dict) -> dict:
        if not local_or_operator("project_relocate"):
            return {"success": False, "error": "relocation requires local operator authority"}

        async def policy_reader(project):
            pin = project_development_pin(project)
            if pin is None:
                raise RelocationRefused("project has no current playbook binding")
            definition = await asyncio.to_thread(
                ArtifactStore(self.config.compiled_root).load, pin.artifact_sha256,
            )
            if definition.scope.project_id != project["id"]:
                raise RelocationRefused("playbook belongs to another project")
            table = definition.integration_policy.tables.get("root_batch")
            required = table.required_checks if table else None
            if required is None:
                raise RelocationRefused("current playbook must declare its required checks")
            return {"artifact": pin.artifact_sha256, "required": required.model_dump(mode="json")}

        async def checks_reader(plan, paths):
            # Read the existing exact-commit cache, never accept caller-supplied
            # green assertions. Final exact-SHA evidence follows the same lifetime
            # as the train; missing or unfinished evidence requires normal CI refresh.
            required = RequiredChecks.model_validate(plan["policy"]["required"])
            checks = ExactChecks(self.db, SimpleNamespace(required=required))
            from src.projects.storage_binding import bound_path

            for repo in plan["snapshot"]["repos"]:
                source = repo["checkout_base_path"] or repo["source_path"]
                if not source:
                    raise RelocationRefused("repository has no local source checkout")
                destination = bound_path({"paths": paths}, source)
                sha = await self.orchestrator.git.arev_parse(str(destination), "HEAD")
                head = HeadIdentity(repository_id=repo["id"], ref="HEAD", sha=sha, generation=0)
                result = await checks.read(head)
                if result.state != ChecksState.GREEN:
                    raise RelocationRefused("currently required exact-SHA CI is not green: "
                                             + ", ".join(required.names))

        service = ProjectRelocation(self.db, self.config, self.orchestrator.git,
                                    policy_reader=policy_reader, checks_reader=checks_reader)
        try:
            common = {"owner_inventory": args["owner_inventory"]}
            if args.get("apply"):
                return await service.apply(args["project_id"], args["destination"],
                                           expected_digest=args["expected_digest"], **common)
            return await service.preview(args["project_id"], args["destination"], **common)
        except (ValueError, OSError, KeyError, GitError) as exc:
            return {"success": False, "error_code": "relocation_refused", "error": str(exc)}
