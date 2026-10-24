#!/usr/bin/env python3
"""Compile this disabled review candidate offline; never install or activate it."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from src.playbooks.authoring import PlaybookSource  # noqa: E402
from src.playbooks.definition import artifact_sha256, canonical_bytes, load_definition_json  # noqa: E402
from src.playbooks.profiles import shipped_profile_lookup  # noqa: E402
from src.playbooks.proposal import load_semantic_body_json, propose  # noqa: E402
from src.playbooks.validation import (  # noqa: E402
    RegisteredEventLookup,
    RegistryContractLookup,
    validate_definition,
)

HERE = Path(__file__).resolve().parent
BASELINE = ROOT / "tests/fixtures/quilt_health_watch/installed/artifact.json"


def compile_candidate():
    source = PlaybookSource.load(HERE / "source.md", vault_root=ROOT)
    assert isinstance(source, PlaybookSource), source
    assert source.frontmatter["enabled"] is False
    baseline = load_definition_json(BASELINE.read_text())
    proposal = propose(
        source, load_semantic_body_json((HERE / "semantic-body.json").read_text()),
        baseline=baseline, version=baseline.version + 1,
        contracts=RegistryContractLookup(), profiles=shipped_profile_lookup(),
        events=RegisteredEventLookup(),
    )
    blocking = [asdict(d) for d in proposal.diagnostics if d.severity in {"error", "question"}]
    if not proposal.activatable or proposal.artifact is None:
        raise ValueError(json.dumps(blocking, indent=2, default=str))
    artifact = proposal.artifact
    # Preserve the recording timestamp; rebuilding cannot manufacture a new identity.
    if (HERE / "artifact.json").exists():
        recorded = load_definition_json((HERE / "artifact.json").read_text())
        artifact = artifact.model_copy(update={"compiled_at": recorded.compiled_at})
    diagnostics = validate_definition(
        artifact, inventory=None, contracts=RegistryContractLookup(),
        profiles=shipped_profile_lookup(), events=RegisteredEventLookup(),
    )
    assert not [d for d in diagnostics if d.severity in {"error", "question"}], diagnostics
    return artifact, proposal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    artifact, proposal = compile_candidate()
    sha = artifact_sha256(artifact)
    metadata = {
        "playbook_id": artifact.id, "artifact_sha256": sha,
        "source_sha256": artifact.source_hash,
        "contract_fingerprint": artifact.contract_fingerprint(),
        "questions_resolved": 0, "profiles_referenced": [],
    }
    outputs = {
        "artifact.json": canonical_bytes(artifact),
        "artifact.sha256": (sha + "\n").encode(),
        "diagnostics.json": (json.dumps(
            [asdict(d) for d in proposal.diagnostics], indent=2, sort_keys=True, default=str,
        ) + "\n").encode(),
        "manifest.md": (
            "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n\n"
            "# Review candidate — not approved or installed\n\n"
            "Port of the supervisor-supplied installed Quilt health watch. "
            "Source remains disabled. Review this exact artifact before any import "
            "or activation. See README.md for retained policy and verification.\n"
        ).encode(),
    }
    for name, data in outputs.items():
        path = HERE / name
        if args.check:
            assert path.read_bytes() == data, f"candidate drift: {path}"
        else:
            path.write_bytes(data)
    print(f"{'Checked' if args.check else 'Compiled'} {artifact.id}: {sha}")


if __name__ == "__main__":
    main()
