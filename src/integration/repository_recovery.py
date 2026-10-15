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
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from src.git.github_contracts import GitHubAccessError
from src.git.manager import (
    GitError, RemoteRefState, _object_store_fingerprint, safe_git_detail,
)

logger = logging.getLogger(__name__)
_HEX = re.compile(r"[0-9a-f]+\Z")
_LOOSE_PATH = re.compile(r"(?:[^\s:]*objects)/([0-9a-f]{2})/([0-9a-f]{38}|[0-9a-f]{62})(?![0-9a-f])")
_FSCK = ["--no-replace-objects", "fsck", "--full", "--no-dangling", "--no-reflogs",
         "--no-progress"]
# Every write to a retained store disables auto-gc (the stores' config and the isolated
# import environment); this foreground run, with a valid cwd under the repository
# transaction, replaces it.  Command-line -c beats both.
_MAINTENANCE = ["-c", "gc.auto=6700", "-c", "gc.autoDetach=false", "-c", "maintenance.auto=false",
                "gc", "--auto", "--quiet"]
_RETAINED_CONFIG = (("core.fsync", "committed"), ("core.fsyncMethod", "fsync"),
                    ("gc.auto", "0"), ("gc.autoDetach", "false"), ("maintenance.auto", "false"))
MAX_ATTEMPTS = 2


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


async def _maintain(git, store, git_dir):
    """Best-effort foreground gc; a successful run makes any old gc.log obsolete."""
    try:
        result = await git.arun_git_result(_MAINTENANCE, cwd=str(store), lock_held=True,
                                           env={"LC_ALL": "C"})
    except GitError as exc:
        logger.warning("retained cache %s maintenance failed: %s", store, exc)
        return
    if result.returncode:
        logger.warning("retained cache %s maintenance failed: %s", store,
                       safe_git_detail(result.stderr, limit=1000))
        return
    report = git_dir / "gc.log"
    if report.is_file() and not report.is_symlink():
        report.unlink()
        logger.info("retained cache %s: removed stale gc.log after foreground gc", store)


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
                                repository_id=None, lock_held=False):
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
        return await git.arun_git_result(_FSCK, cwd=str(store), lock_held=True,
                                        env={"LC_ALL": "C"})

    async def fetch(**options):
        await git.afetch_origin(str(store), repository_url=repository_url, all_heads=True,
                                lock_held=True, **options)

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
            try:
                if pending.exists():
                    raise GitError("retained repository recovery pending")
                await fetch()
            except (GitError, GitHubAccessError) as exc:
                fetch_error = exc
            reusable = getattr(git, "retained_store_verified", None)
            if (not pending.exists() and reusable is not None
                    and reusable(str(directory))):
                if fetch_error:
                    raise fetch_error
                return
            fingerprint = _object_store_fingerprint(str(directory))
            started = time.monotonic()
            before = await fsck()
            if _clean(before) and not pending.exists():
                record = getattr(git, "record_retained_store_verified", None)
                if record is not None:
                    record(str(directory), fingerprint, started)
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
        after = await fsck()
        state["checks"][-1]["after"] = _diagnostics(after)
        if not _clean(after):
            raise GitError("retained full fsck failed after publication")
        state["status"] = "recovered"
        await save()
        if state["status"] != "recovered":
            raise GitError("retained recovery incident remains held")
        pending.unlink(missing_ok=True)
        _sync_directory(root)
        logger.warning("retained cache %s recovered: incident=%s attempts=%s backup=%s",
                       store, state["id"], state["attempts"], state["backup"])

    deadline = asyncio.get_running_loop().time() + git._command_timeout()
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
            if isinstance(exc, TimeoutError):
                raise GitError("retained Git acquisition timed out") from exc
            raise
        await _maintain(git, store, directory)


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
                                           env={"LC_ALL": "C"})
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
