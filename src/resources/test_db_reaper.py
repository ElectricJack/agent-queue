"""Conservative inventory and selection for an operator-run test DB reaper.

Only the PostgreSQL test namespaces are eligible.  The Unix
status-change time of each database's PG_VERSION file is an age floor: a
metadata update can make an old database look new, but cannot make a recently
created file look old.  Missing file metadata is never an invitation to drop.
"""

from __future__ import annotations

import re
import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Iterator
from urllib.parse import urlsplit

import yaml

TEST_NAME = re.compile(r"aq_test_[A-Za-z0-9_]+\Z")
TEMPLATE_NAME = re.compile(r"aq_tmpl_([0-9a-f]{16})\Z")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def daemon_database_name(config_path: Path) -> str:
    """Read the operator's config file directly, bypassing worker DB sentinels."""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        url = raw["database"]["url"]
        name = urlsplit(url).path.lstrip("/")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"cannot identify daemon database from {config_path}") from exc
    if not name or "/" in name:
        raise RuntimeError(f"cannot identify daemon database from {config_path}")
    return name


@dataclass(frozen=True)
class DatabaseRecord:
    name: str
    oid: int
    changed_at: datetime | None
    connections: int
    invalid: bool
    is_template: bool = False


@dataclass(frozen=True)
class ReaperDecision:
    database: DatabaseRecord
    eligible: bool
    reason: str


async def inventory(conn: Any) -> list[DatabaseRecord]:
    """Read only the test database namespaces and their activity/age evidence."""
    rows = await conn.fetch(
        """SELECT d.datname, d.oid, d.datconnlimit, d.datistemplate,
                  stamp.change AS changed_at,
                  (SELECT count(*) FROM pg_stat_activity a WHERE a.datid = d.oid)
                      AS connections
           FROM pg_database d
           LEFT JOIN LATERAL
             pg_stat_file('base/' || d.oid || '/PG_VERSION', true) stamp ON true
           WHERE left(d.datname, 8) IN ('aq_test_', 'aq_tmpl_')
           ORDER BY d.datname"""
    )
    return [
        DatabaseRecord(
            name=row["datname"],
            oid=row["oid"],
            changed_at=row["changed_at"],
            connections=row["connections"],
            invalid=row["datconnlimit"] == -2,
            is_template=row["datistemplate"],
        )
        for row in rows
    ]


def checkout_template_slugs(extra_checkouts: Iterable[Path] = ()) -> set[str]:
    """Keep the schema template of every linked checkout, including slots.

    An unreadable checkout aborts template cleanup rather than guessing that
    its schema is obsolete. Separate Git clones must be supplied by the
    operator before template cleanup is considered complete.
    """
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    paths = [
        Path(line.removeprefix("worktree "))
        for line in listing.stdout.splitlines()
        if line.startswith("worktree ")
    ]
    paths.extend(extra_checkouts)
    if not paths:
        raise RuntimeError("no linked Git worktrees found; refusing template cleanup")
    slugs: set[str] = set()
    for path in paths:
        if not (path / "alembic.ini").is_file():
            raise RuntimeError(f"checkout {path} is unavailable; refusing template cleanup")
        # Older branches predate the PostgreSQL template substrate entirely.
        if (
            not (path / "tests" / "db_fixtures.py").exists()
            and not (path / "src" / "database" / "schema_key.py").exists()
        ):
            probe = subprocess.run(
                ["git", "grep", "-q", "aq_tmpl_", "--", "tests", "src"],
                cwd=path,
                capture_output=True,
                timeout=15,
            )
            if probe.returncode != 1:
                raise RuntimeError(
                    f"cannot rule out PostgreSQL templates in {path}; refusing template cleanup"
                )
            continue
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from src.database.schema_key import schema_key_slug; print(schema_key_slug())",
            ],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        slug = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"[0-9a-f]{16}", slug):
            raise RuntimeError(f"could not read schema slug from {path}; refusing template cleanup")
        slugs.add(slug)
    return slugs


def live_pytest_pids() -> list[int]:
    """Find bare pytest controllers that do not necessarily hold an aq slot."""
    result: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [
                arg.decode(errors="replace")
                for arg in (entry / "cmdline").read_bytes().split(b"\0")
                if arg
            ]
        except OSError:
            continue  # Exited while we inspected it, or belongs to another user.
        if any(Path(arg).name in {"pytest", "py.test"} for arg in argv) or any(
            left == "-m" and right == "pytest" for left, right in zip(argv, argv[1:])
        ):
            result.append(int(entry.name))
    return sorted(result)


def observed_slot_count(lock_dir: Path, configured_slots: int) -> int:
    """Also reserve slots left by a larger AQ_TEST_SLOTS override."""
    count = max(1, configured_slots)
    for path in lock_dir.glob("slot-*.lock"):
        match = re.fullmatch(r"slot-([0-9]+)\.lock", path.name)
        if match:
            count = max(count, int(match.group(1)) + 1)
    return count


@contextmanager
def reserve_all_test_slots(lock_dir: Path, slots: int) -> Iterator[None]:
    """Block new `aq test` runs; fail immediately if any slot is occupied."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    held: list[int] = []
    try:
        for slot in range(max(1, slots)):
            fd = os.open(lock_dir / f"slot-{slot}.lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(fd)
                raise RuntimeError(f"test slot {slot} is occupied; refusing cleanup") from exc
            held.append(fd)
            os.ftruncate(fd, 0)
            os.write(
                fd,
                json.dumps(
                    {
                        "slot": slot,
                        "pid": os.getpid(),
                        "since": time.time(),
                        "command": "test-db-reaper",
                    }
                ).encode(),
            )
        yield
    finally:
        for fd in reversed(held):
            os.ftruncate(fd, 0)
            os.close(fd)


def decide(
    database: DatabaseRecord,
    *,
    now: datetime,
    minimum_age: timedelta,
    protected_names: set[str],
    active_template_slugs: set[str],
    active_run_ids: set[str],
) -> ReaperDecision:
    """Keep any database whose ownership or age cannot be proved safe."""
    name = database.name
    if name in protected_names:
        return ReaperDecision(database, False, "protected database")
    template = TEMPLATE_NAME.fullmatch(name)
    if not template and not TEST_NAME.fullmatch(name):
        return ReaperDecision(database, False, "name outside test-owned patterns")
    if database.connections:
        return ReaperDecision(database, False, "active PostgreSQL connections")
    if database.changed_at is None:
        return ReaperDecision(database, False, "age unavailable")
    if now - database.changed_at < minimum_age:
        return ReaperDecision(database, False, "younger than minimum age")
    if template:
        if template.group(1) in active_template_slugs:
            return ReaperDecision(database, False, "schema produced by a checkout")
        return ReaperDecision(database, True, "unused schema template")
    if any(name.startswith(f"aq_test_{run_id}_") for run_id in active_run_ids):
        return ReaperDecision(database, False, "live test run token")
    return ReaperDecision(
        database,
        True,
        "orphaned invalid test database" if database.invalid else "orphaned test database",
    )


async def drop_if_still_eligible(
    conn: Any,
    previous: DatabaseRecord,
    *,
    now: datetime,
    minimum_age: timedelta,
    protected_names: set[str],
    active_template_slugs: set[str],
    active_run_ids: set[str],
) -> str:
    """Recheck a planned drop and use plain DROP, never WITH FORCE.

    The caller must hold every aq test slot and verify that no bare pytest
    controller is running. The OID check catches a target replaced since the
    dry run; DROP itself refuses if a connection races with this check.
    """
    row = await conn.fetchrow(
        """SELECT d.datname, d.oid, d.datconnlimit, d.datistemplate,
                  stamp.change AS changed_at,
                  (SELECT count(*) FROM pg_stat_activity a WHERE a.datid = d.oid)
                      AS connections
           FROM pg_database d
           LEFT JOIN LATERAL
             pg_stat_file('base/' || d.oid || '/PG_VERSION', true) stamp ON true
           WHERE d.datname = $1""",
        previous.name,
    )
    if row is None:
        return "already absent"
    current = DatabaseRecord(
        name=row["datname"],
        oid=row["oid"],
        changed_at=row["changed_at"],
        connections=row["connections"],
        invalid=row["datconnlimit"] == -2,
        is_template=row["datistemplate"],
    )
    if current.oid != previous.oid:
        return "database identity changed"
    decision = decide(
        current,
        now=now,
        minimum_age=minimum_age,
        protected_names=protected_names,
        active_template_slugs=active_template_slugs,
        active_run_ids=active_run_ids,
    )
    if not decision.eligible:
        return decision.reason
    template = TEMPLATE_NAME.fullmatch(current.name) is not None
    if template and current.is_template:
        await conn.execute(
            "UPDATE pg_database SET datistemplate = false WHERE oid = $1 AND datname = $2",
            current.oid,
            current.name,
        )
    try:
        await conn.execute(f'DROP DATABASE "{current.name}"')
    except Exception:
        if template and current.is_template:
            await conn.execute(
                "UPDATE pg_database SET datistemplate = true WHERE oid = $1 AND datname = $2",
                current.oid,
                current.name,
            )
        raise
    return "dropped"
