"""Resolve a recorded hotfix origin against the project's configured chain."""

from src.integration.promotion_steps import flow_targets


def resolve_origin_parent(default_branch, origin, repository_id, cutover=None):
    """Resolve old root routing without changing the immutable filing origin."""
    parent = (origin.get("parent_ref") or "").removeprefix("refs/heads/")
    if (cutover and cutover["repository_id"] == repository_id
            and origin.get("repository_id") == repository_id
            and origin.get("retired_at") is None
            and origin.get("parent_repository_id") in {None, repository_id}
            and origin.get("parent_task_id") is None
            and origin["created_at"] < cutover["cutover_at"]
            and parent == cutover["old_default"].removeprefix("refs/heads/")):
        return default_branch.removeprefix("refs/heads/")
    return parent


def cutover_origin_clause(cutover, repository_id):
    """SQL counterpart of root resolution for pending/admitted source queries."""
    from sqlalchemy import and_, false, or_
    from src.database.tables import task_branch_origins as origin

    if not cutover or cutover["repository_id"] != repository_id:
        return false()
    old = cutover["old_default"].removeprefix("refs/heads/")
    return and_(origin.c.repository_id == repository_id, origin.c.retired_at.is_(None),
                or_(origin.c.parent_repository_id.is_(None),
                    origin.c.parent_repository_id == repository_id),
                origin.c.parent_task_id.is_(None), origin.c.created_at < cutover["cutover_at"],
                origin.c.parent_ref.in_((old, "refs/heads/" + old)))


def promotion_origin_target(default_branch, flow, origin, repository_id, cutover=None):
    """Only a live origin in the same repository can select a chain target."""
    if (
        origin
        and origin.get("repository_id") == repository_id
        and origin.get("retired_at") is None
        and origin.get("parent_repository_id") in {None, repository_id}
    ):
        parent = resolve_origin_parent(default_branch, origin, repository_id, cutover)
        if parent in flow_targets(flow):
            return parent
    return default_branch.removeprefix("refs/heads/")
