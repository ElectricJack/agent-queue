"""Compile only this review candidate; never import, install, or activate it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from src.playbooks.authoring import PlaybookSource  # noqa: E402
from src.playbooks.definition import canonical_bytes  # noqa: E402
from src.playbooks.profiles import shipped_profile_lookup  # noqa: E402
from src.playbooks.proposal import propose  # noqa: E402
from src.playbooks.validation import RegisteredEventLookup, RegistryContractLookup  # noqa: E402

BUNDLE = Path(__file__).resolve().parent


def compile_bundle():
    source = PlaybookSource.load(BUNDLE / "source.md", vault_root=BUNDLE)
    assert isinstance(source, PlaybookSource), source
    body = json.loads((BUNDLE / "semantic-body.json").read_text())
    # Policy is recorded, while references follow the review candidate's prose.
    lines = source.raw.splitlines()
    for node in [*body["rules"], *body["steps"].values()]:
        ref = node["source"]
        line = lines.index(ref["excerpt"]) + 1
        node["source"] = {**ref, "path": source.vault_path, "start_line": line, "end_line": line}
    return propose(
        source,
        body,
        contracts=RegistryContractLookup(),
        profiles=shipped_profile_lookup(),
        events=RegisteredEventLookup(),
        version=1,
        enforce_inventory=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    proposal = compile_bundle()
    diagnostics = [
        {"severity": d.severity, "code": d.code, "message": d.message} for d in proposal.diagnostics
    ]
    if proposal.artifact is None or any(
        d["severity"] in {"error", "question"} for d in diagnostics
    ):
        raise SystemExit(json.dumps(diagnostics, indent=2))
    artifact = proposal.artifact
    if args.check:
        stored = json.loads((BUNDLE / "artifact.json").read_text())
        rebuilt = json.loads(canonical_bytes(artifact))
        for payload in (stored, rebuilt):
            payload.pop("compiled_at", None)
        assert stored == rebuilt, "candidate differs from its source/semantic body/contracts"
        assert json.loads((BUNDLE / "diagnostics.json").read_text()) == diagnostics
        print("project-defaults: reproducible (excluding compilation timestamp)")
        return
    (BUNDLE / "artifact.json").write_bytes(canonical_bytes(artifact))
    (BUNDLE / "artifact.sha256").write_text(proposal.artifact_sha256 + "\n")
    (BUNDLE / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2) + "\n")
    (BUNDLE / "manifest.md").write_text(f"""---
playbook_id: project-defaults
artifact_sha256: {proposal.artifact_sha256}
source_sha256: {artifact.source_hash}
contract_fingerprint: {artifact.contract_fingerprint()}
profiles_referenced: []
review_status: pending
---

# Project-defaults review candidate

Prepared for crisp-meadow-72. This is not an approval or an installation.
See [the equivalence note](equivalence.md) for provenance, deliberate retentions,
validation and the separate operator review/import/activation boundary.
""")
    print(f"project-defaults: {proposal.artifact_sha256}")


if __name__ == "__main__":
    main()
