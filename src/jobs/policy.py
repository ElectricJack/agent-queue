"""Immutable admission contracts and deterministic queue order."""

from __future__ import annotations

import hashlib
import json
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


def presets(root: Path) -> dict[str, Preset]:
    """Executable paths are supplied by the server, never by a submitter."""
    python = str(Path(sys.executable).absolute())
    return {
        "test": Preset("test", (python, "-m", "pytest"), pytest=True),
        "lint": Preset("lint", (python, "-m", "ruff", "check")),
        "build": Preset("build", ("/usr/bin/npm", "run", "build"), weight=2),
        "e2e": Preset("e2e", (python, str(root / "src/jobs/e2e.py")), "exclusive"),
    }


def request_hash(request: dict) -> str:
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def queue_key(row: dict, now: float) -> tuple:
    band = max(0, row["priority_band"] - int(max(0, now - row["submitted_at"]) // 600))
    return band, row["submitted_at"], row["id"]


def validate_args(preset: Preset, args: list[str], root: Path, worker_cap: int) -> list[str]:
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
        if preset.name in {"build", "e2e"} and args:
            raise JobError("jobs.preset_denied")
    if preset.pytest:
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
