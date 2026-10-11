"""Portable allow-list policy, checked copies and review-preserving command flow."""

from __future__ import annotations

import copy
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from src.event_bus import EventBus
from src.models import Project, RepoConfig, RepoSourceType, Workspace
from src.database.tables import projects
from src.policy_profiles.archive import files, pack, read_bundle, write_bundle
from src.policy_profiles.models import Bundle, PlaybookPolicy, PromotionPolicy, TemplatePolicy
from src.policy_profiles.service import item

MORNING = Path("tests/fixtures/playbooks/v2/morning-report")
FLOW = [{"id": "release", "source": "main", "target": "stable", "type": "request"}]


def put(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
async def env(command_handler_factory, tmp_path):
    handler = await command_handler_factory()
    handler.config.playbooks.enabled = True
    handler.orchestrator.bus = EventBus(env="dev")
    for pid in ("source", "target"):
        await handler.db.create_project(
            Project(id=pid, name=pid.title(), repo_url=f"https://github.com/acme/{pid}.git")
        )
        workspace = tmp_path / pid
        workspace.mkdir()
        await handler.db.create_workspace(
            Workspace(
                id=pid,
                project_id=pid,
                workspace_path=str(workspace),
                source_type=RepoSourceType.LINK,
            )
        )
    try:
        yield handler, Path(handler.config.vault_root), tmp_path
    finally:
        await handler.db.close()


async def reviewed(handler, vault, identifier="morning-report"):
    source = (MORNING / "source.md").read_text().replace("morning-report", identifier)
    put(vault, f"system/playbooks/{identifier}.md", source)
    artifact = json.loads((MORNING / "artifact.json").read_text())
    submitted = await handler.execute(
        "review_submit",
        {
            "project_id": "source",
            "title": identifier,
            "content": source,
            "playbook_id": identifier,
            "activate_on_approval": True,
            "semantic_body": json.dumps({"rules": artifact["rules"], "steps": artifact["steps"]}),
        },
    )
    assert submitted["success"], submitted
    approved = await handler.execute(
        "review_decide", {"review_id": submitted["review_id"], "revision": 1, "decision": "approve"}
    )
    assert approved["success"] and approved["playbook"]["activated"], approved


async def legacy_source_path(handler, identifier):
    from src.playbooks.artifact_store import ArtifactStore

    row = next(
        row for row in await handler.db.list_playbook_activations()
        if row["playbook_id"] == identifier
    )
    store = ArtifactStore(handler.config.compiled_root)
    return store.source_path_for(row["active_artifact_sha256"])


@pytest.mark.parametrize("retained", ["missing", "invalid"])
async def test_export_recovers_matching_vault_source_without_writing_during_preview(env, retained):
    handler, vault, _ = env
    await reviewed(handler, vault, "legacy-vault")
    path = await legacy_source_path(handler, "legacy-vault")
    if retained == "missing":
        path.unlink()
    else:
        path.write_text("corrupted retained source")
    exported = await handler.execute("policy_export", {"project_id": "source"})
    assert exported["success"] and exported["warnings"] == [], exported
    entry = next(row for row in exported["bundle"]["items"] if row["name"] == "legacy-vault")
    assert entry["payload"]["source"] == (vault / "system/playbooks/legacy-vault.md").read_text()
    assert not path.exists() if retained == "missing" else path.read_text() == "corrupted retained source"


@pytest.mark.parametrize("vault_state", ["changed", "missing"])
async def test_export_omits_unavailable_source_with_per_item_warning_and_keeps_other_policy(env, vault_state):
    handler, vault, _ = env
    await reviewed(handler, vault, "lost-source")
    await reviewed(handler, vault, "healthy-source")
    (await legacy_source_path(handler, "lost-source")).unlink()
    source = vault / "system/playbooks/lost-source.md"
    if vault_state == "changed":
        source.write_text(source.read_text() + "\nChanged behavior\n")
    else:
        source.unlink()
    put(vault / "projects/source", "templates/spec/example.md", "# Example")
    result = await handler.execute("policy_export", {"project_id": "source"})
    assert result["success"], result
    names = {entry["name"] for entry in result["bundle"]["items"]}
    assert "lost-source" not in names and {"healthy-source", "spec.example"} <= names
    assert result["warnings"] == [{
        "item_id": "playbook:system:lost-source",
        "playbook_id": "lost-source",
        "message": "Skipped lost-source: source unavailable, recompile and reactivate lost-source",
    }]
    assert read_bundle(pack(Bundle.model_validate(result["bundle"]))).items


async def test_export_prefers_retained_source_when_vault_has_changed(env):
    handler, vault, _ = env
    await reviewed(handler, vault, "retained-source")
    retained = (await legacy_source_path(handler, "retained-source")).read_text()
    (vault / "system/playbooks/retained-source.md").write_text("changed vault source")
    result = await handler.execute("policy_export", {"project_id": "source"})
    assert result["success"] and not result["warnings"], result
    entry = next(row for row in result["bundle"]["items"] if row["name"] == "retained-source")
    assert entry["payload"]["source"] == retained


async def test_export_missing_compiled_artifact_returns_contextual_error(env):
    handler, vault, _ = env
    await reviewed(handler, vault, "missing-artifact")
    (await legacy_source_path(handler, "missing-artifact")).with_suffix(".json").unlink()
    result = await handler.execute("policy_export", {"project_id": "source"})
    assert not result["success"]
    assert "missing-artifact" in result["error"] and "recompile and reactivate" in result["error"]
    assert "Errno" not in result["error"] and handler.config.compiled_root not in result["error"]


async def test_export_disappearing_policy_file_never_surfaces_raw_filesystem_error(env, monkeypatch):
    from src.policy_profiles.service import PolicyFiles

    handler, _, _ = env

    def disappeared(_adapter):
        raise FileNotFoundError(2, "No such file or directory", "/private/path.md")

    monkeypatch.setattr(PolicyFiles, "read_local", disappeared)
    result = await handler.execute("policy_export", {"project_id": "source"})
    assert not result["success"] and "preview again" in result["error"]
    assert "Errno" not in result["error"] and "/private/path" not in result["error"]


async def test_source_doctor_reports_enabled_missing_and_invalid_sources_without_repair(env):
    from src.doctor import default_registry
    from src.doctor.models import DoctorContext, Severity
    from src.doctor.playbook_v2_checks import SOURCE_CHECK_ID, _check_source_unavailable

    handler, vault, _ = env
    await reviewed(handler, vault, "doctor-source")
    ctx = DoctorContext(config=handler.config, db=handler.db)
    assert (await _check_source_unavailable(ctx)).severity is Severity.OK
    path = await legacy_source_path(handler, "doctor-source")
    original = path.read_text()
    path.unlink()
    result = await _check_source_unavailable(ctx)
    assert result.severity is Severity.WARN and result.data["count"] == 1
    assert result.data["faults"][0]["problem"] == "source_missing"
    assert result.data["faults"][0]["remediation"] == "recompile and reactivate doctor-source"
    assert not path.exists() and not result.fixable
    path.write_text("wrong source")
    assert (await _check_source_unavailable(ctx)).data["faults"][0]["problem"] == "source_invalid"
    path.write_text(original)
    assert (await _check_source_unavailable(ctx)).severity is Severity.OK
    handler.config.playbooks.enabled = False
    assert (await _check_source_unavailable(ctx)).severity is Severity.INFO
    check = next(row for row in default_registry().checks() if row.id == SOURCE_CHECK_ID)
    assert check.fix is None and check.owner == "playbook-v2"


async def preview(handler, bundle, **extra):
    args = {"project_id": "target", "bundle": bundle, **extra}
    data = await handler.execute("policy_diff", args)
    assert data["success"], data
    return args, data


def acknowledge(args, data, *, scope="project", overwrite=False):
    args["selections"] = {
        row["id"]: {
            "scope": scope,
            "overwrite": overwrite,
            "expected_checksum": row["current_checksum"],
        }
        for row in data["items"]
    }


def simple_bundle(*entries, placeholders=()):
    return Bundle(
        name="Example",
        source_aq_version="test",
        items=list(entries),
        placeholders=list(placeholders),
    )


async def train_pair(handler):
    """Two independent repository bindings with the recorded AQ train policy."""
    from src.integration.models import HierarchicalIntegrationPolicy
    from types import SimpleNamespace

    handler.config.integration.git_first = "active"
    handler.orchestrator.integration_service = SimpleNamespace(delivery_modes=lambda: {"train"})
    policies = {}
    for pid in ("source", "target"):
        policy = json.loads(Path("docs/config/agent-queue-train-policy.json").read_text())
        policy["train"] = {"cadence_seconds": 120 if pid == "source" else 600,
                           "settling_cap_seconds": 900}
        for name in ("parent", "root"):
            boundary = policy[name]
            boundary["route"]["scope_identifier"] = pid
            boundary["required_checks"] = {
                "version": pid + "-v1", "producer_id": "123" if pid == "source" else "456",
                "names": [pid + "-lint", pid + "-test"] if pid == "source" else ["cpp-test"],
            }
        policies[pid] = HierarchicalIntegrationPolicy.model_validate(policy).model_dump(mode="json")
        await handler.db.create_repo(RepoConfig(
            id=pid + "-repo", project_id=pid, source_type=RepoSourceType.CLONE,
            url=f"https://github.com/acme/{pid}.git", default_branch="main",
        ))
        await handler.db.update_project(
            pid, integration_mode="pull_request", integration_repository_id=pid + "-repo",
            hierarchical_integration_mode="train" if pid == "source" else "observe",
            hierarchical_integration_policy=policies[pid],
        )
    return policies


async def integration_preview(handler, **extra):
    exported = await handler.execute("policy_export", {"project_id": "source"})
    assert exported["success"], exported
    bundle = read_bundle(pack(Bundle.model_validate(exported["bundle"])))
    args, data = await preview(handler, bundle.model_dump(mode="json"),
                               only=["integration_settings:project:integration"], **extra)
    acknowledge(args, data, overwrite=True)
    return args, data


async def test_export_apply_train_settings_preserves_target_bindings_and_is_idempotent(env):
    handler, _, _ = env
    policies = await train_pair(handler)
    before_activations = await handler.db.list_playbook_activations()
    args, data = await integration_preview(handler)
    entry = next(e for e in args["bundle"]["items"] if e["type"] == "integration_settings")
    assert entry["payload"]["version"] == 1
    text = json.dumps(entry)
    assert "source-repo" not in text and "source-lint" not in text
    assert "artifact_sha256" not in text and "steady-delta" not in text
    assert "{{integration_repository}}" in text
    assert "{{integration_root_checks_names}}" in text
    row = next(r for r in data["items"] if r["type"] == "integration_settings")
    assert row["state"] == "configure integration" and not row["selected"]
    result = await handler.execute("policy_apply", args)
    assert result["success"], result
    assert result["configured_integration"] == [entry["id"]]
    target = await handler.db.get_project("target")
    assert target.integration_repository_id == "target-repo"
    assert target.hierarchical_integration_mode == "train"
    assert target.hierarchical_integration_generation == 1
    expected = copy.deepcopy(policies["source"])
    for name in ("parent", "root"):
        for field in ("route", "required_checks", "authorized_task_ids"):
            expected[name][field] = policies["target"][name][field]
    assert target.hierarchical_integration_policy == expected
    assert await handler.db.list_playbook_activations() == before_activations
    again = await handler.execute("policy_diff", args)
    assert again["success"], again
    assert next(r for r in again["items"] if r["id"] == entry["id"])["status"] == "identical"
    acknowledge(args, again)
    repeated = await handler.execute("policy_apply", args)
    assert repeated["success"] and not repeated["configured_integration"], repeated
    assert (await handler.db.get_project("target")).hierarchical_integration_generation == 1


async def test_integration_import_refuses_stale_live_generation_before_other_writes(env):
    handler, vault, _ = env
    await train_pair(handler)
    args, _ = await integration_preview(handler)
    changed = await handler.execute("edit_project", {
        "project_id": "target", "hierarchical_integration_mode": "observe",
        "expected_integration_generation": 0,
    })
    assert changed["outcome"] == "configured", changed
    result = await handler.execute("policy_apply", args)
    assert not result["success"] and "preview changed" in result["error"], result
    assert not (vault / "projects/target/policy").exists()
    assert (await handler.db.get_project("target")).hierarchical_integration_mode == "observe"


@pytest.mark.parametrize("problem", ["route", "engine", "repository", "policy"])
async def test_integration_import_uses_supported_validation_without_fabricating_routes(env, problem):
    handler, vault, _ = env
    await train_pair(handler)
    if problem == "route":
        await handler.db.update_project("target", hierarchical_integration_policy=None)
    extra = {}
    if problem == "route":
        extra["values"] = {f"integration_{name}_checks_{field}": value
                           for name in ("parent", "root")
                           for field, value in (("names", '["cpp-test"]'), ("version", "v1"),
                                                ("producer_id", "456"))}
    args, _ = await integration_preview(handler, **extra)
    entry = next(e for e in args["bundle"]["items"] if e["type"] == "integration_settings")
    if problem == "engine":
        entry["payload"]["engine"] = "shadow"
    elif problem == "repository":
        args["values"] = {"integration_repository": "source-repo"}
    elif problem == "policy":
        entry["payload"]["policy"]["train"]["cadence_seconds"] = -1
    from src.policy_profiles.models import checksum

    entry["checksum"] = checksum(entry["payload"])
    result = await handler.execute("policy_apply", args)
    assert not result["success"], result
    assert not (vault / "projects/target/policy").exists()
    assert (await handler.db.get_project("target")).hierarchical_integration_generation == 0
    if problem == "route":
        assert "cannot fabricate" in result["error"]


async def test_integration_global_template_and_skip_do_not_configure_or_need_unused_values(env):
    handler, vault, _ = env
    await train_pair(handler)
    args, data = await integration_preview(handler)
    args["selections"]["integration_settings:project:integration"]["scope"] = "skip"
    await handler.db.update_project("target", hierarchical_integration_policy=None)
    result = await handler.execute("policy_apply", args)
    assert result["success"] and not result["configured_integration"], result
    assert (await handler.db.get_project("target")).hierarchical_integration_generation == 0
    # A reusable global template requires filled check placeholders, but changes no project row.
    args["values"] = data["values"]
    args["selections"] = {"integration_settings:project:integration": {"scope": "global"}}
    result = await handler.execute("policy_apply", args)
    assert result["success"] and not result["configured_integration"], result
    assert (vault / "system/policy/integration_settings/integration.json").is_file()


async def test_integration_flow_validates_target_trust_and_preserves_local_operators(env):
    from unittest.mock import AsyncMock
    from src.integration.promotion_steps import read_stored_promotion_flow
    from tests.test_promotion_flow import DEV, FakeGit, TRUST

    handler, _, _ = env
    await train_pair(handler)
    for pid in ("source", "target"):
        flow = [{**FLOW[0], "gate": {"operator_logins": [pid + "-operator"]}}]
        async with handler.db._engine.begin() as conn:
            await conn.execute(update(projects).where(projects.c.id == pid).values(
                promotion_flow=flow))
    args, _ = await integration_preview(handler)
    assert "source-operator" not in json.dumps(args["bundle"])
    args["values"] = {placeholder["name"]: "stable"
                      for placeholder in args["bundle"]["placeholders"]
                      if placeholder["name"].startswith("branch_")}
    handler.orchestrator.git = FakeGit({"main": DEV, "stable": DEV})
    handler._promotion_manifest = AsyncMock(side_effect=RuntimeError("trust unavailable"))
    refused = await handler.execute("policy_apply", args)
    assert not refused["success"] and refused["error"] == "trust_manifest_unavailable", refused
    assert (await handler.db.get_project("target")).hierarchical_integration_generation == 0
    handler._promotion_manifest.side_effect = None
    handler._promotion_manifest.return_value = TRUST
    applied = await handler.execute("policy_apply", args)
    assert applied["success"] and applied["configured_integration"], applied
    flow = await read_stored_promotion_flow(handler.db, "target")
    assert flow[0]["target"] == "stable"
    assert flow[0]["gate"]["operator_logins"] == ["target-operator"]


async def test_integration_receipt_records_configuration_if_later_file_write_fails(env, monkeypatch):
    handler, _, _ = env
    await train_pair(handler)
    args, _ = await integration_preview(handler)

    def fail_write(_planned):
        raise OSError("destination read-only")

    monkeypatch.setattr("src.commands.policy_commands.apply_files", fail_write)
    result = await handler.execute("policy_apply", args)
    assert not result["success"] and result["error"] == "destination read-only"
    assert result["configured_integration"] == ["integration_settings:project:integration"]
    assert (await handler.db.get_project("target")).hierarchical_integration_generation == 1


async def test_disabled_integration_without_policy_retains_target_policy_and_is_idempotent(env):
    handler, _, _ = env
    policies = await train_pair(handler)
    await handler.db.update_project("source", hierarchical_integration_mode="disabled",
                                    hierarchical_integration_policy=None)
    args, _ = await integration_preview(handler)
    result = await handler.execute("policy_apply", args)
    assert result["success"], result
    target = await handler.db.get_project("target")
    assert target.hierarchical_integration_mode == "disabled"
    assert target.hierarchical_integration_policy == policies["target"]
    again = await handler.execute("policy_diff", args)
    assert again["success"], again
    assert next(row for row in again["items"] if row["type"] == "integration_settings")[
        "status"] == "identical"


def test_playbook_machine_paths_are_portable_in_sources_and_artifacts(tmp_path):
    from src.playbooks.definition import source_digest
    from src.policy_profiles.service import PolicyFiles

    source_root, target_root = tmp_path / "source", tmp_path / "target"
    source = PolicyFiles(source_root / "vault", Project(id="source", name="Source"),
                         str(source_root / "workspace"))
    target = PolicyFiles(target_root / "vault", Project(id="target", name="Target"),
                         str(target_root / "workspace"))
    raw = (MORNING / "source.md").read_text()
    raw += f"\nUse {source.vault}/system/playbooks and {source.workspace}/scripts/graft.py.\n"
    raw += "Run `/opt/private-tools/graft.py` afterwards.\n"
    artifact = json.loads((MORNING / "artifact.json").read_text())
    artifact["source_hash"] = source_digest(raw)
    bundle = source.portable([item("morning-report", "system", PlaybookPolicy(
        source=raw, artifact=artifact))], None)
    serialized = bundle.model_dump_json()
    assert str(source_root) not in serialized and "/opt/private-tools" not in serialized
    assert "{{vault_root}}" in serialized and "{{workspace}}" in serialized
    unknown = next(p.name for p in bundle.placeholders if p.name.startswith("path_"))
    with pytest.raises(ValueError, match="fill placeholders"):
        target.materialize(bundle, {})
    imported = target.materialize(bundle, {unknown: "/usr/local/bin/graft.py"})[0]
    assert str(target.vault) + "/system/playbooks" in imported.payload.source
    assert str(target.workspace) + "/scripts/graft.py" in imported.payload.source
    assert "/usr/local/bin/graft.py" in imported.payload.source
    assert imported.payload.artifact["source_hash"] == source_digest(imported.payload.source)


async def test_allowlist_roundtrip_all_six_types_excludes_personal_state_and_activation(env):
    handler, vault, tmp_path = env
    await reviewed(handler, vault)
    source = vault / "projects/source"
    put(
        source,
        "agent-types/reviewer/profile.md",
        """---
id: reviewer
name: Reviewer
---
## Config
```json
{"harness":"codex","default_class":"deep-high","provider":"PERSONAL-PROVIDER","env":{"API_KEY":"SECRET"}}
```
## Role
Review source in https://github.com/acme/source.git.
## Rules
Use the project source workspace.
""",
    )
    put(source, "overrides/reviewer.md", "Project source reviewer rules\n")
    put(source, "templates/spec.base.md", "unowned file ignored")
    put(
        source,
        "templates/spec/base.md",
        "# Spec for source\nWorkspace: " + str(tmp_path / "source"),
    )
    put(source, "templates/plan/base.md", "# Plan for source\n")
    put(
        source,
        "formulas/base.md",
        """---
name: base
---
```aq-graph
version: 1
parent:
  title: Example
nodes:
  - key: implement
    title: Implement
    description: Implement the plan
```
""",
    )
    put(
        tmp_path / "source",
        "tests/selection_rules.yaml",
        "version: 1\nnon_behavioral: [docs/**]\nownership: []\n",
    )
    put(
        tmp_path / "source",
        ".github/agent-queue-integration.json",
        json.dumps(
            {
                "required_checks": {"version": "v1", "names": ["tests"]},
                "github_app_id": "PERSONAL-APP",
                "repository_id": 123,
            }
        ),
    )
    for path in (
        "memory/private.md",
        "notes/report.md",
        "research/book.md",
        "reviews/rev.md",
        "secrets/token.json",
        "policy/unknown/private.json",
        "agent-types/reviewer/memory.md",
    ):
        put(source, path, "SECRET-PERSONAL")
    async with handler.db._engine.begin() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "source").values(promotion_flow=FLOW)
        )
    result = await handler.execute("policy_export", {"project_id": "source"})
    assert result["success"], result
    bundle = result["bundle"]
    assert {entry["type"] for entry in bundle["items"]} == {
        "playbook",
        "agent_settings",
        "promotion_flow",
        "ci_test",
        "routing",
        "template",
    }
    text = json.dumps(bundle)
    assert "SECRET" not in text and "PERSONAL" not in text and "github_app_id" not in text
    assert "https://github.com/acme/source.git" not in text and str(tmp_path / "source") not in text
    assert "{{project_id}}" in text and "{{repo}}" in text and "{{workspace}}" in text
    assert (
        next(entry for entry in bundle["items"] if entry["type"] == "playbook")["original_scope"]
        == "system"
    )
    assert len([entry for entry in bundle["items"] if entry["type"] == "agent_settings"]) == 2
    values = {
        p["name"]: "release" for p in bundle["placeholders"] if p["name"].startswith("branch_")
    }
    args, data = await preview(handler, bundle, values=values)
    assert not next(row for row in data["items"] if row["type"] == "playbook")["selected"]
    assert (
        next(row for row in data["items"] if row["type"] == "promotion_flow")["state"]
        == "pending configuration"
    )
    acknowledge(args, data)
    applied = await handler.execute("policy_apply", args)
    assert applied["success"], applied
    assert not applied["activated"] and len(applied["reviews"]) == 1
    assert (
        vault / "projects/target/templates/spec/base.md"
    ).read_text() == "# Spec for target\nWorkspace: " + str(tmp_path / "target")
    saved = json.loads((vault / "projects/target/policy/promotion_flow/flow.json").read_text())
    assert saved["flow"] == [{**FLOW[0], "target": "release"}]
    async with handler.db._engine.connect() as conn:
        assert (
            await conn.execute(select(projects.c.promotion_flow).where(projects.c.id == "target"))
        ).scalar_one() is None
    assert not any(
        row["playbook_id"].startswith("target.")
        for row in await handler.db.list_playbook_activations()
    )
    copied = await handler.execute("policy_export", {"project_id": "target"})
    assert copied["success"], copied
    checks = next(
        entry["payload"] for entry in copied["bundle"]["items"] if entry["type"] == "ci_test"
    )
    assert checks["ci"]["required_checks"] == {"version": "v1", "names": ["tests"]}
    assert checks["selection_rules"]["non_behavioral"] == ["docs/**"]
    review = await handler.db.get_review_revision(applied["reviews"][0]["review_id"], 1)
    assert review["playbook"]["playbook_id"] == "target.morning-report"
    assert not review["playbook"]["activate_on_approval"]
    approved = await handler.execute(
        "review_decide",
        {"review_id": applied["reviews"][0]["review_id"], "revision": 1, "decision": "approve"},
    )
    assert (
        approved["success"]
        and approved["playbook"]["stored"]
        and not approved["playbook"]["activated"]
    )
    _, again = await preview(handler, bundle, values=values, selections=args["selections"])
    assert all(row["status"] == "identical" for row in again["items"])
    acknowledge(args, again)
    repeated = await handler.execute("policy_apply", args)
    assert (
        repeated["success"]
        and not repeated["applied"]
        and repeated["reviews"] == applied["reviews"]
    )


@pytest.mark.parametrize("scope", ["project", "global"])
async def test_selection_overwrite_optin_no_overwrite_and_preview_fence(env, scope):
    handler, vault, _ = env
    entry = item("spec.base", "project", TemplatePolicy(kind="spec", content="first"))
    bundle = simple_bundle(entry).model_dump(mode="json")
    args, data = await preview(handler, bundle, selections={entry.id: {"scope": scope}})
    acknowledge(args, data, scope=scope)
    assert (await handler.execute("policy_apply", args))["success"]
    root = vault / ("projects/target" if scope == "project" else "system")
    changed = simple_bundle(
        item("spec.base", "project", TemplatePolicy(kind="spec", content="second"))
    ).model_dump(mode="json")
    args, data = await preview(handler, changed, selections={entry.id: {"scope": scope}})
    assert data["items"][0]["status"] == "will overwrite" and not data["items"][0]["selected"]
    assert "first" in data["items"][0]["diff"] and "second" in data["items"][0]["diff"]
    acknowledge(args, data, scope=scope, overwrite=True)
    result = await handler.execute("policy_apply", {**args, "no_overwrite": True})
    assert result["success"] and not result["applied"]
    (root / "templates/spec/base.md").write_text("external change")
    refused = await handler.execute("policy_apply", args)
    assert not refused["success"] and "preview changed" in refused["error"]
    assert (root / "templates/spec/base.md").read_text() == "external change"
    args, data = await preview(handler, changed, selections={entry.id: {"scope": scope}})
    acknowledge(args, data, scope=scope, overwrite=True)
    assert (await handler.execute("policy_apply", args))["success"]
    assert (root / "templates/spec/base.md").read_text() == "second"


async def test_only_skip_system_explicit_scopes_and_missing_placeholders(env):
    handler, vault, _ = env
    entry = item("spec.base", "system", TemplatePolicy(kind="spec", content="{{custom_path}}"))
    other = item("plan.base", "project", TemplatePolicy(kind="plan", content="skip me"))
    bundle = simple_bundle(
        entry, other, placeholders=[{"name": "custom_path", "description": "Template path"}]
    ).model_dump(mode="json")
    args, data = await preview(handler, bundle, only=[entry.id])
    assert not any(row["selected"] for row in data["items"])
    _, undecided = await preview(
        handler, bundle, only=[entry.id], selections={entry.id: {"overwrite": True}}
    )
    assert not any(row["selected"] for row in undecided["items"])
    acknowledge(args, data)
    refused = await handler.execute("policy_apply", args)
    assert not refused["success"] and "fill placeholders" in refused["error"]
    assert not (vault / "projects/target/templates").exists()
    args, data = await preview(
        handler,
        bundle,
        values={"custom_path": "/target/path"},
        only=[entry.id],
        skip=[other.id],
        selections={entry.id: {"scope": "global"}},
    )
    acknowledge(args, data, scope="global")
    result = await handler.execute("policy_apply", args)
    assert result["success"] and result["applied"] == [entry.id]
    assert (vault / "system/templates/spec/base.md").read_text() == "/target/path"
    assert not (vault / "system/templates/plan/base.md").exists()


@pytest.mark.parametrize("suffix", ["", ".aqpolicy"])
def test_archive_exact_preview_roundtrip_and_tamper_checks(tmp_path, suffix):
    bundle = simple_bundle(
        item("spec.base", "project", TemplatePolicy(kind="spec", content="# Example"))
    )
    path = tmp_path / ("example" + suffix)
    assert write_bundle(str(path), bundle) == sorted(files(bundle))
    assert read_bundle(str(path)) == bundle and read_bundle(pack(bundle)) == bundle
    with pytest.raises(ValueError, match="already exists"):
        write_bundle(str(path), bundle)
    damaged = copy.deepcopy(bundle.model_dump(mode="json"))
    damaged["items"][0]["payload"]["content"] = "tampered"
    with pytest.raises(ValidationError, match="checksum"):
        Bundle.model_validate(damaged)
    damaged["items"][0]["payload"]["secret"] = "forbidden"
    with pytest.raises(ValidationError, match="Extra inputs"):
        Bundle.model_validate(damaged)


def test_archive_never_extracts_or_reads_undeclared_paths_and_rejects_symlinks(tmp_path):
    bundle = simple_bundle(item("spec.base", "project", TemplatePolicy(kind="spec", content="ok")))
    contents = files(bundle)
    manifest = json.loads(contents["manifest.json"])
    manifest["items"][0]["file"] = "../private.json"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("manifest.json", json.dumps(manifest))
        zipped.writestr("../private.json", contents["items/0000.json"])
    with pytest.raises(ValueError, match="invalid policy item"):
        read_bundle(archive.getvalue())
    root = tmp_path / "bundle"
    write_bundle(str(root), bundle)
    (root / "items/0000.json").unlink()
    (root / "items/0000.json").symlink_to(
        put(tmp_path, "outside.json", contents["items/0000.json"].decode())
    )
    with pytest.raises(ValueError, match="symlink"):
        read_bundle(str(root))


async def test_export_preview_acknowledgement_is_required_and_checked(env):
    handler, vault, tmp_path = env
    path = tmp_path / "export.aqpolicy"
    previewed = await handler.execute("policy_export", {"project_id": "source", "path": str(path)})
    assert previewed["success"] and not path.exists()
    put(vault / "projects/source", "templates/spec/base.md", "new policy")
    rejected = await handler.execute(
        "policy_export",
        {"project_id": "source", "path": str(path), "expected_checksum": previewed["checksum"]},
    )
    assert not rejected["success"] and not path.exists()
    previewed = await handler.execute("policy_export", {"project_id": "source", "path": str(path)})
    applied = await handler.execute(
        "policy_export",
        {"project_id": "source", "path": str(path), "expected_checksum": previewed["checksum"]},
    )
    assert applied["success"] and applied["written"] == sorted(
        files(Bundle.model_validate(previewed["bundle"]))
    )


@pytest.mark.parametrize("missing_source", [False, True])
async def test_typed_routes_support_dashboard_archive_and_operator_authority(env, missing_source):
    from src.api.app import create_app
    from src.api import dependencies as deps
    from src.commands.principal import ExecutionPrincipal, principal_context

    handler, vault, _ = env
    if missing_source:
        await reviewed(handler, vault, "api-lost-source")
        (await legacy_source_path(handler, "api-lost-source")).unlink()
        (vault / "system/playbooks/api-lost-source.md").unlink()
    saved = (
        deps._orchestrator,
        deps._command_handler,
        deps._token_store,
        deps._require_session_token,
    )
    try:
        app = create_app(handler.orchestrator, handler.config)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            exported = await client.post("/api/policy/export", json={"project_id": "source"})
            assert exported.status_code == 200, exported.text
            assert bool(exported.json()["warnings"]) == missing_source
            if missing_source:
                assert exported.json()["warnings"][0]["playbook_id"] == "api-lost-source"
            args = {"project_id": "target", "archive": exported.json()["archive"]}
            diff = await client.post("/api/policy/diff", json=args)
            assert diff.status_code == 200, diff.text
            data = diff.json()
            acknowledge(args, data)
            applied = await client.post("/api/policy/apply", json=args)
            assert applied.status_code == 200, applied.text
            invalid = await client.post(
                "/api/policy/diff", json={"project_id": "target", "archive": "bogus"}
            )
            assert invalid.status_code == 422
        with principal_context(ExecutionPrincipal.service("test")):
            refused = await handler._cmd_policy_export({"project_id": "source"})
        assert refused["success"] is False and "local operator" in refused["error"]
    finally:
        (
            deps._orchestrator,
            deps._command_handler,
            deps._token_store,
            deps._require_session_token,
        ) = saved


@pytest.mark.parametrize("inner_dependency_missing", [False, True])
async def test_export_omits_playbooks_with_unavailable_transitive_dependencies(env, inner_dependency_missing):
    from src.playbooks.artifact_store import ArtifactStore
    from src.playbooks.definition import load_definition_json

    handler, vault, _ = env
    store = ArtifactStore(handler.config.compiled_root)
    for identifier in ("outer", "middle", "inner"):
        await reviewed(handler, vault, identifier)
    references = [("outer", "middle"), ("middle", "inner")]
    if inner_dependency_missing:
        references.append(("inner", "not-installed"))
    for identifier, dependency in references:
        path = await legacy_source_path(handler, identifier)
        definition = load_definition_json(path.with_suffix(".json").read_text())
        artifact = definition.model_dump(mode="json", exclude_none=True)
        command = next(step for step in artifact["steps"].values() if step["type"] == "command")
        command["inputs"]["playbook_id"] = {"type": "literal", "value": dependency}
        definition = load_definition_json(json.dumps(artifact))
        ref = store.put(
            definition, source_digest=definition.source_hash,
            contract_fingerprint="sha256:" + "b" * 64,
            profile_fingerprint="profile", compiler_build="test",
        )
        store.put_source(ref.artifact_sha256, path.read_text())
        await handler.db.upsert_playbook_artifact(
            ref, scope="system", path=store.path_for(ref.artifact_sha256), size_bytes=1
        )
        await handler.db.set_playbook_activation(
            playbook_id=identifier, scope="system", scope_identifier="",
            artifact_sha256=ref.artifact_sha256, enabled=True,
            activated_by="test", health="ready", reasons="[]",
        )
    (await legacy_source_path(handler, "inner")).unlink()
    (vault / "system/playbooks/inner.md").unlink()
    result = await handler.execute("policy_export", {"project_id": "source"})
    assert result["success"], result
    assert not any(entry["type"] == "playbook" for entry in result["bundle"]["items"])
    assert {warning["playbook_id"] for warning in result["warnings"]} == {"outer", "middle", "inner"}
    assert all("recompile and reactivate" in warning["message"] for warning in result["warnings"])


async def test_imported_flow_activates_only_after_repository_trust_setup_and_existing_generation_fence(
    env,
):
    from unittest.mock import AsyncMock
    from src.integration.promotion_steps import FlowSchema
    from tests.test_promotion_flow import DEV, FakeGit, TRUST

    handler, _, _ = env
    bundle = simple_bundle(item("flow", "project", PromotionPolicy(flow=FLOW))).model_dump(
        mode="json"
    )
    args, data = await preview(handler, bundle)
    acknowledge(args, data)
    assert (await handler.execute("policy_apply", args))["success"]
    activation = {
        "project_id": "target",
        "promotion_flow": FLOW,
        "expected_integration_generation": 0,
        "reason": "activate reviewed imported flow",
    }
    refused = await handler.execute("edit_project", activation)
    assert not refused["success"] and refused["error"] == "integration_repository_required"
    await handler.db.create_repo(
        RepoConfig(
            id="target-repo",
            project_id="target",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/acme/target.git",
            default_branch="main",
        )
    )
    await handler.db.update_project("target", integration_repository_id="target-repo")
    handler.orchestrator.git = FakeGit({"main": DEV, "stable": DEV})
    handler._promotion_manifest = AsyncMock(return_value=TRUST)
    stale = await handler.execute(
        "edit_project", {**activation, "expected_integration_generation": 7}
    )
    assert not stale["success"] and stale["error"] == "integration_generation_stale"
    activated = await handler.execute("edit_project", activation)
    assert activated["success"] and activated["generation"] == 1, activated
    async with handler.db._engine.connect() as conn:
        stored = (
            await conn.execute(select(projects.c.promotion_flow).where(projects.c.id == "target"))
        ).scalar_one()
    assert stored == FlowSchema.validate(FLOW, default_branch="main", manifest=TRUST).flow


async def test_system_assignment_router_default_pipeline_and_routing_preferences_are_exported(env):
    handler, vault, _ = env
    from src.playbooks.definition import referenced_profile_ids
    from src.models import AgentProfile

    for identifier in ("default-assignment-routing", "default-pipeline"):
        fixture = Path("tests/fixtures/playbooks/v2") / identifier
        source = (fixture / "source.md").read_text()
        artifact = json.loads((fixture / "artifact.json").read_text())
        if identifier == "default-assignment-routing":
            for step in artifact["steps"].values():
                if step.get("command") == "task_route_plan":
                    step["inputs"]["policy"]["value"] += (
                        "risk:\n  high: {min_class: deep-high, harnesses: [codex]}\n"
                    )
        put(vault, f"system/playbooks/{identifier}.md", source)
        from src.playbooks.definition import load_definition_json

        for profile_id in referenced_profile_ids(load_definition_json(json.dumps(artifact))):
            await handler.db.upsert_profile(AgentProfile(id=profile_id, name=profile_id))
        submitted = await handler.execute(
            "review_submit",
            {
                "project_id": "source",
                "title": identifier,
                "content": source,
                "playbook_id": identifier,
                "activate_on_approval": True,
                "semantic_body": json.dumps(
                    {"rules": artifact["rules"], "steps": artifact["steps"]}
                ),
            },
        )
        assert submitted["success"], submitted
        approved = await handler.execute(
            "review_decide",
            {"review_id": submitted["review_id"], "revision": 1, "decision": "approve"},
        )
        assert approved["success"] and approved["playbook"]["activated"], approved
    exported = await handler.execute("policy_export", {"project_id": "source"})
    assert exported["success"], exported
    by_name = {entry["name"]: entry for entry in exported["bundle"]["items"]}
    for name in ("default-assignment-routing", "default-pipeline"):
        assert by_name[name]["type"] == "playbook" and by_name[name]["original_scope"] == "system"
    routing = by_name["preferences"]["payload"]["policy"]
    assert routing["lanes"] and routing["risk"] and routing["balance"]["harness_weights"]


async def test_project_profile_drafts_survive_startup_migration_without_global_changes(env):
    from src.policy_profiles.models import PolicyAgentSettings
    from src.profiles.project_override_migration import promote_project_profile_overrides

    handler, vault, _ = env
    global_path = put(
        vault,
        "agent-types/reviewer/profile.md",
        "---\nid: reviewer\nname: Reviewer\n---\n## Rules\nGlobal rules\n",
    )
    before = global_path.read_bytes()
    entry = item(
        "reviewer", "project", PolicyAgentSettings(name="Reviewer", rules="Project draft rules")
    )
    args, data = await preview(handler, simple_bundle(entry).model_dump(mode="json"))
    assert data["items"][0]["state"] == "pending configuration"
    acknowledge(args, data)
    applied = await handler.execute("policy_apply", args)
    assert applied["success"] and applied["pending_configuration"] == [entry.id]
    assert not (vault / "projects/target/agent-types").exists()
    promote_project_profile_overrides(handler.config.data_dir)
    assert global_path.read_bytes() == before
    assert (
        json.loads((vault / "projects/target/policy/agent_settings/reviewer.json").read_text())[
            "rules"
        ]
        == "Project draft rules"
    )
    assert await handler.db.get_profile("project:target:reviewer") is None


async def test_derived_profiles_export_authorable_templates_and_cannot_be_overwritten(env):
    from src.policy_profiles.models import PolicyAgentSettings

    handler, vault, _ = env
    put(
        vault, "projects/source/agent-types/generated-worker/profile.md", "---\nname: Worker\n---\n"
    )
    put(
        vault,
        "agent-types/worker-template/profile.md",
        "---\nid: worker-template\nname: Template\ntemplate: true\n---\n## Rules\nTemplate rules\n",
    )
    rung = put(
        vault,
        "agent-types/generated-worker/profile.md",
        "---\nid: generated-worker\nname: Worker\nextends: worker-template\ntags: [derived]\n---\n",
    )
    exported = await handler.execute("policy_export", {"project_id": "source"})
    assert exported["success"], exported
    ids = {entry["id"] for entry in exported["bundle"]["items"]}
    assert "agent_settings:system:worker-template" in ids
    assert "agent_settings:system:generated-worker" not in ids
    entry = item("generated-worker", "system", PolicyAgentSettings(name="Worker", rules="Change"))
    args, data = await preview(
        handler,
        simple_bundle(entry).model_dump(mode="json"),
        selections={entry.id: {"scope": "global"}},
    )
    acknowledge(args, data, scope="global", overwrite=True)
    before = rung.read_bytes()
    result = await handler.execute("policy_apply", args)
    assert not result["success"] and "derived profile" in result["error"]
    assert rung.read_bytes() == before


async def test_global_profile_import_validates_and_preserves_local_pool_account_sections(env):
    from src.policy_profiles.models import PolicyAgentSettings, AgentConfig
    from src.profiles.parser import parse_profile

    handler, vault, _ = env
    existing = put(
        vault,
        "agent-types/reviewer/profile.md",
        """---
id: reviewer
name: Reviewer
---
## Config
```json
{"harness":"codex","lifecycle":"pool","max_active":3,"enabled":false}
```
## Rules
Previous rules
## Reflection
Local personal reflection
""",
    )
    entry = item(
        "reviewer",
        "system",
        PolicyAgentSettings(
            name="Reviewer",
            config=AgentConfig(harness="codex", lifecycle="pool"),
            rules="Imported rules",
        ),
    )
    args, data = await preview(
        handler,
        simple_bundle(entry).model_dump(mode="json"),
        selections={entry.id: {"scope": "global"}},
    )
    acknowledge(args, data, scope="global", overwrite=True)
    applied = await handler.execute("policy_apply", args)
    assert applied["success"], applied
    parsed = parse_profile(existing.read_text())
    assert parsed.config["max_active"] == 3 and parsed.config["enabled"] is False
    assert parsed.rules == "Imported rules" and parsed.reflection == "Local personal reflection"
    assert (await handler.db.get_profile("reviewer")).max_active == 3
    bad = simple_bundle(
        item(
            "invalid",
            "system",
            PolicyAgentSettings(name="Bad", config=AgentConfig(permission_mode="invalid")),
        )
    ).model_dump(mode="json")
    result = await handler.execute(
        "policy_apply",
        {
            "project_id": "target",
            "bundle": bad,
            "selections": {"agent_settings:system:invalid": {"scope": "global"}},
        },
    )
    assert not result["success"] and not (vault / "agent-types/invalid/profile.md").exists()


async def test_global_profile_sync_failure_retains_draft_and_identical_retry_syncs(
    env, monkeypatch
):
    from src.policy_profiles.models import PolicyAgentSettings
    from src.profiles import sync

    handler, vault, _ = env
    entry = item("reviewer", "system", PolicyAgentSettings(name="Reviewer", rules="Portable rules"))
    args, data = await preview(
        handler,
        simple_bundle(entry).model_dump(mode="json"),
        selections={entry.id: {"scope": "global"}},
    )
    acknowledge(args, data, scope="global")
    normal_sync = sync.sync_profile_text_to_db

    async def unavailable(*args, **kwargs):
        return sync.ProfileSyncResult(
            success=False, action="none", errors=["temporary sync failure"]
        )

    monkeypatch.setattr(sync, "sync_profile_text_to_db", unavailable)
    failed = await handler.execute("policy_apply", args)
    assert not failed["success"] and failed["applied"] == [entry.id]
    assert failed["items"][0]["destinations"]
    assert (vault / "agent-types/reviewer/profile.md").exists()
    assert await handler.db.get_profile("reviewer") is None
    monkeypatch.setattr(sync, "sync_profile_text_to_db", normal_sync)
    args, data = await preview(handler, args["bundle"], selections=args["selections"])
    assert data["items"][0]["status"] == "identical"
    acknowledge(args, data, scope="global")
    retried = await handler.execute("policy_apply", args)
    assert retried["success"] and not retried["applied"]
    assert "Portable rules" in (await handler.db.get_profile("reviewer")).system_prompt_suffix
