"""Build the §13.3 manual-scenario payloads from the checked-in §10 fixtures.

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md``
§13.3 asks for seven screenshots of the V2 semantic review surface.  Standing a
daemon up inside a worker slot is not allowed (no migrations, never the
operator database), so this script produces the *same* API responses the
daemon's V2 command mixin would return — by calling the real projections
(``project_graph``, ``diff_artifacts``, ``project_overlay``, ``evaluate_health``)
on the real §10 artifacts — and writes them as one JSON file per scenario.

``dashboard/scenarios/`` mounts the real dashboard components against these
files, so what is screenshotted is production code rendering production
projections of a real artifact.

Usage:  python scripts/pkg5_scenarios/build_payloads.py [OUT_DIR]
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.commands.playbook_v2_commands import RegistryContractLookup
from src.playbooks.activation import evaluate_health, profile_fingerprint
from src.playbooks.artifact_diff import diff_artifacts
from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.definition import load_definition_json
from src.playbooks.graph_projection import project_graph
from src.playbooks.run_overlay import project_overlay
from tests.playbook_v2_helpers import StubProfiles

FIXTURES = ROOT / "tests" / "fixtures" / "playbooks" / "v2"

GRAPH_URL = "/api/playbook/v2-graph"
HEALTH_URL = "/api/playbook/activation-health"
ARTIFACTS_URL = "/api/playbook/artifacts"
DIFF_URL = "/api/playbook/artifact-diff"
PENDING_URL = "/api/playbook/pending-events"
OVERLAY_URL = "/api/playbook/run-overlay"
RUNS_URL = "/api/playbook/list-runs"


def _definition(name: str):
    return load_definition_json((FIXTURES / name).read_text())


def _ref(definition) -> ArtifactRef:
    return ArtifactRef(
        playbook_id=definition.id,
        artifact_sha256=definition.artifact_sha256(),
        schema_generation=definition.schema_version,
        contract_fingerprint=definition.contract_fingerprint(),
        source_digest=definition.source_hash,
        compiler_build=definition.compiler_build or "fixture",
        compiled_at=definition.compiled_at.isoformat(),
        version=definition.version,
    )


def _ref_dict(ref: ArtifactRef) -> dict:
    return {
        "playbook_id": ref.playbook_id,
        "artifact_sha256": ref.artifact_sha256,
        "schema_generation": ref.schema_generation,
        "contract_fingerprint": ref.contract_fingerprint,
        "source_digest": ref.source_digest,
        "compiler_build": ref.compiler_build,
        "compiled_at": ref.compiled_at,
        "version": ref.version,
    }


def _activation(definition, ref, *, health="ready", reasons=(), enabled=True) -> dict:
    """The ``ActivationStateDTO`` shape the daemon serves for one activation."""
    return {
        "playbook_id": definition.id,
        "scope": definition.scope.type,
        "scope_identifier": None,
        "enabled": enabled,
        "active_artifact_sha256": ref.artifact_sha256,
        "health": getattr(health, "value", health),
        "reasons": [reason.as_dict() for reason in reasons],
        "activated_at": 1_756_000_000.0,
        "activated_by": "operator",
        "pending_event_count": 0,
        "running_count": 0,
    }


def _health_response(activation: dict) -> dict:
    return {
        "success": True,
        "activations": [activation],
        "count": 1,
        "by_health": {activation["health"]: 1},
    }


def _artifacts_response(playbook_id: str, entries: list[tuple[ArtifactRef, bool]]) -> dict:
    active = next((ref.artifact_sha256 for ref, is_active in entries if is_active), None)
    return {
        "success": True,
        "playbook_id": playbook_id,
        "artifacts": [
            {
                "artifact": _ref_dict(ref),
                "scope": "system",
                "scope_identifier": None,
                "size_bytes": 0,
                "created_at": 1_756_000_000.0 + ref.version,
                "is_active": is_active,
            }
            for ref, is_active in entries
        ],
        "count": len(entries),
        "active_artifact_sha256": active,
    }


class _BumpedContracts:
    """The live command registry with one command's fingerprint moved.

    Scenario 5 needs an artifact whose ``gate_create`` contract has changed
    since it was compiled.  The registry is real; only the one fingerprint the
    scenario is about is overridden, so every other lookup — argument specs,
    result schemas, outcomes — stays production-accurate.
    """

    def __init__(self, command: str, fingerprint: str) -> None:
        self._inner = RegistryContractLookup()
        self._command = command
        self._fingerprint = fingerprint
        if self._inner.get(command) is None:
            raise SystemExit(f"{command!r} is not a registered command contract")

    def get(self, name: str):
        info = self._inner.get(name)
        if info is not None and name == self._command:
            return replace(info, execution_fingerprint=self._fingerprint)
        return info


def build() -> dict[str, dict]:
    v5 = _definition("review-pipeline.artifact.json")
    v6 = _definition("review-pipeline.v6.artifact.json")
    v5_ref, v6_ref = _ref(v5), _ref(v6)
    contracts, profiles = RegistryContractLookup(), StubProfiles()
    receipts = json.loads((FIXTURES / "review-pipeline.receipts.json").read_text())["receipts"]

    def graphs(activation, lookup=None):
        """Every projection the review surface can ask for, keyed the way the
        request is: artifact hash first, then event scope (``""`` = all events).

        Both dimensions matter. The dashboard refetches when an operator
        narrows the event scope *or* picks a different candidate artifact, so a
        harness that answered with one fixed projection would show a chooser
        and a filter that both look inert.
        """
        lookup = lookup or contracts
        out: dict[str, dict] = {}
        for definition, ref in ((v5, v5_ref), (v6, v6_ref)):
            scopes = [None] + [rule.trigger.event_type for rule in definition.rules]
            out[ref.artifact_sha256] = {
                (event or ""): project_graph(
                    definition, ref, activation,
                    event_type=event, contracts=lookup, profiles=profiles,
                )
                for event in dict.fromkeys(scopes)
            }
        active = activation.get("active_artifact_sha256") if activation else None
        out["active"] = out.get(active, out[v5_ref.artifact_sha256])
        return out

    def diffs(base_ref, lookup=None):
        """One diff per candidate target, against whichever artifact is active."""
        lookup = lookup or contracts
        base = v5 if base_ref is v5_ref else v6
        return {
            ref.artifact_sha256: diff_artifacts(
                base, definition, base_ref=base_ref, target_ref=ref,
                contracts=lookup, profiles=profiles,
            )
            for definition, ref in ((v5, v5_ref), (v6, v6_ref))
        }

    ready = _activation(v5, v5_ref)
    pending = {"success": True, "events": [], "count": 0, "oldest_received_at": None, "by_reason": {}}
    runs_none = {"success": True, "runs": [], "count": 0}
    run = {
        "run_id": "run-7",
        "playbook_id": v5.id,
        "artifact_sha256": v5_ref.artifact_sha256,
        "rule_id": "review-on-task-completed",
        "lifecycle": "completed",
        "event_type": "task.completed",
        "event": {},
        "bindings": {},
        "budget": {"llm_calls": 1, "total_tokens": 3168},
    }
    runs_one = {
        "success": True,
        "runs": [
            {
                "run_id": "run-7",
                "playbook_id": v5.id,
                "rule_id": "review-on-task-completed",
                "status": "completed",
                "artifact_sha256": v5_ref.artifact_sha256,
            }
        ],
        "count": 1,
    }

    base = {
        HEALTH_URL: _health_response(ready),
        GRAPH_URL: graphs(ready),
        ARTIFACTS_URL: _artifacts_response(v5.id, [(v6_ref, False), (v5_ref, True)]),
        DIFF_URL: diffs(v5_ref),
        PENDING_URL: pending,
        RUNS_URL: runs_none,
        OVERLAY_URL: None,
    }

    scenarios: dict[str, dict] = {
        # 1, 2, 4 and 6 all review the same v5-active playbook; they differ only
        # in what the driver selects on screen.
        "01-branching": dict(base),
        "02-convergence": dict(base),
        "04-ai-node": dict(base),
        "06-diff-review": dict(base),
    }

    # 3 — loop: the run overlay supplies the traversal counts.
    scenarios["03-loop"] = {
        **base,
        RUNS_URL: runs_one,
        OVERLAY_URL: project_overlay(
            run, receipts, v5, v5_ref, active_sha256=v5_ref.artifact_sha256
        ),
    }

    # 5 — stale contract: the same artifact, a registry that moved under it.
    stale_contracts = _BumpedContracts("gate_create", "sha256:" + "ab" * 32)
    health, reasons = evaluate_health(
        enabled=True,
        artifact=v5_ref,
        artifact_present=True,
        validation={},
        current_contract_fingerprints={
            name: getattr(stale_contracts.get(name), "execution_fingerprint", "")
            for name in v5.compiled_against.commands
        },
        artifact_contract_fingerprints=dict(v5.compiled_against.commands),
        current_profile_fingerprints=dict(v5.compiled_against.profiles),
        artifact_profile_fingerprints=dict(v5.compiled_against.profiles),
        stored_profile_fingerprint=profile_fingerprint(dict(v5.compiled_against.profiles)),
    )
    stale_activation = _activation(v5, v5_ref, health=health, reasons=reasons)
    scenarios["05-stale-contract"] = {
        **base,
        HEALTH_URL: _health_response(stale_activation),
        GRAPH_URL: graphs(stale_activation, stale_contracts),
        DIFF_URL: diffs(v5_ref, stale_contracts),
    }

    # 7 — a completed run of v5 while v6 is the active artifact.
    v6_active = _activation(v6, v6_ref)
    scenarios["07-run-overlay-old-artifact"] = {
        **base,
        HEALTH_URL: _health_response(v6_active),
        ARTIFACTS_URL: _artifacts_response(v5.id, [(v6_ref, True), (v5_ref, False)]),
        GRAPH_URL: graphs(v6_active),
        DIFF_URL: diffs(v6_ref),
        RUNS_URL: runs_one,
        OVERLAY_URL: project_overlay(
            run, receipts, v5, v5_ref, active_sha256=v6_ref.artifact_sha256
        ),
    }
    return scenarios


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dashboard" / "scenarios" / "payloads"
    out.mkdir(parents=True, exist_ok=True)
    scenarios = build()
    for name, payloads in sorted(scenarios.items()):
        path = out / f"{name}.json"
        path.write_text(json.dumps(payloads, indent=2, sort_keys=True) + "\n")
        display_path = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        print(f"wrote {display_path}")
    (out / "index.json").write_text(json.dumps(sorted(scenarios), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
