"""Preparation and exact-source notes admission over real Git and PostgreSQL."""

import asyncio
import copy
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.commands.contracts import CONTRACTS
from src.commands.contracts.promote import PromoteRequestArgs
from src.commands.handler import CommandHandler
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import AppConfig
from src.database.tables import (
    archived_tasks,
    events,
    integration_batches,
    projects,
    task_branch_origins,
    task_completion_records,
    task_labels,
    task_metadata,
    tasks,
)
from src.git.github_contracts import GitHubAccessError
from src.integration.promotion_notes import (
    MAX_PR_BODY_BYTES,
    NOTES_TRUNCATION_MARKER,
    NotesRefusal,
    assemble_notes_input,
    draft_notes,
    notes_metadata,
    previous_tag,
    render_release_notes,
    step_pr_body,
)
from src.profiles.capabilities import DENY_ALL
from tests.test_integration_gitops import commit, git
from tests.test_integration_gitops import setup as setup
from tests.test_promote_commands import _supervisor, request
from tests.test_promote_commands import promote_env as promote_env
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
    git(e.repo.store, "push", "origin", e.source + ":refs/heads/main")
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
@pytest.mark.parametrize("oversized", [False, True])
async def test_pr_preserves_authored_notes_and_renders_operator_input(promote_env, kind, oversized):
    e = promote_env
    step = await configure(e, notes_kind=kind, bootstrap=e.source)
    value = await notes(e, step, e.source)
    body = draft_notes(value, kind=kind, version="0.2.0") + "\nHuman release edit.\n"
    if oversized:
        body += "🔧" * MAX_PR_BODY_BYTES + "\n"
    section = body
    if kind == "changelog_heading":
        section += "\n"
        body = "# Changelog\n\n" + section + "## [0.1.0]\nOld release must stay out of this PR.\n"
    source = commit(e.repo.store, {step["notes"]["path"].format(version="0.2.0"): body}, base=e.source)
    git(e.repo.store, "push", "origin", source + ":refs/heads/dev")
    e.github.runs[source] = "success"
    result = await request(e, notes_reviewed=True)
    assert result["success"], result
    assert "## Release notes" in e.github.body and "## Operator notes" in e.github.body
    assert "Human release edit." in e.github.body
    assert "Old release must stay out" not in e.github.body
    assert result["promotion"]["notes_sha256"] == hashlib.sha256(section.encode()).hexdigest()
    assert len(e.github.body.encode()) <= MAX_PR_BODY_BYTES
    assert (NOTES_TRUNCATION_MARKER in e.github.body) is oversized
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
    step = await configure(e, notes_kind="file_template", bootstrap=e.source)
    source = commit(e.repo.store, {"feature.txt": "feature\n", "migrations/versions/feature.py": "migration\n"}, base=e.source)
    head = merge_source(e, e.source, source, "feature")
    await task_row(e, "feature", summary="Adds useful release behavior")
    value = await notes(e, step, head)
    head = commit(e.repo.store, {"notes/0.2.0.md": draft_notes(
        value, kind="file_template", version="0.2.0",
    )}, base=head)
    git(e.repo.store, "push", "origin", head + ":refs/heads/dev")
    e.github.runs[head] = "success"
    result = await request(e, notes_reviewed=True)
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


@pytest.mark.parametrize("versioning", [
    {"kind": "none"}, {"kind": "custom", "tag_format": "build-{sha12}"},
])
async def test_unversioned_range_uses_target_tip_on_long_history(promote_env, monkeypatch, versioning):
    e = promote_env
    step = await configure(e, bootstrap=e.base)
    step["versioning"] = versioning
    monkeypatch.setattr("src.integration.promotion_notes.MAX_NOTES_COMMITS", 8)
    # History before the target tip must not consume the bounded range budget.
    tree = git(e.repo.store, "rev-parse", e.source + "^{tree}")
    target = e.source
    for index in range(32):
        target = git(e.repo.store, "commit-tree", tree, "-p", target, "-m", f"Old history {index}")
    git(e.repo.store, "push", "origin", target + ":refs/heads/main")
    head = commit(e.repo.store, {"recent.txt": "new change"}, base=target)
    assert await previous_tag(e.ops, e.repo, step) is None
    value = await notes(e, step, head, {"sha": e.base, "tag": "old-build"})
    assert value["range"] == {"base": target, "head": head, "previous_tag": None}
    assert [entry["sha"] for entry in value["bare_commits"]] == [head]
    body = step_pr_body(step, head, "request", value)
    assert "Old history" not in body and len(body.encode()) <= MAX_PR_BODY_BYTES
    # The next promotion starts where the target has advanced, without a tag.
    git(e.repo.store, "push", "origin", head + ":refs/heads/main")
    next_head = commit(e.repo.store, {"next.txt": "another change"}, base=head)
    assert (await notes(e, step, next_head))["range"]["base"] == head


@pytest.mark.parametrize("bootstrap", [False, True])
async def test_first_versioned_range_uses_bootstrap_or_target_tip(promote_env, bootstrap):
    e = promote_env
    step = await configure(e, notes_kind="file_template", bootstrap=e.base if bootstrap else None)
    git(e.repo.store, "push", "origin", e.source + ":refs/heads/main")
    head = commit(e.repo.store, {"recent.txt": "new change"}, base=e.source)
    assert await previous_tag(e.ops, e.repo, step) is None
    assert (await notes(e, step, head))["range"]["base"] == (e.base if bootstrap else e.source)


async def test_notes_range_limit_refuses_before_reading_commit_messages(promote_env, monkeypatch):
    e = promote_env
    step = await configure(e, notes_kind="file_template")
    monkeypatch.setattr("src.integration.promotion_notes.MAX_NOTES_COMMITS", 2)
    head = e.source
    tree = git(e.repo.store, "rev-parse", head + "^{tree}")
    for index in range(3):
        head = git(e.repo.store, "commit-tree", tree, "-p", head, "-m", f"New history {index}")
    e.ops.run = AsyncMock(wraps=e.ops.run)
    with pytest.raises(NotesRefusal) as caught:
        await notes(e, step, head)
    assert caught.value.outcome == "notes_range_too_large"
    assert any(call.args[1:3] == ("rev-list", "--topo-order")
               and "--max-count=3" in call.args for call in e.ops.run.await_args_list)
    assert not any(call.args[1] == "log" for call in e.ops.run.await_args_list)


@pytest.mark.parametrize("command", ["request", "prepare", "notes_input"])
async def test_disabled_notes_skip_assembly_and_unknown_task_trailers(promote_env, monkeypatch, command):
    e = promote_env
    await configure(e)
    source = commit(e.repo.store, {"unknown.txt": "unknown source"}, base=e.source)
    head = merge_source(e, e.source, source, "unknown-task")
    git(e.repo.store, "push", "origin", head + ":refs/heads/dev")
    e.github.runs[head] = "success"
    assembler = AsyncMock(side_effect=AssertionError("disabled notes were assembled"))
    monkeypatch.setattr("src.commands.promote_commands.assemble_notes_input", assembler)
    if command == "request":
        result = await request(e)
        assert result["promotion"]["notes_input"] is None
        assert result["promotion"]["notes_sha256"] is None
        assert "## Release notes" not in e.github.body
        assert len(e.github.body.encode()) <= MAX_PR_BODY_BYTES
    elif command == "prepare":
        result = await e.handler._cmd_promote_prepare({
            "project_id": "p", "step_id": "release", "bump": "patch",
        })
        assert result["notes_input"] is None and result["draft"] is None
        task = await e.db.get_task(result["task_id"])
        assert "draft the configured notes" not in task.description
    else:
        result = await e.handler._cmd_integration_promotion_notes_input({
            "project_id": "p", "step_id": "release",
        })
        assert result["notes_input"] is None
    assert result["success"], result
    assembler.assert_not_awaited()


@pytest.mark.parametrize("versioning", [
    {"kind": "none"}, {"kind": "custom", "tag_format": "build-{sha12}"},
])
async def test_request_without_version_skips_all_release_history(promote_env, monkeypatch, versioning):
    e = promote_env
    step = await configure(e)
    step["versioning"] = versioning
    async with e.db._engine.begin() as conn:
        await conn.execute(update(projects).values(promotion_flow=[step]))
    e.ops.run = AsyncMock(wraps=e.ops.run)
    monkeypatch.setattr("src.commands.promote_commands.previous_tag",
                        AsyncMock(side_effect=AssertionError("unversioned tag inventory")))
    result = await request(e)
    assert result["success"], result
    assert result["promotion"]["version"] is None
    assert result["promotion"]["notes_input"] is None
    assert not any(call.args[1] in {"log", "rev-list"} for call in e.ops.run.await_args_list)


def pr_notes_input():
    return {"range": {"base": "a" * 40, "head": "b" * 40},
            "source_digest": "c" * 64, "sources": [], "bare_commits": [],
            "migration_files": ["migrations/versions/add.py"]}


@pytest.mark.parametrize("authored", [False, True])
def test_pr_body_caps_notes_and_keeps_required_evidence(authored):
    value = pr_notes_input()
    text = "🔧" * MAX_PR_BODY_BYTES
    value["bare_commits"] = [{"subject": text}]
    body = step_pr_body({"source": "dev", "target": "main"}, "b" * 40, "request",
                        value, text if authored else None)
    assert len(body.encode()) <= MAX_PR_BODY_BYTES
    assert NOTES_TRUNCATION_MARKER in body
    assert "## Operator notes" in body and value["source_digest"] in body
    assert "migrations/versions/add.py" in body
    assert body.endswith("AQ-Promotion-Request: request\n")


def test_pr_body_exact_size_and_untruncated_authored_text():
    step, value = {"source": "dev", "target": "main"}, pr_notes_input()
    required = step_pr_body(step, "b" * 40, "request", value, "")
    release = "x" * (MAX_PR_BODY_BYTES - len(required.encode()))
    body = step_pr_body(step, "b" * 40, "request", value, release)
    assert len(body.encode()) == MAX_PR_BODY_BYTES and release in body
    assert NOTES_TRUNCATION_MARKER not in body


@pytest.mark.parametrize("notes_enabled", [False, True])
def test_pr_body_refuses_when_required_evidence_cannot_fit(notes_enabled):
    value = pr_notes_input() if notes_enabled else None
    if value:
        value["migration_files"] = ["x" * MAX_PR_BODY_BYTES]
    identity = "request" if notes_enabled else "x" * MAX_PR_BODY_BYTES
    with pytest.raises(NotesRefusal) as caught:
        step_pr_body({"source": "dev", "target": "main"}, "b" * 40, identity, value)
    assert caught.value.outcome == "promotion_body_too_large"


async def test_required_pr_body_limit_refuses_before_any_ref_or_intent(promote_env, monkeypatch):
    e = promote_env
    monkeypatch.setattr("src.integration.promotion_notes.MAX_PR_BODY_BYTES", 100)
    before = git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/")
    result = await request(e)
    assert result["outcome"] == "promotion_body_too_large", result
    assert e.github.created == 0
    assert git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/") == before
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.id)) is None
        assert await conn.scalar(select(integration_batches.c.id)) is None


@pytest.mark.parametrize("outcome", ["notes_range_too_large", "promotion_body_too_large"])
async def test_notes_limits_survive_registered_request_adapter(promote_env, monkeypatch, outcome):
    e = promote_env
    if outcome == "notes_range_too_large":
        await configure(e, notes_kind="file_template")
        source = commit(e.repo.store, {"notes/0.2.0.md": "Authored notes\n"}, base=e.source)
        git(e.repo.store, "push", "origin", source + ":refs/heads/dev")
        e.github.runs[source] = "success"
        monkeypatch.setattr("src.integration.promotion_notes.MAX_NOTES_COMMITS", 1)
    else:
        monkeypatch.setattr("src.integration.promotion_notes.MAX_PR_BODY_BYTES", 100)
    # Exercise the registered boundary and real dispatch, rather than calling
    # the mixin directly or synthesizing a handler's refusal response.
    e.handler.orchestrator.db = e.db
    handler = CommandHandler(e.handler.orchestrator, AppConfig())
    handler._promotion_manifest = e.handler._promotion_manifest
    monkeypatch.setattr("src.commands.contracts.builtin._handler", lambda: handler)
    registration = CONTRACTS.require("promote_request")
    before = git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/")
    result = await registration.invoke(PromoteRequestArgs(
        project_id="p", step_id="release", notes_reviewed=True,
    ), None)
    assert result.outcome == outcome, result
    spec = next(spec for spec in registration.contract.execution.outcomes if spec.name == outcome)
    assert spec.classification == "failure"
    assert e.github.created == 0
    assert git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/") == before
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.id)) is None
        assert await conn.scalar(select(integration_batches.c.id)) is None


@pytest.mark.parametrize("command", ["request", "prepare", "prepare_older_version", "notes_input"])
@pytest.mark.parametrize("target_moved", [False, True])
async def test_outstanding_hotfix_refuses_before_notes_range(promote_env, command, target_moved):
    e = promote_env
    await configure(e, notes_kind="changelog_heading")
    hotfix = commit(e.repo.store, {"hotfix.txt": "hotfix"}, base=e.base)
    git(e.repo.store, "tag", "v0.1.9", hotfix)
    git(e.repo.store, "push", "origin", "refs/tags/v0.1.9")
    if target_moved:
        git(e.repo.store, "push", "origin", hotfix + ":refs/heads/main")
    source = commit(e.repo.store, {"CHANGELOG.md": "## [0.2.0]\nAuthored notes\n"}, base=e.source)
    git(e.repo.store, "push", "origin", source + ":refs/heads/dev")
    e.github.runs[source] = "success"
    await task_row(e, "pending-backmerge", kind="backmerge")
    async with e.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "pending-backmerge").values(status="DEFINED"))
        await conn.execute(insert(task_metadata).values(
            task_id="pending-backmerge", key="backmerge",
            # Include the daemon's originating branch identity while preserving
            # the DEFINED outstanding-debt regression from the E2 review.
            value=json.dumps({"source_sha": hotfix, "target_ref": "refs/heads/main",
                              "origin_ref": "refs/heads/main"}),
        ))
    args = {"project_id": "p", "step_id": "release"}
    if command in {"prepare", "prepare_older_version"}:
        version_choice = {"version": "0.1.8"} if command == "prepare_older_version" else {"bump": "patch"}
        result = await e.handler._cmd_promote_prepare({**args, **version_choice})
    elif command == "request":
        result = await request(e, notes_reviewed=True)
    else:
        result = await e.handler._cmd_integration_promotion_notes_input(args)
    assert result["outcome"] == "backmerge_pending", result
    assert e.github.created == 0
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.id)) is None


@pytest.mark.parametrize("condition,outcome", [
    ("rate_limit", "rate_limited"), ("malformed_producer", "promotion_source_untrusted"),
    ("wrong_head", "promotion_source_untrusted"), ("foreign_producer", "promotion_source_red"),
    ("pr_only", "promotion_source_pending"),
])
async def test_request_source_trust_and_rate_limit_refuse_before_writes(promote_env, condition, outcome):
    e = promote_env
    listing = e.github.paged_items
    if condition == "pr_only":
        e.github.runs.clear()
        e.github.pr_runs[e.source] = "success"

    async def observe(path, *, key):
        if condition == "rate_limit":
            raise GitHubAccessError("rate_limited", "source CI rate limited", retry_at=1234.5)
        rows = await listing(path, key=key)
        if key == "check_runs":
            for row in rows:
                if condition == "malformed_producer":
                    row["app"] = None
                elif condition == "foreign_producer":
                    row["app"] = {"id": 999}
                elif condition == "wrong_head":
                    row["head_sha"] = e.base
        return rows

    e.github.paged_items = observe
    before = git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/")
    result = await request(e)
    assert result["outcome"] == outcome, result
    if condition == "rate_limit":
        assert result["retry_at"] == 1234.5
    assert e.github.created == 0
    assert git(e.ops.git.remote_path, "for-each-ref", "refs/heads/aq/promote/") == before
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(tasks.c.id)) is None
        assert await conn.scalar(select(integration_batches.c.id)) is None


@pytest.mark.parametrize("command", ["prepare", "request"])
@pytest.mark.parametrize("project_id", ["p", "other-project"])
async def test_prepare_and_request_enforce_supervisor_project_scope(promote_env, command, project_id):
    e = promote_env
    await configure(e)
    with principal_context(_supervisor(project_id)):
        if command == "prepare":
            result = await e.handler._cmd_promote_prepare({
                "project_id": "p", "step_id": "release", "bump": "patch",
            })
        else:
            result = await request(e)
    assert result["success"] is (project_id == "p"), result
    assert result["outcome"] == ("unauthorized" if project_id != "p" else
                                  "prepared" if command == "prepare" else "requested")
    if project_id != "p":
        assert e.github.created == 0
        async with e.db._engine.connect() as conn:
            assert await conn.scalar(select(tasks.c.id)) is None
            assert await conn.scalar(select(integration_batches.c.id)) is None
