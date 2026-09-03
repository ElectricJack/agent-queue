"""Command-boundary tests for :class:`~src.commands.mcp_commands.McpCommandsMixin`.

`tests/test_mcp_registry.py` covers the parser/registry primitives but never
dispatches a command.  These tests drive the real ``_cmd_*`` methods against
the real in-memory registry/catalog created by ``Orchestrator.__init__`` and
assert the vault write, the eager registry/catalog sync, and the refusal
branches (test-coverage plan, commands 1–9).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from src.models import AgentProfile
from src.profiles.mcp_probe import ProbedTool, ProbeResult
from src.profiles.mcp_registry import McpServerConfig, render_server_markdown


@pytest.fixture
def probe_recorder(monkeypatch):
    """Replace the network probe used by ``refresh_one`` with a recorder."""
    calls: list[McpServerConfig] = []

    async def _fake_probe(config, timeout: float = 10.0):
        calls.append(config)
        return ProbeResult(
            server_name=config.name,
            transport=config.transport,
            tools=[ProbedTool(name="do_thing", description="d")],
            probed_at=1234.0,
            error=None,
        )

    monkeypatch.setattr("src.profiles.mcp_catalog.probe_server", _fake_probe)
    return calls


def _vault(handler) -> Path:
    return Path(handler.config.data_dir) / "vault"


def _write_server(handler, config: McpServerConfig) -> Path:
    """Write a server markdown file directly into the vault."""
    path = Path(handler._vault_mcp_server_path(config.name, config.project_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_server_markdown(config), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1–3: create
# ---------------------------------------------------------------------------


async def test_create_stdio_server_writes_vault_and_eagerly_registers(
    command_handler_factory, probe_recorder
):
    handler = await command_handler_factory()

    result = await handler._cmd_create_mcp_server(
        {
            "name": "files",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem"],
            "env": {"ROOT": "/srv"},
            "description": "filesystem tools",
            "notes": "hand-written notes",
        }
    )

    assert "error" not in result
    assert result["created"] == "files"
    assert result["scope"] == "system"
    assert result["project_id"] is None

    # Vault markdown at the system-scope path, with the args we sent.
    path = Path(result["path"])
    assert path == _vault(handler) / "mcp-servers" / "files.md"
    text = path.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---")[1])
    assert frontmatter["name"] == "files"
    assert frontmatter["transport"] == "stdio"
    assert frontmatter["command"] == "npx"
    assert frontmatter["args"] == ["-y", "@modelcontextprotocol/server-filesystem"]
    assert frontmatter["env"] == {"ROOT": "/srv"}
    assert "hand-written notes" in text

    # Eager registry upsert — visible before the watcher runs.
    entry = handler.orchestrator.mcp_registry.get("files")
    assert entry is not None
    assert entry.command == "npx"
    assert entry.env == {"ROOT": "/srv"}

    # Eager probe attempted, and its result landed in the catalog.
    assert [c.name for c in probe_recorder] == ["files"]
    cat = handler.orchestrator.mcp_tool_catalog.get("files")
    assert cat is not None
    assert [t.name for t in cat.tools] == ["do_thing"]


@pytest.mark.parametrize(
    ("patch", "expected_error"),
    [
        ({}, "http transport requires 'url'"),
        ({"url": "   "}, "http transport requires 'url'"),
        (
            {"url": "https://example.test/mcp", "headers": {"A": 1}},
            "'headers' must be an object of strings to strings",
        ),
        (
            {"url": "https://example.test/mcp", "headers": ["A", "B"]},
            "'headers' must be an object of strings to strings",
        ),
    ],
)
async def test_create_http_server_rejects_missing_url_and_bad_headers(
    command_handler_factory, probe_recorder, patch, expected_error
):
    handler = await command_handler_factory()

    args = {"name": "remote", "transport": "http", **patch}
    result = await handler._cmd_create_mcp_server(args)

    assert result == {"error": expected_error}
    # No vault file, no registry entry, no probe for a rejected create.
    assert not os.path.exists(_vault(handler) / "mcp-servers" / "remote.md")
    assert handler.orchestrator.mcp_registry.get("remote") is None
    assert probe_recorder == []


@pytest.mark.parametrize(
    ("patch", "expected_error"),
    [
        ({}, "stdio transport requires 'command'"),
        ({"command": "npx", "env": {"A": 3}}, "'env' must be an object of strings to strings"),
        ({"command": "npx", "env": {4: "A"}}, "'env' must be an object of strings to strings"),
        ({"command": "npx", "args": "not-a-list"}, "'args' must be a list of strings"),
        ({"command": "npx", "args": ["ok", 2]}, "'args' must be a list of strings"),
        ({"name": "", "command": "npx"}, "name is required"),
        ({"transport": "carrier-pigeon", "command": "npx"}, "transport must be 'stdio' or 'http'"),
    ],
)
async def test_create_stdio_server_rejects_missing_command_and_nonstring_env(
    command_handler_factory, probe_recorder, patch, expected_error
):
    handler = await command_handler_factory()

    args = {"name": "local", "transport": "stdio", **patch}
    result = await handler._cmd_create_mcp_server(args)

    assert result == {"error": expected_error}
    assert not os.path.exists(_vault(handler) / "mcp-servers" / "local.md")
    assert handler.orchestrator.mcp_registry.get("local") is None
    assert probe_recorder == []


# ---------------------------------------------------------------------------
# 4: read cascade
# ---------------------------------------------------------------------------


async def test_get_and_list_mcp_servers_honor_project_then_system_scope(
    command_handler_factory, probe_recorder
):
    handler = await command_handler_factory()
    registry = handler.orchestrator.mcp_registry
    catalog = handler.orchestrator.mcp_tool_catalog

    registry.upsert(
        McpServerConfig(
            name="shared", transport="stdio", command="shared-bin", description="system shared"
        )
    )
    registry.upsert(McpServerConfig(name="override", transport="stdio", command="system-bin"))
    registry.upsert(
        McpServerConfig(
            name="override",
            transport="http",
            project_id="alpha",
            url="https://alpha.test/mcp",
            headers={"X-Key": "k"},
        )
    )
    registry.upsert(
        McpServerConfig(name="beta-only", transport="stdio", project_id="beta", command="beta-bin")
    )
    await handler._cmd_probe_mcp_server({"name": "shared"})

    # System scope sees only system entries.
    system = await handler._cmd_list_mcp_servers({})
    assert sorted(s["name"] for s in system["servers"]) == ["override", "shared"]
    assert system["count"] == 2

    # Project scope: project entries plus inherited system ones, with the
    # project's own "override" winning.
    alpha = await handler._cmd_list_mcp_servers({"project_id": "alpha"})
    by_name = {s["name"]: s for s in alpha["servers"]}
    assert sorted(by_name) == ["override", "shared"]
    assert by_name["override"]["transport"] == "http"
    assert by_name["override"]["project_id"] == "alpha"
    # Another project's entry never leaks in.
    assert "beta-only" not in by_name

    # Catalog metadata is merged onto the inherited system entry.
    assert by_name["shared"]["tool_count"] == 1
    assert by_name["shared"]["last_probed_at"] == 1234.0
    assert by_name["shared"]["last_error"] is None
    assert catalog.get("shared") is not None

    # get() returns the project override with adapter + inline fields.
    got = await handler._cmd_get_mcp_server({"name": "override", "project_id": "alpha"})
    assert got["adapter_config"] == {
        "type": "http",
        "url": "https://alpha.test/mcp",
        "headers": {"X-Key": "k"},
    }
    assert got["url"] == "https://alpha.test/mcp"
    assert got["headers"] == {"X-Key": "k"}
    assert got["command"] == ""

    # get() with no project falls through to the system entry of the same name.
    system_got = await handler._cmd_get_mcp_server({"name": "override"})
    assert system_got["adapter_config"] == {"command": "system-bin", "args": []}

    assert await handler._cmd_get_mcp_server({"name": ""}) == {"error": "name is required"}
    assert await handler._cmd_get_mcp_server({"name": "nope"}) == {
        "error": "MCP server 'nope' not found"
    }


# ---------------------------------------------------------------------------
# 5: probe
# ---------------------------------------------------------------------------


async def test_probe_mcp_server_requires_registry_catalog_and_existing_name(
    command_handler_factory, probe_recorder
):
    handler = await command_handler_factory()
    orch = handler.orchestrator
    registry, catalog = orch.mcp_registry, orch.mcp_tool_catalog

    assert await handler._cmd_probe_mcp_server({"name": " "}) == {"error": "name is required"}

    orch.mcp_registry = None
    assert await handler._cmd_probe_mcp_server({"name": "files"}) == {
        "error": "MCP registry/catalog not initialised"
    }
    orch.mcp_registry = registry

    orch.mcp_tool_catalog = None
    assert await handler._cmd_probe_mcp_server({"name": "files"}) == {
        "error": "MCP registry/catalog not initialised"
    }
    orch.mcp_tool_catalog = catalog

    assert await handler._cmd_probe_mcp_server({"name": "files"}) == {
        "error": "MCP server 'files' not found"
    }
    assert probe_recorder == []

    registry.upsert(McpServerConfig(name="files", transport="stdio", command="npx"))
    result = await handler._cmd_probe_mcp_server({"name": "files"})

    probed = result["probed"]
    assert probed["server_name"] == "files"
    assert probed["scope"] == "system"
    assert probed["tool_count"] == 1
    assert probed["tools"] == [{"name": "do_thing", "description": "d", "input_schema": {}}]
    assert probed["ok"] is True
    assert [c.name for c in probe_recorder] == ["files"]
    assert catalog.get("files").tools[0].name == "do_thing"


# ---------------------------------------------------------------------------
# 6: edit
# ---------------------------------------------------------------------------


async def test_edit_mcp_server_merges_patch_and_rejects_malformed_existing_markdown(
    command_handler_factory, probe_recorder
):
    handler = await command_handler_factory()
    registry = handler.orchestrator.mcp_registry

    assert await handler._cmd_edit_mcp_server({"name": ""}) == {"error": "name is required"}
    assert await handler._cmd_edit_mcp_server({"name": "ghost"}) == {
        "error": "MCP server 'ghost' not found in vault (scope=system)"
    }

    original = McpServerConfig(
        name="files",
        transport="stdio",
        command="npx",
        args=["-y", "server"],
        env={"ROOT": "/srv"},
        description="original description",
        notes="original notes",
    )
    path = _write_server(handler, original)

    result = await handler._cmd_edit_mcp_server({"name": "files", "description": "patched"})

    assert "error" not in result
    assert result["updated"] == "files"
    assert result["scope"] == "system"

    # Unpatched fields are retained; the patched one changed.
    merged = registry.get("files")
    assert merged.description == "patched"
    assert merged.command == "npx"
    assert merged.args == ["-y", "server"]
    assert merged.env == {"ROOT": "/srv"}
    on_disk = yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])
    assert on_disk["description"] == "patched"
    assert on_disk["command"] == "npx"

    # The edit re-probes eagerly.
    assert [c.name for c in probe_recorder] == ["files"]

    # A patch that breaks validation is rejected and does not rewrite the file.
    before = path.read_text(encoding="utf-8")
    bad = await handler._cmd_edit_mcp_server({"name": "files", "command": ""})
    assert bad == {"error": "stdio transport requires 'command'"}
    assert path.read_text(encoding="utf-8") == before

    # Malformed existing markdown is reported, not silently overwritten.
    path.write_text("---\nnot: [valid\n", encoding="utf-8")
    malformed = await handler._cmd_edit_mcp_server({"name": "files", "description": "x"})
    assert "Existing markdown is malformed" in malformed["error"]
    assert path.read_text(encoding="utf-8") == "---\nnot: [valid\n"


# ---------------------------------------------------------------------------
# 7–8: delete
# ---------------------------------------------------------------------------


async def test_delete_mcp_server_refuses_builtin_and_referenced_profiles(
    command_handler_factory, probe_recorder
):
    handler = await command_handler_factory()
    registry = handler.orchestrator.mcp_registry

    assert await handler._cmd_delete_mcp_server({"name": ""}) == {"error": "name is required"}

    # Builtin: refused before anything is touched.
    registry.set_builtin(McpServerConfig(name="agent-queue", transport="stdio", command="aq"))
    refused = await handler._cmd_delete_mcp_server({"name": "agent-queue"})
    assert refused["error"] == "Cannot delete 'agent-queue' — it's the embedded agent-queue server"
    assert registry.get("agent-queue") is not None

    # Referenced by a profile: refused, with the referencing profile ids.
    config = McpServerConfig(name="files", transport="stdio", command="npx")
    path = _write_server(handler, config)
    registry.upsert(config)
    await handler.db.create_profile(
        AgentProfile(id="reviewer", name="Reviewer", mcp_servers=["files"])
    )
    await handler.db.create_profile(
        AgentProfile(id="planner", name="Planner", mcp_servers=["other"])
    )

    result = await handler._cmd_delete_mcp_server({"name": "files"})
    assert result["referenced_by"] == ["reviewer"]
    assert "still referenced by 1 profile(s)" in result["error"]
    assert path.exists()
    assert registry.get("files") is not None

    # Creating over a reserved builtin name is refused too.
    created = await handler._cmd_create_mcp_server(
        {"name": "agent-queue", "transport": "stdio", "command": "x"}
    )
    assert "reserved by the embedded agent-queue server" in created["error"]

    # And creating a name that already has a vault file is refused.
    dup = await handler._cmd_create_mcp_server(
        {"name": "files", "transport": "stdio", "command": "x"}
    )
    assert dup["error"] == "MCP server 'files' already exists (scope=system)"


async def test_delete_mcp_server_removes_vault_registry_and_catalog(
    command_handler_factory, probe_recorder
):
    handler = await command_handler_factory()
    registry = handler.orchestrator.mcp_registry
    catalog = handler.orchestrator.mcp_tool_catalog

    created = await handler._cmd_create_mcp_server(
        {
            "name": "files",
            "transport": "stdio",
            "command": "npx",
            "project_id": "alpha",
        }
    )
    path = Path(created["path"])
    assert path == _vault(handler) / "projects" / "alpha" / "mcp-servers" / "files.md"
    assert registry.get("files", project_id="alpha") is not None
    assert catalog.get("files", project_id="alpha") is not None

    # Profiles are global, so any profile naming the server blocks the delete —
    # the reference cannot be attributed to one project's copy of it.
    await handler.db.create_profile(
        AgentProfile(id="reviewer", name="Reviewer", mcp_servers=["files"])
    )
    blocked = await handler._cmd_delete_mcp_server({"name": "files", "project_id": "alpha"})
    assert blocked["referenced_by"] == ["reviewer"]
    await handler.db.delete_profile("reviewer")

    result = await handler._cmd_delete_mcp_server({"name": "files", "project_id": "alpha"})

    assert result["deleted"] == "files"
    assert result["scope"] == "project"
    assert not path.exists()
    assert registry.get("files", project_id="alpha") is None
    assert catalog.get("files", project_id="alpha") is None

    # Second delete: file is gone.
    again = await handler._cmd_delete_mcp_server({"name": "files", "project_id": "alpha"})
    assert again == {"error": "MCP server 'files' not found"}


# ---------------------------------------------------------------------------
# 9 [inline]: CMD-1 / CMD-2 — path traversal in name / project_id
# ---------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "../escape"},
        {"name": "a/../../escape"},
        {"name": "sub/escape"},
        {"name": "/abs-escape"},
        {"project_id": "../../escape"},
        {"project_id": "a/../.."},
        {"project_id": "/abs"},
    ],
)
async def test_mcp_name_and_project_id_traversal_are_rejected(
    command_handler_factory, probe_recorder, tmp_path, bad
):
    """CMD-1 / CMD-2: traversal ids must never touch a path outside the roots."""
    handler = await command_handler_factory()

    # Plant a sentinel outside mcp-servers/ and outside any project directory.
    vault = _vault(handler)
    (vault / "mcp-servers").mkdir(parents=True, exist_ok=True)
    (vault / "projects" / "alpha" / "mcp-servers").mkdir(parents=True, exist_ok=True)
    sentinel = vault / "escape.md"
    sentinel.write_text("sentinel", encoding="utf-8")
    project_sentinel = vault / "projects" / "escape.md"
    project_sentinel.write_text("project sentinel", encoding="utf-8")

    before = _snapshot(vault)

    base = {"name": "safe", "transport": "stdio", "command": "npx", "project_id": "alpha"}
    args = {**base, **bad}

    for cmd in (
        handler._cmd_create_mcp_server,
        handler._cmd_edit_mcp_server,
        handler._cmd_delete_mcp_server,
    ):
        result = await cmd(dict(args))
        assert result.get("success") is not True, f"{cmd.__name__} accepted {bad}"
        assert "error" in result, f"{cmd.__name__} accepted {bad}: {result}"
        assert "invalid" in result["error"].lower() or "must" in result["error"].lower(), result

    # Nothing anywhere in the vault was created, modified, or deleted.
    assert _snapshot(vault) == before
