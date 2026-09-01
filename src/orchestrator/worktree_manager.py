"""WorktreeSlotManager — per-slot git worktrees.

Implements worktree-execution design §2–§3 and implementation spec §4
(phases 0–2).  A **slot** is a reusable worktree at
``<base_repo>/.aq/worktrees/slot-<n>``; a **base** is the project's normal
clone.  Agents run in slots, one task at a time; the branch — not the
worktree — is the durable artifact.

What lives here (P1 + P2)
-------------------------
* :meth:`WorktreeSlotManager.ensure_slots` / :meth:`create_slot` — lazy slot
  provisioning up to the project's agent cap.
* :meth:`reset_slot_for_task` — the per-assignment reset (salvage → fetch →
  hard reset + clean → fresh ``aq/<task_id>`` branch → sentinel).
* :meth:`restore_slot_after_task` — the slot-shaped replacement for the
  clone path's commit → stash → ``clean -fdx`` cleanup ladder, none of whose
  rungs is safe inside a worktree.
* :meth:`salvage_dirty` — archive a crashed predecessor's work as a patch on
  a ``task_context`` row rather than stashing it.  ``git stash`` is
  deliberately never used: the stash stack is shared by every worktree of a
  repository, so a pop in one slot can restore another slot's work.
* :meth:`ensure_git_exclude` — the idempotent ``.git/info/exclude`` marker.
* Sentinel I/O (``<slot>/.aq-worktree.json``).

Deliberately **not** here yet: ``adopt_existing``, ``reap_slot`` and
``prune_branches`` (phase 4), and everything merge-slot (phase 3).

Path handling uses :mod:`pathlib` throughout — the runtime target is WSL2
but development happens on Windows, so no separator is ever assumed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from collections.abc import Callable
from pathlib import Path

from dataclasses import dataclass, field

from src.config import WorktreesConfig
from src.git.manager import GitError, _validate_ref
from src.models import (
    RepoSourceType,
    WORKTREE_SENTINEL_NAME,
    Workspace,
    WorkspaceKind,
    WorktreeSentinel,
    worktree_setup_hash,
)


@dataclass
class AdoptReport:
    """Result of :meth:`WorktreeSlotManager.adopt_existing`.

    * ``adopted`` — workspace ids for slots whose row + directory + sentinel
      all check out on boot.
    * ``repaired`` — workspace ids for bases whose ``.git/info/exclude``
      block was missing or drifted and has now been rewritten.
    * ``pruned`` — worktree paths (or "prune" sentinel) that had a stale
      ``.git/worktrees`` registration and were pruned.
    """

    adopted: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)

logger = logging.getLogger(__name__)

#: Directory (relative to the base repo) that holds every slot.
SLOTS_DIRNAME = Path(".aq") / "worktrees"

#: Marker block written to ``<base>/.git/info/exclude`` (design §2.4).
EXCLUDE_BEGIN = "# >>> agent-queue managed — do not edit between markers >>>"
EXCLUDE_END = "# <<< agent-queue managed <<<"
#: Design §2.4 specifies ``/.aq/``.  The sentinel is listed alongside it:
#: ``.git/info/exclude`` lives in the *common* dir and is therefore shared by
#: every worktree of the repository, so one line here keeps ``git status``
#: clean in every slot.  Without it the sentinel shows up as untracked in each
#: slot and gets swept into the salvage patch.
#:
#: ``/.codex/`` is here for the same reason: Codex discovers its hook file by
#: path only (no ``--settings`` analogue), so the session runtime has to write
#: ``<work_dir>/.codex/hooks.json`` into the slot rather than pass it as a
#: flag.  Excluding only untracked paths, so a repo that genuinely tracks
#: ``.codex/`` is unaffected.
EXCLUDE_BODY = "/.aq/\n/.aq-worktree.json\n/.codex/"
EXCLUDE_BLOCK = f"{EXCLUDE_BEGIN}\n{EXCLUDE_BODY}\n{EXCLUDE_END}\n"

#: The block is *located* by these ASCII-only prefixes rather than by
#: ``EXCLUDE_BEGIN`` itself.  The design-mandated marker contains an em-dash,
#: and a copy of the file written by another tool in a non-UTF-8 encoding
#: would not match the exact string — leaving us appending a second block on
#: every start instead of rewriting the first.
_EXCLUDE_BEGIN_ASCII = "# >>> agent-queue managed"
_EXCLUDE_END_ASCII = "# <<< agent-queue managed <<<"

#: Branch prefix every task branch carries.  Fixed — no title slug — so the
#: branch is derivable from the task id alone (design §3.2).
BRANCH_PREFIX = "aq/"


def slot_name(slot_index: int) -> str:
    return f"slot-{slot_index}"


def slot_path(base_path: str | Path, slot_index: int) -> Path:
    return Path(base_path) / SLOTS_DIRNAME / slot_name(slot_index)


def task_branch_name(task_id: str) -> str:
    return f"{BRANCH_PREFIX}{task_id}"


class WorktreeSlotManager:
    """Create, reset and salvage slot worktrees for one daemon.

    Every git call that touches the base repository or the shared
    ``.git/worktrees`` registry runs under the caller-supplied per-base
    mutex, so concurrent acquisitions on one repo serialize.
    """

    def __init__(
        self,
        db,
        git,
        bus,
        config: WorktreesConfig,
        git_mutex: Callable[[str], asyncio.Lock],
        *,
        daemon_epoch: str = "",
    ):
        self.db = db
        self.git = git
        self.bus = bus
        self.config = config
        self._git_mutex = git_mutex
        # Per-base provisioning lock.  Distinct from the git mutex, which
        # ``create_slot`` takes internally — asyncio.Lock is not reentrant, so
        # ensure_slots cannot reuse it.  Without this, two concurrent
        # dispatches both see "no free slot", both try to create slot N, and
        # the partial unique index on (base_workspace_id, slot_index) turns
        # the loser into an IntegrityError instead of a queued wait.
        self._provision_locks: dict[str, asyncio.Lock] = {}
        self.daemon_epoch = daemon_epoch or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )

    # ───────────────────────────── exclude block ─────────────────────────

    @staticmethod
    def ensure_git_exclude(base_path: str | Path) -> bool:
        """Idempotently maintain the ``.aq/`` block in ``.git/info/exclude``.

        Returns True when the file was written.  Foreign content outside the
        markers is preserved verbatim; a drifted block is rewritten in place.
        No commit is involved, so this works for repos we do not own
        (design §2.4).
        """
        info_dir = Path(base_path) / ".git" / "info"
        exclude = info_dir / "exclude"
        try:
            # git's shipped template is not guaranteed UTF-8 (it is cp1252 on
            # some Windows installs) and an operator's own rules may be in any
            # encoding.  ``surrogateescape`` round-trips undecodable bytes
            # untouched instead of raising or corrupting them.
            #
            # ``newline=""`` on both read and write disables universal-newline
            # translation, so an LF-terminated exclude file is not silently
            # rewritten as CRLF on Windows (and vice versa) just because we
            # appended one block to it.
            if exclude.exists():
                with exclude.open(
                    encoding="utf-8", errors="surrogateescape", newline=""
                ) as f:
                    existing = f.read()
            else:
                existing = ""
        except OSError as e:
            logger.warning("Cannot read %s: %s", exclude, e)
            return False

        start = existing.find(_EXCLUDE_BEGIN_ASCII)
        end = existing.find(_EXCLUDE_END_ASCII)
        if start != -1 and end != -1 and end > start:
            current = existing[start : end + len(_EXCLUDE_END_ASCII)]
            if current.strip() == EXCLUDE_BLOCK.strip():
                return False  # already correct — leave it alone
            updated = (
                existing[:start]
                + EXCLUDE_BLOCK.rstrip("\n")
                + existing[end + len(_EXCLUDE_END_ASCII) :]
            )
        else:
            prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
            updated = prefix + EXCLUDE_BLOCK

        try:
            info_dir.mkdir(parents=True, exist_ok=True)
            exclude.write_text(
                updated, encoding="utf-8", errors="surrogateescape", newline=""
            )
        except OSError as e:
            logger.warning("Cannot write %s: %s", exclude, e)
            return False
        return True

    # ─────────────────────────────── sentinel ────────────────────────────

    @staticmethod
    def sentinel_path(slot_dir: str | Path) -> Path:
        return Path(slot_dir) / WORKTREE_SENTINEL_NAME

    @classmethod
    def read_sentinel(cls, slot_dir: str | Path) -> WorktreeSentinel | None:
        """Read a slot's sentinel, or ``None`` when absent/unparseable."""
        p = cls.sentinel_path(slot_dir)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return WorktreeSentinel.from_dict(data)

    @classmethod
    def write_sentinel(cls, slot_dir: str | Path, sentinel: WorktreeSentinel) -> None:
        p = cls.sentinel_path(slot_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(sentinel.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # ───────────────────────────── provisioning ──────────────────────────

    async def ensure_slots(
        self,
        project,
        base_ws: Workspace,
        kind: WorkspaceKind,
        count: int,
    ) -> list[Workspace]:
        """Create missing slot rows/dirs for *base_ws* up to *count*.

        Lazy (design §3.1): only the gap between the slots that already exist
        and *count* is filled, and creation failures are per-slot — a fetch
        outage that blocks slot 3 still leaves slots 0–2 usable.  Returns
        every slot row for the base, existing and new, ordered by index.
        """
        lock = self._provision_locks.setdefault(base_ws.id, asyncio.Lock())
        async with lock:
            return await self._ensure_slots_locked(base_ws, kind, count)

    async def _ensure_slots_locked(
        self, base_ws: Workspace, kind: WorkspaceKind, count: int
    ) -> list[Workspace]:
        existing = await self._slots_for_base(base_ws.id)
        by_index = {ws.slot_index: ws for ws in existing}
        for idx in range(max(0, count)):
            if idx in by_index:
                continue
            try:
                by_index[idx] = await self.create_slot(base_ws, kind, idx)
            except (GitError, OSError) as e:
                # Slot creation requires one successful fetch or an existing
                # ref (design §3.5).  Give up on the remaining indices — they
                # would fail the same way — and let the caller work with what
                # exists.
                logger.warning(
                    "Slot %d creation failed for base %s: %s", idx, base_ws.id, e
                )
                break
        return [by_index[i] for i in sorted(by_index)]

    async def create_slot(
        self,
        base_ws: Workspace,
        kind: WorkspaceKind,
        slot_index: int,
    ) -> Workspace:
        """Provision one slot worktree and register its ``workspaces`` row.

        exclude-block → prune → fetch → ``worktree add --detach`` →
        ``worktree_setup`` → sentinel → DB row → ``worktree.created``.
        """
        from src.workspace_names import generate_workspace_id

        base_path = base_ws.workspace_path
        target = slot_path(base_path, slot_index)
        default_branch = await self._default_branch(base_path)

        async with self._git_mutex(base_path):
            self.ensure_git_exclude(base_path)
            try:
                await self.git.aworktree_prune(base_path)
            except GitError as e:
                logger.debug("worktree prune failed in %s: %s", base_path, e)

            start_ref = await self._fetch_and_resolve_start_ref(
                base_path, default_branch, required=True
            )

            if target.exists() and any(target.iterdir()):
                # A directory survived without a row (crash between
                # `worktree add` and the INSERT).  Re-register rather than
                # destroy: adoption proper is phase 4, but refusing to
                # clobber is correct at every phase.
                logger.info("Reusing existing slot directory %s", target)
            else:
                await self.git.aworktree_add(
                    base_path, str(target), ref=start_ref, detach=True
                )

        setup_hash = worktree_setup_hash(kind.worktree_setup)
        await self._run_setup(target, kind.worktree_setup)

        ws_id = await generate_workspace_id(self.db)
        slot_ws = Workspace(
            id=ws_id,
            project_id=base_ws.project_id,
            workspace_path=str(target),
            source_type=RepoSourceType.WORKTREE,
            name=f"{base_ws.id}:{slot_name(slot_index)}",
            kind_id=kind.id,
            slot_index=slot_index,
            base_workspace_id=base_ws.id,
        )
        await self.db.create_workspace(slot_ws)

        self.write_sentinel(
            target,
            WorktreeSentinel(
                slot=slot_name(slot_index),
                slot_index=slot_index,
                base_workspace_id=base_ws.id,
                project_id=base_ws.project_id,
                workspace_id=ws_id,
                created_at=time.time(),
                daemon_epoch=self.daemon_epoch,
                setup_hash=setup_hash,
            ),
        )

        await self._emit(
            "worktree.created",
            {
                "project_id": base_ws.project_id,
                "workspace_id": ws_id,
                "slot": slot_name(slot_index),
                "path": str(target),
                "base_workspace_id": base_ws.id,
            },
        )
        logger.info("Created worktree slot %s at %s", slot_name(slot_index), target)
        return slot_ws

    # ─────────────────────────────── per-task ────────────────────────────

    async def reset_slot_for_task(
        self,
        slot_ws: Workspace,
        task,
        *,
        base_branch: str | None = None,
        resume_branch: str | None = None,
        kind: WorkspaceKind | None = None,
    ) -> str:
        """Bring a slot to a pristine per-task state.  Returns the branch.

        Design §3.2: salvage-if-dirty → fetch → ``reset --hard`` +
        ``clean -fd`` (never ``-x``, so gitignored caches survive) → fresh
        ``aq/<task_id>`` (or *resume_branch*) → sentinel → ``worktree.reset``.
        """
        # ``base_branch`` arrives from task metadata — untrusted text by
        # trust-and-ops §2.2.  Validate here rather than letting an
        # unresolvable value fall through to the "start from HEAD" path, which
        # would silently ignore a hostile *or* merely typo'd value.
        if base_branch is not None:
            _validate_ref(base_branch, field="base branch")
        if resume_branch is not None:
            _validate_ref(resume_branch, field="resume branch")

        slot_dir = Path(slot_ws.workspace_path)
        from src.orchestrator.task_checkpoint import prepare_checkpoint, restore_checkpoint
        checkpoint = await prepare_checkpoint(self.db, self.git, task.id, str(slot_dir))
        if checkpoint:
            # No hard-reset/clean on the saved task branch. Previous users'
            # dirty work is salvaged before switching back to the checkpoint.
            prev = self.read_sentinel(slot_dir)
            await self.salvage_dirty(slot_ws, prev.task_id if prev else None, incoming_task_id=task.id)
            await self.git._arun(["reset", "--hard"], cwd=str(slot_dir))
            await self.git._arun(["clean", "-fd", "-e", WORKTREE_SENTINEL_NAME], cwd=str(slot_dir))
            branch = await restore_checkpoint(self.db, self.git, task.id, str(slot_dir), saved=checkpoint)
            self.write_sentinel(slot_dir, WorktreeSentinel(
                slot=slot_name(slot_ws.slot_index or 0), slot_index=slot_ws.slot_index or 0,
                base_workspace_id=slot_ws.base_workspace_id or "", project_id=slot_ws.project_id,
                workspace_id=slot_ws.id, task_id=task.id, branch=branch,
                created_at=prev.created_at if prev else time.time(),
                assigned_at=time.time(), daemon_epoch=self.daemon_epoch,
                setup_hash=prev.setup_hash if prev else "",
            ))
            return branch
        base_ws = await self._base_of(slot_ws)
        base_path = base_ws.workspace_path if base_ws else str(slot_dir)
        prev = self.read_sentinel(slot_dir)

        # A slot left mid-rebase — or holding a stale index.lock — by a killed
        # agent fails both `reset --hard` and `switch`, which would send every
        # task that lands on it down the PAUSED path indefinitely.  The legacy
        # clone path has always cleared that first (`aprepare_for_task`);
        # slots need it just as much.  Outside the mutex block, because this
        # goes through the locking ``_arun``.
        await self._abort_in_progress(slot_dir)

        salvaged = await self.salvage_dirty(
            slot_ws, prev.task_id if prev else None, incoming_task_id=task.id
        )

        default_branch = await self._default_branch(base_path)
        start_point = base_branch or default_branch

        async with self._git_mutex(base_path):
            start_ref = await self._fetch_and_resolve_start_ref(
                base_path, start_point, required=False, cwd=str(slot_dir)
            )

            # Hard reset + clean.  The sentinel is exempted so a reset never
            # destroys the record of what this slot is.
            await self.git._arun_unlocked(["reset", "--hard"], cwd=str(slot_dir))
            await self.git._arun_unlocked(
                ["clean", "-fd", "-e", WORKTREE_SENTINEL_NAME], cwd=str(slot_dir)
            )

            branch = resume_branch or task_branch_name(task.id)
            await self._switch_to_branch(
                slot_dir, branch, start_ref, resume=bool(resume_branch)
            )

        if kind is not None:
            desired = worktree_setup_hash(kind.worktree_setup)
            if prev is None or prev.setup_hash != desired:
                await self._run_setup(slot_dir, kind.worktree_setup)
        else:
            desired = prev.setup_hash if prev else ""

        now = time.time()
        self.write_sentinel(
            slot_dir,
            WorktreeSentinel(
                slot=slot_name(slot_ws.slot_index or 0),
                slot_index=slot_ws.slot_index if slot_ws.slot_index is not None else 0,
                base_workspace_id=slot_ws.base_workspace_id or "",
                project_id=slot_ws.project_id,
                workspace_id=slot_ws.id,
                task_id=task.id,
                branch=branch,
                created_at=prev.created_at if prev else now,
                assigned_at=now,
                daemon_epoch=self.daemon_epoch,
                setup_hash=desired,
            ),
        )

        await self._emit(
            "worktree.reset",
            {
                "project_id": slot_ws.project_id,
                "workspace_id": slot_ws.id,
                "slot": slot_name(slot_ws.slot_index or 0),
                "task_id": task.id,
                "branch": branch,
                "salvaged": salvaged,
            },
        )
        return branch

    async def restore_slot_after_task(
        self,
        slot_ws: Workspace,
        *,
        task_id: str | None = None,
    ) -> bool:
        """Return a slot to a clean tree after a task ends badly.

        The slot-mode replacement for the legacy
        ``_cleanup_workspace_for_next_task``, whose three-step
        commit → **stash** → ``clean -fdx`` ladder is wrong in a worktree on
        every rung:

        * ``git stash`` pushes onto the **repository-wide** stash stack, which
          every worktree of the base shares — a pop in one slot can restore
          another slot's work.  Design §3.2 forbids it outright, which is why
          :meth:`salvage_dirty` exists.
        * ``clean -fdx`` destroys the gitignored caches (``node_modules``,
          build output) whose survival is the entire economic argument for
          reusing a slot.
        * ``git checkout <default_branch>`` fails hard in a slot — git refuses
          a branch already checked out in the base, which is the normal
          topology.

        So: salvage → ``reset --hard`` → ``clean -fd`` (never ``-x``), and the
        slot deliberately stays on its task branch.  The branch is the durable
        artifact (§3.4); the next :meth:`reset_slot_for_task` moves it.

        Returns True when uncommitted work was archived to a task context.
        """
        slot_dir = Path(slot_ws.workspace_path)
        base_ws = await self._base_of(slot_ws)
        base_path = base_ws.workspace_path if base_ws else str(slot_dir)
        prev = self.read_sentinel(slot_dir)

        await self._abort_in_progress(slot_dir)
        salvaged = await self.salvage_dirty(
            slot_ws, prev.task_id if prev else task_id, incoming_task_id=task_id
        )

        async with self._git_mutex(base_path):
            try:
                await self.git._arun_unlocked(["reset", "--hard"], cwd=str(slot_dir))
                await self.git._arun_unlocked(
                    ["clean", "-fd", "-e", WORKTREE_SENTINEL_NAME], cwd=str(slot_dir)
                )
            except GitError as e:
                # Never fatal: the next reset_slot_for_task repeats this under
                # its own error handling, and a slot that cannot be cleaned
                # simply fails acquisition-time setup instead of a task.
                logger.warning("Could not restore slot %s: %s", slot_dir, e)
        return salvaged

    async def _abort_in_progress(self, slot_dir: Path | str) -> None:
        """Best-effort ``merge/rebase/cherry-pick --abort`` + stale-lock sweep."""
        try:
            await self.git.aabort_in_progress_operations(str(slot_dir))
        except Exception as e:  # best-effort by contract
            logger.debug("Abort-in-progress failed in %s: %s", slot_dir, e)

    async def salvage_dirty(
        self,
        slot_ws: Workspace,
        prev_task_id: str | None,
        *,
        incoming_task_id: str | None = None,
    ) -> bool:
        """Archive a dirty slot's work as a patch, then let the caller reset.

        Never stashes: the stash stack is shared by every worktree of a
        repository, so a ``pop`` in one slot can restore another slot's
        work.  The patch lands on the *previous* task (from the sentinel) so
        it is attributable; on the incoming task when the predecessor is
        unknown.  Returns True when something was archived.

        ``--binary`` is not optional: without it git emits only
        ``Binary files a/x and b/x differ`` and the following ``reset --hard``
        destroys the bytes, so an image, a fixture or a compiled asset would
        be silently unrecoverable.  Patches past ``salvage_max_bytes`` are
        replaced by their ``--stat`` summary rather than stored whole.
        """
        if not self.config.salvage_dirty:
            return False
        slot_dir = str(slot_ws.workspace_path)
        try:
            if not await self.git.ahas_uncommitted_changes(slot_dir):
                return False
            await self.git._arun_unlocked(["add", "-A"], cwd=slot_dir)
            patch = await self.git.astaged_patch(slot_dir)
        except GitError as e:
            logger.warning("Salvage failed in %s: %s", slot_dir, e)
            return False

        if not patch.strip():
            return False

        owner = prev_task_id or incoming_task_id
        if not owner:
            logger.warning("Dirty slot %s has no owner to attribute salvage to", slot_dir)
            return False

        cap = getattr(self.config, "salvage_max_bytes", 0) or 0
        if cap and len(patch.encode("utf-8", "replace")) > cap:
            try:
                stat = await self.git._arun_unlocked(
                    ["diff", "--cached", "--stat", "HEAD"], cwd=slot_dir
                )
            except GitError:
                stat = "(diffstat unavailable)"
            logger.warning(
                "Salvage patch from %s exceeds salvage_max_bytes=%d — archiving "
                "the diffstat only; the working-tree content is not recoverable",
                slot_dir,
                cap,
            )
            patch = (
                f"# Salvage patch omitted: it exceeded worktrees.salvage_max_bytes "
                f"({cap} bytes).\n"
                f"# Only the diffstat is kept: the contents of the files below "
                f"were NOT archived and are gone after the slot reset.\n\n{stat}"
            )
        try:
            await self.db.add_task_context(
                owner,
                type="worktree_salvage",
                label=f"Salvaged uncommitted work from {slot_ws.workspace_path}",
                content=patch,
            )
        except Exception as e:  # DB shape varies across adapters; never block a task
            logger.warning("Could not record worktree_salvage context: %s", e)
            return False
        logger.info(
            "Salvaged %d bytes of uncommitted work from %s onto task %s",
            len(patch),
            slot_dir,
            owner,
        )
        return True

    # ──────────────────────────────── helpers ────────────────────────────

    async def _slots_for_base(self, base_workspace_id: str) -> list[Workspace]:
        rows = await self.db.list_workspaces()
        return [
            ws
            for ws in rows
            if ws.base_workspace_id == base_workspace_id and ws.slot_index is not None
        ]

    async def _base_of(self, slot_ws: Workspace) -> Workspace | None:
        if not slot_ws.base_workspace_id:
            return None
        return await self.db.get_workspace(slot_ws.base_workspace_id)

    async def _default_branch(self, base_path: str) -> str:
        try:
            return await self.git.aget_default_branch(base_path)
        except Exception:
            return "main"

    async def _fetch_and_resolve_start_ref(
        self,
        base_path: str,
        branch: str,
        *,
        required: bool,
        cwd: str | None = None,
    ) -> str:
        """Fetch in the base and return the ref new work should start from.

        Prefers ``origin/<branch>``.  Design §3.5: a fetch failure is
        non-fatal *if* the ref already exists locally; slot **creation**
        (``required=True``) still needs a resolvable start point or it fails
        so the task takes the no-workspace PAUSED backoff.
        """
        fetch_failed = False
        try:
            if await self.git.ahas_remote(base_path):
                await self.git._arun_unlocked(["fetch", "origin"], cwd=base_path)
        except GitError as e:
            fetch_failed = True
            logger.warning("fetch origin failed in %s: %s", base_path, e)

        probe_cwd = cwd or base_path
        for candidate in (f"origin/{branch}", branch):
            if await self._ref_exists(probe_cwd, candidate):
                if fetch_failed:
                    logger.warning(
                        "Using possibly stale ref %s in %s (fetch failed)",
                        candidate,
                        probe_cwd,
                    )
                return candidate
        if required:
            raise GitError(
                f"cannot resolve a start ref for {branch!r} in {base_path} "
                "(no origin/<branch> and no local branch)"
            )
        return "HEAD"

    async def _ref_exists(self, cwd: str, ref: str) -> bool:
        try:
            await self.git._arun_unlocked(
                ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=cwd
            )
            return True
        except GitError:
            return False

    async def _switch_to_branch(
        self,
        slot_dir: Path,
        branch: str,
        start_ref: str,
        *,
        resume: bool,
    ) -> None:
        """Create or resume *branch* in the slot.

        Retry of a task reuses its existing branch (design §3.2 step 4); a
        continuation resumes its predecessor's branch and its tip is left
        exactly where it was.
        """
        exists = await self._ref_exists(str(slot_dir), branch)
        if exists:
            await self.git._arun_unlocked(["switch", branch], cwd=str(slot_dir))
            if not resume:
                # Retry: re-point the branch at the fresh start point.  A
                # continuation deliberately keeps its accumulated commits.
                try:
                    await self.git._arun_unlocked(
                        ["reset", "--hard", start_ref], cwd=str(slot_dir)
                    )
                except GitError as e:
                    logger.warning("Could not re-base %s onto %s: %s", branch, start_ref, e)
            return
        await self.git._arun_unlocked(
            ["switch", "-c", branch, start_ref], cwd=str(slot_dir)
        )

    async def _run_setup(self, slot_dir: Path, commands: list[str] | None) -> None:
        """Run the kind's ``worktree_setup`` commands inside the slot.

        Operator-authored config (trust rule G.3): no task- or chat-derived
        text is ever interpolated, the slot is the cwd, and each command is
        bounded by ``worktrees.setup_timeout_seconds``.  A failure is logged,
        not fatal — a slot without its caches still works, just slower.
        """
        for cmd in commands or []:
            if not cmd.strip():
                continue
            try:
                argv = shlex.split(cmd, posix=os.name != "nt")
            except ValueError as e:
                logger.warning("worktree_setup command %r is not parseable: %s", cmd, e)
                continue
            if not argv:
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(slot_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (OSError, FileNotFoundError) as e:
                logger.warning("worktree_setup %r could not start in %s: %s", cmd, slot_dir, e)
                continue
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.config.setup_timeout_seconds
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                logger.warning(
                    "worktree_setup %r timed out after %ss in %s",
                    cmd,
                    self.config.setup_timeout_seconds,
                    slot_dir,
                )
                continue
            if proc.returncode != 0:
                logger.warning(
                    "worktree_setup %r failed (exit %s) in %s: %s",
                    cmd,
                    proc.returncode,
                    slot_dir,
                    stderr.decode(errors="replace").strip()[:500],
                )

    async def _emit(self, event_type: str, payload: dict) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit(event_type, payload)
        except Exception as e:
            logger.warning("Failed to emit %s: %s", event_type, e)

    # ─────────────────────────────── phase 4 ─────────────────────────────

    async def reap_slot(
        self,
        slot_ws: Workspace,
        *,
        reason: str,
    ) -> bool:
        """Remove a retired slot's worktree dir + row.  Design §5.

        Fenced by a liveness check: any process carrying an ``AQ_TASK_ID``
        that matches this slot's current lock — or whose cwd is inside the
        slot dir — blocks the reap.  If the ``/proc`` scan itself fails
        we *skip* (never reap on unknown), because reaping a live agent
        destroys its work.

        Returns True when the slot was removed, False otherwise (live,
        unknowable liveness, or removal failure).
        """
        slot_dir = Path(slot_ws.workspace_path)

        # ── Liveness check ────────────────────────────────────────────
        try:
            live = await self._slot_is_live(slot_ws)
        except Exception as e:
            logger.warning(
                "Slot %s reap skipped: liveness scan failed: %s", slot_ws.id, e
            )
            return False
        if live:
            logger.info(
                "Slot %s reap skipped: a live process is still using it", slot_ws.id
            )
            return False

        # ── Removal ───────────────────────────────────────────────────
        base_ws = await self._base_of(slot_ws)
        base_path = base_ws.workspace_path if base_ws else str(slot_dir.parent)

        async with self._git_mutex(base_path):
            try:
                await self.git.aremove_worktree(base_path, str(slot_dir))
            except Exception as e:
                logger.warning(
                    "git worktree remove %s failed (trying force): %s", slot_dir, e
                )
                try:
                    # Force removal — the slot is retired; we own the dir.
                    await self.git._arun(
                        ["worktree", "remove", "--force", str(slot_dir)],
                        cwd=base_path,
                    )
                except Exception as e2:
                    logger.warning(
                        "git worktree remove --force %s also failed: %s", slot_dir, e2
                    )
                    # Fall through to prune + row delete anyway.
            try:
                await self.git.aworktree_prune(base_path)
            except GitError as e:
                logger.debug("worktree prune failed: %s", e)

        # Best-effort dir removal if git didn't take it.
        if slot_dir.exists():
            import shutil

            try:
                shutil.rmtree(slot_dir)
            except OSError as e:
                logger.warning("rmtree %s failed: %s", slot_dir, e)

        try:
            await self.db.delete_workspace(slot_ws.id)
        except Exception as e:
            logger.warning("delete_workspace(%s) failed: %s", slot_ws.id, e)

        await self._emit(
            "worktree.reaped",
            {
                "project_id": slot_ws.project_id,
                "workspace_id": slot_ws.id,
                "slot": slot_name(slot_ws.slot_index or 0),
                "path": str(slot_dir),
                "reason": reason,
            },
        )
        logger.info("Reaped slot %s at %s (%s)", slot_ws.id, slot_dir, reason)
        return True

    async def _slot_is_live(self, slot_ws: Workspace) -> bool:
        """True when a process is still using *slot_ws*.  Raises on scan failure.

        A ``/proc`` scan raising is the "unknowable" case that the caller
        turns into "skip".  A clean empty list is not the same thing.
        """
        from src.sessions.proctable import scan_by_env_marker

        # Ids to treat as this slot's live task: the DB lock's current
        # holder, and (as a belt) the sentinel's last-assigned task id.
        # Sentinels persist across a release, so a process still running
        # after the DB lock cleared is still recognisable here.
        candidate_ids: set[str] = set()
        if slot_ws.locked_by_task_id:
            candidate_ids.add(slot_ws.locked_by_task_id)
        try:
            sentinel = self.read_sentinel(slot_ws.workspace_path)
            if sentinel and sentinel.task_id:
                candidate_ids.add(sentinel.task_id)
        except Exception:
            pass

        entries = await scan_by_env_marker("AQ_TASK_ID")
        for e in entries:
            if e.marker and e.marker in candidate_ids:
                return True
        # Any AQ-marked process whose cwd is inside the slot is also live.
        slot_dir_resolved = str(Path(slot_ws.workspace_path).resolve())
        for e in entries:
            try:
                cwd = Path(f"/proc/{e.pid}/cwd").resolve()
            except (OSError, RuntimeError):
                continue
            if str(cwd).startswith(slot_dir_resolved):
                return True
        return False

    async def prune_branches(
        self,
        base_ws: Workspace,
        *,
        default_branch: str,
    ) -> list[str]:
        """Delete stale local ``aq/*`` branches beneath *base_ws*.

        Two categories are pruned (design §6.5):

        * **Merged** local ``aq/*`` branches — always safe (spec §5).
        * **Failed & old** local ``aq/*`` branches — the branch name
          encodes the task id (``aq/<task_id>``); if that task exists and
          is in a terminal-FAILED state and the branch's last commit is
          older than ``retain_failed_days``, delete it with ``-D``
          (force, because it's unmerged).  Branches whose task can not
          be looked up, or whose task is not FAILED, are kept — the
          reaper never destroys unmerged work speculatively.

        Returns the combined list of deleted branch names.
        """
        from src.models import TaskStatus

        base_path = base_ws.workspace_path
        _validate_ref(default_branch, field="default branch")
        if not Path(base_path).is_dir():
            logger.debug("Skipping branch cleanup for missing repository %s", base_path)
            return []

        async with self._git_mutex(base_path):
            # A merged or failed branch may still belong to a worktree. Never
            # delete it or detach that worktree as part of routine cleanup.
            try:
                worktrees = await self.git.aworktree_list(base_path)
            except GitError as exc:
                logger.warning("Skipping branch cleanup; worktree inventory failed in %s: %s", base_path, exc)
                return []
            attached = {entry["branch"] for entry in worktrees if entry.get("branch")}
            try:
                merged = await self.git.alist_merged_branches(
                    base_path, into=default_branch, prefix=BRANCH_PREFIX
                )
            except GitError as e:
                logger.warning("alist_merged_branches failed in %s: %s", base_path, e)
                merged = []

            deleted: list[str] = []
            for br in merged:
                if br in attached:
                    continue
                try:
                    await self.git.adelete_local_branch(base_path, br)
                    deleted.append(br)
                except GitError as e:
                    logger.warning("delete branch %s failed: %s", br, e)

            # Failed-and-old pruning.
            retain_days = int(getattr(self.config, "retain_failed_days", 0) or 0)
            if retain_days > 0:
                cutoff = time.time() - (retain_days * 86400.0)
                # List all local aq/* branches with their last-commit unix time.
                try:
                    out = await self.git._arun(
                        [
                            "for-each-ref",
                            "--format=%(refname:short) %(committerdate:unix)",
                            f"refs/heads/{BRANCH_PREFIX}",
                        ],
                        cwd=base_path,
                    )
                except GitError as e:
                    logger.warning("for-each-ref failed in %s: %s", base_path, e)
                    out = ""
                for line in out.splitlines():
                    parts = line.strip().split()
                    if len(parts) != 2:
                        continue
                    br, ts_raw = parts
                    if br in deleted or br in attached:
                        continue
                    try:
                        ts = float(ts_raw)
                    except ValueError:
                        continue
                    if ts >= cutoff:
                        continue  # still within retention window
                    # Derive task id from branch name (aq/<task_id>).
                    if not br.startswith(BRANCH_PREFIX):
                        continue
                    task_id = br[len(BRANCH_PREFIX):]
                    if not task_id:
                        continue
                    try:
                        task = await self.db.get_task(task_id)
                    except Exception as e:
                        logger.debug("get_task(%s) failed during prune: %s", task_id, e)
                        continue
                    if task is None:
                        continue  # unknown → keep
                    status = getattr(task, "status", None)
                    status_val = getattr(status, "value", status)
                    if status_val != TaskStatus.FAILED.value:
                        continue
                    try:
                        await self.git.adelete_local_branch(base_path, br, force=True)
                        deleted.append(br)
                    except GitError as e:
                        logger.warning("force-delete branch %s failed: %s", br, e)

            if self.config.prune_remote_branches and deleted:
                for br in deleted:
                    try:
                        await self.git._arun(
                            ["push", "origin", "--delete", br], cwd=base_path
                        )
                    except GitError as e:
                        logger.debug("remote delete %s failed: %s", br, e)
        return deleted

    async def adopt_existing(self, project) -> AdoptReport:
        """Boot-time adoption.  Design §6, spec §6.4.

        Cross-checks ``git worktree list --porcelain`` in each base of the
        project against slot rows and their sentinels; repairs the exclude
        block; runs ``git worktree prune`` for stale registrations; and
        re-registers rows for intact directories.  Does **not** delete slot
        directories or branches — the whole point of adoption is that the
        directory is durable state.
        """
        report = AdoptReport()

        # Every workspace owned by this project.
        workspaces = await self.db.list_workspaces(project_id=project.id)
        bases = [ws for ws in workspaces if not ws.is_slot]
        slots = [ws for ws in workspaces if ws.is_slot]

        for base in bases:
            base_path = base.workspace_path
            if not Path(base_path).is_dir():
                continue
            # Repair exclude block idempotently.
            try:
                if self.ensure_git_exclude(base_path):
                    report.repaired.append(base.id)
            except Exception as e:
                logger.warning("ensure_git_exclude failed for %s: %s", base_path, e)

            # Prune stale git worktree registrations.  Only add to the
            # report when we actually pruned something (diff porcelain
            # list before/after) — the previous behavior appended a
            # ``"{base_id}:prune"`` sentinel every sweep, which made the
            # ``pruned`` list vacuous.
            try:
                async with self._git_mutex(base_path):
                    try:
                        before = await self.git.aworktree_list(base_path)
                    except Exception:
                        before = []
                    await self.git.aworktree_prune(base_path)
                    try:
                        after = await self.git.aworktree_list(base_path)
                    except Exception:
                        after = []
                before_paths = {e.get("path") for e in before if e.get("path")}
                after_paths = {e.get("path") for e in after if e.get("path")}
                removed = before_paths - after_paths
                for p in removed:
                    report.pruned.append(p)
            except (GitError, OSError) as e:
                logger.debug("prune in %s failed: %s", base_path, e)

            # Adopt slots that still have a matching directory + sentinel.
            for slot in slots:
                if slot.base_workspace_id != base.id:
                    continue
                slot_dir = Path(slot.workspace_path)
                if not slot_dir.is_dir():
                    continue
                sentinel = self.read_sentinel(slot_dir)
                if sentinel is None:
                    continue
                if sentinel.workspace_id and sentinel.workspace_id != slot.id:
                    logger.warning(
                        "Sentinel workspace_id %s does not match row %s at %s",
                        sentinel.workspace_id,
                        slot.id,
                        slot_dir,
                    )
                    continue
                report.adopted.append(slot.id)

        return report
