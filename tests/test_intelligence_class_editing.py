"""Dashboard class editing: safe persistence, preservation, scope and live resolution."""

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.dependencies import get_command_handler
from src.api.scope import check_command_scope
from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.event_bus import EventBus
from src.intelligence_classes import load_intelligence_classes
from src.sessions.harness_parser import Harness
from src.sessions.spec import SessionSpecBuilder
from src.vault import ensure_default_intelligence_classes


@pytest.fixture
async def handler(tmp_path):
    config = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"), data_dir=str(tmp_path / "data")
    )
    ensure_default_intelligence_classes(config.data_dir)
    builder = SessionSpecBuilder(
        config, intelligence_classes=load_intelligence_classes(config.data_dir)
    )
    orch = SimpleNamespace(
        db=SimpleNamespace(), bus=EventBus(validate_events=False), session_spec_builder=builder
    )
    result = CommandHandler(orch, config)
    result.set_active_project(None)
    yield result
    result._current_scope = None
    result.set_active_project(None)


def class_path(handler, cid="fast-low"):
    return Path(handler.config.data_dir) / "vault" / "intelligence-classes" / f"{cid}.md"


async def payload(handler, cid="fast-low"):
    rows = (await handler._cmd_list_intelligence_classes({}))["classes"]
    row = next(row for row in rows if row["id"] == cid)
    assert row.get("revision"), "class editing needs a revision of the raw vault file"
    return {
        "class_id": cid,
        "name": row["name"],
        "description": row["description"],
        "mapping": row["mapping"],
        "expected_revision": row["revision"],
    }


async def edit(handler, args):
    method = getattr(handler, "_cmd_edit_intelligence_class", None)
    assert method is not None, "dashboard editing needs a persistence command"
    return await method(args)


async def test_list_revision_is_raw_file_hash(handler):
    args = await payload(handler)
    assert args["expected_revision"] == hashlib.sha256(class_path(handler).read_bytes()).hexdigest()


async def test_save_preserves_frontmatter_prose_unknown_options_and_real_id(handler):
    path = class_path(handler, "file-name")
    original = {
        "anthropic": {"model": "claude-sonnet-5", "thinking": "low", "future": {"keep": [1, None]}},
        "codex": None,
        "future-provider": {"model": "custom", "odd": True},
    }
    path.write_text(
        "---\nid: custom-id\nname: Old\ndescription: Old description\n# keep this comment\ntags: [custom, note]\ntier: user\n---\nBefore prose.\n```json\n"
        + json.dumps(original)
        + "\n```\nAfter prose.\n```text\nuntouched\n```\n"
    )
    args = await payload(handler, "custom-id")
    args.update(name="Before---after", description="A multiline\ndescription")
    args["mapping"]["future-provider"]["literal"] = "``` in a JSON string"
    result = await edit(handler, args)
    assert result["success"] and result["intelligence_class"]["id"] == "custom-id"
    saved = path.read_text()
    assert "# keep this comment" in saved and "tags: [custom, note]" in saved
    assert "tier: user" in saved and "Before prose.\n```json" in saved
    assert saved.endswith("After prose.\n```text\nuntouched\n```\n")
    fresh = load_intelligence_classes(handler.config.data_dir)["custom-id"]
    assert fresh.name == "Before---after" and fresh.description == args["description"]
    assert fresh.mapping == args["mapping"] and fresh.customized
    assert result["intelligence_class"]["revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not class_path(handler, "custom-id").exists()


async def test_explicit_legacy_selection_survives_reload_and_updates_shared_builder(handler):
    args = await payload(handler)
    args["mapping"] = {
        "anthropic": {"model": "claude-haiku-4-5", "thinking": "low"},
        "openai": {"model": "gpt-5-mini", "reasoning_effort": "low"},
        "codex": {},
    }
    builder = handler.orchestrator.session_spec_builder
    result = await edit(handler, args)
    assert result["success"]
    assert handler.orchestrator.session_spec_builder is builder
    fresh = load_intelligence_classes(handler.config.data_dir)["fast-low"]
    assert fresh.mapping == args["mapping"]
    assert builder._intelligence_classes["fast-low"].mapping == args["mapping"]
    profile = SimpleNamespace(id="worker", default_class="fast-low")
    assert (
        builder._resolve_model(profile, Harness(id="claude", command="claude"), None)
        == "claude-haiku-4-5"
    )


async def test_revision_conflict_does_not_overwrite_external_edit(handler):
    args = await payload(handler)
    path = class_path(handler)
    path.write_bytes(path.read_bytes() + b"\nExternal edit.\n")
    current = path.read_bytes()
    args["name"] = "Do not save"
    result = await edit(handler, args)
    assert result["error_code"] == "revision_conflict"
    assert result["current_revision"] == hashlib.sha256(current).hexdigest()
    assert path.read_bytes() == current


async def test_concurrent_edits_with_same_revision_have_one_winner(handler):
    args = await payload(handler)
    results = await asyncio.gather(
        edit(handler, {**args, "name": "One"}), edit(handler, {**args, "name": "Two"})
    )
    assert sum(bool(result.get("success")) for result in results) == 1
    assert sum(result.get("error_code") == "revision_conflict" for result in results) == 1
    saved = load_intelligence_classes(handler.config.data_dir)["fast-low"]
    assert handler.orchestrator.session_spec_builder._intelligence_classes["fast-low"] == saved


async def test_optional_revision_remains_compatible(handler):
    args = await payload(handler)
    args.pop("expected_revision")
    args["name"] = "Compatible caller"
    assert (await edit(handler, args))["success"]


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"name": 42},
        {"description": []},
        {"mapping": []},
        {"mapping": {"anthropic": {"model": 4}}},
        {"mapping": {"anthropic": {"model": "model", "thinking": "impossible"}}},
        {"mapping": {"codex": {"model": "model", "reasoning_effort": "impossible"}}},
        {"mapping": {"google": {"model": "model", "thinking_budget": True}}},
        {"mapping": {"other": {"model": "model", "value": float("nan")}}},
    ],
)
async def test_invalid_values_leave_file_unchanged(handler, change):
    args = await payload(handler)
    before = class_path(handler).read_bytes()
    result = await edit(handler, {**args, **change})
    assert "error" in result and class_path(handler).read_bytes() == before


async def test_existing_unknown_values_can_be_saved_unchanged(handler):
    path = class_path(handler, "custom")
    mapping = {
        "anthropic": {"model": "model", "thinking": "future-level"},
        "future": ["legacy"],
        "codex": {},
    }
    path.write_text("---\nid: custom\nname: Old\n---\n```json\n" + json.dumps(mapping) + "\n```\n")
    args = await payload(handler, "custom")
    args["name"] = "Rename only"
    assert (await edit(handler, args))["success"]
    assert load_intelligence_classes(handler.config.data_dir)["custom"].mapping == mapping
    args = await payload(handler, "custom")
    args["mapping"]["anthropic"]["thinking"] = "another-unknown"
    assert "error" in await edit(handler, args)


@pytest.mark.parametrize("cid", ["../outside", "missing", "/tmp/outside"])
async def test_only_existing_fenced_class_ids_can_be_edited(handler, cid):
    args = await payload(handler)
    before = sorted(class_path(handler).parent.iterdir())
    assert "error" in await edit(handler, {**args, "class_id": cid, "expected_revision": None})
    assert sorted(class_path(handler).parent.iterdir()) == before


async def test_symlink_outside_class_vault_cannot_be_edited(handler, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("---\nid: escape\nname: Original\n---\n```json\n{}\n```\n")
    original = outside.read_bytes()
    class_path(handler, "escape").symlink_to(outside)
    args = {"class_id": "escape", "name": "Changed", "description": "", "mapping": {}}
    assert "error" in await edit(handler, args)
    assert outside.read_bytes() == original


async def test_failed_atomic_replace_preserves_original_and_cleans_temp(handler, monkeypatch):
    args = await payload(handler)
    from src.intelligence_classes import editing

    before = class_path(handler).read_bytes()

    def failed(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(editing.os, "replace", failed)
    assert "error" in await edit(handler, {**args, "name": "Do not save"})
    assert class_path(handler).read_bytes() == before
    assert not list(class_path(handler).parent.glob("*.tmp"))


SCOPES = [
    RequestScope(kind="session", session_id="worker", project_id="p"),
    RequestScope(kind="session", session_id="project-supervisor", project_id="p", elevated=True),
    RequestScope(kind="session", session_id="worker", project_id=None),
    RequestScope(kind="session", session_id="worker", project_id=None, task_id="t", elevated=True),
]


@pytest.mark.parametrize("scope", SCOPES)
async def test_scope_is_enforced_for_http_gate_and_direct_dispatch(handler, scope):
    args = await payload(handler)
    before = class_path(handler).read_bytes()
    assert check_command_scope("edit_intelligence_class", dict(args), scope)
    result = await handler.execute("edit_intelligence_class", {**args, "_scope": vars(scope)})
    assert "global admin" in result["error"]
    handler._current_scope = vars(scope)
    assert "global admin" in (await edit(handler, args))["error"]
    assert class_path(handler).read_bytes() == before


async def test_global_admin_dispatch_can_edit(handler):
    args = await payload(handler)
    result = await handler.execute(
        "edit_intelligence_class",
        {
            **args,
            "_scope": {
                "kind": "session",
                "session_id": "supervisor-global",
                "elevated": True,
                "project_id": None,
                "task_id": None,
            },
        },
    )
    assert result["success"]


async def test_generated_http_route_roundtrips_and_reports_conflicts(handler):
    from src.api.codegen import build_category_routers

    app = FastAPI()
    scope = LOCAL_SCOPE

    @app.middleware("http")
    async def inject_scope(request: Request, call_next):
        request.state.scope = scope
        return await call_next(request)

    for router in build_category_routers():
        if router.prefix == "/api/system":
            app.include_router(router)
    app.dependency_overrides[get_command_handler] = lambda: handler
    spec = app.openapi()
    responses = spec["paths"]["/api/system/edit-intelligence-class"]["post"]["responses"]
    assert "409" in responses, "Generated clients need the revision conflict payload"
    conflict_ref = responses["409"]["content"]["application/json"]["schema"]["$ref"]
    conflict_schema = spec["components"]["schemas"][conflict_ref.rsplit("/", 1)[1]]
    assert set(conflict_schema["required"]) == {"error", "error_code", "current_revision"}
    assert conflict_schema["properties"]["error_code"]["const"] == "revision_conflict"
    assert "409" not in spec["paths"]["/api/system/list-intelligence-classes"]["post"]["responses"]
    args = await payload(handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/api/system/edit-intelligence-class", json=args)
        assert first.status_code == 200, first.text
        row = first.json()["intelligence_class"]
        assert row["revision"] and row["mapping"] == args["mapping"]
        assert "customized" not in row
        conflict = await client.post("/api/system/edit-intelligence-class", json=args)
        assert (
            conflict.status_code == 409 and conflict.json()["current_revision"] == row["revision"]
        )
        assert set(conflict.json()) == set(conflict_schema["required"])
        assert conflict.json()["error_code"] == "revision_conflict"
        scope = SCOPES[1]
        denied = await client.post(
            "/api/system/edit-intelligence-class",
            json={**args, "expected_revision": row["revision"]},
        )
        assert denied.status_code == 403


@pytest.mark.parametrize(
    "previous",
    [
        {"model": "claude-sonnet-5", "thinking": "off"},
        {"model": "claude-fable-5", "thinking": "low"},
    ],
)
async def test_new_fable_off_combination_is_rejected(handler, previous):
    path = class_path(handler, "custom")
    path.write_text(
        "---\nid: custom\nname: Original\n---\n```json\n"
        + json.dumps({"anthropic": previous})
        + "\n```\n"
    )
    args = await payload(handler, "custom")
    args["mapping"]["anthropic"].update(model="claude-fable-5", thinking="off")
    before = path.read_bytes()
    result = await edit(handler, args)
    assert "Fable" in result.get("error", "")
    assert path.read_bytes() == before


async def test_unchanged_legacy_fable_off_is_preserved_on_unrelated_edit(handler):
    path = class_path(handler, "custom")
    original = {"anthropic": {"model": "claude-fable-5", "thinking": "off"}}
    path.write_text(
        "---\nid: custom\nname: Original\n---\n```json\n" + json.dumps(original) + "\n```\n"
    )
    args = await payload(handler, "custom")
    args["name"] = "Renamed"
    args["mapping"]["anthropic"]["future"] = True
    assert (await edit(handler, args))["success"]
    assert load_intelligence_classes(handler.config.data_dir)["custom"].mapping == args["mapping"]


@pytest.mark.parametrize("budget", [0, 1])
async def test_google_boolean_budget_does_not_equal_previous_integer(handler, budget):
    path = class_path(handler, "custom")
    original = {"google": {"model": "custom", "thinking_budget": budget}}
    path.write_text(
        "---\nid: custom\nname: Original\n---\n```json\n" + json.dumps(original) + "\n```\n"
    )
    args = await payload(handler, "custom")
    args["mapping"]["google"]["thinking_budget"] = bool(budget)
    before = path.read_bytes()
    result = await edit(handler, args)
    assert "integer" in result.get("error", "")
    assert path.read_bytes() == before


async def test_cancelled_save_finishes_cache_publication(handler, monkeypatch):
    from src.intelligence_classes import editing

    args = await payload(handler)
    args["name"] = "Committed despite disconnect"
    committed = threading.Event()
    release = threading.Event()
    original = editing.edit_intelligence_class

    def delayed_return(*a, **kw):
        result = original(*a, **kw)
        committed.set()
        assert release.wait(3), "test did not release persistence thread"
        return result

    monkeypatch.setattr(editing, "edit_intelligence_class", delayed_return)
    task = asyncio.create_task(edit(handler, args))
    try:
        assert await asyncio.to_thread(committed.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            handler.orchestrator.session_spec_builder._intelligence_classes["fast-low"].name
            == args["name"]
        )
    finally:
        release.set()


@pytest.mark.parametrize("existing_model", [True, False])
async def test_missing_model_preserves_options_and_resolves_no_model(handler, existing_model):
    path = class_path(handler, "custom")
    options = {"thinking": "low", "future": {"keep": [None, "value"]}}
    original = {"anthropic": {"model": "claude-sonnet-5", **options}} if existing_model else {}
    path.write_text(
        "---\nid: custom\nname: Original\n---\n```json\n" + json.dumps(original) + "\n```\n"
    )
    args = await payload(handler, "custom")
    args["mapping"]["anthropic"] = options
    result = await edit(handler, args)
    assert result.get("success"), result
    assert result["intelligence_class"]["mapping"] == {"anthropic": options}
    fresh = load_intelligence_classes(handler.config.data_dir)["custom"]
    assert fresh.mapping == {"anthropic": options}
    builder = handler.orchestrator.session_spec_builder
    # A class slice with no ``model`` resolves to no launch model at all: the
    # per-profile ``model`` pin was removed, so there is nothing to fall back to.
    profile = SimpleNamespace(id="worker", default_class="custom")
    assert builder._resolve_model(profile, Harness(id="claude", command="claude"), None) == ""


@pytest.mark.parametrize("model", [" claude-sonnet-5", "claude-sonnet-5 "])
async def test_new_model_rejects_surrounding_whitespace(handler, model):
    args = await payload(handler)
    args["mapping"]["anthropic"]["model"] = model
    before = class_path(handler).read_bytes()
    result = await edit(handler, args)
    assert "whitespace" in result.get("error", "")
    assert class_path(handler).read_bytes() == before
