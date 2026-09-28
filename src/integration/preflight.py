"""Read-only functional dependency checks for integration enablement."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any, Self

from pydantic import ValidationError

from src.git.github_contracts import (
    GitHubCredentialIdentity,
    GitHubCredentialMode,
    credential_identity_from_client,
)
from src.integration import app_mode
from src.integration.ci import TRUST_MANIFEST_PATH
from src.integration.models import HierarchicalIntegrationPolicy


class FunctionalPreflight(tuple):
    """Blocker codes, plus the ``warnings`` that never block readiness.

    A tuple of blocker codes, so every caller that reads codes keeps working;
    App mode adds its non-blocking warnings (spec §6.2) alongside.
    """

    warnings: tuple[str, ...]

    def __new__(
        cls, blockers: Iterable[str] = (), warnings: Iterable[str] = ()
    ) -> Self:
        value = super().__new__(cls, dict.fromkeys(blockers))
        value.warnings = tuple(dict.fromkeys(warnings))
        return value


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _definition_scope_matches(definition: Any, route: Any) -> bool:
    scope = definition.scope
    if getattr(scope, "type", None) != route.scope:
        return False
    if route.scope == "system":
        return route.scope_identifier == ""
    if route.scope == "project":
        return getattr(scope, "project_id", None) == route.scope_identifier
    return False


def _artifact_matches(definition: Any, route: Any) -> bool:
    artifact = route.artifact
    try:
        return bool(
            definition.id == route.playbook_id
            and _definition_scope_matches(definition, route)
            and definition.schema_version == artifact.schema_generation
            and definition.source_hash == artifact.source_digest
            and definition.contract_fingerprint() == artifact.contract_fingerprint
            and definition.version == artifact.version
        )
    except (AttributeError, TypeError, ValueError):
        return False


async def read_committed_trust_manifest(
    client: Any, binding: Any, default_branch: str
) -> tuple[str, bytes | None]:
    """``(default-branch SHA, raw manifest bytes)`` read through the App client.

    The file is read at the SHA, not the branch name, so the bytes are the
    ones that SHA carries even if the branch moves between the two reads.
    ``None`` means the file is absent there; any other failure raises.
    """
    sha = await client.exact_head_ref(default_branch)
    if sha is None:
        raise ValueError(f"default branch {default_branch!r} does not exist")
    return sha, await app_mode.read_file_at(client, binding, TRUST_MANIFEST_PATH, sha)


async def daemon_functional_preflight(
    orchestrator: Any, project_id: str, repository_id: str
) -> FunctionalPreflight:
    """Read only the dependencies and repository configuration used at runtime.

    Under App credentials the repository half is the App-mode item report
    (:func:`src.integration.app_mode.evaluate`), the one ``aq integration
    app-verify`` returns: every ``fail`` code is a blocker of the same name and
    every ``warn`` code a warning.
    """
    blockers: list[str] = []
    factory = getattr(orchestrator, "github_client_factory", None)
    resolver = getattr(orchestrator, "github_repository_binding_resolver", None)
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
    credential_identity: GitHubCredentialIdentity | None = None
    if factory is not None and binding is not None:
        try:
            client = await _resolve(factory(binding))
            if client is None or client.repository != binding:
                client = None
                blockers.append("provider_binding_failed")
            else:
                credential_identity = credential_identity_from_client(client)
        except Exception:
            client = None
            credential_identity = None
            blockers.append("provider_binding_failed")

    if (
        client is not None
        and credential_identity is not None
        and credential_identity.mode is GitHubCredentialMode.EXISTING_LOGIN
    ):
        # Existing gh credentials replace App installation and hosted-variable
        # setup. Repository identity and access still come from GitHub, never
        # from a task's claims or a locally authored manifest.
        try:
            remote = await client.request_json("GET", f"/repos/{binding.full_name}")
            if (
                type(remote.get("id")) is not int
                or remote["id"] != binding.repository_id
                or remote.get("full_name") != binding.full_name
            ):
                blockers.append("repository_mismatch")
            permissions = remote.get("permissions")
            if not isinstance(permissions, dict) or permissions.get("push") is not True:
                blockers.append("repository_write_permission_missing")
        except Exception:
            blockers.append("github_auth_unavailable")
        if policy is not None:
            from src.integration.ci import ci_trust_from_policy

            try:
                for boundary in ("parent", "root"):
                    ci_trust_from_policy(
                        canonical_repository_id=repository_id,
                        repository_id=binding.repository_id,
                        full_name=binding.full_name,
                        policy=policy,
                        boundary=boundary,
                    )
            except (TypeError, ValueError):
                blockers.append("ci_policy_invalid")
        return FunctionalPreflight(blockers)

    if client is None or credential_identity is None:
        # Nothing App-mode can be read without a bound client; the binding
        # blocker above names the cause.
        return FunctionalPreflight(blockers)

    report = await app_mode.evaluate(
        app_mode.AppModeContext(
            project_id=project_id,
            repository_id=repository_id,
            default_branch=repository.default_branch,
            binding=binding,
            client=client,
            identity=credential_identity,
            policy=policy,
        )
    )
    return FunctionalPreflight([*blockers, *report.blockers], report.warnings)


__all__ = [
    "FunctionalPreflight",
    "daemon_functional_preflight",
    "read_committed_trust_manifest",
]
