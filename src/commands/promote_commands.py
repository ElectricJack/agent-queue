"""Read-only promotion schema and validation commands."""

from __future__ import annotations

import inspect
import json

from sqlalchemy import select

from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.commands.supervisor_authority import integration_operator
from src.database.tables import projects
from src.integration.promotion_steps import FlowSchema


class PromoteCommandsMixin:
    async def _cmd_promote_schema(self, args: dict) -> dict:
        """Print the promotion-flow JSON schema."""
        return {"success": True, "outcome": "schema", "schema": FlowSchema.schema()}

    async def _promotion_manifest(self, repository) -> dict:
        """Read trusted configuration at the designated default branch's exact SHA.

        A caller-supplied flow never supplies its own trust anchors. Missing
        trust fails the layer-3 membership checks rather than trusting defaults.
        """
        from src.integration.preflight import read_committed_trust_manifest

        resolver = getattr(self.orchestrator, "github_repository_binding_resolver", None)
        factory = getattr(self.orchestrator, "github_client_factory", None)
        if resolver is None or factory is None:
            return {}
        binding = resolver(repository)
        if inspect.isawaitable(binding):
            binding = await binding
        if binding is None:
            return {}
        client = factory(binding)
        if inspect.isawaitable(client):
            client = await client
        _sha, raw = await read_committed_trust_manifest(client, binding, repository.default_branch)
        document = json.loads(raw) if raw is not None else {}
        return document if isinstance(document, dict) else {}

    async def _cmd_promote_validate(self, args: dict) -> dict:
        """Validate a supplied or stored flow without changing project configuration."""
        from src.commands.contracts.integration import PromoteValidateArgs

        request = PromoteValidateArgs.model_validate(args)
        project_id = request.project_id
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.SESSION and principal.project_id is None:
            _label, refusal = await integration_operator(self.db, project_id)
            authorized = refusal is None
        elif principal.kind in {PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}:
            authorized = principal.project_id == project_id and not principal.unresolved
        else:
            authorized = principal.kind in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}
        if not authorized:
            return {
                "success": False,
                "outcome": "unauthorized",
                "error": "Project is outside caller scope.",
            }
        project = await self.db.get_project(project_id)
        if project is None:
            return {
                "success": False,
                "outcome": "not_found",
                "error": f"Project {project_id!r} does not exist.",
            }
        document = request.flow
        if request.use_stored:
            async with self.db._engine.connect() as conn:
                document = (
                    await conn.execute(
                        select(projects.c.promotion_flow).where(projects.c.id == project_id)
                    )
                ).scalar_one()
        repository_id = project.integration_repository_id
        repository = await self.db.get_repo(repository_id) if repository_id else None
        if repository is not None and repository.project_id != project_id:
            repository = None
        default_branch = repository.default_branch if repository else project.repo_default_branch
        if not default_branch:
            return {
                "success": False,
                "outcome": "not_found",
                "error": "Repository default branch is missing.",
            }
        result = FlowSchema.validate(document, default_branch=default_branch)
        # Structural/chain errors win over trust read failures. Empty flows
        # have no trust requirements and can validate before configuration.
        warnings = []
        if result.layer == 3 and result.flow:
            try:
                manifest = await self._promotion_manifest(repository) if repository else {}
            except Exception as exc:  # noqa: BLE001 - provider boundary; validation fails closed
                manifest = {}
                warnings.append(
                    {
                        "code": "trust_manifest_unavailable",
                        "pointer": "",
                        "message": str(exc),
                        "layer": 3,
                    }
                )
            result = FlowSchema.validate(document, default_branch=default_branch, manifest=manifest)
        remote = {}
        if request.remote and result.valid:
            from src.integration.promotion_steps import validate_promotion_remote

            try:
                binding, client, app_id = await self._promotion_client(repository)
                remote = await validate_promotion_remote(
                    client, binding, result.flow, default_branch=default_branch, app_id=app_id,
                )
                warnings.extend(remote.pop("warnings"))
            except Exception as exc:  # noqa: BLE001 - layer four is diagnostic, never a refusal
                warnings.append({
                    "code": "remote_unverifiable", "pointer": "", "layer": 4,
                    "message": f"Remote validation is unavailable: {type(exc).__name__}.",
                })
        return {
            "success": result.valid,
            "outcome": "valid" if result.valid else "invalid",
            "project_id": project_id,
            **result.as_dict(),
            "warnings": warnings,
            **remote,
        }

    # E1 read-only configuration commands. Keep separate from intent/PR
    # commands so those can register independently in the shared group.
    async def _promotion_client(self, repository):
        from src.git.github_contracts import credential_identity_from_client

        resolver = getattr(self.orchestrator, "github_repository_binding_resolver", None)
        factory = getattr(self.orchestrator, "github_client_factory", None)
        if repository is None or resolver is None or factory is None:
            raise ValueError("Repository client is unavailable.")
        binding = resolver(repository)
        if inspect.isawaitable(binding):
            binding = await binding
        if binding is None:
            raise ValueError("Repository binding is unavailable.")
        client = factory(binding)
        if inspect.isawaitable(client):
            client = await client
        if client is None or client.repository != binding:
            raise ValueError("Repository client does not match its binding.")
        identity = credential_identity_from_client(client)
        if identity.app_id is None:
            raise ValueError("Rulesets require the daemon's App identity.")
        return binding, client, identity.app_id

    async def _cmd_promote_rulesets(self, args: dict) -> dict:
        """Print admin-owned rulesets and copyable workflow triggers; never write GitHub."""
        from src.integration.promotion_steps import promotion_rulesets, promotion_workflow_triggers

        result = await self._cmd_promote_validate({**args, "remote": False})
        if not result.get("valid"):
            return result
        project = await self.db.get_project(args["project_id"])
        repository = await self.db.get_repo(project.integration_repository_id) \
            if project.integration_repository_id else None
        if repository is not None and repository.project_id != args["project_id"]:
            repository = None
        try:
            _binding, _client, app_id = await self._promotion_client(repository)
        except Exception as exc:  # noqa: BLE001 - configuration boundary
            return {"success": False, "outcome": "not_found", "error": str(exc)}
        default_branch = repository.default_branch
        return {
            **result, "outcome": "rulesets", "app_id": app_id,
            "rulesets": promotion_rulesets(result["flow"], default_branch=default_branch,
                                          app_id=app_id),
            "workflow_triggers": promotion_workflow_triggers(result["flow"],
                                                            default_branch=default_branch),
        }
