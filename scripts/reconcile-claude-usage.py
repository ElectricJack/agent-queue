"""Read-only reconciliation from frozen exports; there is intentionally no apply mode.

Run from the checkout with:
python scripts/reconcile-claude-usage.py --audit usage-corrected.json \
    --ledger-export ledger-export.json --output reconciliation.json

The ledger export is {since: ISO timestamp, until: ISO timestamp, ledger: [rows]}.
The audit's sessions contain paths to local transcripts. Original evidence is never
overwritten. Export production rows in a READ ONLY transaction before invoking this tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sessions.transcripts.base import parse_iso_ts
from src.sessions.transcripts.reconciliation import evidence_hash, reconcile_claude_usage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--ledger-export", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())
    export = json.loads(args.ledger_export.read_text())
    if (audit["since"], audit["until"]) != (export["since"], export["until"]):
        parser.error("audit and ledger export must use the same frozen window")
    report = reconcile_claude_usage(
        [Path(row["path"]) for row in audit["sessions"]], export["ledger"],
        since=parse_iso_ts(audit["since"]), until=parse_iso_ts(audit["until"]),
    )
    report["evidence"] = {
        "audit_path": str(args.audit), "audit_sha256": evidence_hash(audit),
        "ledger_export_path": str(args.ledger_export), "ledger_export_sha256": evidence_hash(export),
    }
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    args.output.chmod(0o600)
    print(json.dumps({key: value for key, value in report.items()
                      if key not in {"proposals", "transcript_window_sha256"}}, indent=2))
    print(f"Proposals: {len(report['proposals'])}; immutable report: {args.output}")


if __name__ == "__main__":
    main()
