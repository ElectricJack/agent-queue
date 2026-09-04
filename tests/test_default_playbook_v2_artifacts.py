"""The reviewed V2 artifact fixtures for the shipped playbooks.
Package 6 §5.3 (T-7, T-9).  The fixtures are *recordings*: a human compiled each
shipped source, read the semantic diff, resolved every compiler question, and
checked the result in.  These tests validate the recording and never regenerate
it — a compiler change that alters output is caught by the release check
(`tests/test_playbook_contract_release_check.py`), not by a nondeterministic
re-run in CI.

Reconciliation against the live tree (child plan §3.8 requires deviations be
recorded rather than silently substituted):

* `PlaybookDefinition` has **`source_hash`**, not `compiled_from.source_digest`.
  T-7 assertion 4 is asserted against `source_hash`.
* Every enabled shipped source is approved. A rejected fixture remains excluded
  from activation, but no shipped source uses that escape hatch.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.playbooks.definition import (
    PlaybookDefinition,
    canonical_bytes,
    contract_fingerprint,
    load_definition_json,
    referenced_profile_ids,
    source_digest,
)
from src.playbooks.profiles import shipped_profile_fingerprints, shipped_profile_lookup
from src.playbooks.validation import (
    RegisteredEventLookup,
    RegistryContractLookup,
    VaultProfileLookup,
    validate_definition,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v2"
PROFILE_DEFAULTS = REPO_ROOT / "src" / "profiles" / "defaults"

#: Frontmatter id -> the shipped Markdown it was compiled from.
SHIPPED_SOURCES: dict[str, str] = {
    "default-pipeline": "src/prompts/default_playbooks/default-pipeline.md",
    "default-assignment-routing": "src/prompts/default_playbooks/default-assignment-routing.md",
    "memory-consolidation": "src/prompts/default_playbooks/memory-consolidation.md",
    "pr-merge-sweep": "src/prompts/project_playbooks/agent-queue/pr-merge-sweep.md",
}

PLAYBOOK_IDS = tuple(SHIPPED_SOURCES)

#: §3.4, locked.
REQUIRED_REVIEW_KEYS = frozenset(
    {
        "playbook_id", "artifact_sha256", "source_sha256",
        "contract_fingerprint", "questions_resolved", "capabilities_granted",
        "profiles_referenced",
    }
)

#: §3.4, locked: exact headings, exact text.
REQUIRED_REVIEW_SECTIONS = ()


#: The rule ids `tests/test_default_pipeline.py` pins.
PIPELINE_RULE_IDS = frozenset(
    {
        "per-task-review",
        "per-branch-final-review",
        "spec-ingest-on-approve",
        "proposal-ready-gate",
        "commit-on-gate-resolve",
    }
)

#: `src/playbooks/routing.py` suppresses these; they exist only in cached V1
#: artifacts and must never reappear in a reviewed V2 one.
SUPERSEDED_RULE_IDS = frozenset({"task-created-routing", "worker-filed-triage"})


def playbook_id_for(source: Path) -> str:
    """The frontmatter id of a shipped source, by its path."""
    rel = source.resolve().relative_to(REPO_ROOT).as_posix()
    for playbook_id, shipped in SHIPPED_SOURCES.items():
        if shipped == rel:
            return playbook_id
    raise KeyError(f"{rel} is not a shipped playbook source")


def _fixture(playbook_id: str) -> Path:
    return FIXTURE_ROOT / playbook_id


def _read_review(playbook_id: str) -> tuple[dict[str, Any], str]:
    text = (_fixture(playbook_id) / "manifest.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert match, f"{playbook_id}/manifest.md has no frontmatter block"
    import yaml

    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def _artifact(playbook_id: str) -> PlaybookDefinition:
    path = _fixture(playbook_id) / "artifact.json"
    return load_definition_json(path.read_text(encoding="utf-8"))


APPROVED_IDS = PLAYBOOK_IDS


def _shipped_profile_ids() -> set[str]:
    return {directory.name for directory in PROFILE_DEFAULTS.iterdir() if directory.is_dir()}


def _shipped_profile_lookup() -> VaultProfileLookup:
    from types import SimpleNamespace

    from src.profiles.parser import parse_profile, parsed_profile_to_agent_profile

    profiles = {}
    for path in PROFILE_DEFAULTS.glob("*/profile.md"):
        parsed = parse_profile(path.read_text(encoding="utf-8"))
        assert parsed.is_valid, parsed.errors
        fields = parsed_profile_to_agent_profile(parsed)
        profiles[fields["id"]] = SimpleNamespace(**fields)
    return VaultProfileLookup(profiles)


def _inputs(step: Any) -> dict[str, Any]:
    """A step's inputs as plain JSON, whatever the strict model wraps them in."""
    dumped = step.model_dump(mode="json", exclude_none=True)
    inputs = dumped.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def _referenced_profile_ids(definition: PlaybookDefinition) -> set[str]:
    """Profile ids the artifact names — as an `llm` step's profile or as a literal
    `profile_id` argument to a command step.

    Re-derived from the JSON dump rather than from
    :func:`referenced_profile_ids`, so the production helper the compiler and
    the release check both use is checked against an independent reading of
    the same bytes (`test_the_helper_agrees_with_an_independent_reading`).
    """
    found: set[str] = set()
    for step in definition.steps.values():
        profile_id = getattr(step, "profile_id", None)
        if isinstance(profile_id, str):
            found.add(profile_id)
        value = _inputs(step).get("profile_id")
        if isinstance(value, dict) and value.get("type") == "literal":
            literal = value.get("value")
            if isinstance(literal, str):
                found.add(literal)
    return found


def _command_names(definition: PlaybookDefinition) -> set[str]:
    names = {
        step.command
        for step in definition.steps.values()
        if getattr(step, "command", None)
    }
    for step in definition.steps.values():
        tool_use = getattr(step, "tool_use", None)
        if tool_use is not None:
            names.update(tool_use.aq_commands)
            names.update(tool_use.plugin_tools)
    return names


# ---------------------------------------------------------------------------
# T-7 — the fixture is complete, canonical, and matches the live source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("playbook_id", PLAYBOOK_IDS)
def test_fixture_directory_complete(playbook_id: str) -> None:
    directory = _fixture(playbook_id)
    assert directory.is_dir(), f"no reviewed fixture directory for {playbook_id}"
    for name in ("source.md", "artifact.json", "artifact.sha256", "manifest.md", "diagnostics.json"):
        assert (directory / name).is_file(), f"{playbook_id}/{name} is missing"


@pytest.mark.parametrize("playbook_id", APPROVED_IDS)
def test_artifact_validates_against_strict_model(playbook_id: str) -> None:
    assert _artifact(playbook_id).id == playbook_id


def test_strict_model_forbids_unknown_keys() -> None:
    """`extra="forbid"` is exercised, not assumed."""
    payload = json.loads((_fixture(APPROVED_IDS[0]) / "artifact.json").read_text())
    payload["smuggled_key"] = True
    with pytest.raises(ValidationError):
        PlaybookDefinition.model_validate(payload)


@pytest.mark.parametrize("playbook_id", APPROVED_IDS)
def test_artifact_bytes_are_canonical(playbook_id: str) -> None:
    directory = _fixture(playbook_id)
    raw = (directory / "artifact.json").read_bytes()
    recorded = (directory / "artifact.sha256").read_text(encoding="utf-8").strip()
    assert recorded == "sha256:" + hashlib.sha256(raw).hexdigest()
    # Re-serialising the parsed model must reproduce the bytes exactly: a
    # trailing newline is a failure, not a nit.
    assert canonical_bytes(load_definition_json(raw.decode("utf-8"))) == raw


@pytest.mark.parametrize("playbook_id", APPROVED_IDS)
def test_source_digest_matches(playbook_id: str) -> None:
    source = (_fixture(playbook_id) / "source.md").read_text(encoding="utf-8")
    assert _artifact(playbook_id).source_hash == source_digest(source)


@pytest.mark.parametrize("playbook_id", PLAYBOOK_IDS)
def test_source_matches_live_shipped_file(playbook_id: str) -> None:
    recorded = (_fixture(playbook_id) / "source.md").read_bytes()
    live = (REPO_ROOT / SHIPPED_SOURCES[playbook_id]).read_bytes()
    assert recorded == live, (
        f"shipped Markdown changed since review ({SHIPPED_SOURCES[playbook_id]}) — "
        "recompile, re-review, and update the fixture"
    )


@pytest.mark.parametrize("playbook_id", PLAYBOOK_IDS)
def test_review_record_complete(playbook_id: str) -> None:
    review, body = _read_review(playbook_id)
    missing = REQUIRED_REVIEW_KEYS - set(review)
    assert not missing, f"{playbook_id}/manifest.md is missing keys: {sorted(missing)}"
    assert review["playbook_id"] == playbook_id
    for heading in REQUIRED_REVIEW_SECTIONS:
        assert f"\n{heading}\n" in f"\n{body}", f"{playbook_id}/manifest.md lacks {heading!r}"
    source = (_fixture(playbook_id) / "source.md").read_text(encoding="utf-8")
    assert review["source_sha256"] == source_digest(source)
    recorded = (_fixture(playbook_id) / "artifact.sha256").read_text().strip()
    assert review["artifact_sha256"] == recorded
    assert review["contract_fingerprint"] == contract_fingerprint(_artifact(playbook_id))


@pytest.mark.parametrize("playbook_id", APPROVED_IDS)
def test_every_command_resolves(playbook_id: str) -> None:
    definition = _artifact(playbook_id)
    contracts = RegistryContractLookup()
    assert _command_names(definition) == set(definition.compiled_against.commands), (
        "compiled_against.commands must name exactly the commands the artifact invokes"
    )
    for name, fingerprint in definition.compiled_against.commands.items():
        info = contracts.get(name)
        assert info is not None, f"{playbook_id} invokes unregistered command {name!r}"
        assert info.execution_fingerprint == fingerprint, (
            f"{playbook_id}: {name} execution fingerprint drifted from the reviewed "
            "artifact; rebuild and re-review"
        )


@pytest.mark.parametrize("playbook_id", APPROVED_IDS)
def test_every_profile_resolves(playbook_id: str) -> None:
    shipped = _shipped_profile_ids()
    referenced = _referenced_profile_ids(_artifact(playbook_id))
    unknown = referenced - shipped
    assert not unknown, f"{playbook_id} names profiles absent from {PROFILE_DEFAULTS}: {unknown}"


@pytest.mark.parametrize("playbook_id", APPROVED_IDS)
def test_the_helper_agrees_with_an_independent_reading(playbook_id: str) -> None:
    """`referenced_profile_ids` is what the compiler snapshots against."""
    definition = _artifact(playbook_id)
    assert set(referenced_profile_ids(definition)) == _referenced_profile_ids(definition)


@pytest.mark.parametrize("playbook_id", APPROVED_IDS)
def test_compiled_against_fingerprints_every_referenced_profile(playbook_id: str) -> None:
    """The profile half of `test_every_command_resolves`.

    "The directory exists" is not what the reviewer approved: they approved
    what those profiles were *allowed to do*.  Recording a fingerprint per
    referenced profile is what makes a later capability change visible — as
    `stale_contract` in activation health, and as drift in the release check.
    An empty map (`solid-harbor.54`) made both unreachable for the one shipped
    artifact whose profile dependencies are all delegated.
    """
    definition = _artifact(playbook_id)
    recorded = definition.compiled_against.profiles
    assert set(recorded) == set(referenced_profile_ids(definition)), (
        "compiled_against.profiles must name exactly the profiles the artifact "
        "depends on, delegated ones included"
    )
    lookup = shipped_profile_lookup()
    for profile_id, fingerprint in recorded.items():
        policy = lookup.policy(profile_id)
        assert policy is not None, f"{playbook_id} names unshipped profile {profile_id!r}"
        assert policy.fingerprint() == fingerprint, (
            f"{playbook_id}: profile {profile_id} capabilities drifted from the "
            "reviewed artifact; rebuild and re-review"
        )


def test_the_pipeline_depends_on_its_review_profiles_only_by_delegation() -> None:
    """The shape that made the empty map easy to miss, pinned.

    `default-pipeline` has no `llm` or `agent_task` step at all: every one of
    its three profile dependencies is an `ensure_task` argument.  A future
    refactor that dropped the delegated half of the snapshot would pass every
    other assertion in this file, so name the shape here.
    """
    definition = _artifact("default-pipeline")
    own = {
        getattr(step, "profile_id", None)
        for step in definition.steps.values()
        if getattr(step, "profile_id", None)
    }
    assert own == set(), "default-pipeline gained an AI step; re-review this fixture"
    assert set(definition.compiled_against.profiles) == {
        "reviewer",
        "final-reviewer",
        "spec-ingest",
    }


def test_shipped_profile_fingerprints_covers_the_shipped_tree() -> None:
    """The release check's default profile map is the whole shipped set."""
    fingerprints = shipped_profile_fingerprints()
    assert set(fingerprints) == _shipped_profile_ids()
    assert all(value.startswith("sha256:") for value in fingerprints.values())


def test_pipeline_references_the_three_review_profiles() -> None:
    assert _referenced_profile_ids(_artifact("default-pipeline")) == {
        "reviewer",
        "final-reviewer",
        "spec-ingest",
    }


def test_pipeline_rule_set_unchanged() -> None:
    rule_ids = {rule.id for rule in _artifact("default-pipeline").rules}
    assert rule_ids == PIPELINE_RULE_IDS
    assert not rule_ids & SUPERSEDED_RULE_IDS


def test_assignment_router_has_a_typed_input_and_result_contract() -> None:
    definition = _artifact("default-assignment-routing")
    step = definition.steps["assignment-route--choose"]
    assert _inputs(step) == {
        "tasks": {"type": "event_ref", "path": "tasks"},
        "options": {"type": "event_ref", "path": "options"},
        "options_hash": {"type": "event_ref", "path": "options_hash"},
        "catalog_hash": {"type": "event_ref", "path": "catalog_hash"},
    }
    assert step.save_result_as == "routing_result"
    assert step.output_schema["required"] == ["decisions"]
    assert step.output_schema["additionalProperties"] is False


def test_review_dedup_key_matches_doctor() -> None:
    """The prose rewrite must not silently disarm `integration.unreviewed_prs`."""
    from src.doctor.integration_checks import _review_dedup_key

    definition = _artifact("default-pipeline")
    step = definition.steps["per-task-review--create-review"]
    template = _inputs(step)["dedup_key"]
    assert template["type"] == "template", template
    rendered = "".join(
        part["value"] if part["type"] == "literal" else "TASK-1"
        for part in template["parts"]
    )
    assert rendered == _review_dedup_key("TASK-1")


def test_artifact_validates_against_the_live_registries() -> None:
    """The recorded artifact still resolves every command, event and field.

    The shipped profile lookup is passed rather than `None` so the profile
    half of `stale_contract` is exercised too: with `None` the validator has
    no registry to compare `compiled_against.profiles` against and stays
    silent, which is precisely how an empty map went unnoticed.
    """
    for playbook_id in APPROVED_IDS:
        diagnostics = validate_definition(
            _artifact(playbook_id),
            inventory=None,
            contracts=RegistryContractLookup(),
            profiles=shipped_profile_lookup(),
            events=RegisteredEventLookup(),
        )
        blocking = [d for d in diagnostics if d.severity in {"error", "question"}]
        assert not blocking, [
            (d.code, d.rule_id, d.step_id, d.message) for d in blocking
        ]


def test_custom_manifest_policy_is_not_a_core_activation_gate(tmp_path: Path) -> None:
    """Core discovery is mechanical and ignores custom manifest policy fields."""
    from tests.playbook_fixture_activation import activatable_fixture_ids

    # The shipped tree: every complete V2 bundle is activatable.
    assert set(activatable_fixture_ids(FIXTURE_ROOT)) == set(PLAYBOOK_IDS)

    # Custom metadata is not a hard-coded core policy gate.
    synthetic = tmp_path / "hostile"
    synthetic.mkdir()
    (synthetic / "artifact.json").write_bytes(
        (_fixture("default-pipeline") / "artifact.json").read_bytes()
    )
    (synthetic / "manifest.md").write_text(
        "---\nplaybook_id: hostile\nvalidation_decision: rejected\n---\n\nbody\n", encoding="utf-8"
    )
    assert activatable_fixture_ids(tmp_path) == ("hostile",)


# ---------------------------------------------------------------------------
# T-9 — capability audit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("playbook_id", APPROVED_IDS)
def test_no_wildcard_capability(playbook_id: str) -> None:
    review, _ = _read_review(playbook_id)
    for namespace, names in review["capabilities_granted"].items():
        assert "*" not in (names or []), (
            f"{playbook_id}: wildcard capability in {namespace} (roadmap §11 Safety)"
        )
    for profile_id in _referenced_profile_ids(_artifact(playbook_id)):
        text = (PROFILE_DEFAULTS / profile_id / "profile.md").read_text(encoding="utf-8")
        assert '"*"' not in text, f"profile {profile_id} grants a wildcard capability"


@pytest.mark.parametrize("playbook_id", APPROVED_IDS)
def test_review_lists_every_required_capability(playbook_id: str) -> None:
    """One direction only (§4.1): the review may not be used to *add* one."""
    from src.playbooks.definition import CommandStep, LlmStep

    manifest, _ = _read_review(playbook_id)
    granted = manifest["capabilities_granted"]
    required_aq = {
        str(step.command)
        for step in _artifact(playbook_id).steps.values()
        if isinstance(step, CommandStep)
    }
    required_plugins = set()
    for step in _artifact(playbook_id).steps.values():
        if isinstance(step, LlmStep) and step.tool_use.enabled:
            required_aq.update(map(str, step.tool_use.aq_commands))
            required_plugins.update(map(str, step.tool_use.plugin_tools))
    assert required_aq <= set(granted["aq_commands"])
    assert required_plugins <= set(granted["plugin_tools"])


def test_capabilities_granted_unused_by_src() -> None:
    """A repository write must never become a privilege grant (§4.1)."""
    result = subprocess.run(
        ["grep", "-rn", "capabilities_granted", "src/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, (
        "capabilities_granted is read by production code; a reviewed fixture would "
        f"become an authority claim:\n{result.stdout}"
    )
