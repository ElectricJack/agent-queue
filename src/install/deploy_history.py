"""Bounded, private deployment evidence shared by the daemon and operator updater."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path

from .redaction import redact

HISTORY_FILE = "deploy-history.json"
RECOVERY_FILE = "deploy-recovery-required"
HISTORY_LIMIT = 100
MAX_BYTES = 512 * 1024


def _safe_text(value: str) -> str:
    # Diagnostics can contain credentials in the middle of a sentence, where
    # the installer's whole-value redactor would not recognize them.
    value = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]*:[^\s/@]*@", "[redacted]@", value)
    value = re.sub(r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{8,}", "[redacted]", value)
    return redact(value)[:2000]


def read_history(state_dir: Path) -> list[dict]:
    try:
        with (state_dir / HISTORY_FILE).open("rb") as handle:
            payload = handle.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            return []
        entries = json.loads(payload)
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    try:
        hold = json.loads((state_dir / RECOVERY_FILE).read_text())
        if isinstance(hold, dict) and not any(
            isinstance(row, dict) and row.get("timestamp") == hold.get("timestamp")
            for row in entries
        ):
            entries = entries[-(HISTORY_LIMIT - 1):] + [hold]
    except (OSError, ValueError):
        pass
    # Only our public evidence fields leave the installation; unknown stored
    # keys (or a damaged row) never become part of the config response.
    result = []
    for entry in entries[-HISTORY_LIMIT:]:
        if not isinstance(entry, dict) or not isinstance(entry.get("timestamp"), (int, float)):
            continue
        result.append({
            "timestamp": entry["timestamp"],
            **{key: _safe_text(str(entry.get(key, ""))) for key in (
                "outcome", "branch", "previous", "target", "message",
            )},
            "automatic": entry.get("automatic") is True,
        })
    return result


def record_deploy(
    state_dir: Path, outcome: str, *, branch: str = "", previous: str = "",
    target: str = "", message: str = "", automatic: bool = False,
) -> None:
    """Append atomically under a cross-process lock; keep the last 100 events."""
    state_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(state_dir / "deploy-history.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = read_history(state_dir)
        entry = {
            "timestamp": time.time(), "outcome": _safe_text(outcome),
            "branch": _safe_text(branch), "previous": _safe_text(previous),
            "target": _safe_text(target), "message": _safe_text(message), "automatic": automatic,
        }
        if outcome == "recovery_required":
            # Keep the hold independently of bounded history, even if later
            # diagnostics fill the history or the history write fails.
            marker = os.open(state_dir / RECOVERY_FILE, os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(marker, "w") as handle:
                json.dump(entry, handle)
                handle.flush()
                os.fsync(handle.fileno())
        entries.append(entry)
        descriptor, name = tempfile.mkstemp(prefix=".deploy-history-", dir=state_dir)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(entries[-HISTORY_LIMIT:], handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, state_dir / HISTORY_FILE)
            if outcome == "recovered":
                (state_dir / RECOVERY_FILE).unlink(missing_ok=True)
        finally:
            Path(name).unlink(missing_ok=True)


def failed_target(state_dir: Path, target: str) -> bool:
    """Do not repeatedly deploy a known failing commit after restoring the daemon."""
    for entry in reversed(read_history(state_dir)):
        if entry["target"] == target and entry["outcome"] in {
            "updated", "failed", "rolled_back", "recovery_required", "recovered",
        }:
            return entry["outcome"] not in {"updated", "recovered"}
    return False


def recovery_pending(state_dir: Path) -> bool:
    """A migration recovery hold blocks all automatic targets until operator recovery."""
    return (state_dir / RECOVERY_FILE).exists()
