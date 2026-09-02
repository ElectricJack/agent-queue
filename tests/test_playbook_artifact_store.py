"""Content-addressed Playbook V2 artifact storage."""

from __future__ import annotations

import hashlib

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


# ---------------------------------------------------------------------------
# A-9 — retention sweep, file half (child plan §12).
#
# The database half (reference protection, per-category counts against real
# rows) lives in ``tests/test_playbook_activation.py``, which already carries
# the sqlite/postgres ``db`` fixture.  What is provable without a database is
# proved here: the sweeper's file handling.
# ---------------------------------------------------------------------------


class _StubDb:
    """The four retention queries, with recorded arguments and canned answers."""

    def __init__(self, *, collected=(), pending=0, receipts=0, runs=0):
        self._collected = list(collected)
        self._pending = pending
        self._receipts = receipts
        self._runs = runs
        self.calls: dict[str, object] = {}

    async def purge_pending_events(self, now, **kwargs):
        self.calls["purge_pending_events"] = now
        return self._pending

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
        receipts=5,
        runs=2,
    )
    counts = await _sweeper(
        tmp_path, db, v2_receipt_retention_days=30, v2_artifact_retention_days=60
    ).sweep(now)

    assert set(counts) == set(CATEGORIES)
    assert counts["pending_events"] == 3
    assert counts["receipts"] == 5
    assert counts["runs"] == 2
    assert counts["artifact_rows"] == 1
    assert counts["artifact_files"] == 1
    assert not doomed_file.exists()
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
