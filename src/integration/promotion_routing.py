"""Resolve a recorded hotfix origin against the project's configured chain."""

from src.integration.promotion_steps import flow_targets


def promotion_origin_target(default_branch, flow, origin, repository_id):
    """Only a live origin in the same repository can select a chain target."""
    if (
        origin
        and origin.get("repository_id") == repository_id
        and origin.get("retired_at") is None
        and origin.get("parent_repository_id") in {None, repository_id}
    ):
        parent = (origin.get("parent_ref") or "").removeprefix("refs/heads/")
        if parent in flow_targets(flow):
            return parent
    return default_branch.removeprefix("refs/heads/")
