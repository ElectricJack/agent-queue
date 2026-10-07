"""Preparation and exact-source notes admission over real Git and PostgreSQL."""

import asyncio
import copy
import hashlib
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.database.tables import (
    archived_tasks, events, integration_batches, projects, task_branch_origins,
    task_completion_records, task_labels, tasks,
)
from src.git.github_contracts import GitHubAccessError
from src.integration.promotion_notes import (
    assemble_notes_input, draft_notes, notes_metadata, previous_tag, render_release_notes,
)
from src.profiles.capabilities import DENY_ALL
from tests.test_integration_gitops import commit, git, setup as setup
from tests.test_promote_commands import promote_env as promote_env, request
from tests.test_promotion_steps import promotion as promotion


async def configure(e, *, notes_kind="none", bootstrap=None):
    step = copy.deepcopy(e.meta["step"])
    step["notes"] = {"kind": notes_kind}
    if notes_kind != "none":
        step["notes"]["path"] = "notes/{version}.md" if notes_kind == "file_template" else "CHANGELOG.md"
    if bootstrap:
        step["notes"]["bootstrap_sha"] = bootstrap
    async with e.db._engine.begin() as conn:
        await conn.execute(update(projects).values(
            promotion_flow=[step], hierarchical_integration_mode="train",
        ))
    return step


async def notes(e, step, head, previous=None):
    async with e.db._engine.connect() as conn:
        return await assemble_notes_input(conn, e.ops, e.repo, project_id="p", repository_id="r",
                                          step=step, head=head, previous=previous)


async def task_row(e, identity, *, kind="feature", archived=False, summary="Delivered feature"):
    async with e.db._engine.begin() as conn:
        table = archived_tasks if archived else tasks
        values = dict(id=identity, project_id="p", repo_id="r", title=identity + " title",
                      task_type=kind, description="", status="COMPLETED", created_at=1, updated_at=2)
        if archived:
            values["archived_at"] = 3
        await conn.execute(insert(table).values(**values))
        if summary:
            await conn.execute(insert(task_completion_records).values(
                id="completed-" + identity, task_id=identity, outcome="pass", summary=summary,
                completed_at=2,
            ))


def merge_source(e, head, source, identity):
    git(e.repo.store, "checkout", "--detach", head)
    git(e.repo.store, "merge", "--no-ff", "-m", f"Land {identity}\n\nAQ-Source: {identity}@{source}", source)
    return git(e.repo.store, "rev-parse", "HEAD")


@pytest.mark.parametrize("bump,version", [("minor", "0.3.0"), ("patch", "0.2.1")])
async def test_prepare_files_one_ordinary_root_pinned_to_default(promote_env, bump, version):
    e = promote_env
    await configure(e, notes_kind="file_template", bootstrap=e.base)
    args = {"project_id": "p", "step_id": "release", "bump": bump}
    first, second = await asyncio.gather(e.handler._cmd_promote_prepare(args),
                                        e.handler._cmd_promote_prepare(args))
    assert {first["outcome"], second["outcome"]} == {"prepared", "prepare_in_progress"}, (first, second)
    result = first if first["success"] else second
    assert result["version"] == version
    async with e.db._engine.connect() as conn:
        task = (await conn.execute(select(tasks))).mappings().one()
        assert task["task_type"] == "chore" and task["parent_task_id"] is None
        assert task["created_by_kind"] == "promotion_prepare" and task["status"] == "DEFINED"
        assert task["profile_id"] is None
        origin = (await conn.execute(select(task_branch_origins))).mappings().one()
        assert origin["parent_ref"] == "dev" and origin["base_sha"] == e.source
        assert await conn.scalar(select(integration_batches.c.id)) is None
    assert await e.db.get_task_meta(task["id"], "notes_input") == result["notes_input"]
    assert "0.3.0" in result["draft"] if bump == "minor" else "0.2.1" in result["draft"]
    assert e.github.created == 0
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    assert git(e.ops.git.remote_path, "rev-parse", "dev") == e.source


async def test_prepare_refuses_unversioned_and_nonincreasing_version(promote_env):
    e = promote_env
    step = await configure(e)
    git(e.repo.store, "tag", "v0.2.0", e.source)
    git(e.repo.store, "push", "origin", "refs/tags/v0.2.0")
    result = await e.handler._cmd_promote_prepare({"project_id": "p", "step_id": "release", "version": "0.1.9"})
    assert result["outcome"] == "version_not_increasing", result
    step["versioning"] = {"kind": "none"}
    async with e.db._engine.begin() as conn:
        await conn.execute(update(projects).values(promotion_flow=[step]))
    result = await e.handler._cmd_promote_prepare({"project_id": "p", "step_id": "release", "bump": "patch"})
    assert result["outcome"] == "step_not_versioned", result


@pytest.mark.parametrize("condition,outcome", [
    ("failure", "promotion_source_red"), ("missing", "promotion_source_pending"),
    ("outage", "promotion_source_unavailable"), ("unknown", "promotion_source_not_on_chain"),
    ("sibling", "promotion_source_not_on_chain"),
])
async def test_request_refuses_source_before_any_intent_or_pr(promote_env, condition, outcome):
    e = promote_env
    source = e.source
    before_refs = git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/")
    if condition == "missing":
        e.github.runs.clear()
    elif condition == "outage":
        e.github.paged_items = AsyncMock(side_effect=GitHubAccessError("transient", "CI unavailable"))
    elif condition == "unknown":
        source = "f" * 40
    elif condition == "sibling":
        source = commit(e.repo.store, {"outside.txt": "outside\n"}, base=e.base)
        e.github.runs[source] = "success"
    else:
        e.github.runs[source] = condition
    result = await request(e, source_sha=source)
    assert result["outcome"] == outcome, result
    assert not result["success"] and e.github.created == 0
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.id)) is None
        assert await conn.scalar(select(integration_batches.c.id)) is None
    assert git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/") == before_refs


async def test_nested_epic_includes_child_migrations_archived_summary_and_reverts(promote_env):
    e = promote_env
    step = await configure(e, notes_kind="file_template", bootstrap=e.source)
    feature = commit(e.repo.store, {"feature.txt": "feature\n",
                     "migrations/versions/new.py": "migration\n"}, base=e.source)
    side = commit(e.repo.store, {"epic.txt": "epic\n"}, base=e.source)
    epic = merge_source(e, side, feature, "child")
    default = commit(e.repo.store, {"default.txt": "default\n"}, base=e.source)
    head = merge_source(e, default, epic, "epic")
    await task_row(e, "child", archived=True, summary="Adds an archived feature")
    await task_row(e, "epic", kind="chore", summary=None)
    async with e.db._engine.begin() as conn:
        await conn.execute(insert(task_labels).values(task_id="epic", label="breaking"))
        await conn.execute(insert(events), [
            {"event_type": "label.added", "task_id": "child", "project_id": "p",
             "payload": "breaking", "timestamp": 1},
            {"event_type": "label.added", "task_id": "child", "project_id": "p",
             "payload": "removed", "timestamp": 1.5},
            {"event_type": "label.removed", "task_id": "child", "project_id": "p",
             "payload": "removed", "timestamp": 2},
        ])
    value = await notes(e, step, head)
    assert {s["task"] for s in value["sources"]} == {"epic", "child"}
    child = next(s for s in value["sources"] if s["task"] == "child")
    assert child["summary"] == "Adds an archived feature" and child["migrations"] == ["migrations/versions/new.py"]
    assert child["labels"] == ["breaking"]
    assert value["range"]["base"] == e.source
    rendered = render_release_notes(value)
    assert "### Added" in rendered and "### Upgrade" in rendered
    assert "Adds an archived feature" in rendered and "epic title [no summary]" in rendered
    assert "### Breaking\n\n- epic title [no summary]" in rendered
    git(e.repo.store, "revert", "--no-edit", feature)
    reverted = await notes(e, step, git(e.repo.store, "rev-parse", "HEAD"))
    assert next(s for s in reverted["sources"] if s["task"] == "child")["reverted"]
    assert "(reverted)" in render_release_notes(reverted)


@pytest.mark.parametrize("kind", ["file_template", "changelog_heading"])
async def test_formats_prior_hotfix_exclusion_and_prepare_digest_stability(promote_env, kind):
    e = promote_env
    step = await configure(e, notes_kind=kind, bootstrap=e.source)
    hotfix = commit(e.repo.store, {"hotfix.txt": "hotfix\n"}, base=e.source)
    feature = commit(e.repo.store, {"new.txt": "feature\n"}, base=e.source)
    base_input = {"sources": [{"task": "hotfix", "sha": hotfix}], "source_digest": "old",
                  "range": {"base": e.base, "head": hotfix}, "migration_files": [], "bare_commits": []}
    # A patch on the lower chain already shipped this hotfix identity; its
    # later merge on dev must not repeat its notes in the next release.
    base_input["sources"][0].update(type="fix", title="hotfix", summary="Fix", labels=[], migrations=[], reverted=False)
    path = step["notes"]["path"].format(version="0.1.9")
    previous_sha = commit(e.repo.store, {path: draft_notes(base_input, kind=kind, version="0.1.9")}, base=e.source)
    git(e.repo.store, "tag", "v0.1.9", previous_sha)
    git(e.repo.store, "push", "origin", "refs/tags/v0.1.9")
    previous = await previous_tag(e.ops, e.repo, step)
    assert previous["sha"] == previous_sha
    head = merge_source(e, previous_sha, hotfix, "hotfix")
    head = merge_source(e, head, feature, "feature")
    await task_row(e, "hotfix", kind="fix")
    await task_row(e, "feature")
    value = await notes(e, step, head, previous)
    assert [s["task"] for s in value["sources"]] == ["feature"]
    assert value["source_digest"] == hashlib.sha256(f"feature@{feature}".encode()).hexdigest()
    draft = draft_notes(value, kind=kind, version="0.2.0")
    assert notes_metadata(draft, kind, "0.2.0")["source_digest"] == value["source_digest"]
    prep = commit(e.repo.store, {step["notes"]["path"].format(version="0.2.0"): draft}, base=head)
    await task_row(e, "prepare", kind="chore")
    async with e.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "prepare").values(created_by_kind="promotion_prepare"))
    head = merge_source(e, head, prep, "prepare")
    after = await notes(e, step, head, previous)
    assert after["source_digest"] == value["source_digest"]


@pytest.mark.parametrize("kind", ["file_template", "changelog_heading"])
async def test_pr_preserves_authored_notes_and_renders_operator_input(promote_env, kind):
    e = promote_env
    step = await configure(e, notes_kind=kind, bootstrap=e.source)
    value = await notes(e, step, e.source)
    body = draft_notes(value, kind=kind, version="0.2.0") + "\nHuman release edit.\n"
    if kind == "changelog_heading":
        body += "\n## [0.1.0]\nOld release must stay out of this PR.\n"
    source = commit(e.repo.store, {step["notes"]["path"].format(version="0.2.0"): body}, base=e.source)
    git(e.repo.store, "push", "origin", source + ":refs/heads/dev")
    e.github.runs[source] = "success"
    result = await request(e, notes_reviewed=True)
    assert result["success"], result
    assert "## Release notes" in e.github.body and "## Operator notes" in e.github.body
    assert "Human release edit." in e.github.body
    assert "Old release must stay out" not in e.github.body
    assert result["promotion"]["notes_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert result["promotion"]["notes_input"]["range"]["head"] == source
    assert value["source_digest"] in e.github.body


async def test_request_refuses_stale_authored_digest(promote_env):
    e = promote_env
    step = await configure(e, notes_kind="file_template", bootstrap=e.source)
    source = commit(e.repo.store, {step["notes"]["path"].format(version="0.2.0"):
                    "---\nsource_digest: stale\n---\nAuthored notes\n"}, base=e.source)
    git(e.repo.store, "push", "origin", source + ":refs/heads/dev")
    e.github.runs[source] = "success"
    assert (await request(e, notes_reviewed=True))["outcome"] == "notes_stale"
    assert e.github.created == 0


async def test_normal_worker_cannot_assemble_cross_task_notes_or_prepare(promote_env):
    e = promote_env
    await configure(e)
    worker = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
                                session_id="worker", project_id="p")
    with principal_context(worker):
        result = await e.handler._cmd_integration_promotion_notes_input({"project_id": "p", "step_id": "release"})
        assert result["outcome"] == "unauthorized"
        result = await e.handler._cmd_promote_prepare({"project_id": "p", "step_id": "release", "bump": "patch"})
        assert result["outcome"] == "unauthorized"


async def test_request_pr_renders_source_summary_and_migration_guidance(promote_env):
    e = promote_env
    step = await configure(e, bootstrap=e.source)
    source = commit(e.repo.store, {"feature.txt": "feature\n", "migrations/versions/feature.py": "migration\n"}, base=e.source)
    head = merge_source(e, e.source, source, "feature")
    await task_row(e, "feature", summary="Adds useful release behavior")
    git(e.repo.store, "push", "origin", head + ":refs/heads/dev")
    e.github.runs[head] = "success"
    result = await request(e)
    assert result["success"], result
    assert "Adds useful release behavior" in e.github.body
    assert "migrations/versions/feature.py" in e.github.body
    value = result["promotion"]["notes_input"]
    assert value["sources"][0]["task"] == "feature"
    assert value["source_digest"] in e.github.body
    assert value["range"]["base"] == step["notes"]["bootstrap_sha"]


async def test_request_uses_step_check_set_on_exact_source(promote_env):
    e = promote_env
    step = await configure(e)
    step["gate"]["checks"] = "manifest:release-only"
    trust = e.trust.model_copy(update={"check_sets": {
        "release-only": ("lint",),
    }})
    e.handler._promotion_manifest = AsyncMock(return_value=trust.model_dump(mode="json", by_alias=True))
    async with e.db._engine.begin() as conn:
        await conn.execute(update(projects).values(promotion_flow=[step]))
    result = await request(e)
    # The finished unit workflow is green, but its missing required lint
    # check makes the selected release set red in the authenticated observer.
    assert e.github.runs[e.source] == "success"
    assert result["outcome"] == "promotion_source_red", result
    assert e.github.created == 0


async def test_notes_exclude_control_tasks_and_keep_bare_merges(promote_env):
    e = promote_env
    step = await configure(e, bootstrap=e.source)
    head = e.source
    for identity, kind in (("promotion", "promotion"), ("backmerge", "backmerge")):
        source = commit(e.repo.store, {identity + ".txt": identity}, base=e.source)
        head = merge_source(e, head, source, identity)
        await task_row(e, identity, kind=kind)
    manual = commit(e.repo.store, {"manual.txt": "manual"}, base=e.source)
    git(e.repo.store, "checkout", "--detach", head)
    git(e.repo.store, "merge", "--no-ff", "-m", "Manual merge without trailer", manual)
    head = git(e.repo.store, "rev-parse", "HEAD")
    value = await notes(e, step, head)
    assert value["sources"] == []
    assert "Manual merge without trailer" in render_release_notes(value)
    assert value["source_digest"] == hashlib.sha256(b"").hexdigest()
