"""Non-executing release admission over exact Git migration trees."""

from __future__ import annotations

import hashlib
import json

from src.database.additive_migrations import check_range
from src.git.manager import GitError
from src.integration.promotion_notes import NotesRefusal, ensure_commit

MAX_FINDINGS = 100


async def migration_sources(ops, repo, sha):
    if sha is None:
        return {}
    paths = (await ops.run(repo, "ls-tree", "-rz", "--name-only", sha,
                           "--", "migrations/versions/")).split("\0")
    sources = {}
    for path in paths:
        if not path.endswith(".py") or path.endswith("/__init__.py"):
            continue
        # ops.run trims whitespace; preserve exact file contents for the
        # checker's immutable historical-migration comparison and line numbers.
        result = await ops.git.arun_git_result(
            ["--no-replace-objects", "show", sha + ":" + path], cwd=str(repo.store),
        )
        if result.returncode:
            raise GitError(result.stderr or "Pinned migration cannot be read")
        sources[path] = result.stdout
    return sources


async def migration_admission(ops, repo, step, source, previous, override):
    boundary = previous["sha"] if previous else step["notes"].get("bootstrap_sha")
    if boundary:
        await ensure_commit(ops, repo, boundary)
        if not await ops.is_ancestor(repo, boundary, source):
            raise NotesRefusal("migration_range_invalid", "Migration boundary is outside source history.")
    try:
        before = await migration_sources(ops, repo, boundary)
        after = await migration_sources(ops, repo, source)
        findings = [str(item) for item in check_range(before, after)] if before or after else []
    except (ValueError, SyntaxError) as exc:
        # An invalid graph is not an operator-waivable compatibility finding.
        raise NotesRefusal("migrations_not_additive", f"Migration graph cannot be audited: {exc}") from exc
    evidence = {
        "schema": "aq.migration-admission.v1",
        "range": {"previous": boundary, "current": source},
        "previous_tag": previous["tag"] if previous else None,
        "status": "breaking" if findings else "additive",
        "finding_count": len(findings),
        "findings": [item[:512] for item in findings[:MAX_FINDINGS]],
        "findings_sha256": hashlib.sha256(json.dumps(findings).encode()).hexdigest(),
        "override": override if findings else None,
    }
    while len(json.dumps(evidence, sort_keys=True).encode()) > 60_000:
        evidence["findings"].pop()
    evidence["findings_truncated"] = evidence["findings"] != findings
    if findings and not override:
        refusal = NotesRefusal(
            "migrations_not_additive",
            "Migration compatibility is unproven. A local operator may explicitly request "
            "--allow-breaking-migration; code rollback across this release will be disabled.",
        )
        refusal.migration_admission = evidence
        raise refusal
    return evidence
