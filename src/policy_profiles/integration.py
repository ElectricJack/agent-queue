"""Portable integration inputs and target-local binding resolution."""

from __future__ import annotations

import copy
import json

from src.integration.models import HierarchicalIntegrationPolicy
from src.policy_profiles.models import IntegrationSettings


def settings(project, flow, engine: str) -> IntegrationSettings | None:
    if project.hierarchical_integration_mode == "development":
        # Development policies pin local task/artifact identities, unlike the
        # train policy. Other portable families must remain exportable.
        return None
    if (project.hierarchical_integration_policy is None
            and project.integration_repository_id is None
            and project.hierarchical_integration_mode == "disabled"):
        return None
    policy = project.hierarchical_integration_policy
    if policy is not None:
        policy = HierarchicalIntegrationPolicy.model_validate(policy).model_dump(mode="json")
        for boundary in (policy["parent"], policy["root"]):
            # Activation snapshots and task grants belong to the target. The
            # train's code engine needs no fabricated playbook artifacts.
            boundary.pop("route")
            boundary["authorized_task_ids"] = []
            boundary.pop("primary_profile_id", None)
            boundary.pop("verifier_profile_id", None)
            boundary["repair"].pop("debug_profile_id", None)
    flow = copy.deepcopy(flow)
    for step in flow or []:
        step.get("gate", {}).pop("operator_logins", None)
    return IntegrationSettings(
        engine=engine,
        integration_mode=project.integration_mode or "pull_request",
        hierarchical_integration_mode=project.hierarchical_integration_mode,
        integration_repository_id=project.integration_repository_id,
        default_branch=project.repo_default_branch or "main",
        policy=policy,
        promotion_flow=flow,
    )


def defaults(payload: IntegrationSettings | None) -> dict[str, str]:
    if payload is None:
        return {}
    values = {"integration_repository": payload.integration_repository_id or ""}
    for name, boundary in (payload.policy or {}).items():
        if name not in {"parent", "root"}:
            continue
        checks = boundary["required_checks"]
        for field in ("names", "version", "producer_id"):
            value = checks[field]
            values[f"integration_{name}_checks_{field}"] = (
                json.dumps(value) if field == "names" else value
            )
    if (payload.policy or {}).get("ci", {}).get("commands"):
        values["integration_ci_commands"] = json.dumps(payload.policy["ci"]["commands"])
    return values


def portable(data: dict) -> dict:
    if data["integration_repository_id"]:
        data["integration_repository_id"] = "{{integration_repository}}"
    for name in ("parent", "root"):
        if data["policy"] is None:
            break
        checks = data["policy"][name]["required_checks"]
        for field in ("names", "version", "producer_id"):
            checks[field] = "{{integration_" + name + "_checks_" + field + "}}"
    if (data["policy"] or {}).get("ci", {}).get("commands"):
        data["policy"]["ci"]["commands"] = "{{integration_ci_commands}}"
    return data


def updates(payload: IntegrationSettings, project, engine: str) -> dict:
    if payload.engine != engine:
        raise ValueError(
            f"integration engine requires {payload.engine}; importing install uses {engine}. "
            "Configure the installation engine through the operator cutover first."
        )
    if payload.default_branch != (project.repo_default_branch or "main"):
        raise ValueError("Change the default branch through integration cutover before importing")
    result = {
        "integration_mode": payload.integration_mode,
        "hierarchical_integration_mode": payload.hierarchical_integration_mode,
        "integration_repository_id": payload.integration_repository_id,
        "promotion_flow": copy.deepcopy(payload.promotion_flow),
    }
    if payload.policy is not None:
        policy = copy.deepcopy(payload.policy)
        existing = project.hierarchical_integration_policy or {}
        for name in ("parent", "root"):
            boundary = policy.get(name)
            if not isinstance(boundary, dict) or "route" in boundary:
                raise ValueError("portable integration boundaries must omit activation routes")
            route = existing.get(name, {}).get("route")
            if route is None:
                raise ValueError(
                    f"Configure a reviewed target-local {name} integration route first; "
                    "policy import cannot fabricate an artifact."
                )
            boundary["route"] = copy.deepcopy(route)
            # Explicit task admissions are local, never copied from another project.
            if boundary.get("authorized_task_ids"):
                raise ValueError("portable integration settings cannot grant task admissions")
            boundary["authorized_task_ids"] = existing.get(name, {}).get("authorized_task_ids", [])
        result["hierarchical_integration_policy"] = (
            HierarchicalIntegrationPolicy.model_validate(policy).model_dump(mode="json")
        )
    elif payload.hierarchical_integration_mode in {"hierarchy", "train"}:
        raise ValueError("active integration settings require a policy")
    return result
