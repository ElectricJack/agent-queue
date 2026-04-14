"""Tests for :mod:`src.playbooks.store` — compiled playbook JSON storage.

Tests cover the full CRUD surface of :class:`CompiledPlaybookStore`:

- Path resolution for all four scopes (system, orchestrator, agent_type, project)
- Save / load round-trip fidelity
- Delete semantics
- Listing within a scope and across all scopes
- Change detection (``needs_recompile``)
- Version querying (``get_version``)
- Error handling (corrupt files, missing directories, invalid scopes)
"""

from __future__ import annotations

import json
import os

import pytest

from src.playbooks.models import CompiledPlaybook, PlaybookNode, PlaybookTransition
from src.playbooks.store import COMPILED_SUFFIX, CompiledPlaybookStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeVaultManager:
    """Minimal stand-in for VaultManager — only ``compiled_root`` is needed."""

    def __init__(self, compiled_root: str) -> None:
        self._compiled_root = compiled_root

    @property
    def compiled_root(self) -> str:
        return self._compiled_root


@pytest.fixture()
def compiled_root(tmp_path):
    """Return a fresh temporary directory for compiled playbooks."""
    root = str(tmp_path / "compiled")
    # Do NOT pre-create — the store should create directories on-demand
    return root


@pytest.fixture()
def store(compiled_root):
    """Return a CompiledPlaybookStore backed by a temporary directory."""
    vm = FakeVaultManager(compiled_root)
    return CompiledPlaybookStore(vm)


def _make_playbook(
    playbook_id: str = "test-playbook",
    version: int = 1,
    source_hash: str = "abc123def456",
    triggers: list[str] | None = None,
    scope: str = "system",
    cooldown: int | None = None,
    max_tokens: int | None = None,
) -> CompiledPlaybook:
    """Create a minimal valid CompiledPlaybook for testing."""
    return CompiledPlaybook(
        id=playbook_id,
        version=version,
        source_hash=source_hash,
        triggers=triggers or ["task.completed"],
        scope=scope,
        cooldown_seconds=cooldown,
        max_tokens=max_tokens,
        nodes={
            "start": PlaybookNode(
                entry=True,
                prompt="Analyze the event.",
                transitions=[
                    PlaybookTransition(goto="done", when="analysis complete"),
                ],
            ),
            "done": PlaybookNode(terminal=True),
        },
    )


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    """Tests for _scope_dir and compiled_path."""

    def test_system_scope(self, store, compiled_root):
        path = store.compiled_path("my-playbook", "system")
        assert path == os.path.join(compiled_root, "system", "my-playbook.compiled.json")

    def test_orchestrator_scope(self, store, compiled_root):
        path = store.compiled_path("task-routing", "orchestrator")
        assert path == os.path.join(compiled_root, "orchestrator", "task-routing.compiled.json")

    def test_agent_type_scope(self, store, compiled_root):
        path = store.compiled_path("quality-gate", "agent_type", "coding")
        assert path == os.path.join(
            compiled_root, "agent-types", "coding", "quality-gate.compiled.json"
        )

    def test_project_scope(self, store, compiled_root):
        path = store.compiled_path("deploy-check", "project", "mech-fighters")
        assert path == os.path.join(
            compiled_root, "projects", "mech-fighters", "deploy-check.compiled.json"
        )

    def test_agent_type_requires_identifier(self, store):
        with pytest.raises(ValueError, match="requires an identifier"):
            store.compiled_path("x", "agent_type")

    def test_agent_type_rejects_empty_identifier(self, store):
        with pytest.raises(ValueError, match="requires an identifier"):
            store.compiled_path("x", "agent_type", "")

    def test_project_requires_identifier(self, store):
        with pytest.raises(ValueError, match="requires an identifier"):
            store.compiled_path("x", "project")

    def test_project_rejects_empty_identifier(self, store):
        with pytest.raises(ValueError, match="requires an identifier"):
            store.compiled_path("x", "project", "")

    def test_unknown_scope_raises(self, store):
        with pytest.raises(ValueError, match="Unknown scope"):
            store.compiled_path("x", "galaxy")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


class TestSave:
    """Tests for CompiledPlaybookStore.save."""

    def test_save_creates_directories_and_file(self, store, compiled_root):
        pb = _make_playbook()
        path = store.save(pb, "system")

        assert os.path.isfile(path)
        assert path.endswith(".compiled.json")
        # Verify the directory was created
        assert os.path.isdir(os.path.join(compiled_root, "system"))

    def test_save_returns_expected_path(self, store, compiled_root):
        pb = _make_playbook(playbook_id="deploy")
        path = store.save(pb, "system")
        assert path == os.path.join(compiled_root, "system", "deploy.compiled.json")

    def test_save_writes_valid_json(self, store):
        pb = _make_playbook()
        path = store.save(pb, "system")

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        assert data["id"] == "test-playbook"
        assert data["version"] == 1
        assert data["source_hash"] == "abc123def456"
        assert data["triggers"] == ["task.completed"]
        assert data["scope"] == "system"
        assert "start" in data["nodes"]
        assert "done" in data["nodes"]

    def test_save_trailing_newline(self, store):
        """POSIX compliance: file ends with a newline."""
        pb = _make_playbook()
        path = store.save(pb, "system")

        with open(path, "rb") as f:
            content = f.read()
        assert content.endswith(b"\n")

    def test_save_overwrites_existing(self, store):
        pb_v1 = _make_playbook(version=1, source_hash="hash_v1")
        pb_v2 = _make_playbook(version=2, source_hash="hash_v2")

        store.save(pb_v1, "system")
        path = store.save(pb_v2, "system")

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["version"] == 2
        assert data["source_hash"] == "hash_v2"

    def test_save_agent_type_scope(self, store, compiled_root):
        pb = _make_playbook(playbook_id="lint-check", scope="agent-type:coding")
        path = store.save(pb, "agent_type", "coding")

        assert os.path.isfile(path)
        assert "agent-types/coding" in path.replace("\\", "/")

    def test_save_project_scope(self, store, compiled_root):
        pb = _make_playbook(playbook_id="ci-check", scope="project")
        path = store.save(pb, "project", "my-app")

        assert os.path.isfile(path)
        assert "projects/my-app" in path.replace("\\", "/")

    def test_save_preserves_optional_fields(self, store):
        pb = _make_playbook(cooldown=120, max_tokens=5000)
        path = store.save(pb, "system")

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["cooldown_seconds"] == 120
        assert data["max_tokens"] == 5000

    def test_save_idempotent(self, store):
        """Saving the same playbook twice produces identical results."""
        pb = _make_playbook()
        path1 = store.save(pb, "system")
        path2 = store.save(pb, "system")

        assert path1 == path2
        with open(path1, encoding="utf-8") as f:
            data = json.load(f)
        assert data["id"] == "test-playbook"


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


class TestLoad:
    """Tests for CompiledPlaybookStore.load."""

    def test_load_returns_saved_playbook(self, store):
        pb = _make_playbook(playbook_id="my-pb", version=3, source_hash="aabbcc")
        store.save(pb, "system")

        loaded = store.load("my-pb", "system")
        assert loaded is not None
        assert loaded.id == "my-pb"
        assert loaded.version == 3
        assert loaded.source_hash == "aabbcc"
        assert loaded.triggers == ["task.completed"]

    def test_load_preserves_graph_structure(self, store):
        pb = _make_playbook()
        store.save(pb, "system")

        loaded = store.load("test-playbook", "system")
        assert loaded is not None
        assert "start" in loaded.nodes
        assert "done" in loaded.nodes
        assert loaded.nodes["start"].entry is True
        assert loaded.nodes["done"].terminal is True
        assert len(loaded.nodes["start"].transitions) == 1
        assert loaded.nodes["start"].transitions[0].goto == "done"

    def test_load_nonexistent_returns_none(self, store):
        assert store.load("nonexistent", "system") is None

    def test_load_nonexistent_scope_dir_returns_none(self, store):
        # The compiled/agent-types/coding/ directory doesn't exist
        assert store.load("test", "agent_type", "coding") is None

    def test_load_corrupt_json_returns_none(self, store, compiled_root):
        """Corrupt JSON should return None, not raise."""
        scope_dir = os.path.join(compiled_root, "system")
        os.makedirs(scope_dir, exist_ok=True)
        with open(os.path.join(scope_dir, "bad.compiled.json"), "w") as f:
            f.write("{ this is not valid json }")

        assert store.load("bad", "system") is None

    def test_load_missing_required_field_returns_none(self, store, compiled_root):
        """JSON missing required CompiledPlaybook fields should return None."""
        scope_dir = os.path.join(compiled_root, "system")
        os.makedirs(scope_dir, exist_ok=True)
        # Valid JSON but missing 'id' field
        with open(os.path.join(scope_dir, "incomplete.compiled.json"), "w") as f:
            json.dump({"version": 1, "nodes": {}}, f)

        assert store.load("incomplete", "system") is None

    def test_load_agent_type(self, store):
        pb = _make_playbook(playbook_id="code-review", scope="agent-type:coding")
        store.save(pb, "agent_type", "coding")

        loaded = store.load("code-review", "agent_type", "coding")
        assert loaded is not None
        assert loaded.id == "code-review"

    def test_load_project(self, store):
        pb = _make_playbook(playbook_id="ci-gate", scope="project")
        store.save(pb, "project", "frontend-app")

        loaded = store.load("ci-gate", "project", "frontend-app")
        assert loaded is not None
        assert loaded.id == "ci-gate"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    """Tests for CompiledPlaybookStore.delete."""

    def test_delete_existing_returns_true(self, store):
        pb = _make_playbook()
        store.save(pb, "system")

        assert store.delete("test-playbook", "system") is True

    def test_delete_removes_file(self, store):
        pb = _make_playbook()
        path = store.save(pb, "system")

        store.delete("test-playbook", "system")
        assert not os.path.exists(path)

    def test_delete_nonexistent_returns_false(self, store):
        assert store.delete("nonexistent", "system") is False

    def test_delete_nonexistent_scope_dir_returns_false(self, store):
        assert store.delete("test", "agent_type", "unknown-type") is False

    def test_delete_does_not_remove_other_files(self, store):
        pb1 = _make_playbook(playbook_id="keep-me")
        pb2 = _make_playbook(playbook_id="delete-me")
        store.save(pb1, "system")
        store.save(pb2, "system")

        store.delete("delete-me", "system")

        assert store.load("keep-me", "system") is not None
        assert store.load("delete-me", "system") is None

    def test_delete_after_delete_returns_false(self, store):
        pb = _make_playbook()
        store.save(pb, "system")

        assert store.delete("test-playbook", "system") is True
        assert store.delete("test-playbook", "system") is False


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestListPlaybooks:
    """Tests for CompiledPlaybookStore.list_playbooks."""

    def test_list_empty_scope(self, store):
        assert store.list_playbooks("system") == []

    def test_list_returns_all_in_scope(self, store):
        store.save(_make_playbook(playbook_id="alpha"), "system")
        store.save(_make_playbook(playbook_id="beta"), "system")
        store.save(_make_playbook(playbook_id="gamma"), "system")

        playbooks = store.list_playbooks("system")
        ids = [pb.id for pb in playbooks]
        assert ids == ["alpha", "beta", "gamma"]  # sorted

    def test_list_does_not_cross_scopes(self, store):
        store.save(_make_playbook(playbook_id="sys-pb"), "system")
        store.save(_make_playbook(playbook_id="orch-pb"), "orchestrator")

        sys_pbs = store.list_playbooks("system")
        orch_pbs = store.list_playbooks("orchestrator")
        assert [pb.id for pb in sys_pbs] == ["sys-pb"]
        assert [pb.id for pb in orch_pbs] == ["orch-pb"]

    def test_list_skips_non_compiled_files(self, store, compiled_root):
        store.save(_make_playbook(playbook_id="valid"), "system")
        # Create a non-compiled file in the same directory
        scope_dir = os.path.join(compiled_root, "system")
        with open(os.path.join(scope_dir, "notes.txt"), "w") as f:
            f.write("not a playbook")

        playbooks = store.list_playbooks("system")
        assert len(playbooks) == 1
        assert playbooks[0].id == "valid"

    def test_list_skips_corrupt_files(self, store, compiled_root):
        store.save(_make_playbook(playbook_id="good"), "system")
        # Create a corrupt compiled file
        scope_dir = os.path.join(compiled_root, "system")
        with open(os.path.join(scope_dir, "bad.compiled.json"), "w") as f:
            f.write("not json")

        playbooks = store.list_playbooks("system")
        assert len(playbooks) == 1
        assert playbooks[0].id == "good"

    def test_list_agent_type(self, store):
        store.save(
            _make_playbook(playbook_id="lint", scope="agent-type:coding"),
            "agent_type",
            "coding",
        )
        store.save(
            _make_playbook(playbook_id="test-runner", scope="agent-type:coding"),
            "agent_type",
            "coding",
        )

        playbooks = store.list_playbooks("agent_type", "coding")
        assert [pb.id for pb in playbooks] == ["lint", "test-runner"]

    def test_list_project(self, store):
        store.save(
            _make_playbook(playbook_id="deploy"),
            "project",
            "my-app",
        )

        playbooks = store.list_playbooks("project", "my-app")
        assert len(playbooks) == 1
        assert playbooks[0].id == "deploy"


class TestListAll:
    """Tests for CompiledPlaybookStore.list_all."""

    def test_list_all_empty(self, store):
        assert store.list_all() == []

    def test_list_all_aggregates_scopes(self, store):
        store.save(_make_playbook(playbook_id="sys-health"), "system")
        store.save(_make_playbook(playbook_id="task-route"), "orchestrator")
        store.save(
            _make_playbook(playbook_id="code-gate"),
            "agent_type",
            "coding",
        )
        store.save(
            _make_playbook(playbook_id="ci-check"),
            "project",
            "frontend",
        )

        results = store.list_all()
        assert len(results) == 4

        scopes_and_ids = [(scope, ident, pb.id) for scope, ident, pb in results]
        assert ("system", None, "sys-health") in scopes_and_ids
        assert ("orchestrator", None, "task-route") in scopes_and_ids
        assert ("agent_type", "coding", "code-gate") in scopes_and_ids
        assert ("project", "frontend", "ci-check") in scopes_and_ids

    def test_list_all_multiple_agent_types(self, store):
        store.save(
            _make_playbook(playbook_id="lint"),
            "agent_type",
            "coding",
        )
        store.save(
            _make_playbook(playbook_id="review"),
            "agent_type",
            "code-review",
        )

        results = store.list_all()
        types = [(ident, pb.id) for _, ident, pb in results if _ == "agent_type"]
        assert ("code-review", "review") in types
        assert ("coding", "lint") in types

    def test_list_all_multiple_projects(self, store):
        store.save(_make_playbook(playbook_id="check-a"), "project", "app-a")
        store.save(_make_playbook(playbook_id="check-b"), "project", "app-b")

        results = store.list_all()
        projects = [(ident, pb.id) for _, ident, pb in results if _ == "project"]
        assert ("app-a", "check-a") in projects
        assert ("app-b", "check-b") in projects


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


class TestNeedsRecompile:
    """Tests for CompiledPlaybookStore.needs_recompile."""

    def test_needs_recompile_no_existing(self, store):
        """No compiled version exists → needs recompilation."""
        assert store.needs_recompile("new-pb", "somehash", "system") is True

    def test_needs_recompile_same_hash(self, store):
        """Same hash → no recompilation needed."""
        pb = _make_playbook(source_hash="abc123")
        store.save(pb, "system")

        assert store.needs_recompile("test-playbook", "abc123", "system") is False

    def test_needs_recompile_different_hash(self, store):
        """Different hash → needs recompilation."""
        pb = _make_playbook(source_hash="abc123")
        store.save(pb, "system")

        assert store.needs_recompile("test-playbook", "xyz789", "system") is True

    def test_needs_recompile_agent_type(self, store):
        pb = _make_playbook(playbook_id="lint", source_hash="hash1")
        store.save(pb, "agent_type", "coding")

        assert store.needs_recompile("lint", "hash1", "agent_type", "coding") is False
        assert store.needs_recompile("lint", "hash2", "agent_type", "coding") is True

    def test_needs_recompile_project(self, store):
        pb = _make_playbook(playbook_id="gate", source_hash="h1")
        store.save(pb, "project", "proj")

        assert store.needs_recompile("gate", "h1", "project", "proj") is False
        assert store.needs_recompile("gate", "h2", "project", "proj") is True


class TestGetVersion:
    """Tests for CompiledPlaybookStore.get_version."""

    def test_get_version_no_existing(self, store):
        assert store.get_version("nonexistent", "system") == 0

    def test_get_version_returns_current(self, store):
        pb = _make_playbook(version=5)
        store.save(pb, "system")

        assert store.get_version("test-playbook", "system") == 5

    def test_get_version_after_overwrite(self, store):
        pb_v1 = _make_playbook(version=1)
        pb_v3 = _make_playbook(version=3)
        store.save(pb_v1, "system")
        store.save(pb_v3, "system")

        assert store.get_version("test-playbook", "system") == 3


# ---------------------------------------------------------------------------
# Round-trip fidelity
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Verify full save→load fidelity for various playbook configurations."""

    def test_minimal_playbook(self, store):
        pb = _make_playbook()
        store.save(pb, "system")
        loaded = store.load("test-playbook", "system")

        assert loaded is not None
        assert loaded.id == pb.id
        assert loaded.version == pb.version
        assert loaded.source_hash == pb.source_hash
        assert loaded.triggers == pb.triggers
        assert loaded.scope == pb.scope
        assert loaded.cooldown_seconds == pb.cooldown_seconds
        assert loaded.max_tokens == pb.max_tokens
        assert set(loaded.nodes.keys()) == set(pb.nodes.keys())

    def test_playbook_with_optional_fields(self, store):
        pb = _make_playbook(cooldown=300, max_tokens=10000)
        store.save(pb, "system")
        loaded = store.load("test-playbook", "system")

        assert loaded is not None
        assert loaded.cooldown_seconds == 300
        assert loaded.max_tokens == 10000

    def test_complex_graph(self, store):
        """Multi-node graph with mixed transition types."""
        pb = CompiledPlaybook(
            id="complex",
            version=2,
            source_hash="abcdef1234567890",
            triggers=["git.push", "git.commit"],
            scope="project",
            nodes={
                "scan": PlaybookNode(
                    entry=True,
                    prompt="Run a scan.",
                    transitions=[
                        PlaybookTransition(goto="triage", when="findings exist"),
                        PlaybookTransition(goto="done", otherwise=True),
                    ],
                ),
                "triage": PlaybookNode(
                    prompt="Triage findings.",
                    transitions=[
                        PlaybookTransition(goto="create_tasks", when="has errors"),
                        PlaybookTransition(goto="done", otherwise=True),
                    ],
                ),
                "create_tasks": PlaybookNode(
                    prompt="Create tasks for errors.",
                    goto="done",
                ),
                "done": PlaybookNode(terminal=True),
            },
        )
        store.save(pb, "project", "my-project")
        loaded = store.load("complex", "project", "my-project")

        assert loaded is not None
        assert len(loaded.nodes) == 4
        assert loaded.nodes["scan"].entry is True
        assert len(loaded.nodes["scan"].transitions) == 2
        assert loaded.nodes["scan"].transitions[0].when == "findings exist"
        assert loaded.nodes["scan"].transitions[1].otherwise is True
        assert loaded.nodes["create_tasks"].goto == "done"
        assert loaded.nodes["done"].terminal is True
        assert loaded.triggers == ["git.push", "git.commit"]

    def test_agent_type_scope_round_trip(self, store):
        pb = _make_playbook(
            playbook_id="quality-gate",
            scope="agent-type:coding",
        )
        store.save(pb, "agent_type", "coding")
        loaded = store.load("quality-gate", "agent_type", "coding")

        assert loaded is not None
        assert loaded.scope == "agent-type:coding"

    def test_orchestrator_scope_round_trip(self, store):
        pb = _make_playbook(
            playbook_id="task-assignment",
            scope="system",
        )
        store.save(pb, "orchestrator")
        loaded = store.load("task-assignment", "orchestrator")

        assert loaded is not None
        assert loaded.id == "task-assignment"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Miscellaneous edge-case tests."""

    def test_compiled_suffix_constant(self):
        assert COMPILED_SUFFIX == ".compiled.json"

    def test_playbook_id_with_special_characters(self, store):
        """IDs with hyphens and dots should work fine."""
        pb = _make_playbook(playbook_id="my-cool.playbook-v2")
        path = store.save(pb, "system")
        assert os.path.isfile(path)

        loaded = store.load("my-cool.playbook-v2", "system")
        assert loaded is not None
        assert loaded.id == "my-cool.playbook-v2"

    def test_concurrent_scopes_isolation(self, store):
        """Same playbook ID in different scopes should not collide."""
        pb_sys = _make_playbook(playbook_id="shared-id", version=1)
        pb_proj = _make_playbook(playbook_id="shared-id", version=2)

        store.save(pb_sys, "system")
        store.save(pb_proj, "project", "my-app")

        loaded_sys = store.load("shared-id", "system")
        loaded_proj = store.load("shared-id", "project", "my-app")

        assert loaded_sys is not None
        assert loaded_proj is not None
        assert loaded_sys.version == 1
        assert loaded_proj.version == 2

    def test_save_load_after_delete(self, store):
        """Delete then save a new version — should work cleanly."""
        pb_v1 = _make_playbook(version=1)
        store.save(pb_v1, "system")
        store.delete("test-playbook", "system")

        pb_v2 = _make_playbook(version=2)
        store.save(pb_v2, "system")

        loaded = store.load("test-playbook", "system")
        assert loaded is not None
        assert loaded.version == 2

    def test_list_nonexistent_agent_type(self, store):
        """Listing a nonexistent agent type returns empty list."""
        assert store.list_playbooks("agent_type", "nonexistent-type") == []

    def test_list_nonexistent_project(self, store):
        """Listing a nonexistent project returns empty list."""
        assert store.list_playbooks("project", "nonexistent-project") == []
