"""Content-addressed Playbook V2 artifact storage."""

from __future__ import annotations

import hashlib

import pytest


def _definition() -> dict[str, object]:
    return {"id": "task-review", "schema_version": 2, "steps": {"a": {"x": 1}}}


def test_put_writes_hash_named_canonical_bytes_and_is_idempotent(tmp_path):
    from src.playbooks.artifact_store import ArtifactStore

    store = ArtifactStore(str(tmp_path))
    definition = _definition()
    ref = store.put(
        definition,
        source_digest="sha256:" + "a" * 64,
        contract_fingerprint="sha256:" + "b" * 64,
        compiler_build="test-build",
    )

    expected = hashlib.sha256(store.canonical_bytes(definition)).hexdigest()
    assert ref.artifact_sha256 == f"sha256:{expected}"
    assert (tmp_path / "artifacts" / f"{expected}.json").read_bytes() == store.canonical_bytes(definition)
    assert store.put(
        definition,
        source_digest="sha256:" + "a" * 64,
        contract_fingerprint="sha256:" + "b" * 64,
        compiler_build="test-build",
    ) == ref


def test_load_verifies_hash_before_parsing_and_rejects_invalid_identifiers(tmp_path):
    from src.playbooks.artifact_store import ArtifactStore, ArtifactVerificationFailed

    store = ArtifactStore(str(tmp_path))
    ref = store.put(
        _definition(),
        source_digest="sha256:" + "a" * 64,
        contract_fingerprint="sha256:" + "b" * 64,
        compiler_build="test-build",
    )
    assert store.load(ref.artifact_sha256) == _definition()
    (tmp_path / "artifacts" / f"{ref.digest}.json").write_text("{}")
    with pytest.raises(ArtifactVerificationFailed):
        store.load(ref.artifact_sha256)
    with pytest.raises(ValueError):
        store.load("../artifact")
