"""Tests for src/override_handler — override.md vault watcher handler registration."""

from __future__ import annotations

import logging
import time

import pytest

from src.override_handler import (
    OVERRIDE_PATTERN,
    OverrideChangeInfo,
    derive_override_info,
    on_override_changed,
    register_override_handlers,
)
from src.vault_watcher import VaultChange, VaultWatcher


# ---------------------------------------------------------------------------
# derive_override_info
# ---------------------------------------------------------------------------


class TestDeriveOverrideInfo:
    """Tests for derive_override_info — extracting project_id + agent_type from paths."""

    def test_basic_coding_override(self):
        project_id, agent_type = derive_override_info(
            "projects/mech-fighters/overrides/coding.md"
        )
        assert project_id == "mech-fighters"
        assert agent_type == "coding"

    def test_review_specialist_override(self):
        project_id, agent_type = derive_override_info(
            "projects/my-app/overrides/review-specialist.md"
        )
        assert project_id == "my-app"
        assert agent_type == "review-specialist"

    def test_simple_project_name(self):
        project_id, agent_type = derive_override_info(
            "projects/webapp/overrides/testing.md"
        )
        assert project_id == "webapp"
        assert agent_type == "testing"

    def test_project_name_with_numbers(self):
        project_id, agent_type = derive_override_info(
            "projects/app2/overrides/devops.md"
        )
        assert project_id == "app2"
        assert agent_type == "devops"

    def test_backslash_normalisation(self):
        """Windows-style separators should be handled."""
        project_id, agent_type = derive_override_info(
            "projects\\my-app\\overrides\\coding.md"
        )
        assert project_id == "my-app"
        assert agent_type == "coding"

    def test_raises_on_wrong_prefix(self):
        with pytest.raises(ValueError, match="does not match"):
            derive_override_info("agent-types/coding/overrides/something.md")

    def test_raises_on_missing_overrides_segment(self):
        with pytest.raises(ValueError, match="does not match"):
            derive_override_info("projects/my-app/memory/coding.md")

    def test_raises_on_non_md_file(self):
        with pytest.raises(ValueError, match="does not match"):
            derive_override_info("projects/my-app/overrides/coding.yaml")

    def test_raises_on_too_few_parts(self):
        with pytest.raises(ValueError, match="does not match"):
            derive_override_info("projects/my-app/overrides")

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError, match="does not match"):
            derive_override_info("")


# ---------------------------------------------------------------------------
# OverrideChangeInfo
# ---------------------------------------------------------------------------


class TestOverrideChangeInfo:
    """Tests for the OverrideChangeInfo dataclass."""

    def test_creation(self):
        info = OverrideChangeInfo(
            file_path="/vault/projects/app/overrides/coding.md",
            change_type="created",
            project_id="app",
            agent_type="coding",
        )
        assert info.file_path == "/vault/projects/app/overrides/coding.md"
        assert info.change_type == "created"
        assert info.project_id == "app"
        assert info.agent_type == "coding"

    def test_frozen(self):
        info = OverrideChangeInfo(
            file_path="/vault/projects/app/overrides/coding.md",
            change_type="created",
            project_id="app",
            agent_type="coding",
        )
        with pytest.raises(AttributeError):
            info.project_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# on_override_changed (stub handler)
# ---------------------------------------------------------------------------


class TestOnOverrideChanged:
    """Tests for the stub handler on_override_changed."""

    @pytest.mark.asyncio
    async def test_logs_single_change(self, caplog):
        change = VaultChange(
            path="/home/user/.agent-queue/vault/projects/mech-fighters/overrides/coding.md",
            rel_path="projects/mech-fighters/overrides/coding.md",
            operation="created",
        )
        with caplog.at_level(logging.INFO, logger="src.override_handler"):
            await on_override_changed([change])

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "override.md" in record.message
        assert "created" in record.message
        assert "mech-fighters" in record.message
        assert "coding" in record.message

    @pytest.mark.asyncio
    async def test_logs_multiple_changes(self, caplog):
        changes = [
            VaultChange(
                path="/vault/projects/app/overrides/coding.md",
                rel_path="projects/app/overrides/coding.md",
                operation="created",
            ),
            VaultChange(
                path="/vault/projects/app/overrides/testing.md",
                rel_path="projects/app/overrides/testing.md",
                operation="modified",
            ),
            VaultChange(
                path="/vault/projects/other/overrides/devops.md",
                rel_path="projects/other/overrides/devops.md",
                operation="deleted",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="src.override_handler"):
            await on_override_changed(changes)

        assert len(caplog.records) == 3
        messages = [r.message for r in caplog.records]
        assert any("app" in m and "coding" in m and "created" in m for m in messages)
        assert any("app" in m and "testing" in m and "modified" in m for m in messages)
        assert any("other" in m and "devops" in m and "deleted" in m for m in messages)

    @pytest.mark.asyncio
    async def test_empty_changes_no_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.override_handler"):
            await on_override_changed([])

        assert len(caplog.records) == 0

    @pytest.mark.asyncio
    async def test_handler_logs_project_and_agent_type(self, caplog):
        """Verify project_id and agent_type appear in the log message."""
        change = VaultChange(
            path="/vault/projects/mech-fighters/overrides/review-specialist.md",
            rel_path="projects/mech-fighters/overrides/review-specialist.md",
            operation="modified",
        )
        with caplog.at_level(logging.INFO, logger="src.override_handler"):
            await on_override_changed([change])

        msg = caplog.records[0].message
        assert "project=mech-fighters" in msg
        assert "agent_type=review-specialist" in msg

    @pytest.mark.asyncio
    async def test_handler_warns_on_bad_path(self, caplog):
        """Paths that don't match the expected structure should log a warning."""
        change = VaultChange(
            path="/vault/unexpected/path/file.md",
            rel_path="unexpected/path/file.md",
            operation="created",
        )
        with caplog.at_level(logging.WARNING, logger="src.override_handler"):
            await on_override_changed([change])

        assert len(caplog.records) == 1
        assert "unexpected path structure" in caplog.records[0].message


# ---------------------------------------------------------------------------
# register_override_handlers
# ---------------------------------------------------------------------------


class TestRegisterOverrideHandlers:
    """Tests for register_override_handlers — wiring patterns to VaultWatcher."""

    def test_registers_one_handler(self, tmp_path):
        watcher = VaultWatcher(vault_root=str(tmp_path))
        handler_id = register_override_handlers(watcher)

        assert watcher.get_handler_count() == 1
        assert handler_id == f"override:{OVERRIDE_PATTERN}"

    def test_idempotent_registration(self, tmp_path):
        """Registering twice overwrites the same handler ID."""
        watcher = VaultWatcher(vault_root=str(tmp_path))
        id1 = register_override_handlers(watcher)
        id2 = register_override_handlers(watcher)

        assert id1 == id2
        assert watcher.get_handler_count() == 1


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


class TestPatternMatching:
    """Verify that the registered pattern matches the expected override paths."""

    def test_basic_override_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/mech-fighters/overrides/coding.md",
            OVERRIDE_PATTERN,
        )

    def test_different_project_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/my-app/overrides/testing.md",
            OVERRIDE_PATTERN,
        )

    def test_hyphenated_agent_type_matches(self):
        assert VaultWatcher._matches_pattern(
            "projects/app/overrides/review-specialist.md",
            OVERRIDE_PATTERN,
        )

    def test_non_md_file_does_not_match(self):
        assert not VaultWatcher._matches_pattern(
            "projects/app/overrides/coding.yaml",
            OVERRIDE_PATTERN,
        )

    def test_system_scope_does_not_match(self):
        assert not VaultWatcher._matches_pattern(
            "system/overrides/coding.md",
            OVERRIDE_PATTERN,
        )

    def test_memory_dir_does_not_match(self):
        assert not VaultWatcher._matches_pattern(
            "projects/app/memory/coding.md",
            OVERRIDE_PATTERN,
        )

    def test_agent_types_does_not_match(self):
        assert not VaultWatcher._matches_pattern(
            "agent-types/coding/overrides/something.md",
            OVERRIDE_PATTERN,
        )

    def test_nested_subdir_handled_gracefully(self):
        """Nested paths match fnmatch's ``*`` (crosses ``/``), but
        derive_override_info rejects them with a ValueError — the handler
        logs a warning and skips the file.  This is consistent with how
        fnmatch behaves in the VaultWatcher (see facts_handler.py comment).
        """
        # fnmatch's * does cross path separators, so the pattern matches
        assert VaultWatcher._matches_pattern(
            "projects/app/overrides/nested/coding.md",
            OVERRIDE_PATTERN,
        )
        # But derive_override_info rejects the unexpected structure
        with pytest.raises(ValueError, match="does not match"):
            derive_override_info("projects/app/overrides/nested/coding.md")


# ---------------------------------------------------------------------------
# End-to-end: VaultWatcher detects override file change and dispatches
# ---------------------------------------------------------------------------


class TestEndToEndDispatch:
    """Verify the full pipeline: file change -> VaultWatcher -> handler."""

    @pytest.mark.asyncio
    async def test_detects_and_dispatches_override_created(self, tmp_path):
        """Create an override file and verify the handler is called."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler(OVERRIDE_PATTERN, capture_handler)

        # Take initial snapshot (empty)
        await watcher.check()

        # Create override directory and file
        override_dir = vault / "projects" / "mech-fighters" / "overrides"
        override_dir.mkdir(parents=True)
        (override_dir / "coding.md").write_text("# Coding overrides\n")

        # Detect and dispatch
        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/mech-fighters/overrides/coding.md"
        assert dispatched[0].operation == "created"

    @pytest.mark.asyncio
    async def test_detects_override_modification(self, tmp_path):
        """Modify an existing override file and verify dispatch."""
        vault = tmp_path / "vault"
        override_dir = vault / "projects" / "my-app" / "overrides"
        override_dir.mkdir(parents=True)
        override_file = override_dir / "coding.md"
        override_file.write_text("# Original\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler(OVERRIDE_PATTERN, capture_handler)

        # Initial snapshot includes existing file
        await watcher.check()

        # Modify the file
        time.sleep(0.05)
        override_file.write_text("# Updated overrides\n")

        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/my-app/overrides/coding.md"
        assert dispatched[0].operation == "modified"

    @pytest.mark.asyncio
    async def test_detects_override_deletion(self, tmp_path):
        """Delete an override file and verify dispatch."""
        vault = tmp_path / "vault"
        override_dir = vault / "projects" / "my-app" / "overrides"
        override_dir.mkdir(parents=True)
        override_file = override_dir / "testing.md"
        override_file.write_text("# Testing overrides\n")

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler(OVERRIDE_PATTERN, capture_handler)

        # Initial snapshot includes existing file
        await watcher.check()

        # Delete the file
        override_file.unlink()

        await watcher.check()

        assert len(dispatched) == 1
        assert dispatched[0].rel_path == "projects/my-app/overrides/testing.md"
        assert dispatched[0].operation == "deleted"

    @pytest.mark.asyncio
    async def test_full_handler_with_register(self, tmp_path, caplog):
        """Register via register_override_handlers and verify end-to-end."""
        vault = tmp_path / "vault"
        vault.mkdir()

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        register_override_handlers(watcher)

        # Initial snapshot
        await watcher.check()

        # Create override files in two projects
        (vault / "projects" / "app1" / "overrides").mkdir(parents=True)
        (vault / "projects" / "app1" / "overrides" / "coding.md").write_text(
            "# Coding\n"
        )
        (vault / "projects" / "app2" / "overrides").mkdir(parents=True)
        (vault / "projects" / "app2" / "overrides" / "testing.md").write_text(
            "# Testing\n"
        )

        # Detect and dispatch
        with caplog.at_level(logging.INFO, logger="src.override_handler"):
            await watcher.check()

        handler_logs = [
            r
            for r in caplog.records
            if "override.md" in r.message and "created" in r.message
        ]
        assert len(handler_logs) == 2

    @pytest.mark.asyncio
    async def test_non_override_file_not_dispatched(self, tmp_path):
        """Files outside overrides/ directories should not trigger the handler."""
        vault = tmp_path / "vault"
        (vault / "projects" / "app" / "memory").mkdir(parents=True)

        watcher = VaultWatcher(
            vault_root=str(vault),
            poll_interval=0,
            debounce_seconds=0,
        )

        dispatched: list[VaultChange] = []

        async def capture_handler(changes: list[VaultChange]) -> None:
            dispatched.extend(changes)

        watcher.register_handler(OVERRIDE_PATTERN, capture_handler)
        await watcher.check()

        # Create a non-override file
        (vault / "projects" / "app" / "memory" / "arch.md").write_text("# Arch\n")

        await watcher.check()

        assert len(dispatched) == 0


# ---------------------------------------------------------------------------
# OverrideIndexer — chunking, tagging, stale cleanup and delete
# ---------------------------------------------------------------------------
#
# These exercise the real memsearch chunker: faking ``chunk_markdown`` would
# leave the unit under test replaced by the fake.  Only the Milvus-backed
# store, the collection router and the embedding provider are doubled.
# ---------------------------------------------------------------------------


class _FakeStore:
    """Records what the indexer writes; stands in for a Milvus collection."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = set(existing or ())
        self.upserted: list[list[dict]] = []
        self.deleted: list[list[str]] = []
        self.sources_queried: list[str] = []

    def hashes_by_source(self, source: str) -> set[str]:
        self.sources_queried.append(source)
        return set(self.existing)

    def delete_by_hashes(self, hashes: list[str]) -> int:
        self.deleted.append(list(hashes))
        return len(hashes)

    def upsert(self, records: list[dict]) -> int:
        self.upserted.append(list(records))
        return len(records)


class _FakeRouter:
    def __init__(self, store: _FakeStore | None = None, *, has: bool = True) -> None:
        self.store = store
        self.has = has
        self.get_store_calls: list[tuple] = []
        self.has_store_calls: list[tuple] = []

    def has_store(self, scope, key) -> bool:
        self.has_store_calls.append((scope, key))
        return self.has

    def get_store(self, scope, key, description: str | None = None):
        self.get_store_calls.append((scope, key, description))
        if self.store is None:
            raise AssertionError("get_store must not be called")
        return self.store


class _FakeEmbedder:
    model_name = "fake-embed-v1"

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def embed(self, contents: list[str]) -> list[list[float]]:
        self.batches.append(list(contents))
        return [[float(i), 0.5] for i in range(len(contents))]


_OVERRIDE_MD = """\
# Coding override

Prefer small, reviewable diffs over sweeping rewrites.

## Testing

Run the section's existing tests before opening a PR.

## Style

Match the surrounding code rather than the linter's minimum.
"""


def _make_indexer(router, embedder):
    from src.override_handler import OverrideIndexer

    return OverrideIndexer(router=router, embedder=embedder, max_chunk_size=200, overlap_lines=1)


class TestOverrideIndexer:
    @pytest.mark.asyncio
    async def test_index_override_upserts_chunks_with_override_tag_and_weight(self, tmp_path):
        """Every indexed chunk carries the override tag plus its agent type."""
        pytest.importorskip("memsearch")
        import json

        from memsearch.scoping import MemoryScope

        path = tmp_path / "coding.md"
        path.write_text(_OVERRIDE_MD)
        store = _FakeStore()
        router = _FakeRouter(store)
        embedder = _FakeEmbedder()

        n = await _make_indexer(router, embedder).index_override("proj-x", "coding", str(path))

        assert store.upserted, "nothing was written to the store"
        records = store.upserted[0]
        assert n == len(records)
        for record in records:
            assert json.loads(record["tags"]) == ["override", "coding"]
            assert record["entry_type"] == "document"
            assert record["source"] == str(path.resolve())
            assert record["embedding"]
        assert router.get_store_calls == [(MemoryScope.PROJECT, "proj-x", "project/proj-x")]

    @pytest.mark.asyncio
    async def test_index_override_deletes_stale_chunks_from_same_source(self, tmp_path):
        """Re-indexing removes the previous version's chunks and re-embeds only new ones."""
        pytest.importorskip("memsearch")

        path = tmp_path / "coding.md"
        path.write_text(_OVERRIDE_MD)
        store = _FakeStore()
        embedder = _FakeEmbedder()
        indexer = _make_indexer(_FakeRouter(store), embedder)

        await indexer.index_override("proj-x", "coding", str(path))
        written = [r["chunk_hash"] for r in store.upserted[0]]
        assert len(written) >= 2, "fixture must produce more than one chunk"

        # Second pass: the store already holds the first chunk plus one chunk
        # from an older revision of the same file that no longer exists.
        store.existing = {written[0], "stale-chunk-from-old-revision"}
        store.upserted.clear()
        embedder.batches.clear()

        await indexer.index_override("proj-x", "coding", str(path))

        assert store.deleted == [["stale-chunk-from-old-revision"]]
        assert len(embedder.batches) == 1
        assert len(embedder.batches[0]) == len(written) - 1
        assert [r["chunk_hash"] for r in store.upserted[0]] == written[1:]

    @pytest.mark.asyncio
    async def test_delete_override_does_not_create_collection(self, tmp_path):
        """Deleting from a project with no collection must not create one."""
        pytest.importorskip("memsearch")

        path = tmp_path / "coding.md"
        router = _FakeRouter(store=None, has=False)
        indexer = _make_indexer(router, _FakeEmbedder())

        assert await indexer.delete_override("proj-x", str(path)) is False
        assert router.get_store_calls == []

        store = _FakeStore({"h1", "h2"})
        router = _FakeRouter(store, has=True)
        indexer = _make_indexer(router, _FakeEmbedder())

        assert await indexer.delete_override("proj-x", str(path)) is True
        assert sorted(store.deleted[0]) == ["h1", "h2"]

    @pytest.mark.asyncio
    async def test_on_override_changed_routes_delete_to_indexer_and_survives_raise(
        self, tmp_path, caplog
    ):
        """A raising index call is logged and the remaining changes still run."""
        from src.override_handler import set_indexer

        class _RecordingIndexer:
            def __init__(self) -> None:
                self.indexed: list[tuple[str, str, str]] = []
                self.deleted: list[tuple[str, str]] = []
                self.calls = 0

            async def index_override(self, project_id, agent_type, file_path):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("milvus is down")
                self.indexed.append((project_id, agent_type, file_path))
                return 3

            async def delete_override(self, project_id, file_path):
                self.deleted.append((project_id, file_path))
                return True

        indexer = _RecordingIndexer()
        changes = [
            VaultChange(str(tmp_path / "a.md"), "projects/p/overrides/coding.md", "created"),
            VaultChange(str(tmp_path / "b.md"), "projects/p/overrides/review.md", "deleted"),
            VaultChange(str(tmp_path / "c.md"), "projects/p/overrides/qa.md", "modified"),
        ]

        set_indexer(indexer)
        try:
            with caplog.at_level(logging.ERROR, logger="src.override_handler"):
                await on_override_changed(changes)
        finally:
            set_indexer(None)

        assert indexer.deleted == [("p", str(tmp_path / "b.md"))]
        assert indexer.indexed == [("p", "qa", str(tmp_path / "c.md"))]
        assert "milvus is down" in caplog.text
