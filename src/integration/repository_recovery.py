"""Bounded, nondeleting recovery of the existing retained integration caches."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.git.github_contracts import GitHubAccessError
from src.git.manager import (
    RETAINED_FSCK_TIMEOUT_SECONDS, GitError, RemoteRefState, safe_git_detail,
)
from src.git.repository_lock import RepositoryLockTimeout
from src.integration.selection_metrics import selection_count, selection_stage

logger = logging.getLogger(__name__)
_HEX = re.compile(r"[0-9a-f]+\Z")
_LOOSE_PATH = re.compile(r"(?:[^\s:]*objects)/([0-9a-f]{2})/([0-9a-f]{38}|[0-9a-f]{62})(?![0-9a-f])")
_FSCK = ["--no-replace-objects", "fsck", "--full", "--no-dangling", "--no-reflogs",
         "--no-progress"]
_RETAINED_CONFIG = (("core.fsync", "committed"), ("core.fsyncMethod", "fsync"),
                    ("gc.auto", "0"), ("gc.autoDetach", "false"), ("maintenance.auto", "false"))
MAX_ATTEMPTS = 2
# Recovery needs up to four full-body passes, using the same budget as the
# pre-fetch lender check rather than the ordinary short-command timeout.
FSCK_TIMEOUT_SECONDS = RETAINED_FSCK_TIMEOUT_SECONDS


@dataclass
class _Checkout:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    path: Path | None = None


_CHECKOUT_SCOPE: ContextVar[dict | None] = ContextVar("retained_checkout_scope", default=None)


@contextmanager
def retained_checkout_scope():
    token = _CHECKOUT_SCOPE.set({})
    try:
        yield
    finally:
        _CHECKOUT_SCOPE.reset(token)


async def open_retained_checkout(git, store, origin_url):
    """Reuse one successful checkout open per tick, without caching Git authority."""
    scope = _CHECKOUT_SCOPE.get()
    if scope is None:
        return await _open_retained_checkout(git, store, origin_url)
    key = id(git), str(Path(store).absolute()), origin_url
    checkout = scope.setdefault(key, _Checkout())
    async with checkout.lock:
        if checkout.path is None:
            checkout.path = await _open_retained_checkout(git, store, origin_url)
        return checkout.path


async def _open_retained_checkout(git, store, origin_url):
    """Serialize creation when the train and its truth reader share a store."""
    store = Path(store)
    store.parent.mkdir(parents=True, exist_ok=True)
    async with git.arepository_transaction(str(store)):
        if not (store / ".git").exists():
            await git.acreate_checkout(origin_url, str(store), no_checkout=True)
        await ensure_retained_origin(git, store, origin_url, lock_held=True)
        await harden_retained_repository(git, store, lock_held=True)
    return store


async def ensure_retained_origin(git, store, origin_url, *, lock_held=False):
    """Repair an absent origin from configuration without rebinding another remote."""
    if not origin_url:
        raise GitError("retained repository origin is not configured")
    async with _transaction(git, store, lock_held):
        configured = await git.arun_git_result(
            ["config", "--get", "remote.origin.url"], cwd=str(store), lock_held=True,
        )
        if configured.returncode not in {0, 1}:
            raise GitError("retained repository origin cannot be read")
        current = configured.stdout.strip()
        if current:
            if current != origin_url:
                raise GitError("retained repository origin differs from configured repository")
            return
        result = await git.arun_git_result(
            ["config", "--local", "remote.origin.url", origin_url],
            cwd=str(store), lock_held=True,
        )
        if result.returncode:
            raise GitError("retained repository origin could not be restored")


def _git_dir(store: Path) -> Path:
    if store.is_symlink():
        raise GitError("retained repository must be an isolated clone")
    directory = store / ".git" if (store / ".git").exists() else store
    if directory.is_symlink() or not directory.is_dir() or not (directory / "objects").is_dir():
        raise GitError("retained repository must be an isolated clone")
    if (directory / "objects").is_symlink():
        raise GitError("retained object directory is not isolated")
    return directory


async def harden_retained_repository(git, store, *, lock_held=False):
    store = Path(store).resolve()
    git_dir = _git_dir(store)
    identity = (str(store), git_dir.stat().st_ino)
    if identity in git._retained_fsync_stores:
        return
    async with _transaction(git, store, lock_held):
        if identity in git._retained_fsync_stores:
            return
        for key, value in _RETAINED_CONFIG:
            await git._arun_unlocked(["config", key, value], cwd=str(store))
        git._retained_fsync_stores.add(identity)


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, value):
    """Atomic journal replacement; move intents reach disk before object renames."""
    if path.name == "incident.json":
        backup = Path(value["backup"])
        if backup.parent != path.parent or backup.is_symlink():
            raise GitError("retained backup path is not isolated")
        backup.mkdir(exist_ok=True)
        _write(backup / "manifest.json", value)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    _sync_directory(path.parent)


def _diagnostics(result):
    return {"exit_status": result.returncode,
            "stdout": safe_git_detail(result.stdout, limit=16000),
            "stderr": safe_git_detail(result.stderr, limit=16000)}


def _clean(result):
    lines = (result.stdout + "\n" + result.stderr).splitlines()
    unexpected = [line for line in lines if line.strip() and
                  line != "notice: No default references" and not re.fullmatch(
                      r"notice: HEAD points to an unborn branch \([^\r\n]+\)", line)]
    return result.returncode == 0 and not unexpected


def _loose_objects(git_dir, result):
    """Git diagnoses bodies; this code validates only names, paths and file types."""
    objects = git_dir / "objects"
    diagnosed = set()
    diagnostics = result.stdout + "\n" + result.stderr
    if re.search(r"(?i)(?:objects/pack/|pack (?:checksum|file)|packfile|index CRC)", diagnostics):
        raise GitError("pack integrity requires supervisor intervention")
    for line in diagnostics.splitlines():
        if not re.search(r"(?i)(?:error|fatal|corrupt|hash-path mismatch|unable to unpack)", line):
            continue
        for match in _LOOSE_PATH.finditer(line):
            path = Path(match[0])
            candidate = path if path.is_absolute() else (git_dir.parent / path)
            # Bare fsck may name objects/ rather than .git/objects/.
            if not path.is_absolute() and str(path).startswith("objects/"):
                candidate = git_dir / path
            if candidate.absolute() != (objects / match[1] / match[2]).absolute():
                raise GitError("diagnosed object path is outside retained objects")
            diagnosed.add(match[1] + match[2])
    found = {}
    for fanout in objects.iterdir():
        if len(fanout.name) != 2 or not _HEX.fullmatch(fanout.name):
            continue
        if fanout.is_symlink():
            raise GitError("loose object fanout is not a regular directory")
        if not fanout.is_dir():
            continue
        for obj in fanout.iterdir():
            if len(obj.name) not in {38, 62} or not _HEX.fullmatch(obj.name):
                continue
            info = obj.lstat()
            oid = fanout.name + obj.name
            if not stat.S_ISREG(info.st_mode):
                if oid in diagnosed:
                    raise GitError("diagnosed loose object is not a regular file")
                continue
            if info.st_size == 0 or oid in diagnosed:
                found[oid] = (obj, info)
    # Missing objects are refetched, but an unidentified corruption is never moved.
    if not found and not re.search(r"(?im)^(?:missing |broken link|error: .*invalid sha1 pointer)",
                                   diagnostics):
        raise GitError("integrity failure has no safely identified loose object")
    return found


def _move_objects(git_dir, journal, state, result):
    found = _loose_objects(git_dir, result)
    destination = Path(state["backup"])
    destination.mkdir(exist_ok=True)
    _sync_directory(destination.parent)
    for oid, (obj, info) in sorted(found.items()):
        # Revalidate immediately before the move while the repository transaction is held.
        current = obj.lstat()
        if (current.st_ino, current.st_dev, current.st_size, current.st_mtime_ns) != (
            info.st_ino, info.st_dev, info.st_size, info.st_mtime_ns
        ) or not stat.S_ISREG(current.st_mode) or obj.parent.is_symlink():
            raise GitError("diagnosed loose object changed before backup")
        backup = destination / f"{state['attempts']}-{oid}"
        move = {"oid": oid, "original": str(obj), "backup": str(backup),
                "attempt": state["attempts"], "status": "planned"}
        state["moves"].append(move)
        _write(journal, state)
        if backup.exists():
            raise GitError("object backup already exists")
        obj.rename(backup)
        _sync_directory(obj.parent)
        _sync_directory(destination)
        move["status"] = "moved"
        _write(journal, state)


def _reconcile_moves(state):
    for move in state["moves"]:
        if move["status"] == "planned" and Path(move["backup"]).is_file():
            move["status"] = "moved"


async def _publish_local_heads(git, store, repository_url):
    result = await git.arun_git_result(
        ["--no-replace-objects", "for-each-ref", "--format=%(refname) %(objectname)",
         "refs/heads/aq/"], cwd=str(store), lock_held=True, env={"LC_ALL": "C"},
    )
    if result.returncode or re.search(r"(?m)^(?:error|fatal):", result.stderr):
        raise GitError("local AQ branches cannot be read")
    tips = {ref.removeprefix("refs/heads/"): oid for ref, oid in
            (line.split() for line in result.stdout.splitlines())}
    if not tips:
        return
    remote = await git.als_remote_refs(str(store), list(tips), repository_url=repository_url)
    missing = {}
    for branch, oid in tips.items():
        observed = remote[branch]
        if observed.state is RemoteRefState.ERROR:
            raise GitError("local AQ branch remote observation failed")
        if observed.state is RemoteRefState.ABSENT:
            # Read every reachable body before publication, even with missing local history.
            readable = await git.arun_git_result(
                ["--no-replace-objects", "rev-list", "--objects", "--missing=error", oid],
                cwd=str(store), lock_held=True, env={"LC_ALL": "C"},
            )
            if readable.returncode:
                raise GitError("local-only AQ branch history is unreadable")
            missing[branch] = oid
    published = await git.apush_new_refs(str(store), missing, repository_url=repository_url,
                                         lock_held=True)
    if any(published[name].state is not RemoteRefState.PRESENT or
           published[name].oid != oid or published[name].publication_error
           for name, oid in missing.items()):
        raise GitError("guarded local-only AQ branch publication failed")


@asynccontextmanager
async def _transaction(git, store, lock_held):
    if lock_held:
        yield
    else:
        async with git.arepository_transaction(str(store)):
            yield


class RetainedRecoveryExhausted(GitError):
    """The incident's attempt budget is spent; only a supervisor clear reopens admission."""


async def fetch_retained_origin(git, store, *, repository_url, project_id=None,
                                repository_id=None, lock_held=False, skip_unchanged=False):
    """Recover inside the existing acquisition budget, keeping all local refs.

    The journal is durable even without a daemon (disposable Git fixtures). In
    production the CommandHandler port mirrors incident state in PostgreSQL,
    gates project batch admission and atomically coalesces supervisor notices.
    A fetch failure on a store whose full fsck is clean is the caller's ordinary
    failure: it never opens an incident, and closes one that moved no object.
    """
    store = Path(store).absolute()
    directory = _git_dir(store)
    root = directory / "aq-empty-object-backups"
    if root.is_symlink():
        raise GitError("retained backup directory is not isolated")
    journal, pending = root / "incident.json", root / "pending"
    callback = getattr(git, "retained_recovery_state", None)
    state = None

    async def save(*, load=False):
        _write(journal, state)
        if callback is not None:
            durable = await callback(state, load=load)
            if durable is not None:
                state.update(durable)
                _write(journal, state)

    async def fsck():
        with selection_stage("retained_fsck"):
            return await git.arun_git_result(_FSCK, cwd=str(store), lock_held=True,
                                            env={"LC_ALL": "C"}, timeout=FSCK_TIMEOUT_SECONDS)

    async def fetch(**options):
        selection_count("retained_fetch_starts")
        with selection_stage("retained_fetch"):
            await git.afetch_origin(str(store), repository_url=repository_url, all_heads=True,
                                    lock_held=True, **options)

    async def incremental():
        # This runs under the same repository exclusion as acquisition. A fresh
        # advertisement alone says nothing about local integrity or missing refs.
        with selection_stage("snapshot_ref_probe"):
            selection_count("snapshot_ref_probes")
            remote = await git.als_remote_snapshot_refs(str(store), repository_url=repository_url)
            local = await git.arun_git_result(
                ["for-each-ref", "--format=%(refname) %(objectname)",
                 "refs/remotes/origin/", "refs/tags/", "refs/aq/provenance/"],
                cwd=str(store), lock_held=True,
            )
            if local.returncode or local.stderr.strip():
                raise GitError("retained ref inventory is unavailable")
            refs = dict(line.split(" ", 1) for line in local.stdout.splitlines())
            refs.pop("refs/remotes/origin/HEAD", None)
            refs = {("refs/heads/" + ref.removeprefix("refs/remotes/origin/")
                     if ref.startswith("refs/remotes/origin/") else ref): oid
                    for ref, oid in refs.items()}
            changed = sorted(ref for ref, oid in remote.items() if refs.get(ref) != oid)
            removed = {ref: oid for ref, oid in refs.items()
                       if ref not in remote and not ref.startswith("refs/tags/")}
        selection_count("snapshot_changed_refs", len(changed))
        selection_count("snapshot_removed_refs", len(removed))
        if changed:
            # Both authenticated transport and the credential-free local import
            # carry only these explicit refs. Keep the complete captured inventory
            # for lanes selecting different targets from this tick's observation.
            await fetch(refs=changed)
        if removed:
            commands = ["start"]
            for ref, oid in sorted(removed.items()):
                destination = ("refs/remotes/origin/" + ref.removeprefix("refs/heads/")
                               if ref.startswith("refs/heads/") else ref)
                commands.append(f"delete {destination} {oid}")
            commands.extend(["prepare", "commit", ""])
            result = await git.arun_git_result(
                ["update-ref", "--stdin"], cwd=str(store), lock_held=True,
                stdin="\n".join(commands),
            )
            if result.returncode:
                raise GitError("retained ref inventory changed during pruning")
        if not changed:
            selection_count("snapshot_unchanged")
        return not changed

    async def acquire():
        nonlocal state
        before = None
        if journal.exists():
            state = json.loads(journal.read_text())
            if (state["cache"], state["project_id"], state["repository_id"]) != (
                str(store), project_id, repository_id
            ):
                raise GitError("retained recovery identity changed")
            if state["status"] == "recovered":
                await save(load=True)
                if state["status"] == "recovered":
                    pending.unlink(missing_ok=True)
                    state = None
        if state is None:
            fetch_error = None
            # Check the baseline before fetch adds verified objects. Comparing
            # only afterwards makes every incremental pack require a full fsck.
            reusable = await git.aretained_store_verified(str(directory))
            try:
                if pending.exists():
                    raise GitError("retained repository recovery pending")
                if skip_unchanged and reusable:
                    no_transfer = await incremental()
                else:
                    no_transfer = False
                    await fetch()
            except (GitError, GitHubAccessError) as exc:
                # A caller timeout leaves the shared check running. Starting
                # recovery diagnosis now would duplicate that full-body scan.
                if await git.aretained_store_check_running(str(directory)):
                    raise
                fetch_error = exc
            if not pending.exists() and await git.areuse_retained_verification(
                    str(directory), extend=reusable and fetch_error is None):
                if fetch_error:
                    raise fetch_error
                return no_transfer
            baseline = await git.aretained_store_baseline(str(directory))
            before = await fsck()
            if _clean(before) and not pending.exists():
                await git.arecord_retained_store_verified(str(directory), *baseline)
                if fetch_error:
                    raise fetch_error
                return
            root.mkdir(exist_ok=True)
            incident = uuid.uuid4().hex
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            state = {"id": incident, "project_id": project_id,
                     "repository_id": repository_id, "cache": str(store),
                     "backup": str(root / f"{timestamp}-{incident}"),
                     "attempts": 0, "status": "pending", "moves": [],
                     "checks": [], "created_at": time.time()}
            # Load any unresolved PostgreSQL incident rather than refreshing its budget.
            await save(load=True)
        else:
            _reconcile_moves(state)
            await save(load=True)
        if state["attempts"] >= MAX_ATTEMPTS:
            state["status"] = "exhausted"
            await save()
            raise RetainedRecoveryExhausted(
                "retained recovery exhausted its two attempts; last error: "
                + (state.get("error") or "unrecorded"))
        if before is None:
            before = await fsck()
        if _clean(before) and not state["moves"]:
            # Every object verifies and nothing was quarantined, so there is
            # nothing to refetch or publish: close without spending an attempt.
            state["status"] = "recovered"
            state["checks"].append({"attempt": state["attempts"], "cleared": "clean_fsck",
                                    "before": _diagnostics(before)})
            await save()
            if state["status"] == "recovered":
                pending.unlink(missing_ok=True)
                _sync_directory(root)
                logger.warning("retained cache %s incident %s closed: full fsck clean and "
                               "no object moved", store, state["id"])
                state = None
                await fetch()
                return
        state["attempts"] += 1
        state["status"] = "pending"
        state["checks"].append({"attempt": state["attempts"]})
        pending.touch()
        _sync_directory(root)
        await save()
        before = await fsck()
        state["checks"][-1]["before"] = _diagnostics(before)
        await save()
        if not _clean(before):
            _move_objects(directory, journal, state, before)
            await save()
        await fetch(refetch=True)
        after = await fsck()
        state["checks"][-1]["after"] = _diagnostics(after)
        await save()
        if not _clean(after):
            raise GitError("retained full fsck failed after refetch")
        await _publish_local_heads(git, store, repository_url)
        baseline = await git.aretained_store_baseline(str(directory))
        after = await fsck()
        state["checks"][-1]["after"] = _diagnostics(after)
        if not _clean(after):
            raise GitError("retained full fsck failed after publication")
        await git.arecord_retained_store_verified(str(directory), *baseline)
        state["status"] = "recovered"
        await save()
        if state["status"] != "recovered":
            raise GitError("retained recovery incident remains held")
        pending.unlink(missing_ok=True)
        _sync_directory(root)
        logger.warning("retained cache %s recovered: incident=%s attempts=%s backup=%s",
                       store, state["id"], state["attempts"], state["backup"])

    deadline = (asyncio.get_running_loop().time() + git._command_timeout()
                + 4 * FSCK_TIMEOUT_SECONDS)
    async with _transaction(git, store, lock_held):
        try:
            async with asyncio.timeout_at(deadline):
                await acquire()
        except BaseException as exc:
            if state is not None and state["status"] != "recovered":
                state["status"] = "exhausted" if state["attempts"] >= MAX_ATTEMPTS else "failed"
                if not isinstance(exc, RetainedRecoveryExhausted):
                    # Keep each attempt's own cause; exhaustion never hides the last one.
                    detail = safe_git_detail(str(exc) or type(exc).__name__, limit=2000)
                    state["error"] = detail
                    if state["checks"] and state["checks"][-1]["attempt"] == state["attempts"]:
                        state["checks"][-1].setdefault("error", detail)
                # Finish accounting before cancellation releases the repository transaction.
                accounting = asyncio.create_task(save())
                try:
                    await asyncio.shield(accounting)
                except asyncio.CancelledError:
                    await accounting
            if isinstance(exc, TimeoutError) and not isinstance(exc, RepositoryLockTimeout):
                raise GitError("retained Git acquisition timed out") from exc
            raise


async def clear_retained_recovery(git, state, *, by, record):
    """Close one held incident once the store passes a full fsck (supervisor control).

    The journal is marked before ``record`` clears the PostgreSQL ledger: a
    cleared ledger beside an open journal would reopen the hold on the next
    fetch, while the reverse only asks for the clear to be retried.
    """
    store = Path(state["cache"])
    directory = _git_dir(store)
    root = directory / "aq-empty-object-backups"
    if root.is_symlink():
        raise GitError("retained backup directory is not isolated")
    journal, pending = root / "incident.json", root / "pending"
    async with _transaction(git, store, False):
        result = await git.arun_git_result(_FSCK, cwd=str(store), lock_held=True,
                                           env={"LC_ALL": "C"}, timeout=FSCK_TIMEOUT_SECONDS)
        check = _diagnostics(result)
        if not _clean(result):
            return {"cleared": False, "fsck": check, "state": state}
        cleared = {"by": by, "at": time.time(), "fsck": check}
        if journal.exists():
            local = json.loads(journal.read_text())
            if local["status"] != "recovered":
                local.update(status="recovered", cleared=cleared)
                _write(journal, local)
        if root.is_dir():
            pending.unlink(missing_ok=True)
            _sync_directory(root)
        durable = await record(cleared)
        return {"cleared": durable is not None, "fsck": check, "state": durable or state}
