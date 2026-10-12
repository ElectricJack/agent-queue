"""Remote Git delivery proofs with real histories and retained completion identities."""

from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError, GitManager
from src.integration.delivery_truth import DeliveryRequest, DeliveryState
from src.integration.git_truth import (
    GitTruth,
    SharedFetchCancelled,
    commits_added,
    epic_complete,
    repair_progress,
)
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.selection_metrics import SelectionMetrics, selection_metrics_scope


@dataclass
class Repository:
    git: GitManager
    path: Path
    remote: Path
    base: str
    truth: GitTruth

    async def run(self, *args):
        return await self.git._arun(list(args), cwd=str(self.path))

    async def commit(self, filename, content="work\n", *, message="work"):
        (self.path / filename).write_text(content)
        await self.run("add", "--", filename)
        await self.run("commit", "-m", message)
        return await self.run("rev-parse", "HEAD")

    async def retain(self, source, *, task="task", completion="close-1", artifact=True):
        identity = CompletionIdentity("p", "r", task, completion)
        await GitProvenance(self.git, str(self.path), repository_url=str(self.remote)).write_completion(
            CompletedSource(identity, source), artifact=artifact,
        )
        return DeliveryRequest(
            "p", "r", "refs/heads/main", task, 1.0, "legacy-1",
            branch_name="source", completion_id=completion, claim_epoch=1,
            has_recorded_source=True,
        )

    async def publish(self):
        await self.run("push", "origin", "HEAD:refs/heads/main")

    async def snapshot(self, *, target="refs/heads/main", truth=None):
        return await (truth or self.truth).snapshot(
            str(self.path), project_id="p", repository_id="r",
            repository_url=str(self.remote), target_ref=target,
        )


@pytest.fixture
async def repository(tmp_path):
    git = GitManager()
    remote, path = tmp_path / "remote.git", tmp_path / "checkout"
    await git._arun(["init", "--bare", str(remote)], cwd=str(tmp_path))
    await git._arun(["init", "-b", "main", str(path)], cwd=str(tmp_path))
    for key, value in (("user.name", "Tester"), ("user.email", "test@example.com"),
                       ("commit.gpgsign", "false")):
        await git._arun(["config", key, value], cwd=str(path))
    repo = Repository(git, path, remote, "", GitTruth(git))
    await repo.run("remote", "add", "origin", str(remote))
    repo.base = await repo.commit("seed", "seed\n", message="initial")
    await repo.publish()
    await repo.run("checkout", "-b", "source")
    return repo


async def source_work(repo):
    first = await repo.commit("one")
    final = await repo.commit("two")
    request = await repo.retain(final)
    return first, final, request


CATALOGUE = "tests/selection_catalogue.json"


async def generated_base(repo, path=CATALOGUE, *, declared=True):
    await repo.run("checkout", "main")
    await repo.commit(".gitattributes", (
        f"{CATALOGUE} merge=aq-generated\npackages/aq-client/** merge=aq-generated\n"
        if declared else ""
    ))
    (repo.path / path).parent.mkdir(parents=True, exist_ok=True)
    repo.base = await repo.commit(path, "base\n")
    await repo.publish()
    await repo.run("checkout", "-B", "source")


@pytest.mark.parametrize("path", [CATALOGUE, "packages/aq-client/nested/odd :*\t\n.bin"])
async def test_whole_source_patch_ignores_regenerated_artifacts(repository, path):
    repo = repository
    await generated_base(repo, path)
    await repo.commit("one")
    head = await repo.commit(path, "source-generated\n")
    request = await repo.retain(head)
    await repo.run("checkout", "main")
    await repo.commit("one", message="delivered source under another commit")
    await repo.commit(path, "target-generated\n")
    await repo.publish()
    snapshot = await repo.snapshot()
    assert await repo.git.apatch_id(str(repo.path), repo.base, head) != (
        await repo.git.apatch_id(str(repo.path), repo.base, snapshot.target_oid)
    )
    # A checkout's attributes cannot change the immutable target's proof.
    (repo.path / ".gitattributes").write_text("")
    proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert (proof.state, proof.reason) == (DeliveryState.CONTAINED, "whole_source_patch")
    assert await snapshot.contains_source("task", head, repo.base) is True


@pytest.mark.parametrize("complete", [False, True])
async def test_cherry_picked_multi_commit_source_ignores_generated_history(
    repository, monkeypatch, complete,
):
    from src.integration import git_truth

    repo = repository
    await generated_base(repo)
    first = await repo.commit("one")
    await repo.commit(CATALOGUE, "source-generated\n")
    head = await repo.commit("two")
    request = await repo.retain(head)
    await repo.run("checkout", "main")
    await repo.commit("unrelated")
    for index in range(8):
        await repo.commit(CATALOGUE, f"target-generated {index}\n")
    await repo.run("cherry-pick", first)
    if complete:
        await repo.run("cherry-pick", head)
    await repo.publish()
    # Generated-only history must not spend the bounded patch search budget.
    monkeypatch.setattr(git_truth, "HISTORICAL_PATCH_PROBE_LIMIT", 5)
    snapshot = await repo.snapshot()
    proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert (proof.state, proof.reason) == (
        (DeliveryState.CONTAINED, "whole_source_patch") if complete else
        (DeliveryState.PENDING, "source_not_delivered")
    )
    assert await snapshot.contains_source("task", head, repo.base) is complete


async def test_historical_patch_ignores_generated_paths_deleted_from_target(repository):
    repo = repository
    await generated_base(repo)
    await repo.commit("one")
    head = await repo.commit(CATALOGUE, "source-generated\n")
    request = await repo.retain(head)
    await repo.run("checkout", "main")
    extra = "packages/aq-client/obsolete.py"
    (repo.path / extra).parent.mkdir(parents=True)
    (repo.path / extra).write_text("target-only generated output\n")
    await repo.run("add", "--", extra)
    await repo.commit("one", message="delivered source with other generated output")
    await repo.run("rm", "--", "one", extra)
    await repo.run("commit", "-m", "later revert and generated cleanup")
    await repo.publish()
    snapshot = await repo.snapshot()
    assert (await snapshot.is_delivered(request, source_base=repo.base)).reason == (
        "whole_source_patch")
    assert await snapshot.contains_source("task", head, repo.base) is True


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("code", ["delivered", "missing", "conflict"])
async def test_merge_noop_ignores_only_declared_generated_conflicts(repository, declared, code):
    repo = repository
    await generated_base(repo, declared=declared)
    await repo.commit("one", "source\n")
    head = await repo.commit(CATALOGUE, "source-generated\n")
    request = await repo.retain(head)
    await repo.run("checkout", "main")
    if code != "missing":
        await repo.commit("one", "source\n" if code == "delivered" else "conflict\n",
                          message="independent delivery")
    await repo.commit(CATALOGUE, "target-generated\n")
    await repo.publish()
    snapshot = await repo.snapshot()
    # No source base supplied: full-tree equality fails on the generated delta.
    proof = await snapshot.is_delivered(request)
    contained = declared and code == "delivered"
    assert (proof.state, proof.reason) == (
        (DeliveryState.CONTAINED, "merge_noop") if contained else
        (DeliveryState.PENDING, "source_not_delivered")
    )
    assert await snapshot.contains_source("task", head, repo.base) is contained


async def test_merge_noop_ignores_clean_generated_delta(repository):
    repo = repository
    await generated_base(repo)
    await repo.commit("one")
    head = await repo.commit(CATALOGUE, "source-generated\n")
    request = await repo.retain(head)
    await repo.run("checkout", "main")
    await repo.commit("one", message="independent delivery")
    await repo.publish()
    assert (await (await repo.snapshot()).is_delivered(request)).reason == "merge_noop"


async def test_nested_attribute_override_keeps_a_difference_owed(repository):
    repo = repository
    await generated_base(repo)
    await repo.run("checkout", "main")
    repo.base = await repo.commit("tests/.gitattributes", "selection_catalogue.json -merge\n")
    await repo.publish()
    await repo.run("checkout", "-B", "source")
    await repo.commit("one")
    head = await repo.commit(CATALOGUE, "source\n")
    request = await repo.retain(head)
    await repo.run("checkout", "main")
    await repo.commit("one", message="independent delivery")
    await repo.commit(CATALOGUE, "target\n")
    await repo.publish()
    snapshot = await repo.snapshot()
    for source_base in (None, repo.base):
        assert (await snapshot.is_delivered(request, source_base=source_base)).state == (
            DeliveryState.PENDING)
    assert await snapshot.contains_source("task", head, repo.base) is False


async def test_generated_attribute_lookup_failure_never_proves_delivery(repository, monkeypatch):
    repo = repository
    await generated_base(repo)
    await repo.commit("one")
    head = await repo.commit(CATALOGUE, "source-generated\n")
    request = await repo.retain(head)
    await repo.run("checkout", "main")
    await repo.commit("one", message="independent delivery")
    await repo.publish()
    snapshot = await repo.snapshot()
    run = repo.git.arun_git_result

    async def failing(args, **kwargs):
        if "check-attr" in args:
            raise GitError("attribute lookup failed")
        return await run(args, **kwargs)

    monkeypatch.setattr(repo.git, "arun_git_result", failing)
    for source_base in (None, repo.base):
        proof = await snapshot.is_delivered(request, source_base=source_base)
        assert proof.state == DeliveryState.UNKNOWN
        assert "GitError" in proof.error_detail
    assert await snapshot.contains_source("task", head, repo.base) is None


async def test_exact_ancestor_and_revert_remain_delivered(repository):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    snapshot = await repo.snapshot()
    proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert proof.state == DeliveryState.CONTAINED and proof.reason == "ancestor"
    await repo.run("revert", "--no-edit", head)
    await repo.publish()
    assert (await (await repo.snapshot()).is_delivered(request)).reason == "ancestor"


async def test_whole_multi_commit_squash_and_its_revert(repository):
    repo = repository
    _first, head, request = await source_work(repo)
    await repo.run("checkout", "main")
    await repo.commit("unrelated")
    await repo.run("merge", "--squash", "source")
    await repo.run("commit", "-m", "squash without a source trailer")
    squashed = await repo.run("rev-parse", "HEAD")
    await repo.publish()
    snapshot = await repo.snapshot()
    assert not await repo.git.ais_ancestor(str(repo.path), head, snapshot.target_oid)
    proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert proof.state == DeliveryState.CONTAINED and proof.reason == "whole_source_patch"
    await repo.run("revert", "--no-edit", squashed)
    await repo.publish()
    assert (await (await repo.snapshot()).is_delivered(request, source_base=repo.base)).reason == (
        "whole_source_patch"
    )


async def test_rebased_multi_commit_change_is_proved_as_a_whole(repository):
    repo = repository
    _first, head, request = await source_work(repo)
    await repo.run("checkout", "main")
    await repo.run("commit", "--allow-empty", "-m", "different ancestry, same base tree")
    await repo.run("checkout", "source")
    await repo.run("rebase", "main")
    assert await repo.run("rev-parse", "HEAD") != head
    await repo.publish()
    proof = await (await repo.snapshot()).is_delivered(request, source_base=repo.base)
    assert proof.reason == "whole_source_patch"


async def test_full_tree_unchanged_rebase_and_historical_equality(repository):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.run("checkout", "main")
    await repo.run("commit", "--allow-empty", "-m", "new parent")
    await repo.run("checkout", "source")
    await repo.run("rebase", "main")
    assert await repo.run("rev-parse", "HEAD") != head
    await repo.commit("unrelated")
    await repo.publish()
    # No trustworthy source base was supplied: full-tree equality still proves it.
    assert (await (await repo.snapshot()).is_delivered(request)).reason == "full_tree"


async def test_complete_rebased_range_after_unrelated_target_work(repository):
    repo = repository
    _first, head, request = await source_work(repo)
    await repo.run("checkout", "main")
    await repo.commit("unrelated")
    await repo.run("checkout", "source")
    await repo.run("rebase", "main")
    assert await repo.run("rev-parse", "HEAD") != head
    await repo.publish()
    proof = await (await repo.snapshot()).is_delivered(request, source_base=repo.base)
    assert proof.state == DeliveryState.CONTAINED and proof.reason == "whole_source_patch"


async def test_source_base_is_part_of_patch_fact_and_consumption_identity(repository):
    repo = repository
    first, head, request = await source_work(repo)
    await repo.run("checkout", "main")
    await repo.commit("unrelated")
    await repo.run("cherry-pick", head)
    await repo.publish()
    snapshot = await repo.snapshot()
    narrow = await snapshot.is_delivered(request, source_base=first)
    assert narrow.state == DeliveryState.CONTAINED
    assert (await snapshot.is_delivered(request, source_base=repo.base)).state == DeliveryState.PENDING
    assert not await snapshot.usable(narrow, request, current_source_base=repo.base)


async def test_partial_multi_commit_patch_match_is_pending(repository):
    repo = repository
    first, _head, request = await source_work(repo)
    await repo.run("checkout", "main")
    await repo.commit("unrelated")
    await repo.run("cherry-pick", first)
    await repo.publish()
    proof = await (await repo.snapshot()).is_delivered(request, source_base=repo.base)
    assert proof.state == DeliveryState.PENDING


@pytest.mark.parametrize("suffix", ["", "-extra"])
async def test_exact_reachable_trailer_only(repository, suffix):
    repo = repository
    _first, head, request = await source_work(repo)
    await repo.run("checkout", "main")
    # The writer owns complete application; this fixture isolates the reader.
    await repo.commit("unrelated", message=f"integration\n\nAQ-Source: task@{head}{suffix}")
    await repo.publish()
    proof = await (await repo.snapshot()).is_delivered(request, source_base=repo.base)
    assert proof.state == (DeliveryState.CONTAINED if not suffix else DeliveryState.PENDING)
    if not suffix:
        assert proof.reason == "source_trailer"


@pytest.mark.parametrize("message", ["quoted AQ-Source: task@{head}",
                                       "AQ-Source: task@{head}\n\nordinary body follows",
                                       "integration\n\nAQ-Source: other-task@{head}",
                                       "integration\n\nAQ-Source: task@{short}"])
async def test_message_substrings_are_not_source_trailers(repository, message):
    repo = repository
    _first, head, request = await source_work(repo)
    await repo.run("checkout", "main")
    await repo.commit("unrelated", message=message.format(head=head, short=head[:12]))
    await repo.publish()
    assert (await (await repo.snapshot()).is_delivered(request, source_base=repo.base)).state == (
        DeliveryState.PENDING
    )


async def test_unreachable_trailer_cannot_prove_delivery(repository):
    repo = repository
    _first, head, request = await source_work(repo)
    await repo.run("commit", "--allow-empty", "-m", f"marker\n\nAQ-Source: task@{head}")
    await repo.run("push", "origin", "HEAD:refs/heads/unreachable")
    assert (await (await repo.snapshot()).is_delivered(request, source_base=repo.base)).state == (
        DeliveryState.PENDING
    )


async def test_reopen_uses_new_completion_and_old_trailer_is_insufficient(repository):
    repo = repository
    _first, old_head, old_request = await source_work(repo)
    await repo.run("checkout", "main")
    await repo.commit("unrelated", message=f"integration\n\nAQ-Source: task@{old_head}")
    await repo.publish()
    assert (await (await repo.snapshot()).is_delivered(old_request)).state == DeliveryState.CONTAINED
    await repo.run("checkout", "source")
    head = await repo.commit("new-work")
    new_request = await repo.retain(head, completion="close-2")
    snapshot = await repo.snapshot()
    assert (await snapshot.is_delivered(new_request, source_base=repo.base)).state == (
        DeliveryState.PENDING
    )
    assert not await snapshot.usable(await snapshot.is_delivered(old_request), new_request)


async def test_missing_source_is_unknown_even_when_branchless(repository):
    repo = repository
    request = DeliveryRequest("p", "r", "refs/heads/main", "missing", 1, "legacy")
    proof = await (await repo.snapshot()).is_delivered(request)
    assert proof.state == DeliveryState.UNKNOWN and proof.reason == "missing_git_provenance"


async def test_explicit_retained_no_artifact_is_distinct_from_missing_source(repository):
    repo = repository
    request = await repo.retain(repo.base, artifact=False)
    assert (await (await repo.snapshot()).is_delivered(request)).state == DeliveryState.NO_ARTIFACT


async def test_passing_empty_completion_at_its_contained_base_is_no_change(repository):
    repo = repository
    request = replace(await repo.retain(repo.base), completion_outcome="pass", completion_commits=())
    # The proof binds the retained generation, even after its branch moves and
    # the default branch advances beyond the recorded base.
    await repo.commit("later-source-work")
    await repo.run("checkout", "main")
    await repo.commit("later-default-work")
    await repo.publish()
    snapshot = await repo.snapshot()
    proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert (proof.state, proof.reason, proof.source_oid) == (
        DeliveryState.NO_CHANGE, "git_no_change", repo.base,
    )
    assert proof.satisfied
    assert await snapshot.usable(proof, request, current_source_base=repo.base)
    assert not await snapshot.usable(proof, replace(request, completion_outcome="fail"),
                                     current_source_base=repo.base)
    assert await epic_complete(snapshot, [request], green_oid=snapshot.target_oid,
                               source_bases={"task": repo.base})


@pytest.mark.parametrize("scenario", ["missing", "failed", "changed", "missing_base", "off_default"])
async def test_empty_completion_requires_unambiguous_head_base_and_pass(repository, scenario):
    repo = repository
    source = repo.base
    if scenario in {"changed", "off_default"}:
        source = await repo.commit("undelivered")
    request = replace(await repo.retain(source), completion_outcome="pass", completion_commits=())
    base = source if scenario == "off_default" else repo.base
    if scenario == "missing":
        request = replace(request, completion_id="unretained")
    elif scenario == "failed":
        request = replace(request, completion_outcome="fail")
    elif scenario == "missing_base":
        base = None
    proof = await (await repo.snapshot()).is_delivered(request, source_base=base)
    assert proof.state != DeliveryState.NO_CHANGE
    assert (proof.state, proof.reason) == (
        (DeliveryState.PENDING, "no_change_base_not_delivered") if scenario == "off_default" else
        (DeliveryState.UNKNOWN, "missing_git_provenance") if scenario == "missing" else
        (DeliveryState.PENDING, "source_not_delivered") if scenario == "changed" else
        (DeliveryState.CONTAINED, "ancestor")
    )
    assert proof.satisfied is (scenario in {"failed", "missing_base"})


@pytest.mark.parametrize("contained", [False, True])
async def test_empty_commit_list_preserves_exact_source_containment(repository, contained):
    repo = repository
    head = await repo.commit("real-work")
    request = replace(await repo.retain(head), completion_outcome="pass", completion_commits=())
    if contained:
        await repo.publish()
    snapshot = await repo.snapshot()
    proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert (proof.state, proof.reason) == (
        (DeliveryState.CONTAINED, "ancestor") if contained else
        (DeliveryState.PENDING, "source_not_delivered")
    )
    assert proof.satisfied is contained
    assert await epic_complete(snapshot, [request], green_oid=snapshot.target_oid,
                               source_bases={"task": repo.base}) is contained


@pytest.mark.parametrize("operation,step,exception", [
    ("read_completion", "completion_provenance", KeyError("private provenance content")),
    ("ancestor", "source_ancestry", GitError("fatal: bad object private content")),
    ("alog_grep_trailer", "source_trailer", TypeError("https://user:secret@example.test")),
    ("_whole_patch", "whole_source_patch",
     GitError("git command stdin exceeds the bounded input limit")),
    ("atree_sha", "full_tree", OSError("/private/path: private file content")),
])
async def test_failed_proof_names_step_and_exception_without_untrusted_content(
    repository, monkeypatch, caplog, operation, step, exception,
):
    repo = repository
    request = await repo.retain(await repo.commit("one"))
    snapshot = await repo.snapshot()
    if operation in {"read_completion", "ancestor"}:
        owner = GitProvenance
    elif operation == "_whole_patch":
        from src.integration import git_truth

        owner = git_truth
    else:
        owner = repo.git
    monkeypatch.setattr(owner, operation, AsyncMock(side_effect=exception))
    proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert proof.state == DeliveryState.UNKNOWN
    assert proof.reason == "missing_or_ambiguous_source"
    assert proof.error_detail.startswith(f"{step}: {type(exception).__name__}:")
    assert proof.error_detail in caplog.text
    for private in ("private", "secret", "https://", "example.test"):
        assert private not in proof.error_detail
        assert private not in caplog.text


async def test_invalid_exact_source_base_is_unknown_and_diagnosable(repository):
    repo = repository
    request = await repo.retain(await repo.commit("one"))
    proof = await (await repo.snapshot()).is_delivered(request, source_base="origin/main")
    assert proof.state == DeliveryState.UNKNOWN
    assert proof.error_detail == (
        "source_base: ValueError: whole-source proof requires an exact base OID"
    )


async def test_large_historical_diff_does_not_hide_a_pending_source(repository):
    repo = repository
    await repo.run("checkout", "main")
    historical_base = await repo.commit("one", "historical\n")
    historical_head = await repo.commit("one", "historical\n" * 120_000)
    await repo.publish()
    await repo.run("checkout", "-B", "source", "main")
    head = await repo.commit("one", "historical\n" * 120_000 + "new source\n")
    request = await repo.retain(head)
    # The source diff is small, but the same path's earlier target diff is not.
    diff = await repo.git.arun_git_result(
        ["diff", historical_base, historical_head, "--"], cwd=str(repo.path),
    )
    assert len(diff.stdout.encode()) > repo.git._MAX_STDIN_BYTES
    proof = await (await repo.snapshot()).is_delivered(request, source_base=historical_head)
    assert proof.state == DeliveryState.PENDING and proof.error_detail is None
    # Frozen batch checks consume the same complete-source proof.
    assert await (await repo.snapshot()).contains_source("task", head, historical_head) is False


async def test_empty_patch_ids_never_match(repository):
    repo = repository
    await repo.run("commit", "--allow-empty", "-m", "empty source")
    head = await repo.run("rev-parse", "HEAD")
    request = await repo.retain(head)
    assert await repo.git.apatch_id(str(repo.path), repo.base, head) is None
    await repo.run("checkout", "--orphan", "different-root")
    await repo.run("rm", "-rf", ".")
    await repo.commit("different")
    await repo.run("commit", "--allow-empty", "-m", "empty target")
    await repo.run("push", "--force", "origin", "HEAD:refs/heads/main")
    proof = await (await repo.snapshot()).is_delivered(request, source_base=repo.base)
    assert proof.state == DeliveryState.PENDING


async def test_stale_tracking_and_local_refs_cannot_answer_new_visit(repository):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    old = await repo.snapshot()
    assert (await old.is_delivered(request)).state == DeliveryState.CONTAINED
    await repo.run("push", "--force", "origin", f"{repo.base}:refs/heads/main")
    assert await repo.run("rev-parse", "refs/remotes/origin/main") == repo.base
    # Recreate a stale local cache deliberately; the next visit must fetch over it.
    await repo.run("update-ref", "refs/remotes/origin/main", head)
    assert not await old.usable(await old.is_delivered(request), request)
    new = await repo.snapshot()
    assert new.target_oid == repo.base
    assert (await new.is_delivered(request, source_base=repo.base)).state == DeliveryState.PENDING


async def test_failed_fetch_unknown_with_bounded_backoff_and_recovery(repository, monkeypatch):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    assert (await (await repo.snapshot()).is_delivered(request)).satisfied
    now = [0.0]
    truth = GitTruth(repo.git, retry_delay=2, max_retry_delay=4, clock=lambda: now[0])
    fetch = repo.git.afetch_origin
    failing = AsyncMock(side_effect=GitError("transport unavailable"))
    monkeypatch.setattr(repo.git, "afetch_origin", failing)
    for instant, deadline, calls in ((0, 2, 1), (1, 2, 1), (2, 6, 2), (6, 10, 3)):
        now[0] = instant
        snapshot = await repo.snapshot(truth=truth)
        assert snapshot.retry_at == deadline
        assert (await snapshot.is_delivered(request)).state == DeliveryState.UNKNOWN
        assert (await snapshot.for_target("refs/heads/main").is_delivered(request)).state == (
            DeliveryState.UNKNOWN
        )
        assert failing.await_count == calls
    monkeypatch.setattr(repo.git, "afetch_origin", fetch)
    now[0] = 10
    assert (await (await repo.snapshot(truth=truth)).is_delivered(request)).satisfied


async def test_cache_keys_repository_and_oid_pair_never_branch(repository):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    original = await repo.snapshot()
    assert (await original.is_delivered(request)).satisfied
    renamed = replace(request, branch_name="renamed", task_version=2)
    assert (await original.is_delivered(renamed)).satisfied
    assert list(repo.truth._cache) == [("r", str(repo.remote), head, head)]
    await repo.run("push", "--force", "origin", f"{repo.base}:refs/heads/main")
    new = await repo.snapshot()
    assert (await new.is_delivered(renamed, source_base=repo.base)).state == DeliveryState.PENDING
    assert set(repo.truth._cache) == {
        ("r", str(repo.remote), head, head), ("r", str(repo.remote), head, repo.base),
    }


async def test_cached_pair_does_not_reuse_a_different_task_trailer(repository):
    repo = repository
    _first, head, request = await source_work(repo)
    other = await repo.retain(head, task="other")
    await repo.run("checkout", "main")
    await repo.commit("unrelated", message=f"integration\n\nAQ-Source: task@{head}")
    await repo.publish()
    snapshot = await repo.snapshot()
    assert (await snapshot.is_delivered(request)).satisfied
    assert (await snapshot.is_delivered(other)).state == DeliveryState.PENDING


async def test_identical_oid_pairs_in_different_repositories_are_separate(repository, tmp_path):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    snapshot = await repo.snapshot()
    assert (await snapshot.is_delivered(request)).satisfied
    other_remote = tmp_path / "other.git"
    # Mirror all evidence, including provenance outside the head namespace.
    await repo.git._arun(["clone", "--mirror", str(repo.remote), str(other_remote)], cwd=str(tmp_path))
    await repo.run("remote", "set-url", "origin", str(other_remote))
    other_snapshot = await repo.truth.snapshot(
        str(repo.path), project_id="p", repository_id="r", repository_url=str(other_remote),
        target_ref="refs/heads/main",
    )
    assert (await other_snapshot.is_delivered(request)).satisfied
    assert set(repo.truth._cache) == {
        ("r", str(repo.remote), head, head), ("r", str(other_remote), head, head),
    }


async def test_cached_facts_skip_repeat_ancestry_and_cache_is_bounded(repository, monkeypatch):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    truth = GitTruth(repo.git, cache_limit=1)
    # Bind the spy explicitly, so its calls retain the real store instance.
    ancestor = GitProvenance.ancestor

    async def inspect(self, source, target):
        await probe(self, source, target)
        return await ancestor(self, source, target)

    probe = AsyncMock()
    monkeypatch.setattr(GitProvenance, "ancestor", inspect)
    for _ in range(2):
        snapshot = await repo.snapshot(truth=truth)
        assert (await snapshot.is_delivered(request)).satisfied
    assert probe.await_count == 1
    await repo.run("push", "--force", "origin", f"{repo.base}:refs/heads/main")
    snapshot = await repo.snapshot(truth=truth)
    assert (await snapshot.is_delivered(request)).state == DeliveryState.PENDING
    assert list(truth._cache) == [("r", str(repo.remote), head, repo.base)]


async def test_warm_completion_proofs_do_not_launch_git_processes(repository, monkeypatch):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    snapshot = await repo.snapshot()
    assert (await snapshot.is_delivered(request)).satisfied
    run = AsyncMock(side_effect=AssertionError("warm proof launched Git"))
    monkeypatch.setattr(repo.git, "arun_git_result", run)
    # A new request/cycle still uses immutable Git facts; ordinary metadata
    # edits must recheck the completion binding without repeating validation.
    for index in range(137):
        current = replace(request, task_version=index, branch_name=f"renamed-{index}")
        assert (await snapshot.is_delivered(current)).satisfied
    run.assert_not_awaited()


async def test_cache_metrics_report_warm_hits_and_changed_completion_misses(repository):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    observed = await repo.snapshot()
    metrics = SelectionMetrics()
    with selection_metrics_scope(metrics):
        assert (await observed.is_delivered(request)).satisfied
        assert (await observed.is_delivered(request)).satisfied
        assert (await observed.is_delivered(replace(request, task_version=2))).satisfied
        assert not (await observed.is_delivered(replace(request, completion_id="missing"))).satisfied
    assert metrics.counts["completion_cache_hits"] == 1
    assert metrics.counts["completion_cache_misses"] == 2
    assert metrics.counts["delivery_cache_hits"] == 1
    assert metrics.counts["delivery_cache_misses"] == 3


async def test_cached_only_proofs_withhold_cold_changed_and_evicted_inputs(repository, monkeypatch):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    truth = GitTruth(repo.git, cache_limit=1)
    snapshot = await repo.snapshot(truth=truth)
    display = replace(snapshot, cached_only=True)
    run = AsyncMock(wraps=repo.git.arun_git_result)
    monkeypatch.setattr(repo.git, "arun_git_result", run)
    assert (await display.is_delivered(request)).reason == "proof_unavailable"
    run.assert_not_awaited()
    proof = await snapshot.is_delivered(request)
    run.reset_mock()
    assert await display.is_delivered(request) == proof
    for changed in (replace(request, completion_id="close-2"),
                    replace(request, task_version=2), replace(request, task_status="READY")):
        assert not (await display.is_delivered(changed)).satisfied
    assert not (await display.is_delivered(request, source_base=repo.base)).satisfied
    moved_target = replace(display, observation=replace(display.observation, target_oid=repo.base))
    assert not (await moved_target.is_delivered(request)).satisfied
    # Ref retargeting invalidates the proof even when task, source and target
    # are unchanged. Only the newly pinned marker may bind this generation.
    refs = dict(display.observation.source_heads)
    identity = CompletionIdentity("p", "r", "task", "close-1")
    refs[identity.ref] = repo.base
    moved_marker = replace(display, observation=replace(display.observation, source_heads=refs))
    assert not (await moved_marker.is_delivered(request)).satisfied
    other_store = replace(display, observation=replace(display.observation,
                                                       store=str(repo.path / "missing")))
    assert not (await other_store.is_delivered(request)).satisfied
    other_url = replace(display, observation=replace(display.observation, repository_url="other"))
    assert not (await other_url.is_delivered(request)).satisfied
    run.assert_not_awaited()
    # Eviction is a miss, never a request to revalidate in an interactive read.
    assert (await snapshot.is_delivered(replace(request, task_version=2))).satisfied
    assert (await display.is_delivered(request)).reason == "proof_unavailable"
    assert all(len(cache) <= 1 for cache in (truth._proofs, truth._completions, truth._objects))


async def test_cached_completion_records_cannot_be_mutated_by_consumers(repository, monkeypatch):
    repo = repository
    head = await repo.commit("one")
    await repo.retain(head)
    snapshot = await repo.snapshot()
    identity = CompletionIdentity("p", "r", "task", "close-1")
    record = await snapshot.read_completion(identity)
    record["source_oid"] = repo.base
    record["identity"]["generation"] = "other"
    run = AsyncMock(side_effect=AssertionError("cached record launched Git"))
    monkeypatch.setattr(repo.git, "arun_git_result", run)
    cached = await snapshot.read_completion(identity)
    assert cached["source_oid"] == head
    assert cached["identity"]["generation"] == "close-1"
    run.assert_not_awaited()


async def test_pinned_completion_ref_and_two_targets_share_one_fetch(repository, monkeypatch):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    await repo.run("push", "origin", "HEAD:refs/heads/epic")
    fetch = AsyncMock(wraps=repo.git.afetch_origin)
    monkeypatch.setattr(repo.git, "afetch_origin", fetch)
    snapshot = await repo.snapshot()
    identity = CompletionIdentity("p", "r", "task", "close-1")
    marker_ref = identity.ref
    await repo.run("update-ref", "-d", marker_ref)
    # The marker was captured by OID and is still readable after local ref deletion.
    assert (await snapshot.is_delivered(request)).satisfied
    assert (await snapshot.for_target("refs/heads/epic").is_delivered(
        replace(request, target_ref="refs/heads/epic"),
    )).satisfied
    assert fetch.await_count == 1


async def test_completion_cache_pins_marker_and_revalidates_new_fetch(repository, monkeypatch):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    identity = CompletionIdentity("p", "r", "task", "close-1")
    read = GitProvenance.read_completion
    probe = AsyncMock()

    async def inspect(self, requested, **kwargs):
        await probe(requested)
        return await read(self, requested, **kwargs)

    monkeypatch.setattr(GitProvenance, "read_completion", inspect)
    snapshot = await repo.snapshot()
    record = await snapshot.read_completion(identity)
    record["artifact"] = False  # Callers cannot mutate the validated cache.
    assert (await snapshot.read_completion(identity))["artifact"]
    assert (await (await repo.snapshot()).read_completion(identity))["artifact"]
    assert probe.await_count == 1
    # A changed fetched marker is a new immutable object, not the old answer.
    provenance = GitProvenance(repo.git, str(repo.path), repository_url=str(repo.remote))
    record["source_oid"] = head
    marker = await provenance._stage(record)
    await repo.run("push", "--force", "origin", f"{marker}:{identity.ref}")
    assert (await (await repo.snapshot()).is_delivered(request)).state == DeliveryState.NO_ARTIFACT
    assert probe.await_count == 2
    await repo.run("push", "origin", "--delete", identity.ref)
    # The old observation retains its pinned object; the new one sees absence.
    assert (await snapshot.read_completion(identity))["artifact"]
    assert await (await repo.snapshot()).read_completion(identity) is None
    assert probe.await_count == 3  # The shared cache never retains absent provenance.


async def test_completion_cache_is_store_scoped_bounded_and_does_not_cache_failures(
    repository, monkeypatch, tmp_path,
):
    repo = repository
    head = await repo.commit("one")
    await repo.retain(head)
    second = await repo.retain(head, completion="close-2")
    identity = CompletionIdentity("p", "r", "task", "close-1")
    other = replace(identity, generation="close-2")
    truth = GitTruth(repo.git, cache_limit=1)
    snapshot = await repo.snapshot(truth=truth)
    read = GitProvenance.read_completion
    probe = AsyncMock()

    async def inspect(self, requested, **kwargs):
        await probe(requested)
        return await read(self, requested, **kwargs)

    monkeypatch.setattr(GitProvenance, "read_completion", inspect)
    await snapshot.read_completion(identity)
    await snapshot.read_completion(other)
    await snapshot.read_completion(identity)
    assert probe.await_count == 3 and len(truth._completions) == 1
    missing_store = replace(snapshot, observation=replace(
        snapshot.observation, store=str(tmp_path / "unavailable-store")))
    # Scope prevents an available store's cache from hiding a missing checkout.
    assert (await missing_store.is_delivered(replace(second, completion_id="close-1"))).state == (
        DeliveryState.UNKNOWN)
    assert probe.await_count == 4
    assert (await snapshot.read_completion(identity))["source_oid"] == head
    assert probe.await_count == 4
    failure = AsyncMock(side_effect=[GitError("unavailable"), await read(
        GitProvenance(repo.git, str(repo.path), repository_url=str(repo.remote)),
        other, refs=snapshot.observation.source_heads)])
    monkeypatch.setattr(GitProvenance, "read_completion", failure)
    with pytest.raises(GitError):
        await snapshot.read_completion(other)
    assert (await snapshot.read_completion(other))["source_oid"] == head
    assert failure.await_count == 2


@pytest.mark.parametrize("change", [{"task_status": "READY"}, {"claim_epoch": 2},
                                    {"task_version": 2}, {"target_ref": "refs/heads/epic"}])
async def test_use_revalidates_ordinary_identity(repository, change):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    snapshot = await repo.snapshot()
    proof = await snapshot.is_delivered(request)
    assert await snapshot.usable(proof, request)
    assert not await snapshot.usable(proof, replace(request, **change))
    patch_proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert await snapshot.usable(patch_proof, request, current_source_base=repo.base)
    assert not await snapshot.usable(patch_proof, request, current_source_base=head)


async def test_epic_needs_settled_children_exact_green_tree_and_no_hold(repository):
    repo = repository
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.publish()
    snapshot = await repo.snapshot()
    tree = await repo.git.atree_sha(str(repo.path), head)
    assert await epic_complete(snapshot, [request], green_oid=head,
                               review_required=True, approved_tree=tree)
    assert not await epic_complete(snapshot, [request], green_oid=repo.base)
    assert not await epic_complete(snapshot, [request], green_oid=head, held=True)
    assert not await epic_complete(snapshot, [request], green_oid=head,
                                   review_required=True, approved_tree="0" * 40)
    assert not await epic_complete(snapshot, [replace(request, task_status="READY")], green_oid=head)
    assert await epic_complete(snapshot, [replace(request, completion_id="missing")],
                               green_oid=head) is None


async def test_repair_green_precedes_movement_and_rev_list_includes_merged_commits(repository):
    repo = repository
    assert repair_progress(None, None, green=True)
    assert repair_progress(repo.base, repo.base, green=True)
    assert not repair_progress(repo.base, repo.base, green=False)
    assert not repair_progress(None, None, green=False)
    side = await repo.commit("side")
    await repo.run("checkout", "main")
    repair = await repo.commit("repair")
    await repo.run("merge", "--no-ff", "source", "-m", "repair merge")
    head = await repo.run("rev-parse", "HEAD")
    assert repair_progress(repo.base, head, green=False)
    assert set(await commits_added(repo.git, str(repo.path), repo.base, head)) == {side, repair, head}
    assert await commits_added(repo.git, str(repo.path), head, head) == []
    with pytest.raises(GitError):
        await commits_added(repo.git, str(repo.path), "0" * 40, head)
    with pytest.raises(GitError):
        await repo.git.atree_sha(str(repo.path), "0" * 40)


@pytest.mark.parametrize("delivered", [False, True])
async def test_oversized_historical_patch_does_not_block_delivery(repository, delivered):
    repo = repository
    await repo.run("checkout", "main")
    await repo.commit("seed", "historical line\n" * 80000)
    repo.base = await repo.commit("seed", "seed\n")
    await repo.run("checkout", "-B", "source")
    head = await repo.commit("seed", "small intended change\n")
    request = await repo.retain(head)
    await repo.run("checkout", "main")
    await repo.commit("unrelated")
    if delivered:
        await repo.run("merge", "--squash", "source")
        await repo.run("commit", "-m", "squashed source")
    await repo.publish()
    snapshot = await repo.snapshot()
    proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert proof.state == (DeliveryState.CONTAINED if delivered else DeliveryState.PENDING)
    assert proof.reason == ("whole_source_patch" if delivered else "source_not_delivered")
    assert await snapshot.contains_source("task", head, repo.base) is delivered


@pytest.mark.parametrize("proof_kind", ["pending", "patch", "trailer"])
async def test_oversized_source_patch_is_complete_and_uses_delivery_proofs(repository, proof_kind):
    repo = repository
    head = await repo.commit("large", "large source line\n" * 80000)
    request = await repo.retain(head)
    patch_id = await repo.git.apatch_id(str(repo.path), repo.base, head)
    assert patch_id is not None and len(patch_id) == 40
    await repo.run("checkout", "main")
    if proof_kind == "patch":
        await repo.run("merge", "--squash", "source")
        await repo.run("commit", "-m", "same tree, different history")
    elif proof_kind == "trailer":
        await repo.commit("unrelated", message=f"delivered\n\nAQ-Source: task@{head}")
    await repo.publish()
    snapshot = await repo.snapshot()
    proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert proof.reason == {
        "pending": "source_not_delivered", "patch": "whole_source_patch",
        "trailer": "source_trailer",
    }[proof_kind]
    assert await snapshot.contains_source("task", head, repo.base) is (proof_kind != "pending")


async def test_failed_historical_probe_does_not_hide_later_match(repository, monkeypatch):
    repo = repository
    _first, head, request = await source_work(repo)
    await repo.run("checkout", "main")
    failed_head = await repo.commit("one", "historical content\n")
    await repo.run("revert", "--no-edit", failed_head)
    await repo.run("merge", "--squash", "source")
    await repo.run("commit", "-m", "whole source")
    await repo.commit("unrelated")
    await repo.publish()
    patch_id = repo.git.apatch_id
    failures = []

    async def probe(path, base, candidate):
        if candidate == failed_head:
            failures.append(candidate)
            raise GitError("historical probe failed")
        return await patch_id(path, base, candidate)

    monkeypatch.setattr(repo.git, "apatch_id", probe)
    proof = await (await repo.snapshot()).is_delivered(request, source_base=repo.base)
    assert failures
    assert proof.reason == "whole_source_patch"


@pytest.mark.parametrize("delivered", [False, True])
async def test_failed_historical_probes_require_independent_proof_and_are_not_cached(
    repository, monkeypatch, caplog, delivered,
):
    repo = repository
    _first, head, request = await source_work(repo)
    await repo.run("checkout", "main")
    await repo.commit("one", "different historical content\n")
    if delivered:
        await repo.run("checkout", "source", "--", ".")
        await repo.run("commit", "-am", "same tree, different history")
    await repo.publish()
    snapshot = await repo.snapshot()
    patch_id = repo.git.apatch_id
    failures = []

    async def probe(path, base, candidate):
        if candidate != head:
            failures.append(candidate)
            raise GitError("historical probe failed with private content")
        return await patch_id(path, base, candidate)

    monkeypatch.setattr(repo.git, "apatch_id", probe)
    proof = await snapshot.is_delivered(request, source_base=repo.base)
    assert failures
    if delivered:
        assert proof.state == DeliveryState.CONTAINED and proof.reason == "full_tree"
        assert proof.error_detail is None
    else:
        assert proof.state == DeliveryState.UNKNOWN
        assert proof.error_detail == (
            "whole_source_patch: GitError: Git command failed (untrusted detail omitted)"
        )
        assert proof.error_detail in caplog.text
        assert "private content" not in caplog.text
    assert await snapshot.contains_source("task", head, repo.base) is (True if delivered else None)
    assert repo.base not in snapshot.truth._pair(snapshot.observation, head).patches
    monkeypatch.setattr(repo.git, "apatch_id", patch_id)
    retry = await snapshot.is_delivered(request, source_base=repo.base)
    assert retry.state == (DeliveryState.CONTAINED if delivered else DeliveryState.PENDING)
    assert await snapshot.contains_source("task", head, repo.base) is delivered


async def test_spent_patch_budget_is_negative_and_merge_noop_still_proves(repository, monkeypatch):
    """A quadratic range search is bounded; a squash is still proven by a no-op merge."""
    from src.integration import git_truth

    repo = repository
    await repo.commit("hot", "source\n")
    head = await repo.commit("one")
    request = await repo.retain(head)
    await repo.run("checkout", "main")
    await repo.commit("other", "unrelated\n", message="unrelated")
    for index in range(4):  # later target work on the same hot path
        await repo.commit("hot", f"target {index}\n", message=f"hot {index}")
    await repo.run("merge", "--squash", "-X", "theirs", "source")
    await repo.run("commit", "-m", "squash without a source trailer")
    await repo.commit("hot", "moved on\n", message="hot again")
    await repo.publish()
    monkeypatch.setattr(git_truth, "HISTORICAL_PATCH_PROBE_LIMIT", 1)
    # One and hot differ from the source, so only the no-op merge can prove it.
    proof = await (await repo.snapshot()).is_delivered(request, source_base=repo.base)
    assert (proof.state, proof.reason) == (DeliveryState.PENDING, "source_not_delivered")
    await repo.commit("hot", "source\n", message="hot restored")
    await repo.publish()
    proof = await (await repo.snapshot()).is_delivered(request, source_base=repo.base)
    assert (proof.state, proof.reason) == (DeliveryState.CONTAINED, "merge_noop")


async def test_overlapping_train_targets_share_two_fetches_but_next_visit_is_fresh(repository):
    import asyncio

    repo = repository
    truth = GitTruth(repo.git, share_fetches=True)
    head = await repo.commit("shared-source")
    await repo.run("push", "origin", "HEAD:refs/heads/epic")
    fetch = repo.git.afetch_origin
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return await fetch(*args, **kwargs)

    repo.git.afetch_origin = AsyncMock(side_effect=blocked_fetch)
    first = asyncio.create_task(repo.snapshot(truth=truth))
    await entered.wait()
    others = [asyncio.create_task(repo.snapshot(
        truth=truth, target="refs/heads/epic" if i % 2 else "refs/heads/missing",
    )) for i in range(52)]
    await asyncio.sleep(0)
    release.set()
    snapshots = await asyncio.gather(first, *others)
    assert repo.git.afetch_origin.await_count == 2
    assert snapshots[0].target_oid == repo.base
    for i, snapshot in enumerate(snapshots[1:]):
        assert snapshot.target_oid == (head if i % 2 else None)
        assert snapshot.error == (None if i % 2 else "missing_target")
    assert not truth._fetches

    await repo.publish()
    assert (await repo.snapshot(truth=truth)).target_oid == head
    assert repo.git.afetch_origin.await_count == 3
    assert snapshots[0].target_oid == repo.base


async def test_shared_fetch_started_before_a_push_cannot_answer_its_caller(repository, tmp_path):
    import asyncio

    repo = repository
    truth = GitTruth(repo.git, share_fetches=True)
    head = await repo.commit("new-head")
    writer = tmp_path / "writer.git"
    await repo.git._arun(["clone", "--bare", str(repo.path), str(writer)], cwd=str(tmp_path))
    fetch = repo.git.afetch_origin
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetched_before_push(*args, **kwargs):
        await fetch(*args, **kwargs)
        entered.set()
        await release.wait()

    repo.git.afetch_origin = AsyncMock(side_effect=fetched_before_push)
    first = asyncio.create_task(repo.snapshot(truth=truth))
    await entered.wait()
    # Train publication uses a separate repository, so it cannot update the
    # observer's tracking refs or order against its already-started fetch.
    await repo.git._arun(["push", str(repo.remote), "HEAD:refs/heads/main"], cwd=str(writer))
    second = asyncio.create_task(repo.snapshot(truth=truth))
    await asyncio.sleep(0)
    release.set()
    before, after = await asyncio.gather(first, second)
    assert before.target_oid == repo.base
    assert after.target_oid == head
    assert await after.is_fresh()
    assert repo.git.afetch_origin.await_count == 2
    assert not truth._fetches


@pytest.mark.parametrize("cancel_leader", [True, False])
async def test_shared_fetch_cancellation_does_not_strand_other_readers(repository, cancel_leader):
    import asyncio

    repo = repository
    truth = GitTruth(repo.git, share_fetches=True)
    fetch = repo.git.afetch_origin
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return await fetch(*args, **kwargs)

    repo.git.afetch_origin = AsyncMock(side_effect=blocked_fetch)
    leader = asyncio.create_task(repo.snapshot(truth=truth))
    await entered.wait()
    waiter = asyncio.create_task(repo.snapshot(truth=truth))
    await asyncio.sleep(0)
    cancelled, survivor = (leader, waiter) if cancel_leader else (waiter, leader)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    if cancel_leader:
        with pytest.raises(SharedFetchCancelled, match="shared fetch was cancelled"):
            await survivor
    else:
        assert (await survivor).target_oid == repo.base
    assert not truth._fetches
    assert repo.git.afetch_origin.await_count == 1

    repo.git.afetch_origin.side_effect = fetch
    assert (await repo.snapshot(truth=truth)).target_oid == repo.base
    assert repo.git.afetch_origin.await_count == 2


@pytest.mark.parametrize("error", [
    RuntimeError("unexpected failure"),
    GitHubAccessError("rate_limited", "paused", retry_at=1000.0, http_status=403),
    GitHubAccessError("forbidden", "access denied", http_status=403),
])
async def test_shared_fetch_propagates_non_git_failure_without_retries(repository, error):
    import asyncio

    repo = repository
    truth = GitTruth(repo.git, share_fetches=True)
    fetch = repo.git.afetch_origin
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(*args, **kwargs):
        entered.set()
        await release.wait()
        raise error

    repo.git.afetch_origin = AsyncMock(side_effect=fail)
    leader = asyncio.create_task(repo.snapshot(truth=truth))
    await entered.wait()
    waiters = [asyncio.create_task(repo.snapshot(truth=truth)) for _ in range(12)]
    await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(leader, *waiters, return_exceptions=True)
    assert all(outcome is error for outcome in outcomes)
    assert repo.git.afetch_origin.await_count == 1
    assert not truth._fetches

    repo.git.afetch_origin.side_effect = fetch
    assert (await repo.snapshot(truth=truth)).target_oid == repo.base
    assert repo.git.afetch_origin.await_count == 2


async def test_shared_fetch_failure_never_becomes_delivery_evidence(repository):
    import asyncio

    repo = repository
    truth = GitTruth(repo.git, share_fetches=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(*args, **kwargs):
        entered.set()
        await release.wait()
        raise GitError("unavailable")

    repo.git.afetch_origin = AsyncMock(side_effect=fail)
    first = asyncio.create_task(repo.snapshot(truth=truth, target="refs/heads/missing"))
    await entered.wait()
    second = asyncio.create_task(repo.snapshot(truth=truth))
    await asyncio.sleep(0)
    release.set()
    snapshots = await asyncio.gather(first, second)
    assert all(snapshot.error == "snapshot_git_error: unavailable" for snapshot in snapshots)
    assert all(snapshot.target_oid is None for snapshot in snapshots)
    assert repo.git.afetch_origin.await_count == 1
    assert not truth._fetches
