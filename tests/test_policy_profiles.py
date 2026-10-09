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
from src.policy_profiles.models import Bundle, PromotionPolicy, TemplatePolicy
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


async def test_typed_routes_support_dashboard_archive_and_operator_authority(env):
    from src.api.app import create_app
    from src.api import dependencies as deps
    from src.commands.principal import ExecutionPrincipal, principal_context

    handler, _, _ = env
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
