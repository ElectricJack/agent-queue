#!/usr/bin/env python3
"""Operator tool: record git-proven delivery of pre-provenance completions.

Preview (default) lists what git proves and what it cannot; ``--apply`` writes
``integration_legacy_deliveries`` rows for the proven ones only. Where git can
prove nothing and the work must not be delivered now either, ``--abandon-task``
(one completed task, leaf or container) and ``--abandon-epic`` (a container by
name) record an explicit, reasoned ``abandoned`` decision instead. Where the
work landed under other commits (a salvage, repair or squash re-landing),
``--relanded TASK_ID=SHA`` records that landing commit once git shows it on the
default branch. Abandoned and re-landed tasks' branches are queued for
retirement (bundle, then delete). See :mod:`src.integration.legacy_backfill`
and docs/guides/git-first-cutover-runbook.md.

    .venv/bin/python scripts/backfill-legacy-deliveries.py agent-queue
    .venv/bin/python scripts/backfill-legacy-deliveries.py agent-queue --apply \
        --reason "git-first cutover: legacy work already on main"
    .venv/bin/python scripts/backfill-legacy-deliveries.py agent-queue \
        --abandon-task amber-apex --abandon-task azure-falcon --apply \
        --reason "superseded by later work on main"
    .venv/bin/python scripts/backfill-legacy-deliveries.py agent-queue \
        --relanded fleet-torrent-37=<full landing sha> --apply \
        --reason "squash re-landed by the 10-07 salvage"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project_id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--operator", default=os.environ.get("USER", "operator"))
    parser.add_argument("--config", default=os.path.expanduser("~/.agent-queue/config.yaml"))
    parser.add_argument("--output", help="write the full JSON report here")
    parser.add_argument("--abandon-epic", action="append", default=[], metavar="EPIC_ID",
                        help="record an operator decision that this completed epic (and its "
                             "undelivered descendants) is superseded and must not be "
                             "delivered; repeatable, instead of the backfill")
    parser.add_argument("--abandon-task", action="append", default=[], metavar="TASK_ID",
                        help="the same decision for one completed task, leaf or container: "
                             "git can no longer prove it and it must not now be delivered, "
                             "so without this it blocks its target for good; repeatable")
    parser.add_argument("--relanded", action="append", default=[], metavar="TASK_ID=SHA",
                        help="record that this completed task's content landed on the "
                             "default branch as commit SHA (a salvage, repair or squash "
                             "re-landing) and retire its branches after a backup; repeatable")
    args = parser.parse_args(argv)
    if args.apply and not args.reason.strip():
        parser.error("--apply needs a nonblank --reason")
    relanded = []
    for item in args.relanded:
        task_id, sep, sha = item.partition("=")
        if not sep or not task_id or not sha:
            parser.error(f"--relanded takes TASK_ID=SHA, not {item!r}")
        relanded.append((task_id, sha))

    from src.config import load_config
    from src.database import create_database
    from src.git.manager import GitManager
    from src.integration.delivery_observer import DeliveryObserver
    from src.integration.git_truth import GitTruth
    from src.integration.legacy_backfill import (
        abandon_epic,
        abandon_task,
        backfill_legacy_deliveries,
        record_relanding,
    )

    config = load_config(args.config)
    db = create_database(config)
    await db.initialize()
    try:
        truth = GitTruth(GitManager())
        # A private observer clone: never contend with the daemon's fetches.
        data_dir = Path(config.data_dir) / "legacy-backfill"
        db.set_delivery_observer(DeliveryObserver(db, git=truth.git, data_dir=data_dir,
                                                  truth=truth))
        if args.abandon_epic or args.abandon_task or relanded:
            report = {}
            if args.abandon_epic:
                report["epics"] = [await abandon_epic(
                    db, args.project_id, epic, dry_run=not args.apply,
                    operator_id=args.operator, reason=args.reason) for epic in args.abandon_epic]
            if args.abandon_task:
                report["tasks"] = [await abandon_task(
                    db, args.project_id, task, dry_run=not args.apply,
                    operator_id=args.operator, reason=args.reason) for task in args.abandon_task]
            if relanded:
                report["relanded"] = [await record_relanding(
                    db, args.project_id, task, landed_sha=sha, dry_run=not args.apply,
                    operator_id=args.operator, reason=args.reason) for task, sha in relanded]
        else:
            report = await backfill_legacy_deliveries(
                db, args.project_id, dry_run=not args.apply, operator_id=args.operator,
                reason=args.reason,
            )
    finally:
        await db.close()
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=1))
    if "unproven" in report:
        summary = {k: v for k, v in report.items() if k not in {"results", "unproven"}}
        summary["unproven"] = [item["task_id"] for item in report["unproven"]]
        print(json.dumps(summary, indent=1))
    else:
        print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
