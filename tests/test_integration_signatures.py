"""Conservative exact-head signatures and immutable train JSON contracts."""

import json
from dataclasses import FrozenInstanceError, fields, replace
from unittest.mock import AsyncMock

import pytest

from src.git.manager import GitManager
from src.integration.delivery_truth import DeliverySnapshot
from src.integration.git_truth import GitTruth, GitTruthSnapshot, build_source_signature
from src.integration.signatures import (
    GeneratedOutput,
    GeneratedProvenance,
    GraphImpact,
    GraphObservation,
    PathChange,
    SignatureCache,
    SignatureIdentity,
    SignatureRules,
    freeze_signature_documents,
    read_signature_documents,
)


@pytest.fixture
def rules():
    return SignatureRules(
        ("src/a.py", "src/b.py", "src/old.py", "src/new.py"), "generator-v1",
        (GeneratedOutput("generated.json", ("src/a.py",)),), ("policy.toml",),
    )


@pytest.fixture
def identity(rules):
    return SignatureIdentity(
        "project", "repository", "task", "pr-1", "refs/heads/task", "refs/heads/dev",
        "a" * 40, "b" * 40, "c" * 40, "policy-v1", rules.digest,
    )


def build(identity, rules, *paths, **kwargs):
    return build_source_signature(identity, [PathChange("M", p) for p in paths], rules, **kwargs)


def test_rename_deletion_and_unusual_paths(identity, rules):
    changes = [PathChange("R", "src/new.py", "src/old.py"), PathChange("D", "src/a.py")]
    signature = build_source_signature(identity, changes, rules)
    assert signature.paths == ("src/a.py", "src/new.py", "src/old.py")
    assert not signature.universal
    assert signature.conflicts_with(build(identity, rules, "src/old.py"))
    assert signature.conflicts_with(build(identity, rules, "src/new.py"))
    assert not signature.conflicts_with(build(identity, rules, "src/b.py"))


@pytest.mark.parametrize("paths,reason", [
    (("unknown.py",), "unknown_path"),
    (("generated.json",), "generated_output_only"),
    (("generated.json", "src/b.py"), "generated_output_only"),
    (("generated.json", "src/a.py"), "generated_provenance_unavailable"),
    (("policy.toml",), "global_path"),
])
def test_unknown_or_output_only_is_universal(identity, rules, paths, reason):
    signature = build(identity, rules, *paths)
    assert signature.universal and reason in signature.reasons
    assert signature.conflicts_with(build(identity, rules, "src/b.py"))
    assert signature.paths


def test_generated_exclusion_requires_exact_identity_and_receipt(identity, rules):
    proof = GeneratedProvenance(identity, ("generated.json",), "d" * 64)
    signature = build(identity, rules, "src/a.py", "generated.json", provenance=proof)
    assert signature.paths == ("src/a.py",)
    assert signature.excluded_outputs == ("generated.json",)
    assert signature.provenance_digest == proof.receipt_digest
    assert not signature.universal
    for invalid in (replace(proof, identity=replace(identity, source_head="e" * 40)),
                    replace(proof, outputs=()), replace(proof, receipt_digest="")):
        assert build(identity, rules, "src/a.py", "generated.json", provenance=invalid).universal
    assert build(identity, rules, "generated.json", provenance=proof).universal
    # Attributes never enter the API, and an unknown registry cannot drop outputs.
    assert build(identity, replace(rules, revision="different"), "src/a.py").universal


def test_generated_rename_checks_both_names(identity, rules):
    rules = replace(rules, generated=(
        GeneratedOutput("old.json", ("src/a.py",)),
        GeneratedOutput("new.json", ("src/a.py",)),
    ))
    identity = replace(identity, generator_digest=rules.digest)
    changes = [PathChange("R", "new.json", "old.json"), PathChange("M", "src/a.py")]
    proof = GeneratedProvenance(identity, ("new.json",), "d" * 64)
    assert build_source_signature(identity, changes, rules, provenance=proof).universal
    proof = replace(proof, outputs=("old.json", "new.json"))
    signature = build_source_signature(identity, changes, rules, provenance=proof)
    assert not signature.universal
    assert signature.excluded_outputs == ("new.json", "old.json")


@pytest.mark.parametrize("changes", [None, [PathChange("U", "src/a.py")],
    [PathChange("M", "../escape")], [PathChange("R", "src/new.py")],
    [PathChange("M", "src/a.py", "src/old.py")]])
def test_unavailable_and_malformed_diffs_are_not_empty(identity, rules, changes):
    assert build_source_signature(identity, changes, rules).universal
    assert not build_source_signature(identity, [], rules).universal


def test_graph_is_exact_observation_and_never_relaxes_paths(identity, rules):
    identity = replace(identity, graph_digest="f" * 64, suite="unit")
    graph = GraphObservation(identity, (
        GraphImpact("src/a.py", ("//a:lib",), ("//a:test", "//common:test")),
    ))
    signature = build(identity, rules, "src/a.py", "src/b.py", graph=graph)
    assert signature.graph_state == "observational"
    assert signature.owning_targets == ("//a:lib",)
    assert signature.reverse_test_targets == ("//a:test", "//common:test")
    assert signature.uncovered_paths == ("src/b.py",)
    assert signature.conflicts_with(build(identity, rules, "src/b.py"))
    other_graph = replace(graph, impacts=(GraphImpact("src/a.py", ("//other:lib",), ()),))
    assert signature.conflicts_with(build(identity, rules, "src/a.py", graph=other_graph))
    for stale in (replace(graph, identity=replace(identity, source_head="e" * 40)),
                  replace(graph, identity=replace(identity, suite="integration"))):
        unknown = build(identity, rules, "src/a.py", graph=stale)
        assert unknown.graph_state == "unknown" and not unknown.owning_targets
        assert unknown.uncovered_paths == ("src/a.py",)
    assert build(identity, rules, "uncovered.py", graph=graph).universal


def test_canonical_digests_and_deep_immutability(identity, rules):
    a = build(identity, rules, "src/a.py", "src/b.py")
    b = build(identity, rules, "src/b.py", "src/a.py", "src/a.py")
    assert a.digest == b.digest
    with pytest.raises(FrozenInstanceError):
        a.paths = ()
    document = a.to_document()
    document["paths"].clear()
    assert a.paths == ("src/a.py", "src/b.py")
    mutable = ["src/a.py"]
    copied = replace(rules, known_paths=mutable)
    mutable.clear()
    assert copied.known_paths == ("src/a.py",)
    assert a.digest != replace(a, identity=replace(identity, policy_revision="v2")).digest


def test_json_round_trip_preserves_existing_documents_and_rejects_overwrite(identity, rules):
    signature = build(identity, rules, "src/a.py")
    before = {"artifact_id": "approved", "nested": [1]}
    artifact, manifest = freeze_signature_documents(
        before, {"sources": ["legacy"]}, [signature], current_identities=[identity],
        selection_reasons=["eligible"],
    )
    assert before == {"artifact_id": "approved", "nested": [1]}
    assert artifact["train_signatures"]["signature_version"] == 1
    assert manifest["sources"] == ["legacy"]
    assert read_signature_documents(json.loads(json.dumps(artifact)), manifest,
                                    current_identities=[identity]) == (signature,)
    assert freeze_signature_documents(artifact, manifest, [signature],
        current_identities=[identity], selection_reasons=["eligible"]) == (artifact, manifest)
    with pytest.raises(ValueError, match="replace frozen"):
        freeze_signature_documents(artifact, manifest, [build(identity, rules, "src/b.py")],
            current_identities=[identity], selection_reasons=["eligible"])
    artifact["train_signatures"]["sources"].clear()
    assert len(manifest["train_signatures"]["sources"]) == 1


@pytest.mark.parametrize("mutation", ["absent", "version", "source_version", "digest", "stale"])
def test_legacy_mixed_corrupt_stale_documents_require_full_fallback(identity, rules, mutation):
    artifact, manifest = freeze_signature_documents({}, {}, [build(identity, rules, "src/a.py")],
        current_identities=[identity], selection_reasons=["eligible"])
    if mutation == "absent":
        artifact = {}
    elif mutation == "version":
        artifact["train_signatures"]["signature_version"] = 2
    elif mutation == "source_version":
        artifact["train_signatures"]["sources"][0]["signature_version"] = 2
    elif mutation == "digest":
        artifact["train_signatures"]["sources"][0]["paths"] = []
    else:
        identity = replace(identity, target_sha="e" * 40)
    assert read_signature_documents(artifact, manifest, current_identities=[identity]) is None
    assert read_signature_documents(None, None, current_identities=[identity]) is None


def test_freeze_rejects_mixed_versions_stale_or_duplicate_members(identity, rules):
    signature = build(identity, rules, "src/a.py")
    for signatures, current in (
        ([replace(signature, signature_version=2)], [identity]),
        ([signature], [replace(identity, pr_id="new")]),
        ([signature, signature], [identity, identity]),
        ([replace(signature, universal="false")], [identity]),
    ):
        with pytest.raises(ValueError):
            freeze_signature_documents({}, {}, signatures, current_identities=current,
                                       selection_reasons=["eligible"])
    assert replace(signature, signature_version=2).conflicts_with(signature)


def test_cache_absolute_ttl_lru_and_hard_bounds(identity, rules):
    now = [0.0]
    cache = SignatureCache(limit=2, clock=lambda: now[0])
    signature = build(identity, rules, "src/a.py")
    cache.put((1,), signature)
    cache.put((2,), signature)
    now[0] = 599
    assert cache.get((1,)) == signature
    cache.put((3,), signature)
    assert cache.get((2,)) is None
    now[0] = 600
    assert cache.get((1,)) is None  # LRU hit did not extend TTL.
    assert cache.get((3,)) == signature
    cache.invalidate(identity)
    assert cache.get((3,)) is None
    for kwargs in ({"limit": 2049}, {"ttl": 601}, {"limit": 0}, {"ttl": 0}):
        with pytest.raises(ValueError):
            SignatureCache(**kwargs)


@pytest.fixture
async def git_signature(tmp_path, rules, identity):
    git = GitManager()
    async def run(*args):
        return await git._arun(list(args), cwd=str(tmp_path))
    await run("init", "-b", "dev")
    await run("config", "user.name", "Tester")
    await run("config", "user.email", "test@example.com")
    await run("config", "commit.gpgsign", "false")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/old.py").write_text("rename me\n")
    (tmp_path / "src/a.py").write_text("delete me\n")
    await run("add", ".")
    await run("commit", "-m", "base")
    base = await run("rev-parse", "HEAD")
    await run("checkout", "-b", "task")
    await run("mv", "src/old.py", "src/new.py")
    await run("rm", "src/a.py")
    odd = "src/ weird\t\n:* name.py"
    (tmp_path / odd).write_text("odd name\n")
    await run("add", ".")
    await run("commit", "-m", "rename and delete")
    head = await run("rev-parse", "HEAD")
    rules = replace(rules, known_paths=(*rules.known_paths, odd))
    identity = replace(identity, merge_base=base, target_sha=base, source_head=head,
                       generator_digest=rules.digest)
    observed = DeliverySnapshot(git, str(tmp_path), "project", "repository", "local",
                               identity.target_ref, base, {"refs/remotes/origin/task": head})
    snapshot = GitTruthSnapshot(GitTruth(git), observed)
    return snapshot, identity, rules, run


async def test_real_git_diff_and_warm_cache_recheck(git_signature, monkeypatch):
    snapshot, identity, rules, _ = git_signature
    check = AsyncMock(return_value=identity)
    signature = await snapshot.source_signature(identity, rules, recheck=check)
    assert not signature.universal
    assert signature.paths == tuple(sorted(("src/a.py", "src/old.py", "src/new.py",
                                           "src/ weird\t\n:* name.py")))
    assert check.await_count == 2
    no_git = AsyncMock(side_effect=AssertionError("a warm hit must not rerun Git"))
    monkeypatch.setattr(snapshot.truth.git, "arun_git_result", no_git)
    assert await snapshot.source_signature(identity, rules, recheck=check) == signature
    assert check.await_count == 4
    no_git.assert_not_called()
    # Even an identical head cannot reuse success after an ordinary identity failure.
    stale = AsyncMock(return_value=replace(identity, pr_id="retargeted"))
    assert (await snapshot.source_signature(identity, rules, recheck=stale)).universal
    assert not snapshot.truth.signatures._entries


@pytest.mark.parametrize("field", [f.name for f in fields(SignatureIdentity)])
async def test_every_identity_field_invalidates_cached_result(git_signature, field):
    snapshot, identity, rules, _ = git_signature
    check = AsyncMock(return_value=identity)
    await snapshot.source_signature(identity, rules, recheck=check)
    moved = replace(identity, **{field: "different"})
    stale = await snapshot.source_signature(identity, rules, recheck=AsyncMock(return_value=moved))
    assert stale.universal and stale.reasons == ("identity_stale",)
    assert not snapshot.truth.signatures._entries


async def test_identity_changes_during_diff_or_failed_recheck_invalidate(git_signature):
    snapshot, identity, rules, _ = git_signature
    for check in (AsyncMock(side_effect=[identity, None]),
                  AsyncMock(side_effect=OSError("private Git detail"))):
        signature = await snapshot.source_signature(identity, rules, recheck=check)
        assert signature.universal
        assert not snapshot.truth.signatures._entries
        assert "private" not in json.dumps(signature.to_document())


async def test_wrong_merge_base_or_snapshot_cannot_prove_a_smaller_diff(git_signature):
    snapshot, identity, rules, _ = git_signature
    wrong = replace(identity, merge_base=identity.source_head)
    signature = await snapshot.source_signature(wrong, rules, recheck=AsyncMock(return_value=wrong))
    assert signature.universal and signature.reasons == ("merge_base_mismatch",)
    wrong = replace(identity, target_sha=identity.source_head)
    signature = await snapshot.source_signature(wrong, rules, recheck=AsyncMock(return_value=wrong))
    assert signature.universal and signature.reasons == ("snapshot_identity_mismatch",)


async def test_cache_key_binds_new_task_pr_refs_policy_and_graph(git_signature):
    snapshot, identity, rules, _ = git_signature
    first = await snapshot.source_signature(identity, rules, recheck=AsyncMock(return_value=identity))
    for field, value in (
        ("task_id", "other-task"), ("pr_id", "other-pr"),
        ("policy_revision", "other-policy"), ("graph_digest", "d" * 64),
        ("suite", "another-suite"), ("source_ref", "refs/heads/other"),
    ):
        current = replace(identity, **{field: value})
        observed = replace(snapshot.observation, source_heads={
            "refs/remotes/origin/" + current.source_ref.removeprefix("refs/heads/"):
                current.source_head,
        })
        next_snapshot = replace(snapshot, observation=observed)
        signature = await next_snapshot.source_signature(
            current, rules, recheck=AsyncMock(return_value=current),
        )
        assert not signature.universal and signature.identity == current
        assert signature.digest != first.digest
    assert len(snapshot.truth.signatures._entries) == 7


async def test_graph_producer_changes_do_not_reuse_previous_observation(git_signature):
    snapshot, identity, rules, _ = git_signature
    identity = replace(identity, graph_digest="d" * 64, suite="unit")
    graph = GraphObservation(identity, (GraphImpact("src/a.py", ("//a:lib",), ("//a:test",)),))
    check = AsyncMock(return_value=identity)
    first = await snapshot.source_signature(identity, rules, recheck=check, graph=graph)
    missing = await snapshot.source_signature(identity, rules, recheck=check)
    assert first.graph_state == "observational"
    assert missing.graph_state == "unknown" and missing.owning_targets == ()
    assert first.digest != missing.digest
    assert first.paths == missing.paths


async def test_unknown_git_and_cached_only_observations_are_conservative(git_signature, monkeypatch):
    snapshot, identity, rules, _ = git_signature
    check = AsyncMock(return_value=identity)
    cold = await replace(snapshot, cached_only=True).source_signature(identity, rules, recheck=check)
    assert cold.universal and cold.reasons == ("signature_not_cached",)
    monkeypatch.setattr(snapshot.truth.git, "arun_git_result", AsyncMock(side_effect=OSError("lost")))
    assert (await snapshot.source_signature(identity, rules, recheck=check)).universal
    assert not snapshot.truth.signatures._entries
    invalid = replace(identity, source_ref=None)
    assert (await snapshot.source_signature(invalid, rules, recheck=check)).universal


def test_same_invalid_version_in_both_documents_still_falls_back(identity, rules):
    # Rehash both documents to exercise version validation, not just corruption detection.
    import hashlib

    signature = build(identity, rules, "src/a.py")
    artifact, _ = freeze_signature_documents({}, {}, [signature], current_identities=[identity],
                                             selection_reasons=["eligible"])
    for version in (0, 2, True, "1"):
        document = json.loads(json.dumps(artifact))
        envelope = document["train_signatures"]
        envelope["sources"][0] = replace(signature, signature_version=version).to_document()
        envelope["digest"] = hashlib.sha256(json.dumps(
            {k: v for k, v in envelope.items() if k != "digest"},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode()).hexdigest()
        assert read_signature_documents(document, document, current_identities=[identity]) is None


def test_v1_canonical_json_contract_has_stable_digests():
    rules = SignatureRules(("src/a.py",), "generator-v1")
    identity = SignatureIdentity(
        "p", "r", "t", "pr-1", "refs/heads/t", "refs/heads/dev",
        "a" * 40, "b" * 40, "c" * 40, "policy-v1", rules.digest,
    )
    signature = build(identity, rules, "src/a.py")
    artifact, manifest = freeze_signature_documents({}, {}, [signature],
        current_identities=[identity], selection_reasons=["eligible"])
    assert rules.digest == "eeecb34e67fa427cd9ad0c57319346f132c7507b246b2dc7900485da8eca3f46"
    assert signature.digest == "544c32ef7d0a30be551ed2462c811ac37c6959de201ef5c9751ce0af2d0b7661"
    assert artifact == manifest
    assert artifact["train_signatures"]["digest"] == (
        "33fb5625a720ce6b1fbfa3488e7f4fd866fe7c97868f99144e1bea7fd7a522d2"
    )
