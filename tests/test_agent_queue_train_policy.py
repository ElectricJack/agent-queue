"""Static checks for the reviewed agent-queue train configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.integration.models import HierarchicalIntegrationPolicy
from src.integration.ci import ci_trust_from_policy
from src.playbooks.definition import artifact_sha256, canonical_bytes, load_definition_json
from src.playbooks.proposal import source_digest
from src.playbooks.validation import (
    NullProfileLookup,
    RegisteredEventLookup,
    RegistryContractLookup,
    validate_definition,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "docs/config/agent-queue-train-policy.json"
BUNDLES = ROOT / "src/prompts/reviewed_playbooks"


def test_required_checks_match_workflow_matrix() -> None:
    policy = HierarchicalIntegrationPolicy.model_validate(json.loads(POLICY.read_text()))
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    job = workflow["jobs"]["test"]
    assert job["name"] == "Tests (${{ matrix.suite.name }})"
    expected = tuple(f"Tests ({suite['name']})" for suite in job["strategy"]["matrix"]["suite"])
    assert policy.parent.required_checks.names == expected
    assert policy.root.required_checks.names == expected
    assert policy.parent.required_checks.producer_id == "github-actions"
    assert policy.root.required_checks.producer_id == "github-actions"
    for boundary in ("parent", "root"):
        trust = ci_trust_from_policy(
            canonical_repository_id="agent-queue2",
            repository_id=1,
            full_name="example/agent-queue2",
            policy=policy,
            boundary=boundary,
        )
        assert trust.producer_id == "github-actions"
        assert trust.required_checks.names == expected


@pytest.mark.parametrize("boundary", ["parent", "root"])
def test_policy_route_matches_reviewed_bundle(boundary: str) -> None:
    policy = HierarchicalIntegrationPolicy.model_validate(json.loads(POLICY.read_text()))
    route = getattr(policy, boundary).route
    folder = BUNDLES / route.playbook_id
    artifact_bytes = (folder / "artifact.json").read_bytes()
    artifact = load_definition_json(artifact_bytes.decode())
    manifest_text = (folder / "manifest.md").read_text()
    manifest = yaml.safe_load(manifest_text.split("---", 2)[1])
    snapshot = route.artifact

    assert route.scope == "project"
    assert route.scope_identifier == "agent-queue"
    assert artifact.scope.type == "project"
    assert artifact.scope.project_id == "agent-queue"
    assert canonical_bytes(artifact) == artifact_bytes
    assert artifact_sha256(artifact) == snapshot.artifact_sha256
    assert (folder / "artifact.sha256").read_text().strip() == snapshot.artifact_sha256
    assert artifact.id == snapshot.playbook_id == manifest["playbook_id"]
    assert artifact.schema_version == snapshot.schema_generation
    assert artifact.version == snapshot.version
    assert artifact.compiler_build == snapshot.compiler_build
    # ArtifactStore.put records a nullable compile time in its durable ArtifactRef.
    # Preflight compares the route to that stored reference, not the JSON body.
    assert snapshot.compiled_at is None
    assert artifact.contract_fingerprint() == snapshot.contract_fingerprint
    assert source_digest((folder / "source.md").read_text()) == snapshot.source_digest
    assert artifact.source_hash == snapshot.source_digest
    assert manifest["artifact_sha256"] == snapshot.artifact_sha256
    assert manifest["source_sha256"] == snapshot.source_digest
    assert manifest["contract_fingerprint"] == snapshot.contract_fingerprint
    assert manifest["profiles_referenced"] == []
    assert not [
        diagnostic
        for diagnostic in validate_definition(
            artifact,
            inventory=None,
            contracts=RegistryContractLookup(),
            profiles=NullProfileLookup(),
            events=RegisteredEventLookup(),
        )
        if diagnostic.severity in {"error", "question"}
    ]
