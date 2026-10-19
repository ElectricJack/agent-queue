"""WorktreeSlotManager against real temp repos.

Covers worktree-execution design §2.4 (exclude block), §2.5 (sentinel),
§3.1 (create), §3.2 (reset), §3.5 (fetch-failure tolerance), §3.6
(``worktree_setup``), plus the ``GitManager`` worktree primitives from
implementation spec §4.

Every git-touching test runs against a real repo with a real bare
"origin" — no network, no mocks of git itself.  Paths go through
:mod:`pathlib` so the assertions hold on Windows and WSL2 alike.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import WorktreesConfig
from src.git.manager import ForeignSlotClaim, GitError, GitManager
from src.models import (
    KIND_MODE_WORKTREE,
    RepoSourceType,
    WORKTREE_SENTINEL_NAME,
    Workspace,
    WorkspaceKind,
    WorktreeSentinel,
    worktree_setup_hash,
)
from src.orchestrator.worktree_manager import (
    EXCLUDE_BEGIN,
    EXCLUDE_END,
    WorktreeSlotManager,
    slot_path,
    task_branch_name,
)
from src.orchestrator.execution import ExecutionMixin
from src.orchestrator.workspace_attachments import detach_slot_for_integration_handoff
from tests.git_seed_helpers import clone_seed, init_bare_seed


# ───────────────────────────────── fixtures ──────────────────────────────


def _git(args: list[str], cwd: str | Path) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t.com", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def _create_base_repo(tmp_path: Path) -> Path:
    """A clone of a bare origin, with one commit on ``main``."""
    origin = tmp_path / "origin.git"
    init_bare_seed(str(origin))
    base = tmp_path / "base"
    clone_seed(str(origin), str(base))
    (base / "README.md").write_text("init\n")
    (base / ".gitignore").write_text("node_modules/\n")
    _git(["add", "-A"], cwd=base)
    _git(["commit", "-m", "init"], cwd=base)
    _git(["push", "origin", "main"], cwd=base)
    return base


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
    """Build a fresh clone and bare origin for this test's worktrees."""
    return _create_base_repo(tmp_path)


def test_fresh_repos_do_not_launch_auto_maintenance(tmp_path, monkeypatch):
    trace = tmp_path / "git-trace.json"
    config = tmp_path / "empty-git-config"
    config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    seed = _create_base_repo(seed_root)
    seed_head = _git(["rev-parse", "HEAD"], cwd=seed)
    origin = tmp_path / "origin.git"
    base = _create_base_repo(tmp_path)
    _git(["commit", "--allow-empty", "-m", "private work"], cwd=base)
    _git(["push", "origin", "main"], cwd=base)
    _git(["fetch", "origin"], cwd=base)
    assert _git(["rev-parse", "main"], cwd=seed_root / "origin.git") == seed_head
    assert _git(["rev-parse", "main"], cwd=origin) != seed_head
    commands = [json.loads(line).get("argv", []) for line in trace.read_text().splitlines()]
    assert not [
        args for args in commands
        if "--auto" in args and ("maintenance" in args or "gc" in args)
    ]


class FakeDB:
    """Minimal DB double for workspace operations and optional task checkpoints."""

    def __init__(self):
        self.workspaces: dict[str, Workspace] = {}
        self.contexts: list[dict] = []
        self.meta: dict[tuple[str, str], object] = {}

    async def get_task_meta(self, task_id, key):
        return self.meta.get((task_id, key))

    async def get_task(self, task_id):
        return FakeTask(id=task_id)

    async def set_task_meta(self, task_id, key, value) -> None:
        self.meta[(task_id, key)] = value

    async def list_sessions(self, **kwargs):
        """No sessions at all — every slot branch holder is abandonable.

        ``_detach_stale_branch_holders`` consults this as one of its two
        liveness fences; without it every reset in this module raised
        ``AttributeError`` before reaching what it meant to assert.
        """
        return []

    async def create_workspace(self, ws: Workspace) -> None:
        self.workspaces[ws.id] = ws

    async def get_workspace(self, ws_id: str) -> Workspace | None:
        return self.workspaces.get(ws_id)

    async def list_workspaces(self, project_id: str | None = None) -> list[Workspace]:
        return [
            w
            for w in self.workspaces.values()
            if project_id is None or w.project_id == project_id
        ]

    async def add_task_context(self, task_id, *, type, label, content) -> str:
        self.contexts.append(
            {"task_id": task_id, "type": type, "label": label, "content": content}
        )
        return f"ctx-{len(self.contexts)}"


class RecordingBus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]

    def payload(self, event_type: str) -> dict:
        for t, p in self.events:
            if t == event_type:
                return p
        raise AssertionError(f"{event_type} not emitted; got {self.types()}")


@dataclass
class FakeTask:
    id: str = "tsk-1"
    title: str = "do a thing"
    project_id: str = "p1"


@pytest.fixture
def mutexes():
    locks: dict[str, asyncio.Lock] = {}

    def provider(path: str) -> asyncio.Lock:
        return locks.setdefault(str(path), asyncio.Lock())

    return provider


@pytest.fixture
def db():
    return FakeDB()


@pytest.fixture
def bus():
    return RecordingBus()


@pytest.fixture
def mgr(db, bus, mutexes):
    return WorktreeSlotManager(
        db=db,
        git=GitManager(),
        bus=bus,
        config=WorktreesConfig(enabled=True, setup_timeout_seconds=60),
        git_mutex=mutexes,
        daemon_epoch="2026-08-19T10:00:00Z",
    )


@pytest.fixture
def base_ws(base_repo: Path, db: FakeDB) -> Workspace:
    ws = Workspace(
        id="ws-base",
        project_id="p1",
        workspace_path=str(base_repo),
        source_type=RepoSourceType.CLONE,
        kind_id="project-repo",
    )
    db.workspaces[ws.id] = ws
    return ws


@pytest.fixture
def kind() -> WorkspaceKind:
    return WorkspaceKind(
        project_id="__system__",
        id="project-repo",
        is_git_repo=True,
        mode=KIND_MODE_WORKTREE,
    )


# ──────────────────────────── §2.4 exclude block ─────────────────────────


class TestGitExclude:
    def test_creates_block_when_absent(self, base_repo: Path, mgr):
        assert asyncio.run(mgr.ensure_git_exclude(base_repo)) is True
        text = (base_repo / ".git" / "info" / "exclude").read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        assert EXCLUDE_BEGIN in text and EXCLUDE_END in text
        assert "/.aq/" in text
        # info/exclude lives in the common dir, so this one line keeps the
        # sentinel out of `git status` in every slot.
        assert "/.aq-worktree.json" in text

    def test_is_idempotent(self, base_repo: Path, mgr):
        asyncio.run(mgr.ensure_git_exclude(base_repo))
        exclude = base_repo / ".git" / "info" / "exclude"
        first = exclude.read_bytes()
        assert asyncio.run(mgr.ensure_git_exclude(base_repo)) is False
        assert exclude.read_bytes() == first

    def test_preserves_foreign_content(self, base_repo: Path, mgr):
        exclude = base_repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("# operator's own rules\n*.swp\n")
        asyncio.run(mgr.ensure_git_exclude(base_repo))
        text = exclude.read_text()
        assert "# operator's own rules" in text
        assert "*.swp" in text
        assert "/.aq/" in text

    def test_rewrites_a_drifted_block(self, base_repo: Path, mgr):
        exclude = base_repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        # Written cp1252-ish on purpose: the marker's em-dash must not be what
        # the block is located by, or a foreign-encoded copy gets a *second*
        # block appended on every daemon start.
        exclude.write_bytes(
            f"keep-me\n{EXCLUDE_BEGIN}\n/stale/\n{EXCLUDE_END}\ntail\n".encode(
                "cp1252"
            )
        )
        assert asyncio.run(mgr.ensure_git_exclude(base_repo)) is True
        text = exclude.read_text(encoding="utf-8", errors="surrogateescape")
        assert "/stale/" not in text
        assert "/.aq/" in text
        assert "keep-me" in text and "tail" in text
        assert text.count("# >>> agent-queue managed") == 1

    def test_base_status_stays_clean_after_slot_creation(
        self, base_repo: Path, mgr, base_ws, kind
    ):
        asyncio.run(mgr.create_slot(base_ws, kind, 0))
        status = _git(["status", "--porcelain"], cwd=base_repo)
        assert status == "", f"base repo dirtied by slot creation: {status!r}"

    def test_slot_creation_uses_git_resolved_exclude_path(
        self, tmp_path, db, bus, mutexes, kind
    ):
        origin = tmp_path / "separate-origin.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(origin)],
            check=True,
            capture_output=True,
        )
        base = tmp_path / "separate-base"
        metadata = tmp_path / "separate-metadata"
        subprocess.run(
            ["git", "clone", f"--separate-git-dir={metadata}", str(origin), str(base)],
            check=True,
            capture_output=True,
        )
        (base / "README.md").write_text("init\n")
        _git(["add", "README.md"], cwd=base)
        _git(["commit", "-m", "init"], cwd=base)
        _git(["push", "origin", "main"], cwd=base)
        base_ws = Workspace(
            id="ws-separate",
            project_id="p1",
            workspace_path=str(base),
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
        )
        db.workspaces[base_ws.id] = base_ws
        manager = WorktreeSlotManager(
            db=db,
            git=GitManager(),
            bus=bus,
            config=WorktreesConfig(enabled=True),
            git_mutex=mutexes,
        )

        asyncio.run(manager.create_slot(base_ws, kind, 0))

        assert EXCLUDE_BEGIN in (metadata / "info" / "exclude").read_text()

    def test_slot_creation_rejects_a_git_subdirectory_base(
        self, base_repo: Path, db, bus, mutexes, kind
    ):
        nested = base_repo / "nested"
        nested.mkdir()
        nested_ws = Workspace(
            id="ws-nested",
            project_id="p1",
            workspace_path=str(nested),
            source_type=RepoSourceType.LINK,
            kind_id="project-repo",
        )
        db.workspaces[nested_ws.id] = nested_ws
        manager = WorktreeSlotManager(
            db=db,
            git=GitManager(),
            bus=bus,
            config=WorktreesConfig(enabled=True),
            git_mutex=mutexes,
        )

        with pytest.raises(GitError, match="repository root"):
            asyncio.run(manager.create_slot(nested_ws, kind, 0))

    def test_slot_creation_refuses_unverifiable_exclude(self, base_repo, mgr, base_ws, kind):
        info_dir = base_repo / ".git" / "info"
        (info_dir / "exclude").unlink()
        info_dir.rmdir()
        info_dir.write_text("not a directory\n")

        with pytest.raises(OSError):
            asyncio.run(mgr.create_slot(base_ws, kind, 0))


# ───────────────────────────── §2.5 sentinel ─────────────────────────────


class TestSentinel:
    def test_round_trip_on_disk(self, tmp_path: Path):
        s = WorktreeSentinel(
            slot="slot-2",
            slot_index=2,
            base_workspace_id="ws-base",
            project_id="p1",
            workspace_id="ws-s2",
            task_id="tsk-9",
            branch="aq/tsk-9",
            created_at=1.0,
            assigned_at=2.0,
            daemon_epoch="e",
            setup_hash="h",
        )
        WorktreeSlotManager.write_sentinel(tmp_path, s)
        assert (tmp_path / WORKTREE_SENTINEL_NAME).exists()
        assert WorktreeSlotManager.read_sentinel(tmp_path) == s

    def test_missing_sentinel_reads_none(self, tmp_path: Path):
        assert WorktreeSlotManager.read_sentinel(tmp_path) is None

    def test_corrupt_sentinel_reads_none(self, tmp_path: Path):
        (tmp_path / WORKTREE_SENTINEL_NAME).write_text("{not json")
        assert WorktreeSlotManager.read_sentinel(tmp_path) is None


# ────────────────────────────── §3.1 create ──────────────────────────────


class TestCreateSlot:
    def test_creates_directory_row_and_sentinel(self, mgr, base_ws, kind, db, bus, base_repo):
        slot = asyncio.run(mgr.create_slot(base_ws, kind, 0))

        expected = slot_path(base_repo, 0)
        assert Path(slot.workspace_path) == expected
        assert expected.is_dir()
        assert (expected / "README.md").exists()

        assert slot.slot_index == 0
        assert slot.base_workspace_id == "ws-base"
        assert slot.source_type == RepoSourceType.WORKTREE
        assert slot.kind_id == "project-repo"
        assert db.workspaces[slot.id] is slot

        sentinel = WorktreeSlotManager.read_sentinel(expected)
        assert sentinel is not None
        assert sentinel.slot == "slot-0"
        assert sentinel.slot_index == 0
        assert sentinel.base_workspace_id == "ws-base"
        assert sentinel.workspace_id == slot.id
        assert sentinel.task_id is None  # no branch claimed at creation

        payload = bus.payload("worktree.created")
        assert payload["slot"] == "slot-0"
        assert payload["base_workspace_id"] == "ws-base"

    def test_slot_is_detached_no_branch_claimed(self, mgr, base_ws, kind, base_repo):
        asyncio.run(mgr.create_slot(base_ws, kind, 0))
        head = _git(["symbolic-ref", "-q", "--short", "HEAD"], cwd=base_repo)
        assert head == "main", "base must stay on its branch"
        # The slot itself is detached: symbolic-ref fails, so use rev-parse.
        r = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=str(slot_path(base_repo, 0)),
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0, "slot must be detached at creation"

    def test_registers_with_git_worktree_list(self, mgr, base_ws, kind, base_repo):
        asyncio.run(mgr.create_slot(base_ws, kind, 1))
        entries = asyncio.run(GitManager().aworktree_list(str(base_repo)))
        paths = {Path(e["path"]).resolve() for e in entries}
        assert slot_path(base_repo, 1).resolve() in paths

    def test_ensure_slots_is_lazy_and_idempotent(self, mgr, base_ws, kind, base_repo):
        first = asyncio.run(mgr.ensure_slots(None, base_ws, kind, 2))
        assert [w.slot_index for w in first] == [0, 1]

        again = asyncio.run(mgr.ensure_slots(None, base_ws, kind, 2))
        assert [w.id for w in again] == [w.id for w in first], "must not re-create"

        grown = asyncio.run(mgr.ensure_slots(None, base_ws, kind, 3))
        assert [w.slot_index for w in grown] == [0, 1, 2]
        assert slot_path(base_repo, 2).is_dir()

    def test_setup_commands_run_in_the_slot(self, base_ws, kind, db, bus, mutexes, base_repo):
        marker_kind = WorkspaceKind(
            project_id="__system__",
            id="project-repo",
            mode=KIND_MODE_WORKTREE,
            worktree_setup=["git config aq.setupran yes"],
        )
        m = WorktreeSlotManager(
            db=db,
            git=GitManager(),
            bus=bus,
            config=WorktreesConfig(enabled=True, setup_timeout_seconds=60),
            git_mutex=mutexes,
        )
        asyncio.run(m.create_slot(base_ws, marker_kind, 0))
        got = _git(["config", "--get", "aq.setupran"], cwd=slot_path(base_repo, 0))
        assert got == "yes"
        sentinel = WorktreeSlotManager.read_sentinel(slot_path(base_repo, 0))
        assert sentinel.setup_hash == worktree_setup_hash(marker_kind.worktree_setup)

    def test_failing_setup_command_does_not_abort_creation(
        self, base_ws, db, bus, mutexes, base_repo
    ):
        bad = WorkspaceKind(
            project_id="__system__",
            id="project-repo",
            worktree_setup=["definitely-not-a-real-binary-xyz --nope"],
        )
        m = WorktreeSlotManager(
            db=db,
            git=GitManager(),
            bus=bus,
            config=WorktreesConfig(enabled=True, setup_timeout_seconds=30),
            git_mutex=mutexes,
        )
        slot = asyncio.run(m.create_slot(base_ws, bad, 0))
        assert Path(slot.workspace_path).is_dir()


class TestAbandonedMergeRecovery:
    async def _merge(self, mgr, base_ws, kind, *, conflicted=False):
        slot = await mgr.create_slot(base_ws, kind, 0)
        await mgr.reset_slot_for_task(slot, FakeTask(id="tsk-prev"))
        path = Path(slot.workspace_path)
        _git(["switch", "-c", "merge-input"], path)
        (path / "README.md").write_text("incoming merge edit\n")
        (path / "binary.bin").write_bytes(b"\x00merge\xff\n")
        _git(["add", "-A"], path)
        _git(["commit", "-m", "merge input"], path)
        _git(["switch", "aq/tsk-prev"], path)
        if conflicted:
            (path / "README.md").write_text("predecessor edit\n")
            _git(["add", "README.md"], path)
            _git(["commit", "-m", "predecessor"], path)
        result = await mgr.git._arun_subprocess(
            ["git", "merge", "--no-ff", "--no-commit", "merge-input"], cwd=str(path),
        )
        assert result.returncode == (1 if conflicted else 0), result.stderr
        merge_path = Path(await mgr.git.aget_git_path(str(path), "MERGE_HEAD"))
        assert merge_path.exists()
        return slot, path, merge_path

    @pytest.mark.parametrize("conflicted", [False, True])
    async def test_preparation_archives_merge_before_abort(
        self, mgr, base_ws, kind, db, conflicted,
    ):
        slot, path, merge_path = await self._merge(mgr, base_ws, kind, conflicted=conflicted)
        # Diagnostics are mandatory even when normal dirty salvage is off.
        mgr.config.salvage_dirty = False
        original_head = _git(["rev-parse", "HEAD"], path)
        await mgr.ensure_slot_checkout(slot, FakeTask(id="tsk-next"))
        assert not merge_path.exists()
        assert _git(["rev-parse", "HEAD"], path) == original_head
        diagnostic = db.contexts[-1]
        assert diagnostic["task_id"] == "tsk-prev"
        assert diagnostic["type"] == "worktree_salvage"
        assert "# MERGE_HEAD:" in diagnostic["content"]
        assert "incoming merge edit" in diagnostic["content"]
        assert "GIT binary patch" in diagnostic["content"]
        assert ("# Unmerged:" in diagnostic["content"]) == conflicted
        if conflicted:
            assert "<<<<<<< HEAD" in diagnostic["content"]
        applied = subprocess.run(
            ["git", "apply", "--check", "-"], cwd=path, input=diagnostic["content"],
            text=True, capture_output=True,
        )
        assert applied.returncode == 0, applied.stderr
        await mgr.reset_slot_for_task(slot, FakeTask(id="tsk-next"))
        assert _git(["status", "--porcelain"], path) == ""

    async def test_stale_branch_holder_merge_is_archived_and_detached(
        self, mgr, base_ws, kind, db, monkeypatch,
    ):
        holder, path, merge_path = await self._merge(mgr, base_ws, kind, conflicted=True)
        destination = await mgr.create_slot(base_ws, kind, 1)
        monkeypatch.setattr(mgr, "_slot_is_live", AsyncMock(return_value=False))
        branch = await mgr.reset_slot_for_task(destination, FakeTask(id="tsk-prev"))
        assert branch == "aq/tsk-prev"
        assert not merge_path.exists()
        assert _git(["branch", "--show-current"], path) == ""
        assert "<<<<<<< HEAD" in db.contexts[-1]["content"]
        assert db.contexts[-1]["task_id"] == "tsk-prev"

    async def test_checkpoint_restore_clears_abandoned_merge(
        self, mgr, base_ws, kind,
    ):
        from src.orchestrator.task_checkpoint import capture_checkpoint

        slot = await mgr.create_slot(base_ws, kind, 0)
        await mgr.reset_slot_for_task(slot, FakeTask(id="tsk-paused"))
        path = Path(slot.workspace_path)
        (path / "saved.txt").write_text("pause snapshot\n")
        await capture_checkpoint(mgr.db, mgr.git, "tsk-paused", str(path))
        _git(["clean", "-fd", "-e", WORKTREE_SENTINEL_NAME], path)
        _git(["switch", "-c", "merge-input"], path)
        (path / "merge.txt").write_text("abandoned merge\n")
        _git(["add", "merge.txt"], path)
        _git(["commit", "-m", "merge input"], path)
        _git(["switch", "aq/tsk-paused"], path)
        _git(["merge", "--no-ff", "--no-commit", "merge-input"], path)
        await mgr.reset_slot_for_task(slot, FakeTask(id="tsk-paused"))
        assert (path / "saved.txt").read_text() == "pause snapshot\n"
        assert not (path / "merge.txt").exists()
        assert not Path(await mgr.git.aget_git_path(str(path), "MERGE_HEAD")).exists()
        assert "abandoned merge" in mgr.db.contexts[-1]["content"]

    @pytest.mark.parametrize("fence", ["locked", "live", "claim", "retained", "process"])
    async def test_stale_holder_fences_preserve_merge(
        self, mgr, base_ws, kind, db, monkeypatch, fence,
    ):
        holder, path, merge_path = await self._merge(mgr, base_ws, kind, conflicted=True)
        destination = await mgr.create_slot(base_ws, kind, 1)
        index_path = Path(await mgr.git.aget_git_path(str(path), "index"))
        before = index_path.read_bytes(), (path / "README.md").read_bytes(), merge_path.read_bytes()
        if fence == "locked":
            holder.locked_by_agent_id = "foreign-agent"
        elif fence == "live":
            monkeypatch.setattr(db, "list_sessions", AsyncMock(return_value=[
                SimpleNamespace(work_dir=str(path)),
            ]))
        elif fence == "claim":
            from src.claim_file import write_claim_file

            write_claim_file(path, {"task_id": "foreign", "session_id": "foreign", "claim_epoch": 1})
            monkeypatch.setattr(
                "src.orchestrator.workspace_claim_recovery.retire_stopped_slot_claim",
                AsyncMock(return_value=False),
            )
        elif fence == "retained":
            monkeypatch.setattr(mgr, "_slot_has_retained_binding", AsyncMock(
                side_effect=lambda workspace_id: workspace_id == holder.id,
            ))
        else:
            monkeypatch.setattr(mgr, "_slot_is_live", AsyncMock(return_value=True))
        with pytest.raises(GitError):
            await mgr.reset_slot_for_task(destination, FakeTask(id="tsk-prev"))
        assert before == (
            index_path.read_bytes(), (path / "README.md").read_bytes(), merge_path.read_bytes(),
        )
        assert db.contexts == []

    @pytest.mark.parametrize("live", [True, False])
    async def test_acquired_slot_only_allows_its_preparing_session(
        self, mgr, base_ws, kind, db, monkeypatch, live,
    ):
        slot, path, merge_path = await self._merge(mgr, base_ws, kind)
        slot.locked_by_task_id = "tsk-next"
        slot.locked_by_agent_id = "preparing-agent"
        monkeypatch.setattr(db, "list_sessions", AsyncMock(return_value=[SimpleNamespace(
            work_dir=str(path), task_id="tsk-next", agent_id="preparing-agent",
            claim_phase="active" if live else "preparing",
        )]))
        if live:
            with pytest.raises(GitError, match="live holder"):
                await mgr.ensure_slot_checkout(slot, FakeTask(id="tsk-next"))
            assert merge_path.exists()
            assert db.contexts == []
        else:
            await mgr.ensure_slot_checkout(slot, FakeTask(id="tsk-next"))
            assert not merge_path.exists()

    async def test_acquired_slot_preserves_foreign_agent_lock(
        self, mgr, base_ws, kind, db,
    ):
        slot, _, merge_path = await self._merge(mgr, base_ws, kind)
        slot.locked_by_agent_id = "foreign-agent"
        with pytest.raises(GitError, match="foreign agent lock"):
            await mgr.ensure_slot_checkout(slot, FakeTask(id="tsk-next"))
        assert merge_path.exists()
        assert db.contexts == []

    async def test_failed_archive_preserves_index_and_merge(
        self, mgr, base_ws, kind, db, monkeypatch,
    ):
        slot, path, merge_path = await self._merge(mgr, base_ws, kind, conflicted=True)
        index_path = Path(await mgr.git.aget_git_path(str(path), "index"))
        before = index_path.read_bytes(), (path / "README.md").read_bytes(), merge_path.read_bytes()
        monkeypatch.setattr(db, "add_task_context", AsyncMock(side_effect=RuntimeError("archive down")))
        with pytest.raises(RuntimeError, match="archive down"):
            await mgr.ensure_slot_checkout(slot, FakeTask(id="tsk-next"))
        assert before == (
            index_path.read_bytes(), (path / "README.md").read_bytes(), merge_path.read_bytes(),
        )

    async def test_operator_handoff_preserves_dirty_merge_destination(
        self, mgr, base_ws, kind, db,
    ):
        slot, path, merge_path = await self._merge(mgr, base_ws, kind, conflicted=True)
        index_path = Path(await mgr.git.aget_git_path(str(path), "index"))
        before = index_path.read_bytes(), (path / "README.md").read_bytes(), merge_path.read_bytes()
        with pytest.raises(GitError, match="dirty or unproved"):
            await mgr.reset_slot_for_task(
                slot, FakeTask(id="tsk-next"), operator_handoff=True,
                base_branch=_git(["rev-parse", "HEAD"], path), target_branch="aq/tsk-next",
            )
        assert before == (
            index_path.read_bytes(), (path / "README.md").read_bytes(), merge_path.read_bytes(),
        )
        assert db.contexts == []

    @pytest.mark.parametrize("stale_holder", [False, True])
    async def test_unknown_predecessor_diagnostic_belongs_to_incoming_task(
        self, mgr, base_ws, kind, db, monkeypatch, stale_holder,
    ):
        slot, path, merge_path = await self._merge(mgr, base_ws, kind)
        sentinel = mgr.read_sentinel(path)
        sentinel.task_id = None
        mgr.write_sentinel(path, sentinel)
        if stale_holder:
            destination = await mgr.create_slot(base_ws, kind, 1)
            monkeypatch.setattr(mgr, "_slot_is_live", AsyncMock(return_value=False))
            await mgr.reset_slot_for_task(destination, FakeTask(id="tsk-prev"))
            incoming = "tsk-prev"
        else:
            await mgr.ensure_slot_checkout(slot, FakeTask(id="tsk-next"))
            incoming = "tsk-next"
        assert not merge_path.exists()
        assert db.contexts[-1]["task_id"] == incoming

    async def test_oversized_merge_records_explicit_diffstat_diagnostic(
        self, mgr, base_ws, kind, db,
    ):
        slot, _, merge_path = await self._merge(mgr, base_ws, kind)
        mgr.config.salvage_max_bytes = 1
        await mgr.ensure_slot_checkout(slot, FakeTask(id="tsk-next"))
        assert not merge_path.exists()
        content = db.contexts[-1]["content"]
        assert "working-tree contents were NOT archived" in content
        assert "README.md" in content and "binary.bin" in content
        assert "GIT binary patch" not in content

    async def test_failed_abort_returns_error_with_archived_diagnostic(
        self, mgr, base_ws, kind, db, monkeypatch,
    ):
        slot, _, merge_path = await self._merge(mgr, base_ws, kind)
        run = mgr.git._arun_unlocked

        async def fail_abort(args, cwd=None, timeout=None):
            if args == ["merge", "--abort"]:
                raise GitError("abort failed")
            return await run(args, cwd, timeout)

        monkeypatch.setattr(mgr.git, "_arun_unlocked", fail_abort)
        with pytest.raises(GitError, match="abort failed"):
            await mgr.ensure_slot_checkout(slot, FakeTask(id="tsk-next"))
        assert merge_path.exists()
        assert "incoming merge edit" in db.contexts[-1]["content"]


class TestOperatorHandoffCheckpoint:
    async def _checkpoint(self, mgr, base_ws, kind, *, dirty=False):
        from src.orchestrator.task_checkpoint import CHECKPOINT_META, capture_checkpoint

        slot = await mgr.create_slot(base_ws, kind, 0)
        await mgr.reset_slot_for_task(slot, FakeTask())
        path = Path(slot.workspace_path)
        if dirty:
            (path / "staged.txt").write_text("saved stage\n")
            _git(["add", "staged.txt"], path)
            (path / "staged.txt").write_text("saved unstaged\n")
            (path / "binary.bin").write_bytes(b"\x00saved\xff")
        await capture_checkpoint(mgr.db, mgr.git, "tsk-1", str(path))
        saved = await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META)
        _git(["reset", "--hard"], path)
        _git(["clean", "-fd", "-e", WORKTREE_SENTINEL_NAME], path)
        return slot, path, saved

    def _preserved_commit(self, path, head, *, filename="preserved.txt", content="operator work\n"):
        _git(["switch", "--detach", head], path)
        (path / filename).write_text(content)
        _git(["add", filename], path)
        _git(["commit", "-m", "operator preserved work"], path)
        _git(["push", "origin", "HEAD:refs/heads/aq/preserved/owner"], path)
        return _git(["rev-parse", "HEAD"], path)

    async def _restore(self, mgr, slot, sha, **kwargs):
        return await mgr.reset_slot_for_task(
            slot, FakeTask(), base_branch=sha, target_branch="aq/tsk-1",
            operator_handoff=True, **kwargs,
        )

    @pytest.mark.parametrize("owned_branch", ["aq/tsk-1", "aq/epic/owned"])
    @pytest.mark.parametrize("preserve", [False, True])
    @pytest.mark.parametrize("orphan", [False, True])
    async def test_owned_checkpoint_ancestor_restores_from_foreign_checkout(
        self, mgr, base_ws, kind, owned_branch, preserve, orphan,
    ):
        from src.orchestrator.task_checkpoint import CHECKPOINT_META

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind, dirty=True)
        _git(["switch", "-C", owned_branch, saved["head"]], path)
        (path / "owned.txt").write_text("owned progress\n")
        _git(["add", "owned.txt"], path)
        _git(["commit", "-m", "advance owned branch"], path)
        tip = _git(["rev-parse", "HEAD"], path)
        _git(["push", "origin", owned_branch], path)
        _git(["switch", "--orphan", "aq/foreign"] if orphan else
             ["switch", "-c", "aq/foreign", saved["head"]], path)
        (path / "foreign.txt").write_text("other task\n")
        _git(["add", "foreign.txt"], path)
        _git(["commit", "-m", "foreign slot head"], path)
        foreign = _git(["rev-parse", "HEAD"], path)

        branch = await mgr.reset_slot_for_task(
            slot, FakeTask(), target_branch=owned_branch,
            base_branch=saved["head"] if preserve else tip, preserve_branch=preserve,
        )

        assert branch == owned_branch
        assert _git(["branch", "--show-current"], path) == owned_branch
        assert _git(["rev-parse", "HEAD"], path) == tip
        assert (path / "owned.txt").read_text() == "owned progress\n"
        assert not (path / "foreign.txt").exists()
        assert _git(["rev-parse", "aq/foreign"], path) == foreign
        assert _git(["show", ":staged.txt"], path) == "saved stage"
        assert (path / "staged.txt").read_text() == "saved unstaged\n"
        assert (path / "binary.bin").read_bytes() == b"\x00saved\xff"
        assert await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META) == saved
        assert _git(["rev-parse", saved["ref"]], path) == saved["commit"]

    @pytest.mark.parametrize("advanced", [False, True])
    @pytest.mark.parametrize("published", [False, True])
    async def test_owned_checkpoint_restores_missing_local_ref_from_origin(
        self, mgr, base_ws, kind, advanced, published,
    ):
        from src.orchestrator.task_checkpoint import CHECKPOINT_META

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind)
        branch = "aq/epic/remote-owned"
        _git(["switch", "-c", branch], path)
        if advanced:
            (path / "owned.txt").write_text("published progress\n")
            _git(["add", "owned.txt"], path)
            _git(["commit", "-m", "published owned progress"], path)
        tip = _git(["rev-parse", "HEAD"], path)
        _git(["push", "origin", branch], path)
        _git(["switch", "-c", "aq/foreign", saved["head"]], path)
        _git(["branch", "-D", branch], path)
        _git(["update-ref", "-d", f"refs/remotes/origin/{branch}"], path)
        if not published:
            _git(["push", "origin", "--delete", branch], path)
            _git(["update-ref", f"refs/remotes/origin/{branch}", tip], path)
        # The foreign branch is already merged into the preserved base.
        task = FakeTask()
        task.branch_name = None

        if not published:
            with pytest.raises(GitError, match="no proved continuation"):
                await mgr.reset_slot_for_task(
                    slot, task, target_branch=branch,
                    base_branch=saved["head"], preserve_branch=True,
                )
            assert _git(["branch", "--show-current"], path) == "aq/foreign"
            assert _git(["rev-parse", "HEAD"], path) == saved["head"]
            assert await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META) == saved
            return

        restored = await mgr.reset_slot_for_task(
            slot, task, target_branch=branch, base_branch=saved["head"], preserve_branch=True,
        )

        assert restored == branch
        assert _git(["branch", "--show-current"], path) == branch
        assert _git(["rev-parse", "HEAD"], path) == tip
        assert _git(["status", "--porcelain"], path) == ""
        if advanced:
            assert (path / "owned.txt").read_text() == "published progress\n"
        assert await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META) == saved

    @pytest.mark.parametrize("unproved", [False, True])
    async def test_owned_checkpoint_refusal_names_compared_heads_and_preserves_slot(
        self, mgr, base_ws, kind, monkeypatch, unproved,
    ):
        from src.orchestrator.task_checkpoint import CHECKPOINT_META, capture_checkpoint

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind)
        _git(["branch", "aq/owned", saved["head"]], path)
        (path / "checkpoint-only.txt").write_text("checkpoint branch progress\n")
        _git(["add", "checkpoint-only.txt"], path)
        _git(["commit", "-m", "checkpoint branch diverges"], path)
        await capture_checkpoint(mgr.db, mgr.git, "tsk-1", str(path))
        saved = await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META)
        _git(["switch", "-c", "aq/foreign", "aq/owned"], path)
        slot_head = _git(["rev-parse", "HEAD"], path)
        (path / "keep.txt").write_text("destination edits\n")
        if unproved:
            monkeypatch.setattr(mgr.git, "ais_ancestor", AsyncMock(return_value=None))
        before = _git(["status", "--porcelain"], path)

        with pytest.raises(GitError) as caught:
            await mgr.reset_slot_for_task(
                slot, FakeTask(), target_branch="aq/owned", base_branch=slot_head,
            )

        reason = str(caught.value)
        for fact in (saved["head"], saved["commit"], "aq/owned", slot_head, "slot HEAD", "aq/foreign"):
            assert fact in reason
        assert _git(["status", "--porcelain"], path) == before
        assert _git(["branch", "--show-current"], path) == "aq/foreign"
        assert (path / "keep.txt").read_text() == "destination edits\n"
        assert await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META) == saved

    @pytest.mark.parametrize("dirty", [False, True])
    async def test_old_checkpoint_keeps_preserved_commit_and_saved_dirty_layers(
        self, mgr, base_ws, kind, dirty
    ):
        from src.orchestrator.task_checkpoint import CHECKPOINT_META

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind, dirty=dirty)
        sha = self._preserved_commit(path, saved["head"])
        assert await self._restore(mgr, slot, sha) == "aq/tsk-1"
        assert _git(["rev-parse", "HEAD"], path) == sha
        assert (path / "preserved.txt").read_text() == "operator work\n"
        if dirty:
            assert _git(["show", ":staged.txt"], path) == "saved stage"
            assert (path / "staged.txt").read_text() == "saved unstaged\n"
            assert (path / "binary.bin").read_bytes() == b"\x00saved\xff"
            assert "preserved.txt" not in _git(["diff", "HEAD", "--name-only"], path)
        else:
            assert _git(["status", "--porcelain"], path) == ""
        assert await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META) == saved
        assert _git(["rev-parse", saved["ref"]], path) == saved["commit"]

    async def test_newer_saved_head_and_dirty_work_survive_older_handoff(
        self, mgr, base_ws, kind
    ):
        from src.orchestrator.task_checkpoint import capture_checkpoint

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind)
        sha = self._preserved_commit(path, saved["head"])
        _git(["switch", "-C", "aq/tsk-1", sha], path)
        (path / "newer.txt").write_text("later committed work\n")
        _git(["add", "newer.txt"], path)
        _git(["commit", "-m", "later"], path)
        newer = _git(["rev-parse", "HEAD"], path)
        (path / "newer.txt").write_text("later staged work\n")
        _git(["add", "newer.txt"], path)
        (path / "newer.txt").write_text("later unstaged work\n")
        await capture_checkpoint(mgr.db, mgr.git, "tsk-1", str(path))
        _git(["reset", "--hard"], path)
        _git(["switch", "--detach", sha], path)

        await self._restore(mgr, slot, sha)

        assert _git(["rev-parse", "HEAD"], path) == newer
        assert _git(["show", ":newer.txt"], path) == "later staged work"
        assert (path / "newer.txt").read_text() == "later unstaged work\n"

    async def test_newer_local_branch_commit_is_retained(self, mgr, base_ws, kind):
        slot, path, saved = await self._checkpoint(mgr, base_ws, kind, dirty=True)
        (path / "later.txt").write_text("local committed progress\n")
        _git(["add", "later.txt"], path)
        _git(["commit", "-m", "local progress"], path)
        newer = _git(["rev-parse", "HEAD"], path)
        _git(["switch", "--detach", saved["head"]], path)

        await self._restore(mgr, slot, saved["head"])

        assert _git(["rev-parse", "HEAD"], path) == newer
        assert (path / "later.txt").read_text() == "local committed progress\n"
        assert (path / "staged.txt").read_text() == "saved unstaged\n"

    async def test_checkpoint_changes_already_committed_by_handoff_are_coalesced(
        self, mgr, base_ws, kind
    ):
        from src.orchestrator.task_checkpoint import capture_checkpoint

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind)
        (path / "README.md").write_text("saved edit\n")
        _git(["add", "README.md"], path)
        await capture_checkpoint(mgr.db, mgr.git, "tsk-1", str(path))
        _git(["reset", "--hard"], path)
        sha = self._preserved_commit(
            path, saved["head"], filename="README.md", content="saved edit\n"
        )
        await self._restore(mgr, slot, sha)
        assert _git(["rev-parse", "HEAD"], path) == sha
        assert (path / "README.md").read_text() == "saved edit\n"
        assert _git(["status", "--porcelain"], path) == ""

    @pytest.mark.parametrize("corruption", ["ref", "index_tree"])
    async def test_ambiguous_checkpoint_identity_is_refused(
        self, mgr, base_ws, kind, corruption
    ):
        from src.orchestrator.task_checkpoint import CHECKPOINT_META

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind, dirty=True)
        sha = self._preserved_commit(path, saved["head"])
        if corruption == "ref":
            _git(["update-ref", saved["ref"], sha], path)
        else:
            saved = saved | {"index_tree": _git(["rev-parse", "HEAD^{tree}"], path)}
            await mgr.db.set_task_meta("tsk-1", CHECKPOINT_META, saved)
        with pytest.raises(GitError, match="checkpoint (ref changed|identity is ambiguous)"):
            await self._restore(mgr, slot, sha)
        assert _git(["rev-parse", "HEAD"], path) == sha
        assert _git(["status", "--porcelain"], path) == ""
        assert await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META) == saved
        assert _git(["for-each-ref", "refs/aq/task-restores"], path) == ""

    async def test_live_branch_holder_is_never_detached(
        self, mgr, base_ws, kind, monkeypatch
    ):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        _, path, saved = await self._checkpoint(mgr, base_ws, kind)
        sha = self._preserved_commit(path, saved["head"])
        _git(["switch", "aq/tsk-1"], path)
        (path / "live.txt").write_text("live worker progress\n")
        destination = await mgr.create_slot(base_ws, kind, 1)
        monkeypatch.setattr(mgr.db, "list_sessions", AsyncMock(return_value=[
            SimpleNamespace(work_dir=str(path)),
        ]))
        with pytest.raises(GitError, match="already used by worktree"):
            await self._restore(mgr, destination, sha)
        assert _git(["branch", "--show-current"], path) == "aq/tsk-1"
        assert (path / "live.txt").read_text() == "live worker progress\n"
        assert _git(["status", "--porcelain"], destination.workspace_path) == ""

    async def test_unknown_ancestry_refuses_before_reset(
        self, mgr, base_ws, kind, monkeypatch
    ):
        from unittest.mock import AsyncMock

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind)
        sha = self._preserved_commit(path, saved["head"])
        monkeypatch.setattr(mgr.git, "ais_ancestor", AsyncMock(return_value=None))
        with pytest.raises(GitError, match="unproved.*unchanged"):
            await self._restore(mgr, slot, sha)
        assert _git(["rev-parse", "HEAD"], path) == sha
        assert _git(["branch", "--show-current"], path) == ""
        assert _git(["status", "--porcelain"], path) == ""

    @pytest.mark.parametrize("unknown", ["status", "branch_tip"])
    async def test_unknown_destination_proof_refuses_before_reset(
        self, mgr, base_ws, kind, monkeypatch, unknown
    ):
        from unittest.mock import AsyncMock

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind)
        sha = self._preserved_commit(path, saved["head"])
        if unknown == "status":
            monkeypatch.setattr(mgr.git, "aget_dirty_paths", AsyncMock(return_value=None))
        else:
            run = mgr.git._arun

            async def unreadable_tip(args, **kwargs):
                if args[0] == "for-each-ref":
                    raise GitError("branch tip read failed")
                return await run(args, **kwargs)

            monkeypatch.setattr(mgr.git, "_arun", unreadable_tip)
        with pytest.raises(GitError):
            await self._restore(mgr, slot, sha)
        assert _git(["rev-parse", "HEAD"], path) == sha
        assert _git(["branch", "--show-current"], path) == ""
        assert _git(["status", "--porcelain"], path) == ""

    async def test_sibling_fetch_cannot_replace_the_checkpoint_identity_proof(
        self, mgr, base_ws, kind, monkeypatch
    ):
        slot, path, saved = await self._checkpoint(mgr, base_ws, kind, dirty=True)
        sha = self._preserved_commit(path, saved["head"])
        run = mgr.git._arun

        async def racing_fetch(args, **kwargs):
            result = await run(args, **kwargs)
            if args[0] == "fetch" and "--no-write-fetch-head" in args:
                await run(["fetch", "origin", "main"], cwd=str(path))
            return result

        monkeypatch.setattr(mgr.git, "_arun", racing_fetch)
        await self._restore(mgr, slot, sha)
        assert _git(["rev-parse", "HEAD"], path) == sha
        assert (path / "staged.txt").read_text() == "saved unstaged\n"
        assert _git(["for-each-ref", "refs/aq/task-restores"], path) == ""

    @pytest.mark.parametrize("layer", ["index", "worktree"])
    async def test_snapshot_content_conflict_refuses_before_reset(
        self, mgr, base_ws, kind, layer
    ):
        from src.orchestrator.task_checkpoint import CHECKPOINT_META, capture_checkpoint

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind)
        (path / "README.md").write_text("checkpoint edit\n")
        if layer == "index":
            _git(["add", "README.md"], path)
            (path / "README.md").write_text("init\n")
        await capture_checkpoint(mgr.db, mgr.git, "tsk-1", str(path))
        saved = await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META)
        _git(["reset", "--hard"], path)
        sha = self._preserved_commit(path, saved["head"], filename="README.md", content="operator edit\n")
        index = _git(["write-tree"], path)
        sentinel = (path / WORKTREE_SENTINEL_NAME).read_bytes()

        with pytest.raises(GitError, match="content conflicts.*unchanged"):
            await self._restore(mgr, slot, sha)

        assert _git(["rev-parse", "HEAD"], path) == sha
        assert _git(["write-tree"], path) == index
        assert (path / "README.md").read_text() == "operator edit\n"
        assert (path / WORKTREE_SENTINEL_NAME).read_bytes() == sentinel
        assert await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META) == saved

    @pytest.mark.parametrize("diverged", ["checkpoint", "local_branch"])
    async def test_divergent_history_is_refused(self, mgr, base_ws, kind, diverged):
        from src.orchestrator.task_checkpoint import CHECKPOINT_META, capture_checkpoint

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind)
        (path / "local.txt").write_text("local line\n")
        _git(["add", "local.txt"], path)
        _git(["commit", "-m", "local line"], path)
        local = _git(["rev-parse", "HEAD"], path)
        if diverged == "checkpoint":
            await capture_checkpoint(mgr.db, mgr.git, "tsk-1", str(path))
        sha = self._preserved_commit(path, saved["head"])
        index = _git(["write-tree"], path)

        with pytest.raises(GitError, match="diverged.*unchanged"):
            await self._restore(mgr, slot, sha)

        assert _git(["rev-parse", "HEAD"], path) == sha
        assert _git(["rev-parse", "refs/heads/aq/tsk-1"], path) == local
        assert _git(["write-tree"], path) == index
        assert await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META)

    @pytest.mark.parametrize("dirty", ["staged", "unstaged", "untracked", "untracked_hidden"])
    async def test_newer_dirty_destination_is_unchanged(self, mgr, base_ws, kind, dirty):
        from src.orchestrator.task_checkpoint import CHECKPOINT_META

        slot, path, saved = await self._checkpoint(mgr, base_ws, kind)
        sha = self._preserved_commit(path, saved["head"])
        filename = "new.txt" if dirty.startswith("untracked") else "README.md"
        (path / filename).write_text("newer dirty work\n")
        if dirty == "staged":
            _git(["add", filename], path)
        elif dirty == "untracked_hidden":
            _git(["config", "status.showUntrackedFiles", "no"], path)
        index = _git(["write-tree"], path)
        status = _git(["status", "--porcelain"], path)
        # Refusal is independent of best-effort salvage policy.
        mgr.config.salvage_dirty = False

        with pytest.raises(GitError, match="destination is dirty.*unchanged"):
            await self._restore(mgr, slot, sha)

        assert (path / filename).read_text() == "newer dirty work\n"
        assert _git(["write-tree"], path) == index
        assert _git(["status", "--porcelain"], path) == status
        assert await mgr.db.get_task_meta("tsk-1", CHECKPOINT_META) == saved


# ─────────────────────────────── §3.2 reset ──────────────────────────────


class TestResetSlot:
    @pytest.mark.parametrize("delete_predecessor_branch", [False, True])
    async def test_reuses_published_slot_detached_at_default_tip(
        self, mgr, base_ws, kind, delete_predecessor_branch,
    ):
        slot = await mgr.create_slot(base_ws, kind, 0)
        path = Path(slot.workspace_path)
        await mgr.reset_slot_for_task(slot, FakeTask())
        _git(["commit", "--allow-empty", "-m", "published predecessor"], path)
        _git(["push", "origin", "aq/tsk-1"], path)
        await mgr.restore_slot_after_task(slot, task_id="tsk-1")
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], path) == "HEAD"
        assert _git(["rev-parse", "HEAD"], path) == _git(["rev-parse", "origin/main"], path)
        if delete_predecessor_branch:
            _git(["branch", "-D", "aq/tsk-1"], path)
        assert await mgr.reset_slot_for_task(slot, FakeTask(id="tsk-2")) == "aq/tsk-2"

    async def test_exclusive_clone_can_be_prepared_again(self, mgr, base_ws, kind):
        await mgr.reset_slot_for_task(base_ws, FakeTask(), kind=kind)

        branch = await mgr.reset_slot_for_task(base_ws, FakeTask(), kind=kind)

        assert branch == "aq/tsk-1"
        assert mgr.read_sentinel(base_ws.workspace_path).base_workspace_id == ""

    @pytest.mark.parametrize("locked", [False, True])
    async def test_missing_slot_retains_its_registration_for_investigation(
        self, mgr, base_ws, kind, locked,
    ):
        slot = await mgr.create_slot(base_ws, kind, 0)
        await mgr.reset_slot_for_task(slot, FakeTask(), kind=kind)
        if locked:
            _git(["worktree", "lock", slot.workspace_path], base_ws.workspace_path)
        shutil.rmtree(slot.workspace_path)
        registrations = _git(["worktree", "list", "--porcelain"], base_ws.workspace_path)
        with pytest.raises(GitError, match="missing.*supervisor investigation"):
            await mgr.reset_slot_for_task(slot, FakeTask(), kind=kind)
        assert not Path(slot.workspace_path).exists()
        assert mgr.db.workspaces[slot.id] is slot
        assert _git(["worktree", "list", "--porcelain"], base_ws.workspace_path) == registrations

    @pytest.mark.parametrize("binding", ["claim_file", "task_lock"])
    async def test_slot_bound_to_another_task_is_a_foreign_slot_claim(
        self, mgr, base_ws, kind, binding,
    ):
        """A typed refusal, so the claim path releases the task for another slot."""
        from src.claim_file import read_claim_file, write_claim_file

        slot = await mgr.create_slot(base_ws, kind, 0)
        foreign = {"task_id": "paused-holder", "claim_epoch": 3, "session_id": "gone"}
        if binding == "claim_file":
            write_claim_file(slot.workspace_path, foreign)
        else:
            slot.locked_by_task_id = "paused-holder"

        with pytest.raises(ForeignSlotClaim) as refused:
            await mgr.reset_slot_for_task(slot, FakeTask(), kind=kind)

        assert refused.value.holder_task_id == "paused-holder"
        assert refused.value.workspace_id == slot.id
        if binding == "claim_file":
            assert read_claim_file(slot.workspace_path) == foreign


    @pytest.mark.parametrize("protection", ["unmanaged", "agent", "task", "owner"])
    async def test_protected_branch_holder_is_never_detached(
        self, mgr, base_ws, kind, monkeypatch, protection,
    ):
        holder = await mgr.create_slot(base_ws, kind, 0)
        target = await mgr.create_slot(base_ws, kind, 1)
        branch = await mgr.reset_slot_for_task(holder, FakeTask(), kind=kind)
        if protection == "unmanaged":
            del mgr.db.workspaces[holder.id]
        elif protection == "agent":
            holder.locked_by_agent_id = "foreign"
        elif protection == "task":
            holder.locked_by_task_id = "foreign"
        else:
            monkeypatch.setattr(mgr, "_slot_has_retained_binding", AsyncMock(return_value=True))

        with pytest.raises(GitError, match="already used by worktree"):
            await mgr.reset_slot_for_task(target, FakeTask(), kind=kind)

        assert _git(["branch", "--show-current"], holder.workspace_path) == branch

    async def test_foreign_slot_identity_is_preserved(self, mgr, base_ws, kind):
        slot = await mgr.create_slot(base_ws, kind, 0)
        sentinel = mgr.read_sentinel(slot.workspace_path)
        sentinel.workspace_id = "foreign-slot"
        mgr.write_sentinel(slot.workspace_path, sentinel)
        (Path(slot.workspace_path) / "progress.txt").write_text("preserve me")
        with pytest.raises(GitError, match="mismatched sentinel"):
            await mgr.reset_slot_for_task(slot, FakeTask(), kind=kind)
        assert (Path(slot.workspace_path) / "progress.txt").read_text() == "preserve me"

    @pytest.mark.parametrize("disagreement", ["branch", "repository", "claim", "unreadable_claim", "sentinel"])
    async def test_source_disagreement_preserves_index_and_worktree(
        self, mgr, base_ws, kind, disagreement,
    ):
        slot = await mgr.create_slot(base_ws, kind, 0)
        await mgr.reset_slot_for_task(slot, FakeTask(), kind=kind)
        path = Path(slot.workspace_path)
        if disagreement == "branch":
            _git(["switch", "-c", "aq/foreign"], path)
        elif disagreement == "repository":
            other = path.parent / "other-repository"
            _git(["clone", base_ws.workspace_path, str(other)], path.parent)
            other_ws = Workspace(id="other-base", project_id=base_ws.project_id,
                                 workspace_path=str(other), source_type=RepoSourceType.CLONE)
            mgr.db.workspaces[other_ws.id] = other_ws
            slot.base_workspace_id = other_ws.id
            sentinel = mgr.read_sentinel(path)
            sentinel.base_workspace_id = other_ws.id
            mgr.write_sentinel(path, sentinel)
        elif disagreement in {"claim", "unreadable_claim"}:
            from src.claim_file import write_claim_file
            write_claim_file(str(path), {"task_id": "foreign", "claim_epoch": 7})
            if disagreement == "unreadable_claim":
                (path / ".aq" / "claim.json").write_bytes(b"unreadable claim")
        else:
            sentinel = mgr.read_sentinel(path)
            sentinel.workspace_id = "foreign"
            mgr.write_sentinel(path, sentinel)
        (path / "README.md").write_bytes(b"staged source")
        _git(["add", "README.md"], path)
        (path / "untracked").write_bytes(b"untracked source")
        head = _git(["rev-parse", "HEAD"], path)
        index = _git(["write-tree"], path)
        branch = _git(["branch", "--show-current"], path)
        sentinel = (path / WORKTREE_SENTINEL_NAME).read_bytes()

        with pytest.raises(GitError):
            await mgr.reset_slot_for_task(slot, FakeTask(), kind=kind)

        assert _git(["rev-parse", "HEAD"], path) == head
        assert _git(["write-tree"], path) == index
        assert _git(["branch", "--show-current"], path) == branch
        assert (path / "README.md").read_bytes() == b"staged source"
        assert (path / "untracked").read_bytes() == b"untracked source"
        assert (path / WORKTREE_SENTINEL_NAME).read_bytes() == sentinel

    def _make_slot(self, mgr, base_ws, kind):
        return asyncio.run(mgr.create_slot(base_ws, kind, 0))

    @pytest.mark.parametrize("override", [None, 1800])
    @pytest.mark.parametrize("preserve", [False, True])
    def test_reset_and_child_merge_use_project_budget(
        self, mgr, base_ws, kind, monkeypatch, override, preserve,
    ):
        slot = self._make_slot(mgr, base_ws, kind)
        if override:
            mgr.config.slot_reset_git_timeouts[slot.project_id] = override
        parent = _git(["rev-parse", "HEAD"], cwd=slot.workspace_path)
        calls = []
        original = mgr.git._arun_unlocked

        async def record(args, cwd=None, timeout=None):
            calls.append((args[0], mgr.git._command_timeout(timeout)))
            return await original(args, cwd=cwd, timeout=timeout)

        monkeypatch.setattr(mgr.git, "_arun_unlocked", record)
        kwargs = dict(base_branch=parent, target_branch="aq/tsk-1", preserve_branch=True) \
            if preserve else {}
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(), **kwargs))
        assert ("reset", override or 600) in calls
        if preserve:
            assert ("merge", override or 600) in calls
        assert all(budget == (override or 600) for _, budget in calls)
        assert mgr.git._command_timeout() == 120

    def test_failed_reset_restores_default_budget(self, mgr, base_ws, kind, monkeypatch):
        slot = self._make_slot(mgr, base_ws, kind)

        async def fail(*args, **kwargs):
            assert mgr.git._command_timeout() == 600
            raise GitError("simulated I/O failure")

        monkeypatch.setattr(mgr, "ensure_git_exclude", fail)
        with pytest.raises(GitError, match="simulated I/O failure"):
            asyncio.run(mgr.reset_slot_for_task(slot, FakeTask()))
        assert mgr.git._command_timeout() == 120

    def test_creates_the_task_branch(self, mgr, base_ws, kind, base_repo):
        slot = self._make_slot(mgr, base_ws, kind)
        branch = asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-1")))
        assert branch == "aq/tsk-1"
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot.workspace_path) == "aq/tsk-1"

    def test_branch_name_has_no_title_slug(self):
        assert task_branch_name("tsk-abc") == "aq/tsk-abc"

    def test_updates_the_sentinel_and_emits(self, mgr, base_ws, kind, bus):
        slot = self._make_slot(mgr, base_ws, kind)
        created = WorktreeSlotManager.read_sentinel(slot.workspace_path)
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-7")))
        s = WorktreeSlotManager.read_sentinel(slot.workspace_path)
        assert s.task_id == "tsk-7"
        assert s.branch == "aq/tsk-7"
        assert s.assigned_at is not None
        assert s.created_at == created.created_at, "created_at must survive a reset"
        p = bus.payload("worktree.reset")
        assert p["task_id"] == "tsk-7" and p["branch"] == "aq/tsk-7"

    def test_clean_removes_untracked_but_keeps_gitignored(self, mgr, base_ws, kind):
        slot = self._make_slot(mgr, base_ws, kind)
        d = Path(slot.workspace_path)
        (d / "junk.txt").write_text("scratch")
        cache = d / "node_modules"
        cache.mkdir()
        (cache / "dep.js").write_text("expensive")

        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-2")))

        assert not (d / "junk.txt").exists(), "untracked junk must be cleaned"
        assert (cache / "dep.js").exists(), "gitignored caches must survive (no -x)"

    def test_sentinel_survives_the_clean(self, mgr, base_ws, kind):
        slot = self._make_slot(mgr, base_ws, kind)
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-3")))
        assert (Path(slot.workspace_path) / WORKTREE_SENTINEL_NAME).exists()

    def test_dirty_slot_is_salvaged_to_the_previous_task(self, mgr, base_ws, kind, db):
        slot = self._make_slot(mgr, base_ws, kind)
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-prev")))
        # Predecessor crashes mid-edit.
        (Path(slot.workspace_path) / "README.md").write_text("half-finished work\n")

        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-next")))

        assert len(db.contexts) == 1, db.contexts
        ctx = db.contexts[0]
        assert ctx["task_id"] == "tsk-prev", "patch belongs to whoever made the mess"
        assert ctx["type"] == "worktree_salvage"
        assert "half-finished work" in ctx["content"]
        # And the slot came out clean.
        assert _git(["status", "--porcelain"], cwd=slot.workspace_path) == ""
        assert mgr.bus.payload("worktree.reset")["salvaged"] in (True, False)

    def test_salvage_never_uses_the_shared_stash_stack(self, mgr, base_ws, kind, base_repo):
        """`git stash` is repo-global across worktrees — a pop in one slot can
        restore another slot's work.  Nothing here may push onto that stack."""
        slot = self._make_slot(mgr, base_ws, kind)
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-a")))
        (Path(slot.workspace_path) / "README.md").write_text("dirty\n")
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-b")))
        stash = _git(["stash", "list"], cwd=base_repo)
        assert stash == "", f"salvage must not stash, got: {stash!r}"

    def test_salvage_disabled_hard_resets_without_archiving(
        self, base_ws, kind, db, bus, mutexes
    ):
        m = WorktreeSlotManager(
            db=db,
            git=GitManager(),
            bus=bus,
            config=WorktreesConfig(enabled=True, salvage_dirty=False),
            git_mutex=mutexes,
        )
        slot = asyncio.run(m.create_slot(base_ws, kind, 0))
        asyncio.run(m.reset_slot_for_task(slot, FakeTask(id="tsk-a")))
        (Path(slot.workspace_path) / "README.md").write_text("dirty\n")
        asyncio.run(m.reset_slot_for_task(slot, FakeTask(id="tsk-b")))
        assert db.contexts == []
        assert _git(["status", "--porcelain"], cwd=slot.workspace_path) == ""

    def test_retry_reuses_the_existing_branch(self, mgr, base_ws, kind):
        """A retry reuses the branch and resumes the attempt that was preserved.

        Design §3.2 step 4 re-points a retried branch at the fresh start
        point, and did drop the failed attempt's commit — which is why three
        re-runs of ``solid-harbor.43`` each started from ``main`` and could
        not find their predecessor's work.  Two rules now meet here and agree:
        the reset first *publishes* those commits (``salvage_unpushed``), and
        ``_retry_reset_ref`` keeps a published tip the start point does not
        already contain.  So a retry lands on its predecessor's work rather
        than silently below it.  ``test_a_retry_with_nothing_published_starts
        _clean`` covers the other half — the start point still wins when
        there was nothing to preserve.
        """
        slot = self._make_slot(mgr, base_ws, kind)
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-r")))
        (Path(slot.workspace_path) / "attempt.txt").write_text("one")
        _git(["add", "-A"], cwd=slot.workspace_path)
        _git(["commit", "-m", "attempt one"], cwd=slot.workspace_path)
        attempt = _git(["rev-parse", "HEAD"], cwd=slot.workspace_path)

        branch = asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-r")))
        assert branch == "aq/tsk-r"
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot.workspace_path) == "aq/tsk-r"
        assert (
            _git(["ls-remote", "origin", "refs/heads/aq/tsk-r"], cwd=slot.workspace_path).split()[0]
            == attempt
        ), "the attempt must be on origin before anything is reset"
        assert (Path(slot.workspace_path) / "attempt.txt").exists()

    def test_a_retry_without_a_remote_keeps_local_progress(self, mgr, base_ws, kind, base_repo):
        """Local commits remain the retry's progress even without a remote."""
        _git(["remote", "remove", "origin"], cwd=base_repo)
        slot = self._make_slot(mgr, base_ws, kind)
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-r2")))
        (Path(slot.workspace_path) / "attempt.txt").write_text("one")
        _git(["add", "-A"], cwd=slot.workspace_path)
        _git(["commit", "-m", "attempt one"], cwd=slot.workspace_path)

        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-r2")))

        assert (Path(slot.workspace_path) / "attempt.txt").read_text() == "one"

    def test_continuation_resumes_the_branch_tip(self, mgr, base_ws, kind):
        slot = self._make_slot(mgr, base_ws, kind)
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-orig")))
        (Path(slot.workspace_path) / "work.txt").write_text("real progress")
        _git(["add", "-A"], cwd=slot.workspace_path)
        _git(["commit", "-m", "progress"], cwd=slot.workspace_path)
        tip = _git(["rev-parse", "HEAD"], cwd=slot.workspace_path)

        branch = asyncio.run(
            mgr.reset_slot_for_task(
                slot, FakeTask(id="tsk-cont"), resume_branch="aq/tsk-orig"
            )
        )
        assert branch == "aq/tsk-orig"
        assert _git(["rev-parse", "HEAD"], cwd=slot.workspace_path) == tip
        assert (Path(slot.workspace_path) / "work.txt").exists()

    def test_resume_reinstalls_managed_excludes(self, mgr, base_ws, kind, base_repo):
        slot = self._make_slot(mgr, base_ws, kind)
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-original")))
        exclude = base_repo / ".git" / "info" / "exclude"
        exclude.unlink()

        asyncio.run(
            mgr.reset_slot_for_task(
                slot, FakeTask(id="tsk-resume"), resume_branch="aq/tsk-original"
            )
        )

        assert EXCLUDE_BEGIN in exclude.read_text()

    def test_base_branch_override_is_honored(self, mgr, base_ws, kind, base_repo):
        _git(["branch", "release/1.0"], cwd=base_repo)
        _git(["push", "origin", "release/1.0"], cwd=base_repo)
        slot = self._make_slot(mgr, base_ws, kind)
        asyncio.run(
            mgr.reset_slot_for_task(
                slot, FakeTask(id="tsk-b"), base_branch="release/1.0"
            )
        )
        merged = _git(["branch", "--contains", "HEAD", "-a"], cwd=slot.workspace_path)
        assert "release/1.0" in merged

    def test_exact_repair_branch_uses_owned_ref(self, mgr, base_ws, kind, base_repo):
        slot = self._make_slot(mgr, base_ws, kind)
        head = _git(["rev-parse", "HEAD"], cwd=base_repo)
        branch = asyncio.run(mgr.reset_slot_for_task(
            slot, FakeTask(id="repair-1"), base_branch=head,
            target_branch="refs/heads/aq/integration/batch",
        ))
        assert branch == "aq/integration/batch"
        assert _git(["branch", "--show-current"], cwd=slot.workspace_path) == branch
        assert _git(["rev-parse", "HEAD"], cwd=slot.workspace_path) == head

    def test_hostile_base_branch_is_rejected(self, mgr, base_ws, kind):
        """`base_branch` reaches git from task metadata — untrusted text."""
        slot = self._make_slot(mgr, base_ws, kind)
        with pytest.raises(GitError):
            asyncio.run(
                mgr.reset_slot_for_task(
                    slot, FakeTask(id="tsk-x"), base_branch="--upload-pack=evil"
                )
            )

    def test_fetch_failure_with_existing_ref_still_proceeds(
        self, mgr, base_ws, kind, base_repo, tmp_path
    ):
        """Design §3.5: offline origin is non-fatal once the ref is local."""
        slot = self._make_slot(mgr, base_ws, kind)
        # Break origin *after* the slot exists and origin/main is known.
        _git(["remote", "set-url", "origin", str(tmp_path / "gone.git")], cwd=base_repo)
        branch = asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-off")))
        assert branch == "aq/tsk-off"

    def test_setup_reruns_only_when_the_hash_changes(
        self, base_ws, db, bus, mutexes, base_repo
    ):
        m = WorktreeSlotManager(
            db=db,
            git=GitManager(),
            bus=bus,
            config=WorktreesConfig(enabled=True, setup_timeout_seconds=60),
            git_mutex=mutexes,
        )
        k1 = WorkspaceKind(
            project_id="__system__", id="project-repo",
            worktree_setup=["git config aq.rev one"],
        )
        slot = asyncio.run(m.create_slot(base_ws, k1, 0))
        d = slot.workspace_path

        # Same hash → no re-run.  Clear the marker and confirm it stays gone.
        _git(["config", "--unset", "aq.rev"], cwd=d)
        asyncio.run(m.reset_slot_for_task(slot, FakeTask(id="tsk-1"), kind=k1))
        r = subprocess.run(
            ["git", "config", "--get", "aq.rev"], cwd=d, capture_output=True, text=True
        )
        assert r.returncode != 0, "setup must not re-run for an unchanged hash"

        # Changed list → re-run.
        k2 = WorkspaceKind(
            project_id="__system__", id="project-repo",
            worktree_setup=["git config aq.rev two"],
        )
        asyncio.run(m.reset_slot_for_task(slot, FakeTask(id="tsk-2"), kind=k2))
        assert _git(["config", "--get", "aq.rev"], cwd=d) == "two"
        assert (
            WorktreeSlotManager.read_sentinel(d).setup_hash
            == worktree_setup_hash(k2.worktree_setup)
        )


# ───────────────────── project default branch (not main) ─────────────────


@dataclass
class FakeProject:
    id: str = "p1"
    repo_url: str = ""
    repo_default_branch: str | None = "main"


class ProjectDB(FakeDB):
    def __init__(self, project: FakeProject):
        super().__init__()
        self.project = project

    async def get_project(self, project_id):
        return self.project if project_id == self.project.id else None


class TestProjectDefaultBranch:
    """Slots and task branches start from the project's configured branch.

    Seen in matter-engine-cpp: ``aq project set ... branch vg-vt-improvements``
    recorded the branch, integration delivered into it, and every worker slot
    was still cut from ``origin/main`` — origin's HEAD, which is what the
    slot manager asked git for.  A worker who did not notice built on main
    and delivered main's history into the project branch.
    """

    BRANCH = "vg-vt-improvements"

    @pytest.fixture
    def project_tip(self, base_repo: Path) -> str:
        """Push ``vg-vt-improvements`` one commit ahead of main; origin HEAD stays main."""
        _git(["switch", "-c", self.BRANCH], cwd=base_repo)
        (base_repo / "feature.txt").write_text("project branch only\n")
        _git(["add", "feature.txt"], cwd=base_repo)
        _git(["commit", "-m", "project branch work"], cwd=base_repo)
        _git(["push", "origin", self.BRANCH], cwd=base_repo)
        tip = _git(["rev-parse", "HEAD"], cwd=base_repo)
        _git(["switch", "main"], cwd=base_repo)
        assert _git(["rev-parse", "main"], cwd=base_repo) != tip
        return tip

    def _mgr(self, bus, mutexes, base_ws, default_branch):
        db = ProjectDB(FakeProject(repo_default_branch=default_branch))
        db.workspaces[base_ws.id] = base_ws
        return WorktreeSlotManager(
            db=db,
            git=GitManager(),
            bus=bus,
            config=WorktreesConfig(enabled=True, setup_timeout_seconds=60),
            git_mutex=mutexes,
        )

    def test_create_slot_starts_from_the_project_branch(
        self, bus, mutexes, base_ws, kind, project_tip
    ):
        m = self._mgr(bus, mutexes, base_ws, self.BRANCH)
        slot = asyncio.run(m.create_slot(base_ws, kind, 0))
        assert _git(["rev-parse", "HEAD"], cwd=slot.workspace_path) == project_tip

    def test_task_branch_is_cut_from_the_project_branch(
        self, bus, mutexes, base_ws, kind, project_tip
    ):
        m = self._mgr(bus, mutexes, base_ws, self.BRANCH)
        slot = asyncio.run(m.create_slot(base_ws, kind, 0))
        branch = asyncio.run(m.reset_slot_for_task(slot, FakeTask(id="tsk-vg")))
        assert branch == "aq/tsk-vg"
        assert _git(["rev-parse", "HEAD"], cwd=slot.workspace_path) == project_tip
        assert (Path(slot.workspace_path) / "feature.txt").exists()

    def test_a_slot_created_on_main_is_reset_onto_the_project_branch(
        self, bus, mutexes, base_ws, kind, project_tip
    ):
        """An existing slot (cut before the branch was set) must not keep main."""
        before = self._mgr(bus, mutexes, base_ws, "main")
        slot = asyncio.run(before.create_slot(base_ws, kind, 0))
        assert not (Path(slot.workspace_path) / "feature.txt").exists()

        after = self._mgr(bus, mutexes, base_ws, self.BRANCH)
        asyncio.run(after.reset_slot_for_task(slot, FakeTask(id="tsk-next")))
        assert _git(["rev-parse", "HEAD"], cwd=slot.workspace_path) == project_tip

    def test_no_recorded_branch_falls_back_to_origin_head(
        self, bus, mutexes, base_ws, kind, base_repo, project_tip
    ):
        m = self._mgr(bus, mutexes, base_ws, None)
        slot = asyncio.run(m.create_slot(base_ws, kind, 0))
        main_tip = _git(["rev-parse", "origin/main"], cwd=base_repo)
        assert _git(["rev-parse", "HEAD"], cwd=slot.workspace_path) == main_tip

    def test_an_unsafe_recorded_branch_is_refused_not_ignored(
        self, bus, mutexes, base_ws, kind
    ):
        """Falling back to origin HEAD here would reintroduce the bug silently."""
        m = self._mgr(bus, mutexes, base_ws, "--upload-pack=evil")
        with pytest.raises(GitError, match="default branch"):
            asyncio.run(m.create_slot(base_ws, kind, 0))


# ─────────────────── GitManager worktree primitives (§4) ─────────────────


class TestGitManagerWorktreePrimitives:
    def test_worktree_add_list_prune(self, base_repo: Path, tmp_path: Path):
        g = GitManager()
        wt = tmp_path / "wt-a"
        asyncio.run(g.aworktree_add(str(base_repo), str(wt), ref="main", detach=True))
        entries = asyncio.run(g.aworktree_list(str(base_repo)))
        assert len(entries) == 2
        by_path = {Path(e["path"]).resolve(): e for e in entries}
        assert by_path[base_repo.resolve()]["branch"] == "main"
        assert "detached" in by_path[wt.resolve()]

        import shutil

        shutil.rmtree(wt)
        asyncio.run(g.aworktree_prune(str(base_repo)))
        assert len(asyncio.run(g.aworktree_list(str(base_repo)))) == 1

    def test_worktree_add_rejects_option_like_ref(self, base_repo: Path, tmp_path: Path):
        g = GitManager()
        with pytest.raises(GitError):
            asyncio.run(
                g.aworktree_add(
                    str(base_repo), str(tmp_path / "wt"), ref="--upload-pack=evil"
                )
            )

    def test_list_merged_branches_filters_by_prefix(self, base_repo: Path):
        g = GitManager()
        _git(["branch", "aq/tsk-merged"], cwd=base_repo)
        _git(["branch", "feature/keep"], cwd=base_repo)
        merged = asyncio.run(g.alist_merged_branches(str(base_repo), into="main"))
        assert merged == ["aq/tsk-merged"]

    def test_list_merged_branches_excludes_the_target(self, base_repo: Path):
        g = GitManager()
        merged = asyncio.run(
            g.alist_merged_branches(str(base_repo), into="main", prefix="")
        )
        assert "main" not in merged

    def test_list_merged_branches_rejects_bad_target_and_prefix(self, base_repo: Path):
        g = GitManager()
        with pytest.raises(GitError):
            asyncio.run(g.alist_merged_branches(str(base_repo), into="-oops"))
        with pytest.raises(GitError):
            asyncio.run(
                g.alist_merged_branches(str(base_repo), into="main", prefix="-x")
            )

    def test_delete_local_branch(self, base_repo: Path):
        g = GitManager()
        _git(["branch", "aq/tsk-gone"], cwd=base_repo)
        asyncio.run(g.adelete_local_branch(str(base_repo), "aq/tsk-gone"))
        assert "aq/tsk-gone" not in _git(["branch"], cwd=base_repo)

    def test_delete_local_branch_needs_force_when_unmerged(self, base_repo: Path):
        g = GitManager()
        _git(["checkout", "-b", "aq/tsk-unmerged"], cwd=base_repo)
        (base_repo / "x.txt").write_text("x")
        _git(["add", "-A"], cwd=base_repo)
        _git(["commit", "-m", "x"], cwd=base_repo)
        _git(["checkout", "main"], cwd=base_repo)

        with pytest.raises(GitError):
            asyncio.run(g.adelete_local_branch(str(base_repo), "aq/tsk-unmerged"))
        asyncio.run(
            g.adelete_local_branch(str(base_repo), "aq/tsk-unmerged", force=True)
        )
        assert "aq/tsk-unmerged" not in _git(["branch"], cwd=base_repo)

    def test_delete_local_branch_rejects_option_like_name(self, base_repo: Path):
        with pytest.raises(GitError):
            asyncio.run(GitManager().adelete_local_branch(str(base_repo), "-D"))

    def test_worktree_base_path_resolves_without_a_naming_convention(
        self, base_repo: Path, tmp_path: Path
    ):
        g = GitManager()
        wt = tmp_path / "anywhere" / "at" / "all"
        asyncio.run(g.aworktree_add(str(base_repo), str(wt), ref="main"))
        got = asyncio.run(g.aworktree_base_path(str(wt)))
        assert Path(got).resolve() == base_repo.resolve()

    def test_worktree_base_path_returns_none_outside_a_repo(self, tmp_path: Path):
        d = tmp_path / "not-a-repo"
        d.mkdir()
        assert asyncio.run(GitManager().aworktree_base_path(str(d))) is None


# ────────────────────── §3.2 salvage: binaries and size ──────────────────


class TestSalvageFidelity:
    """F7: what ``salvage_dirty`` archives has to be enough to restore."""

    def _slot(self, mgr, base_ws, kind, task_id="tsk-prev"):
        slot = asyncio.run(mgr.create_slot(base_ws, kind, 0))
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id=task_id)))
        return slot

    def test_binary_payload_survives_salvage(self, mgr, base_ws, kind, db):
        """Without ``--binary`` git emits only "Binary files ... differ", and
        the ``reset --hard`` that follows destroys the bytes for good."""
        slot = self._slot(mgr, base_ws, kind)
        blob = bytes(range(256)) * 8
        (Path(slot.workspace_path) / "logo.png").write_bytes(blob)

        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-next")))

        assert len(db.contexts) == 1
        patch = db.contexts[0]["content"]
        assert "GIT binary patch" in patch, (
            "a binary diff without --binary is unappliable; the bytes are gone "
            f"after the reset. Got: {patch[:200]!r}"
        )
        assert not (Path(slot.workspace_path) / "logo.png").exists()

        # And it really restores: apply the patch to the now-clean slot.
        patch_file = Path(slot.workspace_path).parent / "salvage.patch"
        with open(patch_file, "w", encoding="utf-8", newline="") as f:
            f.write(patch)
        _git(["apply", "--binary", str(patch_file)], cwd=slot.workspace_path)
        assert (Path(slot.workspace_path) / "logo.png").read_bytes() == blob

    def test_oversized_patch_is_replaced_by_its_diffstat(
        self, base_ws, kind, db, bus, mutexes
    ):
        m = WorktreeSlotManager(
            db=db,
            git=GitManager(),
            bus=bus,
            config=WorktreesConfig(
                enabled=True, setup_timeout_seconds=60, salvage_max_bytes=512
            ),
            git_mutex=mutexes,
        )
        slot = asyncio.run(m.create_slot(base_ws, kind, 0))
        asyncio.run(m.reset_slot_for_task(slot, FakeTask(id="tsk-prev")))
        (Path(slot.workspace_path) / "huge.txt").write_text("x" * 200_000)

        asyncio.run(m.reset_slot_for_task(slot, FakeTask(id="tsk-next")))

        assert len(db.contexts) == 1
        content = db.contexts[0]["content"]
        assert len(content) < 4096, "task_contexts is not a blob store"
        assert "salvage_max_bytes" in content
        assert "huge.txt" in content, "the operator still learns what was lost"

    def test_cap_of_zero_disables_the_bound(self, base_ws, kind, db, bus, mutexes):
        m = WorktreeSlotManager(
            db=db,
            git=GitManager(),
            bus=bus,
            config=WorktreesConfig(
                enabled=True, setup_timeout_seconds=60, salvage_max_bytes=0
            ),
            git_mutex=mutexes,
        )
        slot = asyncio.run(m.create_slot(base_ws, kind, 0))
        asyncio.run(m.reset_slot_for_task(slot, FakeTask(id="tsk-prev")))
        (Path(slot.workspace_path) / "huge.txt").write_text("x" * 200_000)
        asyncio.run(m.reset_slot_for_task(slot, FakeTask(id="tsk-next")))
        assert len(db.contexts[0]["content"]) > 100_000


# ───────────────── §3.4 restore after a task ends badly ──────────────────


class TestRestoreSlotAfterTask:
    """F4: the clone cleanup ladder is wrong on every rung inside a worktree."""

    def _dirty_slot(self, mgr, base_ws, kind):
        slot = asyncio.run(mgr.create_slot(base_ws, kind, 0))
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-1")))
        d = Path(slot.workspace_path)
        (d / "README.md").write_text("unfinished")
        (d / "scratch.tmp").write_text("junk")
        cache = d / "node_modules"
        cache.mkdir()
        (cache / "dep.js").write_text("expensive")
        return slot, d, cache

    def test_salvages_cleans_and_keeps_the_caches(self, mgr, base_ws, kind, db):
        slot, d, cache = self._dirty_slot(mgr, base_ws, kind)

        assert asyncio.run(mgr.restore_slot_after_task(slot, task_id="tsk-1")) is True

        assert _git(["status", "--porcelain"], cwd=d) == ""
        assert not (d / "scratch.tmp").exists()
        assert cache.joinpath("dep.js").exists(), "no -x: warm caches survive"
        assert [c["type"] for c in db.contexts] == ["worktree_salvage"]
        assert "unfinished" in db.contexts[0]["content"]

    def test_never_touches_the_shared_stash_stack(self, mgr, base_ws, kind, base_repo):
        slot, _d, _c = self._dirty_slot(mgr, base_ws, kind)
        asyncio.run(mgr.restore_slot_after_task(slot, task_id="tsk-1"))
        assert _git(["stash", "list"], cwd=base_repo) == ""
        assert _git(["stash", "list"], cwd=slot.workspace_path) == ""

    def test_slot_stays_on_its_task_branch(self, mgr, base_ws, kind):
        """The branch is the artifact; checking out the default branch here
        would both abandon it and fail (the base holds that branch)."""
        slot, d, _c = self._dirty_slot(mgr, base_ws, kind)
        asyncio.run(mgr.restore_slot_after_task(slot, task_id="tsk-1"))
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=d) == "aq/tsk-1"

    def test_a_stale_index_lock_does_not_block_the_restore(self, mgr, base_ws, kind):
        """F14: a killed agent leaves ``.git/worktrees/<slot>/index.lock``,
        which is *not* under ``<slot>/.git`` — that is a file, not a dir."""
        slot, d, _c = self._dirty_slot(mgr, base_ws, kind)
        git_dir = Path(GitManager._resolve_git_dir(str(d)))
        assert git_dir.is_dir() and git_dir != d / ".git"
        (git_dir / "index.lock").write_text("")

        asyncio.run(mgr.restore_slot_after_task(slot, task_id="tsk-1"))

        assert not (git_dir / "index.lock").exists()
        assert _git(["status", "--porcelain"], cwd=d) == ""

    @pytest.mark.parametrize("dirty", [False, True])
    def test_failed_close_preserves_the_published_tip_until_handoff(
        self, mgr, base_ws, kind, db, dirty
    ):
        """Restoring onto origin/main must not invalidate the exact-tip proof."""
        slot = asyncio.run(mgr.create_slot(base_ws, kind, 0))
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-1")))
        path = Path(slot.workspace_path)
        (path / "work.py").write_text("print(1)\n")
        _git(["add", "work.py"], cwd=path)
        _git(["commit", "-m", "task progress"], cwd=path)
        _git(["push", "origin", "aq/tsk-1"], cwd=path)
        tip = _git(["rev-parse", "HEAD"], cwd=path)
        assert tip != _git(["rev-parse", "origin/main"], cwd=path)
        if dirty:
            (path / "work.py").write_text("unfinished\n")
        orchestrator = SimpleNamespace(
            _slot_workspace_at=AsyncMock(return_value=slot),
            _worktree_slots=lambda: mgr,
        )

        async def close_handoff():
            assert await ExecutionMixin._restore_slot_before_handoff_proof(
                orchestrator, str(path), "tsk-1"
            )
            assert _git(["rev-parse", "HEAD"], cwd=path) == tip
            assert await detach_slot_for_integration_handoff(
                db, mgr.git, mgr._git_mutex, slot, expected_branch="aq/tsk-1"
            )

        asyncio.run(close_handoff())
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path) == "HEAD"
        assert _git(["rev-parse", "HEAD"], cwd=path) == tip
        assert _git(["status", "--porcelain"], cwd=path) == ""
        if dirty:
            assert "unfinished" in db.contexts[-1]["content"]


class TestSalvageUnpushedCommits:
    """§3.4: salvage must *push*, not merely leave a local branch behind.

    ``salvage_dirty`` archives uncommitted files.  Committed-but-unpushed
    work had nothing at all: the branch stayed in the slot, the slot was
    reset for the next task, and the commits were reachable from nowhere any
    other agent could look.  That is how task ``solid-harbor.43`` lost five
    commits and then failed three times over.
    """

    def _slot_with_unpushed_commit(self, mgr, base_ws, kind):
        slot = asyncio.run(mgr.create_slot(base_ws, kind, 0))
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-1")))
        d = Path(slot.workspace_path)
        (d / "work.py").write_text("print(1)\n")
        _git(["add", "-A"], cwd=d)
        _git(["commit", "-m", "real work"], cwd=d)
        return slot, d, _git(["rev-parse", "HEAD"], cwd=d)

    def test_restore_pushes_the_commits_before_resetting(
        self, mgr, base_ws, kind, base_repo, db
    ):
        slot, d, sha = self._slot_with_unpushed_commit(mgr, base_ws, kind)

        asyncio.run(mgr.restore_slot_after_task(slot, task_id="tsk-1"))

        origin = _git(["rev-parse", "--git-dir"], cwd=base_repo)  # sanity: real repo
        assert origin
        assert _git(["ls-remote", "origin", "refs/heads/aq/tsk-1"], cwd=d).split()[0] == sha
        assert db.meta[("tsk-1", "unmerged_branch")] == "aq/tsk-1"
        assert db.meta[("tsk-1", "unmerged_commit")] == sha

    def test_reset_for_the_next_task_pushes_the_predecessors_commits(
        self, mgr, base_ws, kind, db
    ):
        slot, d, sha = self._slot_with_unpushed_commit(mgr, base_ws, kind)

        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-2")))

        assert _git(["ls-remote", "origin", "refs/heads/aq/tsk-1"], cwd=d).split()[0] == sha
        assert db.meta[("tsk-1", "unmerged_branch")] == "aq/tsk-1"
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=d) == "aq/tsk-2"

    def test_a_clean_slot_records_nothing(self, mgr, base_ws, kind, db):
        slot = asyncio.run(mgr.create_slot(base_ws, kind, 0))
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-1")))

        asyncio.run(mgr.restore_slot_after_task(slot, task_id="tsk-1"))

        assert db.meta == {}


# ───────────────────────────── events registry ───────────────────────────


def test_worktree_events_are_registered():
    from src.event_schemas import EVENT_SCHEMAS

    for name in ("worktree.created", "worktree.reset", "worktree.reaped"):
        assert name in EVENT_SCHEMAS, f"{name} must be registered before it is emitted"


# ─────────────────── §3.4 branch affinity: find the holder ───────────────


class TestFindSlotHoldingBranch:
    """A released slot stays on its last task's branch (§3.4), so the *next*
    acquisition for that task must prefer that slot or collide with it.

    ``git worktree list --porcelain`` is the source of truth here on purpose:
    it is what git itself consults when it refuses the second checkout, so
    the hint cannot disagree with the refusal it exists to avoid.
    """

    def _two_slots(self, mgr, base_ws, kind):
        a = asyncio.run(mgr.create_slot(base_ws, kind, 0))
        b = asyncio.run(mgr.create_slot(base_ws, kind, 1))
        return a, b

    def test_finds_the_slot_left_on_the_task_branch(self, mgr, base_ws, kind):
        slot0, slot1 = self._two_slots(mgr, base_ws, kind)
        asyncio.run(mgr.reset_slot_for_task(slot0, FakeTask(id="tsk-1")))
        asyncio.run(mgr.restore_slot_after_task(slot0, task_id="tsk-1"))

        holder = asyncio.run(
            mgr.find_slot_holding_branch(base_ws, [slot0, slot1], task_branch_name("tsk-1"))
        )
        assert holder == slot0.id

    def test_no_holder_for_a_branch_nobody_has(self, mgr, base_ws, kind):
        slot0, slot1 = self._two_slots(mgr, base_ws, kind)
        holder = asyncio.run(
            mgr.find_slot_holding_branch(base_ws, [slot0, slot1], task_branch_name("tsk-9"))
        )
        assert holder is None

    def test_the_base_holding_the_branch_is_not_a_slot_hint(self, mgr, base_ws, kind, base_repo):
        """The base has the default branch checked out. It is never an agent
        cwd (§2.3), so it must not be returned as an affinity hint."""
        slot0, slot1 = self._two_slots(mgr, base_ws, kind)
        default = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=base_repo)
        holder = asyncio.run(
            mgr.find_slot_holding_branch(base_ws, [slot0, slot1], default)
        )
        assert holder is None

    def test_a_freshly_created_detached_slot_holds_nothing(self, mgr, base_ws, kind):
        """Slots are created ``--detach`` (§3.1): no branch is claimed until a
        task lands, so an unused slot never wins the hint."""
        slot0, _slot1 = self._two_slots(mgr, base_ws, kind)
        entries = asyncio.run(mgr.git.aworktree_list(base_ws.workspace_path))
        for e in entries:
            if e["path"].endswith("slot-0"):
                assert "branch" not in e

    def test_empty_inputs_and_a_broken_base_are_just_no_preference(self, mgr, base_ws, kind):
        slot0, _ = self._two_slots(mgr, base_ws, kind)
        assert asyncio.run(mgr.find_slot_holding_branch(base_ws, [slot0], None)) is None
        assert asyncio.run(mgr.find_slot_holding_branch(base_ws, [], "aq/x")) is None

        broken = Workspace(
            id="ws-gone",
            project_id="p1",
            workspace_path=str(Path(base_ws.workspace_path) / "does-not-exist"),
            source_type=RepoSourceType.CLONE,
            kind_id="project-repo",
        )
        # A base that cannot be queried means "no preference", not a failure:
        # dispatch must never hinge on an optimization.
        assert asyncio.run(mgr.find_slot_holding_branch(broken, [slot0], "aq/x")) is None


# ───────────────── exclude block: locking + atomic replacement ─────────────


_FOREIGN_RULE = "# operator's own rules\n*.swp\n"


def _drift(exclude: Path) -> None:
    """Atomically replace *exclude* with a foreign rule plus a stale block."""
    stale = f"{_FOREIGN_RULE}{EXCLUDE_BEGIN}\n/stale/\n{EXCLUDE_END}\n"
    tmp = exclude.with_name(f"exclude.drift.{threading.get_ident()}.tmp")
    tmp.write_text(stale, encoding="utf-8")
    os.replace(tmp, exclude)


def _watch(exclude: Path, stop: threading.Event, bad: list[bytes]) -> None:
    """Record every observation that is not a complete file."""
    while not stop.is_set():
        try:
            data = exclude.read_bytes()
        except FileNotFoundError:
            bad.append(b"<missing>")
            continue
        if b"*.swp" not in data:
            bad.append(data)


class TestGitExcludeConcurrency:
    def test_resolves_exclude_path_through_a_linked_worktree(
        self, base_repo: Path, tmp_path: Path
    ):
        wt = tmp_path / "linked"
        _git(["worktree", "add", "--detach", str(wt)], cwd=base_repo)
        assert (wt / ".git").is_file()

        assert WorktreeSlotManager.resolve_exclude_path(wt) == (
            base_repo / ".git" / "info" / "exclude"
        )
        assert WorktreeSlotManager.ensure_git_exclude_path(
            WorktreeSlotManager.resolve_exclude_path(wt)
        ) is True
        text = (base_repo / ".git" / "info" / "exclude").read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        assert "/.aq/" in text
        # Nothing was written into the worktree's private gitdir.
        assert not (base_repo / ".git" / "worktrees" / "linked" / "info").exists()

    def test_write_waits_for_the_exclude_lock(self, base_repo: Path):
        import fcntl

        exclude = base_repo / ".git" / "info" / "exclude"
        lock_path = WorktreeSlotManager.exclude_lock_path(base_repo)
        assert lock_path.parent == exclude.parent
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = os.open(lock_path, os.O_RDWR | os.O_CREAT)
        fcntl.flock(holder, fcntl.LOCK_EX)

        results: list[bool] = []
        t = threading.Thread(
            target=lambda: results.append(
                WorktreeSlotManager.ensure_git_exclude_path(exclude)
            )
        )
        t.start()
        t.join(0.5)
        assert t.is_alive(), "writer did not wait for the lock"
        assert not exclude.exists() or "/.aq/" not in exclude.read_text()

        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
        t.join(5)
        assert not t.is_alive()
        assert results == [True]
        assert "/.aq/" in exclude.read_text()

    def test_threads_never_expose_a_truncated_file(self, base_repo: Path):
        exclude = base_repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(_FOREIGN_RULE, encoding="utf-8")

        stop = threading.Event()
        bad: list[bytes] = []
        successful_writes: list[bool] = []
        unexpected_errors: list[OSError] = []
        watcher = threading.Thread(target=_watch, args=(exclude, stop, bad))
        watcher.start()

        def hammer() -> None:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                _drift(exclude)
                try:
                    if WorktreeSlotManager.ensure_git_exclude_path(exclude):
                        successful_writes.append(True)
                except OSError as exc:
                    # Another deliberately unlocked _drift may win between
                    # replacement and verification.  Fail-closed is correct;
                    # this stress test asserts only that readers never see a
                    # partial file.
                    if "could not be verified" not in str(exc):
                        unexpected_errors.append(exc)
                        return

        writers = [threading.Thread(target=hammer) for _ in range(8)]
        for w in writers:
            w.start()
        for w in writers:
            w.join()
        stop.set()
        watcher.join()

        assert bad == [], f"{len(bad)} incomplete observation(s), first: {bad[0]!r}"
        assert unexpected_errors == []
        assert successful_writes
        WorktreeSlotManager.ensure_git_exclude_path(exclude)
        assert WorktreeSlotManager.git_exclude_is_current_path(exclude)
        text = exclude.read_text(encoding="utf-8")
        assert "*.swp" in text
        assert text.count("# >>> agent-queue managed") == 1
        assert "/.aq/" in text
        assert not list(exclude.parent.glob("*.tmp")), "temp files left behind"

    def test_processes_never_expose_a_truncated_file(self, base_repo: Path):
        exclude = base_repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(_FOREIGN_RULE, encoding="utf-8")

        script = (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "from src.orchestrator.worktree_manager import (\n"
            "    EXCLUDE_BEGIN, EXCLUDE_END, WorktreeSlotManager)\n"
            "base = Path(sys.argv[1]); exclude = base / '.git' / 'info' / 'exclude'\n"
            "stale = sys.argv[2] + EXCLUDE_BEGIN + '\\n/stale/\\n' + EXCLUDE_END + '\\n'\n"
            "succeeded = False\n"
            "deadline = time.monotonic() + 1.0\n"
            "while time.monotonic() < deadline:\n"
            "    tmp = exclude.with_name(f'exclude.drift.{os.getpid()}.tmp')\n"
            "    tmp.write_text(stale, encoding='utf-8'); os.replace(tmp, exclude)\n"
            "    try:\n"
            "        if WorktreeSlotManager.ensure_git_exclude_path(exclude):\n"
            "            succeeded = True\n"
            "    except OSError as exc:\n"
            "        if 'could not be verified' not in str(exc):\n"
            "            raise\n"
            "if not succeeded:\n"
            "    raise RuntimeError('no managed exclude write succeeded')\n"
        )
        stop = threading.Event()
        bad: list[bytes] = []
        watcher = threading.Thread(target=_watch, args=(exclude, stop, bad))
        watcher.start()
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(base_repo), _FOREIGN_RULE],
                cwd=str(Path(__file__).resolve().parent.parent),
                stderr=subprocess.PIPE,
            )
            for _ in range(6)
        ]
        errs = [p.communicate()[1] for p in procs]
        stop.set()
        watcher.join()

        assert all(p.returncode == 0 for p in procs), errs
        assert bad == [], f"{len(bad)} incomplete observation(s), first: {bad[0]!r}"
        WorktreeSlotManager.ensure_git_exclude_path(exclude)
        assert WorktreeSlotManager.git_exclude_is_current_path(exclude)
        text = exclude.read_text(encoding="utf-8")
        assert "*.swp" in text
        assert text.count("# >>> agent-queue managed") == 1
        assert "/.aq/" in text

    def test_verifies_the_replacement_landed(self, base_repo: Path, monkeypatch):
        exclude = base_repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(_FOREIGN_RULE, encoding="utf-8")

        # A replace that silently does nothing: the post-write read must
        # notice the block never landed instead of reporting success.
        def lost_replace(src, dst, *a, **kw):
            os.remove(src)

        monkeypatch.setattr(os, "replace", lost_replace)
        with pytest.raises(OSError, match="could not be verified"):
            WorktreeSlotManager.ensure_git_exclude_path(exclude)
        assert exclude.read_text(encoding="utf-8") == _FOREIGN_RULE

    def test_preserves_existing_exclude_mode(self, base_repo: Path):
        exclude = base_repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(_FOREIGN_RULE, encoding="utf-8")
        exclude.chmod(0o640)

        assert WorktreeSlotManager.ensure_git_exclude_path(exclude) is True

        assert exclude.stat().st_mode & 0o777 == 0o640

    def test_chmod_failure_leaves_existing_exclude_unchanged(
        self, base_repo: Path, monkeypatch
    ):
        exclude = base_repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(_FOREIGN_RULE, encoding="utf-8")
        original = exclude.read_bytes()

        def fail_chmod(_path, _mode):
            raise OSError("mode preservation failed")

        monkeypatch.setattr(os, "chmod", fail_chmod)
        with pytest.raises(OSError, match="mode preservation failed"):
            WorktreeSlotManager.ensure_git_exclude_path(exclude)

        assert exclude.read_bytes() == original
        assert not list(exclude.parent.glob("exclude.*.tmp"))


@pytest.mark.asyncio
async def test_exact_available_origin_avoids_fetch(mgr, base_repo):
    sha = _git(["rev-parse", "HEAD"], base_repo)
    fetch = AsyncMock(side_effect=AssertionError("exact available commit needs no fetch"))
    mgr.git.afetch_origin = fetch
    assert await mgr._fetch_and_resolve_start_ref(
        str(base_repo), sha, required=True,
    ) == sha
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_exact_origin_still_fetches(mgr, base_repo):
    fetch = AsyncMock()
    mgr.git.afetch_origin = fetch
    with pytest.raises(GitError, match="cannot resolve"):
        await mgr._fetch_and_resolve_start_ref(str(base_repo), "a" * 40, required=True)
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_unheld_branch_skips_peer_liveness_reads(mgr, base_ws, monkeypatch):
    monkeypatch.setattr(mgr, "_base_of_slot_dir", AsyncMock(return_value=base_ws))
    monkeypatch.setattr(mgr.git, "aworktree_list", AsyncMock(return_value=[{
        "path": base_ws.workspace_path, "branch": "main",
    }]))
    monkeypatch.setattr(mgr.db, "list_sessions", AsyncMock(side_effect=AssertionError("no peer")))
    monkeypatch.setattr(mgr.db, "list_workspaces", AsyncMock(side_effect=AssertionError("no peer")))
    await mgr._detach_stale_branch_holders(Path(base_ws.workspace_path) / "slot", "aq/new")


@pytest.mark.asyncio
async def test_available_exact_origin_still_refreshes_mutable_child_refs(mgr, base_repo):
    sha = _git(["rev-parse", "HEAD"], base_repo)
    fetch = AsyncMock()
    mgr.git.afetch_origin = fetch
    assert await mgr._fetch_and_resolve_start_ref(
        str(base_repo), sha, required=True, refresh_refs=True,
    ) == sha
    fetch.assert_awaited_once()


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_setup_cancellation_settles_children_before_return(tmp_path, interruption):
    from src.git.process import _group_has_writer
    import signal
    import shlex

    if os.name != "posix":
        pytest.skip("POSIX process groups")
    script = tmp_path / "setup.py"
    script.write_text(
        "import os,pathlib,subprocess,sys,time\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "pathlib.Path('ready').write_text(str(os.getpgrp()))\n"
        "p.wait()\n"
    )
    mgr = WorktreeSlotManager(
        db=None, git=GitManager(), bus=None, git_mutex=lambda _: None,
        config=WorktreesConfig(setup_timeout_seconds=1 if interruption == "timeout" else 60),
    )
    request = asyncio.create_task(mgr._run_setup(
        tmp_path, [shlex.join([sys.executable, str(script)])],
    ))
    pgid = None
    try:
        async with asyncio.timeout(5):
            while not (tmp_path / "ready").exists():
                await asyncio.sleep(.01)
        pgid = int((tmp_path / "ready").read_text())
        if interruption == "cancel":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, timeout=10)
        else:
            await asyncio.wait_for(request, timeout=10)
        assert not _group_has_writer(pgid)
    finally:
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_unknown_cleanup_cannot_be_swallowed_before_reset(mgr, base_repo, monkeypatch):
    from src.git.process import ProcessCleanupError

    monkeypatch.setattr(mgr.git, "aabort_in_progress_operations", AsyncMock(
        side_effect=ProcessCleanupError("unproved child liveness"),
    ))
    with pytest.raises(ProcessCleanupError):
        await mgr._abort_in_progress(base_repo)
