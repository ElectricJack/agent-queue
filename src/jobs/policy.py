"""Immutable admission contracts and deterministic queue order."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "lost"})
TRANSITIONS = {
    "queued": {"starting", "cancelled", "failed"},
    "starting": {"running", "cancelling", "failed", "lost"},
    "running": TERMINAL | {"cancelling"},
    "cancelling": {"cancelled", "failed", "lost"},
}


class JobError(ValueError):
    """A stable jobs.* failure code suitable for command responses."""


@dataclass(frozen=True)
class Preset:
    name: str
    argv: tuple[str, ...]
    job_class: str = "shared"
    weight: int = 1
    version: int = 1
    pytest: bool = False


NODE_PRESETS = frozenset({
    "build", "npm_ci", "npm_test", "pnpm_install", "pnpm_check", "pnpm_build"
})


def node_executable(name: str) -> str | None:
    """Resolve the daemon user's Node tools without trusting a submitted path."""
    search = os.pathsep.join((
        os.environ.get("PATH", ""),
        str(Path.home() / ".local" / "share" / "pnpm"),
        str(Path.home() / ".local" / "bin"),
    ))
    found = shutil.which(name, path=search)
    return str(Path(found).absolute()) if found else None


def presets(root: Path, *, test_python: str | None = None) -> dict[str, Preset]:
    """Executable paths are supplied by the server, never by a submitter."""
    python = str(Path(sys.executable).absolute())
    available = {
        "test": Preset("test", (test_python or python, "-m", "pytest"), pytest=True),
        "lint": Preset("lint", (python, "-m", "ruff", "check")),
        "e2e": Preset("e2e", (python, str(root / "src/jobs/e2e.py")), "exclusive"),
    }
    if node_executable("node") is None:
        return available
    npm, pnpm = node_executable("npm"), node_executable("pnpm")
    if npm:
        available.update({
            "build": Preset("build", (npm, "run", "build"), weight=2),
            "npm_ci": Preset("npm_ci", (npm, "ci"), weight=2),
            "npm_test": Preset("npm_test", (npm, "test")),
        })
    if pnpm:
        available.update({
            "pnpm_install": Preset("pnpm_install", (pnpm, "install", "--frozen-lockfile"), weight=2),
            "pnpm_check": Preset("pnpm_check", (pnpm, "check")),
            "pnpm_build": Preset("pnpm_build", (pnpm, "run", "build"), weight=2),
        })
    return available


def request_hash(request: dict) -> str:
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def queue_key(row: dict, now: float) -> tuple:
    band = max(0, row["priority_band"] - int(max(0, now - row["submitted_at"]) // 600))
    return band, row["submitted_at"], row["id"]


def validate_args(
    preset: Preset, args: list[str], root: Path, worker_cap: int, *, xdist: bool = True
) -> list[str]:
    if not isinstance(args, list) or any(not isinstance(a, str) or "\0" in a for a in args):
        raise JobError("jobs.preset_denied")
    if len(args) > 256 or sum(len(a) for a in args) > 32768:
        raise JobError("jobs.preset_denied")
    # Pytest plugins/config and ruff config can load arbitrary code. This is
    # resource separation, not a sandbox; still refuse resource-cap overrides
    # and explicit paths outside the pinned workspace.
    for i, arg in enumerate(args):
        if arg in {"--quiet-box", "--quiet"} and preset.name != "lint":
            raise JobError("jobs.quiet_unsupported")
        if arg.startswith("--aq-") or arg.startswith("--numprocesses") or arg.startswith("-n"):
            raise JobError("jobs.preset_denied")
        value = arg.split("=", 1)[-1] if "=" in arg else arg
        if value.startswith("/") or ".." in Path(value.split("::", 1)[0]).parts:
            raise JobError("jobs.cwd_invalid")
        if preset.name in NODE_PRESETS | {"e2e"} and args:
            raise JobError("jobs.preset_denied")
    if preset.pytest and xdist and not any(
        arg == "-pno:xdist" or (arg == "-p" and next_arg == "no:xdist")
        for arg, next_arg in zip(args, [*args[1:], ""])
    ):
        args = [*args, "-n", str(worker_cap)]
    return [*preset.argv, *args]


def next_admission(rows: list[dict], now: float, capacity: int, per_owner_active: int):
    """One ordered launch per tick; an exclusive frontier drains shared work."""
    active = [r for r in rows if r["state"] not in TERMINAL and r["state"] != "queued"]
    if any(r["job_class"] == "exclusive" for r in active):
        return None
    used = sum(r["weight"] for r in active)
    counts = {}
    for row in active:
        counts[row["owner_id"]] = counts.get(row["owner_id"], 0) + 1
    for row in sorted((r for r in rows if r["state"] == "queued"), key=lambda r: queue_key(r, now)):
        if row["job_class"] == "exclusive":
            return row if not active else None
        if counts.get(row["owner_id"], 0) >= per_owner_active:
            continue
        if used + row["weight"] <= capacity:
            return row
        # Preserve FIFO at the weighted frontier rather than letting a
        # continuous feed of light jobs starve this waiting request.
        return None
    return None
