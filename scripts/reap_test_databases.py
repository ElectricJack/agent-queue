"""Inventory orphaned PostgreSQL test databases; remove them only with --apply.

From the repository root::

    export POSTGRES_TEST_DSN=postgresql+asyncpg://.../postgres
    python -m scripts.reap_test_databases
    python -m scripts.reap_test_databases --apply

The operator must include any independent clones with ``--checkout PATH``
before trusting the unused-template decisions. Linked Git worktrees are
discovered automatically. No daemon database migration is involved.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

from src.cli.test_runner import _caps
from src.config import load_config
from src.resources.semaphore import SlotSemaphore, default_lock_dir
from src.resources.test_db_reaper import (
    TEMPLATE_NAME,
    checkout_template_slugs,
    daemon_database_name,
    decide,
    drop_if_still_eligible,
    inventory,
    live_pytest_pids,
    observed_slot_count,
    reserve_all_test_slots,
)


def _maintenance_dsn(protected_name: str) -> str:
    raw = os.environ.get("POSTGRES_TEST_DSN", "").strip()
    parts = urlsplit(raw)
    database = parts.path.lstrip("/")
    if (
        parts.scheme not in {"postgresql", "postgresql+asyncpg", "postgres"}
        or not parts.hostname
        or database != "postgres"
        or database == protected_name
    ):
        raise ValueError(
            "POSTGRES_TEST_DSN must identify the test server's /postgres maintenance "
            "database, separate from the daemon database"
        )
    return raw.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _run(
    dsn: str,
    *,
    apply: bool,
    minimum_age: timedelta,
    protected_names: set[str],
    active_run_ids: set[str],
    extra_checkouts: list[Path],
) -> int:
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        databases = await inventory(conn)
        template_scan_error = None
        try:
            active_slugs = checkout_template_slugs(extra_checkouts)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            # Test databases can still be inventoried. Fail closed for every
            # template if even one checkout's slug is uncertain.
            template_scan_error = str(exc)
            active_slugs = {
                match.group(1)
                for row in databases
                if (match := TEMPLATE_NAME.fullmatch(row.name)) is not None
            }
        now = datetime.now(timezone.utc)
        decisions = [
            decide(
                row,
                now=now,
                minimum_age=minimum_age,
                protected_names=protected_names,
                active_template_slugs=active_slugs,
                active_run_ids=active_run_ids,
            )
            for row in databases
        ]
        if template_scan_error:
            print(f"Template cleanup held: {template_scan_error}")
        eligible = 0
        for decision in decisions:
            label = "WOULD_DROP" if decision.eligible else "KEEP"
            print(f"{label:10} {decision.database.name:64} {decision.reason}")
            eligible += decision.eligible
        print(
            f"Plan: {eligible} eligible of {len(decisions)} test/template databases; "
            f"minimum age {minimum_age.total_seconds() / 3600:g} hours"
        )
        if not apply:
            print("Dry run only; no database was changed. Use --apply for deletion.")
            return 0
        dropped = 0
        failed = 0
        for decision in decisions:
            if not decision.eligible:
                continue
            pids = live_pytest_pids()
            if pids:
                print(f"STOP: pytest started during cleanup (PIDs: {pids})", file=sys.stderr)
                return 2
            try:
                result = await drop_if_still_eligible(
                    conn,
                    decision.database,
                    now=datetime.now(timezone.utc),
                    minimum_age=minimum_age,
                    protected_names=protected_names,
                    active_template_slugs=active_slugs,
                    active_run_ids=active_run_ids,
                )
            except Exception as exc:
                failed += 1
                print(
                    f"FAILED     {decision.database.name}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            else:
                dropped += result == "dropped"
                print(f"RESULT     {decision.database.name}: {result}")
        print(f"Apply complete: {dropped} dropped, {failed} failed.")
        return 1 if failed else 0
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inventory abandoned PostgreSQL test databases; dry run by default.",
        epilog=__doc__.split("\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="Delete eligible databases")
    parser.add_argument("--min-age-hours", type=float, default=6.0)
    parser.add_argument("--config", type=Path, default=Path.home() / ".agent-queue" / "config.yaml")
    parser.add_argument(
        "--checkout",
        type=Path,
        action="append",
        default=[],
        help="An independent checkout whose schema template must be retained",
    )
    args = parser.parse_args(argv)
    if args.min_age_hours <= 0:
        parser.error("--min-age-hours must be positive")
    try:
        daemon_name = daemon_database_name(args.config)
        dsn = _maintenance_dsn(daemon_name)
        config = load_config(str(args.config))
        slots = _caps(config.resources)[0]
        lock_dir = default_lock_dir(config)
        slots = observed_slot_count(lock_dir, slots)
        protected = {daemon_name, "postgres", "template0", "template1"}
        minimum_age = timedelta(hours=args.min_age_hours)
        if args.apply:
            with reserve_all_test_slots(lock_dir, slots):
                pids = live_pytest_pids()
                if pids:
                    raise RuntimeError(f"pytest is running (PIDs: {pids}); refusing cleanup")
                return asyncio.run(
                    _run(
                        dsn,
                        apply=True,
                        minimum_age=minimum_age,
                        protected_names=protected,
                        active_run_ids=set(),
                        extra_checkouts=args.checkout,
                    )
                )
        snapshot = SlotSemaphore(lock_dir, slots).snapshot()
        active_ids = {
            holder["test_run_id"]
            for slot in snapshot["slots"]
            if slot["held"]
            if (holder := slot["holder"]).get("test_run_id")
        }
        if any(slot["held"] for slot in snapshot["slots"]):
            print("Live aq test slots exist; --apply will refuse until they are free.")
        pids = live_pytest_pids()
        if pids:
            print(f"Live pytest controllers exist (PIDs: {pids}); --apply will refuse.")
        return asyncio.run(
            _run(
                dsn,
                apply=False,
                minimum_age=minimum_age,
                protected_names=protected,
                active_run_ids=active_ids,
                extra_checkouts=args.checkout,
            )
        )
    except (OSError, RuntimeError, ValueError, asyncpg.PostgresError) as exc:
        print(f"Reaper refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
