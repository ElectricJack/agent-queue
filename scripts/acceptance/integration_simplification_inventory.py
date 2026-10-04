#!/usr/bin/env python3
"""Measure the simplification candidate without contacting a daemon or database.

Run from any directory; imports always come from this checkout. Counts describe
source, never production rollout. Emit JSON to stdout for an acceptance record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import click  # noqa: E402

from src.cli.app import cli  # noqa: E402
from src.commands.integration_legacy import (  # noqa: E402
    AGENT_PROTOCOL_CONTROLS,
    APPROVED_INTEGRATION_CONTROLS,
    APPROVED_INTEGRATION_DOCTOR_CHECKS,
    LEGACY_INTEGRATION_CONTROLS,
    LEGACY_INTEGRATION_DOCTOR_CHECKS,
)
from src.database.tables import metadata  # noqa: E402
from src.doctor import default_registry  # noqa: E402
from src.integration.subjects import PRIMITIVE_OUTCOMES, UNKNOWN, Primitive  # noqa: E402
from src.integration.table_retirement import (  # noqa: E402
    FAMILIES,
    RETAINED_TABLES,
    code_references,
)


def inventory(source_sha: str) -> dict:
    modules = sorted((ROOT / "src/integration").glob("*.py"))
    source_files = sorted(set(modules) | {
        ROOT / "src/commands/integration_legacy.py",
        ROOT / "src/cli/integration.py",
        ROOT / "src/database/tables.py",
        ROOT / "src/doctor/integration_checks.py",
        ROOT / "src/doctor/integration_subject_checks.py",
    })
    files = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_files
    }
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    group = cli.get_command(click.Context(cli), "integration")
    assert isinstance(group, click.Group)
    tables = sorted(name for name in metadata.tables if "integration" in name or name in {
        "task_branch_origins", "task_delivery_receipts",
    })
    retiring = tuple(table for family in FAMILIES for table in family.tables)
    references = code_references(retiring, source_root=ROOT / "src")
    outcomes = {primitive.value: sorted(PRIMITIVE_OUTCOMES[primitive]) for primitive in Primitive}
    return {
        "source_sha": source_sha,
        "scope": "checkout source only; no live rollout or CI claims",
        "measurement": {
            "modules": "top-level src/integration/*.py, including __init__.py",
            "lines": "physical lines, including blank lines and comments",
            "tables": "metadata names containing integration plus task origins/receipts",
            "outcomes": "distinct closed primitive labels; unknown is already included",
        },
        "source_files_sha256": fingerprint,
        "source_files": files,
        "targets": {
            "python_modules": 9,
            "python_lines_less_than": 12000,
            "primitives": 20,
            "approximate_distinct_outcomes": 45,
            "operator_controls": 6,
            "doctor_checks": 3,
            "tables": 9,
        },
        "modules": {
            "count": len(modules),
            "lines": sum(len(path.read_bytes().splitlines()) for path in modules),
            "files": [str(path.relative_to(ROOT)) for path in modules],
        },
        "primitives": {
            "count": len(Primitive),
            "distinct_outcomes": len(set().union(*PRIMITIVE_OUTCOMES.values()) | {UNKNOWN}),
            "primitive_outcome_pairs": sum(map(len, PRIMITIVE_OUTCOMES.values())),
            "outcomes": outcomes,
        },
        "controls": {
            "approved": [control._asdict() for control in APPROVED_INTEGRATION_CONTROLS],
            "agent_protocol": [control._asdict() for control in AGENT_PROTOCOL_CONTROLS],
            "legacy": [control._asdict() for control in LEGACY_INTEGRATION_CONTROLS],
            "visible_cli_entries": sorted(
                name for name, command in group.commands.items() if not command.hidden
            ),
        },
        "doctor": {
            "approved": list(APPROVED_INTEGRATION_DOCTOR_CHECKS),
            "legacy": [entry._asdict() for entry in LEGACY_INTEGRATION_DOCTOR_CHECKS],
            "registered": sorted(
                name for name in default_registry().ids() if name.startswith("integration.")
            ),
        },
        "tables": {
            "count": len(tables),
            "names": tables,
            "retained": RETAINED_TABLES,
            "retirement_families": [{
                "name": family.name,
                "target": family.target,
                "code_removed": family.code_removed,
                "tables": list(family.tables),
                "source_references": {name: list(references[name]) for name in family.tables},
            } for family in FAMILIES],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True, help="full Git HEAD for the measured checkout")
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{40}", args.source_sha) is None:
        parser.error("--source-sha must be a full lowercase 40-hex Git OID")
    print(json.dumps(inventory(args.source_sha), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
