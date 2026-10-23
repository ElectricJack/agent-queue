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
