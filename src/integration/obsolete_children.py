"""Read-only detection of a child's edits to paths its parent has removed.

Missing paths are a reason to ask for a disposition, never delivery evidence.
New files must not look obsolete merely because the parent has not merged them.
"""

import shlex

from src.git.manager import GitError, is_valid_git_oid
from src.integration.provenance import GitProvenance


async def obsolete_child_blocker(observed, *, task_id, source_base, source_oid):
    """Return a diagnostic only for a nonempty diff of existing, now absent paths.

    All probes use the observed immutable OIDs. Unavailable Git leaves the normal
    pending/unknown diagnosis intact. Callers own target and identity freshness.
    """
    if observed.error or not all(is_valid_git_oid(oid or "") for oid in (
        source_base, source_oid, observed.target_oid,
    )):
        return None
    try:
        if not await GitProvenance(
            observed.git, observed.store, repository_url=observed.repository_url,
        ).strict_ancestor(
            source_base, source_oid,
        ):
            return None
        diff = await observed.git.arun_git_result([
            "--no-replace-objects", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
            "--name-status", "-z",
            source_base, source_oid, "--",
        ], cwd=observed.store)
        if diff.returncode:
            return None
        fields = diff.stdout.rstrip("\0").split("\0") if diff.stdout else []
        if not fields or len(fields) % 2 or any(
            status not in {"M", "D", "T"} for status in fields[::2]
        ):
            return None
        paths = set(fields[1::2])
        tree = await observed.git.arun_git_result([
            "--no-replace-objects", "--literal-pathspecs", "ls-tree",
            "-r", "-t", "--name-only", "-z", observed.target_oid, "--", *sorted(paths),
        ], cwd=observed.store)
        if tree.returncode or paths.intersection(tree.stdout.rstrip("\0").split("\0")):
            return None
    except (GitError, OSError):
        return None
    remedy = (f"aq integration redrive-child {shlex.quote(task_id)} --dispose-obsolete "
              f"--superseded-by <parent-removal-commit> --apply --head {source_oid} "
              f"--parent-head {observed.target_oid} "
              "--reason '<explain why this work is obsolete>'")
    return {
        "code": "obsolete_child", "ref": task_id, "task_id": task_id,
        "detail": "Every changed path existed at the child's base and is absent from the "
                  f"parent tree; after reviewing the source, dispose it with `{remedy}`",
        "source_head": source_oid, "target_head": observed.target_oid,
        "target_ref": observed.target_ref,
        "remedy": remedy,
    }
