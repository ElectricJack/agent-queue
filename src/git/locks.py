"""Conservative recovery of abandoned Git locks on the Linux/WSL runtime."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def common_git_dir(git_dir: Path) -> Path:
    """Resolve the common object store, including a worktree's relative pointer."""
    pointer = git_dir / "commondir"
    try:
        target = Path(pointer.read_text().strip())
    except FileNotFoundError:
        return git_dir.resolve()
    return (target if target.is_absolute() else git_dir / target).resolve()


def _common_from_cwd(cwd: Path) -> Path | None:
    for directory in (cwd, *cwd.parents):
        dot_git = directory / ".git"
        if dot_git.is_dir():
            return common_git_dir(dot_git)
        if dot_git.is_file():
            text = dot_git.read_text().strip()
            if not text.startswith("gitdir:"):
                return None
            target = Path(text.split(":", 1)[1].strip())
            return common_git_dir(target if target.is_absolute() else directory / target)
        if (directory / "objects").is_dir() and (directory / "refs").is_dir():
            return directory.resolve()  # bare repository or a Git directory
    return None


def _process_repository(process: Path) -> Path | None:
    """Resolve Git's global directory flags and environment without spawning Git."""
    cwd = (process / "cwd").resolve(strict=True)
    args = [os.fsdecode(arg) for arg in (process / "cmdline").read_bytes().split(b"\0") if arg]
    env = dict(
        os.fsdecode(item).split("=", 1)
        for item in (process / "environ").read_bytes().split(b"\0") if b"=" in item
    )
    git_dir = env.get("GIT_DIR")
    index = 1
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-"):
            break
        if arg in ("-C", "--git-dir", "--work-tree", "-c", "--config-env"):
            index += 1
            if index >= len(args):
                return None
            value = args[index]
        elif arg.startswith("-C"):
            arg, value = "-C", arg[2:]
        elif arg.startswith(("--git-dir=", "--work-tree=")):
            arg, value = arg.split("=", 1)
        else:
            if arg == "--bare":
                git_dir = str(cwd)
            index += 1
            continue
        if arg == "-C" and value:
            cwd = (cwd / value).resolve(strict=True)
        elif arg == "--git-dir":
            git_dir = value
        index += 1
    if env.get("GIT_COMMON_DIR"):
        common = (cwd / env["GIT_COMMON_DIR"]).resolve(strict=True)
        return common if common.is_dir() else None
    if git_dir:
        directory = (cwd / git_dir).resolve(strict=True)
        return common_git_dir(directory) if directory.is_dir() else None
    # --work-tree/GIT_WORK_TREE change the files Git writes, not the object
    # store discovered from cwd. They must not hide a live owner in that store.
    return _common_from_cwd(cwd)


def _live_lock_owner(
    common: Path, identities: set[tuple[int, int]], *, proc_root: Path = Path("/proc"),
) -> bool:
    """Unknown ownership counts as live. Check FDs as well as Git processes.

    Git may close a lock's FD while retaining ownership of the pathname (notably
    maintenance); a Git process in this shared store therefore vetoes cleanup
    even without an open FD. Unresolvable repository overrides count as unknown.
    """
    try:
        processes = list(proc_root.iterdir())
        if not any(process.name.isdigit() for process in processes):
            return True
        for process in processes:
            if not process.name.isdigit():
                continue
            try:
                name = (process / "comm").read_text().strip()
                state = (process / "stat").read_text().rsplit(")", 1)[1].split()[0]
                if state == "Z":
                    continue
                if name == "git" or name.startswith("git-"):
                    repository = _process_repository(process)
                    if repository is None or repository == common:
                        logger.debug("Git process %s may own repository locks", process.name)
                        return True
                # Non-Git tools can own an open lock too. Only inspect FDs of
                # our uid; foreign Git processes above still veto on uncertainty.
                if process.stat().st_uid == os.getuid():
                    try:
                        for fd in (process / "fd").iterdir():
                            try:
                                info = fd.stat()
                            except FileNotFoundError:
                                continue
                            if (info.st_dev, info.st_ino) in identities:
                                return True
                    except PermissionError:
                        # Yama/non-dumpable tools can hide unrelated FDs. Git
                        # owners were checked above independently of their FDs;
                        # unrelated tools need not be dumpable for recovery.
                        continue
            except (FileNotFoundError, ProcessLookupError):
                if process.exists():
                    try:
                        state = (process / "stat").read_text().rsplit(")", 1)[1].split()[0]
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    if state != "Z":
                        return True  # live process with incomplete inspection
                continue
        return False
    except (OSError, ValueError, IndexError) as exc:
        logger.debug("Git lock ownership inspection was inconclusive: %s", exc)
        return True


def remove_stale_locks(git_dir: Path) -> None:
    """Unlink only unchanged, regular lock files after proving no live owner."""
    try:
        common = common_git_dir(git_dir)
        paths = {
            git_dir / "index.lock", git_dir / "shallow.lock", git_dir / "refs/heads.lock",
            common / "index.lock", common / "shallow.lock", common / "refs/heads.lock",
            common / "objects/maintenance.lock",
        }
        snapshots = {}
        for path in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                snapshots[path] = info
        if not snapshots or _live_lock_owner(
            common, {(info.st_dev, info.st_ino) for info in snapshots.values()},
        ):
            return
        for path, before in snapshots.items():
            try:
                after = path.lstat()
                if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) == (
                    after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns,
                ):
                    path.unlink()
                    logger.info("Removed abandoned Git lock %s", path)
            except FileNotFoundError:
                continue
    except OSError:
        logger.debug("Could not prove Git locks safe to remove in %s", git_dir, exc_info=True)
