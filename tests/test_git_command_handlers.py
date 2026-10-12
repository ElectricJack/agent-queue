"""Unit tests for git-related command handler methods.

Tests each new command handler (_cmd_create_branch, _cmd_checkout_branch,
_cmd_commit_changes, _cmd_push_branch, _cmd_merge_branch, _cmd_git_log,
_cmd_git_diff) with mocked GitManager and Database to test command handler
logic in isolation.
"""

import pytest

from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitError, GitManager
from src.git.github_contracts import GitHubAccessError
from src.models import Project, RepoConfig, RepoSourceType, TaskStatus
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def provenance_repo(tmp_path):
    """Real local Git transport, with independent source/target histories."""
    from src.integration.provenance import GitProvenance

    git = GitManager()
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "source"
    await git._arun(["init", "--bare", str(remote)], cwd=str(tmp_path))
    await git._arun(["init", "-b", "main", str(checkout)], cwd=str(tmp_path))
    await git._arun(["config", "user.name", "Tester"], cwd=str(checkout))
    await git._arun(["config", "user.email", "test@example.com"], cwd=str(checkout))
    (checkout / "seed").write_text("seed\n")
    await git.acommit_all(str(checkout), "initial")
    await git._arun(["remote", "add", "origin", str(remote)], cwd=str(checkout))
    await git.apush_branch(str(checkout), "main")
    await git._arun(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=str(remote))
    store = GitProvenance(git, str(checkout), repository_url=str(remote))
    base = await store.run("rev-parse", "HEAD")
    await store.run("checkout", "-b", "aq/task")
    return git, store, checkout, remote, base


async def _provenance_commit(repo, name, text="work", message="worker change"):
    git, store, checkout, _remote, _base = repo
    (checkout / name).write_text(text)
    await git.acommit_all(str(checkout), message)
    return await store.run("rev-parse", "HEAD")


class TestExactGitProvenance:
    async def test_multiple_commits_partial_pick_and_copied_marker_do_not_prove_completion(
        self, provenance_repo
    ):
        from src.integration.provenance import CompletedSource, CompletionIdentity

        _git, store, _path, _remote, base = provenance_repo
        first = await _provenance_commit(provenance_repo, "one", message="one\n\nAQ-Task: task")
        final = await _provenance_commit(provenance_repo, "two", message="two\n\nAQ-Task: task")
        completed = CompletedSource(CompletionIdentity("p", "r", "task", "close-1"), final)
        marker = await store.write_completion(completed, claim_epoch=1)
        assert completed.identity.ref.startswith("refs/aq/provenance/completions/")
        assert completed.identity.ref in await store.run("ls-remote", "origin")
        assert "aq-provenance" not in await store.run("ls-remote", "--heads", "origin")
        assert "aq-provenance" not in await store.run("for-each-ref", "--format=%(refname)")
        await store.run("checkout", "-b", "target", base)
        # Force an actual rewrite, not cherry-pick's optional fast-forward.
        await _provenance_commit(provenance_repo, "target-only")
        await store.run("cherry-pick", first)
        partial = await store.run("rev-parse", "HEAD")
        assert not await store.contained(completed, partial)
        # Copying even the whole completion message into an empty commit has
        # neither source ancestry nor explicit replacement authority.
        message = await store.run("show", "-s", "--format=%B", marker)
        await store.run("commit", "--allow-empty", "-m", message)
        assert not await store.contained(completed, await store.run("rev-parse", "HEAD"))
        await store.run("merge", "--no-ff", "aq/task", "-m", "complete external merge")
        assert await store.contained(completed, await store.run("rev-parse", "HEAD"))

    async def test_repeated_record_is_idempotent_and_reopened_generation_is_distinct(self, provenance_repo):
        from src.integration.provenance import CompletedSource, CompletionIdentity

        _git, store, _path, _remote, _base = provenance_repo
        first = await _provenance_commit(provenance_repo, "one")
        old = CompletedSource(CompletionIdentity("p", "r", "task", "close-1"), first)
        oid = await store.write_completion(old, claim_epoch=1)
        assert await store.write_completion(old, claim_epoch=1) == oid
        final = await _provenance_commit(provenance_repo, "two")
        new = CompletedSource(CompletionIdentity("p", "r", "task", "close-2"), final)
        await store.write_completion(new, claim_epoch=2)
        assert await store.contained(old, first)
        assert not await store.contained(new, first)
        with pytest.raises(ValueError, match="already binds"):
            await store.write_completion(CompletedSource(old.identity, final), claim_epoch=1)

    async def test_batch_write_publishes_in_one_transfer_and_settles_each_generation(
        self, provenance_repo, monkeypatch
    ):
        """noble-horizon: one push per generation made a migration page outlive its client."""
        from src.integration.provenance import CompletedSource, CompletionIdentity

        git, store, _path, _remote, base = provenance_repo
        first = await _provenance_commit(provenance_repo, "one")
        second = await _provenance_commit(provenance_repo, "two")
        taken = CompletedSource(CompletionIdentity("p", "r", "taken", "g"), first)
        await store.write_completion(CompletedSource(taken.identity, base))
        kept = CompletedSource(CompletionIdentity("p", "r", "kept", "g"), second)
        await store.write_completion(kept)
        fresh = [CompletedSource(CompletionIdentity("p", "r", f"task-{i}", "g"), source)
                 for i, source in enumerate((first, second, first))]
        pushes, single = [], []
        real_push = git.apush_new_refs

        async def counted(checkout, tips, **kwargs):
            pushes.append(dict(tips))
            return await real_push(checkout, tips, **kwargs)

        async def refused(*_args, **_kwargs):
            single.append(True)
            raise AssertionError("a batch must not fall back to one push per generation")

        monkeypatch.setattr(git, "apush_new_refs", counted)
        monkeypatch.setattr(git, "_apush_oid", refused)
        results = await store.write_completions([*fresh, taken, kept])
        # One transfer carries every absent ref, and nothing else.
        assert [sorted(tips) for tips in pushes] == [sorted(c.identity.ref for c in fresh)]
        assert not single
        for completed in fresh:
            assert (await store.read_completion(completed.identity))["source_oid"] == completed.source_oid
            assert results[completed.identity] == await store.run(
                "rev-parse", completed.identity.ref)
        # A generation bound to other evidence fails alone; an identical one is kept.
        assert isinstance(results[taken.identity], ValueError)
        assert "already binds different evidence" in str(results[taken.identity])
        assert isinstance(results[kept.identity], str)
        remote_refs = await store.run("ls-remote", "origin")
        assert await store.write_completions(fresh) == {c.identity: results[c.identity] for c in fresh}
        assert len(pushes) == 1 and await store.run("ls-remote", "origin") == remote_refs
        # A repeat shares one result; one generation bound twice binds neither.
        twice = CompletionIdentity("p", "r", "twice", "g")
        repeated = await store.write_completions([CompletedSource(twice, first)] * 2)
        assert list(repeated) == [twice] and isinstance(repeated[twice], str)
        split = CompletionIdentity("p", "r", "split", "g")
        conflicted = await store.write_completions(
            [CompletedSource(split, first), CompletedSource(split, second)])
        assert "two sources" in str(conflicted[split])
        assert split.ref not in await store.run("ls-remote", "origin")

    async def test_archive_and_source_branch_deletion_preserve_git_proof(self, provenance_repo, db):
        from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
        from src.models import Task, TaskCompletion

        git, store, path, remote, _base = provenance_repo
        source = await _provenance_commit(provenance_repo, "one")
        completed = CompletedSource(CompletionIdentity("p", "r", "task", "close-1"), source)
        await db.create_project(Project(id="p", name="P"))
        await db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote)))
        await db.create_task(Task(id="task", project_id="p", repo_id="r", title="T", description="d"))
        await db.transition_task("task", TaskStatus.COMPLETED)
        await db.save_task_completion(TaskCompletion(id="close-1", task_id="task", outcome="pass",
                                                      commits=[source], completed_at=1))
        await git.apush_branch(str(path), "aq/task")
        await store.write_completion(completed)
        await db.archive_task("task")
        await git.adelete_remote_ref_exact(str(path), "aq/task", source)
        await store.run("checkout", "main")
        await store.run("branch", "-D", "aq/task")
        fresh = path.parent / "fresh"
        await git.acreate_checkout(str(remote), str(fresh), no_checkout=True)
        await git.afetch_origin(str(fresh), repository_url=str(remote), all_heads=True)
        retained = GitProvenance(git, str(fresh), repository_url=str(remote))
        assert await retained.contained(completed, source)
        assert await db.get_task("task") is None

    async def test_complete_rebased_repair_requires_exact_generation_source_and_full_evidence(self, provenance_repo):
        from src.integration.provenance import CompletedSource, CompletionIdentity

        _git, store, path, _remote, base = provenance_repo
        first = await _provenance_commit(provenance_repo, "one")
        final = await _provenance_commit(provenance_repo, "two")
        original = CompletedSource(CompletionIdentity("p", "r", "task", "close-1"), final)
        await store.write_completion(original)
        await store.run("checkout", "-b", "repair", base)
        await _provenance_commit(provenance_repo, "advanced-main")
        repair_base = await store.run("rev-parse", "HEAD")
        await store.run("cherry-pick", first, final)
        repair = await store.run("rev-parse", "HEAD")
        assert not await store.contained(original, repair)
        evidence = await store.write_replacement(source_oid=repair, base_oid=repair_base,
            replaces=[original], authority="repair_contract", reason="all work resolved")
        assert evidence in await store.run("ls-remote", "origin", "refs/aq/provenance/replacements/*")
        assert "aq-provenance" not in await store.run("ls-remote", "--heads", "origin")
        assert await store.contained(original, repair)
        assert not await store.contained(original, await store.run("rev-parse", repair + "^"))
        reopened = CompletedSource(CompletionIdentity("p", "r", "task", "close-2"), final)
        await store.write_completion(reopened, claim_epoch=2)
        assert not await store.contained(reopened, repair)
        with pytest.raises(ValueError, match="exact original"):
            await store.write_replacement(source_oid=repair, base_oid=repair_base,
                replaces=[CompletedSource(original.identity, first)], authority="operator", reason="bad")
        await store.run("checkout", "-b", "empty", base)
        await store.run("commit", "--allow-empty", "-m", "marker")
        with pytest.raises(ValueError, match="empty marker"):
            await store.write_replacement(source_oid=await store.run("rev-parse", "HEAD"),
                base_oid=base, replaces=[original], authority="operator", reason="bad marker")
        assert (path / "seed").exists()
        assert evidence

    async def test_provenance_does_not_touch_hook_index_or_other_worktree(self, provenance_repo):
        from src.integration.provenance import CompletedSource, CompletionIdentity

        git, store, path, _remote, base = provenance_repo
        from src.claim_file import write_claim_file

        write_claim_file(str(path), {"task_id": "task", "claim_epoch": 1})
        # Pool metadata is excluded from ordinary worker commits by GitManager.
        hook_dir = path / ".git" / "hooks"
        hook = hook_dir / "commit-msg"
        hook.write_text('#!/bin/sh\necho ran >> "$PWD/hook-log"\n')
        hook.chmod(0o755)
        source = await _provenance_commit(provenance_repo, "one")
        assert "AQ-Task: task" in await store.run("show", "-s", "--format=%B", source)
        assert (path / "hook-log").read_text() == "ran\n"
        other = path.parent / "other"
        await git.aworktree_add(str(path), str(other), ref=base, detach=True)
        (path / "staged").write_text("staged")
        await store.run("add", "staged")
        before = await store.run("write-tree")
        await store.write_completion(CompletedSource(CompletionIdentity("p", "r", "task", "g"), source))
        assert await store.run("write-tree") == before
        assert await git._arun(["rev-parse", "HEAD"], cwd=str(other)) == base
        assert (path / "hook-log").read_text() == "ran\n"

    async def test_missing_wrong_identity_and_git_error_fail_closed(self, provenance_repo, monkeypatch):
        from src.integration.provenance import CompletedSource, CompletionIdentity

        _git, store, _path, _remote, base = provenance_repo
        source = await _provenance_commit(provenance_repo, "one")
        item = CompletedSource(CompletionIdentity("p", "r", "task", "g"), source)
        assert not await store.contained(item, source)
        await store.write_completion(item)
        wrong = CompletedSource(CompletionIdentity("p", "elsewhere", "task", "g"), source)
        assert not await store.contained(wrong, source)
        with pytest.raises(ValueError, match="full lowercase"):
            await store.exact(source[:12])
        await store.run("replace", base, source)
        assert not await store.contained(item, base)
        async def failed(*args, **kwargs):
            raise GitError("network unavailable")
        monkeypatch.setattr(store.git, "als_remote_qualified_refs", failed)
        with pytest.raises(GitError, match="network unavailable"):
            await store.write_completion(item)

    async def test_code_free_provenance_is_not_a_delivered_artifact(self, provenance_repo):
        from src.integration.provenance import CompletedSource, CompletionIdentity

        _git, store, _path, _remote, base = provenance_repo
        item = CompletedSource(CompletionIdentity("p", "r", "task", "g"), base)
        await store.write_completion(item, artifact=False)
        assert (await store.read_completion(item.identity))["artifact"] is False
        assert not await store.contained(item, base)


class TestProvenanceNamespaceMigration:
    async def _legacy(self, provenance_repo):
        from src.integration.provenance import CompletedSource, CompletionIdentity

        git, store, path, remote, _base = provenance_repo
        source = await _provenance_commit(provenance_repo, "legacy-source")
        item = CompletedSource(CompletionIdentity("p", "r", "task", "g"), source)
        marker = await store.write_completion(item)
        # Model a pre-upgrade remote and bare/local branch without rewriting its object.
        await git._arun(["update-ref", "refs/heads/" + item.identity.legacy_branch, marker],
                       cwd=str(remote))
        await git._arun(["update-ref", "-d", item.identity.ref], cwd=str(remote))
        await store.run("update-ref", "refs/heads/" + item.identity.legacy_branch, marker)
        await store.run("update-ref", "-d", item.identity.ref)
        return item, marker

    async def test_dry_run_changes_no_refs_and_apply_preserves_exact_object(self, provenance_repo):
        from src.integration.provenance_migration import ProvenanceMigration

        git, store, path, remote, _base = provenance_repo
        item, marker = await self._legacy(provenance_repo)
        assert (await store.read_completion(item.identity))["source_oid"] == item.source_oid
        remote_before = await store.run("ls-remote", "origin")
        local_before = await store.run("for-each-ref", "--format=%(refname) %(objectname)")
        config_before = (path / ".git" / "config").read_bytes()
        migration = ProvenanceMigration(git)
        preview = await migration.run(str(remote), checkout=str(path))
        assert preview["outcome"] == "preview" and len(preview["rows"]) == 2
        assert await store.run("ls-remote", "origin") == remote_before
        assert await store.run("for-each-ref", "--format=%(refname) %(objectname)") == local_before
        assert (path / ".git" / "config").read_bytes() == config_before
        applied = await migration.run(str(remote), checkout=str(path), dry_run=False)
        assert applied["outcome"] == "migrated" and applied["remaining"] == 0
        assert {row["action"] for row in applied["rows"]} == {"migrated"}
        assert await store.run("rev-parse", item.identity.ref) == marker
        assert marker in await store.run("ls-remote", "origin", item.identity.ref)
        assert "aq-provenance" not in await store.run("ls-remote", "--heads", "origin")
        assert "aq-provenance" not in await store.run("for-each-ref", "--format=%(refname)")
        assert await store.contained(item, item.source_oid)
        assert (await migration.run(str(remote), dry_run=False))["rows"] == []

    async def test_backstop_keeps_live_legacy_refs_and_audits_before_deletion(self, provenance_repo):
        from contextlib import asynccontextmanager

        from src.integration.provenance_migration import ProvenanceMigration

        git, store, path, remote, _base = provenance_repo
        item, marker = await self._legacy(provenance_repo)
        allowed = False
        audits = []

        @asynccontextmanager
        async def guard(branch):
            assert branch == item.identity.legacy_branch
            yield allowed

        def audit(scope, branch, oid, proof):
            assert branch == item.identity.legacy_branch and oid == marker
            assert proof == "provenance:" + item.identity.ref
            audits.append((scope, branch, oid))

        migration = ProvenanceMigration(git)
        held = await migration.run(str(remote), checkout=str(path), dry_run=False,
                                   deletion_guard=guard, audit_delete=audit)
        assert all(row["action"] == "blocked" for row in held["rows"])
        assert audits == []
        assert await git.arev_parse(str(remote), "refs/heads/" + item.identity.legacy_branch) == marker
        assert await git.arev_parse(str(path), "refs/heads/" + item.identity.legacy_branch) == marker
        allowed = True
        original_remote_delete = git.adelete_remote_ref_exact
        original_local_delete = git.adelete_local_ref_exact

        async def remote_delete(*args, **kwargs):
            assert ("remote", item.identity.legacy_branch, marker) in audits
            return await original_remote_delete(*args, **kwargs)

        async def local_delete(*args, **kwargs):
            assert ("local", item.identity.legacy_branch, marker) in audits
            return await original_local_delete(*args, **kwargs)

        git.adelete_remote_ref_exact = remote_delete
        git.adelete_local_ref_exact = local_delete
        applied = await migration.run(str(remote), checkout=str(path), dry_run=False,
                                      deletion_guard=guard, audit_delete=audit)
        assert all(row["action"] == "migrated" for row in applied["rows"])

    @pytest.mark.parametrize("failure", ["conflict", "copy", "moved", "invalid"])
    async def test_failed_copy_or_changed_old_ref_is_never_deleted(
        self, provenance_repo, monkeypatch, failure,
    ):
        from src.git.manager import RemoteRefResult, RemoteRefState
        from src.integration.provenance_migration import ProvenanceMigration

        git, store, _path, remote, base = provenance_repo
        item, marker = await self._legacy(provenance_repo)
        old_ref = "refs/heads/" + item.identity.legacy_branch
        if failure == "conflict":
            await git._arun(["update-ref", item.identity.ref, base], cwd=str(remote))
        elif failure == "copy":
            async def failed_copy(checkout, tips, **kwargs):
                return {ref: RemoteRefResult(RemoteRefState.ERROR, error="copy failed")
                        for ref in tips}
            monkeypatch.setattr(git, "apush_new_refs", failed_copy)
        elif failure == "moved":
            delete = git.adelete_remote_ref_exact
            async def changed(checkout, branch, expected_old_oid, **kwargs):
                await git._arun(["update-ref", old_ref, base], cwd=str(remote))
                await delete(checkout, branch, expected_old_oid, **kwargs)
            monkeypatch.setattr(git, "adelete_remote_ref_exact", changed)
        else:
            await git._arun(["update-ref", old_ref, base], cwd=str(remote))
        result = await ProvenanceMigration(git).run(str(remote), dry_run=False)
        assert result["outcome"] == "blocked"
        assert result["rows"][0]["action"] == "blocked"
        expected = base if failure in {"moved", "invalid"} else marker
        assert (await git.als_remote_ref(str(store.checkout), item.identity.legacy_branch)).oid == expected

    async def test_replacement_migration_and_fresh_fetched_snapshot(self, provenance_repo):
        from src.integration.delivery_truth import delivery_snapshot
        from src.integration.provenance import GitProvenance
        from src.integration.provenance_migration import ProvenanceMigration

        git, store, path, remote, base = provenance_repo
        item, _marker = await self._legacy(provenance_repo)
        await store.run("checkout", "-b", "repair", base)
        await _provenance_commit(provenance_repo, "target-work")
        repair_base = await store.run("rev-parse", "HEAD")
        await store.run("cherry-pick", item.source_oid)
        repair = await store.run("rev-parse", "HEAD")
        replacement = await store.write_replacement(source_oid=repair, base_oid=repair_base,
            replaces=[item], authority="operator", reason="entire source replaced")
        ref = (await store.run("for-each-ref", "--format=%(refname)",
                               "refs/aq/provenance/replacements/")).strip()
        legacy = "refs/heads/aq-provenance/" + ref.removeprefix("refs/aq/provenance/")
        await git._arun(["update-ref", legacy, replacement], cwd=str(remote))
        await git._arun(["update-ref", "-d", ref], cwd=str(remote))
        applied = await ProvenanceMigration(git).run(str(remote), dry_run=False, limit=1)
        assert applied["remaining"] == 1
        assert (await ProvenanceMigration(git).run(str(remote), dry_run=False))["remaining"] == 0
        fresh = str(path.parent / "observer")
        await git.acreate_checkout(str(remote), fresh, no_checkout=True)
        snapshot = await delivery_snapshot(git, fresh, project_id="p", repository_id="r",
            repository_url=str(remote), target_ref="refs/heads/main")
        assert item.identity.ref in snapshot.source_heads
        retained = GitProvenance(git, fresh, repository_url=str(remote))
        assert (await retained.read_completion(item.identity, refs=snapshot.source_heads))["source_oid"] == item.source_oid
        assert await retained.read_completion(item.identity, refs={}) is None
        assert await retained.contained(item, repair)

    @pytest.mark.parametrize("failure", ["conflict", "attached", "moved"])
    async def test_local_migration_preserves_conflicting_attached_or_changed_head(
        self, provenance_repo, monkeypatch, failure,
    ):
        from src.integration.provenance_migration import ProvenanceMigration

        git, store, path, remote, base = provenance_repo
        item, marker = await self._legacy(provenance_repo)
        old_ref = "refs/heads/" + item.identity.legacy_branch
        if failure == "conflict":
            await store.run("update-ref", item.identity.ref, base)
        elif failure == "attached":
            await store.run("checkout", item.identity.legacy_branch)
        else:
            delete = git.adelete_local_ref_exact
            async def changed(checkout, *, ref, expected_old_oid):
                await store.run("update-ref", ref, base)
                await delete(checkout, ref=ref, expected_old_oid=expected_old_oid)
            monkeypatch.setattr(git, "adelete_local_ref_exact", changed)
        result = await ProvenanceMigration(git).run(
            str(remote), checkout=str(path), dry_run=False,
        )
        local = [row for row in result["rows"] if row["scope"] == "local"]
        assert result["outcome"] == "blocked" and local[0]["action"] == "blocked"
        assert await store.run("rev-parse", old_ref) == (base if failure == "moved" else marker)

    async def test_writers_never_rebind_legacy_generation(self, provenance_repo):
        from src.integration.provenance import CompletedSource

        _git, store, _path, _remote, base = provenance_repo
        item, marker = await self._legacy(provenance_repo)
        conflicting = CompletedSource(item.identity, base)
        with pytest.raises(ValueError, match="already binds"):
            await store.write_completion(conflicting)
        assert isinstance((await store.write_completions([conflicting]))[item.identity], ValueError)
        assert (await store.git.als_remote_qualified_refs(
            store.checkout, [item.identity.ref]))[item.identity.ref].oid is None
        assert await store.write_completion(item) == marker
        assert "aq-provenance" in await store.run("ls-remote", "--heads", "origin")

    async def test_operator_command_uses_designated_repo_and_refuses_worker(self, provenance_repo, db):
        from types import SimpleNamespace
        from src.commands.integration_commands import IntegrationCommandsMixin
        from src.commands.principal import (
            ExecutionPrincipal, PrincipalKind, TRUSTED_LOCAL, principal_context,
        )

        git, _store, _path, remote, _base = provenance_repo
        await self._legacy(provenance_repo)
        await db.create_project(Project(id="p", name="P"))
        await db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE,
                                       url=str(remote)))
        await db.update_project("p", integration_repository_id="r")
        handler = IntegrationCommandsMixin()
        handler.db = db
        handler.orchestrator = SimpleNamespace(git=git)
        request = {"project_id": "p", "dry_run": True}
        preview = await handler._cmd_integration_migrate_provenance_refs(request)
        assert preview["success"] and preview["outcome"] == "preview"
        with principal_context(ExecutionPrincipal(kind=PrincipalKind.SESSION,
                               policy=TRUSTED_LOCAL.policy, project_id="p")):
            refused = await handler._cmd_integration_migrate_provenance_refs(request)
        assert not refused["success"] and refused["outcome"] == "refused"


class TestLegacyGitProvenanceMigration:
    async def _seed(self, db, remote, source, *, commits=None):
        from src.models import Task, TaskCompletion

        await db.create_project(Project(id="p", name="P", hierarchical_integration_mode="development"))
        await db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote)))
        await db.update_project("p", integration_repository_id="r")
        await db.create_task(Task(id="task", project_id="p", repo_id="r", title="T", description="d"))
        await db.transition_task("task", TaskStatus.COMPLETED)
        await db.save_task_completion(TaskCompletion(id="g", task_id="task", outcome="pass",
            commits=[source] if commits is None else commits, completed_at=1))

    @staticmethod
    def _delivery(identity, manifest, *, created_at, evidence=None, state="delivered"):
        return {"id": identity, "project_id": "p", "repository_id": "r",
                "target_ref": "refs/heads/main", "expected_sha": None, "prepared_sha": None,
                "state": state, "manifest": manifest, "evidence": evidence or {},
                "reason": "legacy", "created_at": created_at, "updated_at": created_at}

    @staticmethod
    async def _retain(db, rows):
        """Retired journal rows, retained exactly as revision a00000000038 keeps them."""
        import json
        from importlib import import_module

        from sqlalchemy import insert
        from src.database.tables import events

        retire = import_module("migrations.versions.a00000000038_retire_development_deliveries")
        async with db._engine.begin() as conn:
            await conn.execute(insert(events), [{
                "event_type": retire.PROVENANCE_EVENT, "project_id": row["project_id"],
                "payload": json.dumps(retire._provenance(
                    row, row["evidence"], retire._members(row["manifest"]))),
                "timestamp": row["created_at"],
            } for row in rows])




    async def _completed(self, db, identity, *, commits=None, completed_at=2, branch=True):
        from src.models import Task, TaskCompletion

        await db.create_task(Task(id=identity, project_id="p", repo_id="r", title="T",
                                  description="d", status=TaskStatus.COMPLETED,
                                  branch_name=f"aq/{identity}" if branch else None))
        if commits is not None:
            await db.save_task_completion(TaskCompletion(
                id=identity + "-g", task_id=identity, outcome="pass", commits=commits,
                completed_at=completed_at))













    @pytest.mark.parametrize("recorded_id", [None, "missing-close"])
    @pytest.mark.parametrize("no_artifact", [False, True])
    @pytest.mark.parametrize("reopened", [False, True])
    async def test_upgrade_restores_audited_timestamp_identity_only_before_reopen(
        self, provenance_repo, db, recorded_id, no_artifact, reopened,
    ):
        import hashlib
        import json
        from importlib import import_module

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY
        from src.integration.development import DevelopmentPrimitives
        from src.integration.delivery_truth import DeliveryState
        from src.integration.provenance import CompletedSource, CompletionIdentity

        git, store, path, remote, base = provenance_repo
        await self._seed(db, remote, base)
        await self._completed(db, "legacy")
        version = await db.get_task_updated_at("legacy")
        old_id = "legacy:" + hashlib.sha256(json.dumps(
            ["p", "r", "legacy", version], separators=(",", ":"),
        ).encode()).hexdigest()
        identity = CompletionIdentity("p", "r", "legacy", old_id)
        await store.write_completion(CompletedSource(identity, base), artifact=not no_artifact)
        if recorded_id:
            await db.set_task_meta("legacy", DEVELOPMENT_COMPLETION_ID_KEY, recorded_id)
            await store.write_completion(CompletedSource(
                CompletionIdentity("p", "r", "legacy", recorded_id), base,
            ), artifact=not no_artifact)
        await db.log_event("development.provenance_attested", project_id="p", task_id="legacy",
            payload=json.dumps({"operator_id": "supervisor-p", "reason": "legacy reviewed",
                "source_oid": None if no_artifact else base, "artifact": not no_artifact,
                "results": [{"task_id": "legacy", "generation": recorded_id or old_id,
                             "legacy_generation": old_id, "action": "written",
                             "source_oid": base, "artifact": not no_artifact, "error": None}]}))
        await db.add_task_comment("legacy", "post-attestation comment", author_kind="supervisor",
                                  author_id="supervisor-p")
        await db.update_task_description("legacy", "new findings", expected_description="d")
        await db.update_task("legacy", description="new description")
        if reopened:
            await db.transition_task("legacy", TaskStatus.READY)
            await db.transition_task("legacy", TaskStatus.COMPLETED)
        revision = import_module("migrations.versions.a00000000062_legacy_completion_identity")

        def upgrade_old_schema(conn):
            # Only this fixture's disposable DB is changed, never an operator DB.
            with Operations.context(MigrationContext.configure(conn)):
                revision.downgrade()
                revision.upgrade()

        async with db._engine.begin() as conn:
            await conn.run_sync(upgrade_old_schema)
        observer = DevelopmentPrimitives(db, data_dir=path / "data", git=git).delivery_observer
        db.set_delivery_observer(observer)
        proof = (await observer.observe(["legacy"])).get("legacy")
        if reopened:
            assert proof.request.legacy_generation != old_id
            assert proof.state == DeliveryState.UNKNOWN
        else:
            expected = DeliveryState.NO_ARTIFACT if no_artifact else DeliveryState.CONTAINED
            assert proof.request.legacy_generation == old_id
            assert proof.state == expected
            assert await db.archive_task("legacy")
            archived = (await observer.observe(["legacy"])).get("legacy")
            assert archived.request.completion_id == old_id
            assert archived.state == expected
        # Repeating upgrade must not replace a recovered or newly rotated identity.
        async with db._engine.begin() as conn:
            def repeat(conn):
                with Operations.context(MigrationContext.configure(conn)):
                    revision.upgrade()
            await conn.run_sync(repeat)
        assert (await observer.observe(["legacy"])).get("legacy") == (
            archived if not reopened else proof
        )








# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Create a real in-memory database for tests."""
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url=lease_dsn("test.db")),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
def mock_git():
    """Autospec'd GitManager with sensible defaults.

    ``create_autospec(GitManager, instance=True)`` (F2) makes every
    configured method track the real ``GitManager`` surface — renaming a
    method in production now fails these tests instead of leaving them
    green against a test-defined mock surface.
    """
    from unittest.mock import create_autospec

    git = create_autospec(GitManager, instance=True)
    # Sync defaults (kept for backward compat / sync-path tests)
    git.validate_checkout.return_value = True
    git.get_current_branch.return_value = "main"
    git.get_recent_commits.return_value = "abc1234 Initial commit"
    git.get_diff.return_value = "diff --git a/file.py b/file.py"
    git._run.return_value = ""
    git.create_branch.return_value = None
    git.checkout_branch.return_value = None
    git.commit_all.return_value = True
    git.push_branch.return_value = None
    git.merge_branch.return_value = True
    git.slugify.side_effect = GitManager.slugify
    # Async defaults — command handler now uses the async API
    git.avalidate_checkout.return_value = True
    git.aget_current_branch.return_value = "main"
    git.aget_recent_commits.return_value = "abc1234 Initial commit"
    git.aget_diff.return_value = "diff --git a/file.py b/file.py"
    git._arun.return_value = ""
    git.acreate_branch.return_value = None
    git.acheckout_branch.return_value = None
    git.acommit_all.return_value = True
    git.apush_branch.return_value = None
    git.amerge_branch.return_value = True
    git.apull_branch.return_value = "main"
    git.aget_changed_files.return_value = []
    git.alist_branches.return_value = ["* main"]
    git.acreate_pr.return_value = "https://github.com/test/pr/1"
    git.acreate_github_repo.return_value = "https://github.com/user/repo"
    git.aget_status.return_value = ""
    git.aget_default_branch.return_value = "main"
    return git


@pytest.fixture
async def handler(db, config, mock_git, internal_plugins_handler):
    """CommandHandler with a mocked GitManager and internal plugins loaded
    (built by the shared ``internal_plugins_handler`` factory — FU-13)."""
    return await internal_plugins_handler(db=db, config=config, git=mock_git)


@pytest.fixture
async def project_with_repo(db, tmp_path):
    """Create a test project and linked repo in the database.

    Returns (project_id, repo_id, checkout_path).
    """
    project_id = "test-proj"
    repo_id = "test-repo"
    checkout_path = str(tmp_path / "workspaces" / "test-proj")

    # Create the directory so _resolve_repo_path doesn't fail the isdir check
    import os

    os.makedirs(checkout_path, exist_ok=True)

    await db.create_project(
        Project(
            id=project_id,
            name="Test Project",
        )
    )
    await db.create_repo(
        RepoConfig(
            id=repo_id,
            project_id=project_id,
            source_type=RepoSourceType.LINK,
            source_path=checkout_path,
            default_branch="main",
        )
    )
    return project_id, repo_id, checkout_path


# ---------------------------------------------------------------------------
# test_create_branch
# ---------------------------------------------------------------------------


class TestCreateBranch:
    """Tests for _cmd_create_branch."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "create_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/new-thing",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/new-thing"
        assert result["status"] == "created"
        mock_git.acreate_branch.assert_called_once_with(
            checkout_path,
            "feature/new-thing",
        )

    async def test_missing_branch_name(self, handler, project_with_repo):
        project_id, repo_id, _ = project_with_repo

        result = await handler.execute(
            "create_branch",
            {
                "project_id": project_id,
            },
        )

        assert result == {"error": "branch_name is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "create_branch",
            {
                "project_id": "nonexistent",
                "branch_name": "feature/x",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.acreate_branch.side_effect = GitError("branch already exists")

        result = await handler.execute(
            "create_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/dup",
            },
        )

        assert "error" in result
        assert "branch already exists" in result["error"]


# ---------------------------------------------------------------------------
# test_checkout_branch
# ---------------------------------------------------------------------------


class TestCheckoutBranch:
    """Tests for _cmd_checkout_branch."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "checkout_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/existing",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/existing"
        assert result["status"] == "checked_out"
        mock_git.acheckout_branch.assert_called_once_with(
            checkout_path,
            "feature/existing",
        )

    async def test_branch_not_found(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.acheckout_branch.side_effect = GitError(
            "error: pathspec 'no-such-branch' did not match any file(s) known to git"
        )

        result = await handler.execute(
            "checkout_branch",
            {
                "project_id": project_id,
                "branch_name": "no-such-branch",
            },
        )

        assert "error" in result
        assert "no-such-branch" in result["error"]

    async def test_missing_branch_name(self, handler, project_with_repo):
        project_id, _, _ = project_with_repo

        result = await handler.execute(
            "checkout_branch",
            {
                "project_id": project_id,
            },
        )

        assert result == {"error": "branch_name is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "checkout_branch",
            {
                "project_id": "nonexistent",
                "branch_name": "main",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_warns_if_tasks_in_progress(self, handler, db, mock_git, project_with_repo):
        """When tasks are IN_PROGRESS, result should include a warning."""
        project_id, _, _ = project_with_repo

        # Create an IN_PROGRESS task for this project
        from src.models import Task

        task = Task(
            id="task-1",
            project_id=project_id,
            title="Running task",
            description="A task that is running",
            status=TaskStatus.IN_PROGRESS,
        )
        await db.create_task(task)

        result = await handler.execute(
            "checkout_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/switch",
            },
        )

        assert "error" not in result
        assert result["status"] == "checked_out"
        assert "warning" in result
        assert "IN_PROGRESS" in result["warning"]


# ---------------------------------------------------------------------------
# test_commit_changes
# ---------------------------------------------------------------------------


class TestCommitChanges:
    """Tests for _cmd_commit_changes."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.acommit_all.return_value = True

        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
                "message": "feat: add new feature",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["commit_message"] == "feat: add new feature"
        assert result["status"] == "committed"
        mock_git.acommit_all.assert_called_once()
        call_args = mock_git.acommit_all.call_args
        assert call_args[0] == (checkout_path, "feat: add new feature")

    async def test_nothing_to_commit(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.acommit_all.return_value = False  # nothing to commit

        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
                "message": "empty commit",
            },
        )

        assert "error" not in result
        assert result["status"] == "nothing_to_commit"
        assert result["message"] == "No eligible changes to commit"

    async def test_missing_message(self, handler, project_with_repo):
        project_id, _, _ = project_with_repo

        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
            },
        )

        assert result == {"error": "message is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "commit_changes",
            {
                "project_id": "nonexistent",
                "message": "some message",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.acommit_all.side_effect = GitError("failed to commit")

        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
                "message": "a commit",
            },
        )

        assert "error" in result
        assert "failed to commit" in result["error"]

    async def test_warns_if_tasks_in_progress(self, handler, db, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.acommit_all.return_value = True

        from src.models import Task

        task = Task(
            id="task-running",
            project_id=project_id,
            title="Active task",
            description="Running",
            status=TaskStatus.IN_PROGRESS,
        )
        await db.create_task(task)

        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
                "message": "fix: something",
            },
        )

        assert "error" not in result
        assert result["status"] == "committed"
        assert "warning" in result
        assert "IN_PROGRESS" in result["warning"]


# ---------------------------------------------------------------------------
# test_push_branch
# ---------------------------------------------------------------------------


class TestPushBranch:
    """Tests for _cmd_push_branch."""

    async def test_success_with_explicit_branch(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "push_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/push-me",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/push-me"
        assert result["status"] == "pushed"
        mock_git.apush_branch.assert_called_once()
        call_args = mock_git.apush_branch.call_args
        assert call_args[0] == (checkout_path, "feature/push-me")

    async def test_success_with_current_branch(self, handler, mock_git, project_with_repo):
        """When branch_name is not provided, uses current branch."""
        project_id, _, checkout_path = project_with_repo
        mock_git.aget_current_branch.return_value = "feature/auto-detect"

        result = await handler.execute(
            "push_branch",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["branch"] == "feature/auto-detect"
        assert result["status"] == "pushed"
        mock_git.aget_current_branch.assert_called_with(checkout_path)
        mock_git.apush_branch.assert_called_once()
        call_args = mock_git.apush_branch.call_args
        assert call_args[0] == (checkout_path, "feature/auto-detect")

    async def test_push_failure(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.apush_branch.side_effect = GitError("git push origin feature/x failed: rejected")

        result = await handler.execute(
            "push_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/x",
            },
        )

        assert "error" in result
        assert "rejected" in result["error"]

    async def test_cannot_determine_current_branch(self, handler, mock_git, project_with_repo):
        """When no branch_name given and current branch can't be determined."""
        project_id, _, _ = project_with_repo
        mock_git.aget_current_branch.return_value = ""

        result = await handler.execute(
            "push_branch",
            {
                "project_id": project_id,
            },
        )

        assert "error" in result
        assert "Could not determine current branch" in result["error"]

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "push_branch",
            {
                "project_id": "nonexistent",
                "branch_name": "main",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# test_merge_branch
# ---------------------------------------------------------------------------


class TestMergeBranch:
    """Tests for _cmd_merge_branch."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.amerge_branch.return_value = True

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/done",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/done"
        assert result["target"] == "main"
        assert result["status"] == "merged"
        mock_git.amerge_branch.assert_called_once_with(
            checkout_path,
            "feature/done",
            "main",
            repository_url="",
        )

    async def test_conflict_scenario(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git.amerge_branch.return_value = False  # conflict

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/conflicting",
            },
        )

        assert "error" not in result  # conflict is not an error, just a status
        assert result["status"] == "conflict"
        assert "conflict" in result["message"].lower()
        assert result["branch"] == "feature/conflicting"
        assert result["target"] == "main"

    async def test_missing_branch_name(self, handler, project_with_repo):
        project_id, _, _ = project_with_repo

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
            },
        )

        assert result == {"error": "branch_name is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "merge_branch",
            {
                "project_id": "nonexistent",
                "branch_name": "feature/x",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.amerge_branch.side_effect = GitError("fatal: not a git repo")

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/bad",
            },
        )

        assert "error" in result
        assert "not a git repo" in result["error"]

    async def test_uses_repo_default_branch(self, handler, db, mock_git, tmp_path):
        """Merge should use the project's configured default branch, not hardcoded 'main'."""
        import os
        from src.models import Workspace

        project_id = "proj-develop"
        checkout_path = str(tmp_path / "workspaces" / "proj-develop")
        os.makedirs(checkout_path, exist_ok=True)

        await db.create_project(
            Project(
                id=project_id,
                name="Develop Project",
                repo_default_branch="develop",
            )
        )
        await db.create_workspace(
            Workspace(
                id="ws-develop",
                project_id=project_id,
                workspace_path=checkout_path,
                source_type=RepoSourceType.LINK,
            )
        )

        mock_git.amerge_branch.return_value = True

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/custom-default",
            },
        )

        assert "error" not in result
        assert result["target"] == "develop"
        mock_git.amerge_branch.assert_called_once_with(
            checkout_path,
            "feature/custom-default",
            "develop",
            repository_url="",
        )

    async def test_warns_if_tasks_in_progress(self, handler, db, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.amerge_branch.return_value = True

        from src.models import Task

        await db.create_task(
            Task(
                id="task-active",
                project_id=project_id,
                title="Active",
                description="Running",
                status=TaskStatus.IN_PROGRESS,
            )
        )

        result = await handler.execute(
            "merge_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/merge-warn",
            },
        )

        assert "error" not in result
        assert result["status"] == "merged"
        assert "warning" in result
        assert "IN_PROGRESS" in result["warning"]


# ---------------------------------------------------------------------------
# test_git_log
# ---------------------------------------------------------------------------


class TestGitLog:
    """Tests for _cmd_git_log."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_recent_commits.return_value = (
            "abc1234 feat: add feature\ndef5678 fix: bug fix"
        )
        mock_git.aget_current_branch.return_value = "feature/test"

        result = await handler.execute(
            "git_log",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/test"
        assert "abc1234" in result["log"]
        assert "def5678" in result["log"]
        # Default count is 10
        mock_git.aget_recent_commits.assert_called_once_with(
            checkout_path,
            count=10,
        )

    async def test_custom_count(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git.aget_recent_commits.return_value = "abc1234 commit"

        result = await handler.execute(
            "git_log",
            {
                "project_id": project_id,
                "count": 3,
            },
        )

        assert "error" not in result
        mock_git.aget_recent_commits.assert_called_once_with(
            checkout_path,
            count=3,
        )

    async def test_empty_log(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.aget_recent_commits.return_value = ""

        result = await handler.execute(
            "git_log",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["log"] == "(no commits)"

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "git_log",
            {
                "project_id": "nonexistent",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# test_git_diff
# ---------------------------------------------------------------------------


class TestGitDiff:
    """Tests for _cmd_git_diff."""

    async def test_working_tree_diff(self, handler, mock_git, project_with_repo):
        """Without base_branch, shows working tree diff (unstaged changes)."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git._arun.return_value = "diff --git a/file.py b/file.py\n+new line"

        result = await handler.execute(
            "git_diff",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["base_branch"] == "(working tree)"
        assert "new line" in result["diff"]
        mock_git._arun.assert_called_once_with(["diff"], cwd=checkout_path)

    async def test_diff_against_base_branch(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git.aget_diff.return_value = "diff --git a/src/app.py b/src/app.py\n+import new_module"

        result = await handler.execute(
            "git_diff",
            {
                "project_id": project_id,
                "base_branch": "main",
            },
        )

        assert "error" not in result
        assert result["base_branch"] == "main"
        assert "new_module" in result["diff"]
        mock_git.aget_diff.assert_called_once_with(checkout_path, "main")

    async def test_no_changes(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git._arun.return_value = ""

        result = await handler.execute(
            "git_diff",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["diff"] == "(no changes)"

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git._arun.side_effect = GitError("fatal: bad revision")

        result = await handler.execute(
            "git_diff",
            {
                "project_id": project_id,
            },
        )

        assert "error" in result
        assert "bad revision" in result["error"]

    async def test_invalid_project(self, handler):
        result = await handler.execute(
            "git_diff",
            {
                "project_id": "nonexistent",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_diff_against_base_branch_git_error(self, handler, mock_git, project_with_repo):
        """GitError when diffing against a base branch."""
        project_id, _, _ = project_with_repo
        mock_git.aget_diff.side_effect = GitError("fatal: bad object 'develop'")

        result = await handler.execute(
            "git_diff",
            {
                "project_id": project_id,
                "base_branch": "develop",
            },
        )

        assert "error" in result
        assert "bad object" in result["error"]


# ---------------------------------------------------------------------------
# test_resolve_repo_path edge cases
# ---------------------------------------------------------------------------


class TestResolveRepoPath:
    """Edge cases for _resolve_repo_path used by all commands."""

    async def test_missing_project_id_no_active(self, handler):
        """Commands without project_id and no active project should error."""
        result = await handler.execute(
            "create_branch",
            {
                "branch_name": "feature/orphan",
            },
        )

        assert "error" in result
        assert "project_id" in result["error"].lower() or "required" in result["error"].lower()

    async def test_with_workspace_param(self, handler, db, mock_git, project_with_repo, tmp_path):
        """Commands should work when project_id is explicitly specified."""
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "create_branch",
            {
                "project_id": project_id,
                "branch_name": "feature/via-repo-id",
            },
        )

        assert "error" not in result
        assert result["status"] == "created"
        mock_git.acreate_branch.assert_called_once_with(
            checkout_path,
            "feature/via-repo-id",
        )


# ---------------------------------------------------------------------------
# test_active_project_fallback
# ---------------------------------------------------------------------------


class TestActiveProjectFallback:
    """Tests for active project inference in git commands."""

    async def test_create_branch_infers_active_project(self, handler, mock_git, project_with_repo):
        """create_branch should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        handler.set_active_project(project_id)

        result = await handler.execute(
            "create_branch",
            {
                "branch_name": "feature/auto-project",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/auto-project"
        assert result["status"] == "created"
        mock_git.acreate_branch.assert_called_once_with(
            checkout_path,
            "feature/auto-project",
        )

    async def test_commit_changes_infers_active_project(self, handler, mock_git, project_with_repo):
        """commit_changes should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.acommit_all.return_value = True
        handler.set_active_project(project_id)

        result = await handler.execute(
            "commit_changes",
            {
                "message": "feat: auto-inferred project",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["status"] == "committed"
        mock_git.acommit_all.assert_called_once()
        call_args = mock_git.acommit_all.call_args
        assert call_args[0] == (checkout_path, "feat: auto-inferred project")

    async def test_push_branch_infers_active_project(self, handler, mock_git, project_with_repo):
        """push_branch should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_current_branch.return_value = "feature/auto"
        handler.set_active_project(project_id)

        result = await handler.execute("push_branch", {})

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["status"] == "pushed"
        mock_git.apush_branch.assert_called_once()
        call_args = mock_git.apush_branch.call_args
        assert call_args[0] == (checkout_path, "feature/auto")

    async def test_checkout_branch_infers_active_project(
        self, handler, mock_git, project_with_repo
    ):
        """checkout_branch should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        handler.set_active_project(project_id)

        result = await handler.execute(
            "checkout_branch",
            {
                "branch_name": "feature/existing",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["status"] == "checked_out"

    async def test_merge_branch_infers_active_project(self, handler, mock_git, project_with_repo):
        """merge_branch should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.amerge_branch.return_value = True
        handler.set_active_project(project_id)

        result = await handler.execute(
            "merge_branch",
            {
                "branch_name": "feature/auto-merge",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["status"] == "merged"

    async def test_git_log_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_log should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_recent_commits.return_value = "abc1234 test commit"
        handler.set_active_project(project_id)

        result = await handler.execute("git_log", {})

        assert "error" not in result
        assert result["project_id"] == project_id
        assert "abc1234" in result["log"]

    async def test_git_diff_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_diff should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git._arun.return_value = "diff output"
        handler.set_active_project(project_id)

        result = await handler.execute("git_diff", {})

        assert "error" not in result
        assert result["project_id"] == project_id

    async def test_git_commit_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_commit (old-style) should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.acommit_all.return_value = True
        handler.set_active_project(project_id)

        result = await handler.execute(
            "git_commit",
            {
                "message": "feat: inferred commit",
            },
        )

        assert "error" not in result
        assert result["committed"] is True
        assert result["project_id"] == project_id
        mock_git.acommit_all.assert_called_once()
        call_args = mock_git.acommit_all.call_args
        assert call_args[0] == (checkout_path, "feat: inferred commit")

    async def test_git_commit_reports_no_eligible_changes(
        self, handler, mock_git, project_with_repo
    ):
        project_id, _, _ = project_with_repo
        mock_git.acommit_all.return_value = False
        handler.set_active_project(project_id)

        result = await handler.execute("git_commit", {"message": "empty"})

        assert result == {
            "project_id": project_id,
            "committed": False,
            "message": "No eligible changes to commit",
        }

    async def test_git_pull_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_pull should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.apull_branch.return_value = "main"
        handler.set_active_project(project_id)

        result = await handler.execute("git_pull", {})

        assert "error" not in result
        assert result["pulled"] == "main"
        assert result["project_id"] == project_id
        mock_git.apull_branch.assert_called_once_with(
            checkout_path, None, repository_url=""
        )

    async def test_git_pull_with_branch(self, handler, mock_git, project_with_repo):
        """git_pull with explicit branch should pass it through."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.apull_branch.return_value = "feature/xyz"
        handler.set_active_project(project_id)

        result = await handler.execute("git_pull", {"branch": "feature/xyz"})

        assert "error" not in result
        assert result["pulled"] == "feature/xyz"
        mock_git.apull_branch.assert_called_once_with(
            checkout_path, "feature/xyz", repository_url=""
        )

    async def test_git_pull_error(self, handler, mock_git, project_with_repo):
        """git_pull should return error when pull fails."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.apull_branch.side_effect = GitError("merge conflict")
        handler.set_active_project(project_id)

        result = await handler.execute("git_pull", {})

        assert "error" in result
        assert "merge conflict" in result["error"]

    async def test_git_push_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_push (old-style) should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_current_branch.return_value = "feature/auto"
        handler.set_active_project(project_id)

        result = await handler.execute("git_push", {})

        assert "error" not in result
        assert result["pushed"] == "feature/auto"
        assert result["project_id"] == project_id

    async def test_git_push_passes_explicit_lease(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        oid = "a" * 40
        result = await handler.execute(
            "git_push",
            {"project_id": project_id, "branch": "feature/leased", "expected_remote_oid": oid},
        )
        assert result["pushed"] == "feature/leased"
        mock_git.apush_branch.assert_awaited_once_with(
            checkout_path, "feature/leased",
            expected_remote_oid=oid,
            event_bus=handler._bus,
            project_id=project_id,
        )

    async def test_push_reports_the_exact_published_oid(self, handler, mock_git, project_with_repo):
        """The OID a later explicit lease names comes back from the push itself."""
        project_id, _, _ = project_with_repo
        tip = "c" * 40
        mock_git.apush_branch.return_value = tip

        pushed = await handler.execute(
            "git_push", {"project_id": project_id, "branch": "feature/x"}
        )
        aliased = await handler.execute(
            "push_branch", {"project_id": project_id, "branch_name": "feature/x"}
        )

        assert pushed == {"project_id": project_id, "pushed": "feature/x", "oid": tip}
        assert aliased == {
            "project_id": project_id, "branch": "feature/x", "status": "pushed", "oid": tip,
        }

    async def test_push_branch_alias_passes_explicit_lease(
        self, handler, mock_git, project_with_repo
    ):
        project_id, _, checkout_path = project_with_repo
        oid = "0" * 40
        await handler.execute(
            "push_branch",
            {"project_id": project_id, "branch_name": "feature/new", "expected_remote_oid": oid},
        )
        mock_git.apush_branch.assert_awaited_once_with(
            checkout_path, "feature/new",
            expected_remote_oid=oid,
            event_bus=handler._bus,
            project_id=project_id,
        )

    async def test_git_create_branch_infers_active_project(
        self, handler, mock_git, project_with_repo
    ):
        """git_create_branch (old-style) should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        handler.set_active_project(project_id)

        result = await handler.execute(
            "git_create_branch",
            {
                "branch_name": "feature/auto-branch",
            },
        )

        assert "error" not in result
        assert result["created_branch"] == "feature/auto-branch"
        assert result["project_id"] == project_id

    async def test_git_changed_files_infers_active_project(
        self, handler, mock_git, project_with_repo
    ):
        """git_changed_files (old-style) should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_changed_files.return_value = ["file1.py", "file2.py"]
        handler.set_active_project(project_id)

        result = await handler.execute("git_changed_files", {})

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["count"] == 2

    async def test_no_fallback_without_active_project(self, handler):
        """Without active project, commands should still fail gracefully."""
        handler.set_active_project(None)

        result = await handler.execute(
            "git_commit",
            {
                "message": "should fail",
            },
        )

        assert "error" in result
        assert (
            "project_id" in result["error"].lower() or "active project" in result["error"].lower()
        )

    async def test_get_git_status_infers_active_project(self, handler, mock_git, project_with_repo):
        """get_git_status should work without project_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.aget_status.return_value = "nothing to commit"
        mock_git.aget_current_branch.return_value = "main"
        mock_git.aget_recent_commits.return_value = "abc1234 commit"
        handler.set_active_project(project_id)

        result = await handler.execute("get_git_status", {})

        assert "error" not in result
        assert result["project_id"] == project_id
        assert len(result["repos"]) > 0

    async def test_get_git_status_no_fallback_without_active(self, handler):
        """get_git_status without project_id and no active project should error."""
        handler.set_active_project(None)

        result = await handler.execute("get_git_status", {})

        assert "error" in result
        assert (
            "project_id" in result["error"].lower() or "active project" in result["error"].lower()
        )

    async def test_git_merge_infers_active_project(self, handler, mock_git, project_with_repo):
        """git_merge (old-style) should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.amerge_branch.return_value = True
        handler.set_active_project(project_id)

        result = await handler.execute(
            "git_merge",
            {
                "branch_name": "feature/auto-merge",
            },
        )

        assert "error" not in result
        assert result["merged"] is True
        assert result["project_id"] == project_id

    async def test_git_create_pr_infers_active_project(self, handler, db, mock_git, project_with_repo):
        """git_create_pr should work without repo_id when active project is set."""
        project_id, repo_id, checkout_path = project_with_repo
        from src.git.github_contracts import GitHubRepositoryBinding

        await db.update_project(project_id, repo_url="https://github.com/test/repo")
        mock_git.bind_github_repository.return_value = GitHubRepositoryBinding(1, "test/repo")
        mock_git.aget_current_branch.return_value = "feature/pr-test"
        mock_git.acreate_pr.return_value = "https://github.com/test/repo/pull/1"
        handler.set_active_project(project_id)

        result = await handler.execute(
            "git_create_pr",
            {
                "title": "Test PR",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert "pr_url" in result

    async def test_explicit_project_id_overrides_active(
        self, handler, db, mock_git, project_with_repo, tmp_path
    ):
        """Explicit project_id should take precedence over active project."""
        project_id, repo_id, checkout_path = project_with_repo

        # Set active project to something else
        handler.set_active_project("some-other-project")

        # But explicitly pass the real project_id
        result = await handler.execute(
            "commit_changes",
            {
                "project_id": project_id,
                "message": "explicit project",
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["status"] == "committed"


# ---------------------------------------------------------------------------
# test_create_github_repo
# ---------------------------------------------------------------------------


class TestCreateGithubRepo:
    """Tests for _cmd_create_github_repo."""

    async def test_success(self, handler, mock_git):
        mock_git.acreate_github_repo.return_value = "https://github.com/user/my-app"

        result = await handler.execute(
            "create_github_repo",
            {
                "name": "my-app",
            },
        )

        assert "error" not in result
        assert result["created"] is True
        assert result["repo_url"] == "https://github.com/user/my-app"
        assert result["name"] == "my-app"
        mock_git.acreate_github_repo.assert_called_once_with(
            "my-app",
            private=True,
            org=None,
            description="",
        )

    async def test_success_with_options(self, handler, mock_git):
        mock_git.acreate_github_repo.return_value = "https://github.com/my-org/my-app"

        result = await handler.execute(
            "create_github_repo",
            {
                "name": "my-app",
                "private": False,
                "org": "my-org",
                "description": "A cool app",
            },
        )

        assert "error" not in result
        assert result["created"] is True
        assert result["repo_url"] == "https://github.com/my-org/my-app"
        mock_git.acreate_github_repo.assert_called_once_with(
            "my-app",
            private=False,
            org="my-org",
            description="A cool app",
        )

    async def test_missing_name(self, handler, mock_git):
        result = await handler.execute("create_github_repo", {})

        assert result == {"error": "name is required"}

    async def test_gh_not_authenticated(self, handler, mock_git):
        mock_git.acreate_github_repo.side_effect = GitHubAccessError(
            "credentials", "GitHub CLI is not authenticated"
        )

        result = await handler.execute(
            "create_github_repo",
            {
                "name": "my-app",
            },
        )

        assert "error" in result
        assert "not authenticated" in result["error"].lower()
        assert result["error_code"] == "credentials"

    async def test_app_mode_rejects_repository_creation(self, handler, mock_git):
        mock_git.acreate_github_repo.side_effect = GitHubAccessError(
            "github_operation_unsupported", "Repository creation is unavailable"
        )

        result = await handler.execute("create_github_repo", {"name": "my-app"})

        assert result == {
            "error": "Repository creation is unavailable",
            "error_code": "github_operation_unsupported",
        }

    async def test_git_error(self, handler, mock_git):
        mock_git.acreate_github_repo.side_effect = GitError(
            "gh repo create failed: Name already exists"
        )

        result = await handler.execute(
            "create_github_repo",
            {
                "name": "my-app",
            },
        )

        assert "error" in result
        assert "Name already exists" in result["error"]


# ---------------------------------------------------------------------------
# test_generate_readme
# ---------------------------------------------------------------------------


class TestGenerateReadme:
    """Tests for _cmd_generate_readme."""

    async def test_success_full(self, handler, mock_git, project_with_repo, tmp_path):
        """README generated with description and tech stack, committed and pushed."""
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
                "name": "My Awesome App",
                "description": "A web application for managing tasks.",
                "tech_stack": "Python, FastAPI, PostgreSQL",
            },
        )

        assert "error" not in result
        assert result["committed"] is True
        assert result["pushed"] is True
        assert result["status"] == "generated"

        import os

        readme_path = os.path.join(checkout_path, "README.md")
        assert os.path.isfile(readme_path)
        with open(readme_path) as f:
            content = f.read()
        assert "# My Awesome App" in content
        assert "A web application for managing tasks." in content
        assert "- Python" in content
        assert "- FastAPI" in content
        assert "- PostgreSQL" in content

        mock_git.acommit_all.assert_called_once()
        call_args = mock_git.acommit_all.call_args
        assert call_args[0] == (checkout_path, "Add generated README.md")
        mock_git.apush_branch.assert_called_once()

    async def test_success_minimal(self, handler, mock_git, project_with_repo):
        """README generated with only name, no description or tech stack."""
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
                "name": "Minimal Project",
            },
        )

        assert "error" not in result
        assert result["committed"] is True

        import os

        readme_path = os.path.join(checkout_path, "README.md")
        with open(readme_path) as f:
            content = f.read()
        assert "# Minimal Project" in content
        assert "## Tech Stack" not in content

    async def test_missing_name(self, handler, project_with_repo):
        """Error returned when name is missing."""
        project_id, _, _ = project_with_repo

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
            },
        )

        assert result == {"error": "name is required"}

    async def test_invalid_project(self, handler):
        """Error returned for nonexistent project."""
        result = await handler.execute(
            "generate_readme",
            {
                "project_id": "nonexistent",
                "name": "Test",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_commit_failure(self, handler, mock_git, project_with_repo):
        """Error returned when git commit fails."""
        project_id, _, checkout_path = project_with_repo
        mock_git.acommit_all.side_effect = GitError("commit failed")

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
                "name": "Test",
            },
        )

        assert "error" in result
        assert "commit failed" in result["error"]

    async def test_push_failure_non_fatal(self, handler, mock_git, project_with_repo):
        """Push failure is non-fatal — commit succeeds but pushed is False."""
        project_id, _, checkout_path = project_with_repo
        mock_git.apush_branch.side_effect = GitError("push failed")

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
                "name": "Test",
            },
        )

        assert "error" not in result
        assert result["committed"] is True
        assert result["pushed"] is False

    async def test_nothing_to_commit(self, handler, mock_git, project_with_repo):
        """When commit_all returns False, result reflects nothing committed."""
        project_id, _, checkout_path = project_with_repo
        mock_git.acommit_all.return_value = False

        result = await handler.execute(
            "generate_readme",
            {
                "project_id": project_id,
                "name": "Test",
            },
        )

        assert "error" not in result
        assert result["committed"] is False
        assert result["pushed"] is False
