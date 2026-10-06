#!/usr/bin/env python3
"""Operator tool: record git-proven delivery of pre-provenance completions.

Preview (default) lists what git proves and what it cannot; ``--apply`` writes
``integration_legacy_deliveries`` rows for the proven ones only. See
:mod:`src.integration.legacy_backfill` and docs/guides/git-first-cutover-runbook.md.

    .venv/bin/python scripts/backfill-legacy-deliveries.py agent-queue
    .venv/bin/python scripts/backfill-legacy-deliveries.py agent-queue --apply \
        --reason "git-first cutover: legacy work already on main"
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
    args = parser.parse_args(argv)
    if args.apply and not args.reason.strip():
        parser.error("--apply needs a nonblank --reason")

    from src.config import load_config
    from src.database import create_database
    from src.git.manager import GitManager
    from src.integration.delivery_observer import DeliveryObserver
    from src.integration.git_truth import GitTruth
    from src.integration.legacy_backfill import backfill_legacy_deliveries

    config = load_config(args.config)
    db = create_database(config)
    await db.initialize()
    try:
        truth = GitTruth(GitManager())
        # A private observer clone: never contend with the daemon's fetches.
        data_dir = Path(config.data_dir) / "legacy-backfill"
        db.set_delivery_observer(DeliveryObserver(db, git=truth.git, data_dir=data_dir,
                                                  truth=truth))
        report = await backfill_legacy_deliveries(
            db, args.project_id, dry_run=not args.apply, operator_id=args.operator,
            reason=args.reason,
        )
    finally:
        await db.close()
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=1))
    summary = {k: v for k, v in report.items() if k not in {"results", "unproven"}}
    summary["unproven"] = [item["task_id"] for item in report["unproven"]]
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
