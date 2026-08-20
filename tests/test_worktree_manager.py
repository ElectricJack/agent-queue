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
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.config import WorktreesConfig
from src.git.manager import GitError, GitManager
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


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
    """A clone of a bare origin, with one commit on ``main``."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    base = tmp_path / "base"
    subprocess.run(
        ["git", "clone", str(origin), str(base)], check=True, capture_output=True
    )
    (base / "README.md").write_text("init\n")
    (base / ".gitignore").write_text("node_modules/\n")
    _git(["add", "-A"], cwd=base)
    _git(["commit", "-m", "init"], cwd=base)
    _git(["push", "origin", "main"], cwd=base)
    return base


class FakeDB:
    """Minimal DB double: the four methods the slot manager actually calls."""

    def __init__(self):
        self.workspaces: dict[str, Workspace] = {}
        self.contexts: list[dict] = []

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
    def test_creates_block_when_absent(self, base_repo: Path):
        assert WorktreeSlotManager.ensure_git_exclude(base_repo) is True
        text = (base_repo / ".git" / "info" / "exclude").read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        assert EXCLUDE_BEGIN in text and EXCLUDE_END in text
        assert "/.aq/" in text
        # info/exclude lives in the common dir, so this one line keeps the
        # sentinel out of `git status` in every slot.
        assert "/.aq-worktree.json" in text

    def test_is_idempotent(self, base_repo: Path):
        WorktreeSlotManager.ensure_git_exclude(base_repo)
        exclude = base_repo / ".git" / "info" / "exclude"
        first = exclude.read_bytes()
        assert WorktreeSlotManager.ensure_git_exclude(base_repo) is False
        assert exclude.read_bytes() == first

    def test_preserves_foreign_content(self, base_repo: Path):
        exclude = base_repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("# operator's own rules\n*.swp\n")
        WorktreeSlotManager.ensure_git_exclude(base_repo)
        text = exclude.read_text()
        assert "# operator's own rules" in text
        assert "*.swp" in text
        assert "/.aq/" in text

    def test_rewrites_a_drifted_block(self, base_repo: Path):
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
        assert WorktreeSlotManager.ensure_git_exclude(base_repo) is True
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


# ─────────────────────────────── §3.2 reset ──────────────────────────────


class TestResetSlot:
    def _make_slot(self, mgr, base_ws, kind):
        return asyncio.run(mgr.create_slot(base_ws, kind, 0))

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
        slot = self._make_slot(mgr, base_ws, kind)
        asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-r")))
        (Path(slot.workspace_path) / "attempt.txt").write_text("one")
        _git(["add", "-A"], cwd=slot.workspace_path)
        _git(["commit", "-m", "attempt one"], cwd=slot.workspace_path)

        branch = asyncio.run(mgr.reset_slot_for_task(slot, FakeTask(id="tsk-r")))
        assert branch == "aq/tsk-r"
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=slot.workspace_path) == "aq/tsk-r"
        # A retry starts clean: the failed attempt's commit is dropped.
        assert not (Path(slot.workspace_path) / "attempt.txt").exists()

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


# ───────────────────────────── events registry ───────────────────────────


def test_worktree_events_are_registered():
    from src.event_schemas import EVENT_SCHEMAS

    for name in ("worktree.created", "worktree.reset", "worktree.reaped"):
        assert name in EVENT_SCHEMAS, f"{name} must be registered before it is emitted"
