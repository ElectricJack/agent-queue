#!/usr/bin/env python3
"""Build a review candidate from an old worker-pinned Playbooks V2 bundle.

This is an offline compiler aid. It writes a NEW directory and never imports,
activates, edits the input vault, or connects to the daemon database. Import
revalidates the candidate against the daemon's live registries after review.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.playbooks.authoring import PlaybookSource  # noqa: E402
from src.playbooks.definition import canonical_bytes, referenced_profile_ids  # noqa: E402
from src.playbooks.profiles import shipped_profile_lookup  # noqa: E402
from src.playbooks.proposal import (  # noqa: E402
    load_legacy_baseline_json,
    load_semantic_body_json,
    propose,
)
from src.playbooks.validation import RegisteredEventLookup, RegistryContractLookup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--semantic-body", type=Path, required=True)
    parser.add_argument("--baseline-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="new candidate bundle directory; must not exist")
    args = parser.parse_args()

    source = PlaybookSource.load(args.source, vault_root=args.vault_root)
    if not isinstance(source, PlaybookSource):
        parser.error(f"source could not be loaded: {source}")
    baseline_bytes = args.baseline_artifact.read_bytes()
    baseline = load_legacy_baseline_json(baseline_bytes.decode("utf-8"))
    if baseline.id != source.frontmatter["id"]:
        parser.error("baseline and source playbook ids differ")
    body = load_semantic_body_json(args.semantic_body.read_text(encoding="utf-8"))
    proposal = propose(
        source, body, baseline=baseline,
        contracts=RegistryContractLookup(), profiles=shipped_profile_lookup(),
        events=RegisteredEventLookup(), version=baseline.version + 1,
    )
    if not proposal.activatable or proposal.artifact is None:
        for diagnostic in proposal.diagnostics:
            print(f"{diagnostic.severity}: {diagnostic.code}: {diagnostic.message}", file=sys.stderr)
        return 1
    artifact = proposal.artifact
    data = canonical_bytes(artifact)
    sha = proposal.artifact_sha256
    assert sha is not None
    if args.output.name != artifact.id:
        parser.error(f"output directory must be named {artifact.id!r} for import")

    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "artifact.json").write_bytes(data)
    (args.output / "artifact.sha256").write_text(sha + "\n", encoding="utf-8")
    (args.output / "source.md").write_text(source.raw, encoding="utf-8")
    manifest = {
        "playbook_id": artifact.id,
        "artifact_sha256": sha,
        "source_sha256": artifact.source_hash,
        "contract_fingerprint": artifact.contract_fingerprint(),
        "questions_resolved": 0,
        "profiles_referenced": list(referenced_profile_ids(artifact)),
    }
    frontmatter = yaml.safe_dump(manifest, sort_keys=False).rstrip()
    (args.output / "manifest.md").write_text(
        f"---\n{frontmatter}\n---\n\n"
        "# Migration candidate\n\nReview the semantic diff and approve this new artifact "
        "before import or activation.\n",
        encoding="utf-8",
    )
    (args.output / "migration-report.json").write_text(
        json.dumps({
            "old_sha256": "sha256:" + hashlib.sha256(baseline_bytes).hexdigest(),
            "new_sha256": sha,
            "old_version": baseline.version,
            "new_version": artifact.version,
            "diagnostics": [
                {"severity": item.severity, "code": item.code, "message": item.message}
                for item in proposal.diagnostics
            ],
            "semantic_diff": asdict(proposal.semantic_diff) if proposal.semantic_diff else None,
        }, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote candidate {args.output} ({sha}); review before import")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
