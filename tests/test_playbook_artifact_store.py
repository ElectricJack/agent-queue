"""Content-addressed Playbook V2 artifact storage."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from tests.playbook_v2_helpers import twin


def _definition():
    """The smallest artifact the strict Package 2 model accepts.

    This was a hand-rolled dict while Package 2 was in flight; now that
    ``PlaybookDefinition`` exists, ``load`` verifies the strict schema before
    returning (child plan §17.3), so the stored bytes must be a real artifact.
    """
    from src.playbooks.definition import PlaybookDefinition

    return PlaybookDefinition.model_validate(twin())


def test_put_accepts_the_locked_profile_fingerprint_keyword(tmp_path):
    """The Package 3 store keeps its exact locked keyword-only interface."""
    from src.playbooks.artifact_store import ArtifactStore

    store = ArtifactStore(str(tmp_path))
    ref = store.put(
        _definition(),
        source_digest="sha256:" + "a" * 64,
        contract_fingerprint="sha256:" + "b" * 64,
        profile_fingerprint="profile-opaque",
        compiler_build="test-build",
    )

    parameters = inspect.signature(ArtifactStore.put).parameters
    assert list(parameters) == [
        "self",
        "definition",
        "source_digest",
        "contract_fingerprint",
        "profile_fingerprint",
        "compiler_build",
        "version",
    ]
    profile = parameters["profile_fingerprint"]
    assert profile.kind is inspect.Parameter.KEYWORD_ONLY
    assert profile.default is inspect.Parameter.empty
    assert ref.playbook_id == _definition().id


def test_artifact_store_exports_the_canonical_storage_error_family():
    from src.playbooks import artifact_store, run_state

    for name in (
        "ArtifactTooLarge",
        "ArtifactHashCollision",
        "ArtifactVerificationFailed",
    ):
        store_error = getattr(artifact_store, name)
        canonical_error = getattr(run_state, name)
        assert store_error is canonical_error
        assert issubclass(store_error, run_state.PlaybookStorageError)


def test_canonical_bytes_match_package_two():
    from src.playbooks.artifact_store import ArtifactStore
    from src.playbooks.definition import canonical_bytes

    definition = _definition()
    assert ArtifactStore.canonical_bytes(definition) == canonical_bytes(definition)


def test_put_rejects_invalid_definition_before_writing(tmp_path):
    from pydantic import ValidationError

    from src.playbooks.artifact_store import ArtifactStore

    store = ArtifactStore(str(tmp_path))
    with pytest.raises(ValidationError):
        store.put(
            {"id": "not-a-valid-v2-definition"},
            source_digest="sha256:" + "a" * 64,
            contract_fingerprint="sha256:" + "b" * 64,
            profile_fingerprint="profile-opaque",
            compiler_build="test-build",
        )

    assert not (tmp_path / "artifacts").exists()


def test_put_writes_hash_named_canonical_bytes_and_is_idempotent(tmp_path):
    from src.playbooks.artifact_store import ArtifactStore
    from src.playbooks.definition import artifact_sha256, canonical_bytes

    store = ArtifactStore(str(tmp_path))
    definition = _definition()
    ref = store.put(
        definition,
        source_digest="sha256:" + "a" * 64,
        contract_fingerprint="sha256:" + "b" * 64,
        profile_fingerprint="profile-opaque",
        compiler_build="test-build",
    )

    expected = artifact_sha256(definition)
    assert ref.artifact_sha256 == expected
    assert (tmp_path / "artifacts" / f"{expected[7:]}.json").read_bytes() == canonical_bytes(
        definition
    )
    assert store.put(
        definition,
        source_digest="sha256:" + "a" * 64,
        contract_fingerprint="sha256:" + "b" * 64,
        profile_fingerprint="profile-opaque",
        compiler_build="test-build",
    ) == ref


def test_load_verifies_hash_before_parsing_and_rejects_invalid_identifiers(tmp_path):
    from src.playbooks.artifact_store import ArtifactStore, ArtifactVerificationFailed

    store = ArtifactStore(str(tmp_path))
    ref = store.put(
        _definition(),
        source_digest="sha256:" + "a" * 64,
        contract_fingerprint="sha256:" + "b" * 64,
        profile_fingerprint="profile-opaque",
        compiler_build="test-build",
    )
    assert store.load(ref.artifact_sha256) == _definition()
    (tmp_path / "artifacts" / f"{ref.digest}.json").write_text("{}")
    with pytest.raises(ArtifactVerificationFailed):
        store.load(ref.artifact_sha256)
    with pytest.raises(ValueError):
        store.load("../artifact")


# ---------------------------------------------------------------------------
# A-9 — retention sweep, file half (child plan §12).
#
# The database half (reference protection, per-category counts against real
# rows) lives in ``tests/test_playbook_activation.py``, which already carries
# the sqlite/postgres ``db`` fixture.  What is provable without a database is
# proved here: the sweeper's file handling.
# ---------------------------------------------------------------------------


class _StubDb:
    """The retention queries, with recorded arguments and canned answers."""

    def __init__(
        self, *, collected=(), pending=0, pending_expired=0, receipts=0, runs=0, referenced=()
    ):
        self._collected = list(collected)
        self._pending = pending
        self._pending_expired = pending_expired
        self._receipts = receipts
        self._runs = runs
        self._referenced = set(referenced)
        self.calls: dict[str, object] = {}

    async def purge_pending_events(self, now, **kwargs):
        self.calls["purge_pending_events"] = (now, kwargs)
        return SimpleNamespace(expired=self._pending_expired, purged=self._pending)

    async def purge_receipts(self, before, **kwargs):
        self.calls["purge_receipts"] = before
        return self._receipts

    async def purge_runs(self, before, **kwargs):
        self.calls["purge_runs"] = before
        return self._runs

    async def collect_playbook_artifacts(self, before, *, min_versions=10, limit=1000):
        self.calls["collect_playbook_artifacts"] = (before, min_versions)
        return self._collected

    async def list_playbook_activations(self, *, enabled_only=False):
        return []

    async def get_playbook_artifact_path(self, sha):  # pragma: no cover - no rows above
        return None

    async def filter_referenced_artifact_shas(self, shas):
        asked = list(shas)
        self.calls["filter_referenced_artifact_shas"] = asked
        return {sha for sha in asked if sha in self._referenced}


def _sweeper(tmp_path, db, **config_overrides):
    from src.config import PlaybooksConfig
    from src.playbooks.retention import ArtifactRetentionSweeper

    return ArtifactRetentionSweeper(db, PlaybooksConfig(**config_overrides), str(tmp_path))


async def test_retention_removes_stale_temp_files(tmp_path):
    """``*.json.tmp-*`` leftovers age out; a fresh one may be a live write."""
    import os
    import time

    from src.playbooks.retention import TEMP_FILE_TTL_SECONDS

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    now = time.time()
    stale = artifacts / f"{'a' * 64}.json.tmp-1234-deadbeef"
    fresh = artifacts / f"{'b' * 64}.json.tmp-1234-cafebabe"
    keeper = artifacts / f"{'c' * 64}.json"
    for path in (stale, fresh, keeper):
        path.write_text("{}")
    os.utime(stale, (now - TEMP_FILE_TTL_SECONDS - 1, now - TEMP_FILE_TTL_SECONDS - 1))

    counts = await _sweeper(tmp_path, _StubDb()).sweep(now)

    assert counts["temp_files"] == 1
    assert not stale.exists()
    assert fresh.exists() and keeper.exists()


async def test_sweep_returns_counts_per_category(tmp_path):
    """Every category is reported every time, and the horizons come from config."""
    import time

    from src.playbooks.retention import CATEGORIES

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    digest = "d" * 64
    doomed_file = artifacts / f"{digest}.json"
    doomed_file.write_text("{}")

    now = time.time()
    db = _StubDb(
        collected=[(f"sha256:{digest}", str(doomed_file))],
        pending=3,
        pending_expired=2,
        receipts=5,
        runs=2,
    )
    counts = await _sweeper(
        tmp_path,
        db,
        v2_pending_event_retention_days=14,
        v2_receipt_retention_days=30,
        v2_artifact_retention_days=60,
    ).sweep(now)

    assert set(counts) == set(CATEGORIES)
    assert counts["pending_events"] == 3
    assert counts["pending_events_expired"] == 2
    assert counts["receipts"] == 5
    assert counts["runs"] == 2
    assert counts["artifact_rows"] == 1
    assert counts["artifact_files"] == 1
    assert not doomed_file.exists()
    pending_now, pending_kwargs = db.calls["purge_pending_events"]
    assert pending_now == pytest.approx(now)
    assert pending_kwargs["resolved_before"] == pytest.approx(now - 14 * 86400)
    assert db.calls["purge_receipts"] == pytest.approx(now - 30 * 86400)
    assert db.calls["purge_runs"] == pytest.approx(now - 30 * 86400)
    assert db.calls["collect_playbook_artifacts"] == (pytest.approx(now - 60 * 86400), 10)


async def test_sweep_leaves_a_file_whose_row_names_an_unrelated_path(tmp_path):
    """A row whose ``path`` is not its own hash is reported, never unlinked.

    The row is written by this daemon, so a mismatch means something went
    wrong upstream; unlinking on that evidence would let one bad row delete an
    arbitrary file.
    """
    import time

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    innocent = tmp_path / "not-an-artifact.json"
    innocent.write_text("keep me")

    db = _StubDb(collected=[("sha256:" + "e" * 64, str(innocent))])
    counts = await _sweeper(tmp_path, db).sweep(time.time())

    assert counts["artifact_rows"] == 1
    assert counts["artifact_files"] == 0
    assert innocent.exists()


async def test_sweep_survives_a_missing_artifacts_directory(tmp_path):
    """A daemon that has never stored an artifact still sweeps cleanly."""
    import time

    counts = await _sweeper(tmp_path, _StubDb()).sweep(time.time())
    assert counts["temp_files"] == 0


# ---------------------------------------------------------------------------
# Orphan artifact files: the recovery half of the row-then-file ordering.
# ---------------------------------------------------------------------------


async def test_sweep_removes_an_aged_hash_named_file_no_row_references(tmp_path):
    """The file a crashed sweep left behind is found from the directory side.

    ``_unlink_artifacts`` can only ever see the rows the *current* sweep
    deleted, so a file orphaned by an earlier process is invisible to it; this
    is the step that makes §12.1's "the next sweep removes it" true.
    """
    import os
    import time

    from src.playbooks.retention import ORPHAN_FILE_TTL_SECONDS

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    now = time.time()
    orphan = artifacts / f"{'a' * 64}.json"
    orphan.write_text("{}")
    aged = now - ORPHAN_FILE_TTL_SECONDS - 1
    os.utime(orphan, (aged, aged))

    db = _StubDb()
    counts = await _sweeper(tmp_path, db).sweep(now)

    assert counts["orphan_files"] == 1
    assert not orphan.exists()
    assert db.calls["filter_referenced_artifact_shas"] == [f"sha256:{'a' * 64}"]


async def test_sweep_keeps_an_orphan_file_younger_than_the_ttl(tmp_path):
    """``put`` writes bytes before its caller writes the row, so age is the guard."""
    import time

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    fresh = artifacts / f"{'b' * 64}.json"
    fresh.write_text("{}")

    db = _StubDb()
    counts = await _sweeper(tmp_path, db).sweep(time.time())

    assert counts["orphan_files"] == 0
    assert fresh.exists()
    assert "filter_referenced_artifact_shas" not in db.calls


async def test_sweep_keeps_an_aged_file_something_still_references(tmp_path):
    """A hash any of the three tables still names survives regardless of age."""
    import os
    import time

    from src.playbooks.retention import ORPHAN_FILE_TTL_SECONDS

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    now = time.time()
    kept = artifacts / f"{'c' * 64}.json"
    doomed = artifacts / f"{'d' * 64}.json"
    for path in (kept, doomed):
        path.write_text("{}")
        aged = now - ORPHAN_FILE_TTL_SECONDS - 1
        os.utime(path, (aged, aged))

    db = _StubDb(referenced={f"sha256:{'c' * 64}"})
    counts = await _sweeper(tmp_path, db).sweep(now)

    assert counts["orphan_files"] == 1
    assert kept.exists()
    assert not doomed.exists()


async def test_sweep_never_touches_a_file_this_store_did_not_name(tmp_path):
    """Only a bare 64-hex stem is a candidate; nothing else is even asked about."""
    import os
    import time

    from src.playbooks.retention import ORPHAN_FILE_TTL_SECONDS

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    now = time.time()
    aged = now - ORPHAN_FILE_TTL_SECONDS - 1
    strangers = [
        artifacts / "index.json",
        artifacts / f"{'e' * 63}.json",
        artifacts / f"{'F' * 64}.json",
        artifacts / f"{'a' * 64}.txt",
    ]
    for path in strangers:
        path.write_text("{}")
        os.utime(path, (aged, aged))

    db = _StubDb()
    counts = await _sweeper(tmp_path, db).sweep(now)

    assert counts["orphan_files"] == 0
    assert all(path.exists() for path in strangers)
    assert "filter_referenced_artifact_shas" not in db.calls


def test_put_refreshes_the_mtime_of_a_file_it_adopts(tmp_path):
    """Adopting existing bytes must mark the file live for the orphan sweep.

    Content addressing means a re-``put`` of identical bytes does not rewrite
    the file, so without this the file would keep the mtime of its first write
    and the retention sweep could age it out between the adoption and the
    caller's row write.
    """
    import os
    import time

    from src.playbooks.artifact_store import ArtifactStore

    store = ArtifactStore(str(tmp_path))
    definition = _definition()
    kwargs = {
        "source_digest": "sha256:" + "a" * 64,
        "contract_fingerprint": "sha256:" + "b" * 64,
        "profile_fingerprint": "profile-opaque",
        "compiler_build": "test-build",
    }
    ref = store.put(definition, **kwargs)
    path = tmp_path / "artifacts" / f"{ref.digest}.json"
    long_ago = time.time() - 10 * 86400
    os.utime(path, (long_ago, long_ago))

    assert store.put(definition, **kwargs) == ref
    assert path.stat().st_mtime > long_ago
