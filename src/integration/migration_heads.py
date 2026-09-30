"""Read Alembic declarations from reviewed objects without executing branch code."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

REVIEWED_FILE_GUARDS = frozenset(
    {
        "resolved_paths_do_not_match_reviewed_source",
        "deleted_reviewed_path_was_restored",
        "reviewed_path_was_not_resolved",
        "added_reviewed_path_does_not_match_source",
    }
)


@dataclass(frozen=True)
class MigrationHead:
    path: str
    revision: str
    down_revisions: tuple[str, ...]

    @property
    def scope(self) -> str:
        return str(PurePosixPath(self.path).parent)


def declaration(path: str, source: str) -> MigrationHead:
    values = {}
    for node in ast.parse(source, filename=path).body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        for name in names:
            if name in {"revision", "down_revision"}:
                values[name] = ast.literal_eval(node.value)
    revision = values.get("revision")
    down = values.get("down_revision")
    if down is not None and not isinstance(down, (str, tuple, list)):
        raise ValueError(f"{path}: Alembic down_revision must be a literal revision sequence")
    parents = () if down is None else (down,) if isinstance(down, str) else tuple(down)
    if (
        not isinstance(revision, str)
        or not revision
        or any(not isinstance(parent, str) or not parent for parent in parents)
        or "down_revision" not in values
    ):
        raise ValueError(f"{path}: Alembic revision/down_revision must be literal declarations")
    return MigrationHead(path, revision, parents)


class MigrationInspector:
    """Use the integration service's retained repository and authenticated Git reads."""

    def __init__(self, promotion):
        self.promotion = promotion

    async def __call__(self, member: dict) -> tuple[MigrationHead, ...]:
        resolved = await self.promotion._resolve_repository(member["repository_id"])
        await self.promotion._ensure_retained_repository(resolved)
        git = self.promotion.git
        store = str(resolved.retained_git_dir)
        async with git.arepository_transaction(store):
            # Fetch exact objects, rather than reading the current branch tip.
            binding = await git.bind_github_repository(resolved.origin_url)
            for oid in (member["source_base"], member["source_head"]):
                await git.afetch_repository_oid(
                    store,
                    repository=binding,
                    oid=oid,
                    destination_ref=f"refs/aq/migration-inspection/{oid}",
                )
            delta = await git.arun_git_result(
                [
                    "diff",
                    "--no-renames",
                    "--diff-filter=A",
                    "--name-only",
                    "-z",
                    member["source_base"],
                    member["source_head"],
                ],
                cwd=store,
            )
            if delta.returncode:
                raise RuntimeError(delta.stderr or "cannot inspect reviewed migration delta")
            heads = []
            for path in delta.stdout.split("\0"):
                parts = PurePosixPath(path).parts
                if (
                    not path.endswith(".py")
                    or len(parts) < 3
                    or parts[-2] != "versions"
                    or parts[-3] not in {"migrations", "alembic"}
                    or parts[-1] == "__init__.py"
                ):
                    continue
                source = await git.arun_git_result(
                    ["show", f"{member['source_head']}:{path}"],
                    cwd=store,
                )
                if source.returncode:
                    raise RuntimeError(source.stderr or f"cannot read reviewed migration {path}")
                heads.append(declaration(path, source.stdout))
            return tuple(heads)


def select_members(
    members: list[dict],
    inspected: dict,
    *,
    dependencies: dict | None = None,
    delivered: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Keep the first ordered member of each revision/sibling-head collision."""
    revisions = {}
    parents = {}
    accepted, deferred = [], []
    placed = set(delivered or ())
    for member in members:
        if (dependencies or {}).get(member["task_id"], set()) - placed:
            continue
        key = (
            member["task_id"],
            member["repository_id"],
            member["source_base"],
            member["source_head"],
        )
        heads = inspected[key]
        conflict = None
        for head in heads:
            first = revisions.get((head.scope, head.revision))
            if first is None:
                first = next(
                    (
                        parents[head.scope, parent]
                        for parent in head.down_revisions or (None,)
                        if (head.scope, parent) in parents
                    ),
                    None,
                )
            if first is not None:
                conflict = head, first
                break
        if conflict:
            head, first = conflict
            deferred.append(
                {
                    "task_id": member["task_id"],
                    "source_head": member["source_head"],
                    "source_branch": member.get("source_branch"),
                    "conflicts_with": first,
                    "path": head.path,
                    "revision": head.revision,
                    "down_revisions": list(head.down_revisions),
                    "reason": "alembic_head_collision",
                }
            )
            continue
        accepted.append(member)
        placed.add(member["task_id"])
        for head in heads:
            revisions[head.scope, head.revision] = member["task_id"]
            for parent in head.down_revisions or (None,):
                parents[head.scope, parent] = member["task_id"]
    return accepted, deferred
