"""Create quiescent disposable Git seeds for per-test filesystem copies."""

import subprocess
from pathlib import Path


_SEED_CONFIG = (
    "maintenance.auto=false",
    "gc.auto=0",
    "receive.autogc=false",
)


def init_bare_seed(path: str | Path) -> None:
    """Configure the receive side before any push can start background work."""
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(path)],
        check=True,
        capture_output=True,
    )
    for setting in _SEED_CONFIG:
        key, value = setting.split("=", 1)
        subprocess.run(
            ["git", "-C", str(path), "config", "--local", key, value],
            check=True,
            capture_output=True,
        )


def clone_seed(remote: str | Path, path: str | Path) -> None:
    """Persist clone settings before the initial fetch and subsequent writes."""
    options = [arg for setting in _SEED_CONFIG for arg in ("--config", setting)]
    subprocess.run(
        ["git", "clone", *options, str(remote), str(path)],
        check=True,
        capture_output=True,
    )
