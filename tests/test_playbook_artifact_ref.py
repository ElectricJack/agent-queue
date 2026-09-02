"""The shared Playbook V2 artifact identity (durable-state child plan §4.2, A-1).

``ArtifactRef`` is the one type both Package 3 branches, Package 4's run
pinning and Package 5's ``ArtifactRefDTO`` agree on, so its validation and its
field names are pinned here rather than left to whichever suite happens to
construct one.
"""

from __future__ import annotations

import pytest

from src.playbooks.artifact_ref import (
    ARTIFACT_SCHEMA_GENERATION,
    SHA256_RE,
    ArtifactRef,
    ArtifactRefError,
)

DIGEST = "a" * 64
CONTRACT = "sha256:" + "b" * 64
SOURCE = "sha256:" + "c" * 64


def _ref(**overrides) -> ArtifactRef:
    fields = {
        "playbook_id": "task-review",
        "artifact_sha256": f"sha256:{DIGEST}",
        "schema_generation": ARTIFACT_SCHEMA_GENERATION,
        "contract_fingerprint": CONTRACT,
        "source_digest": SOURCE,
        "compiler_build": "test-build",
    }
    fields.update(overrides)
    return ArtifactRef(**fields)


def test_rejects_a_bare_digest():
    """The prefix is part of the identity, not decoration.

    A bare digest is what a caller gets from ``hashlib``; accepting it would
    make ``sha256:<hex>`` and ``<hex>`` two spellings of one artifact and
    silently break every equality comparison downstream.
    """
    with pytest.raises(ArtifactRefError):
        _ref(artifact_sha256=DIGEST)


def test_rejects_uppercase_hex():
    """Case is load-bearing: the digest is also the filename."""
    with pytest.raises(ArtifactRefError):
        _ref(artifact_sha256="sha256:" + "A" * 64)
    assert SHA256_RE.fullmatch("sha256:" + "A" * 64) is None


@pytest.mark.parametrize("field", ["contract_fingerprint", "source_digest"])
def test_every_digest_field_is_validated_not_just_the_hash(field):
    with pytest.raises(ArtifactRefError):
        _ref(**{field: "not-a-digest"})


def test_digest_strips_the_prefix():
    assert _ref().digest == DIGEST


def test_rejects_a_foreign_schema_generation():
    with pytest.raises(ArtifactRefError):
        _ref(schema_generation=ARTIFACT_SCHEMA_GENERATION + 1)


def test_rejects_a_missing_playbook_id_or_compiler_build():
    with pytest.raises(ArtifactRefError):
        _ref(playbook_id="")
    with pytest.raises(ArtifactRefError):
        _ref(compiler_build="")


def test_rejects_a_negative_version():
    with pytest.raises(ArtifactRefError):
        _ref(version=-1)


def test_as_dict_field_names_match_package_five():
    """Exactly the eight ``ArtifactRefDTO`` fields, no more and no fewer (§4.6).

    Package 5 projects the ref into its DTO as a copy, not a rename, so a
    field added or renamed here is a wire break there.
    """
    assert set(_ref().as_dict()) == {
        "playbook_id",
        "artifact_sha256",
        "schema_generation",
        "contract_fingerprint",
        "source_digest",
        "compiler_build",
        "compiled_at",
        "version",
    }


def test_as_dict_matches_the_api_dto_field_for_field():
    from src.api.models.playbook_v2 import ArtifactRefDTO

    assert set(_ref().as_dict()) == set(ArtifactRefDTO.model_fields)


def test_from_row_round_trips_a_database_mapping():
    ref = _ref(version=3, compiled_at="2026-09-01T00:00:00Z")
    assert ArtifactRef.from_row(ref.as_dict()) == ref


def test_from_row_coerces_the_integer_columns():
    """SQLite hands back whatever it stored; the ref normalizes it."""
    row = dict(_ref().as_dict(), schema_generation="2", version="7")
    assert ArtifactRef.from_row(row).version == 7


def test_a_ref_is_frozen():
    with pytest.raises((AttributeError, TypeError)):
        _ref().playbook_id = "other"
