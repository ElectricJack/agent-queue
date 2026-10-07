"""Real Git/PostgreSQL invariants for the shared retained-clone primitives."""

import asyncio
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, update

from src.database import Database
from src.database.tables import (
    integration_branch_owners,
    integration_subjects,
    playbook_artifacts,
    projects,
)
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitError, GitManager, is_valid_git_oid
from src.integration.cleanup import SubjectCleanup, SubjectCleanupItem
from src.integration.development import DevelopmentBusy, publisher_exclusion
from src.integration.gitops import (
    GitOperations,
    RetainedRepository,
    SubjectGitAuthority,
)
from src.integration.models import BranchKey
from src.integration.ownership import BranchOwnership
from src.integration.source_ancestry import effective_source_base
from src.integration.subjects import (
    AncestryArgs,
    AncestryQuery,
    CleanupArgs,
    MaterializeRefArgs,
    MemberRef,
    MergeMembersArgs,
    PolicyArtifactPin,
    PreserveArgs,
    Primitive,
    PrimitivePorts,
    PublishArgs,
    Subject,
    SubjectEngine,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    WriterLease,
    WriterStatus,
)
from src.models import Project, RepoConfig, RepoSourceType
from tests.db_fixtures import lease_dsn

ARTIFACT = "sha256:" + "1" * 64
NOW = 1700000000.0


def git(path, *args):
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def commit(path, files, *, base=None):
    if base:
        git(path, "checkout", "--detach", base)
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(path, "add", "-A")
    git(path, "commit", "-m", "source change")
    return git(path, "rev-parse", "HEAD")


def inherited_source(path, base, scenario):
    """A stale origin, a real parent merge, and subsequent target movement."""
    recorded = base
    if scenario == "generated":
        recorded = commit(path, {
            ".gitattributes": "generated.txt merge=aq-generated\n",
            "generated.txt": "base\n",
        }, base=base)
    inherited = commit(path, {"base.txt": "inherited parent\n"}, base=recorded)
    own = commit(path, {"own.txt": "own change\n"}, base=recorded)
    git(path, "merge", "--no-ff", "-m", "inherit newer parent", inherited)
    head = git(path, "rev-parse", "HEAD")
    if scenario == "conflict":
        head = commit(path, {"base.txt": "source change\n"})
    elif scenario == "revert":
        head = commit(path, {"base.txt": "base\n"})
    elif scenario == "generated":
        head = commit(path, {"generated.txt": "source\n"})
    current = commit(path, {"target.txt": "target moved on\n"} if scenario == "revert" else {
        "base.txt": "target moved on\n",
        **({"generated.txt": "target\n"} if scenario == "generated" else {}),
    }, base=inherited)
    assert git(path, "merge-base", current, head) == inherited
    assert git(path, "merge-base", "--is-ancestor", own, head) == ""
    return recorded, inherited, current, head


def reserved_restoration_source(path, base):
    """The recorded delta hides a restoration of target-deleted bookkeeping."""
    recorded = commit(path, {".aq/claim.json": "{}\n"}, base=base)
    git(path, "rm", ".aq/claim.json")
    git(path, "commit", "-m", "target deletes bookkeeping")
    inherited = git(path, "rev-parse", "HEAD")
    commit(path, {"own.txt": "own change\n"}, base=recorded)
    git(path, "merge", "--no-ff", "-m", "inherit target deletion", inherited)
    head = commit(path, {".aq/claim.json": "{}\n"})
    current = commit(path, {"target.txt": "target change\n"}, base=inherited)
    assert git(path, "diff", "--name-only", recorded, head) == "own.txt"
    assert ".aq/claim.json" in git(path, "diff", "--name-only", inherited, head).splitlines()
    return recorded, inherited, current, head


def stacked_member_sources(path, base):
    """Child P's own delta excludes Q1; Q must subsequently land Q1 and Q2."""
    intermediate = commit(path, {"q1.txt": "Q1\n"}, base=base)
    child = commit(path, {"p.txt": "P\n"})
    source = commit(path, {"q2.txt": "Q2\n"}, base=intermediate)
    return intermediate, child, source


class LocalGit(GitManager):
    """Only the credential boundary is substituted; transport is real Git."""

    def __init__(self, remote):
        super().__init__()
        self.remote_path = remote
        self.pushes = 0
        self.deletes = 0
        self.ambiguous = False
        self.unreadable = False
        self.fail_push = False
        self.read_hook = None
        self.push_hook = None
        self.delete_hook = None

    async def aremote_branch_head(self, *, repository, branch):
        if self.read_hook:
            await self.read_hook()
        if self.unreadable:
            raise GitError("remote temporarily unreadable")
        result = await self.arun_git_result(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=str(self.remote_path)
        )
        if result.returncode == 1:
            return None
        if result.returncode:
            raise GitError(result.stderr)
        return await self.arev_parse(str(self.remote_path), f"refs/heads/{branch}")

    async def apush_repository_oid(
        self, store, *, repository, tip_oid, branch, expected_old_oid, authority_deadline=None
    ):
        self.pushes += 1
        if self.push_hook:
            await self.push_hook()
        if self.fail_push:
            raise GitError("transport refused")
        result = await self.arun_git_result(
            [
                "push",
                f"--force-with-lease=refs/heads/{branch}:{expected_old_oid}",
                str(self.remote_path),
                f"{tip_oid}:refs/heads/{branch}",
            ],
            cwd=store,
        )
        if result.returncode:
            raise GitError(result.stderr)
        if self.ambiguous:
            raise GitError("lost successful push response")
        return tip_oid

    async def adelete_repository_ref(
        self, store, *, repository, branch, expected_old_oid, authority_deadline=None
    ):
        self.deletes += 1
        if self.delete_hook:
            await self.delete_hook()
        result = await self.arun_git_result(
            [
                "push",
                f"--force-with-lease=refs/heads/{branch}:{expected_old_oid}",
                str(self.remote_path),
                f":refs/heads/{branch}",
            ],
            cwd=store,
        )
        if result.returncode:
            raise GitError(result.stderr)
        if self.ambiguous:
            raise GitError("lost successful delete response")


@pytest.fixture
async def setup(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    store = tmp_path / "retained"
    git(tmp_path, "clone", str(remote), str(store))
    git(store, "config", "user.name", "Tester")
    git(store, "config", "user.email", "tester@example.test")
    base = commit(store, {"base.txt": "base\n"})
    git(store, "push", "origin", "HEAD:main")
    head = commit(store, {"new.txt": "new\n"})
    db = Database(lease_dsn("gitops"))
    await db.initialize()
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(playbook_artifacts).values(
                artifact_sha256=ARTIFACT,
                playbook_id="test",
                source_digest="sha256:" + "c" * 64,
                contract_fingerprint="sha256:" + "d" * 64,
                compiler_build="test",
                path="/artifacts/test.json",
                created_at=1.0,
            )
        )
    fence = await BranchOwnership(db).acquire(
        BranchKey(repository_id="r", branch="refs/heads/main"), "writer", "collector"
    )
    subject = Subject(
        id="s",
        project_id="p",
        repository_id="r",
        kind=SubjectKind.ROOT_BATCH,
        subject_key="test",
        engine=SubjectEngine.RECONCILER,
        phase=SubjectPhase.BUILDING,
        policy=PolicyArtifactPin(playbook_id="test", artifact_sha256=ARTIFACT),
        target_ref="refs/heads/main",
        head_sha=head,
        base_sha=base,
        writer=WriterLease(
            status=WriterStatus.WORKING, task_id="writer", fence_token=fence.token, last_push_at=NOW
        ),
        schedule=SubjectSchedule.progress(now=NOW, max_wait_seconds=3600),
        created_at=NOW,
        updated_at=NOW,
    )
    await db.ensure_integration_subject(subject.to_row())
    repo = RetainedRepository("r", store, GitHubRepositoryBinding(123, "test/repo"), "main")

    async def resolver(subject):
        # GitOperations resolves a subject for the subject runtimes and a bare
        # repository id for batch construction; both must name this clone.
        assert getattr(subject, "repository_id", subject) == "r"
        return repo

    green = {head}

    async def trusted_green(subject, sha):
        return sha in green and sha == subject.head_sha

    transport = LocalGit(remote)
    ops = GitOperations(
        db,
        git=transport,
        repository=resolver,
        authority=SubjectGitAuthority(db, trusted_green=trusted_green),
    )
    yield db, ops, subject, fence, repo, base, head, green
    await db.close()


def merge_args(subject, base, *heads):
    return MergeMembersArgs(
        target_ref=subject.target_ref,
        base_sha=base,
        members=tuple(
            MemberRef(task_id=f"t{i}", head_sha=h, base_sha=base) for i, h in enumerate(heads)
        ),
    )


async def journal_rows(db):
    return await db.list_integration_subject_journal("s", limit=100)


async def test_merge_replay_preserves_exact_source_ancestry_and_pins(setup):
    db, ops, s, fence, repo, base, head, green = setup
    other = commit(repo.store, {"other.txt": "other\n"}, base=base)
    args = merge_args(s, base, head, other)
    first = await ops.merge_members(s, args)
    assert first.outcome == "merged", first
    final = first.detail["head"]
    assert git(repo.store, "merge-base", "--is-ancestor", head, final) == ""
    assert git(repo.store, "merge-base", "--is-ancestor", other, final) == ""
    assert git(repo.store, "merge-base", "--is-ancestor", base, final) == ""
    assert git(repo.store, "show", f"{final}:new.txt") == "new"
    assert git(repo.store, "show", f"{final}:other.txt") == "other"
    rows = await journal_rows(db)
    assert rows == []
    assert (
        len(git(repo.store, "for-each-ref", "--format=%(refname)", "refs/aq/batch-objects").split()) == 2
    )
    assert (await ops.merge_members(s, args)) == first
    assert len(await journal_rows(db)) == len(rows)
    # Construction never changes the remote target.
    assert git(ops.git.remote_path, "rev-parse", "main") == base


async def test_each_merged_member_records_one_exact_source_trailer(setup):
    """One AQ-Source per member merge, on that member's whole head only."""
    from src.integration.source_trailer import SourceIdentity, parse_source_trailers

    _db, ops, s, _fence, repo, base, head, _green = setup
    other = commit(repo.store, {"other.txt": "other\n"}, base=base)
    result = await ops.merge_members(s, merge_args(s, base, head, other))
    assert result.outcome == "merged", result
    merges = git(
        repo.store, "rev-list", "--reverse", "--min-parents=2", f"{base}..{result.detail['head']}"
    ).split()
    assert len(merges) == 2
    for sha, head_sha in zip(merges, (head, other), strict=True):
        message = git(repo.store, "show", "-s", "--format=%B", sha)
        task_id = "t0" if head_sha == head else "t1"
        assert parse_source_trailers(message) == {SourceIdentity(task_id, head_sha)}
        assert [line for line in message.splitlines() if line.startswith("AQ-Source:")] == [
            f"AQ-Source: {task_id}@{head_sha}"
        ]
        assert f"Integrate {task_id} ({head_sha})" in message
    # Neither merge names the other's member, and the subject line each
    # recorded before this change is unchanged.
    recorded = set().union(*(
        parse_source_trailers(git(repo.store, "show", "-s", "--format=%B", sha)) for sha in merges
    ))
    assert recorded == {SourceIdentity("t0", head), SourceIdentity("t1", other)}


async def test_real_conflict_records_partial_head_and_paths(setup):
    db, ops, s, _, repo, base, *_ = setup
    a = commit(repo.store, {"base.txt": "ours\n"}, base=base)
    b = commit(repo.store, {"base.txt": "theirs\n"}, base=base)
    result = await ops.merge_members(s, merge_args(s, base, a, b))
    assert result.outcome == "conflict", result
    assert result.detail["member"] == "t1"
    assert result.detail["files"] == ["base.txt"]
    assert result.detail["members"][0]["source"] == a
    assert (await ops.merge_members(s, merge_args(s, base, a, b))) == result
    assert await journal_rows(db) == []


@pytest.mark.parametrize("scenario", ("clean", "conflict", "generated", "revert"))
async def test_merge_uses_inherited_target_base_and_records_evidence(setup, scenario):
    _, ops, _, _, repo, base, *_ = setup
    recorded, inherited, current, head = inherited_source(repo.store, base, scenario)
    regen = repo.store.parent / "regenerate.py"
    regen.write_text("from pathlib import Path\nPath('generated.txt').write_text('rebuilt')\n")
    repo = replace(repo, regenerate=f"{sys.executable} {regen}")
    members = (MemberRef(task_id="inherited", head_sha=head, base_sha=recorded),)

    result = await ops.merge_sources(repo, current, members, created_at=NOW)
    assert result["outcome"] == ("conflict" if scenario == "conflict" else "merged"), result
    evidence = result if scenario == "conflict" else result["members"][0]
    assert evidence["source_base_sha"] == recorded
    assert evidence["effective_base_sha"] == inherited
    assert evidence["target_head_sha"] == current
    if scenario == "conflict":
        assert result["files"] == ["base.txt"]
        assert result["head"] == current
    else:
        merged = result["head"]
        expected = "base" if scenario == "revert" else "target moved on"
        assert git(repo.store, "show", f"{merged}:base.txt") == expected
        assert git(repo.store, "show", f"{merged}:own.txt") == "own change"
        assert git(repo.store, "show", "-s", "--format=%P", merged).split() == [current, head]
        message = git(repo.store, "show", "-s", "--format=%B", merged)
        assert f"Source-base: {recorded}" in message
        assert f"Effective-merge-base: {inherited}" in message
        if scenario == "generated":
            assert git(repo.store, "show", f"{merged}:generated.txt") == "rebuilt"
        elif scenario == "clean":
            forced = await ops.git.arun_git_result([
                "merge-tree", "--write-tree", f"--merge-base={recorded}", current, head,
            ], cwd=str(repo.store))
            assert forced.returncode == 1  # The old construction really conflicts.
    assert await ops.merge_sources(repo, current, members, created_at=NOW) == result


async def test_source_base_outside_target_history_keeps_only_recorded_delta(setup):
    _, ops, _, _, repo, base, *_ = setup
    recorded = commit(repo.store, {"unreviewed.txt": "exclude me\n"}, base=base)
    source = commit(repo.store, {"own.txt": "own change\n"})
    current = commit(repo.store, {"target.txt": "target change\n"}, base=base)
    member = MemberRef(task_id="divergent", head_sha=source, base_sha=recorded)
    result = await ops.merge_sources(repo, current, (member,), created_at=NOW)
    assert result["outcome"] == "merged", result
    assert result["members"][0]["effective_base_sha"] == recorded
    assert git(repo.store, "ls-tree", "--name-only", result["head"]).splitlines() == [
        "base.txt", "own.txt", "target.txt",
    ]


async def test_effective_base_does_not_hide_inherited_reserved_paths(setup):
    _, ops, _, _, repo, base, *_ = setup
    inherited = commit(repo.store, {".aq/claim.json": "{}\n"}, base=base)
    head = commit(repo.store, {"own.txt": "own change\n"})
    member = MemberRef(task_id="reserved", head_sha=head, base_sha=base)
    result = await ops.merge_sources(repo, inherited, (member,), created_at=NOW)
    assert result["outcome"] == "source_moved", result
    assert "reserved AQ bookkeeping paths" in result["reason"]
    assert result["head"] == inherited


async def test_effective_base_refuses_restored_reserved_path(setup):
    _, ops, _, _, repo, base, *_ = setup
    recorded, inherited, current, head = reserved_restoration_source(repo.store, base)
    member = MemberRef(task_id="reserved-restoration", head_sha=head, base_sha=recorded)
    result = await ops.merge_sources(repo, current, (member,), created_at=NOW)
    assert result["outcome"] == "source_moved", result
    assert "reserved AQ bookkeeping paths" in result["reason"]
    assert result["head"] == current
    assert result["source_base_sha"] == recorded
    assert result["effective_base_sha"] == inherited
    assert result["members"] == []


async def test_effective_base_keeps_source_changes_after_stacked_child_lands_first(setup):
    _, ops, _, _, repo, base, *_ = setup
    intermediate, child, source = stacked_member_sources(repo.store, base)
    members = (
        MemberRef(task_id="P", head_sha=child, base_sha=intermediate),
        MemberRef(task_id="Q", head_sha=source, base_sha=base),
    )
    result = await ops.merge_sources(repo, base, members, created_at=NOW)
    assert result["outcome"] == "merged", result
    partial = result["members"][0]["head"]
    assert git(repo.store, "merge-base", partial, source) == intermediate
    assert intermediate not in git(repo.store, "rev-list", "--first-parent", partial).split()
    assert git(repo.store, "ls-tree", "--name-only", partial).splitlines() == ["base.txt", "p.txt"]
    assert result["members"][1]["effective_base_sha"] == base
    assert git(repo.store, "show", f"{result['head']}:q1.txt") == "Q1"
    assert git(repo.store, "show", f"{result['head']}:q2.txt") == "Q2"
    assert git(repo.store, "show", f"{result['head']}:p.txt") == "P"


async def test_effective_base_keeps_real_migration_collisions(setup):
    _, ops, _, _, repo, base, *_ = setup
    inherited = commit(repo.store, {
        "migrations/versions/parent.py": "revision = 'parent'\ndown_revision = None\n",
    }, base=base)
    own = commit(repo.store, {"own.txt": "own change\n"}, base=base)
    git(repo.store, "merge", "--no-ff", "-m", "inherit parent migration", inherited)
    head = commit(repo.store, {
        "migrations/versions/source.py": "revision = 'same'\ndown_revision = 'parent'\n",
    })
    current = commit(repo.store, {
        "migrations/versions/target.py": "revision = 'same'\ndown_revision = 'parent'\n",
    }, base=inherited)
    member = MemberRef(task_id="migrations", head_sha=head, base_sha=base)
    result = await ops.merge_sources(repo, current, (member,), created_at=NOW)
    assert result["outcome"] == "conflict", result
    assert result["reason"] == "alembic_head_collision"
    assert result["effective_base_sha"] == inherited
    assert result["files"] == ["migrations/versions/source.py", "migrations/versions/target.py"]
    assert git(repo.store, "merge-base", "--is-ancestor", own, head) == ""


@pytest.mark.parametrize("probe", ("merge_base", "ancestry", "first_parent"))
async def test_effective_base_probe_error_never_becomes_an_ancestry_proof(probe):
    ok = SimpleNamespace(returncode=0, stdout="b" * 40, stderr="")
    failed = SimpleNamespace(returncode=128, stdout="", stderr="objects unavailable")
    manager = SimpleNamespace(arun_git_result=AsyncMock(
        side_effect={
            "merge_base": [failed], "ancestry": [ok, failed],
            "first_parent": [ok, ok, ok, ok, failed],
        }[probe],
    ))
    with pytest.raises(GitError, match="objects unavailable"):
        await effective_source_base(manager, "store", "a" * 40, "c" * 40, "d" * 40)


@pytest.mark.parametrize("invalid", ["missing", "unrelated_base"])
async def test_source_identity_movement_refuses_without_merging(setup, invalid):
    _, ops, s, _, repo, base, head, *_ = setup
    args = merge_args(s, base, head)
    if invalid == "missing":
        member = MemberRef(task_id="t", head_sha="f" * 40, base_sha=base)
    else:
        member = MemberRef(task_id="t", head_sha=base, base_sha=head)
    args = args.model_copy(update={"members": (member,)})
    result = await ops.merge_members(s, args)
    assert result.outcome == "source_moved"
    assert result.detail["head"] == base


async def test_live_source_ref_may_advance_without_changing_frozen_sha(setup):
    _, ops, s, _, repo, base, head, *_ = setup
    git(repo.store, "push", "origin", f"{head}:refs/heads/aq/source")
    later = commit(repo.store, {"later.txt": "later\n"})
    git(repo.store, "push", "origin", f"{later}:refs/heads/aq/source")
    result = await ops.merge_members(s, merge_args(s, base, head))
    assert result.outcome == "merged"
    assert git(repo.store, "ls-tree", "--name-only", result.detail["head"]).split() == [
        "base.txt",
        "new.txt",
    ]


@pytest.mark.parametrize("during", [False, True])
async def test_target_base_movement_before_or_during_construction(setup, during):
    _, ops, s, _, repo, base, head, *_ = setup
    calls = 0

    async def move():
        nonlocal calls
        calls += 1
        if calls == (2 if during else 1):
            git(repo.store, "push", "origin", f"{head}:main")

    ops.git.read_hook = move
    result = await ops.merge_members(s, merge_args(s, base, head))
    assert result.outcome == "base_moved"
    assert result.detail["observed_sha"] == head


async def test_migration_collision_is_a_member_conflict(setup):
    _, ops, s, _, repo, base, *_ = setup
    a = commit(
        repo.store,
        {"migrations/versions/a.py": "revision = 'same'\ndown_revision = None\n"},
        base=base,
    )
    b = commit(
        repo.store,
        {"migrations/versions/b.py": "revision = 'same'\ndown_revision = None\n"},
        base=base,
    )
    result = await ops.merge_members(s, merge_args(s, base, a, b))
    assert result.outcome == "conflict", result
    assert result.detail["reason"] == "alembic_head_collision"
    assert result.detail["files"] == ["migrations/versions/a.py", "migrations/versions/b.py"]


async def test_generated_conflict_rebuilds_merged_sources_and_rejects_stray_writes(setup):
    db, ops, s, _, repo, base, *_ = setup
    common = commit(
        repo.store,
        {".gitattributes": "generated.txt merge=aq-generated\n", "generated.txt": "base\n"},
        base=base,
    )
    git(repo.store, "push", "origin", f"{common}:main")
    a = commit(repo.store, {"generated.txt": "ours\n", "a.txt": "a\n"}, base=common)
    b = commit(repo.store, {"generated.txt": "theirs\n", "b.txt": "b\n"}, base=common)
    regen = repo.store.parent / "regenerate.py"
    regen.write_text("from pathlib import Path\nPath('generated.txt').write_text('rebuilt')\n")
    revised = replace(repo, regenerate=f"{sys.executable} {regen}")

    async def resolver(_):
        return revised

    ops.repository = resolver
    result = await ops.merge_members(s, merge_args(s, common, a, b))
    assert result.outcome == "merged", result
    assert result.detail["members"][-1]["regenerated"]
    assert git(repo.store, "show", f"{result.detail['head']}:generated.txt") == "rebuilt"
    assert git(repo.store, "show", f"{result.detail['head']}:a.txt") == "a"
    assert git(repo.store, "show", f"{result.detail['head']}:b.txt") == "b"
    regen.write_text("from pathlib import Path\nPath('base.txt').write_text('stray')\n")
    # New generation makes the changed regeneration rule a fresh request.
    async with db._engine.begin() as conn:
        await db.update_integration_subject_on(
            conn,
            subject_id=s.id,
            expected_version=0,
            values={"generation": 1},
            now=NOW,
        )
    s = Subject.from_row(await db.get_integration_subject(s.id))
    result = await ops.merge_members(s, merge_args(s, common, a, b))
    assert result.outcome == "conflict", result
    assert "non-generated" in result.detail["reason"]


async def test_unconfigured_regenerator_is_a_blocker_not_a_member_conflict(setup):
    """A repository with no regenerate command is a project configuration gap.

    The member's content is fine, so the merge must not park it: the batch
    merge answers ``no_regenerator`` and the closed subject primitive answers
    ``unknown``, which no decision table routes to repair.
    """
    db, ops, s, _, repo, base, *_ = setup
    common = commit(
        repo.store,
        {".gitattributes": "generated.txt merge=aq-generated\n", "generated.txt": "base\n"},
        base=base,
    )
    git(repo.store, "push", "origin", f"{common}:main")
    a = commit(repo.store, {"generated.txt": "ours\n", "a.txt": "a\n"}, base=common)
    b = commit(repo.store, {"generated.txt": "theirs\n", "b.txt": "b\n"}, base=common)
    unconfigured = replace(repo, regenerate=None)

    async def resolver(_):
        return unconfigured

    ops.repository = resolver
    merged = await ops.merge_sources(unconfigured, common,
                                      merge_args(s, common, a, b).members,
                                      created_at=NOW)
    assert merged["outcome"] == "no_regenerator", merged
    # The first member merged on its own; the second is the one whose generated
    # overlap cannot be rebuilt, and its own source is named.
    assert [member["member"] for member in merged["members"]] == ["t0"]
    assert merged["member"] == "t1"
    assert is_valid_git_oid(merged["head"])

    result = await ops.merge_members(s, merge_args(s, common, a, b))
    assert result.is_unknown and "regenerate" in result.reason
    # Neither answer changed the remote target, and no journal row was written.
    assert git(repo.store, "rev-parse", "origin/main") == common
    assert await journal_rows(db) == []


async def test_declined_regeneration_keeps_the_generated_conflict(setup):
    """``regenerate_generated: false`` is a policy choice, not a gap.

    The generated overlap is then an ordinary conflict with its own evidence,
    because the policy — not the configuration — refused the rebuild.
    """
    _db, ops, s, _, repo, base, *_ = setup
    common = commit(
        repo.store,
        {".gitattributes": "generated.txt merge=aq-generated\n", "generated.txt": "base\n"},
        base=base,
    )
    git(repo.store, "push", "origin", f"{common}:main")
    a = commit(repo.store, {"generated.txt": "ours\n", "a.txt": "a\n"}, base=common)
    b = commit(repo.store, {"generated.txt": "theirs\n", "b.txt": "b\n"}, base=common)
    result = await ops.merge_sources(
        repo, common, merge_args(s, common, a, b).members, created_at=NOW,
        regenerate_generated=False,
    )
    assert result["outcome"] == "conflict", result
    assert result["files"] == ["generated.txt"]
    assert "regeneration is disabled" in result["reason"]


async def test_ambiguous_publish_reads_back_and_replays_without_second_push(setup):
    db, ops, s, fence, repo, base, head, _ = setup
    ops.git.ambiguous = True

    async def check_no_journal():
        assert await journal_rows(db) == []

    ops.git.push_hook = check_no_journal
    args = PublishArgs(fence=fence, expected_old_sha=base, new_sha=head)
    result = await ops.publish(s, args)
    assert result.outcome == "published", result
    assert git(ops.git.remote_path, "rev-parse", "main") == head
    assert ops.git.pushes == 1
    assert (await ops.publish(s, args)).outcome == "published"
    assert ops.git.pushes == 1
    assert await journal_rows(db) == []
    moved = commit(repo.store, {"moved.txt": "moved"}, base=head)
    git(repo.store, "push", "origin", f"{moved}:main")
    assert (await ops.publish(s, args)).outcome == "target_moved"
    assert ops.git.pushes == 1


async def test_unknown_after_push_survives_remote_read_failure_and_replay(setup):
    _, ops, s, fence, repo, base, head, _ = setup
    calls = 0

    async def read_hook():
        nonlocal calls
        calls += 1
        if calls > 1:
            ops.git.unreadable = True

    ops.git.read_hook = read_hook
    args = PublishArgs(fence=fence, expected_old_sha=base, new_sha=head)
    assert (await ops.publish(s, args)).outcome == "unknown_after_push"
    assert ops.git.pushes == 1
    ops.git.read_hook, ops.git.unreadable = None, False
    assert (await ops.publish(s, args)).outcome == "published"
    assert ops.git.pushes == 1


async def test_remote_expected_old_lease_closes_preflight_race(setup):
    _, ops, s, fence, repo, base, head, _ = setup
    other = commit(repo.store, {"other.txt": "other\n"}, base=base)

    async def move_before_push():
        git(repo.store, "push", "origin", f"{other}:main")

    ops.git.push_hook = move_before_push
    result = await ops.publish(s, PublishArgs(fence=fence, expected_old_sha=base, new_sha=head))
    assert result.outcome == "target_moved", result
    assert git(ops.git.remote_path, "rev-parse", "main") == other


@pytest.mark.parametrize("change", ["green", "head", "fence", "held", "legacy", "expired"])
async def test_publication_refuses_missing_green_and_stale_authority(setup, change):
    db, ops, s, fence, _, base, head, green = setup
    if change == "green":
        green.clear()
    elif change == "head":
        head = base
    elif change == "fence":
        fence = fence.model_copy(update={"token": 99})
    elif change == "expired":
        async with db._engine.begin() as conn:
            await conn.execute(update(integration_branch_owners).values(expires_at=1))
    else:
        async with db._engine.begin() as conn:
            values = (
                {"engine": "legacy"}
                if change == "legacy"
                else {
                    "gate_id": "gate",
                    "next_due_at": None,
                    "wait_reason": None,
                }
            )
            await conn.execute(update(integration_subjects).values(**values))
    result = await ops.publish(
        s, PublishArgs(fence=fence, expected_old_sha=base, new_sha=head, require_green=False)
    )
    assert result.is_unknown, result
    assert ops.git.pushes == 0


async def test_non_descendant_publication_refused_even_when_green(setup):
    _, ops, s, fence, _, base, head, green = setup
    # Remote main has head already; asking to publish the older base discards history.
    s = s.model_copy(update={"head_sha": base, "base_sha": head})

    # Validate the shape with a callback to isolate the immutable ancestry guard.
    async def authority(subject, fence=None):
        pass

    authority.trusted_green = ops.authority.trusted_green
    ops.authority = authority
    green.add(base)
    result = await ops.publish(s, PublishArgs(fence=fence, expected_old_sha=head, new_sha=base))
    assert result.reason == "publication_discards_target_history"
    assert ops.git.pushes == 0


async def test_materialization_and_preservation_are_exact_and_do_not_overwrite(setup):
    db, ops, s, _, repo, base, head, _ = setup
    ref = "refs/heads/aq/parent"
    await BranchOwnership(db).acquire(
        BranchKey(repository_id="r", branch=ref), "writer", "collector"
    )
    args = MaterializeRefArgs(repository_id="r", ref=ref, base_sha=base)
    assert (await ops.materialize_ref(s, args)).outcome == "created"
    assert (await ops.materialize_ref(s, args)).outcome == "exists_exact"
    assert (
        await ops.materialize_ref(s, args.model_copy(update={"base_sha": head}))
    ).outcome == "exists_other"
    preserve = PreserveArgs(repository_id="r", sha=head, retention_ref="refs/heads/aq/preserved/s")
    assert (await ops.preserve(s, preserve)).outcome == "preserved"
    assert (await ops.preserve(s, preserve)).outcome == "exists"
    assert (await ops.preserve(s, preserve.model_copy(update={"sha": base}))).is_unknown
    assert (await ops.preserve(s, preserve.model_copy(update={"retention_ref": ref}))).is_unknown
    assert (await ops.materialize_ref(s, args.model_copy(update={"ref": s.target_ref}))).is_unknown
    assert git(ops.git.remote_path, "rev-parse", "aq/preserved/s") == head


async def test_repository_publisher_exclusion_is_shared_and_independent_of_ref(setup):
    db, ops, s, fence, _, base, head, _ = setup
    # Shared Git cleanup can take exclusion without acquiring root authority.
    async with publisher_exclusion(db, "r"):
        pass
    # The primitives take that same lock, naming the subject they act for, so a
    # publisher already holding it is exactly the "already running" outcome.
    async with publisher_exclusion(db, "r", s):
        result = await ops.publish(s, PublishArgs(fence=fence, expected_old_sha=base, new_sha=head))
        assert result.is_unknown and "already running" in result.reason
        with pytest.raises(DevelopmentBusy):
            async with publisher_exclusion(db, "r", s):
                pass
        async with publisher_exclusion(db, "other-repository"):
            pass
    assert ops.git.pushes == 0


async def test_exact_ancestry_and_patch_equivalence(setup):
    _, ops, s, _, repo, base, head, _ = setup
    commit(repo.store, {"other.txt": "other\n"}, base=base)
    git(repo.store, "cherry-pick", head)
    equivalent = git(repo.store, "rev-parse", "HEAD")
    result = await ops.ancestry(
        s,
        AncestryArgs(
            repository_id="r",
            queries=(
                AncestryQuery(ancestor=base, descendant=head),
                AncestryQuery(ancestor=head, descendant=equivalent),
            ),
        ),
    )
    assert result.outcome == "facts", result
    a, b = result.detail["queries"]
    assert a["is_ancestor"] and a["merge_bases"] == [base]
    assert not b["is_ancestor"] and b["patch_equivalent"]
    assert b["ancestor"] == head and b["descendant"] == equivalent


async def test_branch_fence_cannot_transfer_while_bounded_push_runs(setup):
    db, ops, s, fence, _, base, head, _ = setup
    started, finish = asyncio.Event(), asyncio.Event()

    async def pause():
        started.set()
        await finish.wait()

    ops.git.push_hook = pause
    publish = asyncio.create_task(
        ops.publish(s, PublishArgs(fence=fence, expected_old_sha=base, new_sha=head))
    )
    await asyncio.wait_for(started.wait(), 5)
    transfer = asyncio.create_task(BranchOwnership(db).transfer(fence, "next", "collector"))
    try:
        await asyncio.sleep(0.05)
        assert not transfer.done()
    finally:
        finish.set()
    assert (await publish).outcome == "published"
    assert (await transfer).token > fence.token


async def test_cleanup_retention_expected_old_ambiguous_delete_and_replay(setup):
    db, ops, s, _, repo, base, head, _ = setup
    items = [
        SubjectCleanupItem("remote_ref", "refs/heads/aq/delivered", head, successful_source=True),
        SubjectCleanupItem("remote_ref", "refs/heads/aq/failed", head, failed_at=NOW),
        SubjectCleanupItem("remote_ref", "refs/heads/main", base),
        SubjectCleanupItem("remote_ref", "refs/heads/aq/moved", base),
    ]
    for name in ("aq/delivered", "aq/failed", "aq/moved"):
        git(repo.store, "push", "origin", f"{head}:refs/heads/{name}")
    async with db._engine.begin() as conn:
        await conn.execute(update(integration_subjects).values(phase="cleaning"))
    s = Subject.from_row(await db.get_integration_subject(s.id))

    async def inventory(subject):
        return items

    async def held(subject, item):
        return False

    cleanup = SubjectCleanup(ops, inventory=inventory, held=held, clock=lambda: NOW + 10)
    ops.git.ambiguous = True
    result = await cleanup(s, CleanupArgs(max_tries=1))
    assert result.outcome == "pending", result
    assert result.detail["deleted"] == ["refs/heads/aq/delivered"]
    assert result.detail["retained"] == ["refs/heads/aq/failed", "refs/heads/main"]
    assert result.detail["pending"][0]["reason"] == "cleanup ref moved"
    assert ops.git.deletes == 1
    result = await cleanup(s, CleanupArgs(max_tries=1))
    assert result.detail["pending"][0]["reason"] == "cleanup ref moved"
    assert ops.git.deletes == 1


async def test_subject_cleanup_keeps_default_and_every_flow_target(setup):
    db, ops, s, _, repo, _, head, _ = setup
    await db.create_project(Project(id="p", name="Promotion cleanup"))
    await db.create_repo(RepoConfig(
        id="r", project_id="p", source_type=RepoSourceType.CLONE,
        url=str(ops.git.remote_path), default_branch="dev",
    ))
    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            promotion_flow=[{"target": "staging"}, {"target": "aq/release"}],
        ))
    protected = ("dev", "staging", "aq/release", "gh-pages")
    items = []
    for branch in (*protected, "aq/ordinary"):
        ref = f"refs/heads/{branch}"
        git(repo.store, "push", "origin", f"{head}:{ref}")
        items.append(SubjectCleanupItem("remote_ref", ref, head, successful_source=True))
    s = await cleanup_subject(db, s)

    async def inventory(subject):
        return items

    async def held(subject, item):
        return False

    result = await SubjectCleanup(ops, inventory=inventory, held=held)(s, CleanupArgs())
    assert result.outcome == "clean", result
    assert result.detail["retained"] == [f"refs/heads/{branch}" for branch in protected]
    assert result.detail["deleted"] == ["refs/heads/aq/ordinary"]
    assert ops.git.deletes == 1
    for branch in protected:
        assert await ops.git.aremote_branch_head(repository=repo.binding, branch=branch) == head


async def test_ports_register_only_owned_mechanisms(setup):
    _, ops, *_ = setup
    ports = PrimitivePorts()
    ops.bind(ports)
    assert ports.bound == {
        Primitive.GIT_MATERIALIZE_REF,
        Primitive.GIT_MERGE_MEMBERS,
        Primitive.GIT_PRESERVE,
        Primitive.GIT_PUBLISH,
        Primitive.GIT_ANCESTRY,
    }


async def test_subject_collector_can_publish_without_a_repair_writer(setup):
    db, ops, s, fence, _, base, head, _ = setup
    async with db._engine.begin() as conn:
        await conn.execute(
            update(integration_subjects).values(
                writer_status="none",
                writer_task_id=None,
                writer_fence_token=None,
                writer_last_push_at=None,
            )
        )
    s = Subject.from_row(await db.get_integration_subject(s.id))
    # Another subject's valid collector fence never grants this subject a write.
    args = PublishArgs(fence=fence, expected_old_sha=base, new_sha=head)
    assert (await ops.publish(s, args)).is_unknown
    assert ops.git.pushes == 0
    collector = await BranchOwnership(db).transfer(fence, s.id, "collector")
    args = args.model_copy(update={"fence": collector})
    assert (await ops.publish(s, args)).outcome == "published"
    ref = "refs/heads/aq/canonical"
    await BranchOwnership(db).acquire(BranchKey(repository_id="r", branch=ref), s.id, "collector")
    assert (
        await ops.materialize_ref(
            s,
            MaterializeRefArgs(
                repository_id="r",
                ref=ref,
                base_sha=head,
            ),
        )
    ).outcome == "created"


async def test_merge_crash_after_pin_before_result_replays_the_same_commit(setup):
    db, ops, s, _, repo, base, head, _ = setup
    original = ops.run

    async def crash_after_pin(repo, *args, **kwargs):
        result = await original(repo, *args, **kwargs)
        if args[0] == "update-ref":
            raise RuntimeError("simulated crash after pin")
        return result

    ops.run = crash_after_pin
    with pytest.raises(RuntimeError, match="simulated crash"):
        await ops.merge_members(s, merge_args(s, base, head))
    pinned = git(repo.store, "for-each-ref", "--format=%(objectname)", "refs/aq/batch-objects")
    assert pinned
    assert await journal_rows(db) == []
    ops.run = original
    result = await ops.merge_members(s, merge_args(s, base, head))
    assert result.outcome == "merged"
    assert result.detail["head"] == pinned


async def test_green_rechecked_before_push_without_intent_record(setup):
    db, ops, s, fence, _, base, head, _ = setup
    calls = 0

    async def green(subject, sha):
        nonlocal calls
        calls += 1
        return calls == 1

    ops.authority.trusted_green = green
    result = await ops.publish(s, PublishArgs(fence=fence, expected_old_sha=base, new_sha=head))
    assert result.is_unknown and "green changed" in result.reason
    assert ops.git.pushes == 0
    assert await journal_rows(db) == []


@pytest.mark.parametrize("tamper", ["replace", "graft"])
async def test_retained_ancestry_customization_cannot_manufacture_proof(setup, tamper):
    _, ops, s, _, repo, base, head, _ = setup
    if tamper == "replace":
        git(repo.store, "replace", base, head)
    else:
        grafts = repo.store / ".git/info/grafts"
        grafts.write_text(f"{base} {head}\n")
    result = await ops.ancestry(
        s, AncestryArgs(repository_id="r", queries=(AncestryQuery(ancestor=head, descendant=base),))
    )
    assert result.is_unknown


async def cleanup_subject(db, subject):
    async with db._engine.begin() as conn:
        await conn.execute(update(integration_subjects).values(phase="cleaning"))
    return Subject.from_row(await db.get_integration_subject(subject.id))


async def test_cleanup_unowned_ref_serializes_first_writer_acquisition(setup):
    db, ops, s, _, repo, base, head, _ = setup
    ref = "refs/heads/aq/source"
    git(repo.store, "push", "origin", f"{head}:{ref}")
    s = await cleanup_subject(db, s)
    item = SubjectCleanupItem("remote_ref", ref, head)

    async def inventory(subject):
        return [item]

    async def held(subject, item):
        return False

    started, finish = asyncio.Event(), asyncio.Event()

    async def pause():
        started.set()
        await finish.wait()

    ops.git.delete_hook = pause
    cleanup = SubjectCleanup(ops, inventory=inventory, held=held)
    deletion = asyncio.create_task(cleanup(s, CleanupArgs()))
    await asyncio.wait_for(started.wait(), 5)
    acquire = asyncio.create_task(
        BranchOwnership(db).acquire(
            BranchKey(repository_id="r", branch=ref), "new-writer", "worker"
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert not acquire.done()
    finally:
        finish.set()
    assert (await deletion).outcome == "clean"
    assert (await acquire).token == 1
    assert await ops.git.aremote_branch_head(repository=repo.binding, branch="aq/source") is None


async def test_cleanup_reconciles_uncertain_delete_even_after_last_try(setup):
    db, ops, s, _, repo, _, head, _ = setup
    ref = "refs/heads/aq/source"
    git(repo.store, "push", "origin", f"{head}:{ref}")
    s = await cleanup_subject(db, s)

    async def inventory(subject):
        return [SubjectCleanupItem("remote_ref", ref, head)]

    async def held(subject, item):
        return False

    async def make_readback_unknown():
        ops.git.unreadable = True

    ops.git.delete_hook = make_readback_unknown
    cleanup = SubjectCleanup(ops, inventory=inventory, held=held)
    args = CleanupArgs(max_tries=1)
    assert (await cleanup(s, args)).outcome == "pending"
    assert ops.git.deletes == 1
    ops.git.unreadable = False
    assert (await cleanup(s, args)).outcome == "clean"
    assert ops.git.deletes == 1


async def test_cleanup_policy_retention_deadline_and_irreversible_marker(setup):
    db, ops, s, _, repo, _, head, _ = setup
    ref = "refs/heads/aq/source"
    git(repo.store, "push", "origin", f"{head}:{ref}")
    s = await cleanup_subject(db, s)
    items = [SubjectCleanupItem("remote_ref", ref, head, successful_source=True, failed_at=NOW)]

    async def inventory(subject):
        return items

    async def held(subject, item):
        return False

    cleanup = SubjectCleanup(ops, inventory=inventory, held=held, clock=lambda: NOW + 5)
    result = await cleanup(s, CleanupArgs(retain_failed_seconds=10))
    assert result.outcome == "pending" and result.detail["retention_due_at"] == NOW + 10
    assert ops.git.deletes == 0
    cleanup.clock = lambda: NOW + 11
    result = await cleanup(
        s, CleanupArgs(delete_successful_sources=False, retain_failed_seconds=10)
    )
    assert result.outcome == "clean" and result.detail["retained"] == [ref]
    items.append(SubjectCleanupItem("remote_ref", "refs/heads/aq/marker", head, irreversible=True))
    assert (await cleanup(s, CleanupArgs(retain_failed_seconds=0))).outcome == "irreversible_marker"
    assert ops.git.deletes == 0
    items.pop()
    assert (await cleanup(s, CleanupArgs(retain_failed_seconds=0))).outcome == "clean"
    assert ops.git.deletes == 1


async def test_cleanup_local_refs_and_pr_port_preserve_holds_and_replay(setup):
    db, ops, s, _, repo, _, head, _ = setup
    local = "refs/aq/retained/test"
    git(repo.store, "update-ref", local, head)
    s = await cleanup_subject(db, s)
    items = [
        SubjectCleanupItem("local_ref", local, head),
        SubjectCleanupItem("pull_request", "pr:1", head),
    ]
    closes = []
    hold = True
    pr_open = True

    async def inventory(subject):
        return [item for item in items if item.kind != "pull_request" or pr_open]

    async def held(subject, item):
        return hold

    async def close_pr(repo, item):
        nonlocal pr_open
        closes.append((repo.binding.full_name, item.identity, item.expected_sha))
        pr_open = False
        return True

    cleanup = SubjectCleanup(ops, inventory=inventory, held=held, close_pr=close_pr)
    assert (await cleanup(s, CleanupArgs())).outcome == "pending"
    assert not closes and git(repo.store, "rev-parse", local) == head
    hold = False
    assert (await cleanup(s, CleanupArgs())).outcome == "clean"
    assert closes == [("test/repo", "pr:1", head)]
    assert await ops.git.aref_exists(str(repo.store), local) is False
    assert (await cleanup(s, CleanupArgs())).outcome == "clean"
    assert len(closes) == 1
