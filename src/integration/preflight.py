"""Read-only functional dependency checks for integration enablement."""

from __future__ import annotations

import base64
import inspect
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError

from src.integration.attestation import _parse_trust_manifest
from src.integration.ci import TRUST_MANIFEST_PATH
from src.integration.models import HierarchicalIntegrationPolicy


_HOSTED_VARIABLES = (
    "AQ_INTEGRATION_ATTESTATION_APP_ID",
    "AQ_INTEGRATION_REQUIRED_CHECK_VERSION",
)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _artifact_matches(definition: Any, route: Any) -> bool:
    artifact = route.artifact
    try:
        return bool(
            definition.id == route.playbook_id
            and definition.schema_version == artifact.schema_generation
            and definition.source_hash == artifact.source_digest
            and definition.contract_fingerprint() == artifact.contract_fingerprint
            and definition.version == artifact.version
        )
    except (AttributeError, TypeError, ValueError):
        return False


async def _read_trust(client: Any, binding: Any, default_branch: str) -> Any:
    path = quote(TRUST_MANIFEST_PATH, safe="/")
    ref = quote(default_branch, safe="")
    payload = await client.request_json(
        "GET", f"/repos/{binding.full_name}/contents/{path}?ref={ref}"
    )
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise ValueError("trust content response is malformed")
    encoded = "".join(payload["content"].split())
    raw = base64.b64decode(encoded, validate=True)
    return _parse_trust_manifest(raw)


async def _read_hosted_variables(client: Any, binding: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in _HOSTED_VARIABLES:
        payload = await client.request_json(
            "GET", f"/repos/{binding.full_name}/actions/variables/{name}"
        )
        if payload.get("name") != name or not isinstance(payload.get("value"), str):
            raise ValueError("hosted variable response is malformed")
        values[name] = payload["value"]
    return values


async def daemon_functional_preflight(
    orchestrator: Any, project_id: str, repository_id: str
) -> tuple[str, ...]:
    """Read only the dependencies and repository configuration used at runtime."""
    blockers: list[str] = []
    factory = getattr(orchestrator, "integration_app_client_factory", None)
    resolver = getattr(orchestrator, "integration_repository_binding_resolver", None)
    runtime = getattr(orchestrator, "playbook_manager", None)
    if factory is None:
        blockers.append("provider_not_wired")
    if resolver is None:
        blockers.append("repository_binding_not_wired")
    if runtime is None:
        blockers.append("playbook_runtime_not_wired")
    if getattr(orchestrator, "integration_attestation_service", None) is None:
        blockers.append("attestation_not_wired")
    if getattr(orchestrator, "root_promotion_service", None) is None:
        blockers.append("promotion_not_wired")
    if getattr(orchestrator, "integration_cleanup_service", None) is None:
        blockers.append("cleanup_not_wired")
    if getattr(orchestrator, "git", None) is None:
        blockers.append("git_transport_not_wired")

    project = await orchestrator.db.get_project(project_id)
    repository = await orchestrator.db.get_repo(repository_id)
    binding = None
    if project is None or repository is None or repository.project_id != project_id:
        blockers.append("repository_mismatch")
    elif resolver is not None:
        try:
            binding = await _resolve(resolver(repository))
            if binding is None:
                blockers.append("repository_binding_failed")
        except Exception:
            blockers.append("repository_binding_failed")

    policy = None
    try:
        policy = HierarchicalIntegrationPolicy.model_validate(
            project.hierarchical_integration_policy if project is not None else None
        )
    except (ValidationError, TypeError):
        pass

    if policy is not None:
        class_ids = set(getattr(orchestrator, "intelligence_classes", None) or ())
        profile_ids = {profile.id for profile in await orchestrator.db.list_profiles()}
        store = getattr(runtime, "_store", None)
        for boundary in (policy.parent, policy.root):
            required_classes = {
                boundary.primary_intelligence_class,
                boundary.repair.debug_intelligence_class,
            }
            required_profiles = {
                boundary.primary_profile_id,
                boundary.repair.debug_profile_id,
            }
            if policy.branchless_parent == "verifier":
                required_classes.add(boundary.verifier_intelligence_class)
                required_profiles.add(boundary.verifier_profile_id)
            if None in required_classes or not required_classes.issubset(class_ids):
                blockers.append("intelligence_route_unavailable")
            if None in required_profiles or not required_profiles.issubset(profile_ids):
                blockers.append("profile_route_unavailable")
            try:
                definition = store.load(boundary.route.artifact.artifact_sha256)
            except Exception:
                blockers.append("route_artifact_unavailable")
            else:
                if not _artifact_matches(definition, boundary.route):
                    blockers.append("route_artifact_mismatch")

    client = None
    if factory is not None and binding is not None:
        try:
            client = await _resolve(factory(binding))
            if client is None or client.repository != binding:
                client = None
                blockers.append("provider_binding_failed")
        except Exception:
            blockers.append("provider_binding_failed")

    trust = None
    if client is not None and repository is not None and repository.default_branch:
        try:
            trust = await _read_trust(client, binding, repository.default_branch)
        except Exception:
            blockers.append("trust_manifest_unavailable")
        if trust is not None and policy is not None:
            root_checks = policy.root.required_checks
            producer_ids = {
                policy.parent.required_checks.producer_id,
                policy.root.required_checks.producer_id,
            }
            app_id = getattr(getattr(client, "config", None), "app_id", None)
            if (
                trust.canonical_repository_id != repository_id
                or trust.repository_id != binding.repository_id
                or trust.full_name != binding.full_name
                or trust.attestation_app_id != app_id
                or producer_ids != {str(trust.ci_producer_app_id)}
                or trust.required_checks.version != root_checks.version
                or trust.required_checks.names != root_checks.names
            ):
                blockers.append("trust_manifest_mismatch")

        try:
            variables = await _read_hosted_variables(client, binding)
        except Exception:
            blockers.append("hosted_workflow_variables_unavailable")
        else:
            app_id = getattr(getattr(client, "config", None), "app_id", None)
            required_version = (
                policy.root.required_checks.version if policy is not None else None
            )
            if (
                variables
                != {
                    "AQ_INTEGRATION_ATTESTATION_APP_ID": str(app_id),
                    "AQ_INTEGRATION_REQUIRED_CHECK_VERSION": required_version,
                }
                or (
                    trust is not None
                    and variables
                    != {
                        "AQ_INTEGRATION_ATTESTATION_APP_ID": str(
                            trust.attestation_app_id
                        ),
                        "AQ_INTEGRATION_REQUIRED_CHECK_VERSION": (
                            trust.required_checks.version
                        ),
                    }
                )
            ):
                blockers.append("hosted_workflow_variables_mismatch")
    elif binding is not None and repository is not None:
        blockers.extend(
            ("trust_manifest_unavailable", "hosted_workflow_variables_unavailable")
        )

    return tuple(dict.fromkeys(blockers))


__all__ = ["daemon_functional_preflight"]
