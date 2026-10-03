"""Read-only token-efficiency report for one bounded window, and a two-window comparison.

Run from the checkout, operator side (the export reads the operator database inside a
READ ONLY transaction; nothing is written to it):

    python scripts/token-efficiency-report.py export \
        --since 2026-09-30T17:42:08Z --until 2026-10-01T17:42:08Z --output before-export.json
    python scripts/token-efficiency-report.py report --export before-export.json \
        --output before-report.json
    python scripts/token-efficiency-report.py compare --before before-report.json \
        --after after-report.json

Outputs are created exclusively (an existing file is never overwritten) with mode 0600.
The comparison's halt reasons are evidence for the operator's rollout decision; this
tool never changes configuration, routing or sessions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.metrics.token_efficiency import build_report, compare, export_rows
from src.sessions.transcripts.base import parse_iso_ts


def _write(path: Path, value) -> None:
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, default=str)
        stream.write("\n")
    path.chmod(0o600)


def _db_url(explicit: str | None) -> str:
    if explicit:
        url = explicit
    else:
        import yaml

        config = yaml.safe_load((Path.home() / ".agent-queue" / "config.yaml").read_text())
        url = config["database"]["url"]
    url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if not url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("export needs a PostgreSQL URL")
    return url


async def _export(args) -> dict:
    import asyncpg

    connection = await asyncpg.connect(
        _db_url(args.db_url), server_settings={"default_transaction_read_only": "on"},
    )
    try:
        async with connection.transaction(readonly=True):
            async def fetch(sql, *params):
                return [dict(row) for row in await connection.fetch(sql, *params)]

            return await export_rows(
                fetch, since=parse_iso_ts(args.since), until=parse_iso_ts(args.until),
                now=time.time(),
            )
    finally:
        await connection.close()


def _print_summary(report: dict) -> None:
    print(json.dumps({key: report[key] for key in (
        "overall", "by_harness", "completions_in_window", "integration_repair_share",
        "churn_share", "routes", "ledger_vs_transcript", "codex_quota_used_percent",
        "missing_transcripts",
    )}, indent=2, default=str))
    print(f"Cohorts: {len(report['cohorts'])}; stuck live attempts: "
          f"{len(report['stuck_attempts_now'])}; "
          f"orphaned open rows: {report['orphaned_open_attempts']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="frozen read-only database export")
    export.add_argument("--since", required=True)
    export.add_argument("--until", required=True)
    export.add_argument("--db-url", help="default: database.url in ~/.agent-queue/config.yaml")
    export.add_argument("--output", required=True, type=Path)
    report = commands.add_parser("report", help="per-attempt and per-cohort metrics")
    report.add_argument("--export", required=True, type=Path)
    report.add_argument("--claude-root", type=Path, default=Path.home() / ".claude" / "projects")
    report.add_argument("--codex-root", type=Path, default=Path.home() / ".codex" / "sessions")
    report.add_argument("--threshold", action="append", default=[], metavar="NAME=VALUE",
                        help="override a halt threshold, e.g. min_attempts=8")
    report.add_argument("--output", required=True, type=Path)
    comparison = commands.add_parser("compare", help="matched cohorts and halt conditions")
    comparison.add_argument("--before", required=True, type=Path)
    comparison.add_argument("--after", required=True, type=Path)
    comparison.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.command == "export":
        rows = asyncio.run(_export(args))
        _write(args.output, rows)
        print(f"Exported {len(rows['attempts'])} attempts, {len(rows['routes'])} routes, "
              f"{len(rows['completions'])} completions to {args.output}")
    elif args.command == "report":
        thresholds = {}
        for item in args.threshold:
            name, _, value = item.partition("=")
            thresholds[name] = float(value)
        result = build_report(json.loads(args.export.read_text()), claude_root=args.claude_root,
                              codex_root=args.codex_root, thresholds=thresholds)
        _write(args.output, result)
        _print_summary(result)
    else:
        result = compare(json.loads(args.before.read_text()), json.loads(args.after.read_text()))
        if args.output:
            _write(args.output, result)
        print(json.dumps(result, indent=2, default=str))
        if result["halt"]:
            print("HALT: do not expand the rollout until each reason is explained.")


if __name__ == "__main__":
    main()
