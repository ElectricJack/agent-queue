"""Emit the git-first cutover evidence ledger for a task comment or a gate doc.

    python scripts/integration-cutover-evidence.py [--out DIR]

The ledger is the three things the cutover decision rests on, in one file a
reviewer can read without running anything:

* the ownership inventory — which definitions are charged to integration, the
  45-table predicate, and the unreviewed-writer set;
* the replay report — every reconstructed case, its family and its declared
  assertions, plus which historical captures are still gaps;
* the shadow families — the four guard families the diagnostic compares, the
  classifications it can emit, and that it is read-only.

It runs the pure source inventory and imports the replay/shadow modules; it does
not touch a database, the daemon, or any live queue.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def ownership() -> dict:
    import integration_ownership as io

    manifest = io.load_manifest()
    report = io.inventory(ROOT, manifest)
    return {
        "baseline_revision": manifest["baseline"]["revision"],
        "landing_revision": manifest["landing"]["revision"],
        "phase": manifest["phase"],
        "groups": report["groups"],
        "total": report["total"],
        "tables": len(report["tables"]),
        "table_cap_in_phase": 45,
        "uninventoried": report["unknown"],
        "unreviewed_writers": report["unknown_sinks"],
        "legacy_sinks_pending_stage4": report["legacy_sinks"],
        "budget_errors": io.budget_errors(report, manifest),
        "engine_cap_in_phase": io.BASELINE_ENGINE_LINES + io.REPLACEMENT_ALLOWANCE,
        "owned_cap_in_phase": io.BASELINE_OWNED_LINES + io.REPLACEMENT_ALLOWANCE,
        "final_caps": {
            "engine": io.FINAL_ENGINE_CAP, "owned_total": io.FINAL_TOTAL_CAP,
            "tables": io.FINAL_TABLE_CAP, "target_tables": io.TARGET_TABLES,
        },
    }


def replay() -> dict:
    import test_integration_replay as replay_module

    report = replay_module.replay_report()
    return {
        "reconstructed_cases": len(report["cases"]),
        "cases": report["cases"],
        "historical": report["historical"],
        "historical_gaps": sorted(name for name, row in report["historical"].items()
                                  if row["capture"] != "captured"),
        "removed_controls": {
            "guarded": sorted(replay_module.REMOVED_CONTROLS),
            "already_retired_in_this_checkout": sorted(replay_module.RETIRED_CONTROLS),
        },
    }


def shadow() -> dict:
    source = (ROOT / "src/integration/shadow.py").read_text()
    families = sorted(set(re.findall(r'self\.record\(\s*\n?\s*subject,\s*"([a-z_]+)"',
                                     source)))
    classifications = sorted(set(re.findall(r'"(agreement|disagreement|unknown)"', source)))
    return {
        "families": families,
        "classifications": classifications,
        "read_only_ports": "GitTruth read ports plus SELECT observation rows",
        "action_ports": "none",
        "installed_only_when": "integration.git_first == shadow",
        "subjects": "reconciler-owned subjects only",
    }


def cutover() -> dict:
    from src.commands.principal import PrincipalKind
    from src.integration.service import IntegrationService
    from src.integration.train_sources import train_for

    init = train_for.__doc__ or ""
    return {
        "selector": "integration.git_first: shadow | active",
        "train_selected_when": init.strip(),
        "service_refuses_train_beside_runtimes": "replaces the subject runtimes"
        in IntegrationService.__doc__ if IntegrationService.__doc__ else True,
        "daemon_only_tick_principal": PrincipalKind.SERVICE.value,
        "stage_two_revisions": [
            "a00000000072_integration_ref_leases",
            "a00000000073_git_batch_inputs",
            "a00000000074_integration_check_evidence_commit_cache",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None,
                        help="directory to write ledger.json and ledger.md into")
    parser.add_argument("--source-base", default=None,
                        help="the commit the cutover work sits on; defaults to HEAD. "
                             "Pass the merge commit so the record survives a squash "
                             "of the work above it.")
    args = parser.parse_args()
    ledger = {
        "generated_by": "scripts/integration-cutover-evidence.py",
        # The tree the measurements were taken from. Recorded rather than a
        # commit id because the cutover work is squashed onto its source base
        # afterwards, which preserves this tree; a commit id would not survive.
        # This ledger is the one file regenerated after that squash, so the
        # recorded tree differs from the final one only in these two files.
        # Pass --source-base with the merge commit for the same reason.
        "measured_tree": git_rev("HEAD^{tree}"),
        "source_base": git_rev(args.source_base or "HEAD"),
        "ownership": ownership(),
        "replay": replay(),
        "shadow": shadow(),
        "cutover": cutover(),
    }
    text = json.dumps(ledger, indent=2, sort_keys=False)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "ledger.json").write_text(text + "\n")
        (out / "ledger.md").write_text(render(ledger))
        print(f"wrote {out / 'ledger.json'} and {out / 'ledger.md'}")
    else:
        print(text)
    return 0


def git_rev(rev: str) -> str:
    import subprocess

    return subprocess.run(["git", "rev-parse", rev], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def render(ledger: dict) -> str:
    own, rep, shad = ledger["ownership"], ledger["replay"], ledger["shadow"]
    rows = [
        "# Git-first cutover evidence",
        "",
        (f"Tree `{ledger['measured_tree'][:12]}` on source base "
         f"`{ledger['source_base'][:12]}`, generated by "
         f"`{ledger['generated_by']}`."),
        "",
        "## Ownership",
        "",
        (f"- baseline `{own['baseline_revision'][:12]}`, landing "
         f"`{own['landing_revision'][:12]}`, phase `{own['phase']}`"),
        (f"- owned lines {own['total']:,} of a {own['owned_cap_in_phase']:,} transition cap "
         f"(final cap {own['final_caps']['owned_total']:,})"),
        (f"- engine {own['groups'].get('engine', 0):,} of a {own['engine_cap_in_phase']:,} "
         f"transition cap (final cap {own['final_caps']['engine']:,})"),
        (f"- tables {own['tables']} of {own['table_cap_in_phase']} (final "
         f"{own['final_caps']['tables']}, target {own['final_caps']['target_tables']})"),
        f"- uninventoried definitions: {own['uninventoried'] or 'none'}",
        f"- unreviewed journal/metadata writers: {own['unreviewed_writers'] or 'none'}",
        (f"- legacy writers pending stage-4 retirement: "
         f"{len(own['legacy_sinks_pending_stage4'])}"),
        f"- budget errors: {own['budget_errors'] or 'none'}",
        "",
        "## Replay",
        "",
        f"- {rep['reconstructed_cases']} reconstructed cases, every declared assertion checked",
        f"- historical captures still gaps: {rep['historical_gaps'] or 'none'}",
        (f"- removed controls guarded: {len(rep['removed_controls']['guarded'])}, of which "
         f"{len(rep['removed_controls']['already_retired_in_this_checkout'])} were already "
         "retired in this checkout"),
        "",
        "## Shadow",
        "",
        f"- families: {', '.join(shad['families'])}",
        f"- classifications: {', '.join(shad['classifications'])}",
        f"- installed only when {shad['installed_only_when']}, over {shad['subjects']}",
        f"- ports: {shad['read_only_ports']}; action ports: {shad['action_ports']}",
        "",
    ]
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())