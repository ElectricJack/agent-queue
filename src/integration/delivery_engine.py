"""Shared, read-only diagnostics for projects without a delivery engine."""

from __future__ import annotations

from collections.abc import Mapping

DELIVERY_MODES = frozenset({"development", "hierarchy", "train"})


def _field(project, name, default=None):
    return project.get(name, default) if isinstance(project, Mapping) else getattr(project, name, default)


def engine_refusal(mode: str, config, *, orchestrator=None) -> str | None:
    """Use the live service or offline startup config; observe/disabled need no writer."""
    if mode in {"disabled", "observe", None}:
        return None
    if mode not in DELIVERY_MODES:
        return f"unsupported integration mode {mode!r}; select train, hierarchy or development"
    service = getattr(orchestrator, "integration_service", None)
    if service is not None:
        if mode in service.delivery_modes():
            return None
        return f"the running integration service has no engine for integration mode {mode!r}"
    integration = getattr(config, "integration", None)
    if integration is None:
        return "integration runtime configuration is unavailable"
    if getattr(integration, "git_first", "shadow") == "active":
        # The train also delivers development; the absent legacy runtime is expected.
        if orchestrator is not None and getattr(orchestrator, "integration_train", None) is None:
            return "integration.git_first is active but the integration train is not running"
    elif not getattr(integration, "reconciler_active", True):
        return "integration.reconciler_active is false; delivery visits are paused"
    if orchestrator is not None and hasattr(orchestrator, "integration_service"):
        return "the integration delivery service is not running"
    return None


async def project_delivery_issue(db, project, config, *, orchestrator=None) -> dict | None:
    mode = _field(project, "hierarchical_integration_mode", "disabled")
    if mode in {"disabled", "observe"}:
        return None
    reason = engine_refusal(mode, config, orchestrator=orchestrator)
    repository_id = _field(project, "integration_repository_id")
    if reason is None:
        repository = await db.get_repo(repository_id) if repository_id else None
        if repository is None:
            reason = "the designated integration repository is missing"
        elif _field(repository, "project_id") != _field(project, "id"):
            reason = "the designated integration repository belongs to another project"
    if reason is None:
        return None
    project_id = _field(project, "id")
    remedy = (
        "Restore the selected delivery engine and bind this project's integration repository "
        "with `aq project set <id>`, or explicitly disable integration."
    )
    return {
        "project_id": project_id, "mode": mode, "repository_id": repository_id,
        "reason": reason, "remedy": remedy,
        "detail": f"no delivery engine for this project ({project_id}, mode={mode}): {reason}. {remedy}",
    }


async def project_delivery_issues(db, projects, config, *, orchestrator=None) -> list[dict]:
    return [issue for project in sorted(projects, key=lambda item: _field(item, "id"))
            if (issue := await project_delivery_issue(db, project, config,
                                                     orchestrator=orchestrator)) is not None]
