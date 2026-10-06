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
        if request.remote and result.valid:
            warnings.append(
                {
                    "code": "not_implemented",
                    "pointer": "",
                    "layer": 4,
                    "message": "Remote promotion validation is available in phase 2.",
                }
            )
        return {
            "success": result.valid,
            "outcome": "valid" if result.valid else "invalid",
            "project_id": project_id,
            **result.as_dict(),
            "warnings": warnings,
        }
